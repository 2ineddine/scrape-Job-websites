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


def test_more_offer_links_are_recognised():
    from jobfinder.adapters import is_job_path
    from jobfinder.crawler import _job_links
    assert is_job_path("/offre-emploi/data-scientist-reference-DEF_17-00072298")
    assert is_job_path("/fr/carrieres/postesapourvoir/developpeur-full-stack-hf")
    assert is_job_path("/fr/accueil/carrieres/08a38c64-50d1-4013-bd4f-cabff4fa8ddd.html")
    assert not is_job_path("/nos-offres/filtres/mot-cles/data/page/2") and not is_job_path("/fr/carrieres/nous-rejoindre.html")
    assert not is_job_path("/offre-de-emploi/tous-les-flux-rss.aspx")  # Talentsoft folder: only '/emploi-...' pages are offers
    assert is_job_path("/offre-de-emploi/emploi-cdi-ingenieur-chercheur-h-f_42417.aspx")
    html = ('<a href="/recrutement/offers/@@LINK@@">x</a><a href="/fr/jobs//ia-engineer-h-f-1262">AI engineer</a>'
            '<a href="/en-FR/node/23383">Ingénieur DevOps Réseau Cloud (F/H)</a><a href="/en-FR/node/12">Our values</a>')
    links = _job_links(BeautifulSoup(html, "html.parser"), "https://co.fr/fr/jobs", set())
    assert links == ["https://co.fr/fr/jobs/ia-engineer-h-f-1262", "https://co.fr/en-FR/node/23383"]


def test_card_title_behind_a_read_more_button_and_page_title():
    from jobfinder.adapters import _page_title
    from jobfinder.crawler import _card_title
    html = '<div class="card"><h3>Data Engineer: Multi-stack</h3><p>Paris</p><a href="/carrieres/abc">LIRE LA SUITE</a></div>'
    assert _card_title(BeautifulSoup(html, "html.parser"), "https://i.com/", "https://i.com/carrieres/abc") == "Data Engineer: Multi-stack"
    assert _page_title(BeautifulSoup("<title>Full Stack Developer | Inetum</title>", "html.parser"), "https://www.inetum.com/x") \
        == "Full Stack Developer"


def test_page_data_offers_get_the_page_links_and_their_job_platform():
    from jobfinder.adapters import jobs_from_json
    from jobfinder.crawler import _real_links
    data = [{"id": "90901", "title": "Ingénieur FPGA F/H", "url": "Ingenieur-FPGA-F-H", "location": "Valbonne",
             "applyLink": {"url": "https://jobs.smartrecruiters.com/OCTOTechnology/744000158-ingenieur-fpga"}}]
    jobs = jobs_from_json(data, "https://emploi.co.fr/offers")
    assert jobs[0]["apply_url"].startswith("https://jobs.smartrecruiters.com/OCTOTechnology/")
    jobs = _real_links(jobs, ["https://emploi.co.fr/offers/90901/Ingenieur-FPGA-F-H"])
    assert jobs[0]["url"] == "https://emploi.co.fr/offers/90901/Ingenieur-FPGA-F-H"


def test_page_text_is_fast_on_deep_pages():
    import time
    from jobfinder.dates import _content_text
    html = "<html><body><main>" + "<div>" * 300 + "".join(f"<p>ligne {i}</p>" for i in range(1500)) + "</div>" * 300 + \
           "<nav>menu</nav></main></body></html>"
    soup, t = BeautifulSoup(html, "html.parser"), time.time()
    text = _content_text(soup, "main", 30000)
    assert "ligne 1499" in text and "menu" not in text and time.time() - t < 2


def test_career_urls_become_the_main_page():
    from jobfinder.urlclean import clean_source_url
    c = lambda u: clean_source_url(u)[0]
    # company sites: every filter goes, France and date included; the language and what the page needs stay
    assert c("https://joining.bouygues.com/global/en/search-results?keywords=audio") == \
        "https://joining.bouygues.com/global/en/search-results"
    assert c("https://careers.se.com/jobs?lang=en-US&location=France&woe=12&page=2&sortBy=relevance&keywords=data%20science") \
        == "https://careers.se.com/jobs?lang=en-US"
    assert c("https://www.alten.fr/carriere/jobs/?sortby=date&job_search=ia&pagenum=3&per_page=25") == "https://www.alten.fr/carriere/jobs/"
    assert c("https://recrute.carrefour.fr/offres?order=desc&sort=post_date_extern&contract_type=Stage") == "https://recrute.carrefour.fr/offres"
    assert c("https://careers.datadoghq.com/all-jobs/?igaag=&igacm=236&gad_source=1") == "https://careers.datadoghq.com/all-jobs/"
    assert c("https://recrutement.sfr.com/offer/page/1") == "https://recrutement.sfr.com/offer/"
    assert c("https://www.ibm.com/careers/search?field_keyword_05%5B0%5D=France") == "https://www.ibm.com/careers/search"
    assert c("https://cgi.njoyn.com/corp/xweb/xweb.asp?CLID=21001&page=joblisting&CountryID=FR&lang=2") == \
        "https://cgi.njoyn.com/corp/xweb/xweb.asp?CLID=21001&page=joblisting&lang=2"
    assert c("https://www.emploi.cea.fr/offre-de-emploi/liste-offres.aspx?page=3&LCID=1036").endswith("liste-offres.aspx?LCID=1036")
    assert c("https://www.inetum.com/fr/jobs.html?list-b4837029c4_page=1") == "https://www.inetum.com/fr/jobs.html"
    assert c("https://www.wavestone.com/en/careers/our-offers?city=paris,lyon&sort=desc") == "https://www.wavestone.com/en/careers/our-offers"
    assert c("https://jobs.siemens.com/en_US/externaljobs/SearchJobs/deep%20learning?42386=[812134]&listFilterMode=1") == \
        "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"
    # saved searches written in the path
    assert c("https://jobs.atos.net/go/Jobs-in-France/3677001") == "https://jobs.atos.net/"
    assert c("https://careers.lkqeurope.com/France/go/Welcome-to-LKQ-France/9357555") == "https://careers.lkqeurope.com/"
    assert c("https://jobs.sanofi.com/en/search-jobs/France/2649/2/3017382/46/2/50/2") == "https://jobs.sanofi.com/en/search-jobs"
    # multi-company job boards: their search IS the search
    assert "s=AI+Research+Scientist" in c("https://www.optioncarriere.com/emploi?s=AI+Research+Scientist&l=France")
    assert "query=stage" in c("https://www.welcometothejungle.com/fr/jobs?query=stage+deep+learning&page=3")
    assert "page=" not in c("https://www.welcometothejungle.com/fr/jobs?query=stage+deep+learning&page=3")
    assert c("https://www.hellowork.com/fr-fr/emploi/recherche.html?k=stage+ml+&cod=all&d=all") == \
        "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=stage+ml"
    assert c("https://jobs.makesense.org/en/s/jobs/all/all/cdi?contracts=cdd&sortBy=relevance&s=Data+scientist") == \
        "https://jobs.makesense.org/en/s/jobs/all/all?s=Data+scientist"
    assert "typesConvention=1" in c("https://www.apec.fr/candidat/recherche-emploi.html/emploi?lieux=75&typesConvention=1")
    # one address for one list
    assert c("https://jobs.inria.fr/public/classic/fr/offres/2025-09455") == "https://jobs.inria.fr/public/classic/fr/offres"
    assert c("https://emploi.cnrs.fr/Recherche.aspx?bassin=INDEF&type=STAG&texte=") == "https://emploi.cnrs.fr/Offres/Recherche.aspx"


def test_the_app_puts_back_france_and_the_date_itself():
    from jobfinder.sitefilters import with_own_filters
    cfg = {"site_filter_location": "France"}
    assert with_own_filters("https://www.capgemini.com/careers/join-capgemini/job-search", cfg).endswith("?country_code=fr-fr,FRA")
    assert with_own_filters("https://cgi.njoyn.com/corp/xweb/xweb.asp?CLID=21001&lang=2", cfg).endswith("&CountryID=FR")
    assert with_own_filters("https://recrute.carrefour.fr/offres", {}).endswith("?order=desc&sort=post_date_extern")  # newest first always
    assert with_own_filters("https://www.capgemini.com/x", {}) == "https://www.capgemini.com/x"  # France not asked
    assert with_own_filters("https://other.fr/jobs", cfg) == "https://other.fr/jobs"


def test_workday_france_offices_without_a_country_list():
    from jobfinder.adapters import _workday_france
    facets = [{"facetParameter": "locations", "values": [{"id": "a", "descriptor": "Paris, France"}, {"id": "b", "descriptor": "Boston, USA"}]}]
    assert _workday_france(facets) == {"locations": ["a"]}
    assert _workday_france([{"facetParameter": "locationCountry", "values": [{"id": "c", "descriptor": "France"}]}]) == {"locationCountry": ["c"]}


def test_job_board_is_not_read_as_an_employer_platform():
    from jobfinder.crawler import detect_platform

    class F:
        def __init__(self, html):
            self.html = html

        def get(self, url, **kw):
            return type("R", (), {"text": self.html})()

    page = "<html><body>" + "x" * 100 + "jobs2web j2w rmk</body></html>"
    assert detect_platform(F(page), "https://jobs.makesense.org/en/s/jobs/all/all?s=data") == \
        ("generic", "https://jobs.makesense.org/en/s/jobs/all/all?s=data")
    assert detect_platform(F(page), "https://careers.acme.fr/search")[0] == "successfactors"


def test_avature_capped_feed_is_not_the_reader():
    from jobfinder.adapters import detect_from_page
    page = '<html><head><link rel="alternate" type="application/rss+xml" title="Job offers" href="/SearchJobs/feed/"></head></html>'
    assert detect_from_page(None, "https://jobs.acme.com/en_US/ext/SearchJobs", page) is None
    assert detect_from_page(None, "https://acme.fr/careers", page)[0] == "rss"


def test_technical_titles_are_never_rejected():
    from jobfinder.classify import analyze_title
    prof = {**CFG()["_profile"], "offer_types": ["job", "internship", "phd_postdoc", "apprenticeship"]}
    for t in ("Ingénieur FPGA F/H", "Stage - Vérification UVM d'IPs digitales", "Développeur CUDA / GPU",
              "Stage – Observateur d'angle moteur haute résolution", "Stage - Beamforming / antennes", "Ingénieur PyTorch",
              "Data Engineer Spark / Databricks", "Stage - Étude des matériaux piézoélectriques pour capteurs"):
        assert analyze_title(t, "", prof).verdict != "reject", t
    for t in ("Commercial grands comptes", "Comptable fournisseurs", "Assistant RH", "Chauffeur livreur"):
        assert analyze_title(t, "", prof).verdict == "reject", t


def test_website_deadline_stops_job_page_opening():
    import time
    from jobfinder.adapters import _Budget
    st = ScanState()
    b = _Budget(CFG(), st)
    assert b.take()
    st.deadline = time.time() - 1  # this website's time is up
    assert not b.take() and st.details_capped


def test_an_exhausted_daily_quota_stops_the_next_requests_at_once(monkeypatch):
    import pytest
    from pydantic import BaseModel
    from jobfinder import llm

    class Ans(BaseModel):
        ok: bool = True

    calls = []

    def refuse(*a, **k):
        calls.append(1)
        e = llm.QuotaExhausted("Daily quota of this model used up")
        e.wait = 58000
        raise e
    monkeypatch.setattr(llm, "chat", refuse)
    monkeypatch.setattr(llm, "_EXHAUSTED", {})
    cfg = {**CFG(), "model": {"provider": "gemini", "model": "gemini-2.5-flash"}, "max_ai_calls": 100}

    class S:
        get = staticmethod(lambda *a: None)
    for _ in range(3):
        with pytest.raises(llm.QuotaExhausted):
            llm.call_json("x", Ans, cfg, S())
    assert len(calls) == 1  # the provider is asked once, not once per website


def test_output_token_limit_is_learned_and_respected(monkeypatch):
    from jobfinder import llm
    sent = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self.text, self._b, self.headers = code, body, body, {}

        def json(self):
            import json
            return json.loads(self._b)

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json["max_tokens"])
        if json["max_tokens"] > 1000:
            return Resp(429, '{"error":{"message":"Request too large for model on output tokens per minute (OTPM): '
                             'Limit 1000, Requested 1500."}}')
        return Resp(200, '{"choices":[{"message":{"content":"{}"}}],"usage":{}}')
    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm, "_OUT_CAP", {})
    llm._post("https://api.groq.com/x", {}, {"max_tokens": 1500}, cap_key=("groq", "m"))
    assert sent == [1500, 900] and llm._OUT_CAP == {("groq", "m"): 900}


def test_workday_is_not_trusted_as_strictly_sorted():
    # Accenture: page 1 runs down to 30+ days, page 2 restarts at today: one old offer must not end the list
    from jobfinder.adapters import crawl_workday
    old = {"title": "Ingénieur IA", "externalPath": "/job/1", "postedOn": "Posted 30+ Days Ago"}
    f = FakeFetcher({"/jobs#": {"total": 40, "jobPostings": [old] * 20, "facets": []}, "/job/1": {"jobPostingInfo": {}}})
    a = dict(host="x.wd3.myworkdayjobs.com", tenant="x", site="S", facets={"locations": ["a" * 32]}, prefix="/S")
    pg = next(iter(crawl_workday(f, a, CFG(), set(), ScanState())))
    assert pg.ordered and not pg.sorted


def test_foreign_location_with_a_street_address_is_outside_france():
    from jobfinder.classify import classify_location as C
    for loc in ("United States ; New York ; 130 West 42nd Street", "Belgium ; Liège", "Switzerland ; Grand-lancy ; Geneva"):
        r = C(loc)
        assert r["location_verified"] and not r["france_eligible"], loc
    assert C("Paris ; London")["france_eligible"] and C("France ; Marseille ; 40c Av. de Hambourg")["france_eligible"]


def test_products_and_symbol_titles_are_not_offers():
    from jobfinder.adapters import jobs_from_json
    from jobfinder.classify import is_boilerplate_title
    games = [{"title": "Rayman Legends Retold", "url": "https://www.ubisoft.com/fr/rayman", "releaseDate": "2026-12-03", "price": 39.9}]
    assert jobs_from_json(games, "https://www.ubisoft.com/fr-fr/company/careers/search") == []
    assert is_boilerplate_title("<") and not is_boilerplate_title("QA Lead")


def test_location_from_the_list_card():
    from jobfinder.crawler import _card_location
    html = '<div><a href="/offre-de-emploi/emploi-x_1.aspx">Technicien H/F</a><span>CDI</span><span>Gard (30)</span><span>Bagnols</span></div>'
    assert _card_location(BeautifulSoup(html, "html.parser"), "https://c.fr/", "https://c.fr/offre-de-emploi/emploi-x_1.aspx",
                          "Technicien H/F") == "Gard (30)"


def test_smartrecruiters_public_list_stops_opening_after_the_first_old_offer():
    from jobfinder.adapters import _smartrecruiters_pages
    li = lambda i: f'<li class="opening-job"><a href="https://jobs.smartrecruiters.com/X/{i}-ing"><h4>Ingénieur IA {i}</h4></a></li>'
    page = lambda d: ('<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Ingénieur IA",'
                      f'"datePosted":"{d}","description":"Poste ingénieur IA"}}</script></head></html>')
    f = FakeFetcher({"page=0": "<ul>" + li(3) + li(2) + li(1) + "</ul>", "/3-ing": page(TODAY), "/2-ing": page(OLD), "/1-ing": page(OLD)})
    pg = next(iter(_smartrecruiters_pages(f, "X", CFG(), set(), ScanState())))
    assert pg.sorted and len(pg.jobs) == 2 and not any("/1-ing" in c for c in f.calls)


def test_ai_reads_the_most_promising_offers_first():
    from datetime import datetime
    from types import SimpleNamespace as NS
    from jobfinder.analysis import _by_priority
    mk = lambda name, verdict, status, day: NS(name=name, extra={"title_info": {"verdict": verdict}}, status=status,
                                               pub_date=datetime(2026, 10, day) if day else None)
    jobs = [mk("maybe new", "maybe", "candidate", 30), mk("pass unknown date", "pass", "review", None),
            mk("pass old", "pass", "candidate", 2), mk("pass new", "pass", "candidate", 29)]
    assert [j.name for j in _by_priority(jobs)] == ["pass new", "pass old", "pass unknown date", "maybe new"]


def test_announced_total_reads_only_real_counters():
    from jobfinder.crawler import announced_total
    n = lambda h: announced_total(BeautifulSoup(h, "html.parser"))
    assert n("<div>Filter</div><span>154\n results</span>") == 154
    assert n("<p>1 - 6 of 1,254 jobs</p>") == 1254
    assert n("<p>12 offres d'emploi</p>") == 12
    assert n("<p>Plus de 5000 postes ouverts dans le monde</p>") is None
    assert n("<a>120 jobs</a><p>Nos offres</p>") is None


def test_wrong_reader_needs_other_offers_from_another_website():
    from jobfinder.crawler import wrong_reader
    page = ["offre a", "offre b", "offre c", "offre d", "offre e"]
    other = [{"title": "Melbourne engineer", "url": "https://employer.successfactors.eu/job/1"}]
    assert wrong_reader(page, other, "https://careers.acme.fr/list")
    assert not wrong_reader(page[:4], other, "https://careers.acme.fr/list")  # too few titles to tell
    assert not wrong_reader(page, [{"title": "Offre A", "url": "https://x.greenhouse.io/1"}], "https://careers.acme.fr/list")
    # its own website: the reader applied the France filter, so the offers may differ from the page
    assert not wrong_reader(page, [{"title": "Autre", "url": "https://jobs.acme.fr/1"}], "https://www.acme.fr/list")


def test_read_outcome_tells_what_a_reading_gave():
    from jobfinder.adapters import ScanState
    from jobfinder.scan import read_outcome

    class S:
        def __init__(self, mem=None):
            self.mem, self.added = mem, []

        def get(self, _, __):
            return self.mem

        def add(self, x):
            self.added.append(x)

    u = "https://a.fr/jobs"
    assert read_outcome(S(), u, "completed", 3, 10, ScanState())[0] == "offers_found"
    assert read_outcome(S(), u, "completed", 0, 10, ScanState())[0] == "nothing_in_range"
    assert read_outcome(S(), u, "completed", 0, 0, ScanState())[0] == "no_offers_detected"
    assert read_outcome(S(), u, "blocked", 0, 0, ScanState()) == ("", "")
    out, note = read_outcome(S(), u, "completed", 20, 20, ScanState(announced=154))
    assert out == "partial" and "154" in note
    # a list left early because of the dates is not compared with the total
    assert read_outcome(S(), u, "completed", 20, 20, ScanState(announced=154, stopped_by_date=True))[0] == "offers_found"
    mem = type("M", (), {"usual": 300})()
    out, note = read_outcome(S(mem), u, "completed", 3, 3, ScanState())
    assert out == "partial" and "300" in note and mem.usual == 300  # a bad reading does not become the new normal


