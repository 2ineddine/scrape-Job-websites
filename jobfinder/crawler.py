"""crawler.py - which reader handles a website, and the generic crawler for career pages built on no known platform.

Order for every website (crawl_source):
  1. a platform adapter (adapters.py) when the URL or the page reveals the platform - exact dates, no browser;
  2. the generic crawler: JSON-LD JobPosting, job data embedded in the page (Next.js...), job links + listing-card dates,
     pagination; a page that is empty without JavaScript is rendered once in a headless browser;
  3. the AI browser (browser.py) when nothing was found (automatic mode) - it clicks through any website."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .adapters import (ADAPTERS, Page, _Budget, _open_detail, is_job_path, _jsonld_jobs, _parse_detail, detect_from_page, detect_from_url,
                       detect_one_hop, embedded_jobs, guess_board, jobs_from_json, worth_opening)
from .classify import is_boilerplate_title
from .sitefilters import filtered_url
from .dates import _UPDATE_WORDS, _dayfirst, parse_date, text_publication_date
from .config import utcnow
from .web import UA, AccessError, check_url, norm, normalize_url, syntactic_ok

_ATS_HOSTS = ("greenhouse", "lever.co", "ashby", "smartrecruiters", "myworkdayjobs", "breezy.hr", "recruitee", "teamtailor",
              "icims", "welcomekit", "welcometothejungle", "jobteaser", "taleez", "flatchr", "digitalrecruiters")


def _job_links(soup, base, exclude):
    host = urlparse(base).hostname
    out = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"])
        pu = urlparse(u)
        ats = any(x in (pu.hostname or "") for x in _ATS_HOSTS)
        jobish = is_job_path(pu.path) or (ats and re.search(r"\d{5,}|[0-9a-f]{8}-[0-9a-f]{4}", pu.path))
        if pu.scheme in ("http", "https") and jobish and normalize_url(u) not in exclude:
            if pu.hostname == host or ats:
                nu = normalize_url(u)
                if nu not in out and nu != normalize_url(base):
                    out.append(nu)
    return out[:300]


def _next_page(soup, base, n):
    ln = soup.find("link", rel=lambda v: v and "next" in v)
    if ln and ln.get("href"):
        return urljoin(base, ln["href"])
    for a in soup.find_all("a", href=True):
        label = norm(a.get_text(" ") + " " + (a.get("aria-label") or "") + " " + (a.get("title") or ""))
        if "next" in a.get("rel", []) or re.fullmatch(r"\s*(next( page)?|suivant(e)?|page suivante|aller a la page suivante|"
                                                      r">|>>|›|»|→)\s*", label.strip()):
            return urljoin(base, a["href"])
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == str(n + 1):
            return urljoin(base, a["href"])
    return None


def render_js(f, url, cfg):
    """One headless-browser rendering (no AI) for pages that are empty without JavaScript: clicks 'load more', scrolls,
    and keeps the JSON job lists the page downloads. -> (html, jobs_from_network) or (None, [])."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, []
    if not check_url(url)[0] or not f.robots_ok(url):
        return None, []
    found = []

    def grab(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch") and "json" in (resp.headers.get("content-type") or "") \
                    and len(found) < 2000:
                found.extend(jobs_from_json(resp.json(), url))
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context(user_agent=UA, locale="fr-FR")
            ctx.route("**/*", lambda route: route.continue_() if syntactic_ok(route.request.url) or
                      route.request.url.startswith(("data:", "blob:")) else route.abort())
            pg = ctx.new_page()
            pg.on("response", grab)
            pg.goto(url, timeout=cfg["timeout"] * 1000, wait_until="domcontentloaded")
            try:
                pg.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            for _ in range(8):
                btn = pg.get_by_role("button", name=re.compile(r"load more|show more|voir plus|charger plus|afficher plus|"
                                                               r"plus d'offres|more jobs", re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=4000)
                else:
                    pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(700)
            html = pg.content()
            b.close()
            return html, found
    except Exception as e:
        f.note(f"browser rendering failed: {str(e)[:120]}")
        return None, found


def _card_dates(soup, base, now=None, dayfirst=None):
    """url -> (date, conf, source, text): the date written on the LISTING card of each job link ('Posted 3 days ago',
    'Publiée le 01/10/2026'). Only labelled or relative dates: a bare date could be a deadline. Lets the crawler skip
    jobs that are already older than the range without opening them."""
    now, out = now or utcnow(), {}
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"])
        if urlparse(u).scheme not in ("http", "https") or not is_job_path(urlparse(u).path):
            continue
        nu = normalize_url(u)
        if nu in out:
            continue
        node, best = a, None
        for _ in range(5):  # the smallest container that holds this job link and no other job link
            node = node.parent
            if node is None or node.name in ("body", "html", "main", "ul", "ol", "table", "tbody", "section"):
                break
            others = {normalize_url(urljoin(base, x["href"])) for x in node.find_all("a", href=True)
                      if is_job_path(urlparse(urljoin(base, x["href"])).path)}
            if len(others) > 1:
                break
            best = node
        if best is None:
            continue
        txt = best.get_text(" ", strip=True)
        if len(txt) > 700:
            continue
        d, c, t, _rel = text_publication_date(txt, now, dayfirst, loose=True)
        if d is None:
            for tm in best.find_all("time"):
                ctx = norm(tm.parent.get_text(" ", strip=True)[:160]) if tm.parent else ""
                if _UPDATE_WORDS.search(ctx):
                    continue
                v = tm.get("datetime") or tm.get_text(" ", strip=True)
                d, c = parse_date(v, now, dayfirst)
                t = v
                if d is not None and c >= 0.5 and d <= now + timedelta(days=1):
                    break
                d = None
        if d is not None:
            out[nu] = (d, min(c, 0.75), "listing card", t)
    return out


def _card_title(soup, base, url):
    for a in soup.find_all("a", href=True):
        if normalize_url(urljoin(base, a["href"])) == url:
            # a whole card can be the link: its heading is the title, not 'Location : ... Job Category : Marketing'
            head = a.find(["h1", "h2", "h3", "h4", "h5"]) or a.find(class_=re.compile(r"title", re.I))
            t = (head or a).get_text(" ", strip=True)
            t = _CARD_LABELS.split(t, 1)[0].strip() or t
            if 3 <= len(t) <= 200:
                return t
    return ""


_CARD_LABELS = re.compile(r"\s+(?:offer published|published on|posted on|date de publication|publi[ée]e? le|location\s*:|"
                          r"lieu\s*:|localisation\s*:|job category\s*:|cat[ée]gorie\s*:|contract type\s*:|type de contrat\s*:)",
                          re.I)


_OFFER_WORDS = re.compile(r"\b(missions?|profil|profile|poste|position|responsabilit\w+|responsibilit\w+|competences|skills|"
                          r"requirements|qualifications|experience|candidat\w*|apply|postuler|contrat|contract|salaire|salary|"
                          r"remuneration|teletravail|remote|vos taches|your role|we are looking|nous recherchons|"
                          r"description du poste|job description|stage|internship|these|phd|diplome|degree)\b")


def looks_like_offer(text) -> bool:
    """A page that really describes an offer: long enough and with several job-ad words (FR + EN)."""
    t = norm(text or "")
    return len(t) >= 500 and len(set(_OFFER_WORDS.findall(t))) >= 3


def _drop_listing_pages(jobs, listing_soup):
    """Links that looked like jobs but opened a list / category page: the same heading on several 'jobs', or the
    heading of the listing page itself ('Our jobs', 'Nos offres'). Structured jobs (JSON-LD, page data) are kept."""
    head = lambda s: norm((s.find("h1") or s.title or s).get_text(" ", strip=True))[:120] if s else ""
    own = head(listing_soup)
    jobs = [j for j in jobs if j.get("provenance") != "html-fallback" or looks_like_offer(j["description"])]
    count = Counter(norm(j["title"]) for j in jobs if j.get("provenance") == "html-fallback")
    return [j for j in jobs if not is_boilerplate_title(j["title"]) and (j.get("provenance") != "html-fallback"
            or (count[norm(j["title"])] == 1 and norm(j["title"]) != own and len(norm(j["title"])) >= 4))]


def _error_page(soup) -> bool:
    head = (soup.find("h1") or soup.title)
    return bool(head) and is_boilerplate_title(head.get_text(" ", strip=True)) and len(soup.get_text(" ", strip=True)) < 4000


def _parent_list(url):
    """https://jobs.inria.fr/public/classic/fr/offres/2025-08639 -> .../offres: the list an expired offer belonged to."""
    p = urlparse(url)
    seg = [x for x in p.path.split("/") if x]
    return urlunparse(p._replace(path="/" + "/".join(seg[:-1]), query="")) if len(seg) >= 2 else None


_DATE_SORT = re.compile(r"[?&](?:sort|sortby|sort_by|order|orderby|tri)=(?:[^&]*date|[^&]*recent|[^&]*newest|[^&]*publi)", re.I)


def crawl_generic(f, url, cfg, known, st):
    seen_pages, seen_links, page_url, n, details, guessed_next, rendered = set(), set(known), url, 0, 0, False, False
    budget, filtered = _Budget(cfg, st), False
    by_date = bool(_DATE_SORT.search(url))  # the URL already asks the website for its newest offers first
    while page_url:
        guessed, guessed_next = guessed_next, False
        if n >= cfg["max_pages"]:
            st.truncated = True
            return
        key = normalize_url(page_url)
        if key in seen_pages:
            return  # loop protection
        seen_pages.add(key)
        n += 1
        try:
            dead, err = False, None
            try:
                r = f.get(page_url)
                soup = BeautifulSoup(r.text, "html.parser")
                dead = n == 1 and _error_page(soup)
            except ConnectionError as e:
                if not (n == 1 and re.search(r"HTTP 40[49]|HTTP 410", str(e))):
                    raise
                dead, err = True, e
            if dead:
                parent = _parent_list(page_url)
                if parent and "+parent" not in st.platform:  # an expired offer: read the list it belonged to
                    f.note(f"page not found (expired offer?): reading the list {parent}")
                    st.platform += "+parent"
                    page_url, n = parent, 0
                    continue
                if err:
                    raise err
            if n == 1 and not filtered:  # the website's own filters first: France, posted within, newest first
                filtered = True
                fu, used = filtered_url(soup, page_url, cfg)
                if fu and normalize_url(fu) != key and f.robots_ok(fu):
                    try:
                        s2 = BeautifulSoup(f.get(fu).text, "html.parser")
                        if _job_links(s2, fu, set()) or _jsonld_jobs(s2, fu) or embedded_jobs(s2, fu):
                            f.note(f"website filters used: {', '.join(used)} ({fu[:100]})")
                            soup, page_url, key = s2, fu, normalize_url(fu)
                            by_date = by_date or any(x == "newest first" or x.startswith("posted within") for x in used)
                            seen_pages.add(key)
                    except AccessError:
                        raise
                    except Exception as e:
                        f.note(f"website filters not usable: {str(e)[:80]}")
            jobs = _jsonld_jobs(soup, page_url) or embedded_jobs(soup, page_url)
            links = _job_links(soup, page_url, set())
            if not jobs and not links and n == 1 and not rendered:  # empty without JavaScript: render it once
                rendered = True
                html2, net = render_js(f, page_url, cfg)
                if html2:
                    soup = BeautifulSoup(html2, "html.parser")
                    jobs = _jsonld_jobs(soup, page_url) or embedded_jobs(soup, page_url) or net
                    links = _job_links(soup, page_url, set())
                    if jobs or links:
                        f.note(f"page rendered with JavaScript: {len(jobs)} jobs, {len(links)} job links")
            new_links = [u for u in links if u not in seen_links]
            seen_links.update(links)
            gate = cfg.get("_gate")
            page_out = sum(1 for u in links if u in st.out_known)  # already known to be too old
            page_seen = sum(1 for u in links if u in known and u not in st.out_known)
            cards = _card_dates(soup, page_url, dayfirst=_dayfirst(soup)) if not jobs else {}
            if not jobs:
                older = False
                for lk in new_links:
                    if lk in known or details >= cfg["max_details_per_source"] or not f.robots_ok(lk):
                        continue
                    cd = cards.get(lk)
                    if older or (cd and gate and gate(cd[0], cd[1], cd[3]) is False):  # DATE FIRST: too old -> never opened
                        page_out += 1
                        older = older or by_date  # newest-first list: every next card is older too
                        continue
                    title = _card_title(soup, page_url, lk)
                    if title and not worth_opening(cfg, {"title": title, "date": None}):
                        continue  # clearly irrelevant title on the list: not opened
                    details += 1
                    try:
                        dhtml = f.get(lk).text
                        job = _parse_detail(dhtml, lk, card=cd)
                        if job["provenance"] == "html-fallback" and len(_job_links(BeautifulSoup(dhtml, "html.parser"), lk, set())) >= 8:
                            continue  # the link opened another list ('Account Executive jobs', '170 jobs found'), not a job
                        jobs.append(job)
                    except AccessError:
                        raise
                    except Exception as e:
                        f.note(f"detail failed {lk}: {e}")
            jobs = [_open_detail(f, cfg, j, budget) if j["provenance"] == "network-json" and len(j["description"]) < 300
                    and normalize_url(j["url"]) not in known and "jbid=" not in j["url"] else j for j in jobs]
            jobs = _drop_listing_pages(jobs, soup)
            yield Page(key, page_url, jobs, out=page_out, seen=page_seen, sorted=by_date)
            if not new_links and not jobs and n > 1:
                return  # no new content: reliable end condition
            nxt = _next_page(soup, page_url, n)
            if not nxt and re.search(r"[?&](page|p|pg|start|offset)=\d+", page_url) and (new_links or jobs):
                nxt = re.sub(r"([?&](?:page|p|pg)=)(\d+)", lambda m: m[1] + str(int(m[2]) + 1), page_url)
                guessed_next = nxt != page_url
            if nxt and (not urlparse(nxt).scheme.startswith("http") or not f.robots_ok(nxt)):
                st.more_pages = True  # 'javascript:pagination(2)' or a page robots.txt closes: the sitemap lists the rest
                f.note(f"the next list page cannot be opened ({nxt[:60]}): the website's sitemap is read instead")
                return
            page_url = nxt
        except AccessError:
            raise
        except Exception as e:
            if n == 1:
                raise
            if guessed and re.search(r"HTTP (404|410)", str(e)):
                return  # the guessed page after the last one does not exist: normal end of the list
            yield Page(key, page_url, [], error=f"pagination_failed: {e}")
            return


_SITEMAP_JOB = re.compile(r"/(?:jobs?|offres?|offers?|vacanc\w*|positions?|postings?|emplois?|annonces?|job-offers?)/"
                          r"(?=[^?#]*(?:\d{3,}|[a-z]+-[a-z]+-[a-z]+))", re.I)


def sitemap_jobs(f, url, limit=5000):
    """[(job url, lastmod)] from the website's sitemaps (robots.txt 'Sitemap:' lines, else /sitemap.xml). Index files are
    followed one level, job sitemaps first ('vacanciessitemap.xml')."""
    host, out, queue, done = urlparse(url).hostname, {}, list(f.sitemaps(url)), set()
    while queue and len(done) < 12 and len(out) < limit:
        sm = queue.pop(0)
        if sm in done or sm.endswith(".gz"):
            continue
        done.add(sm)
        try:
            xml = f.get(sm).text
        except AccessError:
            continue
        except Exception as e:
            f.note(f"sitemap not read {sm[:80]}: {e}")
            continue
        children = re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)", xml)
        jobs_first = sorted(children, key=lambda u: not re.search(r"job|vacanc|offre|offer|position|emploi|career", u, re.I))
        queue += [u for u in jobs_first if urlparse(u).hostname == host]
        for m in re.finditer(r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]+))?", xml):
            u = m[1].replace("&amp;", "&")
            if urlparse(u).hostname == host and _SITEMAP_JOB.search(urlparse(u).path) and f.robots_ok(u):
                out.setdefault(normalize_url(u), m[2] or "")
    return list(out.items())[:limit]


