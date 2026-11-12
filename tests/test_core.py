"""Offline tests: URL import, dates, France location, title filter, date-first gate (no network)."""
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from jobfinder.adapters import _parse_detail, _raw, make_gate, raw_verdict
from jobfinder.classify import classify_location, html_location, title_filter
from jobfinder.config import PROFILE_DEFAULT, utcnow
from jobfinder.crawler import _card_dates
from jobfinder.dates import date_decision, html_date, merge_dates, parse_date, refine_raw_date, text_publication_date
from jobfinder.web import check_url, extract_urls, html_to_text, syntactic_ok

S = lambda h: BeautifulSoup(h, "html.parser")
P = PROFILE_DEFAULT
now = datetime(2026, 10, 13, 12)
n16 = datetime(2026, 10, 16, 12)
cfg30 = {"date_preset": "Last 30 days", "unknown_date_policy": "review"}


def test_urls_dates_locations_titles():
    html = """<script>const urls=['https://a.example.com/careers',"https:\\/\\/b.example.org\\/jobs?utm_x=1",
    'http://localhost/x','http://169.254.169.254/latest','javascript:alert(1)','https://a.example.com/careers/'];</script>
    <a href="https://c.example.net/open">x</a><a href="broken"""
    u = extract_urls(html)
    assert u == ["https://a.example.com/careers", "https://b.example.org/jobs", "https://c.example.net/open"], u
    assert parse_date("il y a 3 jours", now)[0] == now - timedelta(days=3)
    assert parse_date("12 janvier 2026")[0] == datetime(2026, 1, 12)
    assert parse_date("March 5, 2026")[0] == datetime(2026, 3, 5)
    assert parse_date("2026-10-10T08:00:00Z")[0] == datetime(2026, 10, 10, 8)
    assert parse_date("n/a")[0] is None
    cfg = {"date_preset": "Last 7 days"}
    assert date_decision(datetime(2026, 10, 10), 0.9, cfg, now)[0] is True
    assert date_decision(datetime(2026, 9, 1), 0.9, cfg, now)[0] is False
    assert date_decision(None, 0, cfg, now)[0] is None
    assert classify_location("Paris, France")["france_eligible"]
    assert classify_location("London, UK")["location_verified"] and not classify_location("London, UK")["france_eligible"]
    assert classify_location("Remote - France")["france_eligible"]
    assert not classify_location("Remote - Germany")["france_eligible"]
    assert classify_location("London; Paris")["france_eligible"]
    assert not classify_location("Remote")["location_verified"]
    assert not classify_location("")["location_verified"]
    assert not classify_location("Paris, Texas, USA")["france_eligible"]
    assert title_filter("Ingénieur de recherche en vision par ordinateur", "", P)[0] == "pass"
    assert title_filter("Applied Scientist — Speech and Audio Intelligence", "", P)[0] in ("pass", "maybe")
    assert title_filter("Senior Accountant", "", P)[0] == "reject"
    assert not check_url("http://127.0.0.1/")[0] and not syntactic_ok("file:///etc/passwd")
    assert html_date(S('<time datetime="2026-10-09">9 Oct</time>'), now)[0] == datetime(2026, 10, 9)
    assert html_date(S('<meta property="article:published_time" content="2026-10-01T10:00:00+02:00">'), now)[0] == datetime(2026, 10, 1, 10)
    assert html_date(S("<p>Publiée le 12 octobre 2026</p>"), now)[0] == datetime(2026, 10, 12)
    assert html_date(S("<p>Posted 3 days ago</p>"), now)[0] == now - timedelta(days=3)
    assert html_date(S("<p>Date de publication : 05/10/2026</p>"), now)[0] == datetime(2026, 10, 5)
    assert html_date(S("<p>Last updated 12 October 2026</p>"), now)[0] is None
    assert html_date(S("<p>Date limite 30/11/2026</p>"), now)[0] is None
    assert html_date(S('<p>Updated <time datetime="2026-10-12">x</time></p>'), now)[0] is None
    for ttl in ("Deploy deep learning model", "Deploying Deep-Learning Models in production", "Ingénieur apprentissage automatique",
                "Machine Learning Engineer", "Research Scientist (PhD)"):
        assert title_filter(ttl, "", P)[0] == "pass", ttl
    assert title_filter("Deploy models on edge devices", "", P)[0] == "maybe"
    for ttl in ("Postdoctoral Researcher - Computer Vision", "Post-doc en traitement du signal", "Internship: Machine Learning",
                "Stage - Ingénieur IA", "Stagiaire Data Scientist", "Alternance Data Scientist", "PhD Student in NLP",
                "PhD - Speech Recognition", "Doctorant en vision par ordinateur", "Thèse CIFRE en IA", "Contrat doctoral - robotique",
                "Working Student Deep Learning", "Apprenti ingénieur IA"):
        assert title_filter(ttl, "", P)[0] == "reject", ttl
    assert title_filter("Machine Learning Engineer", "", P, "Intern")[0] == "reject"
    assert title_filter("Internal Tools Engineer", "", P)[1].startswith("technical")
    assert title_filter("Postdoc Deep Learning", "", {**P, "offer_types": ["job", "phd_postdoc"]})[0] == "pass"
    assert title_filter("Engineer, Transformer Enhancement", "", P)[0] in ("pass", "maybe")
    assert title_filter("Ingénieur de recherche en apprentissage automatique", "", P)[0] == "pass"
    assert title_filter("Research Engineer, Machine Learning", "", P)[0] == "pass"
    assert parse_date("Wed Oct 14 00:00:00 UTC 2026")[0] == datetime(2026, 10, 14)
    assert html_date(S("<p>Publication Date: Oct 14, 2026</p>"), now)[0] == datetime(2026, 10, 14)
    atos = S('<span itemprop="streetAddress"><meta itemprop="streetAddress" content="London, GB"></span>'
             '<span data-careersite-propertyid="location"><p id="job-location"><span class="jobGeoLocation">London, GB</span></p></span>')
    assert html_location(atos) == "London, GB"
    r = classify_location("London, GB")
    assert r["location_verified"] and not r["france_eligible"]
    assert classify_location("Austin, TX")["location_verified"] and not classify_location("Remote, EU")["location_verified"]
    assert classify_location("Paris, FR")["france_eligible"]
    assert title_filter("Specialised Sales Manager - HPC, AI & Quantum Computing", "", {**P, "exclude_titles": []})[0] == "reject"