def test_undated_offer_is_hidden_once_first_seen_before_the_range():
    from datetime import datetime, timedelta
    from jobfinder.dates import date_decision
    cfg = CFG(date_preset="Last 7 days")
    now = datetime(2026, 11, 1)
    assert date_decision(now - timedelta(days=20), 1.0, cfg, now)[0] is False
    assert date_decision(now - timedelta(days=2), 1.0, cfg, now)[0] is True


def test_suggest_page_finds_the_new_address():
    from jobfinder.crawler import suggest_page

    class F:
        def get(self, url, **kw):
            html = {"https://lab.fr/": '<a href="/contact">Contact</a><a href="/old">Emploi</a><a href="/rejoindre">Nous rejoindre</a>',
                    "https://lab.fr/rejoindre": "<h1>Nous rejoindre</h1>" + "<p>texte de la page</p>" * 30}.get(url)
            if html is None:
                raise ConnectionError("HTTP 404")
            return type("R", (), {"text": html})()

        def robots_ok(self, url):
            return True

    assert suggest_page(F(), "https://lab.fr/old") == "https://lab.fr/rejoindre"


def test_elasticsearch_results_are_read():
    from jobfinder.adapters import jobs_from_json
    data = {"hits": {"total": {"value": 1}, "hits": [{"_id": "ab", "_score": 0, "_source": {
        "url": "https://careers.ibm.com/careers/JobDetail?jobId=134659", "title": "Développeur Cobol H/F"}}]}}
    assert [j["title"] for j in jobs_from_json(data, "https://www.ibm.com/careers/search")] == ["Développeur Cobol H/F"]


def test_offer_links_with_the_id_in_the_query_and_reference_as_first_link():
    from jobfinder.crawler import _card_title, _job_links
    html = """<table><tr><td><a href="xweb.asp?clid=1&Page=JobDetails&Jobid=J1026-2917">J1026-2917</a></td>
              <td><a href="xweb.asp?clid=1&Page=JobDetails&Jobid=J1026-2917">Ingénieur vision par ordinateur</a></td></tr></table>
              <a href="xweb.asp?clid=1&Page=Help">Aide</a>"""
    base = "https://cgi.njoyn.com/corp/xweb/xweb.asp?clid=1"
    soup = BeautifulSoup(html, "html.parser")
    links = _job_links(soup, base, set())
    assert len(links) == 1 and "Jobid=J1026-2917" in links[0]
    assert _card_title(soup, base, links[0]) == "Ingénieur vision par ordinateur"


def test_card_title_used_when_job_pages_share_the_website_heading():
    html_list = """<a href="/fr/offres-emplois/ingenieur-a"><h3>Ingénieur prototypage</h3></a>
                   <a href="/fr/offres-emplois/doctorant-b"><h3>Doctorant composition assistée par IA</h3></a>"""
    body = "<h1>Découvrir le laboratoire</h1><main>" + "<p>Missions du poste, profil, compétences, candidature, contrat, stage.</p>" * 12 + "</main>"

    class F(FakeFetcher):
        def get(self, url, **kw):
            return type("R", (), {"text": html_list if url.endswith("/jobs") else body, "url": url, "status_code": 200})()

        def robots_ok(self, url):
            return True

        def note(self, m):
            pass

    from jobfinder.crawler import crawl_generic
    cfg = {**CFG(), "_profile": {**CFG()["_profile"], "offer_types": ["job", "internship", "phd_postdoc", "apprenticeship"]}}
    titles = {j["title"] for pg in crawl_generic(F({}), "https://lab.fr/jobs", cfg, set(), ScanState()) for j in pg.jobs}
    assert "Ingénieur prototypage" in titles and "Découvrir le laboratoire" not in titles


def test_list_page_found_from_a_careers_home_page():
    from jobfinder.crawler import _list_page
    soup = BeautifulSoup('<a href="/en/about">About</a><a href="https://r.michelin.fr/en/job-offer-result-list">Apply</a>', "html.parser")
    assert _list_page(soup, "https://r.michelin.fr/en") == "https://r.michelin.fr/en/job-offer-result-list"
    assert _list_page(BeautifulSoup('<a href="/en/about">About</a>', "html.parser"), "https://r.michelin.fr/en") == ""
    soup = BeautifulSoup('<a href="/en/jobsearch/application">How to apply</a><a href="/en/job-offer-result-list">Jobs</a>', "html.parser")
    assert _list_page(soup, "https://r.michelin.fr/en") == "https://r.michelin.fr/en/job-offer-result-list"


def test_next_page_written_in_a_form_field():
    from jobfinder.crawler import _next_page
    soup = BeautifulSoup('<input type="hidden" name="pn" value="1"><a href="javascript:gotopage(2)">PROCHAIN</a>', "html.parser")
    assert _next_page(soup, "https://c.com/x.asp?CLID=1", 1) == "https://c.com/x.asp?CLID=1&pn=2"
    soup = BeautifulSoup('<input type="hidden" name="pn" value="2"><a href="javascript:gotopage(3)">PROCHAIN</a>', "html.parser")
    assert _next_page(soup, "https://c.com/x.asp?CLID=1&pn=2", 2) == "https://c.com/x.asp?CLID=1&pn=3"


def test_title_with_only_the_kind_of_offer_is_opened():
    from jobfinder.classify import analyze_title
    prof = {**CFG()["_profile"], "offer_types": ["job", "internship"]}
    assert analyze_title("Stagiaire", "", prof).verdict == "maybe"
    assert analyze_title("Stage comptabilité fournisseurs", "", prof).verdict == "reject"


def test_openrouter_daily_limit_stops_at_once():
    from unittest.mock import patch
    import time as _t
    import pytest
    from jobfinder import llm

    class R:
        status_code, text = 429, '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'
        headers = {"x-ratelimit-reset": str(int((_t.time() + 7200) * 1000))}

    with patch.object(llm.requests, "post", return_value=R()) as post, pytest.raises(llm.QuotaExhausted) as e:
        llm._post("https://openrouter.ai/x", {}, {})
    assert post.call_count == 1 and "Daily quota" in str(e.value) and 7000 < e.value.wait < 7300


def _ladder(monkeypatch, answers):
    from jobfinder import llm
    from pydantic import BaseModel

    class A(BaseModel):
        ok: bool

    class S:
        def get(self, *_):
            return None

        def merge(self, _):
            pass

        def commit(self):
            pass

    calls = []

    def chat(prov, model, system, user, cfg, keys=None):
        calls.append((prov, model, keys[prov]))
        got = answers.get((prov, model, keys[prov]), '{"ok": true}')
        if isinstance(got, Exception):
            raise got
        return got, 1, 1

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "_EXHAUSTED", {})
    monkeypatch.setattr(llm, "_HEALTH", {})  # each test judges the models afresh
    llm.reset_usage()
    keys = {"gemini": "g1", "gemini#2": "g2", "groq": "q", "openrouter": "o"}
    cfg = {"auto_models": True, "max_ai_calls": 100, "model": {"provider": "ollama", "model": "llama3.1"}}
    return llm, A, S(), cfg, keys, calls


def test_ladder_order_starts_with_the_best_and_skips_missing_keys():
    from jobfinder import llm
    cfg = {"auto_models": True, "model": {"provider": "ollama", "model": "llama3.1"}}
    chain = llm.model_chain(cfg, {"gemini": "g1", "gemini#2": "g2", "groq": "q"})
    assert chain[:3] == [("gemini", "gemini-3.6-flash", "gemini"), ("gemini", "gemini-3.6-flash", "gemini#2"),
                         ("gemini", "gemini-3.5-flash", "gemini")]
    assert chain[4] == ("groq", "openai/gpt-oss-120b", "groq")  # the next provider after both keys of the two best models
    assert not any(p == "openrouter" for p, _, _ in chain) and chain[-1][0] == "ollama"
    assert llm.model_chain({"auto_models": False, "model": {"provider": "groq", "model": "x"}}, {}) == [("groq", "x", "groq")]


def test_limit_moves_to_the_second_key_then_another_provider(monkeypatch):
    from jobfinder import llm as L
    q = L.QuotaExhausted("Daily quota")
    q.wait = 7200
    gem = [("gemini", m, k) for m in ("gemini-3.6-flash", "gemini-3.5-flash") for k in ("g1", "g2")]
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {x: q for x in gem})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert calls == gem + [("groq", "openai/gpt-oss-120b", "q")]
    assert llm.USAGE["model"] == "groq / openai/gpt-oss-120b"
    calls.clear()
    assert llm.call_json("u2", A, cfg, s, keys).ok  # the used-up models are not asked again...
    assert calls == [("groq", "openai/gpt-oss-120b", "q")]
    llm._EXHAUSTED.clear()  # ...until their limit has passed: the best model is back
    calls.clear()
    llm.call_json("u3", A, cfg, s, keys)
    assert calls[0][1] == "gemini-3.6-flash"


def test_gone_model_and_bad_answer_move_on(monkeypatch):
    from jobfinder import llm as L
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"): L.AIError("HTTP 404: no longer available"),
                                                        ("gemini", "gemini-3.6-flash", "g2"): "not json"})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert calls[-1] == ("gemini", "gemini-3.5-flash", "g1")  # the next model of the ladder
    assert llm._EXHAUSTED[("gemini", "gemini-3.6-flash", "gemini")] > __import__("time").time() + 3000


def test_all_models_at_their_limit_gives_one_clear_message(monkeypatch):
    from jobfinder import llm as L
    q = L.QuotaExhausted("Daily quota")
    q.wait = 7200
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(q))
    import pytest
    with pytest.raises(L.QuotaExhausted, match="AI models have reached"):
        llm.call_json("u", A, {**cfg, "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}}, s, keys)


def test_cv_consent_covers_every_provider_of_the_ladder():
    import pytest
    from jobfinder import llm
    keys = {"gemini": "g", "groq": "q"}
    cfg = {"auto_models": True, "_keys": keys, "cv_consent_providers": ["gemini"], "send_cv_remote_ok": True,
           "model": {"provider": "ollama", "model": "llama3.1"}}
    with pytest.raises(llm.AIError, match="groq"):  # the older single-provider consent does not count
        llm.require_cv_consent(cfg)
    llm.require_cv_consent({**cfg, "cv_consent_providers": ["gemini", "groq"]})


def test_xai_comes_last_and_an_account_without_credits_is_said_clearly(monkeypatch):
    import pytest
    from jobfinder import llm
    cfg = {"auto_models": True, "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}}
    assert llm.model_chain(cfg, {"gemini": "g", "groq": "q", "xai": "x"})[-1] == ("xai", "grok-4.3", "xai")

    class R:
        status_code, text, headers = 403, "Your team has used all available credits or reached its monthly spending limit", {}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    with pytest.raises(llm.AIError, match="no credits"):
        llm._post("https://api.x.ai/v1/chat/completions", {}, {})


def test_second_groq_key_and_monthly_limit(monkeypatch):
    from jobfinder import llm as L
    chain = L.model_chain({"auto_models": True, "model": {"provider": "ollama", "model": "x"}}, {"groq": "a", "groq#2": "b"})
    assert chain[:2] == [("groq", "openai/gpt-oss-120b", "groq"), ("groq", "openai/gpt-oss-120b", "groq#2")]
    gone = L.AIError("Provider error 429: limited to 1000 API calls / month")
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"): gone})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert llm._EXHAUSTED[("gemini", "gemini-3.6-flash", "gemini")] > __import__("time").time() + 3000


def test_next_page_called_from_a_click_and_sitemap_job_paths():
    from jobfinder.crawler import _next_page, sitemap_jobs
    soup = BeautifulSoup("""<input type="hidden" name="Page" value="1"><a onclick="toPage('SearchForm', 2)">Page suivante</a>""", "html.parser")
    assert _next_page(soup, "https://emploi.cnrs.fr/Offres/Recherche.aspx", 1) == "https://emploi.cnrs.fr/Offres/Recherche.aspx?Page=2"

    class F:
        def sitemaps(self, url):
            return ["https://emploi.sncf.com/sitemap-jobs.xml"]

        def get(self, url, **k):
            return type("R", (), {"text": "<url><loc>https://emploi.sncf.com/nos-offres/706050</loc></url>"})()

        def robots_ok(self, url):
            return True

    assert sitemap_jobs(F(), "https://emploi.sncf.com/nos-offres") == [("https://emploi.sncf.com/nos-offres/706050", "")]


def test_logo_heading_vague_age_and_error_page_redirect(monkeypatch):
    from datetime import datetime
    from jobfinder.adapters import _parse_detail
    from jobfinder.dates import date_decision, parse_date
    from jobfinder.web import AccessError, Fetcher
    # PSL: the first <h1> is the site's logo linking home, the offer's title comes after it
    page = ('<html><head><title>Chaire IA | PSL</title></head><body><h1><a href="/">Recrutement</a></h1>'
            '<main><h1>Chaire de professeur junior IA (F/H)</h1><p>publié le 02/11/2026</p></main></body></html>')
    assert _parse_detail(page, "https://recrutement.psl.eu/odyssee-1")["title"] == "Chaire de professeur junior IA (F/H)"
    from jobfinder.crawler import _card_title
    card = BeautifulSoup('<a href="/uzlf8yhb7f"><div class="title_etablissement">MINES Paris - PSL</div>'
                         '<div class="title">Assistant.e de direction (F/H)</div><div class="created">02.11.2026</div></a>', "html.parser")
    assert _card_title(card, "https://recrutement.psl.eu/nos-offres", "https://recrutement.psl.eu/uzlf8yhb7f") == \
        "Assistant.e de direction (F/H)"
    # 'il y a 1 an' is vague (confidence 0.45) but surely outside the last 30 days; '2 semaines' stays decided normally
    now = datetime(2026, 11, 2)
    d, c = parse_date("publiée il y a 1 an", now)
    assert date_decision(d, c, {"date_preset": "Last 30 days"}, now, "publiée il y a 1 an")[0] is False
    d, c = parse_date("30+ days ago", now)
    assert date_decision(d, c, {"date_preset": "Last 30 days"}, now, "30+ days ago")[0] is None
    assert date_decision(d, c, {"date_preset": "Last 7 days"}, now, "30+ days ago")[0] is False
    # LISN: every page redirects to '/403.html' with status 200: blocked, not 'no offers'

    class R:
        def __init__(self, code, loc=None):
            self.status_code, self.headers, self.text, self.encoding = code, {"Location": loc} if loc else {}, "<p>x</p>", "utf-8"
    from jobfinder.config import CONFIG_DEFAULT, run_config
    f = Fetcher({**run_config(CONFIG_DEFAULT), "retries": 0, "timeout": 5}, lambda m: None)
    monkeypatch.setattr(f, "robots_ok", lambda u: True)
    monkeypatch.setattr(f, "_wait", lambda h, asked=0.0: None)
    monkeypatch.setattr("jobfinder.web.check_url", lambda u: (True, ""))
    monkeypatch.setattr(f.s, "request", lambda m, u, **k: R(302, "https://rp.lisn.fr/403.html") if "403" not in u else R(200))
    try:
        f.get("https://www.lisn.fr/job-offers")
        assert False, "the refusal was not seen"
    except AccessError as e:
        assert "access_restricted" in str(e)


def test_a_model_the_provider_removed_is_skipped_until_next_week(monkeypatch):
    from jobfinder import llm as L
    L_error = L.AIError('HTTP 404: {"message": "This model models/gemini-2.5-flash is no longer available to new users"}')
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"): L_error})
    assert llm.call_json("u", A, cfg, s, keys).ok
    chain = llm.model_chain(cfg, keys)
    assert ("gemini", "gemini-3.6-flash", "gemini") not in chain and ("gemini", "gemini-3.6-flash", "gemini#2") in chain


def test_a_scan_in_another_process_is_never_wiped(monkeypatch, tmp_path):
    import subprocess
    import sys
    from jobfinder import store
    monkeypatch.setattr(store, "SCAN_LOCK", tmp_path / "scan.lock")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "scan.lock").write_text(str(other.pid))
        assert store.scan_running_elsewhere() and store.wipe_session_data() is False
        try:
            store.load_new_sources(None, ["https://a.fr/jobs"])
            assert False, "websites were replaced during a scan"
        except RuntimeError:
            pass
    finally:
        other.kill()
        other.wait()
    assert not store.scan_running_elsewhere()  # the process has stopped: its lock no longer counts
    store.lock_scan()
    assert not store.scan_running_elsewhere()  # this process's own scan
    store.unlock_scan()
    assert not (tmp_path / "scan.lock").exists()


def test_health_report_compares_each_website_with_its_previous_scan():
    from datetime import datetime
    from jobfinder.scan import health_note
    from jobfinder.store import SiteMemory

    class S:
        def __init__(self):
            self.rows = {}

        def flush(self):
            pass

        def get(self, _, url):
            return self.rows.get(url)

        def add(self, row):
            self.rows[row.url] = row
    s, src = S(), type("Src", (), {})()
    src.url, src.status, src.listed = "https://co.fr/jobs", "completed", 120
    assert health_note(s, src, "Last 7 days") == ""  # first scan: the reference
    s.rows[src.url].last_scan = datetime(2026, 11, 1)
    src.status, src.listed = "blocked", 0
    assert health_note(s, src, "Last 7 days") == "read on 2026-11-01 (120 offers on its lists), now blocked"
    src.status, src.listed = "completed", 30
    assert health_note(s, src, "Last 7 days").endswith("read again now")
    s.rows[src.url].last_listed = 120
    assert "the website may have changed" in health_note(s, src, "Last 7 days")
    s.rows[src.url].last_listed, src.listed = 120, 30
    assert health_note(s, src, "Last 30 days") == ""  # another date range: list sizes are not comparable
    assert isinstance(s.rows[src.url], SiteMemory)


def test_softy_platform_offers_with_place_date_and_description():
    import json
    from html import escape
    from jobfinder.adapters import crawl_softy

    def page(props, esc=False):
        data = json.dumps({"props": props})
        return f'<div id="app"></div><script data-page="app" type="application/json">{escape(data) if esc else data}</script>'
    item = lambda i, title, day: {"id": i, "softy_id": 900 + i, "title": title, "updated_at": f"{day} 10:00:00",
                                  "contract_type": {"name": "CDD"}, "subsidiary_name": "Occitanie",
                                  "locations": [{"city": "Montpellier", "country": "France"}]}
    lst = page({"offers": {"data": [item(1, "Ingénieur d'études en machine learning - H/F", TODAY),
                                    item(2, "Post-doctorant en vision par ordinateur - H/F", OLD)],
                           "last_page": 1, "next_page_url": None}})
    det = page({"offer": {"description_html": "<p>Apprentissage profond pour l'imagerie médicale.</p>"}}, esc=True)
    f = FakeFetcher({"/offers?page=1": lst, "/offers/901": det, "/offers/902": det})
    jobs = [j for pg in crawl_softy(f, "https://inserm.softy.pro", CFG(), set(), ScanState()) for j in pg.jobs]
    assert [j["title"][:10] for j in jobs] == ["Ingénieur ", "Post-docto"]
    assert jobs[0]["location"] == "Montpellier, France" and jobs[0]["employment_type"] == "CDD"
    assert jobs[0]["url"] == "https://inserm.softy.pro/offers/901" and "imagerie" in jobs[0]["description"]
    assert jobs[0]["date"].strftime("%Y-%m-%d") == TODAY and jobs[0]["date_conf"] == 0.6
    assert detect_from_url("https://inserm.softy.pro/offre/191137?idt=190") == ("softy", "https://inserm.softy.pro")


def test_offers_published_as_pdf_files():
    import pymupdf
    from jobfinder.crawler import crawl_pdfs
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), "Laboratoire d'Imagerie Biomédicale\n15 rue de l'École de Médecine, 75006 Paris\n"
                               "Segmentation d'images IRM par apprentissage profond\nType de stage : Master 2, 6 mois, Paris.\n"
                               + "Le stagiaire développera des réseaux de neurones pour la segmentation. " * 6)
    doc.set_metadata({"creationDate": f"D:{TODAY.replace('-', '')}101500"})
    pdf = doc.tobytes()
    lst = ('<main>'
           '<div class="offre"><h3><a href="/segmentation-irm/">Segmentation IRM par deep learning</a></h3>'
           '<a href="/wp-content/uploads/2026/10/Annonce_Stage_IRM.pdf">Télécharger le sujet de stage</a></div>'
           f'<div class="offre"><p><a href="/files/{TODAY[:4]}/{TODAY[5:7]}/Sujet_stage_IA.pdf">Sujet de stage</a></p></div>'
           '<div class="offre"><h3>Ancien sujet de stage en imagerie médicale</h3>'
           '<a href="/closed/2025/01/Annonce_Stage_old.pdf">Télécharger le sujet de stage</a></div></main>')
    post = ('<html><head><link rel="alternate" type="application/json" href="https://lab.fr/wp-json/wp/v2/posts/12"></head>'
            '<body><h1><a href="/">LIB</a></h1><main>Segmentation IRM : ' + "missions, profil, candidature. " * 30 + '</main></body></html>')
    R = type("R", (), {})

    class F(SiteFetcher):
        def get(self, url, **kw):
            self.calls.append(url)
            r = R()
            r.url, r.status_code = url, 200
            r.text = {"https://lab.fr/stages": lst, "https://lab.fr/segmentation-irm": post}.get(url.rstrip("/"), "")
            r.content = pdf if url.endswith("Sujet_stage_IA.pdf") else b""
            r.json = lambda: {"date": f"{TODAY}T17:10:16"}
            return r
    f = F({})
    jobs = [j for pg in crawl_pdfs(f, "https://lab.fr/stages", CFG(), set(), ScanState()) for j in pg.jobs]
    by = {j["url"].rsplit("/", 1)[-1]: j for j in jobs}
    page = by["segmentation-irm"]  # the card's own offer page, dated by its WordPress data
    assert page["url"] == "https://lab.fr/segmentation-irm" and page["date"].strftime("%Y-%m-%d") == TODAY
    assert page["date_source"] == "wordpress api post date"
    read = next(j for j in jobs if j["provenance"] == "pdf")  # no card page: the PDF itself, its title skips the letterhead
    assert read["title"].startswith("Segmentation d'images IRM") and "Paris" in read["location"]
    assert read["date_source"] == "pdf creation date" and "neurones" in read["description"]
    old = by["Annonce_Stage_old.pdf"]  # robots.txt closes the PDF: the card's title, and its upload month proves it is too old
    assert old["provenance"] == "list-only" and old["date"].strftime("%Y-%m-%d") == "2025-01-31" and old["date_conf"] == 0.8
    assert not any("/closed/" in u for u in f.calls)


