"""scan.py - the search engine: crawl every website, store the offers, filter them DATE FIRST -> France -> title, and hand
each website's remaining offers to the AI as soon as the website is read (the AI works while the next websites are read)."""
from __future__ import annotations

import hashlib
import itertools
import queue
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlparse

from .adapters import ScanState, date_order, distrust_shared_dates, make_gate
from .classify import (analyze_title, classify_location, clean_location, clean_title, france_among_places, is_boilerplate_title,
                       location_from_text,
                       looks_like_date, offer_kinds_heading)
from .config import HW_KEYWORDS_URL, LOG, run_config, utcnow
from .crawler import crawl_source, suggest_page
from .dates import date_decision, range_start, refine_job_date, refine_raw_date
from .store import (RECENT_HOURS, Job, MarketOffer, SeenOffer, SessionLocal, SiteMemory, Source, cv_key, keep_recent_offers,
                    lock_scan, profile_key, restore_recent_offers, scan_ended, scan_record, scan_started, scan_todo,
                    unlock_scan)
from .web import AccessError, Fetcher, html_to_text, norm, normalize_url


# --------------------------------------------------------------------------- persistence / filters
_HTML_TAGS = re.compile(r"</?(?:p|span|div|li|ul|ol|br|strong|em|b|i|u|font|h\d|table|tr|td)\b[^>]*>", re.I)


def upsert_job(s, src, raw, hidden=False) -> bool:
    """Store a job. hidden=True: the job is OUTSIDE the date range - only a tiny record is kept (never shown, never analysed)
    so that the website is not re-opened for it at the next search."""
    raw = dict(raw)
    if _HTML_TAGS.search(raw.get("description") or ""):  # Phenom (Thales, Orange, SNCF...), some JSON-LD: the page's markup
        raw["description"] = html_to_text(raw["description"])  # was stored as text (30% of it), and sent to the AI
    raw = refine_raw_date(raw)  # an explicit 'Date de publication' in the description beats a feed / refresh date
    original_title, raw["title"] = raw["title"], clean_title(raw["title"])  # 'Ingénieur IA H/F (REQ123)' -> 'Ingénieur IA'
    raw["location"] = clean_location(raw.get("location"))  # 'Palaiseau Avantages 40-45k€ ...' -> 'Palaiseau'
    if raw["location"] and ((not re.search(r"[^\W\d_]{2}", raw["location"]) and not re.fullmatch(r"\s*\d{5}\s*", raw["location"]))
                            or looks_like_date(raw["location"])):
        raw["location"] = ""  # '1758' (an internal number), 'Jeudi, 5 novembre, 2026' (a date): not a place
    if not raw["location"] and raw["description"]:
        raw["location"] = location_from_text(raw["description"], raw["title"])
    url = normalize_url(raw["url"]) if raw.get("url") else ""
    ident = url or f"{src.id}:{raw.get('platform_id')}:{raw['title']}"
    uid = hashlib.sha1(ident.encode()).hexdigest()[:16]
    desc = "" if hidden else raw["description"]
    chash = hashlib.sha1(f"{raw['title']}|{raw['location']}|{raw['description'][:3000]}".encode()).hexdigest()
    job = s.query(Job).filter_by(uid=uid).first()
    seen = s.get(SeenOffer, uid)
    if not seen and not hidden:
        seen = SeenOffer(uid=uid, first_seen=utcnow())
        s.add(seen)
    if job:
        job.last_seen = utcnow()
        if hidden:
            if job.status not in ("shortlisted", "out_of_range"):  # the real date is outside the range: take it out of every list
                job.status, job.reason = "out_of_range", f"date: {raw['date']:%Y-%m-%d} {raw['date_source']}" if raw["date"] else "date"
                job.pub_date, job.date_conf, job.date_source, job.pub_date_text = (
                    raw["date"], raw["date_conf"], raw["date_source"], raw["date_text"])
                job.description = ""
            return False
        if job.status == "out_of_range":  # the range was widened (or the date was corrected): the job is back
            job.status, job.reason, job.relevance_score, job.relevance_json = "new", "", None, None
            job.description, job.location_original = desc, raw["location"]
            job.pub_date, job.date_conf, job.date_source, job.pub_date_text = (
                raw["date"], raw["date_conf"], raw["date_source"], raw["date_text"])
            job.content_hash = chash
            return True
        if job.content_hash != chash and job.relevance_score is None:
            job.status = "new"
        return False
    s.add(Job(uid=uid, source_id=src.id, platform_job_id=raw.get("platform_id"), title=raw["title"][:300],
              employer=raw.get("employer") or src.label, url=url, apply_url=raw.get("apply_url") or url,
              description=desc, employment_type=raw.get("employment_type"),
              location_original=raw["location"], pub_date_text=raw["date_text"], pub_date=raw["date"],
              date_source=raw["date_source"], date_conf=raw["date_conf"], extraction_conf=raw["conf"],
              provenance=raw["provenance"], content_hash=chash,
              status="out_of_range" if hidden else "new",
              reason=(f"date: {raw['date']:%Y-%m-%d}" if raw["date"] else "date") if hidden else "",
              extra={**{k: raw.get(k) for k in ("remote_hint", "applicant_countries", "country_hint", "department")},
                     "original_title": original_title,
                     "first_seen": seen.first_seen.strftime("%Y-%m-%d") if seen else None}))
    return not hidden