def test_date_regressions():
    assert parse_date("06 Oct", n16)[0] == datetime(2026, 10, 6) and parse_date("6 octobre", n16)[0] == datetime(2026, 10, 6)
    assert parse_date("Oct 6", n16)[0] == datetime(2026, 10, 6) and parse_date("Posted 06 oct", n16)[0] == datetime(2026, 10, 6)
    assert parse_date("20 dec", n16)[0] == datetime(2025, 12, 20)  # not yet reached this year -> last year
    assert parse_date("06/10/2026") == (datetime(2026, 10, 6), 0.45)  # ambiguous: guessed but marked unsure
    assert parse_date("06/10/2026", dayfirst=True) == (datetime(2026, 10, 6), 0.8)
    assert parse_date("06/10/2026", dayfirst=False) == (datetime(2026, 6, 10), 0.8)
    assert parse_date("13/10/2026")[0] == datetime(2026, 10, 13) and parse_date("10/13/2026")[0] == datetime(2026, 10, 13)
    assert parse_date("posted on 06 oct 2026 - closes in 5 days", n16)[0] == datetime(2026, 10, 6)  # '5 days' is not the date
    assert parse_date("30+ days ago", n16)[1] < 0.5 and parse_date("3 days ago", n16)[1] >= 0.5
    assert parse_date("2026-10-06T00:00:00+02:00")[0] == datetime(2026, 10, 6)  # no shift to the 5th
    assert parse_date("2026-10-05T22:00:00Z")[0] == datetime(2026, 10, 5, 22)
    assert html_date(S("<p>Posted 06 Oct</p>"), n16)[0] == datetime(2026, 10, 6)
    assert html_date(S("<p>Publié le 6 oct.</p>"), n16)[0] == datetime(2026, 10, 6)
    assert html_date(S('<html lang="fr"><p>Date de publication : 06/10/2026</p></html>'), n16)[:2] == (datetime(2026, 10, 6), 0.75)
    assert html_date(S('<html lang="en-US"><p>Posted: 10/06/2026</p></html>'), n16)[0] == datetime(2026, 10, 6)
    assert html_date(S('<time datetime="2026-10-09">a</time><time datetime="2026-09-01">b</time>'), n16)[1] < 0.5
    assert html_date(S('<meta name="date" content="2026-10-16">'), n16)[1] < 0.5  # page date, not the job's
    ld = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"ML Engineer","datePosted":"2026-10-14",'
          '"description":"x"}</script><body><p>Posted 06 Oct 2026</p></body></html>')
    assert _parse_detail(ld, "https://x.com/jobs/1", n16)["date"] == datetime(2026, 10, 6)  # page text beats a refreshed JSON-LD date
    ld2 = ld.replace("Posted 06 Oct 2026", "Posted 3 days ago")
    assert _parse_detail(ld2, "https://x.com/jobs/1", n16)["date"] == datetime(2026, 10, 14)  # relative text does not override
    assert html_date(S("<p>Publication date: 2026-10-12</p>"), n16)[0] == datetime(2026, 10, 12)  # ISO date written in the text
    assert html_date(S("<p>Posted: 2026-10-06 | Deadline: 2026-11-30</p>"), n16)[0] == datetime(2026, 10, 6)


