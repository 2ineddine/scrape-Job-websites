"""scan.py - the search engine: crawl every website, store the offers, then filter them DATE FIRST -> France -> title."""
from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .adapters import ScanState, distrust_shared_dates, make_gate
from .classify import analyze_title, classify_location, clean_title, location_from_text
from .config import LOG, run_config, utcnow
from .crawler import crawl_source
from .dates import date_decision, refine_job_date, refine_raw_date
from .store import Job, SessionLocal, Source
from .web import AccessError, Fetcher, normalize_url


# --------------------------------------------------------------------------- persistence / filters
def upsert_job(s, src, raw, hidden=False) -> bool:
    """Store a job. hidden=True: the job is OUTSIDE the date range - only a tiny record is kept (never shown, never analysed)
    so that the website is not re-opened for it at the next search."""
    raw = refine_raw_date(dict(raw))  # an explicit 'Date de publication' in the description beats a feed / refresh date
    original_title, raw["title"] = raw["title"], clean_title(raw["title"])  # 'Ingénieur IA H/F (REQ123)' -> 'Ingénieur IA'
    if not raw["location"] and raw["description"]:
        raw["location"] = location_from_text(raw["description"])
    url = normalize_url(raw["url"]) if raw.get("url") else ""
    ident = url or f"{src.id}:{raw.get('platform_id')}:{raw['title']}"
    uid = hashlib.sha1(ident.encode()).hexdigest()[:16]
    desc = "" if hidden else raw["description"]
    chash = hashlib.sha1(f"{raw['title']}|{raw['location']}|{raw['description'][:3000]}".encode()).hexdigest()
    job = s.query(Job).filter_by(uid=uid).first()
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
                     "original_title": original_title}))
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
        d, why = date_decision(job.pub_date, job.date_conf, cfg, now, job.pub_date_text)
        if d is False:  # DATE FIRST: outside the range -> hidden, never analysed, never shown (relevant or not)
            hide_out_of_range(s, job, why)
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
        ti = analyze_title(job.title, ex.get("department"), profile, job.employment_type)
        job.extra = {**ex, "title_info": ti.as_dict()}  # reassigned so the JSON change is saved
        verdict, twhy = ti.verdict, ti.reason
        if verdict == "reject":
            job.status, job.reason = "excluded", "title: " + twhy
            st["title_x"] += 1
            continue
        job.status = "review" if flags else "candidate"
        job.review_flags, job.reason = ",".join(flags), f"title: {twhy}; date: {why}"
        st["review" if flags else "candidate"] += 1
    s.commit()
    return dict(st)


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
                      date_x=0, loc_x=0, title_x=0, review=0, candidate=0, sources_done=0, sources_total=0,
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
        pages, found, status, err, stale = 0, 0, "completed", "", 0
        stale_max = int(cfg.get("stale_pages", 2))
        prog.say(f"START {label}")
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
        src.status, src.last_scan, src.pages_visited, src.jobs_found = status, utcnow(), pages, found
        src.last_error, src.platform = err, st.platform
        s.commit()
        prog.say(f"END {label}: {status} {err}")


def run_scan(source_ids, cfg, profile, prog: Progress, then=None):
    """Crawl every page of every source, apply date / France / title filters, then optionally run `then(prog)`."""
    cfg = {**run_config(cfg), "_profile": profile}  # the crawler uses the profile to open relevant / doubtful jobs first
    cfg["_gate"] = make_gate(cfg)  # date_decision for the chosen range: True inside | False outside | None unknown
    prog.reset()
    prog.cancel.clear()
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

    try:
        with ThreadPoolExecutor(max_workers=int(cfg["parallel_sites"])) as ex:
            list(ex.map(one, source_ids))
        prog.set(task="filtering")
        with SessionLocal() as s:
            reset_filters(s)
            prog.set(**apply_filters(s, cfg, profile))
        if then and not prog.cancel.is_set():
            then(prog)
    except Exception as e:  # never crash silently
        prog.say(f"FATAL {e}")
    finally:
        prog.set(running=False, finished=datetime.now(), task="done")