def online_since(s, job):
    """The earliest day the app saw this offer online ('YYYY-MM-DD'): its own memory of the offers (kept between sessions)
    or the statistics history (6 months), whichever is older. For an undated offer, it was published that day or before."""
    days = [(job.extra or {}).get("first_seen") or ""]
    seen = s.get(SeenOffer, job.uid)
    market = s.get(MarketOffer, job.uid)
    days += [f"{x:%Y-%m-%d}" for x in (seen and seen.first_seen, market and market.seen) if x]
    return min((x for x in days if x), default="")


def _before(day, first_day) -> bool:
    """'2026-11-05' (a day the app saw an offer online) is before the range's first day."""
    return bool(day) and datetime.strptime(day, "%Y-%m-%d").date() < first_day


def seen_before_range(s, job, cfg, now=None) -> str:
    """An offer without a reliable publication date that the app already saw online before the date range began was
    published that day or earlier: it is outside the range. -> that day ('YYYY-MM-DD'), or '' when unknown or inside.
    (Scan of 2026-11-12, 'Last 24 hours': 145 of the 213 relevant offers had no date - Inria, Ensta, Davidson... - and,
    without this, the same ones would come back every day.)"""
    since = online_since(s, job)
    return since if _before(since, range_start(cfg, now)) else ""


def hide_out_of_range(s, job, why):
    """The job's real publication date is outside the range: it leaves every list, is never analysed and never shown
    (relevant or not). A tiny record stays so that the website is not re-opened for it."""
    job.status, job.reason, job.description = "out_of_range", "date: " + why, ""
    job.relevance_score, job.relevance_json, job.match_score, job.match_json = None, None, None, None
    s.commit()


def apply_filters(s, cfg, profile, now=None, source_ids=None, merge=True):
    """Pipeline stages 5-7: date -> France location -> fast title filter. Records reason for every outcome.
    source_ids: only the offers of these websites (a website just read); merge: then merge the duplicates of every website."""
    now, st = now or utcnow(), Counter()
    first_day = range_start(cfg, now)
    todo = s.query(Job).filter(Job.status == "new")
    if source_ids is not None:
        todo = todo.filter(Job.source_id.in_(list(source_ids)))
    for job in todo.all():
        flags, ex = [], job.extra or {}
        refine_job_date(job, now)  # the description may state the real publication date
        first = ex.get("first_seen")
        if first and job.pub_date and job.pub_date.date() > datetime.strptime(first, "%Y-%m-%d").date() + timedelta(days=1):
            # the website re-dated an offer already online (a 'refresh'): it cannot have been published after the app saw it
            job.pub_date_text = f"{job.pub_date:%Y-%m-%d} on the website, but first seen on {first}"
            job.pub_date, job.date_conf = datetime.strptime(first, "%Y-%m-%d"), min(job.date_conf or 0.7, 0.7)
            job.date_source = f"first seen by the app (the website re-dated it: {job.date_source})"
        d, why = date_decision(job.pub_date, job.date_conf, cfg, now, job.pub_date_text)
        if d is False:  # DATE FIRST: outside the range -> hidden, never analysed, never shown (relevant or not)
            hide_out_of_range(s, job, why)
            st["date_x"] += 1
            continue
        if d is None:  # no publication date: the offer is RETURNED, with the earliest day the app knows it was online
            since = online_since(s, job)
            if since:
                job.extra = ex = {**ex, "first_seen": since}
            if _before(since, first_day):
                # ...unless the app already saw it online before the range began: published that day or earlier, so it is
                # outside the range like a dated offer (a wider range brings it back)
                hide_out_of_range(s, job, f"not stated by the website, but online since {since}: published before "
                                          f"'{cfg['date_preset']}'")
                st["date_x"] += 1
                continue
        if d is None and cfg["unknown_date_policy"] == "exclude":
            job.status, job.reason = "excluded", "date: " + why
            st["date_x"] += 1
            continue
        if d is None and cfg["unknown_date_policy"] == "review":
            flags.append("publication_date_unknown")
        loc = classify_location(job.location_original, ex.get("country_hint"),
                                ex.get("remote_hint") if cfg["include_remote_fr"] else None, ex.get("applicant_countries"))
        if not loc["location_verified"] and job.description:
            # the location field says nothing ('2 sites', a typo): the offer's own text may name its French place
            # ('notre agence de Lille', '(H/F) à Lyon'). Only France is taken from it: one foreign site named in the text of a
            # multi-site offer does not prove that France is not among the others.
            alt = location_from_text(job.description, job.title)
            alt_loc = classify_location(alt) if alt and alt != job.location_original else None
            if alt_loc and alt_loc["france_eligible"]:
                loc = {**alt_loc, "location_original": job.location_original,
                       "location_evidence": f"the offer's text names '{alt}' (location field: '{job.location_original or '-'}')"}
        job.country_code, job.location_verified, job.france_eligible = loc["country_code"], loc["location_verified"], loc["france_eligible"]
        job.remote_status, job.location_evidence, job.city = loc["remote_status"], loc["location_evidence"], loc["city"]
        if loc["remote_status"] == "remote" and not cfg["include_remote_fr"]:
            job.status, job.reason = "excluded", "location: remote jobs disabled"
            st["loc_x"] += 1
            continue
        said = france_among_places(job.description) if loc["location_verified"] and not loc["france_eligible"] else ""
        if said:  # listed abroad, but the offer says it can be held in France too: included
            job.country_code, job.france_eligible = "FR", True
            job.location_evidence = f"{loc['location_evidence']}; but the offer's text names France among its places: '{said}'"
        elif loc["location_verified"] and not loc["france_eligible"]:
            job.status, job.reason = "excluded", "location: " + loc["location_evidence"]
            st["loc_x"] += 1
            continue
        if not loc["location_verified"]:
            flags.append("location_unverified")
        if is_boilerplate_title(job.title) or offer_kinds_heading(job.title):
            # 'Postdoc, PhD or Master 2', '<<INTERNAL JOB DESCRIPTION>>': a page about offers or a template, not an offer
            job.status, job.reason = "excluded", f"title: '{job.title[:60]}' names no offer (a page about offers or a template)"
            st["title_x"] += 1
            continue
        ti = analyze_title(job.title, ex.get("department"), profile, job.employment_type)
        job.extra = {**ex, "title_info": ti.as_dict()}  # reassigned so the JSON change is saved
        verdict, twhy = ti.verdict, ti.reason
        if verdict == "reject":
            job.status, job.reason = "excluded", "title: " + twhy
            st["title_x"] += 1
            st["type_x"] += "not wanted" in twhy
            continue
        doubts = [x for x in flags if x != "publication_date_unknown"]  # no date alone: still returned with the others
        job.status = "review" if doubts else "candidate"
        job.review_flags, job.reason = ",".join(flags), f"title: {twhy}; date: {why}"
        st["review" if doubts else "candidate"] += 1
    s.commit()
    if merge:
        st["dups_merged"] = merge_duplicates(s, cfg, now)
    return dict(st)


