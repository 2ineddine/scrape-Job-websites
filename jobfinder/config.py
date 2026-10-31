"""config.py - paths, logging and default settings (user settings + internal limits)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent  # data and keys live next to app.py
DATA_DIR = APP_DIR / "data"
SECRETS_FILE = APP_DIR / "api_keys.txt"
for _d in ("", "cvs", "letters"):
    (DATA_DIR / _d).mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("jobfinder")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC everywhere


PRESET_DAYS = {"Last 24 hours": 1, "Last 3 days": 3, "Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30}

# --------------------------------------------------------------------------- settings shown in the app
CONFIG_DEFAULT = {
    "date_preset": "Last 30 days",
    "min_relevance": 60,
    "send_cv_remote_ok": False,
    "model": {"provider": "ollama", "model": "llama3.1"},  # ONE model used for every AI task
    "providers": {
        "ollama": {"base_url": "http://localhost:11434"},
        "openai": {"base_url": "https://api.openai.com/v1"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
        "groq": {"base_url": "https://api.groq.com/openai/v1"},
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta"},
        "anthropic": {"base_url": "https://api.anthropic.com/v1"},
    },
}

# --------------------------------------------------------------------------- fixed behaviour (not settings)
# The PUBLICATION DATE decides when a website is finished; page counts are only safety ceilings.
LIMITS = {
    "nav_mode": "auto",  # platform readers -> page reading -> AI page reader -> AI browser
    "ai_site_filters": True, "site_filter_location": "France",
    "unknown_date_policy": "review",  # an offer without a publication date goes to 'To check'
    "include_remote_fr": True, "min_match": 0,
    "delay": 1.0, "max_duration": 5400, "max_site_duration": 1200, "timeout": 25, "retries": 2, "respect_robots": True,
    "max_pages": 500, "max_details_per_source": 300, "stale_pages": 2, "parallel_sites": 4,
    "max_ai_calls": 1200, "max_ai_jobs_per_run": 400,
    "thin_job_chars": 800, "job_text_chars": 9000, "ai_nav_max_steps": 150, "ai_filter_max_steps": 8,
}


def run_config(cfg: dict) -> dict:
    """User settings + fixed behaviour: the dict every crawler / analysis function receives. Fixed values win over
    anything an older version of the app saved."""
    return {**cfg, **LIMITS}


PROFILE_DEFAULT = {
    "description": "Applied research in machine learning, computer vision, audio/speech and signal processing, robotics.",
    "keywords": [
        "data scientist", "data science", "artificial intelligence", "ai engineer", "machine learning",
        "deep learning", "computer vision", "image processing", "applied ai", "applied scientist",
        "research engineer", "research scientist", "r&d engineer", "audio processing", "speech processing",
        "speech recognition", "signal processing", "robotics", "autonomous systems", "multimodal",
        "neural network", "intelligent systems", "ml engineer", "ingenieur en intelligence artificielle",
        "ingenieur machine learning", "ingenieur en apprentissage automatique", "ingenieur de recherche",
        "chercheur en ia", "vision par ordinateur", "traitement du signal", "traitement de l'image",
        "traitement audio", "traitement de la parole", "reconnaissance vocale", "ingenieur r&d",
        "ingenieur ia", "ia", "ai", "nlp", "llm", "mlops", "ml ops", "ml model", "ai model",
        "model deployment", "generative ai", "genai", "ia generative", "apprentissage profond",
        "apprentissage automatique", "reseau de neurones", "deploiement de modeles"],
    "exclude_titles": ["accountant", "comptable", "sales", "commercial", "recruiter", "recruteur", "marketing",
                       "juridique", "paie", "payroll", "customer success", "support client", "it support"],
    "offer_types": ["job"],  # job | internship | apprenticeship | phd_postdoc
}
APPLICANT_DEFAULT = {"name": "", "address": "", "email": "", "phone": "", "linkedin": "", "website": "",
                     "signoff": "Veuillez agréer, Madame, Monsieur, l'expression de mes salutations distinguées."}
