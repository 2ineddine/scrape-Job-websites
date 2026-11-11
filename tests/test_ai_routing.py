"""Automatic model switching: a misbehaving model is paused, parallel requests use the models in their rank order, and
the analysis runs several offers at a time (jobfinder/llm.py, jobfinder/analysis.py)."""
import threading
import time

from test_platforms import _ladder

BEST = ("gemini", "gemini-3.6-flash", "gemini")


def test_a_model_giving_unusable_answers_is_paused_after_three_in_a_row(monkeypatch):
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"): "not json"})
    assert llm.call_json("u1", A, cfg, s, keys).ok  # two bad answers (one retry), then the second key
    assert llm.call_json("u2", A, cfg, s, keys).ok  # the third bad answer: paused, no retry
    assert llm._EXHAUSTED[BEST] > time.time() + 500
    calls.clear()
    assert llm.call_json("u3", A, cfg, s, keys).ok
    assert calls == [("gemini", "gemini-3.6-flash", "g2")]  # not asked any more: no wasted call
    # the activity log adds up the model's keys: 3 unusable answers on the first key, 3 good ones on the second
    assert [r[:4] for r in llm.health_report()][0] == ("gemini", "gemini-3.6-flash", 3, 3)


def test_a_good_answer_resets_the_count_of_unusable_ones(monkeypatch):
    answers = iter(["not json", '{"ok": true}', "not json", '{"ok": true}', "not json", '{"ok": true}'])
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (next(answers), 1, 1))
    for i in range(3):
        assert llm.call_json(f"u{i}", A, cfg, s, keys).ok  # one slip, then fixed by the retry: never 3 in a row
    assert BEST not in llm._EXHAUSTED


def test_a_model_that_is_slow_twice_in_a_row_is_paused(monkeypatch):
    llm, *_ = _ladder(monkeypatch, {})
    llm._went_well(BEST, 50)
    assert BEST not in llm._EXHAUSTED
    llm._went_well(BEST, 52)
    assert llm._EXHAUSTED[BEST] > time.time() + 500


def test_parallel_requests_go_to_the_next_model_in_rank_when_the_best_is_busy(monkeypatch):
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {})
    inner = llm.chat

    def slow(*a, **k):
        time.sleep(0.3)
        return inner(*a, **k)

    monkeypatch.setattr(llm, "chat", slow)
    cfg = {**cfg, "ai_per_model": 1}
    threads = [threading.Thread(target=llm.call_json, args=(f"u{i}", A, cfg, s, keys)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # one request per model at once: the three best places of the ladder, in their order, all at the same time
    assert sorted(calls) == sorted([("gemini", "gemini-3.6-flash", "g1"), ("gemini", "gemini-3.6-flash", "g2"),
                                    ("gemini", "gemini-3.5-flash", "g1")])


def test_the_analysis_runs_several_offers_at_a_time(monkeypatch, tmp_path):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import analysis, store
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    from jobfinder.scan import Progress
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    store.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(analysis, "SessionLocal", Session)
    text = ("Vous concevrez des modèles de vision par ordinateur pour la robotique, en Python et PyTorch. " * 12)
    with Session() as s:
        for i in range(8):
            s.add(store.Job(uid=f"j{i}", title=f"Ingénieur Vision {i}", url=f"https://a.fr/{i}", status="candidate",
                            description=text, pub_date=datetime.utcnow(), date_conf=0.95, date_source="json-ld",
                            location_original="Paris, France", location_verified=True, france_eligible=True))
        s.commit()
    live, peak, lock = [0], [0], threading.Lock()

    def fake(user, schema, cfg, s, keys=None, system=None):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.1)
        with lock:
            live[0] -= 1
        return schema(is_relevant=True, relevance_score=80, analysis_confidence=0.9, france_eligible=True,
                      location_evidence="Paris, France")

    monkeypatch.setattr(analysis, "call_json", fake)
    prog = Progress()
    analysis._pipeline(run_config(CONFIG_DEFAULT), PROFILE_DEFAULT, {}, prog, rend=None)
    with Session() as s:
        assert s.query(store.Job).filter(store.Job.relevance_score.isnot(None)).count() == 8
    assert prog.snap()[0]["ai_done"] == 8 and peak[0] > 1  # several offers at once


def _borderline_case(monkeypatch, scores):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import analysis, store
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    seen = []

    def fake(user, schema, cfg, s, keys=None, system=None):
        seen.append("SECOND READING" in user)
        return schema(is_relevant=scores[len(seen) - 1] >= 60, relevance_score=scores[len(seen) - 1], analysis_confidence=0.9,
                      france_eligible=True, location_evidence="Paris, France")
    monkeypatch.setattr(analysis, "call_json", fake)
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        text = "Vous développerez des outils de supervision réseau pour nos clients, en Java et en Python. " * 12
        j = store.Job(uid="b1", title="Ingénieur logiciel", url="https://a.fr/1", status="candidate", description=text,
                      location_original="Paris, France", location_verified=True, france_eligible=True)
        s.add(j); s.commit()
        analysis._analyze_deep(s, j, PROFILE_DEFAULT, run_config(CONFIG_DEFAULT), {}, None, None, lambda m: None)
        return j.status, j.review_flags or "", seen


def test_a_borderline_exclusion_read_relevant_the_second_time_goes_to_check(monkeypatch):
    status, flags, seen = _borderline_case(monkeypatch, [40, 85])
    assert seen == [False, True] and status == "review" and "AI_readings_disagree" in flags


def test_a_borderline_exclusion_confirmed_by_the_second_reading_stays_excluded(monkeypatch):
    status, _, seen = _borderline_case(monkeypatch, [40, 30])
    assert len(seen) == 2 and status == "excluded"


def test_a_clear_exclusion_is_not_read_twice(monkeypatch):
    status, _, seen = _borderline_case(monkeypatch, [10])
    assert seen == [False] and status == "excluded"


def test_job_pages_of_a_website_that_does_not_answer_are_not_opened_again(monkeypatch):
    """candidat.francetravail.fr timed out for 125 offers in a row (~100 s each, plus the browser): after two failures the
    website's pages are skipped and the offers analysed with their text."""
    from jobfinder import browser, store
    monkeypatch.setattr(browser, "_DOWN", {})
    calls = []

    class F:
        def get(self, url, **k):
            calls.append(url)
            raise ConnectionError("source_unreachable: ConnectTimeout")

        def note(self, m):
            calls.append("note")

    class R:  # the browser must not be tried for an unreachable server
        def get(self, url):
            raise AssertionError("browser opened")

    class S:
        def commit(self):
            pass

    for i in range(5):
        j = store.Job(uid=f"d{i}", title="Ingénieur", url=f"https://candidat.francetravail.fr/offres/{i}", description="x")
        assert browser.deepen_job(S(), j, {"nav_mode": "auto"}, F(), R()) is False
    assert [c for c in calls if c != "note"] == [f"https://candidat.francetravail.fr/offres/{i}" for i in range(2)]
    assert browser.host_down("candidat.francetravail.fr")
