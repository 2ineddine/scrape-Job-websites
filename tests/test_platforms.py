"""Offline tests of the readers added for websites3.html: Phenom, Eightfold, Jibe, Workable, robots.txt, sitemaps, job data
in script variables, Greenhouse board names written in page data, Workday filters written in the URL."""
from jobfinder.adapters import (ScanState, _workday_arg, crawl_eightfold, crawl_jibe, crawl_phenom, crawl_workable,
                                detect_from_page, detect_from_url, embedded_jobs, guess_board)
from jobfinder.crawler import _slug_title, crawl_sitemap
from jobfinder.web import Robots
from bs4 import BeautifulSoup

from test_adapters import CFG, OLD, TODAY, FakeFetcher


class SiteFetcher(FakeFetcher):
    def robots_ok(self, url):
        return "/closed/" not in url

    def sitemaps(self, url):
        return ["https://co.fr/sitemap.xml"]


def test_robots_longest_rule_wins():
    r = Robots("User-agent: *\nDisallow: /\nAllow: /$\nAllow: /careers\nAllow: /api/apply\n\nSitemap: https://x.ai/sm.xml\n")
    assert r.allowed("https://x.ai/careers") and r.allowed("https://x.ai/api/apply/v2/jobs?start=0") and r.allowed("https://x.ai/")
    assert not r.allowed("https://x.ai/login")
    assert r.sitemaps == ["https://x.ai/sm.xml"]
    r = Robots("User-agent: *\nAllow: /\nAllow: /nos-offres?page=*\nDisallow: /*?*\n")
    assert r.allowed("https://s.fr/nos-offres?page=2") and not r.allowed("https://s.fr/recherche?q=1")
    r = Robots("User-agent: *\nDisallow: */apply\n")
    assert r.allowed("https://p.com/widgets") and not r.allowed("https://p.com/global/en/apply")


def test_detection_of_new_platforms():
    assert detect_from_url("https://stmicroelectronics.eightfold.ai/careers") == ("eightfold", "https://stmicroelectronics.eightfold.ai")
    assert detect_from_url("https://apply.workable.com/withings/") == ("workable", "withings")
    a = _workday_arg("https://sonos.wd1.myworkdayjobs.com/en-US/Sonos?locations=f4abac2e193c01537b8ebb847c1b7a83"
                     "&locations=58852068b6ad012ab42a01159d1d3976")
    assert a["facets"] == {"locations": ["f4abac2e193c01537b8ebb847c1b7a83", "58852068b6ad012ab42a01159d1d3976"]}
    ph = '<script>phApp.ddo = {"siteConfig":{"refNum":"TGPTGWGLOBAL","locale":"en_global"}}; phApp.x=1;</script>' \
         '<a href="https://thales.wd3.myworkdayjobs.com/Careers/job/1">apply</a>'
    name, arg = detect_from_page(None, "https://careers.thalesgroup.com/global/en/search-results?keywords=audio", ph)
    assert name == "phenom" and arg["prefix"] == "/global/en" and arg["keywords"] == "audio"
    jibe = '<script src="https://assets.jibecdn.com/x.js"></script><a href="https://careers-se.icims.com/jobs/1/login">a</a>'
    name, arg = detect_from_page(None, "https://careers.se.com/jobs?location=France&page=2", jibe)
    assert name == "jibe" and arg["query"] == {"location": "France"}
    gh = '{"sources":[[0,&quot;https://boards-api.greenhouse.io/v1/boards/mirakl/jobs?content=true&quot;],' \
         '[0,&quot;https://boards-api.greenhouse.io/v1/boards/mirakllabs/jobs&quot;]]}'
    assert detect_from_page(None, "https://www.mirakl.com/company/careers/jobs/", gh) == ("greenhouse", "mirakl,mirakllabs")
    blog = '<link rel="alternate" type="application/rss+xml" title="Ledger Careers &raquo; Feed" href="https://careers.ledger.com/feed/">'
    assert detect_from_page(None, "https://careers.ledger.com/jobs/search", blog) is None


