"""Offline tests: LaTeX escaping, CV evidence check, AI-browser prompts, job JSON extraction, detail merging."""
from datetime import datetime

from jobfinder.adapters import _raw, jobs_from_json
from jobfinder.adapters import merge_detail as _merge_detail
from jobfinder.browser import (DENY, FilterStep, NavView, _denied, _heuristic_jobs, _heuristic_next, _window_days,
                               build_filter_prompt, build_prompt, triage)
from jobfinder.cv import CVMatch, Evidence, Letter, build_tex, tex, tex_url, verify_evidence
from jobfinder.llm import extract_json
from jobfinder.cv import extract_cv


def test_letters_and_evidence():
    assert tex(r"R&D 50% #1 a_b {x} \input{/etc/passwd} ~^") == \
        r"R\&D 50\% \#1 a\_b \{x\} \textbackslash{}input\{/etc/passwd\} \textasciitilde{}\textasciicircum{}"
    assert tex_url("https://x.org/a?b=1&c=2") == r"https://x.org/a?b=1\&c=2"
    assert tex_url("javascript:alert(1)") is None and tex_url("https://x.org/\\input{a}") is None
    cvtext = "Developed speech recognition models in Python at LabX."
    m = CVMatch(match_score=80, confidence=0.8, evidence=[Evidence(requirement="speech", cv_evidence="Developed speech recognition models in Python"),
                                                           Evidence(requirement="x", cv_evidence="Published 12 papers at NeurIPS")])
    m = verify_evidence(m, cvtext)
    assert len(m.evidence) == 1 and m.removed_evidence == 1
    class J:  # minimal stand-in
        employer, city, platform_job_id, title = "Acme & Co", None, "R_42", "Ingénieur"
    src = build_tex({"name": "Jean Dupont", "address": "1 rue X\n75001 Paris", "email": "j@x.fr"}, J,
                    Letter(subject="Candidature 100%", paragraphs=["Bonjour $$", "Texte"]), datetime(2026, 10, 13))
    assert r"Candidature 100\%" in src and r"Acme \& Co" in src and "13 octobre 2026" in src and r"Bonjour \$\$" in src
    assert extract_json('```json\n{"a":1}\n```') == {"a": 1}
    try:
        extract_cv(b"not a pdf")
        raise SystemExit("should fail")
    except ValueError:
        pass
# --------------------------------------------------------------------------- self-tests (offline)


