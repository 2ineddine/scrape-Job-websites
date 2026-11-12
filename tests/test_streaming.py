"""The AI analyses each website's offers while the next websites are read, and no offer is lost on the way: a failed
reading is tried again, the offers wait when every model is at its limit, and a small model's reading is read again by a
better one (jobfinder/scan.py run_scan, jobfinder/analysis.py _pipeline)."""
import threading
import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobfinder import analysis, llm, scan, store
from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config

TEXT = ("Vous concevrez des modèles de vision par ordinateur pour la robotique, en Python et PyTorch, à Paris, France. " * 12)


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    store.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    for mod in (analysis, scan):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(llm, "_LAST", threading.local())
    return Session


def _job(i, title="Ingénieur Vision", status="candidate", source=None, employer=None):
    return store.Job(uid=f"j{i}-{source}", title=title, url=f"https://a{source}.fr/{i}", status=status, description=TEXT,
                     pub_date=datetime.utcnow(), date_conf=0.95, date_source="json-ld", source_id=source,
                     employer=employer or f"Employer {i}", location_original="Paris, France", location_verified=True,
                     france_eligible=True)


def _answer(schema, score=80):
    return schema(is_relevant=score >= 60, relevance_score=score, analysis_confidence=0.9, france_eligible=True,
                  location_evidence="Paris, France")


def _run(Session, n, fake):
    with Session() as s:
        for i in range(n):
            s.add(_job(i))
        s.commit()
    prog = scan.Progress()
    analysis._pipeline(run_config(CONFIG_DEFAULT), PROFILE_DEFAULT, {}, prog, rend=None)
    with Session() as s:
        return prog, {j.uid: j for j in s.query(store.Job)}


def test_the_ai_reads_a_website_s_offers_while_the_next_websites_are_read(db, monkeypatch):
    """Website A is read in 0.1 s, website B in 1.5 s: A's offers are analysed before B is finished. A business title is
    filtered out without an AI call, and the same offer on two websites is shown once."""
    with db() as s:
        for k in range(3):
            s.add(store.Source(url=f"https://site{k}.fr/careers", label=f"Site {k}"))
        s.commit()
        ids = [x.id for x in s.query(store.Source)]
    ended, asked = {}, []

    def fake_source(sid, cfg, fetcher, prog, t0):
        time.sleep({ids[0]: 0.1, ids[1]: 1.5, ids[2]: 0.2}[sid])
        with db() as s:
            s.add(_job(1, status="new", source=sid, employer="Exemplia" if sid != ids[1] else None,
                       title="Ingénieur Vision" if sid != ids[1] else "Ingénieur Audio"))
            s.add(_job(2, "Commercial grands comptes", status="new", source=sid))
            s.commit()
        ended[sid] = time.time()

    def fake_ai(user, schema, cfg, s, keys=None, system=None):
        asked.append((time.time(), user))
        return _answer(schema)

    monkeypatch.setattr(scan, "scan_source", fake_source)
    for name in ("lock_scan", "unlock_scan"):
        monkeypatch.setattr(scan, name, lambda: None)
    monkeypatch.setattr(scan, "keep_recent_offers", lambda s: 0)
    monkeypatch.setattr(analysis, "call_json", fake_ai)
    prog = scan.Progress()
    cfg = {**CONFIG_DEFAULT, "date_preset": "Last 7 days"}
    scan.run_scan(ids, cfg, PROFILE_DEFAULT, prog,
                  lambda pr, feed: analysis._pipeline(run_config(cfg), PROFILE_DEFAULT, {}, pr, None, feed))
    assert asked and asked[0][0] < ended[ids[1]]  # the AI started before the slow website was read
    assert not any("Commercial" in u for _, u in asked)
    with db() as s:
        jobs = s.query(store.Job).all()
        vision = [j for j in jobs if j.title == "Ingénieur Vision"]
        assert sorted(j.status for j in vision) == ["excluded", "relevant"]  # the copy on the second website: merged
        assert any((j.reason or "").startswith("duplicate") for j in vision)
        assert [j.status for j in jobs if j.title == "Ingénieur Audio"] == ["relevant"]
    d = prog.snap()[0]
    assert d["ai_done"] == d["ai_total"] and not d["running"]


