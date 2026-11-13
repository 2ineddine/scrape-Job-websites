"""store.py - SQLite models, settings, the local API-key file and the list of career websites."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
import os
import re
from urllib.parse import urlparse

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, Text, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import APP_DIR, DATA_DIR, LOG, SECRETS_FILE, utcnow
from .web import normalize_url, syntactic_ok

# --------------------------------------------------------------------------- database
Base = declarative_base()


class Source(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)
    label = Column(String, default="")
    enabled = Column(Boolean, default=True)
    platform = Column(String, default="")
    status = Column(String, default="not_started")
    last_scan = Column(DateTime)
    pages_visited = Column(Integer, default=0)
    jobs_found = Column(Integer, default=0)
    last_error = Column(Text, default="")
    listed = Column(Integer, default=0)  # offers on the website's lists, inside the date range or not
    outcome = Column(String, default="")  # offers_found | nothing_in_range | no_openings | no_offers_detected | partial
    health = Column(String, default="")  # what changed since the previous scan ('read on 2026-11-01, now blocked')
    origin = Column(String, default="")  # the input file(s) that list it, one name per line ('pasted URLs' when pasted)


class SiteMemory(Base):  # kept between sessions: what is normal for a website
    __tablename__ = "site_memory"
    url = Column(String, primary_key=True)
    usual = Column(Integer, default=0)  # usual number of offers on its lists
    last_status = Column(String, default="")  # the previous scan, compared with the next one (health report)
    last_listed = Column(Integer, default=0)
    last_scan = Column(DateTime)
    last_preset = Column(String, default="")  # list sizes are compared only for the same date range


class SeenOffer(Base):  # kept between sessions: an offer cannot be younger than the day it was first seen
    __tablename__ = "seen_offers"
    uid = Column(String, primary_key=True)
    first_seen = Column(DateTime, default=utcnow)


class MarketOffer(Base):  # kept between sessions (6 months): facts of each analysed offer, for the statistics page
    __tablename__ = "market_offers"
    uid = Column(String, primary_key=True)
    seen = Column(DateTime)       # first time the AI analysed it
    pub_date = Column(DateTime)
    title = Column(String, default="")
    company = Column(String, default="")
    city = Column(String, default="")
    contract = Column(String, default="")
    relevant = Column(Boolean, default=False)
    score = Column(Float)
    match = Column(Float)
    data = Column(JSON, default=dict)  # skills, degree, seniority, languages, remote, salary, CV gaps, website


class MarketStats(Base):  # one snapshot every 15 days, kept between sessions
    __tablename__ = "market_stats"
    id = Column(Integer, primary_key=True)
    created = Column(DateTime)
    period_start = Column(DateTime)
    period_end = Column(DateTime)
    data = Column(JSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    uid = Column(String, unique=True, index=True)
    source_id = Column(Integer, index=True)
    platform_job_id = Column(String)
    title = Column(String)
    employer = Column(String)
    url = Column(String)
    apply_url = Column(String)
    description = Column(Text)
    employment_type = Column(String)
    location_original = Column(String)
    city = Column(String)
    country_code = Column(String)
    location_verified = Column(Boolean, default=False)
    remote_status = Column(String)
    france_eligible = Column(Boolean, default=False)
    location_evidence = Column(Text)
    pub_date_text = Column(String)
    pub_date = Column(DateTime)
    date_source = Column(String)
    date_conf = Column(Float, default=0.0)
    extraction_conf = Column(Float, default=0.5)
    provenance = Column(String)
    extra = Column(JSON)
    content_hash = Column(String)
    discovered = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow)
    status = Column(String, default="new", index=True)  # new|excluded|review|candidate|relevant|shortlisted|out_of_range (hidden)
    reason = Column(Text, default="")
    review_flags = Column(String, default="")
    relevance_score = Column(Float)
    relevance_json = Column(JSON)
    best_cv = Column(String)
    match_score = Column(Float)
    match_json = Column(JSON)


class CVDocument(Base):
    __tablename__ = "cv_documents"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    filename = Column(String)
    sha256 = Column(String, unique=True)
    path = Column(String)
    text = Column(Text)
    language = Column(String)
    status = Column(String)
    enabled = Column(Boolean, default=True)
    uploaded = Column(DateTime, default=utcnow)


class CoverLetter(Base):
    __tablename__ = "cover_letters"
    id = Column(Integer, primary_key=True)
    job_uid = Column(String, index=True)
    version = Column(Integer)
    cv_name = Column(String)
    tex_path = Column(String)
    pdf_path = Column(String)
    note = Column(Text)
    created = Column(DateTime, default=utcnow)


class Setting(Base):  # non-secret settings only (API keys are never stored)
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(JSON)


class AICache(Base):
    __tablename__ = "ai_cache"
    key = Column(String, primary_key=True)
    value = Column(Text)
    created = Column(DateTime, default=utcnow)
    kind = Column(String, default="")  # the answer's schema: 'PageOffers' (a lab page read by the AI) is kept between sessions


class RecentOffer(Base):  # kept between sessions for 24 hours: an offer read by a scan, with its AI analysis
    __tablename__ = "recent_offers"
    uid = Column(String, primary_key=True)
    source_url = Column(String, index=True)  # the website it was found on (normalised), to give it back to that website
    seen = Column(DateTime, index=True)  # when the offer was really read (not when it was given back)
    data = Column(JSON)  # the job's columns, dates as ISO text


engine = create_engine(f"sqlite:///{DATA_DIR / 'jobs.db'}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")  # websites are stored in parallel
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # a database made by an older version gets the new columns
        have = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(sources)")}
        for name, kind in (("listed", "INTEGER DEFAULT 0"), ("outcome", "VARCHAR DEFAULT ''"), ("health", "VARCHAR DEFAULT ''"),
                           ("origin", "VARCHAR DEFAULT ''")):
            if name not in have:
                conn.exec_driver_sql(f"ALTER TABLE sources ADD COLUMN {name} {kind}")
        if "kind" not in {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(ai_cache)")}:
            conn.exec_driver_sql("ALTER TABLE ai_cache ADD COLUMN kind VARCHAR DEFAULT ''")
        have = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(site_memory)")}
        for name, kind in (("last_status", "VARCHAR DEFAULT ''"), ("last_listed", "INTEGER DEFAULT 0"), ("last_scan", "DATETIME"),
                           ("last_preset", "VARCHAR DEFAULT ''")):
            if name not in have:
                conn.exec_driver_sql(f"ALTER TABLE site_memory ADD COLUMN {name} {kind}")


# --------------------------------------------------------------------------- API key file (local, chmod 600)
ENV_KEYS = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "gemini#2": "GEMINI_API_KEY_2", "gemini#3": "GEMINI_API_KEY_3", "gemini#4": "GEMINI_API_KEY_4", "groq#2": "GROQ_API_KEY_2", "groq#3": "GROQ_API_KEY_3", "groq#4": "GROQ_API_KEY_4", "openrouter#2": "OPENROUTER_API_KEY_2", "openrouter#3": "OPENROUTER_API_KEY_3",
            "xai": "XAI_API_KEY", "cohere": "COHERE_API_KEY", "cohere#2": "COHERE_API_KEY_2", "cohere#3": "COHERE_API_KEY_3", "nvidia": "NVIDIA_API_KEY",  # '#2': a second key, its own quota
            "jobspipe": "JOBSPIPE_API_KEY",  # not AI: the JobsPipe job-data API (offers of many companies)
            # kept in the file but not used: SambaNova and Ollama Cloud need credits, GitHub Models closed in July 2026
            "sambanova": "SAMBANOVA_API_KEY", "ollama_cloud": "OLLAMA_API_KEY", "github_models": "GITHUB_MODELS_API_KEY",
            "kaggle": "KAGGLE_API_TOKEN", "kaggle#2": "KAGGLE_API_TOKEN_2",  # Kaggle: datasets and notebooks, no AI chat API
            # not AI: your France Travail API access (francetravail.io), kept when an AI key is saved from Settings
            "francetravail_id": "FRANCE_TRAVAIL_CLIENT_ID", "francetravail_secret": "FRANCE_TRAVAIL_CLIENT_SECRET"}


def load_keys() -> dict:
    """Read api_keys.txt (lines like OPENAI_API_KEY=...) -> {provider: key}."""
    out, inv = {}, {v: k for k, v in ENV_KEYS.items()}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, val = line.split("=", 1)
                if inv.get(name.strip()) and val.strip():
                    out[inv[name.strip()]] = val.strip()
    return out


def _write_keys(d: dict):
    body = ["# API keys - private file, never commit it"] + [f"{ENV_KEYS[p]}={k}" for p, k in d.items()]
    SECRETS_FILE.write_text("\n".join(body) + "\n", encoding="utf-8")
    SECRETS_FILE.chmod(0o600)


def save_key(provider: str, key: str):
    key = key.strip()
    if provider not in ENV_KEYS or not key or re.search(r"\s", key):
        raise ValueError("Invalid key (empty or contains spaces/newlines)")
    d = load_keys()
    d[provider] = key
    _write_keys(d)


def delete_key(provider: str):
    d = load_keys()
    d.pop(provider, None)
    _write_keys(d)


def ensure_gitignore():
    g = APP_DIR / ".gitignore"
    have = g.read_text() if g.exists() else ""
    add = [x for x in ("api_keys.txt", "data/", ".venv/", "__pycache__/") if x not in have]
    if add:
        g.write_text(have + ("\n" if have and not have.endswith("\n") else "") + "\n".join(add) + "\n")


def _merge(default, stored):
    out = json.loads(json.dumps(default))
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def get_setting(s, key, default):
    row = s.get(Setting, key)
    return _merge(default, row.value if row else None)


def set_setting(s, key, value):
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    s.commit()


_ATS_DOMAINS = ("myworkdayjobs.com", "smartrecruiters.com", "breezy.hr", "teamtailor.com", "icims.com", "recruitee.com",
                "lever.co", "greenhouse.io", "ashbyhq.com", "myworkdaysite.com", "eightfold.ai", "talentview.io",
                "welcomekit.co", "talent-soft.com", "taleez.com", "jobs.personio.de", "jobs.personio.com", "cvcatcher.io")
_ATS_PATH = {"careers.smartrecruiters.com": 0, "jobs.smartrecruiters.com": 0, "jobs.lever.co": 0, "jobs.ashbyhq.com": 0,
             "job-boards.greenhouse.io": 0, "boards.greenhouse.io": 0, "job-boards.eu.greenhouse.io": 0,
             "apply.workable.com": 0, "careers.werecruit.io": 1}  # host -> position of the company in the path
_COMPANY_PAGES = ("jobs.stationf.co", "www.welcometothejungle.com", "welcometothejungle.com")  # '/companies/<company>'
_HOST_PREFIX = {"www", "jobs", "job", "careers", "career", "carrieres", "carriere", "carrieresfrance", "recrutement", "emploi",
                "emplois", "join", "apply", "talents", "talent", "work", "hiring"}


_RENAMED = {"akka": "akkodis"}  # 'akka-cand.talent-soft.com': Akka is now Akkodis


def company_from_url(url: str) -> str:
    """'recrutement.mitsubishielectric.fr' -> 'Mitsubishielectric'; 'expleo-jobs-fr-fr.icims.com' -> 'Expleo'."""
    host = (urlparse(url).hostname or "").lower()
    parts, seg = host.split("."), [x for x in urlparse(url).path.split("/") if x]
    board = next((m for m in ("companies", "entreprises") if m in seg[:-1]), None)
    if board and (host in _COMPANY_PAGES or host.endswith(".hellowork.com")):  # one company's page on a shared site
        # '/companies/joko-1' -> 'joko', Hellowork '/fr-fr/entreprises/-proxiad--86909.html' -> 'proxiad'
        name = re.sub(r"(?:-+\d+)?(?:\.html?)?$", "", seg[seg.index(board) + 1]).strip("-") or seg[seg.index(board) + 1]
    elif host in _ATS_PATH and len(seg) > _ATS_PATH[host]:
        name = re.sub(r"-\d+$", "", seg[_ATS_PATH[host]]) or seg[_ATS_PATH[host]]  # workable 'corwave-1' -> 'corwave'
    elif any(host.endswith(d) for d in _ATS_DOMAINS) and len(parts) > 2:
        words = [w for w in parts[0].split("-") if w not in _HOST_PREFIX and w not in ("fr", "en", "global", "external", "group",
                 "cand") and not w.isdigit()]  # 'planity-1651249729.teamtailor.com' -> 'planity'
        # 'careers-fr-soitec' -> 'soitec', 'expleo-jobs-fr-fr' -> 'expleo', 'eleven-labs.welcomekit.co' -> 'eleven labs'
        name = _RENAMED.get("-".join(words), "-".join(words)) if words else parts[0]
    else:
        rest = [x for x in parts[:-1] if x not in _HOST_PREFIX and x not in ("gouv", "co", "com", "ac", "org")] or parts[:1]
        name = rest[-1] if len(rest[-1]) > 3 or len(rest) == 1 else rest[0]
    name = name.replace("-", " ").replace("_", " ")
    return name.upper() if len(name) <= 3 else name.title()


def keyword_sources(s, cfg) -> list[int]:
    """The built-in searches of this scan: Hellowork with the profile's keywords when the setting is on (-> its source id).
    Added at Start, after the website files (loading a file replaces every source); removed when the setting is off."""
    from .config import HW_KEYWORDS_LABEL, HW_KEYWORDS_ORIGIN, HW_KEYWORDS_URL
    src = s.query(Source).filter_by(url=HW_KEYWORDS_URL).first()
    if not cfg.get("hellowork_keywords", True):
        if src is not None and src.origin == HW_KEYWORDS_ORIGIN:
            s.query(Job).filter_by(source_id=src.id).delete()
            s.delete(src)
            s.commit()
        return []
    if src is None:
        src = Source(url=HW_KEYWORDS_URL, label=HW_KEYWORDS_LABEL, platform="", origin=HW_KEYWORDS_ORIGIN, last_error="")
        s.add(src)
        s.commit()
    return [src.id]


def add_sources(s, urls: list[str], origins: dict | None = None):
    """Each URL is put in one uniform form first (urlclean): search words, page numbers and tracking codes of a past
    search are removed, so every website is read whole and filtered the same way. Duplicates collapse.
    origins: {url: [file names]}, the input files that list each URL; a website listed by several files keeps them all."""
    from .urlclean import clean_source_url
    added = 0
    for u in urls:
        if not syntactic_ok(u):
            continue
        clean, removed = clean_source_url(u)
        nu = normalize_url(clean)
        names = list((origins or {}).get(u) or [])
        src = s.query(Source).filter_by(url=nu).first()
        if not src:
            s.add(Source(url=nu, label=company_from_url(nu), platform="", origin="\n".join(dict.fromkeys(names)),
                         last_error=("URL simplified: " + ", ".join(removed)) if removed else ""))
            added += 1
        elif names:
            src.origin = "\n".join(x for x in dict.fromkeys([*(src.origin or "").split("\n"), *names]) if x)
    s.commit()
    return added


KEPT_AI_ANSWERS = ("PageOffers",)
SCAN_LOCK = DATA_DIR / "scan.lock"  # holds the id of the process that is scanning


def lock_scan():
    SCAN_LOCK.write_text(str(os.getpid()))


def unlock_scan():
    if scan_owner() == os.getpid():
        SCAN_LOCK.unlink(missing_ok=True)


def scan_owner():
    """The process id of a scan still running, or None (a lock left by a stopped process does not count)."""
    try:
        pid = int(SCAN_LOCK.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def scan_running_elsewhere() -> bool:
    return scan_owner() not in (None, os.getpid())


def wipe_session_data() -> bool:
    """Delete websites, jobs, letter records and AI cache (settings, API keys, CVs and profile are kept).
    Refused while another process scans: opening the app must not erase a scan running in the background."""
    if scan_running_elsewhere():
        LOG.warning("a scan is running in another process (%s): its websites and offers are kept", scan_owner())
        return False
    try:
        with SessionLocal() as s:
            try:  # the analysed offers' facts go to the market history (statistics) before they are deleted
                from .market import archive_offers
                archive_offers(s)
            except Exception as e:  # pragma: no cover - statistics never block a wipe
                s.rollback()
                LOG.warning("market history not updated: %s", e)
            for m in (Job, Source, CoverLetter):
                s.query(m).delete()
            # the AI's reading of a public lab page is kept 30 days: an unchanged page is not sent to the AI again.
            # Every other answer (offer analysis, CV matching, letters) is deleted.
            s.query(AICache).filter((AICache.kind.notin_(KEPT_AI_ANSWERS)) | (AICache.kind.is_(None))
                                    | (AICache.created < utcnow() - timedelta(days=30))).delete(synchronize_session=False)
            s.commit()
            drop_scan_record(s)  # its websites are gone: nothing left to resume
    except Exception as e:  # pragma: no cover
        LOG.warning("wipe failed: %s", e)
    return True


# --------------------------------------------------------------------------- offers kept 24 hours between scans
RECENT_HOURS = 24
_DATES = ("pub_date", "discovered", "last_seen")


# the version of the app's analysis rules (contract, place, date checks; the models' levels): raised with every change of
# them, so that an offer analysed with older rules is analysed again instead of kept (a CDD read as an internship in the
# morning stayed excluded after the rule was fixed, November 2026)
ANALYSIS_RULES = 2


def profile_key(profile) -> str:
    """The search profile and the rules an analysis was made with: another profile, or rules changed since -> the offer is
    analysed again."""
    return hashlib.sha1(json.dumps([ANALYSIS_RULES, profile or {}], sort_keys=True, default=str).encode()).hexdigest()[:12]


def cv_key(s) -> str:
    """The CVs a match was made with: another CV set -> the offer is matched again."""
    cvs = sorted((c.name, hashlib.sha1((c.text or "").encode()).hexdigest()) for c in s.query(CVDocument).filter_by(enabled=True)
                 if c.status == "ok")
    return hashlib.sha1(json.dumps(cvs).encode()).hexdigest()[:12]


def keep_recent_offers(s) -> int:
    """Save the offers of the current session read in the last 24 hours (with their analysis) for the next scans; offers
    older than that are forgotten. Called at the end of every scan."""
    cut = utcnow() - timedelta(hours=RECENT_HOURS)
    s.query(RecentOffer).filter(RecentOffer.seen < cut).delete(synchronize_session=False)
    urls, n = {x.id: normalize_url(x.url) for x in s.query(Source).all()}, 0
    cols = [c.name for c in Job.__table__.columns if c.name not in ("id", "source_id")]
    for j in s.query(Job).filter(Job.last_seen >= cut):
        if j.source_id not in urls or not j.title:
            continue
        d = {c: getattr(j, c) for c in cols}
        d.update({k: d[k].isoformat() if d[k] else None for k in _DATES})
        s.merge(RecentOffer(uid=j.uid, source_url=urls[j.source_id], seen=j.last_seen, data=d))
        n += 1
    s.commit()
    return n


def restore_recent_offers(s, src, prof_key, cvs_key) -> list:
    """Give a website back its offers read in the last 24 hours, so that their pages are not opened again and the AI does
    not analyse them again. An analysis made for another search profile, or a CV match made with other CVs, is dropped:
    those offers go through the filters and the AI again. -> the restored jobs."""
    cut = utcnow() - timedelta(hours=RECENT_HOURS)
    have = {u for (u,) in s.query(Job.uid)}
    out = []
    for r in s.query(RecentOffer).filter(RecentOffer.source_url == normalize_url(src.url), RecentOffer.seen >= cut):
        if r.uid in have:
            continue
        d = dict(r.data or {})
        d.update({k: datetime.fromisoformat(d[k]) if d.get(k) else None for k in _DATES})
        ex = dict(d.get("extra") or {})
        if d.get("status") != "out_of_range":
            if ex.get("profile_key") != prof_key or d.get("relevance_score") is None:
                # not analysed, or analysed for another profile: the filters (and the AI) run again on the saved text
                d.update(status="new", reason="", review_flags="", relevance_score=None, relevance_json=None)
                ex.pop("profile_key", None)
            if ex.get("cv_key") != cvs_key or d.get("relevance_score") is None:
                d.update(best_cv=None, match_score=None, match_json=None)
                ex.pop("cv_key", None)
        d["extra"] = {**ex, "kept_from": r.seen.strftime("%Y-%m-%d %H:%M")}
        job = Job(**{k: v for k, v in d.items() if k in Job.__table__.columns.keys()}, source_id=src.id)
        s.add(job)
        out.append(job)
    s.commit()
    return out


def recent_count(s) -> int:
    """Offers kept from the last 24 hours (the saved ones, and this session's offers that the next save will keep)."""
    cut = utcnow() - timedelta(hours=RECENT_HOURS)
    saved = {u for (u,) in s.query(RecentOffer.uid).filter(RecentOffer.seen >= cut)}
    return len(saved | {u for (u,) in s.query(Job.uid).filter(Job.last_seen >= cut)})


def forget_recent_offers() -> int:
    """The 'Delete' button: the offers kept from the last 24 hours AND the current results are deleted, so that the next
    scan reads every page and analyses every offer again (the loaded websites, settings, CVs and statistics stay)."""
    if scan_running_elsewhere():
        raise RuntimeError("a scan is running in another window or process: delete the kept offers when it has finished")
    with SessionLocal() as s:
        try:  # the analysed offers' facts go to the market history (statistics) before they are deleted
            from .market import archive_offers
            archive_offers(s)
        except Exception as e:  # pragma: no cover - statistics never block a deletion
            s.rollback()
            LOG.warning("market history not updated: %s", e)
        n = s.query(RecentOffer).delete()
        s.query(Job).delete()
        s.query(CoverLetter).delete()
        s.query(Source).update({Source.status: "not_started", Source.jobs_found: 0, Source.last_error: ""})
        s.commit()
        drop_scan_record(s)  # its results are gone: nothing left to resume
    return n


# ---------------------------------------------------------------------------- an unfinished scan, resumed later
# A scan that did not finish (Stop button, 6-hour limit, AI credits or quotas used up, app closed or computer turned off
# during the scan) keeps its websites and results until the next launch, which offers to RESUME it (only the websites not
# read yet, and the offers not analysed yet) or to START AGAIN. The search settings are the scan's own; the AI settings
# and keys are today's (credits added in between are used).
SCAN_KEYS = ("date_preset", "date_from", "date_to", "hellowork_keywords", "respect_robots", "min_relevance")


def scan_record(s) -> dict:
    return get_setting(s, "last_scan", {}) or {}


def drop_scan_record(s):
    set_setting(s, "last_scan", {})


def scan_started(s, source_ids, cfg, profile, resume=None):
    """Records the scan that starts or resumes: its websites, its search settings and profile (never a key)."""
    if resume:
        info = {k: v for k, v in resume.items() if k not in ("todo", "done", "pending")}
        info["resumed"] = int(info.get("resumed") or 0) + 1
    else:
        info = {"started": utcnow().isoformat(), "source_ids": list(source_ids),
                "cfg": {k: cfg[k] for k in SCAN_KEYS if k in cfg}, "profile": profile, "resumed": 0}
    set_setting(s, "last_scan", {**info, "state": "running", "pid": os.getpid(), "why": ""})


def scan_ended(s, why=""):
    """The scan is over: finished (why ''), or not (why: the reason, shown with the offer to resume it)."""
    info = scan_record(s)
    if info:
        set_setting(s, "last_scan", {**info, "state": "interrupted" if why else "finished", "why": why,
                                     "ended": utcnow().isoformat()})


def scan_todo(s, info) -> tuple[list[int], int, int]:
    """What an unfinished scan still has to do -> (websites to read, websites read, offers waiting for the AI). A website
    counts as read once it ended after the scan started; one cut short by its time limit is read again."""
    started, ids = datetime.fromisoformat(info["started"]), list(info.get("source_ids") or [])
    todo, done = [], 0
    for x in s.query(Source).filter(Source.id.in_(ids)):
        if x.last_scan is None or x.last_scan < started or x.status in ("not_started", "partially_completed"):
            todo.append(x.id)
        else:
            done += 1
    pending = s.query(Job).filter(Job.source_id.in_(ids), Job.status.in_(["new", "candidate", "review"]),
                                  Job.relevance_score.is_(None)).count()
    return todo, done, pending


def unfinished_scan(s) -> dict | None:
    """The last scan when it did not finish and is not running (here or in another window) -> its record with 'todo',
    'done' and 'pending', else None. A scan whose process ended without saying so counts as interrupted."""
    info = scan_record(s)
    if info.get("state") == "running":
        if scan_owner() is not None:
            return None
        info = {**info, "state": "interrupted", "why": "the app was closed during the scan"}
        set_setting(s, "last_scan", info)
    if info.get("state") != "interrupted" or not info.get("started"):
        return None
    todo, done, pending = scan_todo(s, info)
    return {**info, "todo": todo, "done": done, "pending": pending} if todo or pending else None


def restart_session(s):
    """'Start again' after an unfinished scan: its offers are kept for 24 hours like any scan's (the new scan does not open
    their pages again, nor analyse them again), then its results are cleared. The websites stay."""
    try:
        from .market import archive_offers
        archive_offers(s)
    except Exception as e:  # pragma: no cover - statistics never block a new scan
        s.rollback()
        LOG.warning("market history not updated: %s", e)
    keep_recent_offers(s)
    s.query(Job).delete()
    s.query(CoverLetter).delete()
    s.query(Source).update({Source.status: "not_started", Source.jobs_found: 0, Source.last_error: ""})
    s.commit()
    drop_scan_record(s)


def load_new_sources(s, urls: list[str], origins: dict | None = None) -> int:
    """A new file REPLACES the previous websites and every job found before."""
    if not wipe_session_data():
        raise RuntimeError("a scan is running in another window or process: load new websites when it has finished")
    return add_sources(s, urls, origins)


def file_coverage(s) -> list[dict]:
    """Per input file: how many of its websites the last scan searched, and how each one ended.
    'not reached': the scan stopped (time limit, Stop button) before this website."""
    out = {}
    for src in s.query(Source).all():
        kind = ("read" if src.status == "completed" else "partial" if src.status == "partially_completed"
                else "failed" if src.status in ("blocked", "failed") else "waiting")
        for name in (src.origin or "").split("\n") or [""]:
            row = out.setdefault(name or "other URLs", dict(file=name or "other URLs", total=0, read=0, partial=0,
                                                           failed=0, waiting=0, missed=[]))
            row["total"] += 1
            row[kind] += 1
            if kind in ("failed", "waiting"):
                row["missed"].append(src.label or src.url)
    return sorted(out.values(), key=lambda r: r["file"])

