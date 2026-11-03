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
    llm.reset_usage()
    keys = {"gemini": "g1", "gemini#2": "g2", "groq": "q", "openrouter": "o"}
    cfg = {"auto_models": True, "max_ai_calls": 100, "model": {"provider": "ollama", "model": "llama3.1"}}
    return llm, A, S(), cfg, keys, calls


def test_ladder_order_starts_with_the_best_and_skips_missing_keys():
    from jobfinder import llm
    cfg = {"auto_models": True, "model": {"provider": "ollama", "model": "llama3.1"}}
    chain = llm.model_chain(cfg, {"gemini": "g1", "gemini#2": "g2", "groq": "q"})
    assert chain[:3] == [("gemini", "gemini-3.5-flash", "gemini"), ("gemini", "gemini-3.5-flash", "gemini#2"),
                         ("groq", "openai/gpt-oss-120b", "groq")]
    assert not any(p == "openrouter" for p, _, _ in chain) and chain[-1][0] == "ollama"
    assert llm.model_chain({"auto_models": False, "model": {"provider": "groq", "model": "x"}}, {}) == [("groq", "x", "groq")]


def test_limit_moves_to_the_second_key_then_another_provider(monkeypatch):
    from jobfinder import llm as L
    q = L.QuotaExhausted("Daily quota")
    q.wait = 7200
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.5-flash", "g1"): q, ("gemini", "gemini-3.5-flash", "g2"): q})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert calls == [("gemini", "gemini-3.5-flash", "g1"), ("gemini", "gemini-3.5-flash", "g2"), ("groq", "openai/gpt-oss-120b", "q")]
    assert llm.USAGE["model"] == "groq / openai/gpt-oss-120b"
    calls.clear()
    assert llm.call_json("u2", A, cfg, s, keys).ok  # the used-up models are not asked again...
    assert calls == [("groq", "openai/gpt-oss-120b", "q")]
    llm._EXHAUSTED.clear()  # ...until their limit has passed: the best model is back
    calls.clear()
    llm.call_json("u3", A, cfg, s, keys)
    assert calls[0][1] == "gemini-3.5-flash"


def test_gone_model_and_bad_answer_move_on(monkeypatch):
    from jobfinder import llm as L
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.5-flash", "g1"): L.AIError("HTTP 404: no longer available"),
                                                        ("gemini", "gemini-3.5-flash", "g2"): "not json"})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert calls[-1][0] == "groq" and llm._EXHAUSTED[("gemini", "gemini-3.5-flash", "gemini")] > __import__("time").time() + 3000


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
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.5-flash", "g1"): gone})
    assert llm.call_json("u", A, cfg, s, keys).ok
    assert llm._EXHAUSTED[("gemini", "gemini-3.5-flash", "gemini")] > __import__("time").time() + 3000


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
    monkeypatch.setattr(f, "_wait", lambda h: None)
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
    llm, A, s, cfg, keys, calls = _ladder(monkeypatch, {("gemini", "gemini-3.5-flash", "g1"): L_error})
    assert llm.call_json("u", A, cfg, s, keys).ok
    chain = llm.model_chain(cfg, keys)
    assert ("gemini", "gemini-3.5-flash", "gemini") not in chain and ("gemini", "gemini-3.5-flash", "gemini#2") in chain


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
