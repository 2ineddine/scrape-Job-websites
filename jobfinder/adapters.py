"""adapters.py - reading job offers from the platforms career websites are built on.

Every adapter yields Page objects (a batch of raw job dicts) and uses the platform's own public data feed when there is
one, because it gives the exact publication date, the location and the full description without a browser:

  workday          *.myworkdayjobs.com          JSON API (list + detail: 'startDate' = posting date)
  smartrecruiters  careers.smartrecruiters.com  public postings API ('releasedDate')
  successfactors   SAP SuccessFactors career    job table sorted newest first + job page ('datePosted')
                   sites (jobs2web / RMK)
  icims            *.icims.com                  job list ('Date de publication ... (10/23/2026)') + job page
  teamtailor       Teamtailor career sites      jobs.rss feed ('pubDate', city, country, full description)
  talentsoft       Talentsoft / Cegid sites     offerRss.ashx feed
  breezy           *.breezy.hr                  /json ('published_date')
  recruitee        *.recruitee.com              /api/offers ('published_at')
  greenhouse, lever, ashby                      official job-board APIs (board name from the page, or guessed from the
                                                company name and checked: guess_board)
  phenom           Phenom career sites          /widgets search API ('postedDate'), France facet
  eightfold        *.eightfold.ai               /api/apply/v2/jobs ('t_create') + detail
  jibe             Jibe career sites            /api/jobs sorted by 'posted_date'
  workable         apply.workable.com           v3 list ('published') + v2 detail
  talentview       *.talentview.io (iframe)     campaign list ('last_activation_at')
  wordpress        WordPress sites with a job   REST API (/wp-json/wp/v2/<job type>: publication date + full text)
                   post type
  rss              any page that links a job feed

detect_platform() recognises the platform from the URL, then from the page itself (links / embeds of these platforms
inside a company's own career page). Anything else is read by the generic crawler (crawler.py) or the AI browser."""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .classify import analyze_title, classify_location, html_location, location_from_text
from .config import PROFILE_DEFAULT, utcnow
from .dates import (NO_DATE, _content_text, _dayfirst, date_decision, html_date, merge_dates, parse_date, reconcile_date,
                    refine_raw_date)
from .web import PARSER, AccessError, html_to_text, norm, normalize_url, syntactic_ok


# --------------------------------------------------------------------------- the common shapes
@dataclass
class Page:
    key: str
    url: str
    jobs: list
    total: int | None = None
    error: str | None = None
    out: int = 0   # job links skipped WITHOUT being opened because their listing date is already outside the range
    seen: int = 0  # jobs of this page already known and inside the range (they prove the list is not finished)
    ordered: bool = True  # the list is sorted newest first: pages of old jobs mean the list is finished
    sorted: bool = False  # the WEBSITE sorts this list by date (its own sort): the first old offer ends the list


@dataclass
class ScanState:
    truncated: bool = False
    details_capped: bool = False  # the job-page budget ran out (the list itself was read to the end)
    deadline: float = 0.0  # time.time() after which no more job page is opened for this website (0 = none)
    platform: str = ""
    out_known: set = field(default_factory=set)  # urls already known to be OUTSIDE the date range (never re-opened)
    stopped_by_date: bool = False
    redirect: tuple | None = None  # (platform, argument): the page's offers live on this job platform, with their dates
    held: list = field(default_factory=list)  # the page's own offers, kept in case that platform cannot be read
    more_pages: bool = False  # the list has more pages the crawler may not open (script links, robots.txt): use the sitemap


def _raw(title, url, **kw):
    d = dict(title=title, url=url, employer="", location="", description="", date_text="", date=None,
             date_source="", date_conf=0.0, apply_url=url, platform_id="", employment_type="", remote_hint=None,
             applicant_countries=None, country_hint=None, department="", conf=0.6, provenance="html")
    d.update(kw)
    return d


def _date(v, source, cap=0.9, dayfirst=None):
    d, c = parse_date(v, dayfirst=dayfirst) if v else (None, 0.0)
    return dict(date=d, date_conf=min(c, cap) if d else 0.0, date_source=source if d else "", date_text=str(v or ""))


def _get_date(r):
    return (r["date"], r["date_conf"], r["date_source"], r["date_text"]) if r.get("date") else NO_DATE


_TRUSTED_SOURCES = re.compile(r"api|feed|workday|smartrecruiters|successfactors list|icims|wordpress|breezy|recruitee|lever|"
                              r"greenhouse|ashby|JSON-LD|job description")


def distrust_shared_dates(jobs):
    """Several different offers of one page carrying the SAME date taken from page markup (meta tags, <time>, page data)
    is the website template's date, not the offers': it becomes unknown. Dates from platform APIs and feeds are kept."""
    weak = [j for j in jobs if j.get("date") and not _TRUSTED_SOURCES.search(j.get("date_source") or "")]
    days = Counter(j["date"].date() for j in weak)
    for day, n in days.items():
        if n >= 3 and len({norm(j["title"]) for j in weak if j["date"].date() == day}) >= 3:
            for j in weak:
                if j["date"].date() == day:
                    j["date_conf"] = min(j["date_conf"], 0.3)
                    j["date_source"] = f"{j['date_source']} (same date on {n} offers: page date, not trusted)"
    return jobs


def make_gate(cfg):
    """gate(date, conf, text) -> True (inside the range) | False (provably outside) | None (unknown). Evaluated at call time."""
    return lambda d, c=0.0, t="": date_decision(d, c, cfg, None, t)[0]


def raw_verdict(cfg, raw):
    """True / False / None for a freshly extracted job dict. No gate configured -> True (nothing is filtered)."""
    gate = cfg.get("_gate")
    if not gate:
        return True
    raw = refine_raw_date(raw)
    return gate(raw["date"], raw["date_conf"], raw["date_text"])


def worth_opening(cfg, raw) -> bool:
    """DATE FIRST, then title: the job page is opened only when the job may be inside the range and its title is not
    clearly irrelevant. Saves hundreds of requests on large company boards."""
    if raw.get("date") and raw_verdict(cfg, raw) is False:
        return False
    if (raw.get("location") or raw.get("country_hint")) and _wants_france(cfg):
        loc = classify_location(raw.get("location"), raw.get("country_hint"), raw.get("remote_hint"))
        if loc["location_verified"] and not loc["france_eligible"]:
            return False  # 'Chennai', 'Frickenhausen, DE': excluded later anyway
    if len(re.sub(r"\W", "", raw.get("title") or "")) < 3:
        return True  # unreadable title: only the job page can tell
    return analyze_title(raw["title"], raw.get("department", ""), cfg.get("_profile") or PROFILE_DEFAULT,
                         raw.get("employment_type", "")).verdict != "reject"


class _Budget:
    """Job pages opened per website (safety ceiling)."""

    def __init__(self, cfg, st):
        self.left, self.st = int(cfg["max_details_per_source"]), st

    def take(self) -> bool:
        if self.st.deadline and time.time() > self.st.deadline:
            self.st.details_capped = True  # this website's time is up: the offers keep their list data
            return False
        if self.left <= 0:
            self.st.details_capped = True
            return False
        self.left -= 1
        return True


def _wants_france(cfg) -> bool:
    return norm(cfg.get("site_filter_location") or "") in ("france", "fr")


# --------------------------------------------------------------------------- JSON-LD and JSON found in pages
def _jsonld_jobs(soup, base):
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            if "@graph" in o:
                walk(o["@graph"])
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                out.append(o)

    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(sc.string or sc.get_text() or "", strict=False))
        except (ValueError, TypeError):
            continue
    jobs = []
    for o in out:
        locs, country = [], None
        jl = o.get("jobLocation") or []
        for place in (jl if isinstance(jl, list) else [jl]):
            a = (place or {}).get("address", {}) if isinstance(place, dict) else {}
            if isinstance(a, dict):
                c = a.get("addressCountry")
                c = c.get("name") if isinstance(c, dict) else c
                country = country or c
                locs.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), c) if x))
        ac = o.get("applicantLocationRequirements") or []
        ac = [x.get("name") if isinstance(x, dict) else x for x in (ac if isinstance(ac, list) else [ac])]
        org = o.get("hiringOrganization")
        org = org.get("name") if isinstance(org, dict) else (org or "")
        dt = o.get("datePosted") or ""
        d, c = parse_date(dt)
        url = urljoin(base, o.get("url") or o.get("sameAs") or base)
        ident = o.get("identifier")
        ident = ident.get("value") if isinstance(ident, dict) else ident
        jobs.append(_raw(html_to_text(o.get("title", "")), url, employer=org, location=" ; ".join(x for x in locs if x),
                         description=html_to_text(o.get("description", "")), date_text=str(dt), date=d,
                         date_source="JSON-LD datePosted", date_conf=c, platform_id=str(ident or ""),
                         employment_type=str(o.get("employmentType") or ""),
                         remote_hint=o.get("jobLocationType") == "TELECOMMUTE",
                         applicant_countries=[x for x in ac if x] or None, country_hint=country, conf=0.9,
                         provenance="json-ld"))
    return jobs