def test_phenom_pages_until_total():
    pages = {0: [{"jobId": "R1", "title": "Stage - Data Scientist", "postedDate": f"{TODAY}T00:00:00.000+0000",
                  "location": "Élancourt, Yvelines", "country": "France", "descriptionTeaser": "Stage IA"}],
             100: [{"jobId": "R2", "title": "Ingénieur IA", "postedDate": f"{OLD}T00:00:00.000+0000", "country": "France"}]}
    f = FakeFetcher({"/widgets": lambda u: {"refineSearch": {"totalHits": 101, "data": {"jobs": pages[0 if '"from": 0' in u else 100]}}},
                     "/global/en/job/": "<html><body>gone</body></html>"})
    a = dict(origin="https://careers.thalesgroup.com", prefix="/global/en", locale="en_global", keywords="")
    jobs = [j for pg in crawl_phenom(f, a, CFG(), set(), ScanState()) for j in pg.jobs]
    assert [j["url"] for j in jobs] == ["https://careers.thalesgroup.com/global/en/job/R1",
                                        "https://careers.thalesgroup.com/global/en/job/R2"]
    assert jobs[0]["date"].strftime("%Y-%m-%d") == TODAY and jobs[0]["country_hint"] == "France"
    assert '"country": ["France"]' in f.calls[0]


def test_eightfold_create_time_and_detail():
    import time
    now = int(time.time())
    f = FakeFetcher({"/api/apply/v2/jobs/7": {"job_description": "<p>Poste en vision par ordinateur à Grenoble</p>"},
                     "/api/apply/v2/jobs?": {"count": 1, "positions": [
                         {"id": 7, "name": "Ingénieur vision par ordinateur", "locations": ["Grenoble, France"], "t_create": now,
                          "t_update": now, "canonicalPositionUrl": "https://st.eightfold.ai/careers/job/7"}]}})
    jobs = [j for pg in crawl_eightfold(f, "https://st.eightfold.ai", CFG(), set(), ScanState()) for j in pg.jobs]
    assert jobs[0]["date"].strftime("%Y-%m-%d") == TODAY and "vision par ordinateur" in jobs[0]["description"]
    assert "location=France" in f.calls[0]


def test_jibe_keeps_site_filters_and_reads_posted_date():
    f = FakeFetcher({"/api/jobs": {"totalCount": 1, "jobs": [{"data": {
        "slug": "140", "req_id": "140", "title": "Stage - Développement IA", "posted_date": "October 22, 2026",
        "full_location": "EYBENS, France", "country": "France", "description": "<p>Stage IA</p>", "language": "fr-fr"}}]}})
    jobs = [j for pg in crawl_jibe(f, dict(origin="https://careers.se.com", query={"keywords": "data"}), CFG(), set(), ScanState())
            for j in pg.jobs]
    assert jobs[0]["url"] == "https://careers.se.com/jobs/140?lang=fr-fr" and jobs[0]["date"].strftime("%Y-%m-%d") == "2026-10-22"
    assert "keywords=data" in f.calls[0] and "sortBy=posted_date" in f.calls[0] and "location=France" in f.calls[0]


def test_workable_published_and_token_paging():
    first = {"nextPage": "TOK", "results": [{"shortcode": "AA", "title": "API Engineer (H/F)", "published": f"{TODAY}T00:00:00Z",
                                             "location": {"city": "Issy-les-Moulineaux", "country": "France"}}]}
    second = {"results": [{"shortcode": "BB", "title": "Stage - QA Python", "published": f"{OLD}T00:00:00Z", "location": {}}]}
    f = FakeFetcher({'"token": "TOK"': second, "/api/v3/accounts/withings/jobs": first,
                     "/api/v2/accounts/withings/jobs/AA": {"description": "<p>Python API</p>", "requirements": "Bac+5"}})
    jobs = [j for pg in crawl_workable(f, "withings", CFG(), set(), ScanState()) for j in pg.jobs]
    assert [j["platform_id"] for j in jobs] == ["AA", "BB"]
    assert jobs[0]["url"] == "https://apply.workable.com/withings/j/AA/" and "Python API" in jobs[0]["description"]