def _offer_key(job, label):
    """Same offer = same title (without gender marks / codes), same employer, same city."""
    norm_ = lambda x, n: re.sub(r"[^a-z0-9]", "", norm(x or ""))[:n]
    return norm_(clean_title(job.title), 60), norm_(job.employer or label, 12), norm_(job.city or job.location_original, 12)


_LANG_SEG = re.compile(r"(?i)[a-z]{2}(?:[-_][a-z]{2})?|int-[a-z]{2}")
_LANG_PARAM = re.compile(r"(?i)lcid|lang|language|locale|hl|lg")


def _lang_free(url):
    """'jobs.makesense.org/en/jobs/x' and 'jobs.makesense.org/fr/jobs/x': one offer shown in two languages. The query stays
    (minus its language): 'detailoffre.aspx?idOffre=39221' and '?idOffre=38837' are two offers."""
    p = urlparse(url or "")
    query = sorted((k.lower(), v) for k, v in parse_qsl(p.query) if not _LANG_PARAM.fullmatch(k))
    return (p.netloc + "/".join(x for x in p.path.split("/") if not _LANG_SEG.fullmatch(x))).lower() + str(query)


def _same_posting(a, b, board):
    """Two copies of ONE offer on one website (same title, employer and city): the same address in another language, or the
    same text posted again (a job board reposting it through several advertising links, a company re-publishing it under a
    new reference). Several openings with one title have different texts (another team, subject or client): kept."""
    if a.url and b.url and _lang_free(a.url) == _lang_free(b.url):
        return True
    x, y = (a.description or "")[:2500], (b.description or "")[:2500]
    if min(len(x), len(y)) < 300:
        return False
    need, sm = (0.9 if board else 0.97), SequenceMatcher(None, x, y)
    return sm.real_quick_ratio() >= need and sm.quick_ratio() >= need and sm.ratio() >= need


def merge_duplicates(s, cfg, now=None):
    """One offer published on several websites (a Thales internship on Thales' site and on stage.fr, a Framatome offer on
    EDF's site) is shown once: the copy from the company's own site (not a job board), dated by the EARLIEST reliable date of
    its copies (job boards pick offers up later). The other copies leave the lists as 'duplicate', pointing at the kept one."""
    from .urlclean import is_job_board
    srcs = {x.id: x for x in s.query(Source).all()}
    groups = {}
    for j in s.query(Job).filter(Job.status.in_(["candidate", "review", "relevant"])):
        src = srcs.get(j.source_id)
        if src and j.title:
            groups.setdefault(_offer_key(j, src.label), []).append(j)
    merged = 0
    for key, js in groups.items():
        if not key[0]:
            continue
        js.sort(key=lambda j: (j.pub_date or datetime.max, len(j.url or "")))  # the earliest copy is the publication
        kept = []
        for j in js:  # copies of one offer on ONE website first; the same title with another text is another opening
            twin = next((k for k in kept if k.source_id == j.source_id and
                         _same_posting(k, j, is_job_board(srcs[j.source_id].url))), None)
            if twin is None:
                kept.append(j)
                continue
            j.status, j.reason = "excluded", f"duplicate: the same offer published again on this website ({twin.url})"
            twin.extra = {**(twin.extra or {}), "also_on": sorted(set((twin.extra or {}).get("also_on", [])) | {j.url})[:10]}
            merged += 1
        js = kept
        if len({j.source_id for j in js}) < 2:
            continue
        js.sort(key=lambda j: (is_job_board(srcs[j.source_id].url), len(j.description or "") < 200, j.pub_date or datetime.max))
        keep, dated = js[0], [j for j in js if j.pub_date and (j.date_conf or 0) >= 0.5]
        first = min(dated, key=lambda j: j.pub_date) if dated else None
        if first and first is not keep and (keep.pub_date is None or first.pub_date < keep.pub_date):
            keep.pub_date, keep.date_conf, keep.pub_date_text = first.pub_date, first.date_conf, first.pub_date_text
            keep.date_source = f"{first.date_source} (earliest of {len(js)} copies, on {srcs[first.source_id].label})"
        copies = [j for j in js[1:] if j.source_id != keep.source_id]  # same website, other text: another opening, kept
        for j in copies:
            j.status, j.reason = "excluded", f"duplicate: the same offer on {srcs[keep.source_id].label} ({keep.url})"
            merged += 1
        keep.extra = {**(keep.extra or {}), "also_on": sorted(set((keep.extra or {}).get("also_on", [])) | {j.url for j in copies})[:10]}
        if date_decision(keep.pub_date, keep.date_conf, cfg, now, keep.pub_date_text)[0] is False:
            hide_out_of_range(s, keep, "earliest copy of this offer is older than the range")
    s.commit()
    return merged