def test_next_page_sent_as_a_form_like_a_browser():
    from jobfinder.crawler import _form_post
    soup = BeautifulSoup("""<form id="SearchForm" method="post" action="../../Offres/Recherche.aspx">
        <input type="text" name="q" value=""><input type="checkbox" name="ContractType" value="CDD">
        <input type="checkbox" name="Kept" value="1" checked><input type="hidden" name="Page" value="0">
        <input type="hidden" name="__VIEWSTATE" value="abc"><input type="submit" name="go" value="OK"></form>
        <a onclick="toPage('SearchForm', 2)">2</a>""", "html.parser")
    url = "https://emploi.cnrs.fr/Offres/Recherche.aspx"
    action, data = _form_post(soup, url, f"{url}?Page=2")
    assert action == url and data == [("q", ""), ("Kept", "1"), ("Page", "2"), ("__VIEWSTATE", "abc")]
    assert _form_post(BeautifulSoup('<form method="get"><input type="hidden" name="page" value="1"></form>', "html.parser"),
                      "https://a.fr/jobs", "https://a.fr/jobs?page=2") is None  # a GET form: the address itself works


def test_sitemap_without_lastmod_stops_after_a_long_run_of_old_offers():
    sm = "".join(f"<url><loc>https://co.fr/jobs/data-scientist-machine-learning-{i}</loc></url>" for i in range(120))
    page = lambda i: (f'<html><head><script type="application/ld+json">{{"@type": "JobPosting", "title": "Data Scientist {i}",'
                      f'"datePosted": "{TODAY if i < 5 else OLD}", "description": "missions profil candidature"}}</script></head></html>')
    f = SiteFetcher({"sitemap.xml": sm, "/jobs/data-scientist-machine-learning-": lambda u: page(int(u.rsplit("-", 1)[1]))})
    st = ScanState()
    jobs = [j for pg in crawl_sitemap(f, "https://co.fr/jobs", CFG(), set(), st) for j in pg.jobs]
    assert len(jobs) == 55 and st.stopped_by_date  # 5 recent offers, then 50 old ones in a row: the other 65 are not opened