def test_a_failed_reading_is_read_again_after_the_others(db, monkeypatch):
    failed = []

    def fake(user, schema, cfg, s, keys=None, system=None):
        if not failed:
            failed.append(1)
            raise llm.MalformedOutput("not JSON")
        return _answer(schema)

    monkeypatch.setattr(analysis, "call_json", fake)
    prog, jobs = _run(db, 3, fake)
    assert all(j.relevance_score is not None for j in jobs.values())
    d = prog.snap()[0]
    assert d["ai_done"] == d["ai_total"] == 3


def test_when_every_model_is_at_its_limit_the_offers_wait_instead_of_being_lost(db, monkeypatch):
    refusals = []

    def fake(user, schema, cfg, s, keys=None, system=None):
        if len(refusals) < 2:
            refusals.append(time.time())
            e = llm.QuotaExhausted("All models have reached their limit")
            e.wait = 0.5
            raise e
        return _answer(schema)

    monkeypatch.setattr(analysis, "call_json", fake)
    prog, jobs = _run(db, 4, fake)
    assert all(j.status == "relevant" for j in jobs.values())
    assert "stopped" not in prog.snap()[0]["ai_msg"]


def test_a_long_quota_out_stops_the_analysis_with_a_message(db, monkeypatch):
    def fake(user, schema, cfg, s, keys=None, system=None):
        e = llm.QuotaExhausted("All models have reached their limit: free again in about 300 min")
        e.wait = 18000
        raise e

    monkeypatch.setattr(analysis, "call_json", fake)
    prog, jobs = _run(db, 3, fake)
    assert "stopped" in prog.snap()[0]["ai_msg"]
    assert all(j.relevance_score is None for j in jobs.values())  # left 'Not analysed', not excluded


def test_a_small_model_s_reading_is_read_again_by_a_better_model(db, monkeypatch):
    weak, strong = ("groq", "openai/gpt-oss-20b", "groq"), ("gemini", "gemini-3.6-flash", "gemini")

    def fake(user, schema, cfg, s, keys=None, system=None):
        better = cfg.get("_max_level")
        llm._LAST.step = strong if better else weak
        return _answer(schema, 85 if better else 30)

    monkeypatch.setattr(analysis, "call_json", fake)
    prog, jobs = _run(db, 2, fake)
    for j in jobs.values():
        assert j.status == "relevant" and j.extra["ai_model"] == "gemini / gemini-3.6-flash"
        assert j.extra["first_reading"] == "groq / openai/gpt-oss-20b: 30"


def test_the_feed_hands_each_offer_over_once_and_the_best_first(db):
    feed = scan.Feed()
    with db() as s:
        a, b = _job(1), _job(2, title="Technicien support")
        a.extra, b.extra = {"title_info": {"verdict": "pass"}}, {"title_info": {"verdict": "maybe"}}
        s.add_all([a, b])
        s.commit()
        assert feed.put([b, a]) == 2 and feed.put([a]) == 0  # pending: not twice
        assert feed.get() == (0, a.id) and feed.get() == (0, b.id) and feed.get() is None
        feed.again(a.id, scan.Feed.AGAIN)
        feed.done(b.id)
        assert feed.put([b]) == 1  # done: may be handed over again
        assert feed.get() == (0, b.id) and feed.get() == (1, a.id)


def _read_pages(db, monkeypatch, pages, website_sort=False):
    """scan_source over a fake reader yielding these pages (lists of publication dates): -> the pages it asked for."""
    from jobfinder.adapters import Page, _raw, make_gate
    with db() as s:
        src = store.Source(url=f"https://s{time.time_ns()}.fr/offres", label="S")
        s.add(src)
        s.commit()
        sid = src.id
    asked = []

    def fake_crawl(f, url, cfg, known, st):
        for k, dates in enumerate(pages):
            asked.append(k)
            yield Page(f"p{k}", url, [_raw(f"Ingénieur vision {k}-{i}", f"{url}/o/{k}-{i}", date=d, date_conf=0.9,
                                          date_source="json-ld", date_text=d.isoformat(), description=TEXT, location="Paris")
                                     for i, d in enumerate(dates)], sorted=website_sort)
    monkeypatch.setattr(scan, "crawl_source", fake_crawl)
    cfg = run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"})
    cfg["_gate"] = make_gate(cfg)
    scan.scan_source(sid, cfg, None, scan.Progress(), time.time())
    return asked