def test_jobs_in_a_script_variable():
    html = ('<script>window.allOffers = [{"Title":{"fr-fr":"STAGE - Base de données Python"},"Address_City":"Les Ulis",'
            '"PublicationStartDate":"2026-10-27T08:26:01+00:00","Url":"https://careers.werecruit.io/fr/asn/offers/stage-caa7d7"}];'
            ' window.x = 1;</script>')
    jobs = embedded_jobs(BeautifulSoup(html, "html.parser"), "https://careers.werecruit.io/fr/asn/offers")
    assert jobs[0]["title"] == "STAGE - Base de données Python" and jobs[0]["location"] == "Les Ulis"
    assert jobs[0]["date"].strftime("%Y-%m-%d") == "2026-10-27"


def test_sitemap_skips_offers_modified_before_the_range():
    sm = (f"<urlset><url><loc>https://co.fr/job/data-scientist-machine-learning-paris-jid-10</loc><lastmod>{TODAY}T10:00:00Z</lastmod></url>"
          f"<url><loc>https://co.fr/job/ingenieur-ia-lyon-jid-2</loc><lastmod>{OLD}T10:00:00Z</lastmod></url>"
          f"<url><loc>https://co.fr/blog/a-nice-story</loc><lastmod>{TODAY}</lastmod></url></urlset>")
    page = ('<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Data Scientist Machine Learning",'
            f'"datePosted":"{TODAY}","jobLocation":{{"address":{{"addressLocality":"Paris","addressCountry":"FR"}}}},'
            '"description":"Stage de six mois en data science, apprentissage automatique."}</script></head></html>')
    f = SiteFetcher({"sitemap.xml": sm, "/job/data-scientist": page})
    jobs = [j for pg in crawl_sitemap(f, "https://co.fr/jobs", CFG(), set(), ScanState()) for j in pg.jobs]
    assert [j["title"] for j in jobs] == ["Data Scientist Machine Learning"]
    assert not any("ingenieur-ia" in c for c in f.calls[1:])  # too old by its lastmod: never opened
    assert _slug_title("https://jobs.sanofi.com/en/job/vitry-sur-seine/technicien-de-production-dsp/2649/46449055744") \
        == "technicien de production dsp"


def test_board_name_guessed_only_when_it_belongs_to_the_company():
    page = '<html><head><title>Careers | Datadog</title></head><body>search</body></html>'
    f = FakeFetcher({"careers.datadoghq.com": page,
                     "boards-api.greenhouse.io/v1/boards/datadog/": {"jobs": [{"absolute_url": "https://careers.datadoghq.com/detail/1"}]}})
    assert guess_board(f, "https://careers.datadoghq.com/all-jobs/") == ("greenhouse", "datadog")
    f = FakeFetcher({"careers.other.com": '<title>Careers | Datadog</title>',
                     "boards-api.greenhouse.io/v1/boards/datadog/": {"jobs": [{"absolute_url": "https://careers.datadoghq.com/detail/1"}]}})
    assert guess_board(f, "https://careers.other.com/") is None  # a board of ANOTHER company with the same name


def test_card_title_and_card_location():
    from jobfinder.classify import location_from_text
    from jobfinder.crawler import _card_title
    html = ('<a href="/en/job-offer-result-list/trainee5"><span>Stage - Génération acoustique</span> Offer published on '
            '27/10/2026 Location : Clermont-Ferrand, France Job Category : Personnel Contract Type : Intern</a>')
    soup = BeautifulSoup(html, "html.parser")
    assert _card_title(soup, "https://m.fr/en/job-offer-result-list", "https://m.fr/en/job-offer-result-list/trainee5") \
        == "Stage - Génération acoustique"
    assert location_from_text("Location : Clermont-Ferrand, France Contract Type : Intern") == "Clermont-Ferrand, France"


def test_page_reader_drops_future_dates_and_shrinks_large_requests(monkeypatch):
    from jobfinder import pagereader
    from jobfinder.llm import RequestTooLarge
    page = ("<html><body><main><h1>Recrutements</h1>" + "<p>Texte de présentation du laboratoire.</p>" * 40 +
            "<h2>Assistant en gestion financière (F/H)</h2><p>Prise de fonctions le 01/01/2027.</p></main></body></html>")
    sizes = []

    def fake(prompt, schema, *a, **k):
        sizes.append(len(prompt))
        if len(sizes) == 1:
            raise RequestTooLarge("too large")
        return schema(offers=[{"title": "Assistant en gestion financière (F/H)", "date_text": "01/01/2027"}])
    monkeypatch.setattr(pagereader, "call_json", fake)
    f = FakeFetcher({"l2s.fr": page})
    jobs = [j for pg in pagereader.read_page(f, "https://l2s.fr/recrutements", CFG(), set(), ScanState()) for j in pg.jobs]
    assert len(sizes) == 2 and jobs[0]["date"] is None  # a date to come is not a publication date