def _k(k) -> str:
    return re.sub(r"[^a-z]", "", str(k).lower())


_TITLE = ("title", "jobtitle", "positiontitle", "postingtitle", "name", "text")
_URL = ("url", "joburl", "absoluteurl", "hostedurl", "canonicalpositionurl", "detailurl", "jobpostingurl", "positionurl",
        "permalink", "externalpath", "applyurl", "link", "href", "slug")
_ID = ("id", "jobid", "reqid", "requisitionid", "jobreqid", "externalid", "positionid", "reference", "jobnumber")
_LOC = ("location", "locations", "locationtext", "locationstext", "primarylocation", "joblocation", "city", "cities",
        "addresscity", "state", "region", "regionvalue", "addressregion", "country", "countryname", "workplace", "address")
_DATE = ("dateposted", "posteddate", "postedon", "postdate", "publisheddate", "publishedat", "publishdate", "publicationdate",
         "dateofpublication", "datepublished", "postingdate", "postingstartdate", "publicationstartdate", "pubdate", "onlinedate", "firstpublished",
         "firstpublisheddate", "releasedate", "releaseddate", "searchabledate", "lastpublicationdate", "startdate", "createddate", "createdat",
         "datecreated", "created")
_DATE_WEAK = {"createddate", "createdat", "datecreated", "created", "startdate", "lastpublicationdate"}  # record creation / start: often not the publication
_DESC = ("description", "jobdescription", "descriptionplain", "shortdescription", "summary", "content", "teaser",
         "descriptionteaser", "jobsummary")
_DEPT = ("department", "team", "category", "function", "businessunit")
_TYPE = ("employmenttype", "contracttype", "worktype", "commitment", "jobtype")
_NAMEKEYS = {"name", "label", "text", "city", "commune", "town", "ville", "region", "country", "state", "value", "displayname", "addresslocality",
             "addresscountry", "descriptor"}


def _flat(v, d=0) -> str:
    if v is None or d > 3:
        return ""
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v).strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, dict):
        if v and all(re.fullmatch(r"[a-z]{2}(?:[-_][a-zA-Z]{2})?", str(kk)) for kk in v):  # {"fr-fr": "Stage ..."}
            return next((_flat(x, d + 1) for x in v.values() if _flat(x, d + 1)), "")
        parts = [_flat(x, d + 1) for kk, x in v.items() if _k(kk) in _NAMEKEYS]
        return ", ".join(p for p in dict.fromkeys(parts) if p)
    if isinstance(v, list):
        return " ; ".join(p for p in dict.fromkeys(_flat(x, d + 1) for x in v[:10]) if p)
    return ""


def _pick(n, keys) -> str:
    for k in keys:
        if k in n:
            s = _flat(n[k])
            if s:
                return s
    return ""


def _pick_date(n):
    """-> (date, confidence, source, text): the first date key that really parses (an 'N/A' value does not block the next key)."""
    for k in _DATE:
        if k in n:
            txt = _flat(n[k])
            d, c = parse_date(txt) if txt else (None, 0.0)
            if d is not None:
                return d, min(c, 0.6) if k in _DATE_WEAK else c, f"network JSON {k}", txt
    return NO_DATE


def _abs_url(u, base):
    if re.match(r"https?://", u, re.I):
        return u
    host, path = urlparse(base).hostname or "", urlparse(base).path
    if host.endswith("myworkdayjobs.com") and u.startswith("/job/"):  # Workday: /<locale>/<site> + externalPath
        seg = [x for x in path.split("/") if x]
        prefix = "/" + "/".join(seg[:2]) if len(seg) >= 2 and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", seg[0]) \
            else ("/" + seg[0] if seg else "")
        u = prefix + u
    return urljoin(base, u)


_PRODUCT_KEYS = {"price", "prices", "sku", "productid", "masterid", "addtocart", "currency", "pricerange", "instock"}


def _strings(o, d=0):
    """Every text value of a JSON item (two levels deep): where an apply link to a job platform may hide."""
    if isinstance(o, str):
        yield o
    elif d < 2 and isinstance(o, dict):
        for v in o.values():
            yield from _strings(v, d + 1)
    elif d < 2 and isinstance(o, list):
        for v in o[:5]:
            yield from _strings(v, d + 1)


def _as_job(o, base):
    if not isinstance(o, dict):
        return None
    n = {}
    for k, v in o.items():
        n.setdefault(_k(k), v)
    if _PRODUCT_KEYS & set(n):
        return None  # a product of the website's shop or catalogue (Ubisoft's games), not an offer
        if str(k).startswith("field_"):  # Drupal: field_searchable_date
            n.setdefault(_k(k)[5:], v)
    title = html_to_text(_pick(n, _TITLE))
    if not (3 <= len(title) <= 200):
        return None
    url, jid = _pick(n, _URL), _pick(n, _ID)
    dd = _pick_date(n)
    loc = ", ".join(dict.fromkeys(x for x in (_flat(n[k]) for k in _LOC if k in n) if x))
    if url:
        url = _abs_url(url, base)
        jobish = is_job_path(urlparse(url).path) or re.search(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}", url)
        if not (jobish or loc or dd[0]) or not syntactic_ok(url):
            url = ""
    if not url:
        if not (jid and (loc or dd[0])):
            return None  # menus, facets, articles: not a job
        b = base.split("#")[0]
        url = f"{b}{'&' if '?' in b else '?'}jbid={jid}"
    ats = next((u for u in _strings(o) if u.startswith("http") and detect_from_url(u)), "")  # its job-platform page
    raw = _raw(title, url, location=loc[:200], date_text=dd[3], date=dd[0], date_conf=dd[1], date_source=dd[2],
               apply_url=ats or url,
               description=html_to_text(_pick(n, _DESC))[:15000],
               platform_id=jid, department=_pick(n, _DEPT), employment_type=_pick(n, _TYPE), conf=0.7,
               provenance="network-json")
    return refine_raw_date(raw)  # a 'Date de publication' written in the description beats the JSON field


JOB_LINK = re.compile(r"/(?:nos-|our-|all-|les-|toutes-les-|job-)?(jobs?|careers?|postes-?a-?pourvoir|positions?|opportunit(?:y|ies)|offres?|emplois?|postes?|vacanc\w*|openings?|"
                      r"postings?|annonces?|offer|offers|job-offers?(?:-[a-z]+)*|stellen\w*|stages?|internships?|sujets?|theses?|"
                      r"offres?-(?:d-?)?emplois?)/[^/?#]+|/offre-de-emploi/emploi-[^/?#]+", re.I)
# pages inside job folders that are lists, searches or tools, not offers
NOT_JOB = re.compile(r"(?:^|/)(?:list|liste|list-of-jobs|search|searchjobs|recherche|ma-selection|selection|map|carte|alerts?|"
                     r"login|connexion|index|rss|feed|page|category|categories|tag|tags)(?:[-_.][\w-]*)?(?:\.\w{2,5})?/?$", re.I)


_ID_IN_CAREERS = re.compile(r"/(?:carri[eè]res?|careers?|recrutement|recruitment|jobs?|emplois?)/(?:[^/]+/)*"
                            r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{5,})(?:\.\w{2,5})?/?$", re.I)
_PAGING = re.compile(r"/(?:page|p|seite|pagina)/\d+/?$", re.I)  # '.../page/2': the next page of a list


def is_job_path(path) -> bool:
    if _PAGING.search(path):
        return False
    return (bool(JOB_LINK.search(path)) and not NOT_JOB.search(path)) or bool(_ID_IN_CAREERS.search(path))


def jobs_from_json(obj, base):
    """Find job postings inside any JSON document (Workday, Phenom, SmartRecruiters, Next.js / Nuxt page data, custom APIs)."""
    found = []

    def walk(o, depth):
        if depth > 7:
            return
        if isinstance(o, list):
            dicts = [x for x in o if isinstance(x, dict)]
            if dicts:
                jobs = [j for j in (_as_job(x, base) for x in dicts) if j]
                if jobs and len(jobs) >= 0.6 * len(dicts):
                    found.extend(jobs)
                    return
            for x in o[:200]:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)

    walk(obj, 0)
    uniq = {}
    for j in found:
        uniq.setdefault(normalize_url(j["url"]), j)
    return list(uniq.values())


_JS_JOBS = re.compile(r"(?:window\.|var\s+|let\s+|const\s+)(\w*(?:job|offer|offre|position|vacanc|posting)\w*)\s*=\s*(?=[\[{])",
                      re.I)