def reset_filters(s, source_ids=None):
    """Offers filtered but not analysed (kept from an earlier scan) are filtered again with this scan's settings."""
    q = s.query(Job).filter(Job.status.in_(["excluded", "review", "candidate"]), Job.relevance_score.is_(None))
    if source_ids is not None:
        q = q.filter(Job.source_id.in_(list(source_ids)))
    n = q.update({Job.status: "new"}, synchronize_session=False)
    s.commit()
    return n


def offer_priority(job):
    """The AI reads the most promising offers first among those waiting, as a run or a free quota may end before the last
    one: titles in the research fields, then offers with a proven date, then the newest."""
    verdict = ((job.extra or {}).get("title_info") or {}).get("verdict")
    return verdict != "pass", job.status != "candidate", -(job.pub_date.timestamp() if job.pub_date else 0)


class Feed:
    """The offers handed to the AI while the websites are still being read: each website's offers as soon as it is read
    and filtered, the most promising first among those waiting. Rounds: 0 first reading, 1 a reading that failed, tried
    again after the others, 2 a small model's reading read again by a better model. An offer waiting or being read is
    'pending': it is not handed over twice."""
    FIRST, AGAIN, BETTER = 0, 1, 2

    def __init__(self):
        self._q, self._n, self._lock = queue.PriorityQueue(), itertools.count(), threading.Lock()
        self.pending, self.closed = set(), threading.Event()

    def put(self, jobs) -> int:
        """Hands over offers (Job rows); -> how many were new to the feed."""
        n = 0
        with self._lock:
            for j in jobs:
                if j.id not in self.pending:
                    self.pending.add(j.id)
                    self._q.put(((self.FIRST, *offer_priority(j)), next(self._n), self.FIRST, j.id))
                    n += 1
        return n

    def again(self, jid, rnd):
        """Back in the queue, after every offer of an earlier round (it stays pending)."""
        with self._lock:
            self.pending.add(jid)
            self._q.put(((rnd,), next(self._n), rnd, jid))

    def get(self):
        """-> (round, job id), or None when nothing is waiting now."""
        try:
            _, _, rnd, jid = self._q.get_nowait()
            return rnd, jid
        except queue.Empty:
            return None

    def done(self, jid):
        with self._lock:
            self.pending.discard(jid)

    def close(self):
        """Every website is read: nothing more will be handed over."""
        self.closed.set()


class Progress:
    def __init__(self):
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.reset()

    def reset(self):
        self.d = dict(running=False, source="", page=0, total=None, pages=0, pages_failed=0, jobs=0, dups=0,
                      date_x=0, loc_x=0, title_x=0, type_x=0, review=0, candidate=0, sources_done=0, sources_total=0,
                      task="idle", started=None, finished=None, ai_done=0, ai_total=0, relevant=0, matched=0, ai_msg="",
                      dups_merged=0)
        self.log = deque(maxlen=300)

    def set(self, **k):
        with self.lock:
            self.d.update(k)

    def inc(self, k, n=1):
        with self.lock:
            self.d[k] += n

    def say(self, msg):
        self.log.append(f"{datetime.now():%H:%M:%S} {msg}")

    def snap(self):
        with self.lock:
            return dict(self.d), list(self.log)


def read_outcome(s, url, status, found, listed, st):
    """What a finished reading really gave, and a note when it looks incomplete. 'completed' only says that nothing failed:
    a website read to the end may have shown no offer at all."""
    if status != "completed":
        return "", ""
    whole = not st.stopped_by_date  # a list left early because of the dates is not comparable with a total
    outcome = ("offers_found" if found else "nothing_in_range" if listed
               else "no_openings" if st.no_openings else "no_offers_detected")  # no_openings: the website says it has none
    note = ""
    if whole and st.announced and listed < st.announced * 0.5:
        outcome, note = "partial", f"the website announces {st.announced} offers, {listed} were read"
    mem = s.get(SiteMemory, url)
    if whole:
        if mem and mem.usual >= 20 and listed < mem.usual * 0.2 and not note:
            outcome, note = "partial", f"{listed} offers read, usually about {mem.usual}: the website may have changed"
        elif listed:  # one bad reading must not become the new normal
            if mem:
                mem.usual = max(listed, round(mem.usual * 0.7 + listed * 0.3))
            else:
                s.add(SiteMemory(url=url, usual=listed))
    return outcome, note


