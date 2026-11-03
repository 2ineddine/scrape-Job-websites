"""dates.py - finding the PUBLICATION date of a job offer, in any format and language, from evidence.

Design: a job's publication date is established from EVIDENCE, never from a single field.
  1. parse_date()         understands almost any written format (ISO, 15/10/2026, 'Jul 01, 2026, 4:19PM', '1er juillet 2026',
                          'Oct 6', epoch, RFC-2822, '3 days ago', 'il y a 2 semaines', 'vor 3 Tagen', ...).
  2. find_dates()         finds EVERY date written in a text and says what it is: published / updated / deadline / start.
  3. reconcile_date()     an explicit 'Date de publication : ...' written in the job text beats a structured field
                          (feeds, JSON-LD and listings very often carry a refresh / re-index date). Conflicts that cannot be
                          settled become 'unknown'.
  4. date_decision()      compares CALENDAR DAYS with the window, never accepts a future date, and for an ambiguous
                          06/10/2026 it decides only if BOTH readings (6 Oct / 10 Jun) give the same answer.
  5. date_candidates()    the numbered list of dates handed to the AI, which only has to pick one."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bs4 import Comment, NavigableString

from .config import PRESET_DAYS, utcnow
from .web import norm

FR_MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
             "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "janv": 1, "fevr": 2, "fev": 2, "avr": 4,
             "juil": 7, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
EN_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
             "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
             "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
OTHER_MONTHS = {  # German, Spanish, Italian (accents are removed by norm())
    "januar": 1, "februar": 2, "marz": 3, "maerz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    "dez": 12, "okt": 10, "mrz": 3, "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "settembre": 9,
    "ottobre": 10, "dicembre": 12, "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8,
    "janeiro": 1, "fevereiro": 2, "marco": 3, "maio": 5, "junho": 6, "julho": 7, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12}
MONTHS = {**EN_MONTHS, **FR_MONTHS, **OTHER_MONTHS}
UNITS = {"minute": "minutes", "min": "minutes", "heure": "hours", "hour": "hours", "hr": "hours", "h": "hours",
         "stunde": "hours", "hora": "hours", "jour": "days", "day": "days", "j": "days", "d": "days", "tag": "days",
         "dia": "days", "giorno": "days", "semaine": "weeks", "sem": "weeks", "week": "weeks", "wk": "weeks",
         "w": "weeks", "woche": "weeks", "semana": "weeks", "mois": "months", "month": "months", "mo": "months",
         "monat": "months", "mes": "months", "meses": "months", "an": "years", "annee": "years", "year": "years",
         "yr": "years", "y": "years", "jahr": "years", "ano": "years"}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_UNIT = "|".join(sorted(UNITS, key=len, reverse=True))
_Y = r"(?:19|20)\d{2}"

# every way a calendar date is written (the text is lower-case, accent-free: see norm())
_ABS = re.compile((
    r"(?<![\w/])(?:"
    r"(?P<jm>@M@)\s+(?P<jd>\d{1,2})\s+\d{1,2}:\d{2}(?::\d{2})?\s+[a-z]{2,5}\s+(?P<jy>@Y@)"            # Wed Oct 14 00:00:00 UTC 2026
    r"|(?P<cy>@Y@)\s*年\s*(?P<cm>\d{1,2})\s*月\s*(?P<cd>\d{1,2})\s*日?"                                    # 2026年10月15日
    r"|(?P<iy>@Y@)[-/.](?P<im>\d{1,2})[-/.](?P<id>\d{1,2})(?!\d)"                                         # 2026-10-15
    r"|(?P<na>\d{1,2})[/.-](?P<nb>\d{1,2})[/.-](?P<ny>@Y@|\d{2})(?!\d)"                                   # 15/10/2026, 15.10.26
    r"|(?P<ad>\d{1,2})(?:er|st|nd|rd|th)?[\s.\-/]*(?:de\s+|of\s+|d')?(?P<am>@M@)(?![a-z])[\s.,\-/]*(?:de\s+|of\s+)?(?P<ay>@Y@)(?!\d)"   # 1er juillet 2026, 01-Jul-2026, 01Jul2026
    r"|(?P<sd>\d{1,2})[\-/](?P<sm>@M@)(?![a-z])[\-/](?P<sy>\d{2})(?!\d)"                                     # 01-JUL-26
    r"|(?P<bm>@M@)(?![a-z])[\s.\-/]*(?P<bd>\d{1,2})(?:st|nd|rd|th)?\b[\s,\-/]*(?P<by>@Y@)(?!\d)"                      # Jul 01, 2026
    r"|(?P<my>@M@)(?![a-z])\.?,?\s*(?P<myy>@Y@)(?!\d)"                                                           # Oct 2026 (no day)
    r"|(?P<ed>\d{1,2})(?:er|st|nd|rd|th)?\.?\s*(?:de\s+|of\s+|d')?(?P<em>@M@)(?![a-z])\.?(?!\s*,?\s*@Y@)"       # 06 Oct (no year)
    r"|(?P<fm>@M@)(?![a-z])\.?\s*(?P<fd>\d{1,2})(?:st|nd|rd|th)?\b(?![:\d]|\s*,?\s*@Y@)"                         # Oct 6 (no year)
    r"|(?P<gy>@Y@)(?P<gm>0[1-9]|1[0-2])(?P<gd>0[1-9]|[12]\d|3[01])(?!\d)"                                  # 20261015
    r")").replace("@M@", _MON).replace("@Y@", _Y))
_NUMW = r"\d+|un|une|a|an|one|ein|eine|einem|una|uno"
_REL = re.compile((
    r"(?<![a-z])(?:(?:il y a|vor|hace|depuis)\s+(?P<more1>plus d[e']\s*|more than\s+|over\s+|environ\s+|about\s+)?"
    r"(?P<n1>@N@)\s*(?P<p1>\+)?\s*(?P<u1>@U@)(?:s|es|n|en)?(?![a-z])"
    r"|(?P<n2>\d+|a|an|one)\s*(?P<p2>\+)?\s*(?P<u2>@U@)(?:s)?\s+ago(?![a-z]))").replace("@N@", _NUMW).replace("@U@", _UNIT))
_WORD_REL = re.compile(r"(?<![a-z])(?P<w>aujourd'?hui|today|just posted|a l'instant|just now|heute|hoy|oggi|"
                       r"hier|yesterday|gestern|ayer|ieri)(?![a-z])")
_YESTERDAY = {"hier", "yesterday", "gestern", "ayer", "ieri"}
_WORDNUM = {"un": 1, "une": 1, "a": 1, "an": 1, "one": 1, "ein": 1, "eine": 1, "einem": 1, "una": 1, "uno": 1}


def _year_for(month, day, now):
    """Date written without a year ('06 Oct'): the most recent such date that is not in the future."""
    for y in (now.year, now.year - 1):
        try:
            d = datetime(y, month, day)
        except ValueError:
            continue
        if d <= now + timedelta(days=2):
            return d
    return None


def _numeric_readings(a, b, y, dayfirst=None):
    """06/10/2026 -> [(date, confidence)]: ONE reading when only one is possible, else day-first and month-first."""
    out = []
    for day, month in ((a, b), (b, a)):
        try:
            out.append(datetime(y, month, day))
        except ValueError:
            pass
    if len(out) == 2 and out[0] == out[1]:
        out = out[:1]
    if len(out) == 1:
        return [(out[0], 0.8)]
    if len(out) == 2:
        if dayfirst is False:
            return [(out[1], 0.8)]
        if dayfirst:
            return [(out[0], 0.8)]
        return [(out[0], 0.45), (out[1], 0.45)]  # unknown convention: guessed day-first but marked unsure
    return []


def _interpret(m, now, dayfirst=None, allow_noyear=True):
    """One regex match of _ABS -> (datetime, confidence) | None."""
    g = m.groupdict()
    try:
        if g["jm"]:
            return datetime(int(g["jy"]), MONTHS[g["jm"]], int(g["jd"])), 0.9
        if g["cy"]:
            return datetime(int(g["cy"]), int(g["cm"]), int(g["cd"])), 0.9
        if g["iy"]:
            return datetime(int(g["iy"]), int(g["im"]), int(g["id"])), 0.85
        if g["na"]:
            y = int(g["ny"])
            short = len(g["ny"]) == 2
            if short:
                y += 2000
            r = _numeric_readings(int(g["na"]), int(g["nb"]), y, dayfirst)
            if not r:
                return None
            return r[0][0], (min(r[0][1], 0.7) if short and r[0][1] >= 0.5 else r[0][1])
        if g["ad"]:
            return datetime(int(g["ay"]), MONTHS[g["am"]], int(g["ad"])), 0.9
        if g["sd"]:
            return datetime(int(g["sy"]) + 2000, MONTHS[g["sm"]], int(g["sd"])), 0.8
        if g["bm"]:
            return datetime(int(g["by"]), MONTHS[g["bm"]], int(g["bd"])), 0.9
        if g["my"]:
            return datetime(int(g["myy"]), MONTHS[g["my"]], 1), 0.4  # the day is unknown
        if g["ed"] and allow_noyear:
            d = _year_for(MONTHS[g["em"]], int(g["ed"]), now)
            return (d, 0.65) if d else None
        if g["fm"] and allow_noyear:
            d = _year_for(MONTHS[g["fm"]], int(g["fd"]), now)
            return (d, 0.65) if d else None
        if g["gy"]:
            return datetime(int(g["gy"]), int(g["gm"]), int(g["gd"])), 0.7
    except (ValueError, KeyError):
        return None
    return None


def _interpret_rel(m, now):
    """One regex match of _REL -> (datetime, confidence)."""
    g = m.groupdict()
    n_raw, plus, u_raw, more = ((g["n1"], g["p1"], g["u1"], g["more1"]) if g["n1"] else (g["n2"], g["p2"], g["u2"], None))
    n = min(_WORDNUM.get(n_raw) or int(n_raw), 3650)
    unit = UNITS[u_raw]
    delta = {"minutes": timedelta(minutes=n), "hours": timedelta(hours=n), "days": timedelta(days=n),
             "weeks": timedelta(weeks=n), "months": timedelta(days=30 * n), "years": timedelta(days=365 * n)}[unit]
    conf = 0.45 if (plus or more or unit == "years") else 0.5 if unit == "months" else 0.7  # '30+ days ago' = at least 30
    return now - delta, conf


def _epoch(raw, now):
    m = re.fullmatch(r"/?date\((\d{10,13})(?:[+-]\d{4})?\)/?", raw.lower()) or re.fullmatch(r"(\d{10}(?:\.\d+)?|\d{13})", raw)
    if not m:
        return None
    v = float(m[1])
    v = v / 1000 if v > 1e11 else v
    try:
        d = datetime.fromtimestamp(v, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None
    return d if 2000 <= d.year <= now.year + 1 else None


def parse_date(text, now=None, dayfirst=None):
    """Return (naive-UTC datetime | None, confidence). Handles ISO, epoch, FR/EN/DE/ES/IT textual dates (with or without
    a year), numeric dates and relative dates. dayfirst: True = 06/10/2026 is 6 Oct, False = 10 Jun, None = unknown -> an
    ambiguous numeric date is guessed day-first but returned with LOW confidence (0.45 = 'unknown', so it is verified
    instead of trusted; date_decision() looks at both readings)."""
    if text is None or isinstance(text, bool):
        return None, 0.0
    raw = str(int(text)) if isinstance(text, (int, float)) else str(text).strip()
    if not raw:
        return None, 0.0
    now = now or utcnow()
    d, c = _parse_date(raw, now, dayfirst)
    if d is not None and not (2000 <= d.year <= now.year + 1):
        return None, 0.0
    return d, c


def _parse_date(raw, now, dayfirst):
    ep = _epoch(raw, now)
    if ep:
        return ep, 0.9
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if d.tzinfo:  # 'Z' is real UTC; an explicit offset is the poster's own clock: 06 Oct 00:00+02:00 stays 06 Oct
                d = d.astimezone(timezone.utc).replace(tzinfo=None) if raw.endswith("Z") else d.replace(tzinfo=None)
            return d, 0.95
        except ValueError:
            pass
    t = re.sub(r"\s+", " ", norm(raw))
    for m in _ABS.finditer(t):
        r = _interpret(m, now, dayfirst, allow_noyear=len(t) <= 45)  # a long text with 'oct 6' inside is not a date
        if r:
            return r
    m = _REL.search(t)
    if m:
        return _interpret_rel(m, now)
    m = _WORD_REL.search(t)
    if m:
        return now - timedelta(days=1 if m["w"] in _YESTERDAY else 0), 0.8
    m = re.search(r"(\d+)\s*(\+)?\s*(" + _UNIT + r")s?\b", t)
    if m and re.search(r"posted|publi|depuis|en ligne|online|active", t):  # 'Posted 3 days'
        n = min(int(m[1]), 3650)
        delta = {"minutes": timedelta(minutes=n), "hours": timedelta(hours=n), "days": timedelta(days=n),
                 "weeks": timedelta(weeks=n), "months": timedelta(days=30 * n), "years": timedelta(days=365 * n)}[UNITS[m[3]]]
        return now - delta, 0.45 if m[2] else 0.6
    return None, 0.0


# ---- which date is it? (published / updated / deadline / start) -----------------------------------------------------
_LABELS = (
    ("pub", re.compile(r"(?:date (?:de |d')?\s*(?:publication|mise en ligne|parution|diffusion|depot|edition|creation de l'(?:offre|annonce))|"
                       r"publication date|date of posting|date of issue|posted date|date listed|listed on|\bpublication\s*(?::|le\b)|"
                       r"gepubliceerd|geplaatst|opublikowano|datum objave|data de publicacao|\bpublicada\b|"
                       r"date of publication|date published|date posted|posting date|\bposted\b|\bpublished\b|"
                       r"\bpublie(?:e)?s?\b|mise en ligne|mis en ligne|en ligne depuis|parution|\bdiffusee?\b|\bdeposee?\b|"
                       r"first published|online since|veroffentlich\w*|\bpublicado\b|fecha de publicacion|"
                       r"\bpubblicat\w*|data di pubblicazione|date d'ajout|ajoutee? le|\bpostee? le|date de l'offre|"
                       r"offre (?:du|publiee)|date advertised|\badvertised\b|opening date|open date|date opened|date added|"
                       r"added on|job posted|created on|date de creation)")),
    ("upd", re.compile(r"(?:update[sd]?\b|modified|modification|\bmodifiee?\b|mise a jour|mis a jour|refreshed|renewed|"
                       r"renouvelee?|\bactive\b|actualis\w+|aktualisiert|actualizado|aggiornat\w+|reposted|republi\w+|last seen)")),
    ("end", re.compile(r"(?:deadline|date limite|closing|closes?\b|expires?\b|expiration|valid until|validity|valable|"
                       r"validite|jusqu'?au|avant le|apply (?:by|before)|candidater avant|candidature avant|cloture\w*|"
                       r"end date|date de fin|au plus tard|bis zum|hasta el|fino al)")),
    ("start", re.compile(r"(?:start(?:ing)? date|starts? on|date de debut|date de prise de poste|prise de poste|begin\w*|"
                         r"prise de fonctions?|date d'embauche|start of (?:the )?contract|starting|"
                         r"available from|disponible|demarrage|a pourvoir|a partir du|ab dem|entry date|embauche)")),
)
_FR_LABEL = re.compile(r"date de|publie|mise en|mis en|depose|diffuse|en ligne|veroffentl|parution")


class DateHit:
    __slots__ = ("kind", "date", "conf", "text", "start", "rel")

    def __init__(self, kind, date, conf, text, start, rel):
        self.kind, self.date, self.conf, self.text, self.start, self.rel = kind, date, conf, text, start, rel


def _label_before(ctx):
    """The label closest to the date ('Date de publication :' -> pub); None when the date is unlabelled."""
    best = None
    for kind, rx in _LABELS:
        for m in rx.finditer(ctx):
            if len(ctx) - m.end() <= 30 and (best is None or m.end() >= best[1]):
                best = (kind, m.end(), m.start(), m.group(0))
    return best


def find_dates(text, now=None, dayfirst=None, limit=60000):
    """Every date written in `text` with its role: pub (publication) | upd | end (deadline) | start | none (unlabelled)."""
    now = now or utcnow()
    t = str(text or "")[:limit]
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t).replace("_", " ")  # datePosted / date_publication
    t = re.sub(r"\s+", " ", norm(t))
    hits = []
    for rx, k in ((_ABS, "abs"), (_REL, "rel"), (_WORD_REL, "word")):
        hits += [(m.start(), m.end(), k, m) for m in rx.finditer(t)]
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    out, last_end = [], 0
    for s0, e0, k, m in hits:
        if s0 < last_end:  # overlaps the previous date
            continue
        ctx = t[max(last_end, s0 - 70):s0]
        lab = _label_before(ctx)
        kind = lab[0] if lab else "none"
        if kind == "none" and k == "rel":  # 'vor 3 Tagen veröffentlicht', 'il y a 3 jours, publiée': label AFTER a relative date
            after = _LABELS[0][1].match(t[e0:e0 + 30].lstrip(" ,-:|·"))
            kind = "pub" if after else kind
        df = dayfirst
        if df is None and lab and _FR_LABEL.search(lab[3]):
            df = True  # a French label: 05/10/2026 is 5 October
        if k == "abs":
            r = _interpret(m, now, df)
        elif k == "rel":
            r = _interpret_rel(m, now)
        else:  # 'today' / 'hier' only mean something right after a publication label
            r = (now - timedelta(days=1 if m["w"] in _YESTERDAY else 0), 0.8) if (kind == "pub" or m["w"] == "just posted") else None
            kind = "pub" if r and kind == "none" else kind
        last_end = e0
        if r:
            snippet = t[s0 - (len(ctx) - lab[2]):e0] if lab else t[s0:e0]
            out.append(DateHit(kind, r[0], r[1], snippet.strip(), s0, k != "abs"))
    return out


def text_publication_date(text, now=None, dayfirst=None, loose=False):
    """The EXPLICIT publication date written in a text ('Date de publication : Jul 01, 2026'). Update, deadline, start and
    unlabelled dates are ignored. -> (datetime|None, confidence, snippet, is_relative)"""
    now = now or utcnow()
    hits = [h for h in find_dates(text, now, dayfirst) if h.kind == "pub" and h.date <= now + timedelta(days=1)]
    absolute = [h for h in hits if not h.rel]
    use = absolute or hits
    if not use and loose:  # the job's own header often says just 'Il y a 3 jours' / '3 days ago' with no label
        use = [h for h in find_dates(text, now, dayfirst) if h.kind == "none" and h.rel and h.start < 400
               and h.conf >= 0.6 and h.date <= now + timedelta(days=1)]
    if not use:
        return None, 0.0, "", False
    h = use[0]
    conf = min(h.conf, 0.85 if h.kind == "pub" else 0.6)
    if len({x.date.date() for x in use}) > 1:
        conf = min(conf, 0.6)  # several different publication dates in the text: first one used, but less sure
    return h.date, conf, h.text, h.rel


_KIND_NAME = {"pub": "published", "upd": "updated", "end": "deadline", "start": "start date", "none": "unlabelled"}


def date_candidates(text, now=None, dayfirst=None, limit=10):
    """The dates written in a job text, numbered, with their role: what the AI chooses from (it answers a NUMBER, so it
    cannot invent a date). -> [(number, DateHit)] - publication-labelled dates first, then unlabelled ones."""
    now = now or utcnow()
    hits = [h for h in find_dates(text, now, dayfirst) if h.date <= now + timedelta(days=1)]
    hits.sort(key=lambda h: (h.kind != "pub", h.kind != "none", h.start))
    return list(enumerate(hits[:limit], 1))


def describe_candidates(cands) -> str:
    return "\n".join(f"[{i}] {h.date:%Y-%m-%d} ({_KIND_NAME[h.kind]}{', relative' if h.rel else ''}): '{h.text[:90]}'"
                      for i, h in cands) or "(no date written in the text)"


def date_readings(text, now=None, dayfirst=None):
    """All possible readings of the first date in `text`: 06/10/2026 -> [6 Oct, 10 Jun]; unambiguous -> one reading."""
    now = now or utcnow()
    t = re.sub(r"\s+", " ", norm(str(text or "")))
    for m in _ABS.finditer(t):
        g = m.groupdict()
        if g["na"]:
            y = int(g["ny"]) + (2000 if len(g["ny"]) == 2 else 0)
            r = [d for d, _ in _numeric_readings(int(g["na"]), int(g["nb"]), y, dayfirst)]
            if len(r) == 2 and dayfirst is None:
                return r
            return r[:1]
        r = _interpret(m, now, dayfirst)
        if r:
            return [r[0]]
    return []


# ---- decision --------------------------------------------------------------------------------------------------------
def _cfg_day(v):
    d, _ = parse_date(v) if v else (None, 0)
    return d.date() if d else None


def _window(cfg, now):
    today, preset = now.date(), cfg["date_preset"]
    if preset == "Custom date range":
        return (_cfg_day(cfg.get("date_from")) or datetime.min.date(), _cfg_day(cfg.get("date_to")) or today + timedelta(days=1))
    return today - timedelta(days=PRESET_DAYS.get(preset, 30)), today + timedelta(days=1)


def _in_window(day, cfg, now):
    """Calendar-day comparison (a date-only value has no time of day, so hours must not matter)."""
    lo, hi = _window(cfg, now)
    return lo <= day <= hi


def date_decision(pub, conf, cfg, now=None, text=None):
    """-> (True|False|None, explanation). None = unknown. `text` = the original date text: used to check BOTH readings
    of an ambiguous numeric date (06/10/2026) - the job is accepted / rejected only if both readings agree."""
    now = now or utcnow()
    preset = cfg["date_preset"]
    if preset == "All dates":
        return True, "all dates allowed"
    if pub is None:
        return None, "publication date unknown"
    if pub.date() > now.date() + timedelta(days=2):
        return None, f"publication date {pub:%Y-%m-%d} is in the future: not reliable"
    if (conf or 0) < 0.5:
        reads = date_readings(text, now) if text else []
        if len(reads) > 1:
            verdicts = {_in_window(r.date(), cfg, now) for r in reads if r.date() <= now.date() + timedelta(days=2)}
            if len(verdicts) == 1:
                ok = verdicts.pop()
                return ok, ("both readings of '%s' (%s) are %s '%s'" %
                            (text, " / ".join(f"{r:%Y-%m-%d}" for r in reads), "inside" if ok else "outside", preset))
        if text and _is_relative(text):  # 'il y a 1 an', '30+ days ago': vague, but a lower bound of the offer's age
            newest = (now - (now - pub) * 0.85).date()  # 'a year ago' is shown from about 11 months
            if newest < _window(cfg, now)[0]:
                return False, f"'{text}': published before {newest:%Y-%m-%d}, outside '{preset}'"
        return None, "publication date unknown"
    if _in_window(pub.date(), cfg, now):
        return True, f"published {pub:%Y-%m-%d}, inside window"
    return False, f"published {pub:%Y-%m-%d}, outside '{preset}'"


# ---- reconciling evidence --------------------------------------------------------------------------------------------
_UPDATE_WORDS = re.compile(r"updat|modif|mis a jour|expire|deadline|date limite|closing|valid|avant le|candidature avant|apply by")
_PUB_WORDS = re.compile(r"posted|published|publi|mise en ligne|date de publication|postee?\b")
_DAY = timedelta(days=1)
NO_DATE = (None, 0.0, "", "")


def merge_dates(a, b):
    """Two STRUCTURED dates (date, conf, source, text) for the same job (listing vs detail page, JSON vs JSON-LD...).
    Agree -> the more reliable one. A known date beats an unknown one. Disagree -> the EARLIEST is kept (refresh and
    re-index dates are always later than the real publication), with lower confidence."""
    if a[0] is None:
        return b
    if b[0] is None:
        return a
    if a[1] < 0.5 <= b[1]:
        return b
    if b[1] < 0.5 <= a[1]:
        return a
    if abs(a[0] - b[0]) <= _DAY:
        return a if a[1] >= b[1] else b
    first, other = (a, b) if a[0] <= b[0] else (b, a)
    return (first[0], min(first[1], 0.6), f"{first[2]} (conflict with {other[2]} {other[0]:%Y-%m-%d}; earliest kept)", first[3])


def reconcile_date(cur, texts, now=None, dayfirst=None):
    """cur = (date, conf, source, text) from structured data. texts = [(label, text, trusted)] where trusted=True is the
    job's own description. An explicit publication date written in a text WINS over a structured field that disagrees
    (-> 'Date de publication : Jul 01, 2026' beats a feed date 15/10/2026). Returns (date, conf, source, text)."""
    now = now or utcnow()
    d0, c0, s0, t0 = cur
    for label, txt, trusted in texts:
        if not txt:
            continue
        d1, c1, t1, rel1 = text_publication_date(txt, now, dayfirst, loose=trusted)
        if d1 is None or (not trusted and (rel1 or c1 < 0.6)):  # page widgets ('similar jobs: posted 2 days ago') are not trusted
            continue
        src = f"{label}: '{t1}'"
        if d0 is None or c0 < 0.5:
            return d1, c1, src, t1
        if abs(d0 - d1) <= _DAY:  # the text confirms the structured date
            return (d0, max(c0, c1), s0 if ("confirmed" in s0 or "'" in s0) else s0 + " + confirmed by text", t0)
        if c1 < 0.5:  # ambiguous 06/10/2026: do the two readings include the structured date?
            reads = date_readings(t1, now, dayfirst)
            if any(abs(r - d0) <= _DAY for r in reads):
                return d0, max(c0, 0.8), s0 + " + confirmed by text", t0
            return d1, c1, f"{src} (replaces {s0} {d0:%Y-%m-%d})", t1
        if not rel1:  # explicit absolute date in the text: it wins
            return d1, c1, f"{src} (replaces {s0} {d0:%Y-%m-%d})", t1
        if d0 > d1 + _DAY:  # relative text ('3 days ago') is EARLIER than the structured date: that one is a refresh date
            return d1, c1, f"{src} (replaces {s0} {d0:%Y-%m-%d})", t1
        return d0, 0.45, f"{s0} (conflict: text says '{t1}')", t0  # cannot settle it -> unknown -> to check
    return cur


def refine_job_date(job, now=None):
    """Re-read the job's own description for an explicit publication date (stored Job). -> True when the date changed."""
    cur = (job.pub_date, job.date_conf or 0.0, job.date_source or "", job.pub_date_text or "") if job.pub_date else NO_DATE
    new = reconcile_date(cur, [("job description", job.description, True)], now)
    if new != cur:
        job.pub_date, job.date_conf, job.date_source, job.pub_date_text = new
        return True
    return False