def _page_json(soup):
    """JSON documents embedded in the page (Next.js __NEXT_DATA__, application/json state blobs) and job data assigned
    to a script variable ('window.allOffers = [...]')."""
    for sc in soup.find_all("script"):
        typ = (sc.get("type") or "").lower()
        txt = sc.string or sc.get_text() or ""
        if not 20 < len(txt) < 5_000_000:
            continue
        if sc.get("id") == "__NEXT_DATA__" or typ in ("application/json", "application/vnd.api+json"):
            try:
                yield json.loads(txt)
            except ValueError:
                continue
        elif typ in ("", "text/javascript", "application/javascript"):
            for m in _JS_JOBS.finditer(txt):
                try:
                    yield json.JSONDecoder().raw_decode(txt, m.end())[0]
                except ValueError:
                    continue


def embedded_jobs(soup, base):
    out = []
    for doc in _page_json(soup):
        out += jobs_from_json(doc, base)
    return out


def _embedded_date(soup):
    """A job page whose only date sits in its page data (Next.js / Nuxt): accepted when every publication key agrees."""
    seen = {}

    def walk(o, depth):
        if depth > 8 or len(seen) > 6:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kk = _k(k)
                if kk in _DATE and kk not in _DATE_WEAK and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    d, c = parse_date(v)
                    if d is not None and c >= 0.5:
                        seen.setdefault(d.date(), (d, c, kk, str(v)))
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for x in o[:50]:
                walk(x, depth + 1)

    for doc in _page_json(soup):
        walk(doc, 0)
    if len(seen) == 1:
        d, c, k, v = next(iter(seen.values()))
        return d, min(c, 0.7), f"page data {k}", v
    return NO_DATE


# --------------------------------------------------------------------------- a single job page
def _page_title(soup, url):
    """<title> without the site's name: 'Data Engineer | Inetum' -> 'Data Engineer'."""
    t = soup.title.get_text(" ", strip=True) if soup.title else ""
    site = soup.find("meta", property="og:site_name")
    names = {norm(site.get("content") or "")} if site else set()
    names.add(norm((urlparse(url).hostname or "").split(".")[-2] if (urlparse(url).hostname or "").count(".") else ""))
    parts = re.split(r"\s+[|–—-]\s+", t)
    while len(parts) > 1 and (norm(parts[-1]) in names or any(n and n in norm(parts[-1]) for n in names)
                              or re.search(r"career|carri|recrut|jobs?\b|emploi", parts[-1], re.I)):
        parts.pop()
    return " - ".join(parts).strip()


def _parse_detail(html, url, now=None, card=None):
    soup = BeautifulSoup(html, PARSER)
    ld = _jsonld_jobs(soup, url)
    if ld:
        job = ld[0]
        job["url"] = job["apply_url"] = url
    else:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else _page_title(soup, url)
        job = _raw(title, url, description=_content_text(soup, "main", 15000), conf=0.4, provenance="html-fallback")
    if not job["location"]:
        job["location"] = html_location(soup) or location_from_text(job["description"])
    now = now or utcnow()
    cur = _get_date(job)
    if card:  # date seen on the listing card: same rules as any two structured dates
        cur = merge_dates(cur, card)
    if cur[0] is None:  # no structured date: search the HTML itself, then the page data
        h = html_date(soup, now)
        cur = h if h[0] else _embedded_date(soup)
    # structured dates are often a refresh / feed date: an EXPLICIT 'Date de publication : ...' written in the job description
    # (or, less trusted, in the page content) wins over them
    cur = reconcile_date(cur, [("job description", job["description"], True),
                               ("page text", _content_text(soup, "main"), False)], now, _dayfirst(soup))
    job.update(date=cur[0], date_conf=cur[1], date_source=cur[2], date_text=cur[3])
    return job


def merge_detail(base, det):
    """base = what the list / feed gave; det = the parsed job page. Longer text wins; two structured dates: agree -> the
    better one, disagree -> the earliest (refresh dates are later); then the full text may state the real date."""
    if base.get("provenance") == "listing-title":
        out = dict(det)
        out["title"] = det.get("title") or base["title"]
        return out
    out = dict(base)
    if len(det["description"]) > len(out["description"]):
        out["description"] = det["description"]
    for k in ("employer", "location", "department", "employment_type", "country_hint"):
        if not out.get(k) and det.get(k):
            out[k] = det[k]
    if len(re.sub(r"\W", "", out.get("title") or "")) < 3 and det.get("title"):
        out["title"] = det["title"]
    out["date"], out["date_conf"], out["date_source"], out["date_text"] = reconcile_date(
        merge_dates(_get_date(out), _get_date(det)), [("job description", out["description"], True)])
    out["conf"] = max(out["conf"], det["conf"])
    out["provenance"] = base["provenance"] + "+detail"
    return out


def _open_detail(f, cfg, raw, budget, fetch_url=None):
    """Read the job page of a promising job (DATE first, then title) and merge it into the raw job."""
    if not worth_opening(cfg, raw) or not budget.take():
        return raw
    try:
        u = fetch_url or raw["url"]
        return merge_detail(raw, _parse_detail(f.get(u).text, raw["url"], card=_get_date(raw) if raw.get("date") else None))
    except AccessError:
        raise
    except Exception as e:
        f.note(f"job page not read {raw['url'][:90]}: {e}")
        return raw


# --------------------------------------------------------------------------- feeds (RSS / Atom)
def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def feed_items(xml_text):
    """RSS / Atom -> [{tag: [texts]}] (namespaces dropped, nested tags flattened: tt:locations/tt:city -> 'city')."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        if _local(it.tag) not in ("item", "entry"):
            continue
        d = {}
        for el in it.iter():
            if el is it:
                continue
            txt = (el.text or "").strip() or (el.get("href") or "").strip()
            if txt:
                d.setdefault(_local(el.tag), []).append(txt)
        out.append(d)
    return out


_CONTRACT_WORDS = re.compile(r"^(cdi|cdd|stage|internship|alternance|apprentissage|interim|intérim|freelance|temps (?:plein|partiel)|"
                             r"full[- ]time|part[- ]time|permanent|temporary|vie|v\.i\.e|contrat|cadre|non cadre)\b", re.I)


def crawl_feed(f, arg, cfg, known, st):
    """Teamtailor / Talentsoft / any job feed: one request gives every offer with its publication date."""
    feed_url, flavor = arg
    r = f.get(feed_url)
    items, budget, jobs = feed_items(r.text), _Budget(cfg, st), []
    if not items:
        raise ConnectionError(f"feed has no items: {feed_url}")
    for it in items:
        g = lambda k: (it.get(k) or [""])[0]
        title, link = html_to_text(g("title")), g("link") or g("guid")
        if not title or not syntactic_ok(link):
            continue
        desc = html_to_text(g("description") or g("content") or g("summary"))
        cats = it.get("category", [])
        cities, countries = it.get("city", []), it.get("country", [])
        loc = " ; ".join(dict.fromkeys(", ".join(x for x in (c, k) if x) for c, k in zip(cities or [""] * len(countries),
                                                                                         countries or [""] * len(cities)) if c or k))
        places = [c for c in cats if not _CONTRACT_WORDS.match(c) and classify_location(c)["location_verified"]]
        if not loc:
            loc = " ; ".join(places) or location_from_text(desc)
        etype = next((c for c in cats if _CONTRACT_WORDS.match(c)), "")
        other = [c for c in cats if c not in places and c != etype]
        remote = norm(g("remotestatus")) in ("remote", "fully", "fully_remote", "full remote")
        raw = _raw(title, link, location=loc[:200], description=desc[:15000], employment_type=etype,
                   department=g("department") or g("role") or ", ".join(other)[:120], employer=g("company_name"),
                   country_hint=countries[0] if len(set(countries)) == 1 else None, remote_hint=remote or None,
                   conf=0.85, provenance=f"{flavor}-feed",
                   **_date(g("pubdate") or g("published") or g("updated"), f"{flavor} feed pubDate", 0.85))
        if len(desc) < 400 or not raw["location"]:
            raw = _open_detail(f, cfg, raw, budget)
        jobs.append(raw)
    yield Page("feed", feed_url, jobs, total=1, ordered=False)


def crawl_talentsoft(f, arg, cfg, known, st):
    """Talentsoft: the RSS feed holds only the 20 newest offers (exact dates); when even the 20th is still inside the date
    range, the site's own offer list is read page by page for the older ones."""
    feed_url, list_url = arg
    jobs = []
    for pg in crawl_feed(f, (feed_url, "talentsoft"), cfg, known, st):
        jobs += pg.jobs
        yield pg
    gate, oldest = cfg.get("_gate"), min((j["date"] for j in jobs if j.get("date")), default=None)
    if len(jobs) >= 20 and not (gate and oldest and gate(oldest, 0.9, "") is False):
        f.note("talentsoft: the feed is full, reading the offer list for older offers")
        from .crawler import crawl_generic  # imported late: crawler imports this module
        yield from crawl_generic(f, list_url, cfg, set(known) | {normalize_url(j["url"]) for j in jobs}, st)


# --------------------------------------------------------------------------- official job-board APIs
def crawl_greenhouse(f, token, cfg, known, st):
    for tok in token.split(","):  # 'mirakl,mirakllabs': a company with several boards
        yield from _greenhouse_board(f, tok, cfg, known, st)