def test_only_lab_page_readings_survive_a_wipe(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.config import utcnow
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    with store.SessionLocal() as s:
        s.add_all([store.AICache(key="lab", value="{}", kind="PageOffers"),
                   store.AICache(key="old-lab", value="{}", kind="PageOffers", created=utcnow() - timedelta(days=31)),
                   store.AICache(key="cv-match", value="{}", kind="Relevance"), store.AICache(key="before", value="{}")])
        s.commit()
    assert store.wipe_session_data()
    with store.SessionLocal() as s:
        assert [r.key for r in s.query(store.AICache)] == ["lab"]  # CV-related answers and old readings are deleted


def test_page_locations_lose_buttons_and_street():
    from jobfinder.classify import classify_location, tidy_place
    assert tidy_place("PSL 60 rue Mazarine 75006 Paris Télécharger en") == "75006 Paris"
    assert tidy_place("4 place Jussieu 75005 Paris Cedex 05") == "75005 Paris"
    assert tidy_place("Paris - Voir sur la carte") == "Paris" and tidy_place("Boulogne-Billancourt, France") == "Boulogne-Billancourt, France"
    assert classify_location(tidy_place("12 avenue de la Paix 91190 Gif-sur-Yvette"))["france_eligible"]


def test_the_offer_type_written_in_the_title_beats_the_ai(monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import analysis, store
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    wrong = analysis.JobAnalysis(contract_kind="job", role_type="data", is_relevant=True, relevance_score=90,
                                 analysis_confidence=0.9, france_eligible=True)  # a small model reading 'Stage' as a job
    monkeypatch.setattr(analysis, "call_json", lambda *a, **k: wrong.model_copy())
    prof = {**PROFILE_DEFAULT, "offer_types": ["internship"]}
    with sessionmaker(bind=engine)() as s:
        job = store.Job(uid="1", title="Stage - Data Scientist (H/F)", url="https://a.fr/1", description="Stage de 6 mois. " * 20,
                        pub_date=datetime.utcnow(), date_conf=0.9, location_verified=True, france_eligible=True, extra={})
        s.add(job)
        s.commit()
        res = analysis.analyze_job(s, job, prof, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"}))
        assert res.contract_kind == "internship" and job.status != "excluded"


def test_an_ai_slip_never_drops_an_offer_whose_title_matches(monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import analysis, store
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    slip = analysis.JobAnalysis(contract_kind="internship", role_type="other", is_relevant=False, relevance_score=20,
                                analysis_confidence=0.6, france_eligible=True)
    monkeypatch.setattr(analysis, "call_json", lambda *a, **k: slip.model_copy())
    cfg = run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"})
    prof = {**PROFILE_DEFAULT, "offer_types": ["internship", "job"]}
    with sessionmaker(bind=engine)() as s:
        mk = lambda i, t: store.Job(uid=str(i), title=t, url=f"https://a.fr/{i}", description="Missions et profil. " * 20,
                                    pub_date=datetime.utcnow(), date_conf=0.9, location_verified=True, france_eligible=True, extra={})
        match, other = mk(1, "Stage - Data Scientist machine learning (H/F)"), mk(2, "Stage - Assistant(e) événementiel (H/F)")
        s.add_all([match, other])
        s.commit()
        analysis.analyze_job(s, match, prof, cfg)
        analysis.analyze_job(s, other, prof, cfg)
        assert match.status == "review" and "title_matches_your_profile" in match.review_flags
        assert other.status == "excluded"  # no match in the title: the AI's 'no' stands
        broad = mk(3, "Chargé de mission IA - transformation numérique (H/F)")  # one broad word only: the AI decides
        s.add(broad)
        s.commit()
        analysis.analyze_job(s, broad, prof, cfg)
        assert broad.status == "excluded"


def test_store_finance_hr_roles_are_rejected_but_technical_ones_stay():
    from jobfinder.classify import analyze_title
    from jobfinder.config import PROFILE_DEFAULT
    prof = {**PROFILE_DEFAULT, "offer_types": ["job", "internship", "phd_postdoc", "apprenticeship"]}
    for t in ("Hôte de caisse CQP en alternance", "Analyste Financier - en stage", "Contrôle de Gestion Corporate – en stage",
              "Acheteur Transport – en stage", "Assistant ressources humaines en alternance"):
        assert analyze_title(t, "", prof).verdict == "reject", t
    for t in ("Stage - Data Scientist (H/F)", "Ingénieur achats data et IA", "Stage - Analyste data RH",
              "Alternance - Ingénieur Machine Learning"):
        assert analyze_title(t, "", prof).verdict != "reject", t


def test_every_offer_gets_the_link_that_opens_it():
    from jobfinder.web import offer_link
    assert offer_link("https://co.fr/job/123-data-scientist", "Data Scientist") == "https://co.fr/job/123-data-scientist"
    # written as text on a lab page: the page, scrolled to the offer's title ('-' and ',' escaped in the anchor)
    assert offer_link("https://lab.fr/jobs?lang=fr&jboffer=ab12", "Post-doc - Vision 3D, robotique") == \
        "https://lab.fr/jobs?lang=fr#:~:text=Post%2Ddoc%20%2D%20Vision%203D%2C%20robotique"
    # an id without address, but the data names the offer's job-platform page: that page
    assert offer_link("https://co.fr/careers?jbid=991", "Data", "https://co.wd3.myworkdayjobs.com/x/job/Paris/Data_R991") == \
        "https://co.wd3.myworkdayjobs.com/x/job/Paris/Data_R991"
    assert offer_link("https://co.fr/careers?page=2&jbcard=ff00&x=1", "Ingénieur ML").startswith("https://co.fr/careers?page=2&x=1#:~:text=")


def test_exact_links_orange_lab_cards_and_ids_without_address():
    from jobfinder.adapters import _phenom_arg
    from jobfinder.crawler import _ids_to_links
    from jobfinder.pagereader import _own_link
    # Phenom site opened at its root: offers open at '/gb/en/job/<id>', never at '/job/<id>' (orange.jobs)
    a = _phenom_arg("https://orange.jobs/", '<script>phApp = {"locale":"en_gb", "refNum":"ORANGE"}</script> phenom')
    assert a is None or a["prefix"] == "/gb/en"
    # lab page: each card holds its PDF BEFORE its title; every offer gets the PDF of its own card
    soup = BeautifulSoup('<ul><li><a href="/pdf/3Dupdate.pdf">pdf</a><p>Change detection and updating of 3D models</p></li>'
                         '<li><a href="/pdf/sunglint.pdf">pdf</a><p>Detection and correction of sunglint</p></li></ul>', "html.parser")
    titles = ["Change detection and updating of 3D models", "Detection and correction of sunglint"]
    assert _own_link(soup, titles[1], titles, "https://lab.fr/join") == "https://lab.fr/pdf/sunglint.pdf"
    assert _own_link(soup, titles[0], titles, "https://lab.fr/join") == "https://lab.fr/pdf/3Dupdate.pdf"
    # ids without address next to offers whose address holds their id
    jobs = [{"url": "https://c.fr/carriere/2420", "apply_url": "", "platform_id": "2420"},
            {"url": "https://c.fr/carriere/2421", "apply_url": "", "platform_id": "2421"},
            {"url": "https://c.fr/?jbid=1655", "apply_url": "", "platform_id": "1655"}]
    fresh = lambda: [dict(j) for j in jobs]
    assert _ids_to_links(fresh())[2]["url"] == "https://c.fr/carriere/1655"
    assert _ids_to_links(fresh()[1:])[1]["url"] == "https://c.fr/?jbid=1655"  # one example is not a pattern


def test_an_offer_redated_by_the_website_keeps_the_day_it_was_first_seen():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import apply_filters
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    seen = (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d")
    with sessionmaker(bind=engine)() as s:
        mk = lambda i: store.Job(uid=str(i), title="Ingénieur Machine Learning (H/F)", url=f"https://a.fr/{i}", status="new",
                                 pub_date=datetime.utcnow(), date_conf=0.9, date_source="listing card", location_original="Paris",
                                 extra={"first_seen": seen})
        a, b = mk(1), mk(2)
        s.add_all([a, b]); s.commit()
        apply_filters(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 30 days"}), PROFILE_DEFAULT)
        assert a.status in ("candidate", "review") and a.pub_date.strftime("%Y-%m-%d") == seen and "re-dated" in a.date_source
        b.status = "new"; s.commit()
        apply_filters(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"}), PROFILE_DEFAULT)
        assert b.status == "out_of_range"  # 'published today' on the website, but online for 20 days


def test_an_offer_without_a_date_is_returned_in_its_normal_list():
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import apply_filters
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        j = store.Job(uid="1", title="Ingénieur Machine Learning (H/F)", url="https://a.fr/1", status="new", pub_date=None,
                      location_original="Paris, France", extra={"first_seen": datetime.utcnow().strftime("%Y-%m-%d")})
        s.add(j); s.commit()
        apply_filters(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"}), PROFILE_DEFAULT)
        assert j.status == "candidate" and "publication_date_unknown" in j.review_flags


def test_the_same_offer_on_two_websites_is_shown_once_with_its_earliest_date():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import merge_duplicates
    from jobfinder.config import CONFIG_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    d1, d2 = datetime.utcnow() - timedelta(days=2), datetime.utcnow() - timedelta(days=1)
    with sessionmaker(bind=engine)() as s:
        own, board = store.Source(url="https://careers.thalesgroup.com/global/en/search-results", label="Thales"), \
            store.Source(url="https://www.stage.fr/jobs/", label="Stage")
        s.add_all([own, board]); s.commit()
        mk = lambda i, src, d, t="STAGE - Ingénieur analyste Data Science (H/F)": store.Job(
            uid=str(i), source_id=src.id, title=t, employer="Thales", city="Vélizy", url=f"https://x.fr/{i}",
            pub_date=d, date_conf=0.9, date_source="api", status="candidate", description="x" * 300, extra={})
        a, b, c = mk(1, board, d1), mk(2, own, d2), mk(3, own, d2, "STAGE - Ingénieur analyste Data Science 2 (H/F)")
        s.add_all([a, b, c]); s.commit()
        assert merge_duplicates(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"})) == 1
        assert b.status == "candidate" and b.pub_date == d1 and "earliest of 2 copies" in b.date_source  # own site, earliest date
        assert a.status == "excluded" and a.reason.startswith("duplicate") and c.status == "candidate"


def test_robots_refusing_one_offer_page_keeps_the_offer_but_a_refused_site_is_blocked():
    from jobfinder.adapters import _Budget, _open_detail, _raw
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError, RobotsRefused

    class F(SiteFetcher):
        def get(self, url, **kw):
            if "/wp-content/" in url or "/closed-site/" in url:
                raise RobotsRefused("access_restricted: disallowed by robots.txt")
            return super().get(url, **kw)
    raw = _raw("Stage - Data Scientist (H/F)", "https://lab.fr/wp-content/uploads/offre.pdf")
    st = ScanState()
    assert _open_detail(F({}), CFG(), raw, _Budget(CFG(), st)) is raw  # the offer stays, without its closed document
    try:
        list(crawl_source(F({}), "https://lab.fr/closed-site/jobs", CFG(), set(), st))
        assert False, "a website whose own page is refused must be reported as blocked"
    except AccessError:
        pass


def test_french_places_come_from_the_official_list_and_foreign_countries_are_known():
    from jobfinder.classify import classify_location, fr_place
    for loc in ("GIF SUY YVETTE", "Gif sur Yvette", "Villeneuve d'Ascq", "Bouches du Rhône (13) ; Saint-Paul-lez-Durance",
                "Porto-Vecchio", "Saint-Maurice", "Dauphine - PSL", "Is-sur-Tille", "Hybrid - Paris"):
        assert classify_location(loc)["france_eligible"], loc
    assert fr_place("Saint-Paul-lez-Durance") == "Saint-Paul-lès-Durance"  # the longest official name, 'lez' = 'lès'
    for loc in ("6 Duntes Street, Riga, Latvia", "Luis Alberto de Herrera, Montevideo, Uruguay", "Kolao Tower, Vientiane, Laos",
                "The HeadHunter Ltd, Rr., Pristina, Kosovo", "Saint Louis, MO", "New Orleans, LA", "US, CA, Santa Clara"):
        r = classify_location(loc)
        assert r["location_verified"] and not r["france_eligible"], loc
    for loc in ("Vienne", "Sales", "2 sites", "1758"):  # Vienna or the French town, a word, a count, a number: unknown
        assert not classify_location(loc)["location_verified"], loc


def test_the_place_written_in_an_offer_text_without_a_label():
    from jobfinder.classify import location_from_text as lt
    assert lt("Retour aux offres Analyste Mainframe (H/F) à Lyon calendar_month 17/08/2026 signature CDI location_on Lyon") == "Lyon"
    assert lt("Retour Data Chef de projet BI & Big Data (F/H) Paris Il y a 6 jours Postuler") == "Paris"
    assert lt("Nous recherchons pour notre agence de Lille des profils Data Analyst") == "Lille"
    assert lt("We are a startup based in London working on AI") == "London"
    assert lt("Ingénieur (H/F) Réf. : 2026-123 CDI Postuler") == ""  # a cue without a real place: nothing


def test_an_unverified_location_is_confirmed_by_the_offer_text_only_for_france():
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import apply_filters
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    today = datetime.utcnow()
    with sessionmaker(bind=engine)() as s:
        mk = lambda i, loc, desc: store.Job(uid=str(i), title="Data Engineer (H/F)", url=f"https://a.fr/{i}", status="new",
                                            pub_date=today, date_conf=0.9, location_original=loc, description=desc, extra={})
        fr, us = mk(1, "2 sites", "Nous recherchons pour notre agence de Lille un Data Engineer"), \
            mk(2, "2 sites", "Join our team based in Seattle to build data pipelines")
        s.add_all([fr, us]); s.commit()
        apply_filters(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"}), PROFILE_DEFAULT)
        assert fr.status == "candidate" and fr.france_eligible and "Lille" in fr.location_evidence
        assert us.status == "review" and "location_unverified" in us.review_flags  # not excluded: France may be a 2nd site


def test_workday_france_filter_in_a_location_hierarchy_and_offers_in_several_places():
    from jobfinder.adapters import _workday_france, _workday_place, crawl_workday
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "descriptor": "Locations",
         "values": [{"descriptor": "United States", "id": "US"}, {"descriptor": "France", "id": "FR1"}]},
        {"facetParameter": "locations", "values": [{"descriptor": "France, Courbevoie", "id": "c"}]}]}]
    assert _workday_france(facets) == {"locationHierarchy1": ["FR1"]}  # the country level, not one office
    assert _workday_place("2 sites", "/job/France-Courbevoie/Solutions-Architect_JR1") == "France Courbevoie (+1 other site)"
    assert _workday_place("2 sites", "/job/US-CA-Santa-Clara/Compiler-Engineer_JR2") == "2 sites"  # France may be the other
    posts = [{"title": "Deep Learning Engineer", "externalPath": "/job/US-CA-Santa-Clara/DL_JR3", "locationsText": "2 sites",
              "postedOn": "Posted Today", "bulletFields": ["JR3"]}]
    f = FakeFetcher({'"limit": 1': {"total": 1, "jobPostings": posts, "facets": []},
                     '"offset": 0': {"total": 1, "jobPostings": posts},
                     "/job/US-CA-Santa-Clara/DL_JR3": {"jobPostingInfo": {
                         "startDate": TODAY, "jobDescription": "<p>Deep learning</p>", "location": "US, CA, Santa Clara",
                         "additionalLocations": ["France, Courbevoie"], "country": {"descriptor": "United States"}}}})
    job = list(crawl_workday(f, _workday_arg("https://t.wd5.myworkdayjobs.com/Site"), CFG(), set(), ScanState()))[0].jobs[0]
    from jobfinder.classify import classify_location
    assert "France, Courbevoie" in job["location"] and job["country_hint"] is None
    assert classify_location(job["location"], job["country_hint"])["france_eligible"]


def _ts_rss(items):
    from datetime import datetime, timedelta
    body = "".join(
        f"<item><link>https://www.x.fr/Pages/Offre/detailoffre.aspx?idOffre={i}&amp;idOrigine=502&amp;LCID=1036&amp;"
        f"offerReference=2026-{i}</link><category>Stage</category><category>Saclay</category><title>2026-{i} - Stage - Data "
        f"Scientist {i} H/F</title><description>{'Apprentissage profond. ' * 30}</description><pubDate>"
        f"{(datetime.utcnow() - timedelta(days=d)):%a, %d %b %Y %H:%M:%S} Z</pubDate></item>" for i, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def _ts_page(cards, host="https://www.x.fr", day=None):
    return "<html><body>" + "".join(
        f'<div class="ts-offer-card"><h3><a class="ts-offer-card__title-link" href="{host}/offre-de-emploi/emploi-stage-'
        f'data-{i}_{i}.aspx">Stage - Data Scientist {i} H/F</a></h3><ul><li>Réf. : 2026-{i}</li>'
        + (f"<li>{day}</li>" if day else "") + '<li>Stage</li><li>Essonne (91)</li><li class="noBorder">Saclay</li></ul></div>'
        for i in cards) + "</body></html>"


def test_talentsoft_offers_are_stored_once_and_dated_by_category_feeds_and_list_order():
    from jobfinder.adapters import crawl_talentsoft, _talentsoft_arg
    f = FakeFetcher({
        "Rss_Contract=579": _ts_rss([(90, 3), (80, 40)]),
        "offerRss.ashx?LCID=1036": _ts_rss([(i, 0) for i in range(120, 100, -1)]),
        "tous-les-flux-rss.aspx": '<a href="/handlers/offerRss.ashx?LCID=1036&Rss_Contract=579">Stage</a>',
        "page=2": _ts_page([90, 85, 80, 75]),
        "page=3": "<html></html>",
        "liste-offres.aspx": _ts_page(list(range(120, 100, -1)) + [95], host="https://x-cand.talent-soft.com"),
    })
    cfg = CFG(date_preset="Last 7 days")
    jobs = [j for pg in crawl_talentsoft(f, _talentsoft_arg("https://www.x.fr/offre-de-emploi/liste-offres.aspx?LCID=1036"),
                                         cfg, set(), ScanState()) for j in pg.jobs]
    ids = [j["url"].split("idOffre=")[1].split("&")[0] for j in jobs]
    assert len(ids) == len(set(ids)) == 25  # the feed, the list and its second host: each offer once
    assert all(j["url"] == f"https://www.x.fr/Pages/Offre/detailoffre.aspx?idOffre={i}&LCID=1036" for j, i in zip(jobs, ids))
    by = dict(zip(ids, jobs))
    gate = cfg["_gate"]
    assert by["90"]["date_source"] == "talentsoft feed pubDate"  # dated by its category feed
    assert "list order" in by["95"]["date_source"] and gate(by["95"]["date"], by["95"]["date_conf"]) is True
    assert by["85"]["date"] is None  # between an offer of 3 days and one of 40 days: unknown, kept undated
    assert "list order" in by["75"]["date_source"] and gate(by["75"]["date"], by["75"]["date_conf"]) is False


def test_talentsoft_date_written_on_the_list_card():
    from jobfinder.adapters import crawl_talentsoft, _talentsoft_arg
    from datetime import datetime
    day = datetime.utcnow().strftime("%d/%m/%Y")
    f = FakeFetcher({"offerRss.ashx": _ts_rss([(i, 0) for i in range(120, 100, -1)]),
                     "page=2": "<html></html>",
                     "liste-offres.aspx": _ts_page([130], host="https://x-cand.talent-soft.com", day=day)})
    jobs = [j for pg in crawl_talentsoft(f, _talentsoft_arg("https://www.x.fr/offre-de-emploi/liste-offres.aspx?LCID=1036"),
                                         CFG(), set(), ScanState()) for j in pg.jobs]
    card = next(j for j in jobs if "idOffre=130&" in j["url"])
    assert card["date"].strftime("%d/%m/%Y") == day and card["date_source"] == "talentsoft list date"
    assert card["location"] == "Saclay, Essonne (91)"


def test_abbreviated_and_research_site_places_and_more_text_cues():
    from jobfinder.classify import classify_location, location_from_text as lt
    for loc in ("St-Paul-lez-Durance Langue /", "Chusclan", "Orme des Merisiers", "Ste-Foy-lès-Lyon"):
        assert classify_location(loc)["france_eligible"], loc
    assert not classify_location("Londres")["france_eligible"] and classify_location("Londres")["location_verified"]
    assert lt("Vous cherchez à rejoindre une équipe jeune et dynamique sur Toulouse ? Travailler") == "Toulouse"
    assert lt("Le poste est à pourvoir à Saint-Cloud dès que possible") == "Saint-Cloud"
    assert lt("DTIS-2027-23 Localisation et cartographie Centre Toulouse Fichier joint") == "Toulouse"
    assert lt("Formation sur Python et Docker, à Data Lake") == ""  # cue words before things that are not places


def test_an_offer_titled_by_its_reference_number_takes_its_subject_from_its_page():
    from jobfinder.adapters import _parse_detail, _raw, code_title, merge_detail, worth_opening
    assert code_title("DTIS-2027-23") and code_title("REQ-12345") and code_title("2026-460")
    assert not code_title("STAGE 2027 - DATA") and not code_title("R&D ENGINEER") and not code_title("Stage IA")
    raw = _raw("DTIS-2027-23", "https://w3.onera.fr/stages/fr/node/1222", provenance="rss-feed")
    assert worth_opening(CFG(), raw)  # a reference says nothing: the page is opened
    html = ('<html><head><meta name="description" content="Localisation et cartographie simultanée (SLAM) à partir des '
            'signaux télécom 4G"></head><body><main><h1>DTIS-2027-23</h1><p>Sujet de stage.</p> Centre Toulouse '
            'Fichier joint</main></body></html>')
    job = merge_detail(raw, _parse_detail(html, raw["url"]))
    assert job["title"].startswith("Localisation et cartographie simultanée (SLAM)") and job["location"] == "Toulouse"


def test_the_browser_does_not_load_data_that_robots_txt_closes():
    from jobfinder.web import browser_guard

    class Req:
        def __init__(self, url, kind):
            self.url, self.resource_type = url, kind

    class Route:
        def __init__(self, url, kind):
            self.request, self.done = Req(url, kind), None

        def continue_(self):
            self.done = "continue"

        def abort(self):
            self.done = "abort"

    class F:
        notes = []

        def robots_ok(self, url):
            return "recruitmentplatform.com" not in url

        def note(self, m):
            self.notes.append(m)
    guard = browser_guard(F())
    cases = [("https://emea3.recruitmentplatform.com/fo/rest/jobs", "xhr", "abort"),       # the widget's API: closed
             ("https://emea3.recruitmentplatform.com/static/widget.js", "script", "continue"),  # not data
             ("https://rejoindre.onera.fr/", "document", "continue"),
             ("http://127.0.0.1/admin", "image", "abort")]                                  # private address (SSRF)
    for url, kind, want in cases:
        r = Route(url, kind)
        guard(r)
        assert r.done == want, (url, kind)


def test_websites_on_one_platform_share_its_request_pace():
    import threading
    import time as _t
    from jobfinder import web
    assert web._server("ag.wd3.myworkdayjobs.com") == web._server("sec.wd3.myworkdayjobs.com") == "wd3.myworkdayjobs.com"
    assert web._server("api.smartrecruiters.com") == "api.smartrecruiters.com"
    cfg = {"delay": 0.2}
    a, b = web.Fetcher(cfg), web.Fetcher(cfg)  # two websites read in parallel
    stamps = []
    host = f"pace-test-{_t.time()}.wd9.myworkdayjobs.com"

    def go(fx):
        fx._wait(host)
        stamps.append(_t.time())
    ts = [threading.Thread(target=go, args=(fx,)) for fx in (a, b, a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    assert all(y - x >= 0.18 for x, y in zip(stamps, stamps[1:]))  # one request at a time on the shared servers


def test_a_refused_workday_site_is_reported_as_such_without_trying_its_pages_again():
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError

    class F(SiteFetcher):
        def post_json(self, url, payload):
            self.calls.append(url)
            raise AccessError("access_restricted: HTTP 999 / anti-bot page - inspect manually")

        def get(self, url, **kw):
            self.calls.append(url)
            if "myworkdayjobs.com/wday/" in url:
                raise AccessError("access_restricted: HTTP 999 / anti-bot page - inspect manually")
            raise AssertionError(f"the refused platform's page was opened again: {url}")
    f = F({})
    try:
        list(crawl_source(f, "https://valeo.wd3.myworkdayjobs.com/en-EN/valeo_jobs", CFG(), set(), ScanState()))
        assert False, "must be reported as blocked"
    except AccessError as e:
        assert "Workday refuses automated requests" in str(e)


def test_a_closed_hellowork_search_is_read_on_its_open_keyword_page():
    from jobfinder.crawler import crawl_source, open_alternative
    from jobfinder.web import RobotsRefused

    class F(FakeFetcher):
        def robots_ok(self, url):  # Hellowork's robots.txt: every address with '?' and the search page are closed
            return "?" not in url and "/emploi/recherche" not in url
    search = "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=stage+deep+learning&l=%C3%8Ele-de-France&c=Stage"
    f = F({"mot-cle_deep-learning.html": "<html><h1>405 offres d'emploi Deep learning</h1></html>"})
    assert open_alternative(f, search) == "https://www.hellowork.com/fr-fr/emploi/mot-cle_deep-learning.html"
    assert any("mot-cle_stage-deep-learning" in c for c in f.calls)  # the exact words first (HTTP 404 here), then the keyword
    assert open_alternative(f, "https://www.hellowork.com/fr-fr/emploi/recherche.html") is None  # no words: no page
    assert open_alternative(f, "https://www.optioncarriere.com/emploi?s=AI") is None
    try:  # no open page for these words: blocked, with the open pages to add instead
        list(crawl_source(F({}), "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=zzz", CFG(), set(), ScanState()))
        assert False, "must be refused"
    except RobotsRefused as e:
        assert "mot-cle_deep-learning.html" in str(e) and "company page" in str(e)


def test_one_offer_twice_on_one_website_is_shown_once_but_other_openings_stay():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import merge_duplicates
    from jobfinder.config import CONFIG_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    d1, d2 = datetime.utcnow() - timedelta(days=3), datetime.utcnow() - timedelta(days=1)
    text = "About Mistral. Mistral provides full-stack AI solutions, from frontier models to developer tools. " * 6
    with sessionmaker(bind=engine)() as s:
        board, own = store.Source(url="https://jobs.makesense.org/en/s/jobs/all/all?s=Data", label="Makesense"), \
            store.Source(url="https://careers.thalesgroup.com/fr/fr/search-results", label="Thales")
        s.add_all([board, own]); s.commit()
        mk = lambda i, src, url, d, desc, t="Junior Python Developer (H/F)": store.Job(
            uid=str(i), source_id=src.id, title=t, employer="AXA Climate", city="Paris", url=url, pub_date=d, date_conf=0.9,
            date_source="api", status="candidate", description=desc, extra={})
        fr = mk(1, board, "https://jobs.makesense.org/fr/jobs/axa-climate-junior-python-developer-hf-8ecNN0", d2, "x" * 50)
        en = mk(2, board, "https://jobs.makesense.org/en/jobs/axa-climate-junior-python-developer-hf-8ecNN0", d2, "x" * 50)
        late = mk(3, own, "https://careers.thalesgroup.com/fr/fr/job/R2/ingenieur-ivv", d2, text, "Ingénieur IVV")
        early = mk(4, own, "https://careers.thalesgroup.com/fr/fr/job/R1/ingenieur-ivv", d1, text + " Réf R1", "Ingénieur IVV")
        other = mk(5, own, "https://careers.thalesgroup.com/fr/fr/job/R3/ingenieur-ivv", d2,
                   "Une autre équipe, un autre sujet : radar de surveillance côtière, tests en mer et en bassin. " * 6, "Ingénieur IVV")
        copy = mk(6, board, "https://jobs.makesense.org/fr/jobs/thales-ingenieur-ivv-Zr41", d2, text, "Ingénieur IVV")
        s.add_all([fr, en, late, early, other, copy]); s.commit()
        assert merge_duplicates(s, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"})) == 3
        assert {fr.status, en.status} == {"candidate", "excluded"}  # the same offer in French and in English
        assert early.status == "candidate" and late.status == "excluded" and "published again" in late.reason
        assert copy.status == "excluded" and "on Thales" in copy.reason  # the job board's copy: the company's own is kept
        assert {late.url, copy.url} <= set(early.extra["also_on"])  # the other links stay reachable
        assert other.status == "candidate"  # same title, another text: another opening
        assert other.url not in early.extra["also_on"]  # ...and it is not listed as a copy


def test_france_travail_offers_come_from_its_official_api_with_your_access(monkeypatch):
    from jobfinder import adapters
    from jobfinder.adapters import crawl_francetravail
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError
    from test_adapters import R
    monkeypatch.setattr(adapters, "load_keys", lambda: {})  # never the real api_keys.txt
    for k in ("FRANCE_TRAVAIL_CLIENT_ID", "FRANCE_TRAVAIL_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)

    def offer(i, title, day=TODAY, partner=None):
        return {"id": f"20{i}", "intitule": title, "description": "Développement de modèles de vision par ordinateur. " * 5,
                "dateCreation": f"{day}T08:30:00.000Z", "dateActualisation": f"{TODAY}T09:00:00.000Z",
                "lieuTravail": {"libelle": "91 - Gif-sur-Yvette", "codePostal": "91190"},
                "entreprise": {"nom": "Labo Vision"}, "typeContrat": "CDD", "typeContratLibelle": "CDD - 18 Mois",
                "origineOffre": {"origine": "2" if partner else "1", "partenaires": [{"nom": "APEC", "url": partner}] if partner else []}}

    class F:
        def __init__(self):
            self.calls, self.notes = [], []

        def note(self, m):
            self.notes.append(m)

        def request(self, method, url, **kw):
            self.calls.append((method, url, kw.get("data")))
            return R(url, js={"access_token": "tok", "expires_in": 1499})

        def get(self, url, params=None, headers=None, **kw):
            self.calls.append(("GET", url, dict(params or {})))
            assert headers["Authorization"] == "Bearer tok" and "api.francetravail.io" in url
            if params["motsCles"] == "computer,vision":
                if params["range"] == "0-149":
                    r = R(url, js={"resultats": [offer(i, f"Ingénieur vision {i}") for i in range(149)] +
                                   [offer(999, "Ingénieur vision partenaire", partner="https://www.apec.fr/offre/999")]},
                          headers={"Content-Range": "offres 0-149/151"})
                else:
                    r = R(url, js={"resultats": [offer(1, "Ingénieur vision 1")]}, headers={"Content-Range": "offres 150-150/151"})
                r.status_code = 206 if params["range"] == "0-149" else 200
                return r
            r = R(url, "")
            r.status_code = 204  # nothing for this keyword
            return r
    cfg = CFG(date_preset="Last 7 days")
    try:  # no access yet: says how to get it, sends nothing
        list(crawl_francetravail(F(), "https://www.francetravail.fr/accueil", cfg, set(), ScanState()))
        assert False, "must say that an access is needed"
    except AccessError as e:
        assert "francetravail.io" in str(e) and "FRANCE_TRAVAIL_CLIENT_ID" in str(e)
    monkeypatch.setattr(adapters, "load_keys", lambda: {"francetravail_id": "id", "francetravail_secret": "secret"})
    f = F()
    url = "https://candidat.francetravail.fr/offres/recherche?motsCles=computer+vision,deep+learning&offresPartenaires=true"
    jobs = [j for pg in crawl_source(f, url, cfg, set(), ScanState()) for j in pg.jobs]
    token = f.calls[0]
    assert token[0] == "POST" and "realm=%2Fpartenaire" in token[1] and token[2]["scope"] == "api_offresdemploiv2 o2dsoffre"
    searches = [c[2] for c in f.calls if c[0] == "GET"]
    assert {s["motsCles"] for s in searches} == {"computer,vision", "deep,learning"}
    assert all(s["publieeDepuis"] == 7 and s["sort"] == 1 for s in searches)
    assert [s["range"] for s in searches if s["motsCles"] == "computer,vision"] == ["0-149", "150-299"]
    assert len(jobs) == 150 and len({j["platform_id"] for j in jobs}) == 150  # offer '201' of page 2 was already read
    j = next(x for x in jobs if x["platform_id"] == "20999")
    assert j["url"] == "https://candidat.francetravail.fr/offres/recherche/detail/20999"
    assert j["apply_url"] == "https://www.apec.fr/offre/999" and j["employer"] == "Labo Vision"
    assert j["date"].strftime("%Y-%m-%d") == TODAY and "dateCreation" in j["date_source"] and j["country_hint"] == "FR"
    assert not any("www.francetravail.fr" in str(c[1]) for c in f.calls)  # its pages are never opened
    from jobfinder.adapters import _ft_since
    assert (_ft_since(CFG(date_preset="Last 30 days")), _ft_since(CFG(date_preset="All dates"))) == (31, None)


def test_boards_without_any_open_access_are_reported_without_a_request():
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError
    for url, word in (("https://www.apec.fr/candidat/recherche-emploi.html/emploi?lieux=75", "Apec"),
                      ("https://fr.indeed.com/jobs?q=deep+learning&l=France", "Indeed")):
        f = SiteFetcher({})
        try:
            list(crawl_source(f, url, CFG(), set(), ScanState()))
            assert False, "must be reported"
        except AccessError as e:
            assert word in str(e) and "career page" in str(e) and not f.calls


def test_places_written_in_more_forms_in_an_offer_text():
    from jobfinder.classify import classify_location, location_from_text
    cases = [  # (text, title, expected place) - all seen in a full run
        ("offres d’emploi / Systèmes embarqués Architecte Cloud Azure H/F CDI / FRANCE Occitanie / Montpellier Postuler", "", "Montpellier"),
        ("Lieu : Stage M2 de 6 mois Equipe ICARE Campus Pasteur 28 avenue Valombrose 06107 Nice Hôpital Pasteur", "", "Nice"),
        ("Lieu du stage : • LURPA ENS Paris-Saclay, 4 avenue des sciences, 91190 Gif-sur-Yvette", "", "ENS Paris-Saclay"),
        ("Ville : Centre Inria de l'Université Grenoble Alpes ou Siège (Grenoble ou Paris) Centre Inria : Siège", "", "Grenoble"),
        ("Retour Technology SOFTWARE CRAFTS(WO)MAN Paris Il y a 7 jours Postuler", "SOFTWARE CRAFTS(WO)MAN", "Paris"),
        ("Job ID 528498 Company Siemens SAS ... location in France Dans un monde industriel qui bouge vite", "", "France"),
    ]
    for text, title, want in cases:
        got = location_from_text(text, title)
        assert want.lower() in got.lower() and classify_location(got)["france_eligible"], (text[:40], got)
    # a sentence is not a label, a search form's prompt is not a place
    assert location_from_text("Representation of urban mobility location: Evidence from Kunming, China. ISPRS Journal") == ""
    assert location_from_text("Localisation : Veuillez sélectionner une valeur Amérique du Nord Europe Asie") == ""
    assert location_from_text("Liens rapides Accueil Offres d'emploi", "Formateur à Saint Avertin (Tours) : atelier") == "Saint-Avertin"
    assert not classify_location("Erlangen")["france_eligible"] and classify_location("Erlangen")["location_verified"]
    loc = classify_location("11 boulevard d’Alembert, 78280")  # a French street address with its postcode
    assert loc["location_verified"] and loc["france_eligible"]
    for field in ("INRAE", "CNRS", "École nationale des ponts et chaussées", "Roissy Charles de Gaulle"):
        assert classify_location(field)["france_eligible"], field
    assert not classify_location("Inria Chile, Santiago")["france_eligible"]


def test_a_commune_written_without_the_end_of_its_name():
    from jobfinder.classify import classify_location, fr_place
    assert fr_place("Montbonnot") == "Montbonnot-Saint-Martin" and fr_place("Le Chesnay") == "Le Chesnay-Rocquencourt"
    assert fr_place("CHERBOURG") == "Cherbourg-en-Cotentin"
    assert fr_place("BONNEVILLE") == "Bonneville"  # several communes start so: French, without inventing which one
    for foreign in ("Vienne", "Bellevue", "Sainte"):  # Vienna in French, Bellevue (Washington), a word
        assert not classify_location(foreign)["france_eligible"], foreign


def test_a_date_in_the_location_field_and_pages_that_are_not_offers():
    from jobfinder.classify import clean_title, is_boilerplate_title, looks_like_date, offer_kinds_heading
    assert looks_like_date("Jeudi, 5 novembre, 2026 - Vendredi, 6 novembre, 2026") and looks_like_date("12/11/2026")
    assert not looks_like_date("Saint-Martin-d'Hères") and not looks_like_date("Marseille 13e")
    for t in ("Postdoc, PhD or Master 2", "Apprenticeships, internships, thesis and VIE",
              "Open positions for 2026 internships/PhDs.", "Offres de stages et thèses"):
        assert offer_kinds_heading(t), t
    for t in ("PhD position", "Stage M2", "Stage - Data Scientist", "Internship - Machine Learning Research", "Thèse CIFRE IA"):
        assert not offer_kinds_heading(t), t
    assert is_boilerplate_title("<<INTERNAL JOB DESCRIPTION>> Follow the rules and procedures")
    assert clean_title("Video Editor (F/H/NB) [AAA Project]") == "Video Editor [AAA Project]"
    assert clean_title("Cloud administrator (W/M/NB)") == "Cloud administrator"


def test_page_data_mixing_other_content_with_offers():
    from jobfinder.crawler import _drop_odd_links
    base = "https://www.ubisoft.com/fr-fr/company/careers"
    jobs = [{"url": f"{base}/search/74400016{i}-video-editor-f-h", "location": "", "employment_type": ""} for i in range(6)]
    odd = {"url": f"{base}/anti-cheat-software", "location": "", "employment_type": ""}
    assert _drop_odd_links(jobs + [odd]) == jobs
    pages = [{"url": f"{base}/{x}", "location": "", "employment_type": ""} for x in ("working-at-ubisoft", "locations", "news-1")]
    assert _drop_odd_links(jobs + pages + [odd]) == jobs  # site pages around the offers in the same page data
    assert _drop_odd_links(jobs[:3] + pages) == jobs[:3] + pages  # too few numbered offers to tell: nothing dropped
    kept = {"url": f"{base}/stage-data", "location": "Paris", "employment_type": ""}  # a place: may be an offer, kept
    assert kept in _drop_odd_links(jobs + [kept])


def test_two_offers_on_one_platform_page_address_are_not_one_offer():
    from jobfinder.scan import _lang_free
    a = "https://www.emploi.cea.fr/Pages/Offre/detailoffre.aspx?idOffre=39221&LCID=1036"
    assert _lang_free(a) != _lang_free(a.replace("39221", "38837"))  # two CEA offers
    assert _lang_free(a) == _lang_free(a.replace("LCID=1036", "LCID=2057"))  # one offer in two languages


def test_keywords_added_to_the_defaults_reach_a_saved_profile_once():
    from jobfinder.config import PROFILE_DEFAULT, upgrade_profile
    old = {"description": "x", "keywords": ["data scientist", "computer vision", "consultant ia"], "exclude_titles": [],
           "offer_types": ["job"]}  # saved before the keyword revisions
    up, changed = upgrade_profile(old)
    assert changed and up["keywords_rev"] == PROFILE_DEFAULT["keywords_rev"]
    assert {"mlops", "lidar", "ingenieur perception", "consultant ia"} <= set(up["keywords"])  # added; own word kept
    assert up["keywords"][:3] == old["keywords"]  # the user's order first
    trimmed = {**up, "keywords": [k for k in up["keywords"] if k != "lidar"]}
    assert upgrade_profile(trimmed) == (trimmed, False)  # a keyword deleted afterwards is not added back
    assert len(PROFILE_DEFAULT["keywords"]) == len(set(PROFILE_DEFAULT["keywords"]))


def test_welcome_to_the_jungle_pages_are_read_on_the_company_career_site_or_reported(monkeypatch):
    from jobfinder import crawler
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))  # no real browser in tests

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    job = ('<html><script type="application/ld+json">{"@type": "JobPosting", "title": "Ingénieur Vision par Ordinateur (H/F)",'
           f' "datePosted": "{TODAY}", "jobLocation": {{"address": {{"addressLocality": "Paris", "addressCountry": "FR"}}}},'
           ' "description": "Vision par ordinateur et apprentissage profond pour l\'audio et l\'image."}</script></html>')
    site = ('<html><ul class="jobs-list"><li><a class="jobs-list-item-link" href="/jobs/ingenieur-vision_paris">'
            '<h3>Ingénieur Vision par Ordinateur (H/F)</h3></a></li></ul></html>')
    f = F({"devialet.welcomekit.co/jobs/ingenieur-vision_paris": job, "devialet.welcomekit.co/": site,
           "dxo.welcomekit.co": "No career website for dxo.welcomekit.co."})
    jobs = [j for pg in crawl_source(f, "https://www.welcometothejungle.com/fr/companies/devialet/jobs", CFG(), set(), ScanState())
            for j in pg.jobs]
    assert [j["title"] for j in jobs] == ["Ingénieur Vision par Ordinateur (H/F)"]  # read on the company's own career site
    assert any("devialet.welcomekit.co" in n for n in f.notes)
    for url in ("https://www.welcometothejungle.com/en/companies/dxo/jobs",  # no career site of its own
                "https://www.welcometothejungle.com/fr/jobs?query=stage+deep+learning"):  # the board's search
        g = F({"dxo.welcomekit.co": "No career website for dxo.welcomekit.co."})
        try:
            list(crawl_source(g, url, CFG(), set(), ScanState()))
            assert False, "must be reported"
        except AccessError as e:
            assert "Welcome to the Jungle" in str(e) and "jobs.stationf.co" in str(e)
        assert not any(x in c for c in g.calls for x in ("teamtailor", "greenhouse", "smartrecruiters", "workable", "lever"))


def test_no_job_board_is_guessed_from_the_name_of_a_platform_that_hosts_the_page(monkeypatch):
    from jobfinder import crawler
    from jobfinder.crawler import _board_host, crawl_source
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    page = ('<html><ul class="jobs-list"><li><a class="jobs-list-item-link" href="/companies/rainpath/jobs/operations-intern_paris">'
            '<h3>Operations Intern</h3></a></li></ul></html>')  # only an offer the profile does not want
    f = F({"jobs.stationf.co/companies/rainpath": page, "teamtailor.com/jobs.rss": "<rss><title>STATION F</title><item/></rss>"})
    assert [j for pg in crawl_source(f, "https://jobs.stationf.co/companies/rainpath", CFG(), set(), ScanState()) for j in pg.jobs] == []
    assert not any(x in c for c in f.calls for x in ("teamtailor", "greenhouse", "smartrecruiters", "workable", "recruitee"))
    assert _board_host("https://devialet.welcomekit.co/") and _board_host("https://jobs.stationf.co/companies/x")
    assert _board_host("https://www.welcometothejungle.com/fr/companies/dxo/jobs") and not _board_host("https://www.parrot.com/en/jobs")


def test_offer_data_with_unescaped_quotes_and_company_pages_on_shared_sites():
    from jobfinder.adapters import _parse_detail
    from jobfinder.store import company_from_url
    # Welcome to the Jungle (STATION F job board) leaves the quotes of the offer's text unescaped in its JSON-LD
    page = ("<html><script type='application/ld+json'>{\"@context\": \"https://schema.org\", \"@type\": \"JobPosting\",\n"
            f"\"title\": \"Data Scientist Engineer\", \"datePosted\": \"{TODAY}T01:03:28+01:00\",\n"
            "\"description\": \"You are a proactive \"doer\": autonomous, \"hands-on\" and curious.\nAnd more: "
            "[blog\\](file:///C:/Users/MARIEB~1/clip\\_filelist.xml)\",\n"  # a pasted Windows path: invalid escapes
            "\"hiringOrganization\": {\"@type\": \"Organization\", \"name\": \"Sekoia\"},\n"
            "\"jobLocation\": [{\"@type\": \"Place\", \"address\": {\"addressLocality\": \"Rennes\", \"addressCountry\": \"France\"}}]}"
            "</script><h1>Data Scientist Engineer</h1><p>Paris, Rennes Possible partial remote Apply Now</p></html>")
    job = _parse_detail(page, "https://jobs.stationf.co/companies/sekoia/jobs/data-scientist-engineer")
    assert job["provenance"] == "json-ld" and job["employer"] == "Sekoia" and job["location"] == "Rennes, France"
    assert str(job["date"])[:10] == TODAY and '"doer"' in job["description"]
    assert company_from_url("https://jobs.stationf.co/companies/neurobus") == "Neurobus"
    assert company_from_url("https://jobs.stationf.co/companies/joko-1") == "Joko"
    assert company_from_url("https://www.welcometothejungle.com/en/companies/dxo/jobs") == "DXO"
    assert company_from_url("https://www.hellowork.com/fr-fr/entreprises/-proxiad--86909.html") == "Proxiad"
    assert company_from_url("https://jobs.ashbyhq.com/arago") == "Arago"  # unchanged


def test_nvidia_models_answer_without_thinking(monkeypatch):
    from jobfinder import llm
    sent = {}

    def post(url, headers, payload, **kw):
        sent.update(url=url, payload=payload, auth=headers.get("Authorization"))
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    monkeypatch.setattr(llm, "_post", post)
    cfg = {"providers": {"nvidia": {"base_url": "https://integrate.api.nvidia.com/v1"}}, "auto_models": False}
    assert llm.chat("nvidia", "nvidia/nemotron-3-ultra-550b-a55b", "s", "u", cfg, {"nvidia": "nvapi-x"})[0] == '{"ok": true}'
    assert sent["url"] == "https://integrate.api.nvidia.com/v1/chat/completions" and sent["auth"] == "Bearer nvapi-x"
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}  # else the answer length goes to thinking
    chain = llm.model_chain({"auto_models": True, "model": {"provider": "ollama", "model": "x"}}, {"nvidia": "k"})
    assert chain[0] == ("nvidia", "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia")


def test_names_of_websites_hosted_on_job_platforms():
    from jobfinder.store import company_from_url
    assert company_from_url("https://job-boards.greenhouse.io/dataiku") == "Dataiku"  # not 'Boards'
    assert company_from_url("https://donecle.taleez.com/") == "Donecle"  # not 'Taleez'
    assert company_from_url("https://xxii-group.welcomekit.co/") == "Xxii"  # not 'Group'
    assert company_from_url("https://careers-fr-soitec.icims.com/jobs/search") == "Soitec"


def test_jobspipe_reads_offers_in_france_without_indeed_or_linkedin(monkeypatch):
    from jobfinder import adapters
    from jobfinder.adapters import crawl_jobspipe
    from jobfinder.crawler import crawl_source
    from jobfinder.web import AccessError
    from test_adapters import R
    monkeypatch.setattr(adapters, "load_keys", lambda: {})  # never the real api_keys.txt
    monkeypatch.delenv("JOBSPIPE_API_KEY", raising=False)

    def offer(i, title):
        return {"id": f"j{i}", "job_title": title, "company": "Modjo", "location": "Paris", "country_code": "FR",
                "date_posted": f"{TODAY}T15:41:12+00:00", "url": f"https://modjo.teamtailor.com/jobs/{i}-ml",
                "description": "Machine learning for speech. " * 20, "employment_statuses": ["full_time"],
                "sources": [{"provider": "teamtailor"}]}

    class F:
        def __init__(self, pages, fail=None):
            self.pages, self.fail, self.bodies, self.notes = list(pages), fail, [], []

        def note(self, m):
            self.notes.append(m)

        def request(self, method, url, _robots=False, json=None, headers=None):
            assert method == "POST" and url == "https://api.jobspipe.dev/v1/jobs/search" and _robots is True
            assert headers["Authorization"] == "Bearer jp_test"
            self.bodies.append(dict(json))
            if self.fail:
                raise self.fail
            return R(url, js=self.pages.pop(0))

    cfg = {**CFG(), "_keys": {"jobspipe": "jp_test"}}
    f = F([{"data": [offer(1, "Machine Learning Engineer"), offer(2, "Speech Scientist")], "metadata": {"next_cursor": "c2"}},
           {"data": [offer(3, "Computer Vision Engineer")], "metadata": {"next_cursor": None, "credits_remaining": 994}}])
    jobs = [j for pg in crawl_jobspipe(f, "https://jobspipe.dev/", cfg, set(), ScanState()) for j in pg.jobs]
    assert [j["title"] for j in jobs] == ["Machine Learning Engineer", "Speech Scientist", "Computer Vision Engineer"]
    body = f.bodies[0]
    assert body["job_country_code_or"] == ["FR"] and body["source_not"] == ["indeed", "linkedin"]
    assert body["posted_at_max_age_days"] == 30 and "machine learning" in body["job_title_or"] and "ia" not in body["job_title_or"]
    assert f.bodies[1]["cursor"] == "c2"  # the next page
    j = jobs[0]
    assert j["url"] == "https://modjo.teamtailor.com/jobs/1-ml" and j["employer"] == "Modjo" and j["country_hint"] == "FR"
    assert str(j["date"])[:10] == TODAY and "credits left: 994" in f.notes[-1]
    import pytest
    with pytest.raises(AccessError, match="JOBSPIPE_API_KEY"):  # no key: reported, nothing sent
        list(crawl_jobspipe(F([]), "https://jobspipe.dev/", CFG(), set(), ScanState()))
    with pytest.raises(AccessError, match="credits used up"):
        list(crawl_jobspipe(F([], ConnectionError("HTTP 402")), "https://jobspipe.dev/", cfg, set(), ScanState()))
    with pytest.raises(AccessError, match="refused the API key"):
        list(crawl_jobspipe(F([], AccessError("access_restricted: HTTP 401")), "https://jobspipe.dev/", cfg, set(), ScanState()))
    st = ScanState()  # a jobspipe.dev address in the website list is read through the API
    f2 = F([{"data": [offer(4, "ML Engineer")], "metadata": {}}])
    assert [j["title"] for pg in crawl_source(f2, "https://jobspipe.dev/", cfg, set(), st) for j in pg.jobs] == ["ML Engineer"]
    assert st.platform.startswith("jobspipe")


def test_personio_boards_are_read_from_their_public_feed():
    from jobfinder.adapters import crawl_personio, detect_from_url
    feed = (f"<workzag-jobs><position><id>193006</id><subcompany>Free2Move</subcompany><office>Paris</office>"
            f"<additionalOffices><office>Lyon</office></additionalOffices><name>Data Scientist - Pricing</name>"
            f"<jobDescriptions><jobDescription><name>Your mission</name><value><![CDATA[<p>Build ML pricing models.</p>]]></value>"
            f"</jobDescription></jobDescriptions><employmentType>permanent</employmentType><schedule>full-time</schedule>"
            f"<createdAt>{TODAY}T07:50:24+00:00</createdAt></position>"
            f"<position><id></id><name>broken</name></position></workzag-jobs>")
    f = FakeFetcher({"free2move.jobs.personio.de/xml": feed})
    jobs = [j for pg in crawl_personio(f, "https://free2move.jobs.personio.de", CFG(), set(), ScanState()) for j in pg.jobs]
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Data Scientist - Pricing" and j["url"] == "https://free2move.jobs.personio.de/job/193006"
    assert j["location"] == "Paris ; Lyon" and "ML pricing models" in j["description"] and str(j["date"])[:10] == TODAY
    assert detect_from_url("https://free2move.jobs.personio.de/job/193006?language=fr") == ("personio", "https://free2move.jobs.personio.de")


def test_ashby_or_lever_board_found_by_name_only_with_offers_in_france():
    from jobfinder.adapters import find_board
    from test_adapters import R

    class F(FakeFetcher):
        def get(self, url, **kw):
            if "api.ashbyhq.com" in url or "api.lever.co" in url:
                self.calls.append(url)
                return R(url, js=self.routes.get(url.split("/")[-1].split("?")[0], {"jobs": []} if "ashby" in url else []))
            raise ConnectionError("HTTP 404")
    us = F({"latitude": {"jobs": [{"title": "Accounts Payable", "location": "Pittsburgh, PA"}]}})
    assert find_board(us, "https://www.latitude.eu/careers/") is None  # a US company with the same name
    fr = F({"elevenlabs": {"jobs": [{"title": "Research Engineer", "location": "Remote",
                                     "secondaryLocations": [{"location": "Paris, France"}]}]}})
    assert find_board(fr, "https://elevenlabs.io/careers") == ("ashby", "elevenlabs", "elevenlabs")
    lev = F({"doctrine": [{"text": "ML Engineer", "categories": {"location": "Paris"}}]})
    assert find_board(lev, "https://www.doctrine.fr/") == ("lever", "doctrine", "doctrine")


def test_board_names_without_their_platform_numbers():
    from jobfinder.store import company_from_url
    assert company_from_url("https://planity-1651249729.teamtailor.com/jobs") == "Planity"
    assert company_from_url("https://apply.workable.com/corwave-1/") == "Corwave"


def test_quota_record_per_key_and_model():
    from jobfinder.llm import update_quota
    step = ("groq", "openai/gpt-oss-120b", "groq#2")
    q = {}
    update_quota(q, step, "2026-11-09", headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "998"})
    update_quota(q, step, "2026-11-09")
    e = q["groq#2|openai/gpt-oss-120b"]
    assert e["calls"] == 2 and e["month_calls"] == 2 and (e["limit"], e["remaining"]) == (1000, 998)
    update_quota(q, step, "2026-11-09", until=1000 + 20, now=1000)  # a per-minute limit: not a daily one
    assert e["until"] == 1020 and "learned" not in e
    update_quota(q, step, "2026-11-09", until=1e12)  # the daily limit is reached after 2 calls
    assert e["until"] == 1e12 and e["learned"] == 2
    update_quota(q, step, "2026-11-09", error="Authentication failed")
    assert "Authentication" in e["error"]
    update_quota(q, step, "2026-11-10")  # a new day: the count starts again, the month goes on, the error is gone
    assert e["calls"] == 1 and e["month_calls"] == 3 and "error" not in e and e["learned"] == 2


def test_a_third_groq_key_comes_right_after_the_second():
    from jobfinder import llm
    chain = llm.model_chain({"auto_models": True, "model": {"provider": "ollama", "model": "x"}}, {"groq": "a", "groq#2": "b", "groq#3": "c"})
    assert chain[:3] == [("groq", "openai/gpt-oss-120b", "groq"), ("groq", "openai/gpt-oss-120b", "groq#2"),
                         ("groq", "openai/gpt-oss-120b", "groq#3")]


def test_a_second_openrouter_key_is_used_after_the_first():
    from jobfinder import llm
    chain = llm.model_chain({"auto_models": True, "model": {"provider": "ollama", "model": "x"}}, {"openrouter": "a", "openrouter#2": "b"})
    assert chain[1] == (chain[0][0], chain[0][1], "openrouter#2") and chain[0][2] == "openrouter"


def test_market_statistics_from_the_analysed_offers_every_15_days():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store, market
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    now = datetime(2026, 11, 9, 12)
    with sessionmaker(bind=engine)() as s:
        src = store.Source(url="https://jobs.lever.co/doctrine", label="Doctrine"); s.add(src)
        s.add(store.CVDocument(name="cv.pdf", text="Data scientist: Python, SQL, statistics.", status="ok", enabled=True)); s.commit()
        ai = lambda skills, **k: {"skills": skills, "degree": "master", "seniority": "junior", "languages": ["anglais", "French"],
                                  "remote": "hybrid", "salary_text": "45-55 k€", "contract_kind": "job", **k}
        mk = lambda i, status, score, rel, cvm=None: store.Job(
            uid=f"u{i}", source_id=src.id, title=f"ML Engineer {i} (H/F)", employer="Doctrine", city="Paris", status=status,
            description="You will build models in Python with PyTorch, write C++ services and connect Salesforce.",
            relevance_score=score, relevance_json=rel, pub_date=now - timedelta(days=2), best_cv="cv.pdf" if cvm else None,
            match_score=70 if cvm else None, match_json={"cv.pdf": cvm} if cvm else None)
        s.add_all([mk(1, "relevant", 90, ai(["pytorch", "Python", "PyTorch"]), {"missing_requirements": ["Kubernetes"], "not_evidenced": ["ROS"]}),
                   mk(2, "relevant", 85, ai(["PyTorch", "C++"])),
                   mk(3, "excluded", 20, ai(["Salesforce"])),
                   mk(4, "out_of_range", 80, ai(["PyTorch"]))])  # outside the range: never in the statistics
        s.commit()
        assert market.archive_offers(s, now) == 3
        snap = market.compute_snapshot(s, now)
        d = snap.data
        assert d["relevant"] == 2 and d["analysed"] == 3 and d["base"] == "relevant"
        assert d["top_skills"][0] == ["PyTorch", 2] and ["Python", 1] in d["top_skills"]  # once per offer, one spelling
        assert ["Salesforce", 1] not in d["top_skills"]  # only the relevant offers describe the market of the field
        # skills the offers name that the CV does not mention (the CV knows Python): PyTorch twice, C++ once
        assert d["skill_gaps"] == [["PyTorch", 2], ["C++", 1]] and d["avg_match"] == 70.0
        assert d["languages"][0][0] in ("English", "French") and d["contracts"] == [["job", 2]]
        assert d["salaries"][0][2] == "45-55 k€" and d["top_companies"] == [["Doctrine", 2]]
        # the history outlives the session's offers; the next snapshot comes 15 days later
        s.query(store.Job).delete(); s.commit()
        assert s.query(store.MarketOffer).count() == 3
        assert not market.due(s, now + timedelta(days=14)) and market.due(s, now + timedelta(days=15))


def test_company_names_and_cities_of_the_statistics():
    from jobfinder.market import company_name
    assert company_name("mistral.ai") == "Mistral AI" and company_name("GE HEALTHCARE") == "GE HealthCare"
    assert company_name("Airliquidehr") == "Air Liquide" and company_name("EasyMile") == "EasyMile" and company_name("IBM") == "IBM"


def test_one_unusable_json_answer_does_not_set_the_model_aside(monkeypatch):
    import pytest
    from jobfinder import llm as L

    class R:
        status_code, headers = 400, {}
        text = '{"error":{"message":"Failed to generate JSON. Please adjust your prompt. See \'failed_generation\'","type":"invalid_request_error","code":"json_validate_failed"}}'
    monkeypatch.setattr(L.requests, "post", lambda *a, **k: R())
    with pytest.raises(L.MalformedOutput):  # not a refused key
        L._post("https://api.groq.com/openai/v1/chat/completions", {}, {"max_tokens": 10})
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"): L.MalformedOutput("bad json")})
    assert llm.call_json("u", A, cfg, s, keys).ok  # the next model answered...
    assert ("gemini", "gemini-3.6-flash", "gemini") not in llm._EXHAUSTED  # ...and the first one stays available


def test_statistics_on_request_at_the_end_of_a_scan():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store, market
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    now = datetime(2026, 11, 9, 12)
    with sessionmaker(bind=engine)() as s:
        src = store.Source(url="https://jobs.lever.co/x", label="X"); s.add(src); s.commit()
        s.add(store.Job(uid="a", source_id=src.id, title="ML Engineer", employer="X", status="relevant", relevance_score=90,
                        relevance_json={"skills": ["Python"]})); s.commit()
        assert market.run_if_due(s, now) is not None  # the first snapshot
        s.add(store.Job(uid="b", source_id=src.id, title="CV Engineer", employer="X", status="relevant", relevance_score=88,
                        relevance_json={"skills": ["OpenCV"]})); s.commit()
        assert market.run_if_due(s, now + timedelta(days=2)) is None  # not due: the 15-day schedule waits
        snap = market.run_if_due(s, now + timedelta(days=2), force=True)  # 'Update the statistics' ticked for this scan
        assert snap is not None and snap.data["relevant"] == 2


def test_a_model_that_times_out_is_left_for_ten_minutes(monkeypatch):
    import time
    from jobfinder import llm as L
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.6-flash", "g1"):
                                                        L.AIError("Provider unreachable (ReadTimeout). Is the service running / reachable?")})
    assert llm.call_json("u", A, cfg, s, keys).ok  # the next model answered
    assert llm._EXHAUSTED[("gemini", "gemini-3.6-flash", "gemini")] > time.time() + 500


def test_with_automatic_switching_a_slow_model_is_not_waited_for(monkeypatch):
    import pytest
    from jobfinder import llm as L
    tries = []

    def slow(*a, **k):
        tries.append(k.get("timeout"))
        raise L.requests.Timeout("read timed out")
    monkeypatch.setattr(L.requests, "post", slow)
    cfg = {"auto_models": True, "providers": {"groq": {"base_url": "https://api.groq.com/openai/v1"}}}
    with pytest.raises(L.AIError, match="unreachable"):
        L.chat("groq", "openai/gpt-oss-120b", "s", "u", cfg, {"groq": "k"})
    assert tries == [60]  # one try of 60 s, not two of 90 s


def test_only_the_skills_an_offer_really_names_are_counted():
    from jobfinder.market import grounded
    text = ("Train time-series foundation models on clusters with thousands of GPUs, fine-tune LLMs, and follow the latest "
            "advancements in natural language processing. Optimisation des modèles, robotique, Vision Transformers.")
    sk = ["GPU", "LLM", "time-series modeling", "NLP", "optimization", "robotics", "Python", "PyTorch", "Git", "computer vision"]
    assert grounded(sk, text) == ["GPU", "LLM", "time-series modeling", "NLP", "optimization", "robotics"]


def test_cities_and_salaries_of_the_statistics():
    from jobfinder.market import _cities
    assert _cities("Paris & Aubervilliers") == ["Paris", "Aubervilliers"] and _cities("France") == [] and _cities("Lyon; Remote") == ["Lyon"]


def test_each_input_file_shows_how_many_of_its_websites_were_searched():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        store.add_sources(s, ["https://a.fr/jobs", "https://b.fr/careers", "https://c.fr/jobs"],
                          {"https://a.fr/jobs": ["one.html"], "https://b.fr/careers": ["one.html", "two.html"],
                           "https://c.fr/jobs": ["two.html"]})
        store.add_sources(s, ["https://a.fr/jobs?page=2"], {"https://a.fr/jobs?page=2": ["two.html"]})  # same website
        a, b, c = (s.query(store.Source).filter(store.Source.url.like(f"https://{x}.fr%")).one() for x in "abc")
        assert a.origin == "one.html\ntwo.html" and b.origin == "one.html\ntwo.html"
        a.status, b.status = "completed", "blocked"
        s.commit()
        one, two = store.file_coverage(s)
        assert (one["file"], one["total"], one["read"], one["failed"], one["waiting"]) == ("one.html", 2, 1, 1, 0)
        assert (two["total"], two["read"], two["failed"], two["waiting"]) == (3, 1, 1, 1)
        assert sorted(two["missed"]) == sorted([b.label, c.label])


def _site(pages):
    """A fake website: {url ending: html}; any other address is an offer page."""
    body = "<h1>Offre</h1><main>" + "<p>Missions du poste, profil, compétences, candidature, contrat CDI, télétravail.</p>" * 12 + "</main>"

    class F(FakeFetcher):
        def get(self, url, **kw):
            text = next((h for k, h in pages.items() if url.endswith(k)), body)
            return type("R", (), {"text": text, "url": url, "status_code": 200})()

        def robots_ok(self, url):
            return True

        def note(self, m):
            pass
    return F({})


def _read(f, url):
    from jobfinder.crawler import crawl_generic
    cfg = {**CFG(), "_profile": {**CFG()["_profile"], "offer_types": ["job", "internship", "phd_postdoc", "apprenticeship"]}}
    return [j for pg in crawl_generic(f, url, cfg, set(), ScanState()) for j in pg.jobs]


def test_offer_links_with_a_prefixed_job_id_and_a_read_more_button():
    # the card's fields are headings too ('Date', 'URL'): the offer's title is the heading that reads like one
    cards = "".join(f'<div class="eolia-job"><div class="eolia-job-param"><h3>Date</h3><p>09/11/2026</p></div>'
                    f'<div class="eolia-job-param"><h3>Localisation</h3><p>Paris</p></div><h2>Ingénieur data {k} (H/F)</h2>'
                    f'<div class="eolia-job-param eolia-job-url"><h3>URL</h3>'
                    f'<a href="/carrieres-offre/?ingeniance_job_id={k}&ingeniance_job_ref=ING-{k}&"><p>En savoir plus</p></a></div></div>'
                    for k in (1076, 1225, 1239))
    jobs = _read(_site({"/carrieres/": cards}), "https://ingeniance.fr/carrieres/")
    assert sorted(j["title"] for j in jobs) == ["Ingénieur data 1076 (H/F)", "Ingénieur data 1225 (H/F)", "Ingénieur data 1239 (H/F)"]


def test_offer_links_win_over_page_data_that_holds_other_items():
    import json
    data = {"props": {"pageProps": {"items": [{"title": t, "slug": s, "url": f"https://x-group.com/{s}", "id": i,
                                               "jobTitle": t, "datePosted": "2026-11-01"}
                                              for i, (t, s) in enumerate([("Quel Extien es-tu ?", "extien"), ("Moving Motivators", "mm")])]}}}
    links = "".join(f'<a href="/join-us/ingenieur-devops-{k}-h-f">Ingénieur DevOps {k} (H/F) CDI Lille</a>' for k in range(6))
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>{links}'
    jobs = _read(_site({"/join-us": page}), "https://x-group.com/join-us")
    titles = {j["title"] for j in jobs}
    assert "Quel Extien es-tu ?" not in titles and len([t for t in titles if t.startswith("Ingénieur DevOps")]) == 6


def test_the_full_list_is_read_when_the_page_shows_a_preview():
    short = '<a href="/nos-offres/a-h-f">Ingénieur A (H/F)</a><a href="/nos-offres?expertise=all&search=all">Voir toutes nos offres</a>'
    full = "".join(f'<a href="/nos-offres/poste-{k}-h-f">Ingénieur logiciel {k} (H/F)</a>' for k in range(8))
    jobs = _read(_site({"/nos-offres?expertise=all&search=all": full, "/nos-offres": short}), "https://davidson.fr/nos-offres")
    assert len(jobs) == 8 and all(j["title"].startswith("Ingénieur logiciel") for j in jobs)


def test_a_sentence_after_a_place_label_is_not_a_place():
    from jobfinder.classify import location_from_text
    text = "Nous croyons en un lieu de travail où équité F/H, inclusion et diversité sont la norme.\nLieu de travail : Metz"
    assert location_from_text(text) == "Metz"
    assert location_from_text("Nous croyons en un lieu de travail où équité F/H, inclusion et diversité sont la norme.") == ""


def test_company_names_from_platform_subdomains():
    from jobfinder.store import company_from_url as c
    assert c("https://eleven-labs.welcomekit.co/") == "Eleven Labs" and c("https://coexya.cvcatcher.io/") == "Coexya"
    assert c("https://akka-cand.talent-soft.com/job/list-of-jobs.aspx") == "Akkodis"
    assert c("https://careers-fr-soitec.icims.com/") == "Soitec" and c("https://planity-1651249729.teamtailor.com/jobs") == "Planity"


def _dated_offer(i, recent):
    return (f'<html><head><script type="application/ld+json">{{"@type": "JobPosting", "title": "Data Scientist {i}",'
            f'"datePosted": "{TODAY if i < recent else OLD}", "description": "missions profil candidature"}}</script></head></html>')


def _undated_list(n):
    return "<ul>" + "".join(f'<li><a href="/jobs/data-scientist-machine-learning-{i}">Data Scientist {i}</a></li>' for i in range(n)) + "</ul>"


def test_a_newest_first_list_ends_at_its_first_old_offers():
    # the cards have no date; the website's own sort is newest first: once 2 opened offers in a row are older than the
    # range, the next offers are not opened, and the scan ends the website after this page
    from jobfinder.crawler import crawl_generic
    page = lambda u: _dated_offer(int(u.rsplit("-", 1)[1]), 3)
    f = SiteFetcher({"offres?sort=date": _undated_list(10), "/jobs/data-scientist-machine-learning-": page})
    pgs = list(crawl_generic(f, "https://co.fr/offres?sort=date", CFG(), set(), ScanState()))
    opened = [u for u in f.calls if "/jobs/" in u]
    assert len(opened) == 5 and pgs[-1].sorted and pgs[-1].out == 5  # 3 recent + 2 old opened; 5 never opened
    # the same list without a date sort: no order is known, every offer is opened
    f = SiteFetcher({"/offres": _undated_list(10), "/jobs/data-scientist-machine-learning-": page})
    list(crawl_generic(f, "https://co.fr/offres", CFG(), set(), ScanState()))
    assert len([u for u in f.calls if "/jobs/" in u]) == 10


def test_a_posted_within_filter_alone_is_not_a_date_order():
    # 'posted within 7 days' with a 24-hour range: the list is not sorted, so an old offer does not end it
    from jobfinder.crawler import crawl_generic
    form = ('<form method="get" action="/offres"><select name="posted"><option value="">Toutes</option>'
            '<option value="7">Moins de 7 jours</option></select></form>')
    old_first = lambda u: _dated_offer(9 - int(u.rsplit("-", 1)[1]), 8)  # offers 0 and 1 old, 2..9 recent
    f = SiteFetcher({"offres?posted=7": _undated_list(10), "/offres": form + _undated_list(10),
                     "/jobs/data-scientist-machine-learning-": old_first})
    jobs = [j for pg in crawl_generic(f, "https://co.fr/offres", CFG(date_preset="Last 24 hours"), set(), ScanState())
            for j in pg.jobs]
    assert len(jobs) == 10


def test_offers_placed_abroad_on_the_list_are_not_opened():
    from jobfinder.crawler import _card_abroad, crawl_generic
    cards = [("Bruxelles, Belgique",), ("Londres", "Paris"), ("Lyon",), ()]  # one short list: every card in one <ul>
    html = "<ul>" + "".join(f'<li><a href="/jobs/data-scientist-machine-learning-{i}">Data Scientist {i}</a>'
                            + "".join(f"<span>{p}</span>" for p in places) + "</li>" for i, places in enumerate(cards)) + "</ul>"
    soup, base = BeautifulSoup(html, "html.parser"), "https://co.fr/offres"
    abroad = [_card_abroad(soup, base, f"https://co.fr/jobs/data-scientist-machine-learning-{i}") for i in range(4)]
    assert abroad == ["Bruxelles, Belgique", "", "", ""]
    f = SiteFetcher({"/offres": html, "/jobs/data-scientist-machine-learning-": lambda u: _dated_offer(1, 9)})
    st = ScanState()
    list(crawl_generic(f, base, CFG(), set(), st))
    assert st.abroad == 1 and not any(u.endswith("-0") for u in f.calls) and sum("/jobs/" in u for u in f.calls) == 3


def test_offers_of_the_last_24_hours_are_kept_with_their_analysis():
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.config import utcnow
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    now = utcnow()
    with sessionmaker(bind=engine)() as s:
        store.add_sources(s, ["https://co.fr/jobs"])
        src = s.query(store.Source).one()
        job = lambda uid, **k: store.Job(uid=uid, source_id=src.id, title=f"Ingénieur {uid}", url=f"https://co.fr/jobs/{uid}",
                                         description="missions", last_seen=k.pop("seen", now), discovered=now, **k)
        s.add_all([job("rel", status="relevant", relevance_score=82.0, relevance_json={"skills": ["Python"]},
                       best_cv="cv", match_score=70.0, match_json={"cv": {}}, extra={"profile_key": "P", "cv_key": "C"}),
                   job("cand", status="candidate"),
                   job("old", status="relevant", relevance_score=90.0, seen=now - timedelta(hours=30))])  # read 30 h ago
        s.commit()
        assert store.keep_recent_offers(s) == 2  # the offer read 30 hours ago is not kept
        assert store.recent_count(s) == 2

        def next_session(prof, cvs):  # the app is restarted: the session's offers and websites are deleted
            s.query(store.Job).delete()
            s.query(store.Source).delete()
            s.commit()
            s.expunge_all()
            store.add_sources(s, ["https://co.fr/jobs"])
            return {j.uid: j for j in store.restore_recent_offers(s, s.query(store.Source).one(), prof, cvs)}

        kept = next_session("P", "C")  # same profile and CVs: nothing to read or analyse again
        assert set(kept) == {"rel", "cand"} and kept["rel"].status == "relevant" and kept["rel"].relevance_score == 82.0
        assert kept["rel"].match_score == 70.0 and kept["rel"].source_id == s.query(store.Source).one().id
        assert kept["cand"].status == "new"  # never analysed: the filters run again on its saved text (no page opened)
        kept = next_session("P", "C2")  # other CVs: the analysis stays, the match is made again
        assert kept["rel"].relevance_score == 82.0 and kept["rel"].match_score is None
        kept = next_session("P2", "C")  # another search profile: the filters and the AI run again
        assert kept["rel"].status == "new" and kept["rel"].relevance_score is None and kept["rel"].description == "missions"


def test_an_analysis_made_with_older_rules_is_not_kept(monkeypatch):
    """A CDD read as an internship in the morning stayed excluded after the contract rule was fixed: the version of the
    analysis rules is part of the key an analysis is reused with."""
    from jobfinder import store
    from jobfinder.config import PROFILE_DEFAULT
    before = store.profile_key(PROFILE_DEFAULT)
    assert store.profile_key(PROFILE_DEFAULT) == before  # same rules, same profile: reused
    monkeypatch.setattr(store, "ANALYSIS_RULES", store.ANALYSIS_RULES + 1)
    assert store.profile_key(PROFILE_DEFAULT) != before  # the app's rules changed: analysed again


def _undated_case(preset, seen_days, market_days=None):
    """An undated offer the app first saw `seen_days` ago (and the statistics history `market_days` ago), filtered with
    `preset` -> (status, reason, review_flags, first_seen)."""
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import apply_filters
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    now = datetime.utcnow()
    with sessionmaker(bind=engine)() as s:
        j = store.Job(uid="u1", title="Ingénieur Machine Learning (H/F)", url="https://a.fr/1", status="new", pub_date=None,
                      location_original="Paris, France", extra={"first_seen": f"{now - timedelta(days=seen_days):%Y-%m-%d}"})
        s.add_all([j, store.SeenOffer(uid="u1", first_seen=now - timedelta(days=seen_days))]
                  + ([store.MarketOffer(uid="u1", seen=now - timedelta(days=market_days), title=j.title)] if market_days else []))
        s.commit()
        apply_filters(s, run_config({**CONFIG_DEFAULT, "date_preset": preset}), PROFILE_DEFAULT)
        return j.status, j.reason, j.review_flags or "", (j.extra or {}).get("first_seen")


def test_an_undated_offer_first_seen_inside_the_range_is_returned_with_that_day():
    """No publication date, first seen yesterday: the offer is returned, marked 'date not stated', with the earliest day
    the app knows it was online."""
    from datetime import datetime, timedelta
    status, _, flags, seen = _undated_case("Last 24 hours", 1)
    assert status == "candidate" and "publication_date_unknown" in flags
    assert seen == f"{datetime.utcnow() - timedelta(days=1):%Y-%m-%d}"


def test_an_undated_offer_seen_before_the_range_began_is_outside_it():
    """Online 20 days ago (45 days in the statistics history): published then or earlier, so outside 'Last 7 days' -
    hidden like a dated offer of that age (scan of 2026-11-12: Inria, Davidson... came back every day)."""
    from datetime import datetime, timedelta
    status, reason, _, _ = _undated_case("Last 7 days", 20, market_days=45)
    assert status == "out_of_range"
    assert f"online since {datetime.utcnow() - timedelta(days=45):%Y-%m-%d}" in reason  # the history knew it earlier
    assert _undated_case("Last 30 days", 20)[0] == "candidate"  # a wider range brings it back
    assert _undated_case("All dates", 400)[0] == "candidate"


def test_a_kept_undated_offer_seen_before_a_shorter_range_is_hidden():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.config import CONFIG_DEFAULT, run_config
    from jobfinder.scan import seen_before_range
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    now = datetime.utcnow()
    with sessionmaker(bind=engine)() as s:
        j = store.Job(uid="u2", title="Data Scientist", url="https://a.fr/2", status="relevant", pub_date=None, extra={})
        s.add_all([j, store.SeenOffer(uid="u2", first_seen=now - timedelta(days=5))])
        s.commit()
        assert seen_before_range(s, j, run_config({**CONFIG_DEFAULT, "date_preset": "Last 24 hours"})) == \
            f"{now - timedelta(days=5):%Y-%m-%d}"
        assert seen_before_range(s, j, run_config({**CONFIG_DEFAULT, "date_preset": "Last 7 days"})) == ""


def _contract_case(monkeypatch, answer_kind, evidence, text):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import analysis, store
    from jobfinder.config import CONFIG_DEFAULT, PROFILE_DEFAULT, run_config
    profile = {**PROFILE_DEFAULT, "offer_types": ["internship"]}
    answer = analysis.JobAnalysis(contract_kind=answer_kind, contract_evidence=evidence, is_relevant=True, relevance_score=80,
                                  analysis_confidence=0.9, france_eligible=True, location_evidence="Paris, France")
    monkeypatch.setattr(analysis, "call_json", lambda *a, **k: answer)
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        j = store.Job(uid="c1", title="Machine Learning - Computer Vision", url="https://a.fr/1", status="candidate",
                      description=text, location_original="Paris, France", location_verified=True, france_eligible=True)
        s.add(j); s.commit()
        analysis.analyze_job(s, j, profile, run_config(CONFIG_DEFAULT))
        return j.status, (j.relevance_json or {}).get("contract_kind")


def test_an_offer_whose_contract_is_not_stated_is_kept(monkeypatch):
    # the model guesses 'job' for an offer that names no contract: an internship search keeps it, contract 'not stated'
    text = "Vous rejoindrez notre équipe de vision par ordinateur à Paris pour travailler sur la détection d'objets. " * 3
    status, kind = _contract_case(monkeypatch, "job", "", text)
    assert status == "relevant" and kind == "unknown"


def test_an_offer_stating_an_unwanted_contract_is_still_excluded(monkeypatch):
    text = "Poste en CDI à Paris dans notre équipe de vision par ordinateur, détection d'objets et suivi. " * 3
    status, kind = _contract_case(monkeypatch, "job", "CDI", text)
    assert status == "excluded" and kind == "job"


def test_a_card_listing_a_city_abroad_then_a_french_one_is_kept_in_france():
    """Davidson: 'Chef de projet BESS - Bruxelles | Île-de-France' was excluded as a Brussels offer (its first place)."""
    from jobfinder.classify import classify_location
    from jobfinder.crawler import _card_location
    card = lambda places: ('<div><a href="/nos-offres/chef-de-projet-bess">Chef de projet BESS</a>'
                           + "".join(f"<span>{p}</span>" for p in places) + "</div>")
    loc = _card_location(BeautifulSoup(card(["Bruxelles", "Île-de-France"]), "html.parser"), "https://d.fr/",
                         "https://d.fr/nos-offres/chef-de-projet-bess", "Chef de projet BESS")
    assert loc == "Bruxelles ; Île-de-France"
    r = classify_location(loc)
    assert r["france_eligible"] and r["city"] == "Île-de-France"
    loc = _card_location(BeautifulSoup(card(["Strasbourg", "Bruxelles"]), "html.parser"), "https://d.fr/",
                         "https://d.fr/nos-offres/chef-de-projet-bess", "Chef de projet BESS")
    assert loc == "Strasbourg"  # a French first place: unchanged


def test_a_contract_quote_that_contradicts_the_ai_answer_wins(monkeypatch):
    """'contrat CDD 6 mois' quoted as the evidence of an 'internship': the research engineer's offer was set aside."""
    status, kind = _contract_case(monkeypatch, "internship", "contrat CDD 6 mois",
                                  "Ingénieur de recherche en vision par ordinateur à Paris, en contrat CDD 6 mois. " * 3)
    assert kind == "job"  # an internship-only search: excluded as a job, not as an internship
    status, kind = _contract_case(monkeypatch, "job", "Ce stage de 4 mois",
                                  "Ce stage de 4 mois porte sur la vision par ordinateur à Paris. " * 3)
    assert kind == "internship" and status != "excluded"


def test_a_description_given_as_html_is_stored_as_text():
    """216 offers (Phenom: Thales, Orange, SNCF...) kept the page's markup: 30% of their text, sent to the AI."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.scan import upsert_job
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        src = store.Source(url="https://x.fr/", label="x"); s.add(src); s.flush()
        raw = {"title": "Data Analyst", "url": "https://x.fr/1", "location": "Paris", "date": None, "date_conf": 0.0,
               "date_text": "", "date_source": "", "conf": 0.9, "provenance": "phenom-api+detail",
               "description": '<p style="text-align:justify">Nous recherchons <strong>un(e) Data Analyst</strong></p><ul><li>SQL</li></ul>'}
        upsert_job(s, src, raw)
        assert s.query(store.Job).one().description == "Nous recherchons un(e) Data Analyst SQL"


def test_a_post_doc_quoted_as_a_cdd_stays_a_post_doc(monkeypatch):
    """A post-doc is a fixed-term contract too: 'CDD 24 mois' must not turn it into a regular job (a PhD/post-doc search
    would lose it), while 'CDD' quoted for an 'internship' answer does make it a job."""
    status, kind = _contract_case(monkeypatch, "phd_postdoc", "CDD 24 mois",
                                  "Post-doctorat en vision par ordinateur à Paris, CDD 24 mois au laboratoire. " * 3)
    assert kind == "phd_postdoc"


def test_a_billed_model_answers_at_most_its_share_of_a_scan(monkeypatch):
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {})
    cfg = {**cfg, "model": {"provider": "xai", "model": "grok-4.3"}, "max_billed_calls": 2}
    keys = {"xai": "x"}  # only the billed model has a key
    assert llm.call_json("u1", A, cfg, s, keys).ok and llm.call_json("u2", A, cfg, s, keys).ok
    import pytest
    with pytest.raises(llm.AIError):
        llm.call_json("u3", A, cfg, s, keys)
    assert len(calls) == 2


def test_a_card_title_must_agree_with_the_offer_address():
    # Sia Partners: the first link of each card only shows the offer's category ('Consulting', 'AI & Tech'); taken as the
    # title, it made 115 offers look unwanted and left them unopened. The card's element that names the offer is taken.
    from jobfinder.crawler import _card_title
    card = ('<div class="bloc_result"><div class="result_img"><a href="/en/career/{s}"><span class="tag_role">{c}</span></a></div>'
            '<div class="result_career"><p class="tag_categ">IT Strategy</p><p class="title_result">{t}</p>'
            '<div class="result_bot"><p class="adress">Lyon, France</p><a href="/en/career/{s}">I\'m interested</a></div></div></div>')
    offers = [("consultante-data-science-bureau-de-lyon", "Consulting", "Consultant(e) Data Science - Bureau de Lyon"),
              ("consultant-data-analyst", "AI &amp; Tech", "Consultant – Data Analyst")]
    soup = BeautifulSoup("<div>" + "".join(card.format(s=s, c=c, t=t) for s, c, t in offers) + "</div>", "html.parser")
    base = "https://sia.com/en/opportunities"
    assert [_card_title(soup, base, f"https://sia.com/en/career/{s}") for s, _, _ in offers] == \
        ["Consultant(e) Data Science - Bureau de Lyon", "Consultant – Data Analyst"]
    # nothing on the card agrees with the address: no title, so the offer is opened instead of being judged by a label
    soup = BeautifulSoup('<a href="/en/career/ingenieur-vision-par-ordinateur"><span>Design</span></a>', "html.parser")
    assert _card_title(soup, base, "https://sia.com/en/career/ingenieur-vision-par-ordinateur") == ""
    # an address made of numbers says nothing about the title: the link's text is taken as before
    soup = BeautifulSoup('<a href="/jobs/12345">Ingénieur IA (H/F)</a>', "html.parser")
    assert _card_title(soup, "https://co.fr/jobs", "https://co.fr/jobs/12345") == "Ingénieur IA (H/F)"


def test_offers_past_the_job_page_budget_are_kept_from_their_cards():
    # the website's job-page budget is used up: the next offers are kept as their cards show them, never dropped
    from jobfinder.crawler import crawl_generic
    f = SiteFetcher({"/offres": _undated_list(6), "/jobs/data-scientist-machine-learning-": lambda u: _dated_offer(1, 9)})
    st, cfg = ScanState(), {**CFG(), "max_details_per_source": 2}
    jobs = [j for pg in crawl_generic(f, "https://co.fr/offres", cfg, set(), st) for j in pg.jobs]
    assert len(jobs) == 6 and sum("/jobs/" in u for u in f.calls) == 2 and st.details_capped
    kept = [j for j in jobs if j["provenance"] == "list-only"]
    assert len(kept) == 4 and kept[0]["title"].startswith("Data Scientist")
    # an offer page robots.txt closes is kept from its card too, without being requested
    f = SiteFetcher({"/offres": '<a href="/closed/jobs/ingenieur-vision-3d">Ingénieur vision 3D (H/F)</a>'})
    jobs = [j for pg in crawl_generic(f, "https://co.fr/offres", CFG(), set(), ScanState()) for j in pg.jobs]
    assert [j["title"] for j in jobs] == ["Ingénieur vision 3D (H/F)"] and not any("/closed/" in u for u in f.calls)


def test_a_job_platform_down_for_a_moment_fails_the_website_so_it_is_read_again(monkeypatch):
    # Parrot: Talentview answered HTTP 503, the page itself shows no offer: 'completed, no offer' hid the failure; the
    # website fails with the platform's reason instead, which the scan's second try reads again
    from jobfinder import crawler
    from jobfinder.crawler import crawl_source
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))
    monkeypatch.setattr(crawler, "detect_platform", lambda f, url, st=None: ("talentview", "parrot-drones"))

    def down(*a, **k):
        raise ConnectionError("HTTP 503")
        yield
    monkeypatch.setitem(crawler.ADAPTERS, "talentview", down)

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    cfg = {**CFG(), "_keys": None}
    try:
        list(crawl_source(F({"parrot.com/en/jobs": "<html><body><h1>Jobs</h1></body></html>"}), "https://www.parrot.com/en/jobs",
                          cfg, set(), ScanState()))
        assert False, "must fail"
    except ConnectionError as e:
        assert "talentview reader: HTTP 503" in str(e)
    from jobfinder.scan import _RETRY
    assert _RETRY.search("talentview reader: HTTP 503")


def test_no_ai_browser_once_the_lists_offers_were_read_and_left_out(monkeypatch):
    # a Welcome Kit page whose only offer is a sales job: the list was read, the AI browser would only find it again
    from jobfinder import browser, crawler, pagereader
    from jobfinder.crawler import crawl_source
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))
    monkeypatch.setattr(pagereader, "read_page", lambda *a, **k: iter(()))
    started = []
    monkeypatch.setattr(browser, "crawl_ai", lambda *a, **k: started.append(1) or iter(()))

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    page = '<html><ul><li><a href="/jobs/sales-manager-uk-nordics_london"><h3>Sales Manager - UK &amp; Nordics</h3></a></li></ul></html>'
    st = ScanState()
    jobs = [j for pg in crawl_source(F({"lighton.welcomekit.co": page}), "https://lighton.welcomekit.co/", {**CFG(), "_keys": {}},
                                     set(), st) for j in pg.jobs]
    assert jobs == [] and st.skipped == 1 and not started


_WK_EMPTY = ('<html><h1>Animaj is hiring!</h1><h2>Our Job Openings</h2><p>We have no job openings at the moment!</p>'
             "<h3>Don't see the position of your dreams?</h3><a href=\"/jobs/candidatures-spontanees\">Spontaneous Application</a>"
             + "<p>About us, our values and our team.</p>" * 20 + "</html>")


def test_a_page_that_says_it_has_no_offers_is_read_without_the_ai_browser(monkeypatch):
    # Animaj, Fieldbox, Whitelab Genomics (Welcome Kit) on 2026-11-12: the AI browser ran 25-50 s each for the same empty
    # list, and the websites were listed as 'No offer found' though they work
    from jobfinder import browser, crawler, pagereader
    from jobfinder.crawler import crawl_source
    from jobfinder.scan import read_outcome, site_problem
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))
    monkeypatch.setattr(pagereader, "read_page", lambda *a, **k: iter(()))
    monkeypatch.setattr(crawler, "suggest_page", lambda f, url: "")
    started = []
    monkeypatch.setattr(browser, "crawl_ai", lambda *a, **k: started.append(1) or iter(()))

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    st = ScanState()
    jobs = [j for pg in crawl_source(F({"animaj.welcomekit.co": _WK_EMPTY}), "https://animaj.welcomekit.co/", {**CFG(), "_keys": {}},
                                     set(), st) for j in pg.jobs]
    assert jobs == [] and not started and st.no_openings == "we have no job openings at the moment"
    outcome, note = read_outcome(type("S", (), {"get": lambda self, *a: None, "add": lambda self, x: None})(),
                                 "https://animaj.welcomekit.co/", "completed", 0, 0, st)
    assert outcome == "no_openings" and not note
    src = type("Src", (), {"status": "completed", "outcome": outcome, "last_error": "", "health": ""})()
    assert site_problem(src) is None  # the website works: not in the list of websites that did not work


def test_the_no_openings_sentence_is_recognised_only_where_the_page_says_so():
    from jobfinder.crawler import no_openings
    soup = lambda h: BeautifulSoup(h, "html.parser")
    assert no_openings(soup("<p>Nos offres</p><p>Nous n’avons pas d’offres en ce moment !</p>"))
    assert no_openings(soup("<p>Aucune offre disponible pour le moment.</p>"))
    assert no_openings(soup("<div>There are currently no open positions.</div>"))
    assert not no_openings(soup("<p>Aucune offre ne vous correspond ? Envoyez une candidature spontanée</p>"))
    assert not no_openings(soup("<p>No job matches your search.</p>"))
    # a template's message next to the list its script fills: hidden, so it says nothing
    assert not no_openings(soup('<div class="empty d-none">No open positions at the moment</div><div id="list"></div>'))
    assert not no_openings(soup('<div style="display: none">Aucune offre pour le moment</div>'))


def test_a_teamtailor_feed_is_read_where_the_offers_live():
    page = ('<html><head><link href="https://teamtailor-cdn.com/x.css"></head><body><a href="https://careers.prophesee.ai/jobs/'
            '8696107-senior-electronics-engineer-f-m-x">Senior Electronics Engineer</a></body></html>')
    assert detect_from_page(None, "https://www.prophesee.ai/careers", page) == \
        ("teamtailor", ("https://careers.prophesee.ai/jobs.rss", "teamtailor"))


def test_a_career_page_that_moved_is_read_at_the_address_the_home_page_links(monkeypatch):
    # LISN: '/job-offers' answers 404; the home page links 'Nous rejoindre': it is read, and the note asks to update the
    # address (before, the website failed at every scan)
    from jobfinder import crawler
    from jobfinder.crawler import crawl_source
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    home = '<html><a href="/nous-rejoindre">Nous rejoindre</a><a href="/le-labo">Le labo</a></html>'
    jobs_page = "<ul>" + "".join(f'<li><a href="/offres/ingenieur-vision-{i}">Ingénieur vision {i} (H/F)</a></li>' for i in range(3)) + "</ul>"
    f = F({"lisn.fr/job-offers": lambda u: (_ for _ in ()).throw(ConnectionError("HTTP 404")),
           "lisn.fr/nous-rejoindre": jobs_page, "lisn.fr/offres/ingenieur-vision-": lambda u: _dated_offer(1, 9),
           "https://lisn.fr/": home})
    st = ScanState()
    jobs = [j for pg in crawl_source(f, "https://lisn.fr/job-offers", {**CFG(), "_keys": None}, set(), st) for j in pg.jobs]
    assert len(jobs) == 3 and st.moved == "https://lisn.fr/nous-rejoindre" and "+moved" in st.platform
    assert any("update this website's address" in n for n in f.notes)


def test_a_date_sort_that_does_not_follow_the_publication_dates_is_read_to_the_end():
    # Randstad Digital: '?sortBy=date' gives 11-10, 11-06, 11-09, then old offers, then recent ones again: two old offers in
    # a row do not end such a list (38 offers of the last 7 days were missed)
    from datetime import datetime, timedelta
    from jobfinder.crawler import crawl_generic
    now = datetime.utcnow()
    ages = [0, 1, 4, 2, 40, 40, 3, 40]  # days since publication, in list order

    def offer(u):
        i = int(u.rsplit("-", 1)[1])
        d = (now - timedelta(days=ages[i])).strftime("%Y-%m-%d")
        return (f'<html><head><script type="application/ld+json">{{"@type": "JobPosting", "title": "Data Scientist {i}",'
                f'"datePosted": "{d}", "description": "missions profil candidature"}}</script></head></html>')
    f = SiteFetcher({"offres?sort=date": _undated_list(len(ages)), "/jobs/data-scientist-machine-learning-": offer})
    jobs = [j for pg in crawl_generic(f, "https://co.fr/offres?sort=date", CFG(date_preset="Last 7 days"), set(), ScanState())
            for j in pg.jobs]
    assert len(jobs) == len(ages)  # every offer opened, the recent ones after the old ones included
    assert any("does not follow the publication dates" in n for n in f.notes)


def test_a_date_sort_lost_on_the_next_page_is_not_trusted_there():
    # Randstad Digital: page 1 is '?sortBy=date', its 'next' link '/page-2/' drops the sort. Page 2 starts with two old
    # offers, then has offers of the week: they must be opened
    from datetime import datetime, timedelta
    from jobfinder.crawler import crawl_generic
    now = datetime.utcnow()
    ages = {f"p1-{i}": a for i, a in enumerate([0, 1, 2, 3])} | {f"p2-{i}": a for i, a in enumerate([40, 41, 1, 2])}

    def offer(u):
        k = u.rsplit("/", 1)[1].replace("data-scientist-", "")
        d = (now - timedelta(days=ages[k])).strftime("%Y-%m-%d")
        return (f'<html><head><script type="application/ld+json">{{"@type": "JobPosting", "title": "Data Scientist {k}",'
                f'"datePosted": "{d}", "description": "missions profil candidature"}}</script></head></html>')
    page = lambda p, nxt: ("<ul>" + "".join(f'<li><a href="/jobs/data-scientist-{p}-{i}">Data Scientist {p}-{i}</a></li>' for i in range(4))
                           + "</ul>" + (f'<a rel="next" href="{nxt}">Suivant</a>' if nxt else ""))
    f = SiteFetcher({"/offres?sortBy=date": page("p1", "/offres/page-2/"), "/offres/page-2/": page("p2", None),
                     "/jobs/data-scientist-": offer})
    jobs = [j for pg in crawl_generic(f, "https://co.fr/offres?sortBy=date", CFG(date_preset="Last 7 days"), set(), ScanState())
            for j in pg.jobs]
    assert {j["title"] for j in jobs} >= {"Data Scientist p2-2", "Data Scientist p2-3"}
    assert any("do not keep the website's date sort" in n for n in f.notes)


def test_a_random_page_token_is_not_read_as_a_successfactors_marker():
    # CEA (Talentsoft, ASP.NET): its __VIEWSTATE changes at every visit and sometimes holds the letters 'j2w' and 'rmk'
    state = "dDwtMTA4j2wNzU5OTt0PDtsPGk8MT47rmkPjtsPHQ8O2w8aTwxPjs"
    page = (f'<html><input type="hidden" name="__VIEWSTATE" value="{state}"><script src="/talentsoft/x.js"></script>'
            '<a href="/offre-de-emploi/emploi-ingenieur-ia_42511.aspx">Ingénieur IA</a></html>')
    name, _ = detect_from_page(None, "https://www.emploi.cea.fr/offre-de-emploi/liste-offres.aspx?LCID=1036", page)
    assert name == "talentsoft"
    sf = '<html><script src="/platform/js/j2w/j2w.fallbacks.js"></script></html>'
    assert detect_from_page(None, "https://careers.idemia.com/", sf)[0] == "successfactors"


def test_hellowork_is_searched_with_the_profile_keywords_inside_the_date_range(monkeypatch):
    # robots.txt closes Hellowork's search and every address with '?': each keyword of the profile is read on its keyword
    # page and its job-title page (20 newest offers each). An offer whose card date is outside the range is never opened,
    # an offer found under two keywords is read once, the links to other keyword pages are not offers
    from jobfinder import crawler
    from jobfinder.config import HW_KEYWORDS_URL
    from jobfinder.crawler import crawl_source, hellowork_keyword_pages
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))
    cfg = {**CFG(date_preset="Last 7 days"), "_keys": None, "_profile": {**CFG()["_profile"], "keywords": ["vision par ordinateur", "data scientist"]}}
    assert hellowork_keyword_pages(cfg) == [
        "https://www.hellowork.com/fr-fr/emploi/mot-cle_vision-par-ordinateur.html",
        "https://www.hellowork.com/fr-fr/emploi/metier_vision-par-ordinateur.html",
        "https://www.hellowork.com/fr-fr/emploi/mot-cle_data-scientist.html",
        "https://www.hellowork.com/fr-fr/emploi/metier_data-scientist.html"]

    def card(i, age):
        return (f'<li><a href="/fr-fr/emplois/{i}.html"><h3>Ingénieur vision {i} H/F</h3></a><span>Paris - 75</span>'
                f'<span>Publiée il y a {age} jours</span></li>')
    vision = "<ul>" + card(1, 1) + card(2, 40) + "</ul>" + '<a href="/fr-fr/emploi/mot-cle_intelligence-artificielle.html">Offres d\'emploi Intelligence artificielle</a>'
    data = "<ul>" + card(1, 1) + card(3, 2) + "</ul>"
    offer = lambda u: _dated_offer(int(u.rsplit("/", 1)[1][:-5]), 9)

    class F(FakeFetcher):
        def robots_ok(self, url):
            return "?" not in url
    f = F({"mot-cle_vision-par-ordinateur.html": vision, "metier_data-scientist.html": data, "/fr-fr/emplois/": offer})
    st = ScanState()
    jobs = [j for pg in crawl_source(f, HW_KEYWORDS_URL, cfg, set(), st) for j in pg.jobs]
    assert sorted(j["url"] for j in jobs) == ["https://www.hellowork.com/fr-fr/emplois/1.html", "https://www.hellowork.com/fr-fr/emplois/3.html"]
    assert not any(c.endswith("/2.html") for c in f.calls)  # its card says 40 days: outside the range, never opened
    assert sum(c.endswith("/1.html") for c in f.calls) == 1  # under two keywords, read once
    assert not any("mot-cle_intelligence-artificielle" in c for c in f.calls)  # another keyword's list, not an offer
    assert st.platform == "hellowork (profile keywords)"


def test_the_hellowork_search_is_a_source_only_when_ticked():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from jobfinder import store
    from jobfinder.config import HW_KEYWORDS_URL
    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        ids = store.keyword_sources(s, {"hellowork_keywords": True})
        assert len(ids) == 1 and s.get(store.Source, ids[0]).url == HW_KEYWORDS_URL
        assert store.keyword_sources(s, {}) == ids  # on by default, added once
        assert store.keyword_sources(s, {"hellowork_keywords": False}) == [] and s.query(store.Source).count() == 0


def test_robots_txt_can_be_switched_off_before_a_scan(monkeypatch):
    from jobfinder import web
    from jobfinder.config import run_config
    calls = []

    class R:
        status_code, text = 200, "User-agent: *\nDisallow: /*?\nSitemap: https://h.fr/sm.xml\n"
    f = web.Fetcher(run_config({"respect_robots": True}))
    monkeypatch.setattr(f, "request", lambda m, u, _robots=False, **k: calls.append(u) or R())
    assert not f.robots_ok("https://h.fr/emploi/recherche.html?k=ia") and f.sitemaps("https://h.fr/") == ["https://h.fr/sm.xml"]
    g = web.Fetcher(run_config({"respect_robots": False}))  # the user's setting is not overridden by the fixed limits
    monkeypatch.setattr(g, "request", lambda m, u, _robots=False, **k: calls.append(u) or R())
    assert g.robots_ok("https://h.fr/emploi/recherche.html?k=ia")
    assert g.sitemaps("https://h.fr/") == ["https://h.fr/sm.xml"]  # robots.txt still names the sitemaps


def test_with_robots_txt_off_hellowork_s_search_is_read_page_after_page_until_the_range_ends(monkeypatch):
    from jobfinder import crawler
    from jobfinder.config import HW_KEYWORDS_URL
    from jobfinder.crawler import crawl_source
    monkeypatch.setattr(crawler, "render_js", lambda f, url, cfg: (None, []))

    def card(i, age):
        return (f'<li><a href="/fr-fr/emplois/{i}.html"><h3>Ingénieur vision {i} H/F</h3></a><span>Paris - 75</span>'
                f'<span>Publiée il y a {age} jours</span></li>')
    page1 = "<ul>" + "".join(card(i, 1) for i in range(30)) + "</ul>"            # 30 offers of yesterday
    page2 = "<ul>" + card(30, 2) + "".join(card(i, 40) for i in range(31, 60)) + "</ul>"  # then older than the range
    offer = lambda u: _dated_offer(1, 9)

    class F(FakeFetcher):
        def robots_ok(self, url):
            return True
    cfg = {**CFG(date_preset="Last 7 days"), "_keys": None, "respect_robots": False,
           "_profile": {**CFG()["_profile"], "keywords": ["vision par ordinateur"]}}
    f = F({"recherche.html?k=vision+par+ordinateur&st=date&p=2": page2, "recherche.html?k=vision+par+ordinateur&st=date": page1,
           "/fr-fr/emplois/": offer})
    jobs = [j for pg in crawl_source(f, HW_KEYWORDS_URL, cfg, set(), ScanState()) for j in pg.jobs]
    assert len(jobs) == 31  # page 1, and the one recent offer of page 2
    assert not any("p=3" in c for c in f.calls)  # page 2 reached offers older than the range: the search ends
    assert not any(c.endswith(f"/{i}.html") for c in f.calls for i in range(31, 60))  # old cards: never opened
    # a Hellowork search address of a website file keeps its own filters (place, contract), sorted newest first
    g = F({"recherche.html?k=stage+deep+learning&l=": page2, "/fr-fr/emplois/": offer})
    url = "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=stage+deep+learning&l=Paris&c=Stage"
    assert len([j for pg in crawl_source(g, url, cfg, set(), ScanState()) for j in pg.jobs]) == 1
    assert "l=Paris" in g.calls[0] and "c=Stage" in g.calls[0] and "st=date" in g.calls[0]
