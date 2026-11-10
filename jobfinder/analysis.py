"""analysis.py - the AI reads each remaining offer: publication date, location, kind of role and relevance.

Dates: the AI does not write a date. It receives the NUMBERED list of every date found in the text, each with its role
(published / updated / deadline / start, see dates.date_candidates) and answers the number of the publication date.
Its answer is then checked against the text. Locations and role are accepted only with a verbatim quote from the page.
Anything the AI cannot prove stays in 'To check'."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

from .classify import OFFER_TYPES, analyze_title, wanted_types
from .config import run_config, utcnow
from .cv import _ws, match_job
from .dates import NO_DATE, date_candidates, date_decision, describe_candidates, parse_date, refine_job_date, text_publication_date
from .llm import AIError, MalformedOutput, call_json, require_cv_consent, reset_usage
from .scan import hide_out_of_range
from .store import CVDocument, Job, SessionLocal, cv_key, profile_key
from .web import Fetcher


class JobAnalysis(BaseModel):
    """What the AI reads from the job page text. Evidence fields must be verbatim excerpts."""
    title: str = ""
    offer_kind: Literal["job", "research_topic"] = "job"
    role_type: Literal["research_ml", "engineering_software", "data", "sales_business", "admin_support", "other"] = "other"
    contract_kind: Literal["job", "internship", "apprenticeship", "phd_postdoc", "unknown"] = "unknown"
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
        for k in ("title", "location_text", "location_evidence", "date_evidence", "reasoning_summary"):
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


def build_prompt(job, profile, text, cands, ti):
    want = wanted_types(profile)
    wanted = ", ".join(OFFER_TYPES[k] for k in OFFER_TYPES if k in want)
    unwanted = ", ".join(OFFER_TYPES[k] for k in OFFER_TYPES if k not in want) or "nothing"
    return (
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
        "apprenticeship (alternance, apprentissage), phd_postdoc (thesis, PhD, doctoral contract, post-doc), unknown.\n"
        "MARKET FACTS (from the text only): skills = up to 12 short names of the tools, programming languages, frameworks and "
        "methods the offer asks for (e.g. 'Python', 'PyTorch', 'C++', 'ROS', 'computer vision', 'Kubernetes'); degree = the "
        "required degree (phd|master|engineer|bachelor|none|unknown); seniority (intern|junior|mid|senior|lead|unknown); "
        "languages = spoken languages required (e.g. 'French', 'English'); remote (onsite|hybrid|remote|unknown); "
        "salary_text = the salary as written in the offer, else empty.\n"
        "JSON keys: title, offer_kind(job|research_topic), contract_kind(job|internship|apprenticeship|phd_postdoc|unknown), "
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


def analyze_job(s, job: Job, profile, cfg, keys=None):
    """AI reads the job text: publication date, location, role type and relevance in ONE call.
    Evidence is verified against the text; anything unproven stays in 'To check'."""
    text = (job.description or "")[:int(cfg.get("job_text_chars", 9000))]
    ex = job.extra or {}
    ti = analyze_title(job.title, ex.get("department"), profile, job.employment_type)
    cands = date_candidates(text)
    res = call_json(build_prompt(job, profile, text, cands, ti), JobAnalysis, cfg, s, keys)
    # the offer type the TITLE states ('Stage - Data Scientist', 'Post-doc', 'CDI') beats the AI's: small models read
    # 'Stage' as a regular job, and an internship-only profile would then lose the offer
    stated = ti.training or ("job" if ti.contract in ("cdi", "cdd", "freelance", "interim") else "")
    if stated and res.contract_kind != stated:
        res.contract_kind = stated
    proven = lambda ev: bool(ev) and len(_ws(ev)) >= 4 and _ws(ev) in _ws(text)
    flags = []
    # ---- publication date: deterministic text evidence first, then the AI's choice among the dates of the text
    refine_job_date(job)  # an explicit 'Date de publication : ...' in the description beats a feed / refresh date
    ai_date, explicit = _ai_date(res, cands, text)
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
    return res


def analyze_pipeline(cfg, profile, keys, prog):
    """AI relevance on every candidate, then CV matching on relevant jobs. Never switches provider silently."""
    from .browser import Renderer
    cfg = run_config(cfg)
    fetcher = Fetcher(cfg, prog.say)
    rend = Renderer(cfg, fetcher)  # a browser is started only if a job page really needs one
    try:
        _pipeline(cfg, profile, keys, prog, fetcher, rend)
    finally:
        rend.close()
        from .market import run_if_due
        with SessionLocal() as s:
            run_if_due(s, force=bool(cfg.get("stats_with_scan")))  # market history; statistics every 15 days or on request


def _by_priority(jobs):
    """The AI reads the most promising offers first, as a run or a free quota may end before the last one: titles in the
    research fields, then offers with a proven date, then the newest."""
    def key(j):
        verdict = ((j.extra or {}).get("title_info") or {}).get("verdict")
        return (verdict != "pass", j.status != "candidate", -(j.pub_date.timestamp() if j.pub_date else 0))
    return sorted(jobs, key=key)


def _pipeline(cfg, profile, keys, prog, fetcher, rend):
    reset_usage()
    prog.set(task="AI analysis", ai_msg="")
    with SessionLocal() as s:
        jobs = s.query(Job).filter(Job.status.in_(["candidate", "review"]), Job.relevance_score.is_(None)).all()
        jobs, left = _by_priority(jobs), max(0, len(jobs) - cfg["max_ai_jobs_per_run"])
        jobs = jobs[:cfg["max_ai_jobs_per_run"]]
        prog.set(ai_total=len(jobs), ai_done=0)
        if left:
            prog.say(f"{left} offers beyond the limit of {cfg['max_ai_jobs_per_run']} per run stay 'Not analysed'")
        # the 'Relevant' tile: the same count as the header and the Results view ('To check' offers are not relevant yet)
        relevant = lambda: s.query(Job).filter(Job.status == "relevant").count()
        failed, pkey = 0, profile_key(profile)
        for j in jobs:
            if prog.cancel.is_set():
                return
            try:
                _analyze_deep(s, j, profile, cfg, keys, fetcher, rend, prog.say)
                if j.relevance_score is not None:  # the profile it was analysed for: the next scans reuse it only for this one
                    j.extra = {**(j.extra or {}), "profile_key": pkey}
                    s.commit()
                failed = 0
            except MalformedOutput as e:  # this offer only: it stays 'Not analysed', the others go on
                s.rollback()
                failed += 1
                prog.say(f"AI answer unusable for '{j.title[:60]}': skipped ({str(e)[:120]})")
                if failed >= 5:
                    prog.set(ai_msg="AI analysis stopped: the model gave 5 unusable answers in a row - try another model in Settings")
                    return
            except AIError as e:  # key, quota, provider down: every next offer would fail too
                prog.set(ai_msg=f"AI analysis stopped: {e}", relevant=relevant())
                prog.say(f"AI error: {e}")
                return
            prog.inc("ai_done")
            prog.set(relevant=relevant())
        todo = s.query(Job).filter(Job.status == "relevant", Job.match_score.is_(None)).limit(cfg["max_ai_jobs_per_run"]).all()
        if not todo:
            return
        if not [c for c in s.query(CVDocument).filter_by(enabled=True) if c.status == "ok"]:
            prog.set(ai_msg="No usable CV uploaded (Settings): CV matching skipped.")
            return
        try:
            require_cv_consent(cfg)
        except AIError as e:
            prog.set(ai_msg=str(e))
            return
        prog.set(task="CV matching")
        ckey = cv_key(s)
        for j in todo:
            if prog.cancel.is_set():
                return
            try:
                match_job(s, j, cfg, keys)
            except MalformedOutput as e:
                s.rollback()
                prog.say(f"CV match unusable for '{j.title[:60]}': skipped ({str(e)[:120]})")
                continue
            except AIError as e:
                prog.set(ai_msg=f"CV matching stopped: {e}")
                return
            if j.match_score is not None:  # the CVs it was matched with: the next scans reuse it only for these
                j.extra = {**(j.extra or {}), "cv_key": ckey}
                s.commit()
            if j.match_score is not None and j.match_score < cfg.get("min_match", 0):
                j.status, j.reason = "excluded", f"CV match {j.match_score:.0f} is below your minimum ({cfg['min_match']})"
                s.commit()
            prog.inc("matched")