def _greenhouse_board(f, token, cfg, known, st):
    r = f.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    jobs = []
    for j in r.json().get("jobs", []):
        fp = j.get("first_published")
        jobs.append(_raw(j.get("title", ""), j.get("absolute_url", ""), employer=token,
                         location=(j.get("location") or {}).get("name", ""),
                         description=html_to_text(html_to_text(j.get("content", ""))),
                         platform_id=str(j.get("id", "")), conf=0.9, provenance="greenhouse-api",
                         department=", ".join(x.get("name", "") for x in j.get("departments", []) or []),
                         **(_date(fp, "greenhouse first_published") if fp else
                            dict(date=None, date_conf=0.0, date_source="", date_text=f"updated_at={j.get('updated_at', '')}"))))
    yield Page("api", r.url, jobs, total=1, ordered=False)


def crawl_lever(f, company, cfg, known, st):
    skip, limit = 0, 100
    for _ in range(cfg["max_pages"]):
        r = f.get(f"https://api.lever.co/v0/postings/{company}?mode=json&skip={skip}&limit={limit}")
        data = r.json()
        jobs = []
        for j in data:
            cat = j.get("categories", {}) or {}
            ms = j.get("createdAt")
            d = datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None) if ms else None
            jobs.append(_raw(j.get("text", ""), j.get("hostedUrl", ""), employer=company,
                             location=" ; ".join(cat.get("allLocations") or [cat.get("location", "")]),
                             description=j.get("descriptionPlain", "") + " " + (j.get("additionalPlain") or ""),
                             date=d, date_text=str(ms or ""), date_source="lever createdAt", date_conf=0.85 if d else 0.0,
                             apply_url=j.get("applyUrl", ""), platform_id=j.get("id", ""),
                             employment_type=cat.get("commitment", ""),
                             remote_hint=(j.get("workplaceType") == "remote") or None, country_hint=j.get("country"),
                             department=cat.get("team", ""), conf=0.9, provenance="lever-api"))
        yield Page(f"skip={skip}", r.url, jobs, ordered=False)
        if len(data) < limit:
            return
        skip += limit
    st.truncated = True


def crawl_ashby(f, name, cfg, known, st):
    r = f.get(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    jobs = []
    for j in r.json().get("jobs", []):
        addr = (j.get("address") or {}).get("postalAddress", {}) or {}
        jobs.append(_raw(j.get("title", ""), j.get("jobUrl", ""), employer=name, location=j.get("location", ""),
                         description=j.get("descriptionPlain", ""), apply_url=j.get("applyUrl", ""),
                         remote_hint=j.get("isRemote") or None, country_hint=addr.get("addressCountry"),
                         department=j.get("department", ""), conf=0.9, provenance="ashby-api",
                         employment_type=j.get("employmentType", ""), **_date(j.get("publishedAt"), "ashby publishedAt")))
    yield Page("api", r.url, jobs, total=1, ordered=False)


def crawl_recruitee(f, origin, cfg, known, st):
    r = f.get(f"{origin}/api/offers/")
    jobs = []
    for j in r.json().get("offers", []):
        jobs.append(_raw(j.get("title", ""), j.get("careers_url") or f"{origin}/o/{j.get('slug', '')}",
                         employer=j.get("company_name", ""),
                         location=j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x),
                         description=html_to_text((j.get("description") or "") + " " + (j.get("requirements") or "")),
                         country_hint=j.get("country_code"), remote_hint=j.get("remote") or None,
                         department=j.get("department") or "", employment_type=j.get("employment_type_code") or "",
                         platform_id=str(j.get("id", "")), conf=0.9, provenance="recruitee-api",
                         **_date(j.get("published_at"), "recruitee published_at")))
    yield Page("api", r.url, jobs, total=1, ordered=False)


def crawl_breezy(f, origin, cfg, known, st):
    r = f.get(f"{origin}/json")
    budget, jobs = _Budget(cfg, st), []
    for j in r.json():
        loc = j.get("location") or {}
        country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")
        raw = _raw(j.get("name", ""), j.get("url", ""), employer=(j.get("company") or {}).get("name", ""),
                   location=loc.get("name") or ", ".join(x for x in (loc.get("city"), country) if x),
                   country_hint=country, remote_hint=loc.get("is_remote") or None, department=j.get("department") or "",
                   employment_type=(j.get("type") or {}).get("name", ""), platform_id=str(j.get("id", "")), conf=0.85,
                   provenance="breezy-api", **_date(j.get("published_date"), "breezy published_date"))
        jobs.append(_open_detail(f, cfg, raw, budget))
    yield Page("api", r.url, jobs, total=1, ordered=False)


def _smartrecruiters_pages(f, company, cfg, known, st):
    """careers.smartrecruiters.com/<company>: 24 offers per page; the job pages carry itemprop datePosted."""
    budget, seen, older = _Budget(cfg, st), set(), False
    for n in range(cfg["max_pages"]):
        r = f.get(f"https://careers.smartrecruiters.com/{company}/api/more?page={n}")
        soup, jobs = BeautifulSoup(r.text, PARSER), []
        for li in soup.select("li.opening-job"):
            a = li.select_one("a[href]")
            if not a or not syntactic_ok(a["href"]) or normalize_url(a["href"]) in seen:
                continue
            u = normalize_url(a["href"])
            seen.add(u)
            h = li.select_one("h4")
            raw = _raw((h or a).get_text(" ", strip=True), u, employer=company, conf=0.7, provenance="smartrecruiters-page")
            if older:
                continue  # strictly newest first: every next offer is older too
            raw = _open_detail(f, cfg, raw, budget) if u not in known else raw
            jobs.append(raw)
            older = bool(raw.get("date")) and raw_verdict(cfg, raw) is False
        yield Page(f"page={n}", r.url, jobs, sorted=True)  # checked on six tenants: no out-of-order offer
        if not jobs:
            return