def test_ai_slips_are_corrected_not_fatal():
    from jobfinder.analysis import JobAnalysis
    from jobfinder.browser import NavView
    from jobfinder.llm import _soften
    r = JobAnalysis.model_validate({"offer_kind": "internship", "contract_kind": "Stage de 6 mois", "location_text": None,
                                    "is_relevant": True, "relevance_score": 80, "analysis_confidence": 0.8})
    assert (r.offer_kind, r.contract_kind, r.location_text) == ("job", "internship", "")
    v = NavView.model_validate(_soften(NavView, {"page_kind": "search_results", "pagination": None, "job_elements": [3]}))
    assert (v.page_kind, v.pagination, v.job_elements) == ("other", "none", [3])


def test_site_filters_from_the_page_itself():
    from jobfinder.sitefilters import filtered_url
    cfg = CFG(date_preset="Last 7 days")
    form = ('<form method="get" action="/offres"><input type="hidden" name="lang" value="fr">'
            '<label for="p">Pays</label><select id="p" name="pays"><option value="">Tous</option><option value="12">Allemagne</option>'
            '<option value="33">France</option></select>'
            '<select name="posted"><option value="">Toutes</option><option value="1">Moins de 24 heures</option>'
            '<option value="7">Moins de 7 jours</option><option value="30">Moins de 30 jours</option></select>'
            '<select name="sort"><option value="old">Plus anciennes</option><option value="new">Plus récentes</option></select></form>')
    u, used = filtered_url(BeautifulSoup(form, "html.parser"), "https://co.fr/offres", cfg)
    assert u == "https://co.fr/offres?lang=fr&pays=33&posted=7&sort=new" and used == ["France", "posted within 7 day(s)", "newest first"]
    deadline = '<a href="/offres?tri=asc" title="Trier par date limite croissante">Date</a>'
    assert filtered_url(BeautifulSoup(deadline, "html.parser"), "https://jobs.inria.fr/offres", cfg) == (None, [])
    link = '<a href="/fr/offres?sortBy=date">Plus récentes</a>'
    assert filtered_url(BeautifulSoup(link, "html.parser"), "https://r.fr/fr/offres", cfg)[0] == "https://r.fr/fr/offres?sortBy=date"


def test_icims_day_month_order_and_sort():
    from jobfinder.adapters import _icims_dayfirst, _icims_list
    assert _icims_dayfirst("(28/10/2026) (07/10/2026)") is True
    assert _icims_dayfirst("(10/23/2026 10:17 AM)") is False and _icims_dayfirst("(10/06/2026 10:17 AM)") is False
    assert _icims_dayfirst("(06/10/2026)") is None
    assert "schemaId=%24T%7BJob%7D.%24T%7BJobPost%7D.%24F%7BPostedDateTime%7D&o=A" in _icims_list("https://x.icims.com/jobs/search", "A")(0)



def test_removing_a_cv_deletes_its_file(tmp_path, monkeypatch):
    from jobfinder import cv as cvm
    (tmp_path / "cvs").mkdir()
    pdf = tmp_path / "cvs" / "abc.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(cvm, "DATA_DIR", tmp_path)

    class Row:
        path = str(pdf)

    class Session:  # no database: only what delete_cv uses
        get = staticmethod(lambda model, i: Row())
        delete = commit = staticmethod(lambda *a: None)
    cvm.delete_cv(Session(), 1)
    assert not pdf.exists()


def test_url_already_sorted_by_date():
    from jobfinder.crawler import _DATE_SORT
    assert _DATE_SORT.search("https://www.alten.fr/carriere/jobs/?sortby=date&job_search=ia")
    assert _DATE_SORT.search("https://r.fr/offres?sortBy=date") and _DATE_SORT.search("https://x.fr/jobs?order=most_recent")
    assert not _DATE_SORT.search("https://x.fr/jobs?sort=relevance") and not _DATE_SORT.search("https://x.fr/jobs?page=2")
