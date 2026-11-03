"""store.py - SQLite models, settings, the local API-key file and the list of career websites."""
from __future__ import annotations

import json
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
    outcome = Column(String, default="")  # offers_found | nothing_in_range | no_offers_detected | partial
    health = Column(String, default="")  # what changed since the previous scan ('read on 2026-11-01, now blocked')


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


engine = create_engine(f"sqlite:///{DATA_DIR / 'jobs.db'}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # a database made by an older version gets the new columns
        have = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(sources)")}
        for name, kind in (("listed", "INTEGER DEFAULT 0"), ("outcome", "VARCHAR DEFAULT ''"), ("health", "VARCHAR DEFAULT ''")):
            if name not in have:
                conn.exec_driver_sql(f"ALTER TABLE sources ADD COLUMN {name} {kind}")
        have = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(site_memory)")}
        for name, kind in (("last_status", "VARCHAR DEFAULT ''"), ("last_listed", "INTEGER DEFAULT 0"), ("last_scan", "DATETIME"),
                           ("last_preset", "VARCHAR DEFAULT ''")):
            if name not in have:
                conn.exec_driver_sql(f"ALTER TABLE site_memory ADD COLUMN {name} {kind}")


# --------------------------------------------------------------------------- API key file (local, chmod 600)
ENV_KEYS = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "gemini#2": "GEMINI_API_KEY_2", "groq#2": "GROQ_API_KEY_2",
            "xai": "XAI_API_KEY", "cohere": "COHERE_API_KEY"}  # '#2': a second key, its own quota


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
                "welcomekit.co", "talent-soft.com")
_ATS_PATH = {"careers.smartrecruiters.com": 0, "jobs.smartrecruiters.com": 0, "jobs.lever.co": 0, "jobs.ashbyhq.com": 0,
             "apply.workable.com": 0, "careers.werecruit.io": 1}  # host -> position of the company in the path
_HOST_PREFIX = {"www", "jobs", "job", "careers", "career", "carrieres", "carriere", "carrieresfrance", "recrutement", "emploi",
                "emplois", "join", "apply", "talents", "talent", "work", "hiring"}


def company_from_url(url: str) -> str:
    """'recrutement.mitsubishielectric.fr' -> 'Mitsubishielectric'; 'expleo-jobs-fr-fr.icims.com' -> 'Expleo'."""
    host = (urlparse(url).hostname or "").lower()
    parts, seg = host.split("."), [x for x in urlparse(url).path.split("/") if x]
    if host in _ATS_PATH and len(seg) > _ATS_PATH[host]:
        name = seg[_ATS_PATH[host]]
    elif any(host.endswith(d) for d in _ATS_DOMAINS) and len(parts) > 2:
        words = [w for w in parts[0].split("-") if w not in _HOST_PREFIX and w not in ("fr", "en", "global", "external")]
        name = max(words, key=len) if words else parts[0]  # 'careers-fr-soitec' -> 'soitec', 'expleo-jobs-fr-fr' -> 'expleo'
    else:
        rest = [x for x in parts[:-1] if x not in _HOST_PREFIX and x not in ("gouv", "co", "com", "ac", "org")] or parts[:1]
        name = rest[-1] if len(rest[-1]) > 3 or len(rest) == 1 else rest[0]
    name = name.replace("-", " ").replace("_", " ")
    return name.upper() if len(name) <= 3 else name.title()


def add_sources(s, urls: list[str]):
    """Each URL is put in one uniform form first (urlclean): search words, page numbers and tracking codes of a past
    search are removed, so every website is read whole and filtered the same way. Duplicates collapse."""
    from .urlclean import clean_source_url
    added = 0
    for u in urls:
        if not syntactic_ok(u):
            continue
        clean, removed = clean_source_url(u)
        nu = normalize_url(clean)
        if not s.query(Source).filter_by(url=nu).first():
            s.add(Source(url=nu, label=company_from_url(nu), platform="",
                         last_error=("URL simplified: " + ", ".join(removed)) if removed else ""))
            added += 1
    s.commit()
    return added


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
            for m in (Job, Source, CoverLetter, AICache):
                s.query(m).delete()
            s.commit()
    except Exception as e:  # pragma: no cover
        LOG.warning("wipe failed: %s", e)
    return True


def load_new_sources(s, urls: list[str]) -> int:
    """A new file REPLACES the previous websites and every job found before."""
    if not wipe_session_data():
        raise RuntimeError("a scan is running in another window or process: load new websites when it has finished")
    return add_sources(s, urls)

