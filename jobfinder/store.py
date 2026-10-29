"""store.py - SQLite models, settings, the local API-key file and the list of career websites."""
from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- API key file (local, chmod 600)
ENV_KEYS = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}


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
                "lever.co", "greenhouse.io", "ashbyhq.com", "myworkdaysite.com")
_HOST_PREFIX = {"www", "jobs", "job", "careers", "career", "carrieres", "carriere", "carrieresfrance", "recrutement", "emploi",
                "emplois", "join", "apply", "talents", "talent", "work", "hiring"}


def company_from_url(url: str) -> str:
    """'recrutement.mitsubishielectric.fr' -> 'Mitsubishielectric'; 'expleo-jobs-fr-fr.icims.com' -> 'Expleo'."""
    host = (urlparse(url).hostname or "").lower()
    parts, seg = host.split("."), [x for x in urlparse(url).path.split("/") if x]
    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com", "jobs.lever.co", "jobs.ashbyhq.com") and seg:
        name = seg[0]
    elif any(host.endswith(d) for d in _ATS_DOMAINS) and len(parts) > 2:
        name = re.split(r"-(?:jobs|careers|career|recrutement|fr|en)\b", parts[0])[0]
    else:
        rest = [x for x in parts[:-1] if x not in _HOST_PREFIX] or parts[:1]
        name = rest[-1] if len(rest[-1]) > 3 or len(rest) == 1 else rest[0]
    return name.replace("-", " ").title()


def add_sources(s, urls: list[str]):
    added = 0
    for u in urls:
        if syntactic_ok(u) and not s.query(Source).filter_by(url=normalize_url(u)).first():
            s.add(Source(url=normalize_url(u), label=company_from_url(u), platform=""))
            added += 1
    s.commit()
    return added


def wipe_session_data():
    """Delete websites, jobs, letter records and AI cache (settings, API keys, CVs and profile are kept)."""
    try:
        with SessionLocal() as s:
            for m in (Job, Source, CoverLetter, AICache):
                s.query(m).delete()
            s.commit()
    except Exception as e:  # pragma: no cover
        LOG.warning("wipe failed: %s", e)


def load_new_sources(s, urls: list[str]) -> int:
    """A new file REPLACES the previous websites and every job found before."""
    wipe_session_data()
    return add_sources(s, urls)

