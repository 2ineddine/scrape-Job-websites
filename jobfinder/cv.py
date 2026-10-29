"""cv.py - CV manager (PDF text extraction), CV / job matching with verified evidence, and French LaTeX cover letters."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import DATA_DIR
from .llm import AIError, call_json, require_cv_consent
from .store import CoverLetter, CVDocument, Job
from .web import norm


# --------------------------------------------------------------------------- CV manager
def extract_cv(data: bytes):
    """-> (text, language, status). Raises ValueError for non-PDF / oversize files."""
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("PDF larger than 15 MB")
    if not data.startswith(b"%PDF"):
        raise ValueError("Not a PDF file")
    import pymupdf as fitz
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.page_count > 30:
        raise ValueError("More than 30 pages")
    text = "\n".join(p.get_text() for p in doc).strip()
    t = " " + norm(text) + " "
    fr = sum(t.count(w) for w in (" le ", " la ", " et ", " des ", " pour ", " avec "))
    en = sum(t.count(w) for w in (" the ", " and ", " of ", " with ", " for ", " in "))
    lang = "fr" if fr > en else "en" if en else "unknown"
    return text, lang, ("ok" if len(text) >= 200 else "needs_ocr")


def _store_pdf(data: bytes) -> tuple[str, Path]:
    sha = hashlib.sha256(data).hexdigest()
    p = DATA_DIR / "cvs" / f"{sha[:24]}.pdf"
    if not p.exists():
        p.write_bytes(data)
        os.chmod(p, 0o444)  # originals are never modified
    return sha, p


def add_cv(s, filename, data: bytes, name=None):
    text, lang, status = extract_cv(data)
    sha, p = _store_pdf(data)
    ex = s.query(CVDocument).filter_by(sha256=sha).first()
    if ex:
        return ex, False
    cv = CVDocument(name=name or Path(filename).stem, filename=filename, sha256=sha, path=str(p), text=text,
                    language=lang, status=status)
    s.add(cv)
    s.commit()
    return cv, True


def replace_cv(s, cv_id, filename, data: bytes):
    cv = s.get(CVDocument, cv_id)
    text, lang, status = extract_cv(data)
    sha, p = _store_pdf(data)
    cv.sha256, cv.path, cv.text, cv.language, cv.status, cv.filename = sha, str(p), text, lang, status, filename
    s.commit()


def delete_cv(s, cv_id):
    """Removes the CV from the app AND its PDF from the disk."""
    cv = s.get(CVDocument, cv_id)
    if cv:
        path = Path(cv.path) if cv.path else None
        s.delete(cv)
        s.commit()
        if path and path.is_file() and path.resolve().parent == (DATA_DIR / "cvs").resolve():
            path.unlink()


# --------------------------------------------------------------------------- matching
RUBRIC = "skills & tools 30, experience 30, research topics 20, education & seniority 10, languages & location 10"


class Evidence(BaseModel):
    requirement: str
    cv_evidence: str


class CVMatch(BaseModel):
    match_score: float = Field(ge=0, le=100)
    matching_skills: list[str] = []
    matching_experience: list[str] = []
    missing_requirements: list[str] = []  # clearly absent / contradicted
    not_evidenced: list[str] = []  # simply not mentioned in the CV
    evidence: list[Evidence] = []
    recommendation: Literal["apply", "maybe", "skip"] = "maybe"
    confidence: float = Field(ge=0, le=1)
    removed_evidence: int = 0

    @field_validator("match_score", mode="before")
    @classmethod
    def _percent(cls, v):  # '0.85' means 85 / 100
        return v * 100 if isinstance(v, (int, float)) and 0 < v <= 1 else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _unit(cls, v):  # '85' means 0.85
        return v / 100 if isinstance(v, (int, float)) and 1 < v <= 100 else v


def _ws(x):
    return re.sub(r"\s+", " ", norm(x))


def verify_evidence(m: CVMatch, cv_text: str) -> CVMatch:
    """Drop any evidence excerpt that is not literally present in the CV (anti-invention guard)."""
    hay, keep = _ws(cv_text), []
    for e in m.evidence:
        if len(_ws(e.cv_evidence)) >= 8 and _ws(e.cv_evidence) in hay:
            keep.append(e)
    m.removed_evidence = len(m.evidence) - len(keep)
    m.evidence = keep
    return m


def match_job(s, job: Job, cfg, keys=None):
    require_cv_consent(cfg)
    cvs = s.query(CVDocument).filter_by(enabled=True).all()
    cvs = [c for c in cvs if c.status == "ok"]
    if not cvs:
        raise AIError("No enabled CV with extractable text")
    results = {}
    for cv in cvs:  # every eligible CV is evaluated before choosing
        user = (f"Score how well this CV fits the job. Weights: {RUBRIC}. Separate requirements that are clearly "
                "missing from those that are merely not mentioned. 'cv_evidence' must be a verbatim excerpt from the CV.\n"
                f"<untrusted>\nJOB: {job.title} @ {job.employer} ({job.location_original})\n"
                f"{(job.description or '')[:3500]}\n</untrusted>\n<untrusted>\nCV ({cv.name}):\n{cv.text[:6000]}\n</untrusted>\n"
                "JSON keys: match_score (0-100), matching_skills[], matching_experience[], missing_requirements[], "
                "not_evidenced[], evidence[{requirement,cv_evidence}], recommendation(apply|maybe|skip), confidence (0-1)")
        results[cv.name] = verify_evidence(call_json(user, CVMatch, cfg, s, keys), cv.text).model_dump()
    best = max(results, key=lambda k: results[k]["match_score"])
    job.best_cv, job.match_score, job.match_json = best, results[best]["match_score"], results
    s.commit()
    return results


# --------------------------------------------------------------------------- LaTeX letters
_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
        "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(s) -> str:
    """Escape untrusted text for LaTeX in a single pass (no command injection possible)."""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\U00010000-\U0010ffff]", "", str(s or ""))
    return "".join(_TEX.get(c, c) for c in s)


def tex_url(u: str) -> str | None:
    u = (u or "").strip()
    if not re.fullmatch(r"https?://[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+", u):
        return None
    return u.replace("%", r"\%").replace("#", r"\#").replace("&", r"\&")


MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
        "décembre"]


def french_date(d: datetime) -> str:
    return f"{d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]} {d.year}"


class Letter(BaseModel):
    subject: str
    salutation: str = "Madame, Monsieur,"
    paragraphs: list[str] = Field(min_length=2, max_length=6)


def generate_letter_content(s, job: Job, cv: CVDocument, cfg, keys=None) -> Letter:
    require_cv_consent(cfg)
    m = (job.match_json or {}).get(cv.name, {})
    ev = "\n".join(f"- {e['requirement']}: {e['cv_evidence']}" for e in m.get("evidence", []))
    user = ("Écris une lettre de motivation en français professionnel (250 mots maximum, 3 à 4 paragraphes) pour ce poste. "
            "Utilise UNIQUEMENT l'expérience réellement présente dans le CV. N'invente ni diplôme, ni publication, ni projet, "
            "ni réalisation. Ne prétends rien savoir de l'entreprise au-delà de l'annonce. Ne recopie pas l'annonce. "
            "Pas de formule d'adresse ni de signature (elles sont ajoutées automatiquement).\n"
            f"<untrusted>\nPOSTE: {job.title} - {job.employer} - {job.location_original}\n"
            f"ANNONCE: {(job.description or '')[:4000]}\n</untrusted>\n"
            f"<untrusted>\nCV:\n{cv.text[:9000]}\n</untrusted>\nPreuves déjà vérifiées:\n{ev}\n"
            'JSON keys: subject (objet de la lettre), salutation, paragraphs[] (liste de textes bruts)')
    return call_json(user, Letter, cfg, s, keys)


def build_tex(applicant: dict, job: Job, letter: Letter, now: datetime | None = None) -> str:
    now = now or datetime.now()
    a = lambda k: tex(applicant.get(k, ""))
    addr = r" \\ ".join(tex(x) for x in applicant.get("address", "").splitlines() if x.strip())
    lines = [rf"\textbf{{{a('name')}}}"] + ([addr] if addr else []) + [x for x in (
        a("email"), a("phone")) if x]
    for k in ("linkedin", "website"):
        u = tex_url(applicant.get(k, ""))
        if u:
            lines.append(rf"\href{{{u}}}{{{tex(applicant[k].split('//', 1)[-1])}}}")
    rec = [rf"\textbf{{{tex(job.employer)}}}", r"Service Recrutement"]
    if job.city:
        rec.append(tex(job.city))
    ref = f" (réf. {tex(job.platform_job_id)})" if job.platform_job_id else ""
    place = tex(job.city) if job.city else ""
    paras = "\n\n".join(tex(p) for p in letter.paragraphs)
    sep = " \\\\ "  # LaTeX line break between lines
    left, right = sep.join(lines), sep.join(rec)
    when = (place + ", " if place else "") + "le " + french_date(now)
    signoff = tex(applicant.get("signoff") or "Veuillez agréer, Madame, Monsieur, l'expression de mes salutations distinguées.")
    subject, salutation, name = tex(letter.subject), tex(letter.salutation), a("name")
    return rf"""\documentclass[11pt,a4paper]{{article}}

