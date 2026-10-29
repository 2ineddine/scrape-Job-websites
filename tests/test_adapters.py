"""Offline tests of the platform adapters and detection, on answers shaped like the real ones (no network)."""
import json
from datetime import datetime, timedelta

from jobfinder.adapters import (ScanState, _workday_arg, crawl_feed, crawl_icims, crawl_successfactors, crawl_wordpress,
                                crawl_workday, detect_from_page, detect_from_url, embedded_jobs, feed_items, make_gate)
from jobfinder.config import PROFILE_DEFAULT, run_config
from bs4 import BeautifulSoup


class R:
    def __init__(self, url, text="", js=None, headers=None):
        self.url, self.text, self._js, self.headers, self.status_code = url, text, js, headers or {}, 200

    def json(self):
        return self._js if self._js is not None else json.loads(self.text)


class FakeFetcher:
    """Answers by URL substring; records what was requested."""

    def __init__(self, routes):
        self.routes, self.calls, self.notes = routes, [], []

    def _find(self, url):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v(url) if callable(v) else v
        raise ConnectionError(f"HTTP 404 {url}")

    def get(self, url, **kw):
        v = self._find(url)
        return R(url, js=v) if not isinstance(v, str) else R(url, v)

    def post_json(self, url, payload):
        v = self._find(url + "#" + json.dumps(payload, sort_keys=True))
        return R(url, js=v)

    def note(self, m):
        self.notes.append(m)


def CFG(**k):
    c = run_config({"date_preset": "Last 30 days", "site_filter_location": "France", "delay": 0, **k})
    c["_gate"], c["_profile"] = make_gate(c), PROFILE_DEFAULT
    return c


TODAY = datetime.utcnow().strftime("%Y-%m-%d")
OLD = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")


def test_detect_from_url():
    assert detect_from_url("https://valeo.wd3.myworkdayjobs.com/en-EN/valeo_jobs")[0] == "workday"
    a = _workday_arg("https://onepoint.wd3.myworkdayjobs.com/fr-FR/OnepointFR?locationCountry=x")
    assert (a["tenant"], a["site"], a["prefix"]) == ("onepoint", "OnepointFR", "/fr-FR/OnepointFR")
    assert detect_from_url("https://careers.smartrecruiters.com/NEXTON") == ("smartrecruiters", "NEXTON")
    assert detect_from_url("https://faac-group.breezy.hr/?&country=France#positions") == ("breezy", "https://faac-group.breezy.hr")
    assert detect_from_url("https://expleo-jobs-fr-fr.icims.com/jobs/search?hashed=1")[0] == "icims"
    assert detect_from_url("https://www.example.com/careers") is None


def test_detect_from_page():
    f = FakeFetcher({})
    assert detect_from_page(f, "https://jobs.blablacar.com/vacancies",
                            '<a href="https://jobs.lever.co/blablacar/123">x</a>') == ("lever", "blablacar")
    assert detect_from_page(f, "https://careers.scalian.com/jobs",
                            '<a href="https://jobs.smartrecruiters.com/ni/Scalian/abc-candidature">x</a>') == ("smartrecruiters", "Scalian")
    assert detect_from_page(f, "https://jobs.smile.eu/jobs",
                            '<link rel="alternate" type="application/rss+xml" title="Jobs" href="/jobs.rss">teamtailor') == \
        ("teamtailor", ("https://jobs.smile.eu/jobs.rss", "teamtailor"))
    assert detect_from_page(f, "https://careers.lkqeurope.com/France/go/x/1/", "<script>jobs2web</script>")[0] == "successfactors"
    wp = FakeFetcher({"wp/v2/types": {"post": {"rest_base": "posts"}, "offre-emploi": {"name": "Offres", "rest_base": "offre-emploi"}}})
    assert detect_from_page(wp, "https://www.smartpoint.fr/carrieres/",
                            '<link rel="https://api.w.org/" href="https://www.smartpoint.fr/wp-json/">') == \
        ("wordpress", ("https://www.smartpoint.fr/wp-json/", ["offre-emploi"]))


