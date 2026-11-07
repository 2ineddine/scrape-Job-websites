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
    "cv_consent_providers": [],  # with automatic switching: the hosted providers allowed to receive the CV
    "auto_models": True,  # start with the most capable model, move down the ladder when a limit is reached (llm.LADDER)
    "model": {"provider": "ollama", "model": "llama3.1"},  # the one model when automatic switching is off, else the last resort
    "providers": {
        "ollama": {"base_url": "http://localhost:11434"},
        "openai": {"base_url": "https://api.openai.com/v1"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
        "groq": {"base_url": "https://api.groq.com/openai/v1"},
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta"},
        "anthropic": {"base_url": "https://api.anthropic.com/v1"},
        "xai": {"base_url": "https://api.x.ai/v1"},
        "cohere": {"base_url": "https://api.cohere.ai/compatibility/v1"},
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
        "apprentissage automatique", "reseau de neurones", "deploiement de modeles",
        # job titles
        "machine learning engineer", "machine learning scientist", "ml scientist", "ai scientist", "ai researcher",
        "ml researcher", "ai developer", "developpeur ia", "datascientist", "algorithm engineer", "ingenieur algorithme",
        "ingenieur algorithmes", "algorithmie", "perception engineer", "ingenieur perception", "ingenieur vision",
        "ingenieur robotique", "roboticien", "dsp engineer", "ingenieur dsp", "ingenieur traitement d'images",
        "chercheur en intelligence artificielle", "chercheur en apprentissage automatique", "consultant data",
        "consultant ia",
        # topics
        "slam", "3d vision", "vision 3d", "point cloud", "nuage de points", "lidar", "sensor fusion", "fusion de capteurs",
        "fusion de donnees", "image analysis", "analyse d'images", "medical imaging", "imagerie medicale",
        "video analysis", "analyse video", "remote sensing", "teledetection", "earth observation", "embedded ai",
        "edge ai", "ia embarquee", "tinyml", "speech synthesis", "synthese vocale", "text-to-speech",
        "speaker recognition", "acoustique", "acoustics", "audio dsp", "foundation models", "large language models",
        "diffusion models", "generative models", "modeles generatifs", "ai agents", "agents ia", "agentic ai",
        "trustworthy ai", "ia de confiance", "explainable ai", "ia explicable", "time series", "series temporelles",
        "anomaly detection", "detection d'anomalies", "predictive maintenance", "maintenance predictive",
        "autonomous driving", "vehicule autonome", "conduite autonome"],
    "exclude_titles": ["accountant", "comptable", "sales", "commercial", "recruiter", "recruteur", "marketing",
                       "juridique", "paie", "payroll", "customer success", "support client", "it support"],
    "offer_types": ["job"],  # job | internship | apprenticeship | phd_postdoc
    "keywords_rev": 3,  # the defaults' keyword revision this profile has received (see upgrade_profile)
}
# keywords added to the defaults after profiles were saved, by revision. A saved profile receives each revision once: the
# user's own keywords stay, and a keyword the user deletes afterwards is not added back.
_KEYWORDS_ADDED = {2: ["mlops", "ml ops", "ml model", "ai model", "model deployment", "generative ai", "genai", "ia generative",
                       "apprentissage profond", "apprentissage automatique", "reseau de neurones", "deploiement de modeles"],
                   3: None}  # 3: every keyword after 'deploiement de modeles' in PROFILE_DEFAULT (filled below)
_kw = PROFILE_DEFAULT["keywords"]
_KEYWORDS_ADDED[3] = _kw[_kw.index("deploiement de modeles") + 1:]


def upgrade_profile(profile: dict) -> tuple[dict, bool]:
    """A profile saved before keywords were added to the defaults receives them once. -> (profile, changed)"""
    rev = int(profile.get("keywords_rev") or 1)
    if rev >= PROFILE_DEFAULT["keywords_rev"]:
        return profile, False
    kws = list(profile.get("keywords") or [])
    for r in sorted(_KEYWORDS_ADDED):
        if r > rev:
            kws += [k for k in _KEYWORDS_ADDED[r] if k not in kws]
    return {**profile, "keywords": kws, "keywords_rev": PROFILE_DEFAULT["keywords_rev"]}, True
APPLICANT_DEFAULT = {"name": "", "address": "", "email": "", "phone": "", "linkedin": "", "website": "",
                     "signoff": "Veuillez agréer, Madame, Monsieur, l'expression de mes salutations distinguées."}
