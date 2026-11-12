"""analysis.py - the AI reads each remaining offer: publication date, location, kind of role and relevance.

Dates: the AI does not write a date. It receives the NUMBERED list of every date found in the text, each with its role
(published / updated / deadline / start, see dates.date_candidates) and answers the number of the publication date.
Its answer is then checked against the text. Locations and role are accepted only with a verbatim quote from the page.
Anything the AI cannot prove stays in 'To check'."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

from .classify import OFFER_TYPES, analyze_title, wanted_types
from .config import run_config, utcnow
from .cv import _ws, match_job
from .dates import NO_DATE, date_candidates, date_decision, describe_candidates, parse_date, refine_job_date, text_publication_date
from .llm import (WEAK_LEVEL, AIError, BudgetExceeded, MalformedOutput, QuotaExhausted, RequestTooLarge, call_json,
                  health_report, last_step, level, require_cv_consent, reset_usage)
from .scan import Feed, hide_out_of_range, merge_duplicates, offer_priority
from .store import CVDocument, Job, SessionLocal, cv_key, profile_key
from .trim import essential_text
from .web import Fetcher


class JobAnalysis(BaseModel):
    """What the AI reads from the job page text. Evidence fields must be verbatim excerpts."""
    title: str = ""
    offer_kind: Literal["job", "research_topic"] = "job"
    role_type: Literal["research_ml", "engineering_software", "data", "sales_business", "admin_support", "other"] = "other"
    contract_kind: Literal["job", "internship", "apprenticeship", "phd_postdoc", "unknown"] = "unknown"
    contract_evidence: str = ""  # the words of the text that state the contract ('CDI', 'Stage de 6 mois')
    research_topics: list[str] = []
    location_text: str = ""
    country_code: str | None = None
    france_eligible: bool | None = None  # True: in France / remote open to France; False: elsewhere; None: unknown
    location_evidence: str = ""
    publication_date_choice: int | None = None  # number of the publication date in the candidate list
    publication_date: str | None = None         # YYYY-MM-DD, only when the date is not in the list
    date_evidence: str = ""
    is_relevant: bool
    relevance_score: float = Field(ge=0, le=100)
    reasoning_summary: str = ""
    analysis_confidence: float = Field(ge=0, le=1)
    needs_review: bool = False
    # market facts for the statistics page (same call, no extra request)
    skills: list[str] = []  # tools, languages, methods the offer asks for
    degree: Literal["phd", "master", "engineer", "bachelor", "none", "unknown"] = "unknown"
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unknown"] = "unknown"
    languages: list[str] = []
    remote: Literal["onsite", "hybrid", "remote", "unknown"] = "unknown"
    salary_text: str = ""  # verbatim, when the offer states one

    @model_validator(mode="before")
    @classmethod
    def _tolerant(cls, d):
        """Small models answer 'internship' for offer_kind or null for a text: such slips are corrected, not fatal."""
        if not isinstance(d, dict):
            return d
        d = dict(d)
        if d.get("offer_kind") not in ("job", "research_topic"):
            d["offer_kind"] = "research_topic" if "research" in str(d.get("offer_kind", "")).lower() else "job"
        if d.get("contract_kind") not in get_args(cls.model_fields["contract_kind"].annotation):
            k = str(d.get("contract_kind") or "").lower()
            d["contract_kind"] = next((v for w, v in (("intern", "internship"), ("stage", "internship"), ("altern", "apprenticeship"),
                                                      ("apprent", "apprenticeship"), ("phd", "phd_postdoc"), ("post", "phd_postdoc"),
                                                      ("these", "phd_postdoc"), ("cdi", "job"), ("cdd", "job")) if w in k), "unknown")
        if d.get("role_type") not in get_args(cls.model_fields["role_type"].annotation):
            d["role_type"] = "other"
        for k in ("title", "location_text", "location_evidence", "date_evidence", "reasoning_summary", "contract_evidence"):
            if d.get(k) is None:
                d[k] = ""
        if d.get("research_topics") is None:
            d["research_topics"] = []
        for k in ("skills", "languages"):  # market facts: never fatal
            v = d.get(k)
            d[k] = [str(x)[:40] for x in v][:15] if isinstance(v, list) else []
        for k in ("degree", "seniority", "remote"):
            if d.get(k) not in get_args(cls.model_fields[k].annotation):
                d[k] = "unknown"
        d["salary_text"] = str(d.get("salary_text") or "")[:120]
        return d


def _exclude(s, job, res, reason):
    job.status, job.reason = "excluded", reason
    job.relevance_score, job.relevance_json = res.relevance_score, res.model_dump()  # marks the job as analysed
    s.commit()
    return res


def build_prompt(job, profile, text, cands, ti, second=False):
    want = wanted_types(profile)
    wanted = ", ".join(OFFER_TYPES[k] for k in OFFER_TYPES if k in want)
    unwanted = ", ".join(OFFER_TYPES[k] for k in OFFER_TYPES if k not in want) or "nothing"
    return (
        ("INDEPENDENT SECOND READING of this offer: judge the work described from the text alone.\n" if second else "") +
        f"Candidate research profile: {profile['description']}\nTarget topics: {', '.join(profile['keywords'][:150])}\n"
        "The candidate wants: offers located in FRANCE (or remote and explicitly open to France), published recently; "
        "research / applied-science / machine-learning / deep-learning / vision / audio-speech / signal / robotics work. "
        "Offers may be written in French or English. An offer can be a classic job title or a RESEARCH SUBJECT (the title "
        "states a scientific problem, e.g. 'Apprentissage profond pour la segmentation d'images médicales'): judge both by "
        "the work described. "
        f"WANTED kinds of offer: {wanted}. NOT wanted: {unwanted}; and sales, business-development, marketing, management, admin or support roles even "
        "if the title mentions AI. A software title can qualify if the work is closely related to the profile.\n"
        f"Title analysis by rules: kind={ti.kind}, role='{ti.role}', topics={ti.topics}, contract='{ti.contract}'.\n"
        "PUBLICATION DATE: below is the numbered list of every date written in the offer, with its role. Answer "
        "publication_date_choice = the NUMBER of the date on which the offer was published or posted. Never choose a "
        "deadline, start, update or expiry date. An explicit 'Date de publication' / 'Posted on' / 'Publié le' / 'Mise en "
        "ligne' wins over an unlabelled date. If no listed date is the publication date, answer null.\n"
        f"Today's date is {utcnow():%Y-%m-%d}.\nDates found in the text:\n{describe_candidates(cands)}\n"
        "LOCATION: location_evidence must be an EXACT excerpt copied from the text (max 120 chars). france_eligible: true "
        "only if the job itself is in France or remote with France explicitly eligible; false if located elsewhere or "
        "remote restricted to another country; null if unknown. A company's offices elsewhere are NOT the job location.\n"
        f"Already extracted by simple rules (may be empty or wrong): title='{job.title}', location='{job.location_original}', "
        f"published='{job.pub_date}'.\n<untrusted>\n{text or '(page text missing)'}\n</untrusted>\n"
        "contract_kind: job (CDI, CDD, permanent, fixed-term, freelance), internship (stage, internship, master project), "
        "apprenticeship (alternance, apprentissage), phd_postdoc (thesis, PhD, doctoral contract, post-doc), unknown. Answer "
        "the kind the TEXT STATES; when the text names no contract, answer unknown: never guess it from the role or the "
        "seniority. contract_evidence = the EXACT words of the text that state it (e.g. 'CDI', 'Stage de 6 mois'), else empty.\n"
        "MARKET FACTS (from the text only): skills = up to 12 short names of the tools, programming languages, frameworks and "
        "methods the offer asks for (e.g. 'Python', 'PyTorch', 'C++', 'ROS', 'computer vision', 'Kubernetes'); degree = the "
        "required degree (phd|master|engineer|bachelor|none|unknown); seniority (intern|junior|mid|senior|lead|unknown); "
        "languages = spoken languages required (e.g. 'French', 'English'); remote (onsite|hybrid|remote|unknown); "
        "salary_text = the salary as written in the offer, else empty.\n"
        "JSON keys: title, offer_kind(job|research_topic), contract_kind(job|internship|apprenticeship|phd_postdoc|unknown), contract_evidence, "
        "role_type(research_ml|engineering_software|data|sales_business|admin_support|other), research_topics[], location_text, country_code, "
        "france_eligible(true|false|null), location_evidence, publication_date_choice(number|null), date_evidence, "
        "is_relevant, relevance_score(0-100), reasoning_summary, analysis_confidence(0-1), needs_review, "
        "skills[], degree, seniority, languages[], remote, salary_text")


def _ai_date(res, cands, text):
    """-> (date, explicit) chosen by the AI, verified; (None, False) when unproven."""
    hit = dict(cands).get(res.publication_date_choice) if res.publication_date_choice else None
    if hit is not None and hit.kind in ("pub", "none"):
        return hit.date, hit.kind == "pub"
    # fallback for models that wrote a date: accepted only if the quote is in the text and says that date
    proven = bool(res.date_evidence) and len(_ws(res.date_evidence)) >= 4 and _ws(res.date_evidence) in _ws(text)
    if proven and re.fullmatch(r"\d{4}-\d{2}-\d{2}", res.publication_date or ""):
        try:
            cand = datetime.strptime(res.publication_date, "%Y-%m-%d")
        except ValueError:
            return None, False
        said, _ = parse_date(res.date_evidence)
        if said is not None and said.date() == cand.date():
            td = text_publication_date(res.date_evidence)[0]  # is the quote really a PUBLICATION statement?
            return cand, td is not None and td.date() == cand.date()
    return None, False


def analyze_job(s, job: Job, profile, cfg, keys=None, second=False):
    """AI reads the job text: publication date, location, role type and relevance in ONE call.
    Evidence is verified against the text; anything unproven stays in 'To check'.
    The AI reads the offer WITHOUT its boilerplate (company presentation, benefits, forms: trim.py); the dates it chooses
    from and every quote it gives are checked against the FULL text."""
    full = job.description or ""
    text = essential_text(full, int(cfg.get("job_text_chars", 9000)), job.employer or "", job.location_original or "")
    ex = job.extra or {}
    ti = analyze_title(job.title, ex.get("department"), profile, job.employment_type)
    cands = date_candidates(full)
    res = call_json(build_prompt(job, profile, text, cands, ti, second), JobAnalysis, cfg, s, keys)
    step = last_step()
    if step:  # which model read it: a small model's reading is read again by a better one when one is free
        job.extra = {**(job.extra or {}), "ai_model": f"{step[0]} / {step[1]}", "ai_level": level(step)}
    # the offer type the TITLE states ('Stage - Data Scientist', 'Post-doc', 'CDI') beats the AI's: small models read
    # 'Stage' as a regular job, and an internship-only profile would then lose the offer
    stated = ti.training or ("job" if ti.contract in ("cdi", "cdd", "freelance", "interim") else "")
    if stated and res.contract_kind != stated:
        res.contract_kind = stated
    ev = _ws(res.contract_evidence)
    if not stated and res.contract_kind != "unknown" and not (
            len(ev) >= 3 and re.search(rf"(?<!\w){re.escape(ev)}(?!\w)", _ws(f"{job.title} {full}"))):
        # a contract the offer does not state is a guess (free models answer 'job' by default): the offer is kept, its
        # contract 'not stated', instead of being dropped as a kind of offer the user does not want
        res.contract_kind = "unknown"
    elif not stated and res.contract_kind != "unknown":
        # the quote must say what the AI answers: 'contrat CDD 6 mois' quoted for an 'internship' is a job (a research
        # engineer's offer was set aside as an internship). The app's own reading of the quote wins.
        q = analyze_title(res.contract_evidence)
        said = q.training or ("job" if q.contract in ("cdi", "cdd", "freelance", "interim") else "")
        if said and said != res.contract_kind and not (said == "job" and res.contract_kind == "phd_postdoc"):
            res.contract_kind = said  # 'contrat CDD 6 mois' quoted for an internship: a job (a post-doc is a CDD too)
    proven = lambda ev: bool(ev) and len(_ws(ev)) >= 4 and _ws(ev) in _ws(full)
    flags = []
    # ---- publication date: deterministic text evidence first, then the AI's choice among the dates of the text
    refine_job_date(job)  # an explicit 'Date de publication : ...' in the description beats a feed / refresh date
    ai_date, explicit = _ai_date(res, cands, full)
    cur = (job.pub_date, job.date_conf or 0.0, job.date_source or "", job.pub_date_text or "") if job.pub_date else NO_DATE
    if ai_date and (cur[0] is None or cur[1] < 0.5):
        cur = (ai_date, 0.8 if explicit else 0.65, "AI (chosen among the dates of the text)", res.date_evidence or f"{ai_date:%Y-%m-%d}")
    elif ai_date and abs(ai_date - cur[0]) > timedelta(days=1):  # the AI points at another date than the stored one
        if "job description" not in cur[2] and explicit:
            cur = (ai_date, 0.8, f"AI (labelled publication date in the text; replaces {cur[2]} {cur[0]:%Y-%m-%d})",
                   res.date_evidence or f"{ai_date:%Y-%m-%d}")
        else:  # cannot settle it: keep the EARLIEST (refresh dates are later) but mark it unsure -> 'To check'
            cur = (min(ai_date, cur[0]), 0.45, f"{cur[2]} (conflict with AI date {ai_date:%Y-%m-%d})", cur[3])
            flags.append("publication_date_conflict")
    job.pub_date, job.date_conf, job.date_source, job.pub_date_text = cur
    ok, why = date_decision(job.pub_date, job.date_conf, cfg, None, job.pub_date_text)
    if ok is False:  # outside the range: the job disappears (never shown, relevant or not)
        hide_out_of_range(s, job, why)
        return res
    if ok is None and cfg["unknown_date_policy"] == "exclude":
        return _exclude(s, job, res, "date: " + why)
    if ok is None and cfg["unknown_date_policy"] == "review":
        flags.append("publication_date_unknown")
    # ---- location
    if job.location_verified and job.france_eligible:
        if res.france_eligible is False and proven(res.location_evidence):
            flags.append("location_conflict")
    elif proven(res.location_evidence) and res.france_eligible is True:
        job.france_eligible, job.location_verified, job.country_code = True, True, "FR"
        job.location_evidence = "AI (quoted from page): " + res.location_evidence
        job.location_original = job.location_original or res.location_text
    elif proven(res.location_evidence) and res.france_eligible is False:
        return _exclude(s, job, res, "location: not in France - " + res.location_evidence)
    else:
        flags.append("location_unverified")
    # ---- role and relevance. A title that matches the profile by itself ('Stage - Data Scientist') is never dropped on the
    # AI's word alone: free models slip now and then (the same offer judged right twice, wrong once), so it goes to 'To check'
    # only a SPECIFIC match protects ('data scientist', 'research engineer', a research topic); one broad word ('IA', 'drone')
    # matches chatbot project managers and AI transformation consultants, which the model rightly turns down
    hits = re.findall(r"'([^']+)'", ti.reason) if ti.reason.startswith("title matches") else []
    title_match = ti.verdict == "pass" and (not hits or any(" " in h or "&" in h for h in hits))
    if res.role_type in ("sales_business", "admin_support"):
        if not title_match:
            return _exclude(s, job, res, f"role: {res.role_type} - {res.reasoning_summary}")
        flags.append("AI_says_non_technical_role_but_the_title_matches_your_profile")
    if res.contract_kind not in ("unknown", *wanted_types(profile)):
        return _exclude(s, job, res, f"contract: {OFFER_TYPES[res.contract_kind]} not wanted - {res.reasoning_summary}")
    if not res.is_relevant or res.relevance_score < cfg["min_relevance"]:
        if not title_match:
            return _exclude(s, job, res, f"AI relevance {res.relevance_score:.0f}: {res.reasoning_summary}")
        flags.append("AI_says_not_relevant_but_the_title_matches_your_profile")
    if res.needs_review:
        flags.append("relevance_needs_review")
    job.relevance_score, job.relevance_json = res.relevance_score, res.model_dump()
    job.review_flags = ",".join(flags)
    # an offer without a publication date is still returned: that alone does not send it to 'To check' (it keeps the flag,
    # and the app shows the day it was first seen instead)
    doubts = [x for x in flags if x != "publication_date_unknown"]
    job.status, job.reason = ("review" if doubts else "relevant"), res.reasoning_summary
    s.commit()
    return res


# --------------------------------------------------------------------------- pipeline (after crawling)
def _date_gate(s, j, cfg):
    """DATE FIRST. -> (True when the job is outside the range and was hidden, verdict True|False|None)."""
    refine_job_date(j)  # an explicit 'Date de publication' in the stored text beats a feed / refresh date
    ok, why = date_decision(j.pub_date, j.date_conf, cfg, None, j.pub_date_text)
    if ok is False:
        hide_out_of_range(s, j, why)
    return ok is False, ok


def _analyze_deep(s, j, profile, cfg, keys, fetcher, rend, say):
    """Check the DATE first (no AI call for a job outside the range). Then analyse relevance. If the text is short, the date
    is unknown or the AI is unsure, read the FULL job page and check / analyse again."""
    from .browser import deepen_job  # late import: needs Playwright only when a page must be rendered
    out, ok = _date_gate(s, j, cfg)
    if out:
        return None
    thin = len(j.description or "") < int(cfg.get("thin_job_chars", 800))
    if (thin or ok is None) and deepen_job(s, j, cfg, fetcher, rend):
        say(f"short text / unknown date -> full job page read: {j.title[:60]}")
        out, ok = _date_gate(s, j, cfg)  # the full page often states the real publication date
        if out:
            return None
    res = analyze_job(s, j, profile, cfg, keys)
    if j.status == "out_of_range":  # the date chosen by the AI put it outside the range
        return res
    if (res.needs_review or res.analysis_confidence < 0.6) and deepen_job(s, j, cfg, fetcher, rend):
        say(f"AI unsure -> full job page read, analysing again: {j.title[:60]}")
        out, ok = _date_gate(s, j, cfg)
        if out:
            return None
        res = analyze_job(s, j, profile, cfg, keys)
    return _second_reading(s, j, res, profile, cfg, keys, say)


SECOND_READING_FROM = 25  # an offer the AI turns down with this score or more is read a second time


def _second_reading(s, j, res, profile, cfg, keys, say):
    """The same model can judge the same offer 35 one time and 85 the next (checked on 30 real offers: 5 verdicts changed
    between two readings of the same text). An offer the AI turns down with a BORDERLINE score (25 or more: below the
    minimum relevance, or 'not relevant' with a fair score, which decides when the minimum is low) is read a second time;
    when that reading finds it relevant, the offer goes to 'To check', marked, instead of being lost."""
    if (res is None or j.status != "excluded" or not (j.reason or "").startswith("AI relevance")
            or res.relevance_score < SECOND_READING_FROM):
        return res
    first = res.relevance_score
    res2 = analyze_job(s, j, profile, cfg, keys, second=True)
    j.extra = {**(j.extra or {}), "read_twice": True}
    s.commit()
    if j.status in ("relevant", "review"):
        flags = [x for x in (j.review_flags or "").split(",") if x] + ["AI_readings_disagree"]
        j.status, j.review_flags = "review", ",".join(dict.fromkeys(flags))
        j.reason = f"two AI readings disagree ({first:.0f}, then {res2.relevance_score:.0f}): {j.reason}"
        s.commit()
        say(f"second AI reading finds it relevant ({first:.0f} -> {res2.relevance_score:.0f}): kept to check: {j.title[:60]}")
    return res2


def analyze_pipeline(cfg, profile, keys, prog, feed=None):
    """AI relevance on every offer handed over by `feed` (scan.run_scan: each website's offers as soon as it is read; None:
    every offer waiting now), then CV matching on relevant jobs."""
    from .browser import ThreadedRenderer
    cfg = run_config(cfg)
    rend = ThreadedRenderer(cfg, Fetcher(cfg, prog.say))  # a browser is started only if a job page really needs one
    try:
        _pipeline(cfg, profile, keys, prog, rend, feed)
    finally:
        rend.close()
        from .market import run_if_due
        with SessionLocal() as s:
            run_if_due(s, force=bool(cfg.get("stats_with_scan")))  # market history; statistics every 15 days or on request


def _by_priority(jobs):
    """The most promising offers first (scan.offer_priority)."""
    return sorted(jobs, key=offer_priority)


def _workers(cfg) -> int:
    """Offers analysed at the same time: several with automatic model switching (the requests spread over the ranked
    models), one with a single chosen model (a local Ollama, one key)."""
    return max(1, int(cfg.get("ai_workers", 4))) if cfg.get("auto_models") else 1


def _in_parallel(ids, work, workers):
    """work(id) for every id, `workers` at a time, started in the given order (best offers first). Yields (id, result or
    the exception it raised) as they finish."""
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai") as ex:
        futs = {ex.submit(work, i): i for i in ids}
        for f in as_completed(futs):
            try:
                yield futs[f], f.result()
            except Exception as e:  # noqa: BLE001 - handed to the caller, which decides
                yield futs[f], e


UNUSABLE_STOP = 10  # offers in a row whose two readings both failed: the models are broken, the analysis stops


def _pipeline(cfg, profile, keys, prog, rend, feed=None):
    """Workers take the offers from the feed, best first. No offer is dropped on one failure:
    - an unusable answer or an error: the offer is read again after the others (another model may take it);
    - every model at its limit: the offers wait in the queue until the first model is free again (up to 'ai_wait_max'
      seconds), instead of staying 'Not analysed';
    - an offer read by a small model (WEAK_LEVEL) is read again by a better model at the end, when one is free.
    Only a broken setup (no key, the call budget, every quota out for longer) stops the analysis."""
    reset_usage()
    if feed is None:  # every offer waiting now (an analysis started after the crawl)
        prog.set(task="AI analysis", ai_msg="")
        feed = Feed()
        with SessionLocal() as s:
            jobs = _by_priority(s.query(Job).filter(Job.status.in_(["candidate", "review"]), Job.relevance_score.is_(None)).all())
            prog.set(ai_total=feed.put(jobs), ai_done=0)
        feed.close()
    workers, pkey, stop = _workers(cfg), profile_key(profile), threading.Event()
    cap, max_wait = int(cfg.get("max_ai_jobs_per_run") or 0), float(cfg.get("ai_wait_max", 1800))
    lock = threading.Lock()
    state = {"active": 0, "resume_at": 0.0, "started": 0, "unusable": 0, "said_wait": 0.0}

    def relevant():  # the 'Relevant' tile: the same count as the header and the Results view
        with SessionLocal() as c:
            return c.query(Job).filter(Job.status == "relevant").count()

    def stop_with(msg):
        if not stop.is_set():
            prog.set(ai_msg=f"AI analysis stopped: {msg}", relevant=relevant())
            prog.say(f"AI stopped: {msg}")
        stop.set()

    def finished(jid):  # this offer's reading is over (analysed, or given up after its second try)
        feed.done(jid)
        prog.inc("ai_done")
        prog.set(relevant=relevant())

    def read(ws, j, fetcher):
        _analyze_deep(ws, j, profile, cfg, keys, fetcher, rend, prog.say)
        if j.relevance_score is not None:  # the profile it was analysed for: the next scans reuse it only for this one
            j.extra = {**(j.extra or {}), "profile_key": pkey}
            ws.commit()

    def read_better(ws, j):
        """A small model's reading, read again by a better model; skipped when none is free now."""
        before, model = j.relevance_score, (j.extra or {}).get("ai_model", "")
        if (j.status not in ("relevant", "review", "excluded") or before is None or (j.reason or "").startswith("duplicate")
                or (j.extra or {}).get("ai_level", 0) < WEAK_LEVEL):
            return
        better = {**cfg, "_max_level": WEAK_LEVEL - 1, "_no_wait": True}
        try:
            res = analyze_job(ws, j, profile, better, keys)
            if j.status != "out_of_range":
                _second_reading(ws, j, res, profile, better, keys, prog.say)
        except AIError:
            ws.rollback()  # no better model free: the small model's reading stays
            return
        j.extra = {**(j.extra or {}), "profile_key": pkey, "first_reading": f"{model}: {before:.0f}"}
        ws.commit()
        prog.say(f"read again by {j.extra.get('ai_model', 'a better model')} ({model}: {before:.0f} -> "
                 f"{j.relevance_score if j.relevance_score is not None else 0:.0f}, {j.status}): {j.title[:60]}")

    def handle(rnd, jid, fetcher):
        with SessionLocal() as ws:
            j = ws.get(Job, jid)
            if rnd == Feed.BETTER:
                try:
                    read_better(ws, j)
                finally:
                    feed.done(jid)
                return
            if j is not None and j.relevance_score is not None:
                # analysed, but its reading failed afterwards: the SECOND reading of a borderline exclusion is done now
                if not (j.extra or {}).get("read_twice") and j.relevance_json:
                    try:
                        _second_reading(ws, j, JobAnalysis.model_validate(j.relevance_json), profile, cfg, keys, prog.say)
                    except Exception:  # noqa: BLE001 - its first reading stands
                        ws.rollback()
                finished(jid)
                return
            if j is None or j.status not in ("candidate", "review"):
                # merged as a duplicate, or being filtered again (a website's second try): handed over again if it stays
                finished(jid)
                return
            if rnd == Feed.FIRST:
                with lock:
                    if cap and state["started"] >= cap:
                        feed.done(jid)  # beyond the configured limit per run: stays 'Not analysed'
                        return
                    state["started"] += 1
            title = j.title or ""
            try:
                read(ws, j, fetcher)
            except QuotaExhausted as e:  # every model at its limit
                ws.rollback()
                wait = float(getattr(e, "wait", 0) or 0)
                if 0 < wait <= max_wait:  # back in the queue; every worker waits for the first model to be free again
                    feed.again(jid, rnd)
                    with lock:
                        state["resume_at"] = max(state["resume_at"], time.time() + wait)
                        say = time.time() - state["said_wait"] > 60
                        state["said_wait"] = time.time() if say else state["said_wait"]
                    if say:
                        prog.say(f"every AI model is at its limit: the analysis waits {wait / 60:.0f} min, offers kept in the queue")
                    return
                finished(jid)
                stop_with(str(e))
                return
            except BudgetExceeded as e:
                ws.rollback()
                finished(jid)
                stop_with(str(e))
                return
            except (MalformedOutput, RequestTooLarge, AIError, Exception) as e:  # noqa: BLE001 - this offer only
                ws.rollback()
                if isinstance(e, AIError) and not isinstance(e, (MalformedOutput, RequestTooLarge)) and \
                        re.search(r"Authentication|No API key|No AI model available", str(e)):
                    finished(jid)
                    stop_with(str(e))
                    return
                if rnd == Feed.FIRST and not isinstance(e, RequestTooLarge):
                    feed.again(jid, Feed.AGAIN)  # read again after the other offers: another model may take it
                    prog.say(f"AI reading failed for '{title[:60]}' ({str(e)[:100]}): read again later")
                    return
                prog.say(f"AI reading failed twice for '{title[:60]}': stays 'Not analysed' ({str(e)[:120]})")
                finished(jid)
                with lock:
                    state["unusable"] += 1
                    broken = state["unusable"] >= UNUSABLE_STOP
                if broken:
                    stop_with(f"the AI failed twice on {UNUSABLE_STOP} offers in a row - try again later")
                return
            with lock:
                state["unusable"] = 0
            if (j.extra or {}).get("ai_level", 0) >= WEAK_LEVEL and j.status in ("relevant", "review", "excluded") \
                    and j.relevance_score is not None:
                prog.inc("ai_done")
                prog.set(relevant=relevant())
                feed.again(jid, Feed.BETTER)  # stays pending until a better model has read it (or none was free)
                return
            finished(jid)

    def worker():
        fetcher = Fetcher(cfg, prog.say)  # one HTTP session per worker
        while not stop.is_set() and not prog.cancel.is_set():
            pause = state["resume_at"] - time.time()
            if pause > 0:
                time.sleep(min(pause, 1.0))
                continue
            with lock:  # taking an offer and counting it active at once: the workers end only when nothing can come
                item = feed.get()
                if item is None and feed.closed.is_set() and state["active"] == 0:
                    return
                state["active"] += item is not None
            if item is None:
                time.sleep(0.2)
                continue
            try:
                handle(*item, fetcher)
            except Exception as e:  # noqa: BLE001 - one offer never ends the analysis
                prog.say(f"analysis failed for offer {item[1]}: {str(e)[:120]}")
                feed.done(item[1])
            finally:
                with lock:
                    state["active"] -= 1

    prog.say(f"AI analysis: up to {workers} offers at a time, best models first (a weaker model only when the better ones "
             "are out for a while)")
    t0 = time.time()
    threads = [threading.Thread(target=worker, name=f"ai-{i}", daemon=True) for i in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _say_models(prog, t0)
    if stop.is_set() or prog.cancel.is_set():
        return
    with SessionLocal() as s:  # the same offer read on two websites while both were being read: shown once
        n = merge_duplicates(s, cfg)
    if n:
        prog.inc("dups_merged", n)
        prog.set(relevant=relevant())
    _match_cvs(cfg, keys, prog, workers)


def _say_models(prog, t0):
    """Which models did the work, how often they failed and how fast they were (activity log)."""
    rows = health_report()
    if rows:
        prog.say(f"AI analysis done in {(time.time() - t0) / 60:.1f} min - " + "; ".join(
            f"{m.split('/')[-1]}: {ok} answers" + (f", {bad} unusable" if bad else "") + (f", {secs:.0f} s each" if ok else "")
            + (f", {lim} rate limits" if lim else "") + (f", up to {peak} at once" if peak > 1 else "")
            for _, m, ok, bad, secs, lim, peak in rows))


def _match_cvs(cfg, keys, prog, workers):
    with SessionLocal() as s:
        order = [j.id for j in s.query(Job).filter(Job.status == "relevant", Job.match_score.is_(None))
                 .limit(int(cfg.get("max_ai_jobs_per_run") or 0) or None)]
        titles = {j.id: j.title or "" for j in s.query(Job).filter(Job.id.in_(order))}
        has_cv = bool([c for c in s.query(CVDocument).filter_by(enabled=True) if c.status == "ok"])
        ckey = cv_key(s) if order and has_cv else ""
    if not order:
        return
    if not has_cv:
        prog.set(ai_msg="No usable CV uploaded (Settings): CV matching skipped.")
        return
    try:
        require_cv_consent(cfg)
    except AIError as e:
        prog.set(ai_msg=str(e))
        return
    prog.set(task="CV matching")
    stop = threading.Event()

    def match(jid):
        if stop.is_set() or prog.cancel.is_set():
            return None
        with SessionLocal() as ws:
            j = ws.get(Job, jid)
            try:
                match_job(ws, j, cfg, keys)
                if j.match_score is not None:  # the CVs it was matched with: the next scans reuse it only for these
                    j.extra = {**(j.extra or {}), "cv_key": ckey}
                    if j.match_score < cfg.get("min_match", 0):
                        j.status, j.reason = "excluded", f"CV match {j.match_score:.0f} is below your minimum ({cfg['min_match']})"
                    ws.commit()
            except Exception:
                ws.rollback()
                raise
        return True

    for jid, out in _in_parallel(order, match, workers):
        if isinstance(out, MalformedOutput):
            prog.say(f"CV match unusable for '{titles.get(jid, '')[:60]}': skipped ({str(out)[:120]})")
        elif isinstance(out, AIError):
            if not stop.is_set():
                prog.set(ai_msg=f"CV matching stopped: {out}")
            stop.set()
        elif isinstance(out, Exception):
            prog.say(f"CV match failed for '{titles.get(jid, '')[:60]}': {str(out)[:120]}")
        elif out:
            prog.inc("matched")