def health_note(s, src, preset):
    """This scan of a website against the previous one: a website that worked and now fails, or whose lists suddenly hold
    far fewer offers (same date range), has probably changed (new address, new layout, a block). Runs once per scan, after
    the second tries; this scan becomes the reference for the next one."""
    s.flush()
    mem = s.get(SiteMemory, src.url)
    if mem is None:
        mem = SiteMemory(url=src.url, usual=0)
        s.add(mem)
    status, listed, before, note = src.status, src.listed or 0, mem.last_status or "", ""
    when = f"{mem.last_scan:%Y-%m-%d}" if mem.last_scan else "before"
    if before == "completed" and status in ("blocked", "failed"):
        note = f"read on {when} ({mem.last_listed} offers on its lists), now {status}"
    elif before in ("blocked", "failed") and status == "completed":
        note = f"{before} on {when}, read again now"
    elif before == "completed" and status == "completed" and mem.last_preset == preset and mem.last_listed >= 20 \
            and listed < mem.last_listed * 0.4:
        note = f"{listed} offers on its lists, {mem.last_listed} on {when}: the website may have changed"
    if status != "partially_completed":
        mem.last_status, mem.last_scan, mem.last_listed, mem.last_preset = status, utcnow(), listed, preset
    return note


# ---------------------------------------------------------------------------- websites that did not work, for the user
PROBLEM_ORDER = ("Blocked", "Not reachable", "Page not found", "Website error", "Partly read", "No offer found",
                 "Possibly incomplete", "Changed since the last scan", "Address moved", "Not reached")


def _todo_blocked(why, respect_robots):
    w = why.lower()
    if "robots.txt" in w:
        return ("robots.txt closes it: switch 'Respect robots.txt' off before the scan" if respect_robots else
                "robots.txt closed it in a scan that respected it: it is read at the next scan")
    if "france travail" in w or "jobspipe" in w:
        return "Add the API access described in the reason to api_keys.txt"
    if "welcome to the jungle" in w:
        return "Add the company's own career site instead (<company>.welcomekit.co, or jobs.stationf.co/companies/...)"
    if "workday" in w:
        return "Workday limits repeated reading for some hours: it is read again at the next scan"
    if "linkedin" in w or "indeed" in w or "apec" in w:
        return "Add the companies' own career pages instead"
    return ("The website refuses automated reading (anti-bot page, login or captcha): add the company's page on an open "
            "job platform (SmartRecruiters, Teamtailor, Welcome Kit...), or ask the website to allow your connection")


def site_problem(src, scan_finished=True, respect_robots=True):
    """A website that did not work at the last scan, in the user's words: (problem, why, what to do), or None when it was
    read normally (with or without offers in the date range)."""
    why = re.sub(r"^\s*access_restricted:\s*", "", src.last_error or "").strip()
    st_ = src.status or ""
    if st_ == "blocked":
        return "Blocked", why or "access refused", _todo_blocked(why, respect_robots)
    if st_ == "failed":
        if re.search(r"HTTP 40[4]|HTTP 410", why):
            new = re.search(r"possible new address: (\S+)", why)
            return "Page not found", why, (f"Replace the address with {new[1]}" if new else
                                           "The page no longer exists: find the company's career page and replace the address")
        if re.search(r"source_unreachable|Timeout|resolved|certificate", why, re.I):
            return "Not reachable", why, ("The website did not answer (it was tried twice): check the address in a browser. "
                                          "It is read again at the next scan")
        if re.search(r"HTTP 5\d\d|HTTP 429", why):
            return "Website error", why, "The website had a passing error: it is read again at the next scan"
        if re.search(r"no api key|no ai model|api key", why, re.I):
            return ("Website error", why, "Only the AI can read this website's offers: add an AI key in Settings > API keys "
                                          "(or check that the keys file is next to app.py)")
        return "Website error", why or "reading failed", "It is read again at the next scan; if it keeps failing, check the address"
    if st_ == "partially_completed":
        return "Partly read", why or "the reading stopped early", ("The offers read are kept. The website is large or slow: "
                                                                   "its other offers are read at the next scan")
    if st_ == "not_started":
        return ("Not reached", "the scan stopped before this website (Stop button or 6-hour limit)",
                "Start the scan again") if scan_finished else None
    if src.outcome == "no_offers_detected":
        return ("No offer found", "the page was read but shows no job offer the app can recognise",
                "Open the address: if it shows offers, the website has changed or loads them in another way; else it has none now")
    if src.outcome == "partial":
        return "Possibly incomplete", why, "Open the address and compare: the offers read are kept"
    if src.health and not src.health.endswith("read again now"):
        return "Changed since the last scan", src.health, "Open the address: the website may have a new address or layout"
    moved = re.search(r"page moved: read at (\S+)", why)
    if moved:
        return "Address moved", why, f"Replace the address with {moved[1]}"
    return None


_RETRY = re.compile(r"source_unreachable|HTTP 5\d\d|HTTP 403|HTTP 429", re.I)  # down, refused or rate-limited for a moment