def test_workday_list_detail_and_france_facet():
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [{"descriptor": "France", "id": "FRID"}, {"descriptor": "India", "id": "IN"}]}]}]
    posts = [{"title": "Machine Learning Engineer", "externalPath": "/job/Paris/MLE_R1", "locationsText": "Paris",
              "postedOn": "Posted 30+ Days Ago", "bulletFields": ["R1"]},
             {"title": "Division Marketing Manager", "externalPath": "/job/Paris/MKT_R2", "locationsText": "Paris",
              "postedOn": "Posted Today", "bulletFields": ["R2"]}]
    f = FakeFetcher({
        '"limit": 1': {"total": 2, "jobPostings": posts[:1], "facets": facets},
        '"offset": 0': {"total": 2, "jobPostings": posts},
        "/job/Paris/MLE_R1": {"jobPostingInfo": {"startDate": TODAY, "jobDescription": "<p>Deep learning for vision</p>",
                                                 "location": "Paris", "country": {"descriptor": "France"}, "jobReqId": "R1"}},
    })
    pages = list(crawl_workday(f, _workday_arg("https://t.wd3.myworkdayjobs.com/fr-FR/Site"), CFG(), set(), ScanState()))
    jobs = pages[0].jobs
    assert any('"locationCountry": ["FRID"]' in c for c in f.calls)  # the site's own France filter is used
    mle = jobs[0]
    assert mle["url"] == "https://t.wd3.myworkdayjobs.com/fr-FR/Site/job/Paris/MLE_R1"
    assert mle["date"].strftime("%Y-%m-%d") == TODAY and "startDate" in mle["date_source"]  # exact, not '30+ days'
    assert mle["country_hint"] == "France" and "Deep learning" in mle["description"]
    assert not any("MKT_R2" in c for c in f.calls)  # irrelevant title: its job page is never opened


def test_feed_teamtailor_and_talentsoft():
    rss = f"""<?xml version="1.0"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
      <item><title>Ingénieur Vision par ordinateur H/F</title><description>&lt;p&gt;{'Texte ' * 100}&lt;/p&gt;</description>
      <pubDate>Mon, 26 Oct 2026 11:27:20 +0100</pubDate><link>https://jobs.x.fr/jobs/1-vision</link><remoteStatus>hybrid</remoteStatus>
      <tt:locations><tt:location><tt:city>Lyon</tt:city><tt:country>France</tt:country></tt:location></tt:locations>
      <tt:department>R&amp;D</tt:department></item></channel></rss>"""
    assert feed_items(rss)[0]["city"] == ["Lyon"]
    job = list(crawl_feed(FakeFetcher({"jobs.rss": rss}), ("https://jobs.x.fr/jobs.rss", "teamtailor"), CFG(), set(), ScanState()))[0].jobs[0]
    assert (job["location"], job["country_hint"], job["department"]) == ("Lyon, France", "France", "R&D")
    assert job["date"] == datetime(2026, 10, 26, 11, 27, 20) or job["date"].date() == datetime(2026, 10, 26).date()
    ts = """<rss version="2.0"><channel><item><link>https://r.fr/Pages/Offre/detailoffre.aspx?idOffre=460</link>
      <category>CDI</category><category>Rueil Malmaison</category><title>2026-460 - Ingénieur ACV H/F</title>
      <description>Type de contrat : CDI</description><pubDate>Wed, 28 Oct 2026 11:39:53 Z</pubDate></item></channel></rss>"""
    f = FakeFetcher({"offerRss": ts, "detailoffre": "<html><h1>x</h1><main>Localisation : Rueil-Malmaison (92)</main></html>"})
    j = list(crawl_feed(f, ("https://r.fr/handlers/offerRss.ashx?LCID=1036", "talentsoft"), CFG(date_preset="All dates"), set(), ScanState()))[0].jobs[0]
    assert j["employment_type"] == "CDI" and j["location"] == "Rueil Malmaison" and j["date"].day == 28