def test_description_date_beats_feed_date():
    orange = ('<strong>Date de publication :</strong> Jul 01, 2026, 4:19PM</p><p><strong>Durée du contrat : </strong>12 months</p>'
              '<p><br> Vous portez des actions qui contribuent à l’amélioration du Système de Management...')
    r = _raw("Chargé de prévention", "https://orange.jobs/1", description=html_to_text(orange), date=datetime(2026, 10, 15),
             date_text="15/10/2026", date_conf=0.95, date_source="network JSON")
    refine_raw_date(r, n16)
    assert r["date"] == datetime(2026, 7, 1) and r["date_conf"] >= 0.8, r
    assert date_decision(r["date"], r["date_conf"], cfg30, n16, r["date_text"])[0] is False
    ldo = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"X","datePosted":"2026-10-15",'
           '"description":"' + orange.replace('"', "'").replace("’", "'") + '"}</script></html>')
    assert _parse_detail(ldo, "https://orange.jobs/2", n16)["date"] == datetime(2026, 7, 1)
    for txt, want in (("Date de publication : Jul 01, 2026, 4:19PM", datetime(2026, 7, 1)),
                      ("Publiée le 1er juillet 2026", datetime(2026, 7, 1)), ("Published: 1st of July 2026", datetime(2026, 7, 1)),
                      ("Posted on July 1, 2026", datetime(2026, 7, 1)), ("Date de publication : 01/07/2026", datetime(2026, 7, 1)),
                      ("Date de publication : 01.07.2026", datetime(2026, 7, 1)), ("Date de publication : 01-07-26", datetime(2026, 7, 1)),
                      ("Posted: 2026/07/01", datetime(2026, 7, 1)), ("publication date 20260701", datetime(2026, 7, 1)),
                      ("Veröffentlicht am 1. Juli 2026", datetime(2026, 7, 1)), ("Fecha de publicación: 1 de julio de 2026", datetime(2026, 7, 1)),
                      ("Mise en ligne le 1 juil. 2026 à 16h19", datetime(2026, 7, 1)),
                      ("Offre publiée il y a 3 jours", n16 - timedelta(days=3)), ("Posted 2 weeks ago", n16 - timedelta(weeks=2)),
                      ("Posted 3d ago", n16 - timedelta(days=3)), ("vor 3 Tagen veröffentlicht", n16 - timedelta(days=3)), ("Publiée hier", n16 - timedelta(days=1)),
                      ("Posted Today", n16), ("Date de publication : 2026-10-12T08:00:00+02:00", datetime(2026, 10, 12))):
        got = text_publication_date(txt, n16)[0]
        assert got == want or (want is None and got is None), (txt, got)
    assert parse_date("1760000000")[0] is not None and parse_date("/Date(1760000000000)/")[0].year == 2025
    assert parse_date(1760000000000)[0].year == 2025
    for txt in ("Date limite de candidature : 30/11/2026", "Mis à jour le 12/10/2026", "Début : 01/12/2026",
                "Last updated: Oct 12, 2026", "Closing date 2026-11-30", "Durée du contrat : 12 months", "Expires Nov 30, 2026"):
        assert text_publication_date(txt, n16)[0] is None, txt
    both = text_publication_date("Posted: 2026-10-06 | Updated: 2026-10-15 | Deadline: 2026-11-30", n16)
    assert both[0] == datetime(2026, 10, 6)
    # conflicts between two structured dates: the earliest is kept, with lower confidence
    m = merge_dates((datetime(2026, 10, 15), 0.95, "listing", "15/10/2026"), (datetime(2026, 7, 1), 0.9, "json-ld", "2026-07-01"))
    assert m[0] == datetime(2026, 7, 1) and m[1] <= 0.6
    assert merge_dates((datetime(2026, 10, 14), 0.45, "listing", "30+ days ago"), (datetime(2026, 10, 1), 0.9, "detail", "x"))[0] == datetime(2026, 10, 1)
    # window decision: calendar days, future dates, ambiguous numeric dates
    assert date_decision(datetime(2026, 9, 16), 0.9, cfg30, n16)[0] is True and date_decision(datetime(2026, 9, 15), 0.9, cfg30, n16)[0] is False
    assert date_decision(datetime(2026, 12, 25), 0.9, cfg30, n16)[0] is None  # future = misparsed
    assert date_decision(datetime(2026, 10, 6), 0.45, cfg30, n16, "06/10/2026")[0] is None   # 6 Oct (in) or 10 Jun (out): cannot tell
    assert date_decision(datetime(2026, 10, 12), 0.45, cfg30, n16, "12/10/2026")[0] is True  # 12 Dec would be the future: only 12 Oct
    assert date_decision(datetime(2026, 7, 1), 0.45, cfg30, n16, "01/07/2026")[0] is False   # 1 Jul or 7 Jan: both too old
    assert date_decision(datetime(2026, 10, 6), 0.45, {**cfg30, "date_preset": "Last 7 days"}, n16, "06/10/2026")[0] is False  # 6 Oct and 10 Jun are both older than 7 days
    assert date_decision(None, 0, cfg30, n16)[0] is None and date_decision(None, 0, {"date_preset": "All dates"}, n16)[0] is True