def scan_source(sid, cfg, fetcher, prog, t0):
    with SessionLocal() as s:
        src = s.get(Source, sid)
        label = src.label or src.url
        gate = cfg["_gate"]
        known, st = set(), ScanState()
        kept = restore_recent_offers(s, src, cfg.get("_profile_key"), cfg.get("_cv_key"))
        if kept:  # offers this website showed in the last 24 hours: not read again, not analysed again
            for job in kept:
                g = gate(job.pub_date, job.date_conf or 0.0, job.pub_date_text or "")
                if job.relevance_score is not None and g is False:
                    hide_out_of_range(s, job, "outside the new date range")  # analysed before, now too old for the range
                elif g is None and job.status not in ("out_of_range", "shortlisted") and (since := seen_before_range(s, job, cfg)):
                    hide_out_of_range(s, job, f"not stated by the website, but online since {since}: before the new date range")
            shown = sum(1 for j in kept if j.status != "out_of_range")
            prog.inc("jobs", shown)
            prog.say(f"{label}: {len(kept)} offers kept from the last {RECENT_HOURS} h ({shown} in range): their pages are not "
                     "read again" + (", nor analysed again" if any(j.relevance_score is not None for j in kept) else ""))
        first_day = range_start(cfg)
        for url_, status_, d_, c_, t_, ex_ in s.query(Job.url, Job.status, Job.pub_date, Job.date_conf, Job.pub_date_text,
                                                      Job.extra).filter(Job.source_id == sid):
            if status_ == "out_of_range":
                g = gate(d_, c_ or 0.0, t_ or "")
                if g is False or (g is None and _before((ex_ or {}).get("first_seen"), first_day)):
                    # still outside the (possibly changed) range, by its date or the day it was first seen: never re-opened
                    st.out_known.add(url_)
                    known.add(url_)
            else:
                known.add(url_)
        pages, found, status, err, stale, listed, dated_pages = 0, 0, "completed", "", 0, 0, 0
        stale_max = int(cfg.get("stale_pages", 2))
        prog.say(f"START {label}")
        t_site = time.time()
        # one website's time; the Hellowork search with the profile's keywords reads about 40 lists: three times as long
        site_limit = cfg.get("max_site_duration", 1200) * (3 if normalize_url(src.url) == normalize_url(HW_KEYWORDS_URL) else 1)
        # a website whose robots.txt asks a longer pause between requests (5 s: Neurones, Sii) gets its time stretched to
        # match, up to 3 times: the pause it asks for must never cut its reading short
        try:
            asked = fetcher._robots(src.url).delay if hasattr(fetcher, "_robots") else 0.0
        except Exception:
            asked = 0.0
        site_limit *= min(3.0, max(1.0, asked / max(float(cfg.get("delay") or 1.0), 0.1)))
        st.deadline = t_site + site_limit  # also stops job-page opening inside a list page
        try:
            for pg in crawl_source(fetcher, src.url, cfg, known, st):
                pages += 1
                prog.set(page=pages, total=pg.total, source=label)
                prog.inc("pages")
                if pg.error:
                    prog.inc("pages_failed")
                    status, err = "partially_completed", pg.error
                    LOG.warning("%s | pagination failed: %s", label, pg.error)
                n_in, n_unk, n_out, last = pg.seen, 0, pg.out, None  # last: verdict of the page's last dated offer
                dated = [d for d in (pg.dates if pg.dates is not None else [r.get("date") for r in pg.jobs]) if d]
                st.order += dated
                dated_pages += bool(dated)
                # the website's list proved newest first by its own dates, ACROSS its pages: Randstad Digital sorts each page
                # by itself (page 1 from 10 to 4 November, page 2 from 10 November again: 25 offers missed)
                in_order = date_order(st.order) is True and dated_pages >= 2
                prog.inc("date_x", pg.out)
                listed += len(pg.jobs) + pg.out + pg.seen
                for k, raw in enumerate(distrust_shared_dates(pg.jobs), 1):
                    if k % 50 == 0:
                        # websites are read in parallel and share one database: a platform page can hold every offer at
                        # once (OpenAI: 813), and writing them in one transaction kept the other websites waiting past the
                        # database's lock timeout ('database is locked' inside an autoflush)
                        s.commit()
                    if not raw.get("title"):
                        continue
                    raw = refine_raw_date(dict(raw))
                    verdict = gate(raw["date"], raw["date_conf"], raw["date_text"])
                    if raw["date"]:
                        last = verdict
                    if verdict is False:  # DATE FIRST: outside the range -> hidden record only, not counted, not analysed
                        n_out += 1
                        prog.inc("date_x")
                        if raw.get("url"):
                            st.out_known.add(normalize_url(raw["url"]))
                        upsert_job(s, src, raw, hidden=True)
                        continue
                    n_in, n_unk = n_in + (verdict is True), n_unk + (verdict is None)
                    found += 1
                    prog.inc("jobs" if upsert_job(s, src, raw) else "dups")
                s.commit()
                prog.say(f"{label} - page {pages}" + (f" of {pg.total}" if pg.total else "") +
                         f": {n_in + n_unk} in range, {n_out} too old")
                # the PUBLICATION DATE decides when the website is finished, not the number of pages
                if n_in == 0 and n_unk == 0 and n_out > 0 and pg.ordered and in_order:
                    stale += 1
                elif n_in or n_unk:
                    stale = 0
                if pg.sorted and in_order and cfg["date_preset"] != "All dates" and (last is False or (n_out and not n_in and not n_unk)):
                    st.stopped_by_date = True  # the website sorts by date: everything after this offer is older
                    prog.say(f"{label}: the website's newest-first list reached offers older than the range -> website finished")
                    break
                if stale >= stale_max and cfg["date_preset"] != "All dates":
                    st.stopped_by_date = True
                    prog.say(f"{label}: {stale} page(s) without any job inside the range -> website finished")
                    break
                if prog.cancel.is_set() or time.time() - t0 > cfg["max_duration"]:
                    status = "partially_completed"
                    break
                if time.time() - t_site > site_limit:  # one website never holds a whole run
                    status, err = "partially_completed", "time limit for this website reached"
                    break
        except AccessError as e:
            status, err = "blocked", str(e)
            LOG.warning("%s | %s", label, err)
        except Exception as e:
            # nothing read at all (its job platform down, the fallbacks found nothing): failed, so it is read again
            status, err = ("failed" if pages == 0 or not (found or listed) else "partially_completed"), str(e)
            LOG.warning("%s | failed: %s", label, err)
        if status == "completed" and st.truncated:
            status, err = "partially_completed", "max pages limit reached"
        elif status == "completed" and st.details_capped:
            err = f"{cfg['max_details_per_source']} job pages opened (limit): the other offers keep their list data"
        s.rollback()
        src = s.get(Source, sid)
        listed += st.skipped + st.abroad
        prog.inc("loc_x", st.abroad)  # the 'Not France / not wanted' tile counts them, though their page was never opened
        if st.abroad:
            prog.say(f"{label}: {st.abroad} offers placed outside France on the list: not opened")
        if st.moved:  # the page moved: read at the address its home page links, which the user should write instead
            err = f"{err}; " if err else ""
            err += f"page moved: read at {st.moved} (update this website's address)"
        src.status, src.last_scan, src.pages_visited, src.jobs_found = status, utcnow(), pages, found
        src.last_error, src.platform, src.listed = err, st.platform, listed
        src.outcome, note = read_outcome(s, src.url, status, found, listed, st)
        if note:
            src.last_error = f"{err}; {note}" if err else note
        s.commit()
        prog.say(f"END {label}: {status} {err}")