def test_successfactors_table():
    row = lambda i, d: (f'<tr class="data-row"><td><a class="jobTitle-link" href="/job/Paris-Data-Scientist/{i}/">Data Scientist {i}</a>'
                        f'<span class="jobLocation">Paris, FR</span><span class="jobDate">{d}</span></td></tr>')
    html = f'<html lang="fr-FR"><table>{row(1, "29 Oct 2026")}{row(2, "29 Oct 2026")}</table><span class="paginationLabel">Résultats 1 – 2 sur 2</span></html>'
    f = FakeFetcher({"sortColumn=referencedate": html, "/job/": "<html><main>Deep learning</main></html>"})
    pg = list(crawl_successfactors(f, "https://careers.x.com/France/go/Welcome/9357555/", CFG(date_preset="All dates"), set(), ScanState()))
    assert len(pg) == 1 and [j["location"] for j in pg[0].jobs] == ["Paris, FR"] * 2
    assert f.calls[0].startswith("https://careers.x.com/France/go/Welcome/9357555/?") and "sortDirection=desc" in f.calls[0]
    assert pg[0].jobs[0]["date"] == datetime(2026, 10, 29)


def test_icims_rows():
    html = """<div class="iCIMS_JobsTable"><div class="row">
      <div class="col-xs-6 header right">Date de publication il y a 6 jours (10/06/2026 10:17 AM)</div>
      <div class="col-xs-12 title"><a href="https://x.icims.com/jobs/57287/ingenieur/job?in_iframe=1"><h3>Ingénieur traitement du signal H/F</h3></a></div>
      <div class="col-xs-12 description">Signal processing</div>
      <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Job Locations</dt><dd class="iCIMS_JobHeaderData">FR-33-Merignac</dd></div>
      <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Type d’emploi</dt><dd class="iCIMS_JobHeaderData">CDI</dd></div>
      </div></div>"""
    f = FakeFetcher({"pr=0": html, "pr=1": "<div></div>", "/jobs/57287": "<html><main>full text</main></html>"})
    pages = list(crawl_icims(f, "https://x.icims.com/jobs/search?hashed=1&searchLocation=FR", CFG(date_preset="All dates"), set(), ScanState()))
    j = pages[0].jobs[0]
    assert j["url"] == "https://x.icims.com/jobs/57287/ingenieur/job" and j["location"] == "FR-33-Merignac"
    assert j["date"] == datetime(2026, 10, 6) and j["employment_type"] == "CDI"  # month first: 6 October, not 10 June
    assert "searchLocation=FR" in f.calls[0] and "hashed" not in f.calls[0]


def test_wordpress_rest():
    items = [{"id": 1, "date_gmt": f"{TODAY}T08:00:00", "link": "https://w.fr/offre/ai/", "title": {"rendered": "AI Engineer LLM &#038; ML (H/F)"},
              "content": {"rendered": "<p>" + "LLM " * 150 + "Localisation : Paris</p>"}, "excerpt": {"rendered": ""}},
             {"id": 2, "date_gmt": f"{OLD}T08:00:00", "link": "https://w.fr/offre/old/", "title": {"rendered": "Old"},
              "content": {"rendered": ""}, "excerpt": {"rendered": ""}}]
    f = FakeFetcher({"wp/v2/offre": lambda u: items})
    f.get = lambda url, **kw: (f.calls.append(url), R(url, js=items, headers={"X-WP-TotalPages": "1"}))[1]
    jobs = list(crawl_wordpress(f, ("https://w.fr/wp-json/", ["offre"]), CFG(), set(), ScanState()))[0].jobs
    assert jobs[0]["title"] == "AI Engineer LLM & ML (H/F)" and jobs[0]["location"] == "Paris"
    assert jobs[0]["date"].strftime("%Y-%m-%d") == TODAY and jobs[1]["date"].strftime("%Y-%m-%d") == OLD
    assert len(f.calls) == 1  # the old offer is not opened (DATE FIRST)


