"""scan.py - the search engine: crawl every website, store the offers, then filter them DATE FIRST -> France -> title."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlparse

from .adapters import ScanState, distrust_shared_dates, make_gate
from .classify import (analyze_title, classify_location, clean_title, is_boilerplate_title, location_from_text,
                       looks_like_date, offer_kinds_heading)
from .config import LOG, run_config, utcnow
from .crawler import crawl_source, suggest_page
from .dates import date_decision, refine_job_date, refine_raw_date
from .store import Job, SeenOffer, SessionLocal, SiteMemory, Source, lock_scan, unlock_scan
from .web import AccessError, Fetcher, norm, normalize_url


# --------------------------------------------------------------------------- persistence / filters
def upsert_job(s, src, raw, hidden=False) -> bool:
    """Store a job. hidden=True: the job is OUTSIDE the date range - only a tiny record is kept (never shown, never analysed)
    so that the website is not re-opened for it at the next search."""
    raw = refine_raw_date(dict(raw))  # an explicit 'Date de publication' in the description beats a feed / refresh date
    original_title, raw["title"] = raw["title"], clean_title(raw["title"])  # 'Ingénieur IA H/F (REQ123)' -> 'Ingénieur IA'
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


def hide_out_of_range(s, job, why):
    """The job's real publication date is outside the range: it leaves every list, is never analysed and never shown
    (relevant or not). A tiny record stays so that the website is not re-opened for it."""
    job.status, job.reason, job.description = "out_of_range", "date: " + why, ""
    job.relevance_score, job.relevance_json, job.match_score, job.match_json = None, None, None, None
    s.commit()


def apply_filters(s, cfg, profile, now=None):
    """Pipeline stages 5-7: date -> France location -> fast title filter. Records reason for every outcome."""
    now, st = now or utcnow(), Counter()
    for job in s.query(Job).filter(Job.status == "new").all():
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
        seen = (ex.get("first_seen") or "") if d is None else ""
        if seen and date_decision(datetime.strptime(seen, "%Y-%m-%d"), 1.0, cfg, now)[0] is False:
            hide_out_of_range(s, job, f"no date, but already online on {seen}")  # published that day at the latest
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
        if loc["location_verified"] and not loc["france_eligible"]:
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


def reset_filters(s):
    n = s.query(Job).filter(Job.status.in_(["excluded", "review", "candidate"]), Job.relevance_score.is_(None)) \
        .update({Job.status: "new"}, synchronize_session=False)
    s.commit()
    return n


class Progress:
    def __init__(self):
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.reset()

    def reset(self):
        self.d = dict(running=False, source="", page=0, total=None, pages=0, pages_failed=0, jobs=0, dups=0,
                      date_x=0, loc_x=0, title_x=0, type_x=0, review=0, candidate=0, sources_done=0, sources_total=0,
                      task="idle", started=None, finished=None, ai_done=0, ai_total=0, relevant=0, matched=0, ai_msg="")
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
    outcome = "offers_found" if found else "nothing_in_range" if listed else "no_offers_detected"
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


_RETRY = re.compile(r"source_unreachable|HTTP 5\d\d|HTTP 403", re.I)


def scan_source(sid, cfg, fetcher, prog, t0):
    with SessionLocal() as s:
        src = s.get(Source, sid)
        label = src.label or src.url
        gate = cfg["_gate"]
        known, st = set(), ScanState()
        for url_, status_, d_, c_, t_ in s.query(Job.url, Job.status, Job.pub_date, Job.date_conf, Job.pub_date_text).filter(Job.source_id == sid):
            if status_ == "out_of_range":
                if gate(d_, c_ or 0.0, t_ or "") is False:  # still outside the (possibly changed) range: never re-opened
                    st.out_known.add(url_)
                    known.add(url_)
            else:
                known.add(url_)
        pages, found, status, err, stale, listed = 0, 0, "completed", "", 0, 0
        stale_max = int(cfg.get("stale_pages", 2))
        prog.say(f"START {label}")
        t_site = time.time()
        st.deadline = t_site + cfg.get("max_site_duration", 1200)  # also stops job-page opening inside a list page
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
                prog.inc("date_x", pg.out)
                listed += len(pg.jobs) + pg.out + pg.seen
                for raw in distrust_shared_dates(pg.jobs):
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
                if n_in == 0 and n_unk == 0 and n_out > 0 and pg.ordered:
                    stale += 1
                elif n_in or n_unk:
                    stale = 0
                if pg.sorted and cfg["date_preset"] != "All dates" and (last is False or (n_out and not n_in and not n_unk)):
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
                if time.time() - t_site > cfg.get("max_site_duration", 1200):  # one website never holds a whole run
                    status, err = "partially_completed", "time limit for this website reached"
                    break
        except AccessError as e:
            status, err = "blocked", str(e)
            LOG.warning("%s | %s", label, err)
        except Exception as e:
            status, err = ("failed" if pages == 0 else "partially_completed"), str(e)
            LOG.warning("%s | failed: %s", label, err)
        if status == "completed" and st.truncated:
            status, err = "partially_completed", "max pages limit reached"
        elif status == "completed" and st.details_capped:
            err = f"{cfg['max_details_per_source']} job pages opened (limit): the other offers keep their list data"
        s.rollback()
        src = s.get(Source, sid)
        listed += st.skipped
        src.status, src.last_scan, src.pages_visited, src.jobs_found = status, utcnow(), pages, found
        src.last_error, src.platform, src.listed = err, st.platform, listed
        src.outcome, note = read_outcome(s, src.url, status, found, listed, st)
        if note:
            src.last_error = f"{err}; {note}" if err else note
        s.commit()
        prog.say(f"END {label}: {status} {err}")


def run_scan(source_ids, cfg, profile, prog: Progress, then=None):
    """Crawl every page of every source, apply date / France / title filters, then optionally run `then(prog)`."""
    cfg = {**run_config(cfg), "_profile": profile}  # the crawler uses the profile to open relevant / doubtful jobs first
    cfg["_gate"] = make_gate(cfg)  # date_decision for the chosen range: True inside | False outside | None unknown
    prog.reset()
    prog.cancel.clear()
    lock_scan()
    prog.set(running=True, started=datetime.now(), sources_total=len(source_ids), task="crawling")
    t0 = time.time()

    def one(sid):  # websites are read in parallel (the polite delay is per host); each one has its own HTTP session
        if prog.cancel.is_set() or time.time() - t0 > cfg["max_duration"]:
            return
        try:
            scan_source(sid, cfg, Fetcher(cfg, prog.say), prog, t0)
        except Exception as e:  # pragma: no cover - scan_source records its own errors
            prog.say(f"website {sid} crashed: {e}")
        prog.inc("sources_done")

    def again(sid):  # a website that was down, or refused once, gets one more calm try after the others
        if prog.cancel.is_set() or time.time() - t0 > cfg["max_duration"]:
            return
        calm = {**cfg, "timeout": cfg["timeout"] * 2, "retries": 0}
        scan_source(sid, calm, Fetcher(calm, prog.say), prog, t0)

    def explain_dead(sid):  # the page is gone: the website's home page may link its new address
        with SessionLocal() as s:
            src = s.get(Source, sid)
            page = suggest_page(Fetcher(cfg, prog.say), src.url)
            if page:
                src.last_error = f"{src.last_error}; possible new address: {page}"
                s.commit()

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
        with SessionLocal() as s:
            reset_filters(s)
            prog.set(**apply_filters(s, cfg, profile))
        if then and not prog.cancel.is_set():
            then(prog)
    except Exception as e:  # never crash silently
        prog.say(f"FATAL {e}")
    finally:
        unlock_scan()
        prog.set(running=False, finished=datetime.now(), task="done")