def test_ai_browser():
    items = [{"i": 0, "tag": "a", "text": "Machine Learning Engineer", "href": "https://x.com/jobs/123?utm=1", "disabled": False},
             {"i": 1, "tag": "button", "text": "Next", "href": "", "disabled": False},
             {"i": 2, "tag": "a", "text": "Benefits", "href": "https://x.com/about/benefits", "disabled": False}]
    p = build_prompt("https://x.com/careers", "Careers", items, "Open positions")
    assert '[0] a "Machine Learning Engineer" -> /jobs/123?utm=1' in p and '[1] button "Next"' in p
    assert DENY.search("Apply now") and DENY.search("Postuler") and not DENY.search("Next page")
    v = NavView.model_validate({"page_kind": "listing", "job_elements": [0], "pagination": "next_button",
                                "pagination_element": 1})
    assert v.pagination_element == 1 and v.dismiss_element is None
    assert _heuristic_jobs(items) == [0] and _heuristic_next(items) == 1
    cfg = {}
    assert triage("Machine Learning Engineer", cfg)[0] == "pass"
    assert triage("Senior Accountant", cfg)[0] == "reject" and triage("Stage - Data Scientist", cfg)[0] == "reject"
    assert triage("Voir l'offre", cfg)[0] == "maybe" and triage("Read more", cfg)[0] == "maybe"
    assert triage("Ingénieur Systèmes Embarqués", cfg)[0] == "maybe"
    wd = {"jobPostings": [{"title": "AI Engineer", "externalPath": "/job/Paris/AI-Engineer_R123", "locationsText": "Paris",
                           "postedOn": "Posted 3 Days Ago"},
                          {"title": "Data Scientist", "externalPath": "/job/Paris/DS_R124", "locationsText": "Paris",
                           "postedOn": "Posted Today"}], "facets": [{"name": "Paris", "id": 3, "count": 4}]}
    js = jobs_from_json(wd, "https://t.wd3.myworkdayjobs.com/en-US/Careers")
    assert [j["url"] for j in js] == ["https://t.wd3.myworkdayjobs.com/en-US/Careers/job/Paris/AI-Engineer_R123",
                                      "https://t.wd3.myworkdayjobs.com/en-US/Careers/job/Paris/DS_R124"], js
    assert js[0]["location"] == "Paris" and js[0]["date"] is not None
    menu = [{"title": "About us", "url": "/about"}, {"title": "News", "url": "/news"}]
    assert jobs_from_json(menu, "https://x.com/") == []
    gen = {"data": {"results": [{"jobTitle": "Research Scientist", "id": "77", "city": "Lyon", "country": "France",
                                 "datePosted": "2026-10-06"}, {"jobTitle": "NLP Engineer", "id": "78", "city": "Paris",
                                                                "datePosted": "2026-10-07"}]}}
    gj = jobs_from_json(gen, "https://x.com/careers")
    assert len(gj) == 2 and gj[0]["location"] == "Lyon, France" and gj[0]["url"].endswith("?jbid=77")
    assert gj[0]["date"].day == 6
    # a feed date (15/10/2026) must not beat the 'Date de publication' written in the description
    orange = {"jobs": [{"title": "Chargé de prévention", "id": "9", "city": "Paris", "country": "France",
                        "datePosted": "15/10/2026",
                        "description": "<p><strong>Date de publication :</strong> Jul 01, 2026, 4:19PM</p><p>Durée du contrat : 12 months</p>"}]}
    oj = jobs_from_json(orange, "https://orange.jobs/careers")
    assert len(oj) == 1 and oj[0]["date"] == datetime(2026, 7, 1), oj
    assert jobs_from_json({"jobs": [{"title": "AI Engineer", "id": "5", "city": "Paris", "postedOn": "n/a",
                                      "createdAt": 1760000000000}]}, "https://x.com/c")[0]["date_conf"] <= 0.6
    nowj = _raw("T", "https://x.com/j/2", date=datetime(2026, 10, 14), date_conf=0.95, date_source="list", date_text="14/10/2026")
    dj = _raw("T", "https://x.com/j/2", date=datetime(2026, 10, 1), date_conf=0.9, date_source="detail", date_text="2026-10-01",
              description="d" * 900)
    assert _merge_detail(nowj, dj)["date"] == datetime(2026, 10, 1)  # disagree -> earliest
    base = _raw("T", "https://x.com/j/1", provenance="listing-title")
    det = _raw("Full Title", "https://x.com/j/1", description="d" * 900, location="Paris, France")
    assert _merge_detail(base, det)["title"] == "Full Title"
    # ---- website filters
    assert _window_days({"date_preset": "Last 7 days"}) == 7 and _window_days({"date_preset": "All dates"}) is None
    assert _window_days({"date_preset": "Last 24 hours"}) == 1
    assert _denied("Apply now") and not _denied("Apply filters", True) and _denied("Apply now", True)
    assert _denied("Sign in", True) and _denied("Job alert", True) and not _denied("Search", True)
    fi = [{"i": 0, "tag": "select", "text": "Posted (dropdown, now: Any time)", "href": "", "field": False, "disabled": False},
          {"i": 1, "tag": "option", "text": "Past 24 hours", "href": "", "field": False, "disabled": False},
          {"i": 2, "tag": "input", "text": "Location", "href": "", "field": True, "disabled": False}]
    fp = build_filter_prompt("https://x.com/jobs", "Jobs", fi, "x", "France", 7,
                             {"location_done": False, "date_done": False})
    assert "last 7 day(s)" in fp and "in France" in fp and '[2] input "Location" (text field)' in fp and '[1] option "Past 24 hours"' in fp
    done = build_filter_prompt("u", "t", fi, "x", "France", None, {"location_done": True, "date_done": True})
    assert "Nothing more to do" in done
    fs = FilterStep.model_validate({"action": "click", "element": 1, "purpose": "date", "date_done": True})
    assert fs.element == 1 and fs.date_done and not fs.location_done and FilterStep().action == "done"


def test_ai_date_is_chosen_by_number_and_verified():
    from jobfinder.analysis import JobAnalysis, _ai_date, build_prompt
    from jobfinder.classify import analyze_title
    from jobfinder.config import PROFILE_DEFAULT
    from jobfinder.dates import date_candidates
    text = ("Ingénieur de recherche. Date de publication : 12/10/2026. Mis à jour le 20/10/2026. "
            "Date limite de candidature : 30/11/2026. Prise de poste : 05/01/2027.")
    cands = date_candidates(text, now=datetime(2026, 10, 29))
    assert cands[0][1].kind == "pub" and cands[0][1].date == datetime(2026, 10, 12)
    assert [h.kind for _, h in cands] == ["pub", "upd"]  # future deadline / start dates are never offered

    class J:
        title, location_original, pub_date = "Ingénieur de recherche", "Paris", None
    p = build_prompt(J, PROFILE_DEFAULT, text, cands, analyze_title(J.title))
    assert "[1] 2026-10-12 (published)" in p and "[2] 2026-10-20 (updated)" in p and "RESEARCH SUBJECT" in p
    base = dict(is_relevant=True, relevance_score=80, analysis_confidence=0.9)
    assert _ai_date(JobAnalysis(publication_date_choice=1, **base), cands, text) == (datetime(2026, 10, 12), True)
    assert _ai_date(JobAnalysis(publication_date_choice=2, **base), cands, text) == (None, False)  # an update date is refused
    assert _ai_date(JobAnalysis(publication_date_choice=99, **base), cands, text) == (None, False)
    invented = JobAnalysis(publication_date="2026-10-25", date_evidence="published on 25 October", **base)
    assert _ai_date(invented, cands, text) == (None, False)  # a quote that is not in the text is refused
