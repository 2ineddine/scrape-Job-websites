"""urlclean.py - the main page of each career website, whatever filters the URL was copied with.

Career URLs are copied from a browser with the filters of a past search inside them: a search word ('keywords=audio'),
a contract type ('contract_type=Stage'), a page number ('page=3'), a relevance sort, tracking codes. Kept as they are,
they hide most offers: the app would only see page 3 of the 'audio' offers. So, before a website is read:

  removed always     tracking, display and session parameters, empty filters, page numbers, relevance sorts, fragments
  removed on company every filter, France and date included: the app applies them itself when it reads the website
  career sites       (sitefilters.with_own_filters, the platform readers, the AI browser)
  kept               the site's language and the identifiers the page cannot open without
  kept on job        search words and location: without them a multi-company job board lists the whole job market.
  boards             Contract types go when search words remain, and stay when they are the only narrowing filter
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

# multi-company job boards: their search words define the search and are kept
JOB_BOARDS = ("optioncarriere.", "hellowork.", "welcometothejungle.com", "jobteaser.", "stage.fr", "apec.fr", "glassdoor.",
              "indeed.", "linkedin.", "jobs.makesense.org", "jobsinparis.fr", "francetravail.", "choisirleservicepublic.",
              "monster.", "cadremploi.", "regionsjob.", "meteojob.", "jobijoba.")

_TRACKING = re.compile(r"^(utm_\w+|gad_\w+|gclid|gbraid|wbraid|fbclid|msclkid|mc_\w+|_ga|ig\w{2,4}|source|src|ref|refid|"
                       r"trk|trackingid|campaign\w*|cid)$", re.I)
_DISPLAY = re.compile(r"^(mobile|width|height|bga|needsredirect|jan1offset|jun1offset|hashed|createnewalert|in_iframe|"
                      r"lastselectedfacet|mode|folderrecordsperpage|per_page|perpage|pagesize|page_size|size|limit|"
                      r"\w*_autocomplete|view|display|layout|ss|stretch|stretchunit|woe)$", re.I)
_PAGE = re.compile(r"^(page|pagenum|pagenumber|pg|p|pr|startrow|start|offset|from|[\w-]+[_-]page)$", re.I)
_NO_FILTER = re.compile(r"all|any|tous|toutes|indef|rechercher|search|submit|valider|ok", re.I)  # 'd=all', 'x_search=Rechercher'
_SORT = re.compile(r"^(sort|sortby|sort_by|order|orderby|order_by|sortorder|sortdirection|sortcolumn)$", re.I)
_DATE_VALUE = re.compile(r"date|recent|newest|latest|publi|posted|desc", re.I)
_SEARCH = re.compile(r"^(q|query|keywords?|kw|k|s|search|search\[?(?:text|query|keywords?)\]?|texte|text|title|job_search|"
                     r"jobs_offer\[query\]|field_keyword\w*|what|motcle|mots?-?cles?)$", re.I)
_TYPE = re.compile(r"contract|contrat|jobtype|job_type|^type$|typesconvention|ijo-contract|employment|worktype|"
                   r"activity_domain|domain|domaine|specialt|diplom|profil|category|categorie|family|famille|"
                   r"department|dept|ijo-diploma|seniority|experience", re.I)
_LOCATION = re.compile(r"countr|pays|location|lieu|region|city|ville|bassin|geo|place|site|zone|where|ou$|l$|country_code",
                       re.I)
_FRANCE = re.compile(r"france|fr|fra|fr-fr(?:,fra)?", re.I)
_PATH_PAGE = re.compile(r"/page/\d+/?$", re.I)
_TECHNICAL = re.compile(r"^(lang|language|locale|hl|lcid|clid|page)$", re.I)  # needed to open the page, not a filter
_PATH_CATEGORY = re.compile(r"^(?:/[^/]+)?/go/[^/]+/\d+/?$", re.I)  # SuccessFactors: '/go/Jobs-in-France/3677001', a saved search
_PATH_SEARCH = re.compile(r"(/search-jobs)/.+$", re.I)  # '/en/search-jobs/France/2649/2/3017382/46/2/50/2'
_PATH_CONTRACT = re.compile(r"/(cdi|cdd|stage|alternance|internship)/?$", re.I)  # makesense: '/s/jobs/all/all/cdi'
_PATH_OFFER = re.compile(r"(/offres)/\d{4}-\d{4,}/?$", re.I)  # Inria: one offer's page, not the list that holds it
_MOVED = {("emploi.cnrs.fr", "/Recherche.aspx"): "/Offres/Recherche.aspx",  # the old address now shows an error page
          ("www.careers.philips.com", "/student/global/en/c/internship-apprenticeship-jobs"): "/global/en/search-results"}


def is_job_board(url) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(b in host for b in JOB_BOARDS) and "/companies/" not in urlparse(url).path


def clean_source_url(url: str) -> tuple[str, list[str]]:
    """-> (uniform URL, [what was removed]) for one career page URL."""
    p = urlparse(url.strip())
    board, removed, keep = is_job_board(url), [], []
    params = parse_qsl(p.query, keep_blank_values=True)
    searched = board and any(v.strip() and _SEARCH.match(re.sub(r"\[\d*\]$", "", k)) for k, v in params)
    for k, v in params:
        name, v = re.sub(r"\[\d*\]$", "", k), v.strip()
        if not v or _NO_FILTER.fullmatch(unquote(v)):
            continue  # 'q=', 'keywords=', 'f[0]=', 'd=all': no filter at all
        if _TRACKING.match(name) or _DISPLAY.match(name) or (name == "s" and re.fullmatch(r"[\d.]+", v)):
            removed.append("tracking / display")
            continue
        if _PAGE.match(name) and re.fullmatch(r"\d+", v):  # 'page=3' (not CGI's 'page=joblisting')
            removed.append(f"page number ({k}={v})")
            continue
        if not board:
            if _TECHNICAL.match(name):
                keep.append((k, v))
            else:
                removed.append(f"filter ({name}={unquote(v)[:30]})")
            continue
        if _FRANCE.fullmatch(unquote(v).strip()):
            keep.append((k, v))  # a country filter, whatever its name
            continue
        if _SORT.match(name):
            if _DATE_VALUE.search(v):
                keep.append((k, v))  # the site's own 'newest first': useful
            else:
                removed.append(f"sort ({v})")
            continue
        if _LOCATION.search(name) and not _SEARCH.match(name):
            keep.append((k, v))  # country / location: the search is about France
            continue
        if searched and _TYPE.search(k):  # a board's search words keep its list small without them
            removed.append(f"type / domain ({unquote(v)[:30]})")
            continue
        keep.append((k, v))
    path = p.path
    if _PATH_PAGE.search(path):
        path = _PATH_PAGE.sub("/", path)
        removed.append("page number (path)")
    if not board and re.search(r"/searchjobs/[^/]+$", path, re.I):  # Siemens: '/SearchJobs/deep%20learning'
        path = re.sub(r"(/searchjobs)/[^/]+$", r"\1", path, flags=re.I)
        removed.append("search words (path)")
    if not board and (_PATH_CATEGORY.match(path) or _PATH_SEARCH.search(path)):
        path = "/" if _PATH_CATEGORY.match(path) else _PATH_SEARCH.sub(r"\1", path)
        removed.append("saved search (path)")
    if searched and _PATH_CONTRACT.search(path):
        path = _PATH_CONTRACT.sub("", path)
        removed.append("contract type (path)")
    if _PATH_OFFER.search(path):
        path = _PATH_OFFER.sub(r"\1", path)
        removed.append("single offer: its list is read")
    path = _MOVED.get(((p.hostname or "").lower(), path), path)
    clean =urlunparse(p._replace(path=path, query=urlencode(keep, doseq=True), fragment=""))
    return clean, list(dict.fromkeys(removed))