def _slug_title(u):
    """'/en/job/vitry-sur-seine/technicien-de-production-dsp/2649/464' -> 'technicien de production dsp' (a provisional
    title for the title filter; the job page gives the real one)."""
    segs = [s for s in urlparse(u).path.split("/") if re.search(r"[a-z]{3}", s, re.I)]
    best = max(segs[1:] or segs or [""], key=lambda s: s.count("-"))
    return re.sub(r"-(?:jid-)?\d+$", "", best).replace("-", " ").strip()


def crawl_sitemap(f, url, cfg, known, st):
    """The jobs a list does not let the crawler page through, from the sitemap: newest 'lastmod' first. An offer cannot be
    published after its last modification, so a lastmod older than the range proves the offer too old (never opened)."""
    gate, details, seen, batch = cfg.get("_gate"), 0, set(known), []
    entries = sorted(sitemap_jobs(f, url), key=lambda e: e[1], reverse=True)
    f.note(f"sitemap: {len(entries)} job pages listed")
    out = 0
    for u, lastmod in entries:
        if u in seen:
            continue
        seen.add(u)
        lm, c = parse_date(lastmod) if lastmod else (None, 0.0)
        if lm and gate and gate(lm, max(c, 0.9), lastmod) is False:
            out += 1  # last modified before the range: published before it too - and so is every next entry
            f.note("sitemap: the next offers were last modified before the range -> finished")
            break
        if not worth_opening(cfg, {"title": _slug_title(u), "date": None}):
            continue
        if details >= cfg["max_details_per_source"]:
            st.details_capped = True
            break
        details += 1
        try:
            job = _parse_detail(f.get(u).text, u)
            job["provenance"] += "+sitemap"
            batch.append(job)
        except AccessError:
            raise
        except Exception as e:
            f.note(f"detail failed {u}: {e}")
        if len(batch) >= 25:
            yield Page("sitemap", url, batch, out=out, ordered=False)
            batch, out = [], 0
    yield Page("sitemap", url, batch, out=out, ordered=False)