def test_a_list_is_left_at_its_old_pages_only_when_its_dates_prove_it_newest_first(db, monkeypatch):
    from datetime import timedelta
    new, old = datetime.utcnow() - timedelta(hours=5), datetime.utcnow() - timedelta(days=40)
    # newest first: two pages of old offers end the list (the fourth page is never asked for)
    assert _read_pages(db, monkeypatch, [[new, new, new], [old, old], [old], [new]]) == [0, 1, 2]
    # Choisir le service public: old and new offers mixed on every page; two pages of old ones prove nothing
    assert _read_pages(db, monkeypatch, [[new, old, new, old, new], [old, old], [old], [new]]) == [0, 1, 2, 3]
    from jobfinder.adapters import date_order
    assert date_order([new, new]) is None and date_order([new, old, old]) is True and date_order([old, new, old, new]) is False
    assert date_order([old, new, new, new]) is True  # an old offer pinned at the top
    days = lambda *ds: [datetime(2026, 11, d) for d in ds]  # Randstad Digital's first ten offers, 'sorted by date'
    assert date_order(days(9, 10, 10, 10, 9, 9, 6, 9, 3, 1)) is False


def test_a_website_sort_is_trusted_only_once_it_holds_across_pages(db, monkeypatch):
    from datetime import timedelta
    new, old = datetime.utcnow() - timedelta(hours=5), datetime.utcnow() - timedelta(days=40)
    # sorted by the website: the first old offer ends the list, once the order held from one page to the next
    assert _read_pages(db, monkeypatch, [[new, new, old], [old, old], [new]], website_sort=True) == [0, 1]
    # Randstad Digital: each page sorted by itself, the next page starts with new offers again: every page is read
    assert _read_pages(db, monkeypatch, [[new, new, old], [new, new, old], [new]], website_sort=True) == [0, 1, 2]


def test_websites_that_did_not_work_are_explained():
    from types import SimpleNamespace as S
    from jobfinder.scan import PROBLEM_ORDER, site_problem
    site = lambda **k: S(**{"status": "completed", "outcome": "offers_found", "last_error": "", "health": "", **k})
    p = site_problem(site(status="blocked", last_error="access_restricted: HTTP 403 / anti-bot page - inspect manually"))
    assert p[0] == "Blocked" and p[1].startswith("HTTP 403") and "open job platform" in p[2]
    p = site_problem(site(status="blocked", last_error="access_restricted: disallowed by robots.txt"), respect_robots=True)
    assert "switch 'Respect robots.txt' off" in p[2]
    p = site_problem(site(status="failed", last_error="HTTP 404; possible new address: https://lisn.fr/nous-rejoindre"))
    assert p[0] == "Page not found" and p[2] == "Replace the address with https://lisn.fr/nous-rejoindre"
    assert site_problem(site(status="failed", last_error="source_unreachable: ConnectTimeout"))[0] == "Not reachable"
    assert site_problem(site(status="failed", last_error="talentview reader: HTTP 503"))[0] == "Website error"
    assert site_problem(site(status="partially_completed", last_error="time limit for this website reached"))[0] == "Partly read"
    assert site_problem(site(outcome="no_offers_detected"))[0] == "No offer found"
    assert site_problem(site(outcome="partial", last_error="the website announces 707 offers, 85 were read"))[0] == "Possibly incomplete"
    assert site_problem(site(health="2 offers on its lists, 55 on 2026-11-11: the website may have changed"))[0] == \
        "Changed since the last scan"
    p = site_problem(site(last_error="page moved: read at https://lisn.fr/nous-rejoindre (update this website's address)"))
    assert p[0] == "Address moved" and "https://lisn.fr/nous-rejoindre" in p[2]
    assert site_problem(site(status="not_started"))[0] == "Not reached" and site_problem(site(status="not_started"), False) is None
    assert site_problem(site()) is None and site_problem(site(outcome="nothing_in_range")) is None  # read normally
    assert all(site_problem(site(status=x, last_error="HTTP 500"))[0] in PROBLEM_ORDER for x in ("blocked", "failed"))