def refine_raw_date(raw, now=None):
    """Same for a freshly extracted job dict (_raw)."""
    cur = (raw["date"], raw["date_conf"], raw["date_source"], raw["date_text"]) if raw.get("date") else NO_DATE
    raw["date"], raw["date_conf"], raw["date_source"], raw["date_text"] = reconcile_date(
        cur, [("job description", raw.get("description"), True)], now)
    return raw


_META_SPECIFIC = {"article:published_time", "datepublished", "dateposted", "og:published_time", "publish_date", "pubdate",
                  "dc.date.issued"}
_META_GENERIC = {"date", "dcterms.created", "dcterms.date", "created", "posted"}  # often the page build date, not the job's


def _dayfirst(soup):
    """True / False / None from <html lang>: French, German, Spanish... write 06/10 as 6 Oct; en-US as 10 Jun; plain 'en' unknown."""
    h = soup.find("html")
    lang = ((h.get("lang") if h else "") or "").lower().replace("_", "-")
    if not lang or lang == "en":
        return None
    return False if lang == "en-us" else True


def _is_relative(txt) -> bool:
    return bool(re.search(r"\bago\b|il y a|today|aujourd|hier|yesterday|just|vor \d|hace \d", norm(txt or "")))


def _content_text(soup, scope="main", limit=30000):
    """Visible text of the job content: scripts, navigation, footers and side columns ('similar jobs') are left out.
    Computed once per page and scope (the date, location and offer checks all read it)."""
    cache = soup.__dict__.setdefault("_jf_text", {})
    if (scope, limit) not in cache:
        cache[(scope, limit)] = _content_text_uncached(soup, scope, limit)
    return cache[(scope, limit)]