def test_embedded_next_data():
    data = {"props": {"pageProps": {"jobs": [{"title": "Research Engineer", "slug": "/careers/jobs/research-engineer-123",
                                              "city": "Paris", "publishedAt": "2026-10-20"}]}}}
    soup = BeautifulSoup(f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>', "html.parser")
    j = embedded_jobs(soup, "https://eviden.com/fr/carrieres")
    assert len(j) == 1 and j[0]["url"] == "https://eviden.com/careers/jobs/research-engineer-123" and j[0]["date"].day == 20


def test_ai_page_reader_keeps_only_what_is_on_the_page(monkeypatch):
    from jobfinder import pagereader
    page = ("<html><head><title>Stages 2026</title></head><body><main><h1>Propositions de stages</h1>"
            "<h3>Stage M2 : Apprentissage profond pour la segmentation d'images médicales</h3>"
            "<p>Publié le 20/10/2026. Lieu : Paris. Encadrant : X.</p>"
            "<h3>Internship: robust speech recognition</h3><p>Posted: 2026-10-01</p>"
            "<a href='/files/stage-asr.pdf'>Sujet (PDF)</a></main></body></html>")
    answer = pagereader.PageOffers(offers=[
        pagereader.PageOffer(title="Stage M2 : Apprentissage profond pour la segmentation d'images médicales",
                             date_text="20/10/2026", location="Paris", contract="stage"),
        pagereader.PageOffer(title="Internship: robust speech recognition", link=0, date_text="2026-10-01"),
        pagereader.PageOffer(title="Invented offer about quantum gravity", date_text="2026-10-25")])
    monkeypatch.setattr(pagereader, "call_json", lambda *a, **k: answer)
    f = FakeFetcher({"/files/stage-asr.pdf": "not a pdf", "lab.fr/stages": page})
    f.get = (lambda g: lambda url, **kw: (lambda r: (setattr(r, "content", r.text.encode()), r)[1])(g(url)))(f.get)
    cfg = CFG(date_preset="All dates", _keys={}, _profile={**PROFILE_DEFAULT, "offer_types": ["internship"]})
    jobs = list(pagereader.read_page(f, "https://lab.fr/stages", cfg, set(), ScanState()))[0].jobs
    assert [j["title"][:20] for j in jobs] == ["Stage M2 : Apprentis", "Internship: robust s"]  # the invented one is dropped
    assert jobs[0]["date"] == datetime(2026, 10, 20) and "jboffer=" in jobs[0]["url"] and jobs[0]["location"] == "Paris"
    assert jobs[1]["url"] == "https://lab.fr/files/stage-asr.pdf" and jobs[1]["date"] == datetime(2026, 10, 1)


def test_shared_template_date_is_not_trusted():
    from jobfinder.adapters import _raw, distrust_shared_dates
    d = datetime(2022, 2, 10)
    jobs = [_raw(t, f"https://x.com/jobs/{i}", date=d, date_conf=0.8, date_source="html meta article:published_time")
            for i, t in enumerate(["Data Scientist", "Lead Data Engineer", "Expert Data Scientist"])]
    assert all(j["date_conf"] < 0.5 for j in distrust_shared_dates(jobs))  # -> 'unknown', checked later
    api = [_raw(t, f"https://x.com/jobs/{i}", date=d, date_conf=0.9, date_source="workday posting startDate")
           for i, t in enumerate(["A role", "B role", "C role"])]
    assert all(j["date_conf"] == 0.9 for j in distrust_shared_dates(api))  # the same day from an API is real