\usepackage[margin=2cm,top=0.7cm,bottom=0.7cm]{{geometry}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[french]{{babel}}
\usepackage{{graphicx}}
\usepackage[hidelinks]{{hyperref}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0.6em}}

\begin{{document}}
\thispagestyle{{empty}}

% Applicant information (left) / recipient (right)
\noindent\begin{{minipage}}[t]{{0.48\textwidth}}
{left}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.45\textwidth}}\raggedleft
{right}
\end{{minipage}}

\vspace{{0.5cm}}

\begin{{flushright}}
{when}
\end{{flushright}}

\vspace{{0.5cm}}

\begin{{center}}
\textbf{{Objet : {subject}{ref}}}
\end{{center}}

\vspace{{0.5cm}}

{salutation}

{paras}

\vspace{{0.5cm}}

{signoff}

\vspace{{0.4cm}}
{name}

\end{{document}}
"""


def compile_tex(path: Path):
    """Compile with latexmk/pdflatex if installed. No shell; fixed argv. -> (pdf_path|None, message)"""
    path = Path(path)
    if shutil.which("latexmk"):
        cmd = ["latexmk", "-pdf", "-silent", "-halt-on-error",
               "-pdflatex=pdflatex -no-shell-escape -interaction=nonstopmode %O %S", path.name]
    elif shutil.which("pdflatex"):
        cmd = ["pdflatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", path.name]
    else:
        return None, ("No LaTeX compiler found. Install TeX Live / MiKTeX, then run: "
                      f"pdflatex {path.name}")
    try:
        r = subprocess.run(cmd, cwd=path.parent, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None, "latex_compilation_failed: timeout"
    pdf = path.with_suffix(".pdf")
    if r.returncode == 0 and pdf.exists():
        return pdf, "compiled"
    return None, "latex_compilation_failed:\n" + (r.stdout or r.stderr)[-1500:]


def save_letter(s, job: Job, cv_name: str, tex_source: str, note="") -> CoverLetter:
    """Every save creates a new version; earlier versions are never overwritten."""
    v = s.query(CoverLetter).filter_by(job_uid=job.uid).count() + 1
    p = DATA_DIR / "letters" / f"{job.uid}_v{v}.tex"
    p.write_text(tex_source, encoding="utf-8")
    pdf, msg = compile_tex(p)
    cl = CoverLetter(job_uid=job.uid, version=v, cv_name=cv_name, tex_path=str(p), pdf_path=str(pdf) if pdf else None,
                     note=msg if not pdf else note)
    s.add(cl)
    s.commit()
    return cl