def crawl_smartrecruiters(f, company, cfg, known, st):
    api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    if not f.robots_ok(api):  # the API is reserved to some bots by robots.txt: read the public career page instead
        f.note("smartrecruiters API closed by robots.txt: reading the public career page")
        yield from _smartrecruiters_pages(f, company, cfg, known, st)
        return
    country = "&country=fr" if _wants_france(cfg) else ""
    budget, offset = _Budget(cfg, st), 0
    while True:
        r = f.get(f"{api}?limit=100&offset={offset}{country}")
        data = r.json()
        content, jobs = data.get("content") or [], []
        for p in content:
            loc = p.get("location") or {}
            url = f"https://jobs.smartrecruiters.com/{company}/{p.get('id', '')}"
            raw = _raw(p.get("name", ""), url, employer=(p.get("company") or {}).get("name") or company,
                       location=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                       country_hint=loc.get("country"), remote_hint=loc.get("remote") or None,
                       department=(p.get("department") or {}).get("label", ""),
                       employment_type=(p.get("typeOfEmployment") or {}).get("label", ""),
                       platform_id=str(p.get("refNumber") or p.get("id", "")), conf=0.9, provenance="smartrecruiters-api",
                       **_date(p.get("releasedDate"), "smartrecruiters releasedDate"))
            if worth_opening(cfg, raw) and budget.take():
                try:
                    det = f.get(f"{api}/{p['id']}").json()
                    sec = ((det.get("jobAd") or {}).get("sections") or {})
                    raw["description"] = html_to_text(" ".join((sec.get(k) or {}).get("text", "") for k in (
                        "jobDescription", "qualifications", "additionalInformation", "companyDescription")))[:15000]
                    raw["url"] = det.get("postingUrl") or url
                    raw["apply_url"] = det.get("applyUrl") or raw["url"]
                    raw = refine_raw_date(raw)
                except AccessError:
                    raise
                except Exception as e:
                    f.note(f"smartrecruiters detail failed {p.get('id')}: {e}")
            jobs.append(raw)
        total = data.get("totalFound") or 0
        yield Page(f"offset={offset}", r.url, jobs, total=-(-total // 100) if total else None, ordered=False)
        offset += 100
        if not content or offset >= total or offset >= cfg["max_pages"] * 100:
            return


_WD_LOCALE = re.compile(r"[a-z]{2}(-[A-Za-z]{2})?")


def _workday_arg(url):
    p = urlparse(url)
    host, seg = (p.hostname or "").lower(), [x for x in p.path.split("/") if x]
    if re.fullmatch(r"[\w-]+\.wd\d+\.myworkdayjobs\.com", host):
        locale = seg.pop(0) if seg and _WD_LOCALE.fullmatch(seg[0]) else ""
        if seg and seg[0] != "wday":
            facets = {}
            for k, v in parse_qsl(p.query):  # '?locations=f4ab...&locations=5885...': the website's own filters
                if re.fullmatch(r"[A-Za-z_]+", k) and re.fullmatch(r"[0-9a-f]{32}", v):
                    facets.setdefault(k, []).append(v)
            return dict(host=host, tenant=host.split(".")[0], site=seg[0], facets=facets,
                        prefix=f"/{locale + '/' if locale else ''}{seg[0]}")
    if re.fullmatch(r"wd\d+\.myworkdaysite\.com", host) and len(seg) >= 3 and seg[0] == "recruiting":
        return dict(host=host, tenant=seg[1], site=seg[2], prefix=f"/recruiting/{seg[1]}/{seg[2]}")
    return None


def _workday_france(facets):
    """The facet that filters on the country France (its parameter name differs between Workday tenants)."""
    stack = [(f.get("facetParameter"), f) for f in facets or []]
    while stack:
        param, node = stack.pop()
        for v in node.get("values") or []:
            if "values" in v:
                stack.append((v.get("facetParameter") or param, v))
            elif norm(v.get("descriptor") or "") == "france" and "country" in norm(param or ""):
                return {param: [v["id"]]}
    return {}


def crawl_workday(f, a, cfg, known, st):
    api = f"https://{a['host']}/wday/cxs/{a['tenant']}/{a['site']}"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    first = f.post_json(api + "/jobs", {**body, "limit": 1}).json()
    if a.get("facets"):
        body["appliedFacets"] = a["facets"]
        f.note("workday: the filters written in the URL are applied")
    elif _wants_france(cfg):
        body["appliedFacets"] = _workday_france(first.get("facets"))
        if body["appliedFacets"]:
            f.note("workday: France filter of the website applied")
    budget, total, offset = _Budget(cfg, st), None, 0
    while offset < cfg["max_pages"] * 20:
        data = f.post_json(api + "/jobs", {**body, "offset": offset}).json()
        total = total if total is not None else (data.get("total") or 0)  # only the first answer carries the total
        posts, jobs = data.get("jobPostings") or [], []
        for p in posts:
            path = p.get("externalPath") or ""
            raw = _raw(p.get("title", ""), f"https://{a['host']}{a['prefix']}{path}", location=p.get("locationsText", ""),
                       platform_id=(p.get("bulletFields") or [""])[0], conf=0.8, provenance="workday-api",
                       country_hint="France" if body["appliedFacets"] else None,
                       **_date(p.get("postedOn"), "workday list 'posted'", 0.75))
            if path and normalize_url(raw["url"]) not in known and worth_opening(cfg, raw) and budget.take():
                try:
                    i = (f.get(api + path, headers={"Accept": "application/json"}).json() or {}).get("jobPostingInfo") or {}
                    country = ((i.get("country") or {}).get("descriptor")
                               or (((i.get("jobRequisitionLocation") or {}).get("country")) or {}).get("descriptor"))
                    raw.update(description=html_to_text(i.get("jobDescription", ""))[:15000],
                               location=i.get("location") or raw["location"], country_hint=country or raw["country_hint"],
                               employment_type=i.get("timeType", ""), apply_url=i.get("externalUrl") or raw["url"],
                               platform_id=i.get("jobReqId") or raw["platform_id"], conf=0.9, provenance="workday-api+detail")
                    if i.get("startDate"):  # the posting date: exact, unlike 'Posted 30+ Days Ago'
                        d = _date(i["startDate"], "workday posting startDate", 0.9)
                        raw["date"], raw["date_conf"], raw["date_source"], raw["date_text"] = merge_dates(
                            _get_date(raw), (d["date"], d["date_conf"], d["date_source"], d["date_text"]))
                    raw = refine_raw_date(raw)
                except AccessError:
                    raise
                except Exception as e:
                    f.note(f"workday detail failed {path}: {e}")
            jobs.append(raw)
        # newest first, but NOT strictly: some tenants pin a block of offers and restart the list after it (Accenture's page 2
        # starts newer than page 1 ends), so only whole pages without any offer in the range end the list
        yield Page(f"offset={offset}", api, jobs, total=-(-total // 20) if total else None)
        offset += 20
        if not posts or offset >= total:
            return
    st.truncated = True


def _rmk_list(url):
    """SAP SuccessFactors career site: the job table sorted newest first. Category pages (/go/<name>/<id>/) page by path."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    q = {k: v for k, v in parse_qsl(p.query) if k not in ("startrow", "sortColumn", "sortDirection")}
    q.setdefault("q", "")
    q.update(sortColumn="referencedate", sortDirection="desc")
    m = re.search(r"(/(?:[^/]+/)?go/[^/]+/\d+)", p.path)
    if m:
        return lambda n: f"{origin}{m.group(1)}/{f'{n}/' if n else ''}?{urlencode(q)}"
    return lambda n: f"{origin}/search/?{urlencode({**q, 'startrow': n})}"


def _rmk_france(f, url, cfg):
    """The site's own country filter (a 'optionsFacetsDD_country' list offering France), when it has one and the URL
    does not already choose a country."""
    if not _wants_france(cfg) or "optionsFacetsDD_country" in url or "/go/" in urlparse(url).path:
        return url
    try:
        soup = BeautifulSoup(f.get(_rmk_list(url)(0)).text, PARSER)
    except AccessError:
        raise
    except Exception:
        return url
    sel = soup.find("select", attrs={"name": "optionsFacetsDD_country"}) or soup.find("select", id=re.compile("country", re.I))
    opt = next((o for o in (sel.find_all("option") if sel else []) if norm(o.get_text(" ", strip=True)).startswith("france")
                or (o.get("value") or "").upper() == "FR"), None)
    if not opt or not opt.get("value"):
        return url
    f.note("successfactors: the website's country filter 'France' is applied")
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "optionsFacetsDD_country"] + [("optionsFacetsDD_country", opt["value"])]
    return urlunparse(p._replace(path=p.path if "search" in p.path else "/search/", query=urlencode(q)))


def crawl_successfactors(f, url, cfg, known, st):
    url = _rmk_france(f, url, cfg)
    page_url, budget, n, seen = _rmk_list(url), _Budget(cfg, st), 0, set()
    for _ in range(cfg["max_pages"]):
        r = f.get(page_url(n))
        soup = BeautifulSoup(r.text, PARSER)
        df = _dayfirst(soup)
        rows = soup.select("tr.data-row") or soup.select("li.job-tile, div.job-tile")
        jobs = []
        for row in rows:
            a = row.select_one("a.jobTitle-link") or row.select_one("a[href*='/job/']")
            if not a or not a.get("href"):
                continue
            u = normalize_url(urljoin(r.url, a["href"]))
            if u in seen:
                continue
            seen.add(u)
            loc = row.select_one(".jobLocation, [class*=location]")
            dt = row.select_one(".jobDate, [class*=date]")
            raw = _raw(a.get_text(" ", strip=True), u, location=loc.get_text(" ", strip=True) if loc else "", conf=0.8,
                       provenance="successfactors", **_date(dt.get_text(" ", strip=True) if dt else "",
                                                            "successfactors list date", 0.75, df))
            jobs.append(_open_detail(f, cfg, raw, budget) if u not in known else raw)
        lab = soup.select_one(".paginationLabel")
        nums = [int(x) for x in re.findall(r"\d+", lab.get_text(" ", strip=True))] if lab else []
        total = nums[-1] if nums else None
        yield Page(f"startrow={n}", r.url, jobs, total=-(-total // max(len(rows), 1)) if total else None, sorted=True)
        n += len(rows)
        if not jobs or (total is not None and n >= total):
            return
    st.truncated = True


_ICIMS_SORT = "$T{Job}.$T{JobPost}.$F{PostedDateTime}"  # the site's own 'sort by posting date' column


def _icims_list(url, order=None):
    p = urlparse(url)
    keep = {k: v for k, v in parse_qsl(p.query) if k.lower().startswith("search")}  # the site's own filters only
    sort = {"schemaId": _ICIMS_SORT, "o": order} if order else {}
    return lambda n: f"{p.scheme}://{p.netloc}/jobs/search?{urlencode({'ss': 1, **keep, **sort, 'in_iframe': 1, 'pr': n})}"


def _strip_param(u, name):
    p = urlparse(u)
    return urlunparse(p._replace(query=urlencode([(k, v) for k, v in parse_qsl(p.query) if k != name])))


_ICIMS_DATE = re.compile(r"\((\d{1,2})/(\d{1,2})/(\d{4})")


def _icims_dayfirst(html, known=None):
    """iCIMS writes '(10/23/2026 ...)' on US sites and '(28/10/2026)' on French ones: a number above 12 tells which."""
    pairs = [(int(a), int(b)) for a, b, _ in _ICIMS_DATE.findall(html)]
    if any(a > 12 for a, b in pairs):
        return True
    if any(b > 12 for a, b in pairs) or re.search(r"\(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M", html):
        return False  # '(10/06/2026 10:17 AM)': a 12-hour clock is the US format, month first
    return known  # still ambiguous: dates like 06/10/2026 are kept only when both readings agree


def _icims_sorted(f, url, cfg):
    """The site's 'posting date' sort, newest first. Its direction letter differs between sites: the first page tells."""
    for order in ("A", "D"):
        try:
            html = f.get(_icims_list(url, order)(0)).text
        except AccessError:
            raise
        except Exception:
            return None, None
        df = _icims_dayfirst(html)
        ds = [d for d in (parse_date("/".join(m), dayfirst=df)[0] for m in _ICIMS_DATE.findall(html)) if d]
        if len(ds) >= 2 and ds[0] >= ds[-1]:
            f.note("icims: the website's sort by posting date is used (newest first)")
            return order, df
    return None, None


def crawl_icims(f, url, cfg, known, st):
    order, dayfirst = _icims_sorted(f, url, cfg)
    page_url, budget, seen = _icims_list(url, order), _Budget(cfg, st), set()
    for n in range(cfg["max_pages"]):
        r = f.get(page_url(n))
        dayfirst = _icims_dayfirst(r.text, dayfirst)
        soup = BeautifulSoup(r.text, PARSER)
        rows, jobs = soup.select(".iCIMS_JobsTable .row"), []
        for row in rows:
            a = row.select_one(".title a") or row.select_one("a[href*='/jobs/']")
            if not a or not a.get("href"):
                continue
            fetch = urljoin(r.url, a["href"])
            u = normalize_url(_strip_param(fetch, "in_iframe"))
            if u in seen:
                continue
            seen.add(u)
            title = re.sub(r"^(titre|title|job title|titel|poste)\s+", "", (a.find(["h2", "h3"]) or a).get_text(" ", strip=True), flags=re.I)
            fields = {}
            for tag in row.select(".iCIMS_JobHeaderTag"):
                k, v = tag.select_one(".iCIMS_JobHeaderField"), tag.select_one(".iCIMS_JobHeaderData")
                if k and v:
                    fields[norm(k.get_text(" ", strip=True))] = v.get_text(" ", strip=True)
            pick = lambda rx: next((v for k, v in fields.items() if re.search(rx, k)), "")
            hdr = (row.select_one(".header.right") or row).get_text(" ", strip=True)
            m = re.search(r"\((\d{1,2}/\d{1,2}/\d{4})", hdr)
            d = _date(m[1], "icims posted date", 0.85, dayfirst) if m else _date(hdr, "icims posted date", 0.7)
            desc = row.select_one(".description")
            loc = pick(r"location|lieu|localisation|site|ville") or location_from_text(row.get_text(" | ", strip=True))
            raw = _raw(title, u, location=loc,  # 'Lieu de travail | FR-Bernin' sits in the row header on some sites
                       employment_type=pick(r"type|contrat|contract"), department=pick(r"categor|metier|department|famille"),
                       description=desc.get_text(" ", strip=True) if desc else "", conf=0.8, provenance="icims", **d)
            jobs.append(_open_detail(f, cfg, raw, budget, fetch) if u not in known else raw)
        yield Page(f"pr={n}", r.url, jobs, ordered=order is not None, sorted=order is not None)  # unsorted: read to the end
        if not jobs:
            return
    st.truncated = True


# --------------------------------------------------------------------------- Phenom, Eightfold, Jibe, Workable
def _phenom_arg(url, html):
    """Phenom career sites (careers.thalesgroup.com, joining.bouygues.com): the page data names the site's locale."""
    m = re.search(r'phApp\.ddo\s*=|"refNum"\s*:\s*"[A-Z0-9]+"', html)
    if not m:
        return None
    p = urlparse(url)
    seg = [x for x in p.path.split("/") if x]
    prefix = "/" + "/".join(seg[:2]) if len(seg) >= 2 and re.fullmatch(r"[a-z]{2}", seg[1]) else ""
    loc = re.search(r'"locale"\s*:\s*"([\w-]+)"', html)
    return dict(origin=_origin(url), prefix=prefix, locale=loc[1] if loc else "en_global",
                keywords=next((v for k, v in parse_qsl(p.query) if k in ("keywords", "keyword", "q")), ""))


def crawl_phenom(f, a, cfg, known, st):
    """POST /widgets (refineSearch): 100 offers per request, France facet; the list cannot be sorted by date, so every
    page is read and filtered on 'postedDate'. Job pages carry JSON-LD."""
    size, budget, offset, total = 100, _Budget(cfg, st), 0, None
    body = {"lang": a["locale"], "deviceType": "desktop", "country": "global", "pageName": "search-results",
            "ddoKey": "refineSearch", "jobs": True, "counts": False, "all_fields": [], "size": size, "clearAll": False,
            "jdsource": "facets", "isSliderEnable": False, "siteType": "external", "keywords": a["keywords"], "global": True,
            "selected_fields": {"country": ["France"]} if _wants_france(cfg) else {}, "locationData": {}}
    for _ in range(cfg["max_pages"]):
        res = (f.post_json(a["origin"] + "/widgets", {**body, "from": offset}).json() or {}).get("refineSearch") or {}
        if total is None and not res.get("totalHits") and body["selected_fields"]:
            body["selected_fields"] = {}  # this site names its country facet differently (orange.jobs: France only)
            f.note("phenom: no 'France' country facet on this site, reading the whole list")
            res = (f.post_json(a["origin"] + "/widgets", {**body, "from": offset}).json() or {}).get("refineSearch") or {}
        total = total if total is not None else (res.get("totalHits") or 0)
        posts, jobs = (res.get("data") or {}).get("jobs") or [], []
        for p in posts:
            u = f"{a['origin']}{a['prefix']}/job/{p.get('jobId') or p.get('reqId')}"
            raw = _raw(p.get("title", ""), u, location=p.get("location") or p.get("cityStateCountry", ""),
                       country_hint=p.get("country"), description=p.get("descriptionTeaser", ""),
                       employment_type=p.get("type", ""), department=p.get("category", ""),
                       platform_id=p.get("reqId", ""), conf=0.85, provenance="phenom-api",
                       **_date(p.get("postedDate") or p.get("dateCreated"), "phenom api postedDate"))
            jobs.append(_open_detail(f, cfg, raw, budget) if normalize_url(u) not in known else raw)
        yield Page(f"from={offset}", a["origin"] + "/widgets", jobs, total=-(-total // size) if total else None, ordered=False)
        offset += size
        if not posts or offset >= total:
            return
    st.truncated = True


def crawl_eightfold(f, origin, cfg, known, st):
    """*.eightfold.ai: /api/apply/v2/jobs (10 per page, France location); 't_create' is the posting date, the detail
    call gives the description."""
    budget, start, num = _Budget(cfg, st), 0, 10
    loc = "&location=France" if _wants_france(cfg) else ""
    for _ in range(cfg["max_pages"]):
        r = f.get(f"{origin}/api/apply/v2/jobs?start={start}&num={num}&sort_by=timestamp{loc}")
        data = r.json()
        posts, jobs = data.get("positions") or [], []
        for p in posts:
            ts = p.get("t_create")
            d = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None) if ts else None
            u = p.get("canonicalPositionUrl") or f"{origin}/careers/job/{p.get('id')}"
            raw = _raw(p.get("name", ""), u, location=" ; ".join(p.get("locations") or [p.get("location", "")]),
                       department=p.get("department", ""), platform_id=str(p.get("display_job_id") or p.get("id", "")),
                       remote_hint=(p.get("work_location_option") == "remote") or None, conf=0.85,
                       provenance="eightfold-api", date=d, date_conf=0.85 if d else 0.0,
                       date_source="eightfold api t_create" if d else "", date_text=str(ts or ""))
            if normalize_url(u) not in known and worth_opening(cfg, raw) and budget.take():
                try:
                    det = f.get(f"{origin}/api/apply/v2/jobs/{p.get('id')}").json()
                    raw.update(description=html_to_text(det.get("job_description", ""))[:15000], conf=0.9)
                except AccessError:
                    raise
                except Exception as e:
                    f.note(f"eightfold detail failed {p.get('id')}: {e}")
            jobs.append(raw)
        yield Page(f"start={start}", r.url, jobs, total=-(-(data.get("count") or 0) // num) or None, ordered=False)
        start += num
        if len(posts) < num or start >= (data.get("count") or 0):
            return
    st.truncated = True


def _jibe_arg(url, html):
    """Jibe career sites (careers.se.com): the page loads /api/jobs; the site's own filters (location, keywords) are kept."""
    if not re.search(r"jibecdn|jibe-|\bjibe\b|/api/jobs\?", html, re.I):
        return None
    q = {k: v for k, v in parse_qsl(urlparse(url).query) if k not in ("page", "sortBy", "descending", "limit")}
    return dict(origin=_origin(url), query=q)


def crawl_jibe(f, a, cfg, known, st):
    """/api/jobs sorted by posted_date, newest first, 100 per page; the list gives the full description."""
    q = {**a["query"], "sortBy": "posted_date", "descending": "true", "limit": 100}
    if _wants_france(cfg) and not q.get("location"):
        q["location"] = "France"
    for n in range(1, cfg["max_pages"] + 1):
        r = f.get(f"{a['origin']}/api/jobs?{urlencode({**q, 'page': n})}")
        data = r.json()
        posts, jobs = data.get("jobs") or [], []
        for x in posts:
            p = x.get("data") or {}
            u = f"{a['origin']}/jobs/{p.get('slug') or p.get('req_id')}?lang={p.get('language', 'en-us')}"
            jobs.append(_raw(p.get("title", ""), u, employer=p.get("hiring_organization", ""),
                             location=p.get("full_location") or p.get("location_name", ""), country_hint=p.get("country"),
                             description=html_to_text(" ".join(p.get(k) or "" for k in ("description", "qualifications",
                                                                                          "responsibilities")))[:15000],
                             employment_type=" ".join(p.get("tags1") or []), apply_url=p.get("apply_url") or u,
                             department=", ".join(c.get("name", "") for c in p.get("categories") or []),
                             platform_id=p.get("req_id", ""), conf=0.9, provenance="jibe-api",
                             **_date(p.get("posted_date") or p.get("create_date"), "jibe api posted_date")))
        yield Page(f"page={n}", r.url, jobs, total=-(-(data.get("totalCount") or 0) // 100) or None, sorted=True)
        if len(posts) < 100:
            return
    st.truncated = True


def crawl_workable(f, account, cfg, known, st):
    """apply.workable.com/<account>: v3 job list (token paging, 'published' date) + v2 detail for the description."""
    api, budget, token = f"https://apply.workable.com/api/v3/accounts/{account}/jobs", _Budget(cfg, st), None
    where = [{"country": "France", "countryCode": "FR"}] if _wants_france(cfg) else []  # the site's location filter
    for n in range(cfg["max_pages"]):
        data = f.post_json(api, {"query": "", "location": where, "department": [], "worktype": [], "remote": [],
                                 **({"token": token} if token else {})}).json()
        jobs = []
        for p in data.get("results") or []:
            loc = p.get("location") or {}
            u = f"https://apply.workable.com/{account}/j/{p.get('shortcode')}/"
            raw = _raw(p.get("title", ""), u, employer=account, country_hint=loc.get("country"),
                       location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                       remote_hint=p.get("remote") or None, department=", ".join(p.get("department") or []),
                       employment_type=p.get("type", ""), platform_id=p.get("shortcode", ""), conf=0.85,
                       provenance="workable-api", **_date(p.get("published"), "workable api published"))
            if normalize_url(u) not in known and worth_opening(cfg, raw) and budget.take():
                try:
                    det = f.get(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{p.get('shortcode')}").json()
                    raw.update(description=html_to_text(" ".join(det.get(k) or "" for k in
                                                                 ("description", "requirements", "benefits")))[:15000], conf=0.9)
                except AccessError:
                    raise
                except Exception as e:
                    f.note(f"workable detail failed {p.get('shortcode')}: {e}")
            jobs.append(raw)
        yield Page(f"page={n}", api, jobs, sorted=True)  # Workable lists the newest first
        token = data.get("nextPage")
        if not token or not jobs:
            return
    st.truncated = True


def crawl_talentview(f, company, cfg, known, st):
    """<company>.talentview.io (Parrot): the public campaign list, 10 per page, newest 'last_activation_at' first. The job
    pages need JavaScript, so title, place, contract and date come from the list."""
    api = f"https://api.talentview.io/funnel/v2/companies/{company}"
    sites = f.get(f"{api}/websites?website_type=public").json() or []
    site = next((x for x in sites if x.get("locale") == "fr"), sites[0] if sites else {})
    for n in range(1, cfg["max_pages"] + 1):
        r = f.get(f"{api}/campaigns?company_website_id={site.get('id', '')}&display_mode=list&offset_start={n}")
        posts, jobs = r.json() or [], []
        for p in posts:
            ad = p.get("address") or {}
            jobs.append(_raw(p.get("name", ""), f"https://{company}.talentview.io/jobs/{p.get('slug')}",
                             employer=(p.get("entity") or {}).get("name", ""), location=ad.get("name") or ad.get("city", ""),
                             country_hint=ad.get("country"), employment_type=p.get("job_type", ""),
                             platform_id=str(p.get("id", "")), conf=0.8, provenance="talentview-api",
                             **_date(p.get("last_activation_at"), "talentview api last_activation_at", 0.8)))
        yield Page(f"page={n}", r.url, jobs, sorted=True)  # newest 'last_activation_at' first
        if len(posts) < 10:
            return
    st.truncated = True


_WP_SKIP = {"post", "page", "attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part", "wp_navigation",
            "wp_font_family", "wp_font_face", "wp_global_styles", "elementor_library", "product"}
_WP_JOBTYPE = re.compile(r"job|offre|offer|emploi|vacanc|poste|position|career|carri|recrut|annonce|opening", re.I)


def wordpress_types(f, root):
    """Job post types exposed by a WordPress site's REST API -> [rest_base]."""
    try:
        types = f.get(root + "wp/v2/types").json()
    except Exception:
        return []
    out = []
    for slug, t in (types.items() if isinstance(types, dict) else []):
        rb = (t or {}).get("rest_base")
        if slug not in _WP_SKIP and rb and _WP_JOBTYPE.search(f"{slug} {t.get('name', '')} {rb}"):
            out.append(rb)
    return out


def crawl_wordpress(f, arg, cfg, known, st):
    root, bases = arg
    budget = _Budget(cfg, st)
    for rb in bases:
        for page in range(1, cfg["max_pages"] + 1):
            r = f.get(f"{root}wp/v2/{rb}?per_page=50&page={page}&orderby=date&order=desc"
                      "&_fields=id,date,date_gmt,link,title,content,excerpt")
            items, jobs = r.json(), []
            if not isinstance(items, list):
                break
            for it in items:
                link = it.get("link") or ""
                if not syntactic_ok(link):
                    continue
                desc = html_to_text(((it.get("content") or {}).get("rendered") or "") + " " +
                                    ((it.get("excerpt") or {}).get("rendered") or ""))
                dt = it.get("date_gmt") and it["date_gmt"] + "Z"
                raw = _raw(html_to_text((it.get("title") or {}).get("rendered", "")), link, description=desc[:15000],
                           location=location_from_text(desc), platform_id=str(it.get("id", "")), conf=0.8,
                           provenance="wordpress-api", **_date(dt or it.get("date"), "wordpress publication date", 0.85))
                raw = refine_raw_date(raw)
                if len(desc) < 400 or not raw["location"] or len(raw["title"]) < 3:
                    raw = _open_detail(f, cfg, raw, budget)
                jobs.append(raw)
            pages = int(r.headers.get("X-WP-TotalPages") or 1)
            yield Page(f"{rb}:{page}", r.url, jobs, total=pages, ordered=False)
            if page >= pages or not items:
                break
            if jobs and jobs[-1].get("date") and raw_verdict(cfg, jobs[-1]) is False:
                break  # orderby=date&order=desc: older offers only from here on (next post type, if any)


# --------------------------------------------------------------------------- platform detection
_SR_SKIP = {"ni", "oneclick-ui", "embed", "static", "company", "v1", "companies"}
_VENDOR_SUB = {"www", "tt", "app", "cdn", "go", "info", "assets", "status", "support", "help", "blog", "api", "career", "careers"}


def _smartrecruiters_ids(text):
    ids = re.findall(r"(?:careers|jobs)\.smartrecruiters\.com/(?:ni/)?([A-Za-z][\w-]{1,60})", text)
    ids += re.findall(r"smartrecruiters\.com/oneclick-ui/company/([A-Za-z][\w-]{1,60})/", text)
    ids += re.findall(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z][\w-]{1,60})", text)
    return Counter(i for i in ids if i.lower() not in _SR_SKIP)


def _origin(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"


def detect_from_url(url):
    p = urlparse(url)
    host, seg = (p.hostname or "").lower(), [x for x in p.path.split("/") if x]
    if host.endswith("greenhouse.io"):
        tok = dict(parse_qsl(p.query)).get("for") or (seg[0] if seg and seg[0] not in ("embed", "v1") else "")
        if tok:
            return "greenhouse", tok
    if host == "jobs.lever.co" and seg:
        return "lever", seg[0]
    if host == "jobs.ashbyhq.com" and seg:
        return "ashby", seg[0]
    wd = _workday_arg(url)
    if wd:
        return "workday", wd
    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com"):
        ids = _smartrecruiters_ids(url)
        if ids:
            return "smartrecruiters", ids.most_common(1)[0][0]
    if host.endswith(".breezy.hr") and host.split(".")[0] not in _VENDOR_SUB:
        return "breezy", _origin(url)
    if host.endswith(".recruitee.com") and host.split(".")[0] not in _VENDOR_SUB:
        return "recruitee", _origin(url)
    if host.endswith(".teamtailor.com") and host.split(".")[0] not in _VENDOR_SUB:
        return "teamtailor", (_origin(url) + "/jobs.rss", "teamtailor")
    if host.endswith(".eightfold.ai") and host.split(".")[0] not in _VENDOR_SUB:
        return "eightfold", _origin(url)
    if host.endswith(".talentview.io") and host.split(".")[0] not in _VENDOR_SUB:
        return "talentview", host.split(".")[0]
    if host == "apply.workable.com" and seg and seg[0] not in ("api", "j"):
        return "workable", seg[0]
    if host.endswith(".icims.com"):
        return "icims", url
    return None


_ICIMS_SKIP = {"www", "cdn", "cdn01", "cdn02", "images", "cookie-policy-scripts", "static", "login", "api", "community"}
_FEED_TITLE = re.compile(r"job|offre|emploi|career|carri|recrut|poste|vacanc|opening|stellen", re.I)


def detect_from_page(f, url, html):
    """The platform behind a company's own career page: its links, embeds, feeds and markers."""
    soup = BeautifulSoup(html, PARSER)
    low = html.lower()
    for name, arg in (("jibe", _jibe_arg(url, html)), ("phenom", _phenom_arg(url, html))):
        if arg:  # before iCIMS / Workday: these sites link their apply forms there
            return name, arg
    if "jobs2web" in low or ("j2w" in low and "rmk" in low) or soup.select_one("tr.data-row a.jobTitle-link"):
        return "successfactors", url
    rmk = next((urljoin(url, a["href"]) for a in soup.find_all("a", href=True) if "optionsFacetsDD_" in a["href"]), None)
    if rmk and syntactic_ok(rmk):  # a link to the company's SuccessFactors job search (Eviden -> jobs.atos.net)
        return "successfactors", rmk
    for a in soup.find_all(["a", "iframe", "script"], href=True) + soup.find_all(["iframe", "script"], src=True):
        found = detect_from_url(urljoin(url, a.get("href") or a.get("src")))
        if found and found[0] in ("greenhouse", "lever", "ashby", "workday", "breezy", "recruitee", "teamtailor",
                                  "talentview", "workable", "eightfold"):
            return found
    gh = list(dict.fromkeys(re.findall(r"(?:boards-api\.greenhouse\.io/v1/boards|(?:job-)?boards\.greenhouse\.io)/([\w-]+)", html)))
    gh = [t for t in gh if t not in ("embed", "v1")]
    if gh:
        return "greenhouse", ",".join(gh[:6])
    ab = re.search(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([\w.-]+)", html)
    if ab and ab[1].lower() not in ("api", "embed"):
        return "ashby", ab[1]
    ids = _smartrecruiters_ids(html)
    if ids:
        return "smartrecruiters", ids.most_common(1)[0][0]
    for m in re.finditer(r"\b([\w-]+)\.icims\.com", html):
        if m[1].lower() not in _ICIMS_SKIP:
            return "icims", f"https://{m[1].lower()}.icims.com/jobs/search?ss=1"
    if "teamtailor" in low and re.search(r"teamtailor-cdn|teamtailor\.com/(?:assets|jobs)|tt\.teamtailor\.com", low):
        return "teamtailor", (_origin(url) + "/jobs.rss", "teamtailor")
    ts = urlparse(url).hostname.endswith(".talent-soft.com") or "offerrss.ashx" in low or (
        "talentsoft" in low and re.search(r"detailoffre\.aspx|/offre-de-emploi/|handlers/", low))
    board = url if ts else next((urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                                 if re.search(r"\.talent-soft\.com/offre-de-emploi/", urljoin(url, a["href"]))), None)
    if board and syntactic_ok(board):
        return "talentsoft", _talentsoft_arg(board)
    api = soup.find("link", rel=lambda v: v and "https://api.w.org/" in v)
    if api and api.get("href") and syntactic_ok(urljoin(url, api["href"])):
        root = urljoin(url, api["href"])
        root = root if root.endswith("/") else root + "/"
        bases = wordpress_types(f, root)
        if bases:
            return "wordpress", (root, bases)
    for ln in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        title = ln.get("title") or ""
        blog = re.fullmatch(r"/(?:\w{2}/)?feed/?", urlparse(urljoin(url, ln.get("href") or "")).path or "")
        capped = re.search(r"/searchjobs", urlparse(url).path, re.I)  # Avature (Siemens): 20 unsorted offers, the list has all
        if ("rss" in (ln.get("type") or "") and ln.get("href") and _FEED_TITLE.search(title) and not blog and not capped
                and not re.search(r"comment", title, re.I)):
            flavor = "teamtailor" if "teamtailor" in low else "rss"
            return flavor, (urljoin(url, ln["href"]), flavor)
    return None


_LIST_LINK = re.compile(r"^(?:voir|see|view|browse|explore|toutes|all|nos|our|découvr\w*|consult\w*)?\s*(?:all\s+|toutes\s+|les\s+|nos\s+|our\s+)*"
                        r"(?:jobs?|offres?(?: d'emploi)?|open (?:positions|roles)|opportunit\w+|postes?|annonces?)"
                        r"(?: ouvert\w*| disponibles?| available)?$", re.I)


def detect_one_hop(f, url, html):
    """A careers home page whose 'See all jobs' link leads to the page that names the platform (mirakl.com/careers ->
    /company/careers/jobs/)."""
    soup = BeautifulSoup(html, PARSER)
    host = urlparse(url).hostname
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        if (_LIST_LINK.match(norm(a.get_text(" ", strip=True))) and urlparse(u).hostname == host
                and normalize_url(u) != normalize_url(url) and syntactic_ok(u)):
            try:
                found = detect_from_url(u) or detect_from_page(f, u, f.get(u).text)
            except AccessError:
                return None
            except Exception:
                continue
            return (found, u) if found else None
    return None


_NOT_BRAND = {"careers", "career", "jobs", "job", "carrieres", "carriere", "emploi", "recrutement", "join", "us", "home",
              "accueil", "the", "and", "work", "with", "at", "our", "team", "www", "com", "fr"}


def guess_board(f, url):
    """A careers page that loads its offers from Greenhouse / Ashby / Lever without writing the board name: the company's
    name is tried as the board name. A board is accepted only if its offers link back to the company's website, or the
    page itself names that platform (so another company's board with the same name is never taken)."""
    try:
        html = f.get(url).text
    except Exception:
        return None
    low, host = html.lower(), urlparse(url).hostname or ""
    soup = BeautifulSoup(html, PARSER)
    site = soup.find("meta", property="og:site_name")
    words = re.split(r"[|\-–—:·,]", (site.get("content") if site else "") + "|" + (soup.title.get_text() if soup.title else ""))
    names = [re.sub(r"[^a-z0-9]", "", norm(w)) for w in words]
    names.append(host.split(".")[-2] if host.count(".") else "")
    names = [n for n in dict.fromkeys(names) if 3 <= len(n) <= 30 and n not in _NOT_BRAND]
    domain = ".".join(host.split(".")[-2:])
    tries = (("greenhouse", "greenhouse", "https://boards-api.greenhouse.io/v1/boards/{}/jobs", "jobs", "absolute_url"),
             ("ashby", "ashbyhq", "https://api.ashbyhq.com/posting-api/job-board/{}", "jobs", "jobUrl"),
             ("lever", "lever.co", "https://api.lever.co/v0/postings/{}?mode=json&limit=5", None, "hostedUrl"))
    for name in names[:3]:
        for platform, marker, api, key, url_key in tries:
            try:
                data = f.get(api.format(name)).json()
            except Exception:
                continue
            posts = (data.get(key) if key else data) or []
            if not isinstance(posts, list) or not posts:
                continue
            links_home = any((urlparse(p.get(url_key) or "").hostname or "").endswith(domain) for p in posts[:20])
            if links_home or marker in low:
                return platform, name
    return None


def _talentsoft_arg(url):
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    lcid = q.get("LCID", "1036")
    if "liste" in p.path:
        lst = urlunparse(p._replace(query=urlencode({k: v for k, v in q.items() if k.lower() != "page"})))
    else:
        lst = f"{_origin(url)}/offre-de-emploi/liste-toutes-offres.aspx?all=1&mode=list&LCID={lcid}"
    return f"{_origin(url)}/handlers/offerRss.ashx?LCID={lcid}", lst


ADAPTERS = {"greenhouse": crawl_greenhouse, "lever": crawl_lever, "ashby": crawl_ashby, "workday": crawl_workday,
            "smartrecruiters": crawl_smartrecruiters, "breezy": crawl_breezy, "recruitee": crawl_recruitee,
            "teamtailor": crawl_feed, "talentsoft": crawl_talentsoft, "rss": crawl_feed,
            "successfactors": crawl_successfactors, "icims": crawl_icims, "wordpress": crawl_wordpress,
            "phenom": crawl_phenom, "eightfold": crawl_eightfold, "jibe": crawl_jibe, "workable": crawl_workable,
            "talentview": crawl_talentview}