def test_date_first_gate():
    gate = make_gate({"date_preset": "Last 24 hours"})
    assert gate(utcnow() - timedelta(hours=5), 0.9, "") is True and gate(utcnow() - timedelta(days=3), 0.9, "") is False
    assert gate(None, 0.0, "") is None and raw_verdict({}, _raw("x", "u")) is True
    html_cards = ('<html lang="fr"><body><div><a href="https://x.com/jobs/1">AI 1</a><span>Publiée il y a 40 jours</span></div>'
                  '<div><a href="https://x.com/jobs/2">AI 2</a><span>Publiée hier</span></div>'
                  '<div><a href="https://x.com/jobs/3">AI 3</a><span>Date limite 30/11/2026</span></div></body></html>')
    cd = _card_dates(BeautifulSoup(html_cards, "html.parser"), "https://x.com/jobs", n16)
    assert cd["https://x.com/jobs/1"][0] == n16 - timedelta(days=40) and cd["https://x.com/jobs/2"][0] == n16 - timedelta(days=1)
    assert "https://x.com/jobs/3" not in cd  # a deadline is not a publication date


def test_the_dates_of_similar_offers_on_a_job_page_are_not_the_offers_date():
    # Choisir le service public: every offer page lists 'Offres similaires', each 'En ligne depuis le 10 novembre 2026'; the
    # offer's own date (JSON-LD and list card: 3 November) was replaced by theirs, which can move a new offer out of a
    # 24-hour range as well as bring an old one in
    from jobfinder.adapters import _parse_detail
    now = datetime(2026, 11, 11, 12)
    similar = "".join(f"<li>Offre {i} Employeur : Communes En ligne depuis le 10 novembre 2026</li>" for i in range(5))
    page = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"TRACFIN - Data scientist",'
            '"datePosted":"2026-11-03","description":"Missions : analyse de données."}</script><body><main>'
            f"<h1>TRACFIN - Data scientist</h1><h2>Offres similaires</h2><ul>{similar}</ul></main></body></html>")
    assert _parse_detail(page, "https://csp.gouv.fr/offre-emploi/tracfin", now)["date"] == datetime(2026, 11, 3)
    # one later date in the page, outside the offer's text, does not replace its structured date either
    page2 = page.replace(similar, "<li>Offre 1 En ligne depuis le 10 novembre 2026</li>")
    assert _parse_detail(page2, "https://csp.gouv.fr/offre-emploi/tracfin", now)["date"] == datetime(2026, 11, 3)


