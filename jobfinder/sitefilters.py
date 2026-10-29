"""sitefilters.py - the career website's OWN filters, used without AI: its search form (GET) or links that show only
France, only recent offers ('posted within 7 days') or the newest offers first. The filtered list is smaller and sorted,
so fewer pages are read; the app's own date and location checks still run on every offer afterwards."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .config import PRESET_DAYS
from .web import norm, normalize_url, syntactic_ok

_COUNTRY_FIELD = re.compile(r"countr|pays|location|localisation|lieu|region|place|site|where|ou\b|zone|geo", re.I)
_DATE_FIELD = re.compile(r"date|posted|publi|period|since|depuis|freshness|age|recent", re.I)
_SORT_FIELD = re.compile(r"sort|order|tri\b|trier|classer|orderby", re.I)
_SORT_NEW = re.compile(r"(plus )?r[ée]cent|newest|latest|most recent|date de (?:publication|mise en ligne)|posted date|"
                       r"publication date|\bdate\b|nouveaut", re.I)
_NOT_NEWEST = re.compile(r"limite|deadline|expir|closing|cl[ôo]ture|ancien|oldest|croissant|ascending|\basc\b", re.I)
_SORT_LINK = re.compile(r"[?&](?:sort|sortby|sort_by|order|orderby|tri)=", re.I)
_PERIOD = [(re.compile(r"(\d+)\s*(?:h|heures?|hours?)\b", re.I), 1 / 24), (re.compile(r"(\d+)\s*(?:j|jours?|days?)\b", re.I), 1),
           (re.compile(r"(\d+)\s*(?:semaines?|weeks?)\b", re.I), 7), (re.compile(r"(\d+)\s*(?:mois|months?)\b", re.I), 30)]
_WORDS = [(re.compile(r"aujourd|today|24\s*h|dernier jour|last day|past day", re.I), 1),
          (re.compile(r"semaine|week", re.I), 7), (re.compile(r"\bmois\b|month", re.I), 30)]


def window_days(cfg):
    p = cfg.get("date_preset", "Last 30 days")
    return None if p == "All dates" else PRESET_DAYS.get(p, 30)


def _option_days(text):
    """'Moins de 7 jours' -> 7, 'Past week' -> 7, 'Last 24 hours' -> 1; None when the option is not a period."""
    t = norm(text)
    for rx, mult in _PERIOD:
        m = rx.search(t)
        if m:
            return int(m[1]) * mult
    return next((d for rx, d in _WORDS if rx.search(t)), None)


def _label(form, el):
    lab = form.find("label", attrs={"for": el.get("id")}) if el.get("id") else None
    return " ".join(x for x in (el.get("name"), el.get("id"), el.get("aria-label"), lab.get_text(" ") if lab else "") if x)


def _choose(form, sel, wants_france, days):
    """-> (value, what) for one <select> of the form, or None when it is not a filter we use."""
    opts = [(o.get("value") if o.get("value") is not None else o.get_text(" ", strip=True), o.get_text(" ", strip=True))
            for o in sel.find_all("option")]
    lab = _label(form, sel)
    if wants_france and (_COUNTRY_FIELD.search(lab) or len(opts) >= 5):
        fr = next((v for v, t in opts if v and (norm(t).startswith("france") or str(v).strip().upper() in ("FR", "FRA")
                                               or norm(str(v)) == "france")), None)
        if fr:
            return fr, "France"
    if _SORT_FIELD.search(lab):
        new = next((v for v, t in opts if v and _SORT_NEW.search(t) and not _NOT_NEWEST.search(f"{t} {v}")), None)
        if new:
            return new, "newest first"
    if days and _DATE_FIELD.search(lab):
        periods = [(d, v) for v, t in opts if v and (d := _option_days(t))]
        fits = sorted((d, v) for d, v in periods if d >= days)
        if fits:
            return fits[0][1], f"posted within {fits[0][0]:g} day(s)"
    return None


def filtered_url(soup, url, cfg):
    """-> (url, [what was applied]) using the page's own GET search form, else its 'sort by date' link; (None, [])."""
    wants_france = norm(cfg.get("site_filter_location") or "") in ("france", "fr")
    days = window_days(cfg)
    for form in soup.find_all("form"):
        if (form.get("method") or "get").lower() != "get":
            continue  # a POST form cannot be expressed as a link
        action = urljoin(url, form.get("action") or url)
        if not syntactic_ok(action) or urlparse(action).hostname != urlparse(url).hostname:
            continue
        fields, used = [], []
        for el in form.find_all(["input", "select"]):
            name = el.get("name")
            if not name or el.get("disabled") is not None:
                continue
            if el.name == "select":
                pick = _choose(form, el, wants_france, days)
                if pick:
                    fields.append((name, pick[0]))
                    used.append(pick[1])
                else:
                    cur = el.find("option", selected=True)
                    if cur is not None and cur.get("value"):
                        fields.append((name, cur["value"]))
            elif (el.get("type") or "text").lower() in ("hidden", "text", "search"):
                fields.append((name, el.get("value") or ""))
            elif (el.get("type") or "").lower() in ("checkbox", "radio") and el.get("checked") is not None:
                fields.append((name, el.get("value") or "on"))
        if used:
            keep = [(k, v) for k, v in parse_qsl(urlparse(url).query) if k not in {n for n, _ in fields}]
            p = urlparse(action)
            return urlunparse(p._replace(query=urlencode(keep + [(k, v) for k, v in fields if v != ""]))), used
    for a in soup.find_all("a", href=True):  # 'Trier par : Plus récentes' as a plain link
        u = urljoin(url, a["href"])
        txt = f"{a.get_text(' ', strip=True)} {a.get('title') or ''} {a.get('aria-label') or ''}"
        if (_SORT_LINK.search(u) and _SORT_NEW.search(txt) and not _NOT_NEWEST.search(f"{txt} {u}") and syntactic_ok(u)
                and urlparse(u).hostname == urlparse(url).hostname and normalize_url(u) != normalize_url(url)):
            return u, ["newest first"]
    return None, []