MERGE_EVERY = 120  # seconds: the duplicates are merged this often while websites are read, then once all are read


def run_scan(source_ids, cfg, profile, prog: Progress, then=None, resume=None):
    """Crawl every page of every source and apply the date / France / title filters to each website as soon as it is read.
    then(prog, feed): the AI analysis. It starts with the scan and receives each website's remaining offers through `feed`
    while the next websites are being read; it ends when the feed is closed (every website read) and empty.
    resume: an unfinished scan (store.unfinished_scan); source_ids are then its websites still to read, and the offers its
    other websites gave that the AI has not analysed yet are analysed first."""
    cfg = {**run_config(cfg), "_profile": profile}  # the crawler uses the profile to open relevant / doubtful jobs first
    cfg["_gate"] = make_gate(cfg)  # date_decision for the chosen range: True inside | False outside | None unknown
    prog.reset()
    prog.cancel.clear()
    lock_scan()
    prog.set(running=True, started=datetime.now(), sources_total=len(source_ids), task="crawling")
    t0 = time.time()
    before = [x for x in (resume or {}).get("source_ids") or [] if x not in set(source_ids)]  # read before the resume
    with SessionLocal() as s:  # an analysis / a CV match kept from an earlier scan is reused only for the same profile / CVs
        cfg["_profile_key"], cfg["_cv_key"] = profile_key(profile), cv_key(s)
        scan_started(s, source_ids, cfg, profile, resume)
        if resume:
            read = s.query(Source).filter(Source.id.in_(before)).all()
            prog.set(sources_total=len(source_ids) + len(read), sources_done=len(read),
                     jobs=sum(x.jobs_found or 0 for x in read))
            for j in s.query(Job).filter(Job.source_id.in_(before), Job.relevance_score.isnot(None),
                                         Job.status.notin_(["out_of_range", "shortlisted"])):
                # resumed another day: an offer analysed then may now be older than the range
                g = cfg["_gate"](j.pub_date, j.date_conf or 0.0, j.pub_date_text or "")
                if g is False or (g is None and seen_before_range(s, j, cfg)):
                    hide_out_of_range(s, j, "outside the date range when the scan was resumed")
            prog.say(f"scan resumed: {len(read)} websites already read, {len(source_ids)} to read; the offers not analysed "
                     "yet go to the AI first")
    feed, filtering, handed, merged_at = Feed(), threading.Lock(), set(), [time.time()]

    def hand_over(sids=None):
        """The offers of websites just read (sids; None: every website, once all are read) go through the filters, and
        the remaining ones to the AI. One website at a time: two filters never judge the same duplicates at once."""
        with filtering, SessionLocal() as s:
            fresh = [x for x in (sids if sids is not None else source_ids) if x not in handed]
            if fresh:  # offers kept from an earlier scan, filtered but not analysed: filtered again with this scan's settings
                reset_filters(s, fresh)
                handed.update(fresh)
            merge = sids is None or time.time() - merged_at[0] > MERGE_EVERY
            st = apply_filters(s, cfg, profile, source_ids=sids, merge=merge)
            if merge:
                merged_at[0] = time.time()
            for k, v in st.items():
                prog.inc(k, v)
            if then:
                q = s.query(Job).filter(Job.status.in_(["candidate", "review"]), Job.relevance_score.is_(None))
                if sids is not None:
                    q = q.filter(Job.source_id.in_(list(sids)))
                prog.inc("ai_total", feed.put(q.all()))

    def hand_over_safe(sids):
        try:
            hand_over(sids)
        except Exception as e:  # pragma: no cover - the final hand-over takes these offers
            prog.say(f"filters of website {sids} postponed to the end of the reading: {e}")

    # the filters run in their own thread (about 1 s per website, 60 ms per offer): the readers go on to the next website
    filters = ThreadPoolExecutor(max_workers=1, thread_name_prefix="filters")

    def one(sid):  # websites are read in parallel (the polite delay is per host); each one has its own HTTP session
        if prog.cancel.is_set() or time.time() - t0 > cfg["max_duration"]:
            return
        try:
            scan_source(sid, cfg, Fetcher(cfg, prog.say), prog, t0)
        except Exception as e:  # pragma: no cover - scan_source records its own errors
            prog.say(f"website {sid} crashed: {e}")
        prog.inc("sources_done")
        filters.submit(hand_over_safe, [sid])

    def again(sid):  # a website that was down, or refused once, gets one more calm try after the others
        if prog.cancel.is_set() or time.time() - t0 > cfg["max_duration"]:
            return
        calm = {**cfg, "timeout": cfg["timeout"] * 2, "retries": 0}
        scan_source(sid, calm, Fetcher(calm, prog.say), prog, t0)
        filters.submit(hand_over_safe, [sid])

    def explain_dead(sid):  # the page is gone: the website's home page may link its new address
        with SessionLocal() as s:
            src = s.get(Source, sid)
            page = suggest_page(Fetcher(cfg, prog.say), src.url)
            if page:
                src.last_error = f"{src.last_error}; possible new address: {page}"
                s.commit()

    def analyse():
        try:
            then(prog, feed)
        except Exception as e:  # never crash silently
            prog.say(f"FATAL (AI analysis) {e}")

    ai = threading.Thread(target=analyse, name="ai-analysis", daemon=True) if then else None
    if not cfg.get("respect_robots", True):
        prog.say("robots.txt is not followed during this scan (setting 'Respect robots.txt' off); anti-bot pages, logins "
                 "and captchas are still never bypassed")
    if ai:
        ai.start()
        prog.say("the AI analyses each website's offers as soon as the website is read")
    if before:  # a resumed scan: the offers of the websites read before, not analysed yet, are filtered and analysed first
        filters.submit(hand_over_safe, before)
    try:
        with ThreadPoolExecutor(max_workers=int(cfg["parallel_sites"])) as ex:
            list(ex.map(one, source_ids))
            with SessionLocal() as s:
                todo = [x.id for x in s.query(Source).filter(Source.id.in_(source_ids), Source.status.in_(["failed", "blocked"]))
                        if _RETRY.search(x.last_error or "")]
            prog.say(f"second try for {len(todo)} websites")
            list(ex.map(again, todo))
            with SessionLocal() as s:
                dead = [x.id for x in s.query(Source).filter(Source.id.in_(source_ids), Source.status == "failed")
                        if re.search(r"HTTP (404|410|5\d\d)", x.last_error or "")]
            list(ex.map(explain_dead, dead))
        with SessionLocal() as s:
            for src in s.query(Source).filter(Source.id.in_(source_ids)):
                src.health = health_note(s, src, cfg["date_preset"])
            s.commit()
        prog.set(task="filtering")
        filters.shutdown(wait=True)
        hand_over(None)  # every offer still waiting, and the duplicates of every website merged
    except Exception as e:  # never crash silently
        prog.say(f"FATAL {e}")
    finally:
        filters.shutdown(wait=True)
        feed.close()
        if ai:
            if ai.is_alive():
                prog.set(task="AI analysis")
            ai.join()
        try:  # the offers read in the last 24 hours, with their analysis, are kept for the next scans (even a stopped one)
            with SessionLocal() as s:
                n = keep_recent_offers(s)
            prog.say(f"{n} offers of the last {RECENT_HOURS} h kept for the next scans")
        except Exception as e:  # pragma: no cover - keeping them never fails a scan
            LOG.warning("offers not kept: %s", e)
        try:  # finished, or not: what is left is offered to resume at the next launch (or now, on the Search page)
            with SessionLocal() as s:
                why = ["stopped with the Stop button"] if prog.cancel.is_set() else []
                if time.time() - t0 > cfg["max_duration"]:
                    why.append(f"time limit of {cfg['max_duration'] / 3600:g} hours reached")
                if then and prog.snap()[0]["ai_msg"]:
                    why.append(prog.snap()[0]["ai_msg"])  # 'AI analysis stopped: ...' (credits, quotas, no key)
                todo, _, pending = scan_todo(s, scan_record(s))
                pending = pending if then else 0  # without the AI analysis, no offer waits for it
                if not why:  # no stop, no time limit, no AI message: what is left says it
                    why = ([f"{len(todo)} websites not read"] if todo else []) + \
                          ([f"{pending} offers not analysed (no AI model answered)"] if pending else [])
                scan_ended(s, "; ".join(why) if todo or pending else "")
                if todo or pending:
                    prog.say(f"scan not finished ({'; '.join(why)}): {len(todo)} websites to read, {pending} offers to "
                             "analyse - it can be resumed")
        except Exception as e:  # pragma: no cover - the record never fails a scan
            LOG.warning("scan state not saved: %s", e)
        unlock_scan()
        prog.set(running=False, finished=datetime.now(), task="done")