def test_pasted_addresses_are_read_one_per_line_without_quotes():
    # the box says 'One URL per line': plain lines were refused (only quoted addresses were read) and its button stayed off
    from jobfinder.web import extract_urls, pasted_urls
    text = "https://www.equans.com/join-us\n  https://jobs.stationf.co/companies/beamy,\n'https://x.fr/jobs'\nnot an address"
    assert pasted_urls(text) == ["https://x.fr/jobs", "https://www.equans.com/join-us", "https://jobs.stationf.co/companies/beamy"]
    assert extract_urls("<!-- https://old.fr/jobs -->") == []  # a website FILE keeps its rule: an address in a comment is not read


def test_old_saved_settings_get_robots_txt_off_once(monkeypatch):
    # older versions saved respect_robots=True in every config (a fixed value, not a choice): once off, then the user's choice
    from jobfinder.config import CONFIG_DEFAULT, upgrade_config
    from jobfinder.store import _merge
    saved = _merge(CONFIG_DEFAULT, {"date_preset": "Last 24 hours", "respect_robots": True})  # as the app loads it
    cfg, changed = upgrade_config(saved)
    assert changed and cfg["respect_robots"] is False
    chosen = {**cfg, "respect_robots": True}
    assert upgrade_config(_merge(CONFIG_DEFAULT, chosen)) == (_merge(CONFIG_DEFAULT, chosen), False)


def test_what_robots_txt_gives_is_used_even_when_its_rules_are_not():
    # rules off: no page is refused, but the pause the website asks for is kept (it costs time, never an offer), and a
    # rendered page's advertising / tracking requests are never loaded
    from jobfinder.web import Robots, _AD_HOSTS
    r = Robots("User-agent: *\nCrawl-delay: 10\nDisallow: /jobs\n\nUser-agent: Googlebot\nCrawl-delay: 1\n")
    assert r.delay == 5.0 and not r.allowed("https://x.fr/jobs/1")
    assert Robots("User-agent: *\nCrawl-delay: 600\n").delay == 5.0 and Robots("").delay == 0.0
    assert _AD_HOSTS.search("ad.doubleclick.net") and _AD_HOSTS.search("px.ads.linkedin.com") and _AD_HOSTS.search("api.hubapi.com")
    assert not _AD_HOSTS.search("careers.smartrecruiters.com") and not _AD_HOSTS.search("api.lever.co")
