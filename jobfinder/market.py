"""market.py - statistics of the job market, computed every 15 days from the offers the AI has analysed.

Each scan deletes its offers; the facts the statistics need are copied first into market_offers (kept 6 months):
company, city, contract, dates, relevance, CV match, and what the AI read in the text (skills, degree, seniority,
languages, remote, salary). Every 15 days a snapshot of the last 15 days is stored in market_stats."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from .config import LOG, utcnow
from .store import CVDocument, Job, MarketOffer, MarketStats, Source

PERIOD_DAYS = 15
KEEP_DAYS = 183
_ALIAS = {"pytorch": "PyTorch", "tensorflow": "TensorFlow", "python": "Python", "c++": "C++", "cpp": "C++", "c": "C",
          "ros": "ROS", "ros2": "ROS 2", "ros 2": "ROS 2", "sql": "SQL", "aws": "AWS", "gcp": "GCP", "azure": "Azure",
          "docker": "Docker", "kubernetes": "Kubernetes", "k8s": "Kubernetes", "linux": "Linux", "git": "Git",
          "computer vision": "Computer vision", "vision par ordinateur": "Computer vision", "deep learning": "Deep learning",
          "apprentissage profond": "Deep learning", "machine learning": "Machine learning",
          "apprentissage automatique": "Machine learning", "llm": "LLMs", "llms": "LLMs", "nlp": "NLP", "mlops": "MLOps",
          "cuda": "CUDA", "matlab": "MATLAB", "scikit-learn": "scikit-learn", "sklearn": "scikit-learn", "opencv": "OpenCV",
          "large language model": "LLMs", "large language models": "LLMs", "gpu": "GPU", "gpus": "GPU",
          "reinforcement learning": "Reinforcement learning", "rl": "Reinforcement learning",
          "anglais": "English", "english": "English", "français": "French", "francais": "French", "french": "French"}


_COUNTRIES = {"france", "fr", "europe", "emea", "remote", "teletravail", "télétravail", "worldwide"}
_KNOWN = {"airliquidehr": "Air Liquide", "infiniteorbits": "Infinite Orbits", "gehealthcare": "GE HealthCare",
          "gevernova": "GE Vernova", "pernodricard": "Pernod Ricard", "arianegroup": "ArianeGroup", "mistral": "Mistral AI",
          "alicebob": "Alice & Bob"}


def company_name(x) -> str:
    """One readable name per company: 'mistral.ai' -> 'Mistral AI', 'GE HEALTHCARE' -> 'GE HealthCare', 'Airliquidehr'
    (a name read from the website address) -> 'Air Liquide'."""
    raw = re.sub(r"\s+", " ", str(x or "")).strip()
    if not raw:
        return ""
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    m = re.fullmatch(r"([a-z0-9-]+)\.(ai|com|io|fr|co|eu|tech)", raw.lower())
    if m:
        key = re.sub(r"[^a-z0-9]", "", m[1])
        raw = m[1].replace("-", " ").title() + (" AI" if m[2] == "ai" else "")
    if key in _KNOWN:
        return _KNOWN[key]
    if raw.islower():  # a name read from an address: 'alta-ares' -> 'Alta Ares', 'quandela' -> 'Quandela'
        return raw.replace("-", " ").replace("_", " ").title()
    return raw.title() if raw.isupper() and len(raw) > 4 else raw


_ACRONYMS = {"nlp": "natural language processing", "llm": "large language model", "llms": "large language models",
             "ml": "machine learning", "dl": "deep learning", "rl": "reinforcement learning", "cv": "computer vision",
             "ai": "artificial intelligence", "ia": "intelligence artificielle", "mlops": "machine learning operations",
             "ci/cd": "continuous integration", "hpc": "high performance computing", "slam": "simultaneous localization and mapping"}


def grounded(items, text) -> list:
    """The skills the offer really names: every word of the skill appears in the text, compared on its first 5 letters
    without accents ('optimization' ~ 'optimisation', 'robotics' ~ 'robotique', 'modeling' ~ 'models'); a short word may be
    in the plural ('LLM' ~ 'LLMs', 'GPU' ~ 'GPUs'). A skill the model only guessed ('Python' for an offer that never says
    it) is dropped."""
    from .web import norm
    words = set(re.findall(r"[a-z0-9+#]+", norm(text or "")))
    stems = {w[:5] for w in words}
    found = lambda w: (w[:5] in stems) if len(w) >= 5 else (w in words or w + "s" in words or w + "es" in words)
    named = lambda s: bool(s) and all(found(w) for w in s)
    out = []
    for x in items or []:
        n = norm(str(x)).strip()
        toks = [w for w in re.findall(r"[a-z0-9+#]+", n) if len(w) >= 2 or w in ("c", "r")]
        # an acronym and its words are the same skill: 'NLP' is named by 'natural language processing', and back
        alt = _ACRONYMS.get(n) or next((a for a, full in _ACRONYMS.items() if full == n), "")
        alt_toks = re.findall(r"[a-z0-9+#]+", alt)
        if named(toks) or named(alt_toks):
            out.append(x)
    return out


def norm_term(x) -> str:
    """The comparison key of a skill or language: 'pytorch', 'PyTorch ' and 'Py Torch' -> 'PyTorch'."""
    k = re.sub(r"\s+", " ", str(x or "")).strip().strip(".;:,").lower()
    if not k or len(k) > 40:
        return ""
    return _ALIAS.get(k) or _ALIAS.get(k.replace(" ", "")) or k


def count_terms(lists) -> Counter:
    """Each offer counts once per skill; a skill is shown as the offers mostly write it ('HPC', 'JAX', 'CI/CD'), an
    all-lowercase one with a capital ('robotics' -> 'Robotics')."""
    keys, spellings = Counter(), {}
    for items in lists:
        seen = set()
        for x in items or []:
            k = norm_term(x)
            if not k or k in seen:
                continue
            seen.add(k)
            keys[k] += 1
            raw = re.sub(r"\s+", " ", str(x)).strip().strip(".;:,")
            spellings.setdefault(k, Counter())[raw if raw != raw.lower() else raw[:1].upper() + raw[1:]] += 1
    show = lambda k: k if k in _ALIAS.values() else spellings[k].most_common(1)[0][0]
    return Counter({show(k): n for k, n in keys.items()})


def archive_offers(s, now=None) -> int:
    """Copy the analysed offers of the current session into the market history (new ones, and updates of known ones)."""
    now = now or utcnow()
    srcs = {x.id: x.label for x in s.query(Source).all()}
    cvs = {c.name: c.text or "" for c in s.query(CVDocument).filter_by(enabled=True) if c.status == "ok"}
    n = 0
    for j in s.query(Job).filter(Job.relevance_score.isnot(None), Job.status != "out_of_range"):
        ai = j.relevance_json or {}
        row = s.get(MarketOffer, j.uid) or MarketOffer(uid=j.uid, seen=now)
        row.pub_date, row.title, row.company = j.pub_date, (j.title or "")[:200], (j.employer or srcs.get(j.source_id, ""))[:120]
        row.city, row.contract = (j.city or "")[:80], ai.get("contract_kind") or ""
        row.relevant, row.score, row.match = j.status == "relevant", j.relevance_score, j.match_score
        # only the skills the offer really names (the model sometimes adds 'Python' to an offer that never says it)
        skills = grounded(ai.get("skills") or [], f"{j.title or ''} {j.description or ''}")
        # the skills this offer names that the best-matching CV does not mention: what to learn or to make visible
        cv = cvs.get(j.best_cv or "") or (next(iter(cvs.values())) if len(cvs) == 1 else "")
        gaps = [x for x in skills if not grounded([x], cv)] if cv else []
        row.data = {"skills": skills, "degree": ai.get("degree") or "unknown",
                    "seniority": ai.get("seniority") or "unknown", "languages": ai.get("languages") or [],
                    "remote": ai.get("remote") or "unknown", "salary": ai.get("salary_text") or "",
                    "gaps": gaps,
                    "website": srcs.get(j.source_id, "")}
        s.merge(row)
        n += 1
    s.query(MarketOffer).filter(MarketOffer.seen < now - timedelta(days=KEEP_DAYS)).delete(synchronize_session=False)
    s.commit()
    return n


def _cities(text):
    """'Paris & Aubervilliers' -> ['Paris', 'Aubervilliers']; a country or 'remote' is not a city."""
    parts = re.split(r"\s*(?:&|;|/|,|\bet\b|\band\b)\s*", str(text or ""))
    return list(dict.fromkeys(p.strip() for p in parts if p.strip() and p.strip().lower() not in _COUNTRIES))


def _top(counter, n):
    """The n most frequent, ties in alphabetical order (a stable page from one view to the next)."""
    return [[k, v] for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:n] if k]


def compute_snapshot(s, now=None, days=PERIOD_DAYS):
    """Statistics of the offers analysed in the last `days` days. -> the stored MarketStats row (None without offers)."""
    now = now or utcnow()
    start = now - timedelta(days=days)
    rows = s.query(MarketOffer).filter(MarketOffer.seen >= start).all()
    if not rows:
        return None
    names = {}  # one spelling per company: the first readable one met
    for r in rows:
        n = company_name(r.company)
        names.setdefault(re.sub(r"[^a-z0-9]", "", n.lower()), n)
    company = lambda r: names.get(re.sub(r"[^a-z0-9]", "", company_name(r.company).lower()), "")
    rel = [r for r in rows if r.relevant]
    base = rel or rows  # the market of the client's field; all analysed offers until some are relevant
    d = lambda r: r.data or {}
    skills = count_terms(d(r).get("skills", []) for r in base)
    gaps = count_terms(d(r).get("gaps", []) for r in base)
    weeks = Counter((r.pub_date or r.seen).strftime("%G-W%V") for r in base)
    matched = [r for r in base if r.match is not None]
    titles = Counter(re.sub(r"\s*[(\[].*?[)\]]\s*", " ", r.title or "").strip() for r in base)
    data = {
        "days": days, "analysed": len(rows), "relevant": len(rel), "base": "relevant" if rel else "analysed",
        "companies": len({company(r) for r in base if company(r)}),
        "top_companies": _top(Counter(company(r) for r in base), 15),
        "top_skills": _top(skills, 20), "skill_gaps": _top(gaps, 15),
        "cities": _top(Counter(c for r in base for c in _cities(r.city)), 10),
        "contracts": _top(Counter(r.contract or "unknown" for r in base), 6),
        "degree": _top(Counter(d(r).get("degree", "unknown") for r in base), 6),
        "seniority": _top(Counter(d(r).get("seniority", "unknown") for r in base), 6),
        "remote": _top(Counter(d(r).get("remote", "unknown") for r in base), 4),
        "languages": _top(count_terms(d(r).get("languages", []) for r in base), 6),
        "salaries": [[r.title, company(r), d(r)["salary"]] for r in base if re.search(r"\d", d(r).get("salary") or "")][:15],
        "weeks": sorted(weeks.items()),
        "top_titles": _top(titles, 10),
        "avg_match": round(sum(r.match for r in matched) / len(matched), 1) if matched else None,
        "best_titles": [[r.title, company(r), round(r.match)] for r in sorted(matched, key=lambda r: -r.match)[:10]],
        "websites": _top(Counter(d(r).get("website", "") for r in base), 10),
    }
    snap = MarketStats(created=now, period_start=start, period_end=now, data=data)
    s.add(snap)
    s.commit()
    return snap


def due(s, now=None) -> bool:
    last = s.query(MarketStats).order_by(MarketStats.created.desc()).first()
    return last is None or last.created <= (now or utcnow()) - timedelta(days=PERIOD_DAYS)


def run_if_due(s, now=None, force=False):
    """The 15-day schedule: called when the app starts and after each AI analysis. force: the scan was started with
    'Update the statistics at the end of this scan' ticked."""
    try:
        archive_offers(s, now)
        if force or due(s, now):
            snap = compute_snapshot(s, now)
            if snap:
                LOG.info("market statistics computed for %s - %s", f"{snap.period_start:%Y-%m-%d}", f"{snap.period_end:%Y-%m-%d}")
            return snap
    except Exception as e:  # pragma: no cover - statistics never stop a scan
        s.rollback()
        LOG.warning("market statistics failed: %s", e)
    return None


def snapshots(s, n=2):
    """The last n snapshots, newest first."""
    return s.query(MarketStats).order_by(MarketStats.created.desc()).limit(n).all()