def _content_text_uncached(soup, scope, limit):
    root = soup.body or soup
    if scope == "main":
        root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article") or root
    # menus, footers and side columns: marked once (a parent search per text would be quadratic on large pages)
    skip = {id(x) for t in root.find_all(["nav", "footer", "aside"]) for x in t.descendants if isinstance(x, NavigableString)}
    parts, size = [], 0
    for s in root.find_all(string=True):
        if isinstance(s, Comment) or s.parent is None or s.parent.name in ("script", "style", "noscript", "template", "head", "title"):
            continue
        if id(s) in skip:
            continue
        parts.append(str(s))
        size += len(s)
        if size > limit * 2:
            break
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:limit]


def html_visible_date(soup, now=None):
    """Publication date WRITTEN in the page text ('Posted 06 Oct 2026', 'Publiée le 6 octobre'). Update / deadline dates
    are ignored. -> (datetime|None, confidence, source, original_text)"""
    now, df = now or utcnow(), _dayfirst(soup)
    for scope in ("main", "body"):
        d, cf, txt, _rel = text_publication_date(_content_text(soup, scope), now, df)
        if d is not None:
            return d, min(cf, 0.75), "html visible text", txt
    return None, 0.0, "", ""


def html_date(soup, now=None):
    """Find the PUBLICATION date in page HTML: itemprop, specific meta tags, labelled <time>, visible 'Posted/Publiée le ...'
    text, then weaker unlabelled sources (low confidence = treated as unknown and verified later).
    Update / deadline dates are ignored. -> (datetime|None, confidence, source, original_text)"""
    now, df = now or utcnow(), _dayfirst(soup)
    ok = lambda d: d is not None and d <= now + timedelta(days=1)
    for tag in soup.find_all(attrs={"itemprop": re.compile("^(datePosted|datePublished)$", re.I)}):
        v = tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True)
        d, cf = parse_date(v, now, df)
        if ok(d):
            return d, min(cf, 0.9), "html itemprop", v
    metas = [((m.get("property") or m.get("name") or m.get("itemprop") or "").lower(), m.get("content")) for m in soup.find_all("meta")]
    for k, v in metas:
        if k in _META_SPECIFIC and v:
            d, cf = parse_date(v, now, df)
            if ok(d):
                return d, min(cf, 0.8), f"html meta {k}", v
    times = []  # <time> tags that are not update / deadline dates
    for t in soup.find_all("time"):
        v = t.get("datetime") or t.get_text(" ", strip=True)
        ctx = norm(t.parent.get_text(" ", strip=True)[:160]) if t.parent else ""
        if _UPDATE_WORDS.search(ctx):
            continue
        d, cf = parse_date(v, now, df)
        if ok(d):
            times.append((d, v, bool(_PUB_WORDS.search(ctx)), bool(t.find_parent(["footer", "aside", "nav", "header"]))))
    for d, v, labelled, _ in times:
        if labelled:
            return d, 0.85, "html <time>", v
    vis = html_visible_date(soup, now)
    if vis[0]:
        return vis
    for k, v in metas:
        if k in _META_GENERIC and v:
            d, cf = parse_date(v, now, df)
            if ok(d):
                return d, min(cf, 0.45), f"html meta {k} (generic: may be the page date)", v
    body = [x for x in times if not x[3]]
    if len(times) == 1 and body:
        return body[0][0], 0.6, "html <time>", body[0][1]
    if body:  # several unlabelled dates on the page: cannot tell which one is the publication date
        return body[0][0], 0.45, "html <time> (unlabelled, several on the page)", body[0][1]
    return None, 0.0, "", ""