def detect_platform(f, url):
    """-> (name, argument) of the reader for this website ('generic', url) when no platform is recognised."""
    found = detect_from_url(url)
    if found:
        return found
    try:
        html = f.get(url).text
    except AccessError:
        raise
    except Exception:
        return "generic", url  # the generic crawler reports the error itself
    found = detect_from_page(f, url, html)
    if not found:  # the job list page one link away names the platform (and every board: mirakl.com)
        hop = detect_one_hop(f, url, html)
        if hop:
            f.note(f"platform found on the job list page {hop[1][:80]}")
            found = hop[0]
    if not found and re.search(r"ashbyhq|greenhouse\.io|lever\.co", html, re.I):  # names the board's platform, not the board
        found = guess_board(f, url)
    return found or ("generic", url)


_UNSUPPORTED = {"linkedin.com": "LinkedIn shows jobs only to signed-in members: add the company's own career page instead",
                "indeed.": "Indeed blocks automated reading: add the company's own career page instead"}


def crawl_source(f, url, cfg, known, st):
    host = (urlparse(url).hostname or "").lower()
    for k, why in _UNSUPPORTED.items():
        if k in host:
            raise AccessError(f"access_restricted: {why}")
    bare = urlunparse(urlparse(url)._replace(query="", fragment=""))
    if hasattr(f, "robots_ok") and not f.robots_ok(url) and bare != url and f.robots_ok(bare):
        f.note(f"robots.txt closes this search URL but not the page itself: reading {bare} (and its sitemap)")
        url = bare
        st.more_pages = True
    mode = cfg.get("nav_mode", "auto")
    name, arg = ("generic", url) if mode == "ai" else detect_platform(f, url)
    st.platform = name
    found = 0
    if name in ADAPTERS:
        f.note(f"platform: {name}")
        try:
            for pg in ADAPTERS[name](f, arg, cfg, known, st):
                found += len(pg.jobs)
                yield pg
        except Exception as e:  # refused (robots.txt) or failed: the company page itself may still be readable
            if found:
                raise
            f.note(f"{name} reader failed ({str(e)[:120]}): trying the generic crawler")
            st.platform = f"{name}->generic"
        if found:
            return
    if mode != "ai":
        got = set()
        for pg in crawl_generic(f, url, cfg, known, st):
            found += len(pg.jobs)
            got.update(normalize_url(j["url"]) for j in pg.jobs)
            yield pg
        if st.more_pages:
            for pg in crawl_sitemap(f, url, cfg, set(known) | got, st):
                found += len(pg.jobs)
                yield pg
        if found == 0:  # a page that loads its jobs from a job board without naming the board
            board = guess_board(f, url)
            if board:
                f.note(f"job board found from the company name: {board[0]} '{board[1]}'")
                st.platform = f"{board[0]} (guessed board)"
                try:
                    for pg in ADAPTERS[board[0]](f, board[1], cfg, known, st):
                        found += len(pg.jobs)
                        yield pg
                except Exception as e:
                    if found:
                        raise
                    f.note(f"{board[0]} board not read: {str(e)[:120]}")
    if found == 0 and mode == "auto" and cfg.get("_keys") is not None:  # offers written on the page itself (lab pages)
        from .pagereader import read_page
        try:
            for pg in read_page(f, url, cfg, known, st):
                found += len(pg.jobs)
                yield pg
            if found:
                st.platform = "ai-page-reader"
                f.note(f"AI page reader: {found} offers written on the page")
                return
        except AccessError:
            raise
        except Exception as e:
            f.note(f"AI page reader: {str(e)[:120]}")
    if found == 0 and mode in ("auto", "ai") and not known and cfg.get("_keys") is not None:
        if mode == "auto":
            f.note("no job found by reading the page: switching to the AI browser")
        st.platform = "ai-browser"
        from .browser import crawl_ai  # imported late: needs Playwright and the AI model
        yield from crawl_ai(f, url, cfg, known, st)
