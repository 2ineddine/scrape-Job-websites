"""apply.py - 'Open and fill the application': the offer's application form opened in a browser window the app controls, on
the user's own computer, with the user's details and a short message written for the offer filled in.

Accounts: when a website asks the candidate to sign in, the app signs in with the account it created there before, or
creates one (the user's account email, a strong password generated for that website alone, kept in data/accounts.json on
this computer only). The window keeps its own browser profile (data/apply_browser), so sessions last.

What the window never does: submit an application, attach a file (the CV and the letter are the user's), get around a
captcha, tick a newsletter or partner-offers box, or tick a website's terms box unless the user allowed it in Settings. It
fills only EMPTY fields, so a value the website or the user gave is never replaced. Pages its rules do not understand are
read once by the Claude API (claude_forms.py: structure only, never the user's details), and the answer is saved.

The window lives in one worker thread (Playwright's objects belong to the thread that made them): the app sends it
requests and reads back what happened."""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import string
import sys
import threading
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from . import claude_forms
from .config import DATA_DIR

PROFILE_DIR = DATA_DIR / "apply_browser"


# --------------------------------------------------------------------------- the user's details, as the forms ask them
def _fold(s) -> str:
    """lower case, no accents, one space: 'Prénom *' -> 'prenom *'."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def split_name(name: str) -> tuple[str, str]:
    """'Jean-Marc Dupont Moretti' -> ('Jean-Marc', 'Dupont Moretti'): the first word is the first name."""
    parts = (name or "").split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def form_values(ap: dict) -> dict:
    """The applicant's settings -> the value of every kind of form field ('' when not given)."""
    first, last = ap.get("first_name") or "", ap.get("last_name") or ""
    if not (first or last):
        first, last = split_name(ap.get("name", ""))
    lines = [x.strip() for x in (ap.get("address") or "").splitlines() if x.strip()]
    m = re.search(r"\b(\d{5})\s+([^\n,]+)", ap.get("address") or "")  # '75011 Paris' in the letter's address
    street = next((x for x in lines if not re.match(r"\d{5}\b", x)), "")
    auth = ap.get("work_authorization") or ""
    return {
        "first_name": first, "last_name": last, "full_name": f"{first} {last}".strip(),
        "email": ap.get("email", ""), "phone": ap.get("phone", ""),
        "address": street, "postal_code": ap.get("postal_code") or (m[1] if m else ""),
        "city": ap.get("city") or (m[2].strip() if m else ""), "country": ap.get("country", ""),
        "linkedin": ap.get("linkedin", ""), "website": ap.get("website", ""),
        "civility": ap.get("civility", ""), "nationality": ap.get("nationality", ""),
        "degree": ap.get("degree", ""), "school": ap.get("school", ""), "grad_year": str(ap.get("grad_year") or ""),
        "field_of_study": ap.get("field_of_study", ""), "how_heard": ap.get("how_heard", ""),
        # yes / no questions: '' leaves them to the user
        "work_authorization": "yes" if auth == "yes" else "no" if auth == "visa" else "",
        "sponsorship": "no" if auth == "yes" else "yes" if auth == "visa" else "",
        # questions many forms ask: '' leaves them to the user
        "salary": re.sub(r"\D", "", str(ap.get("salary_expectation") or "")),
        "availability": {"immediately": "Immédiatement", "1 month": "Sous 1 mois", "3 months": "Sous 3 mois"}.get(
            ap.get("availability") or "", ap.get("availability") or ""),
        "english": {"B2": "Professionnel (B2)", "C1": "Courant (C1)", "C2": "Bilingue (C2)"}.get(
            ap.get("english_level") or "", ap.get("english_level") or ""),
        "language": {"B2": "Professionnel (B2)", "C1": "Courant (C1)", "C2": "Bilingue (C2)"}.get(
            ap.get("english_level") or "", ap.get("english_level") or ""),  # a language list: English, at that level
        "current_position": ap.get("current_position", ""),
        "phone_code": "+33" if re.search(r"france", ap.get("country") or "France", re.I) else "",
        "relocation": "yes" if (ap.get("city") or (m[2].strip() if m else "")) else "",  # only where it names the city
    }


# --------------------------------------------------------------------------- which field asks for what
# the browser's own autofill names (the most reliable sign when a form gives it)
_AUTOCOMPLETE = {"given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email",
                 "tel": "phone", "tel-national": "phone", "postal-code": "postal_code", "address-level2": "city",
                 "country": "country", "country-name": "country", "street-address": "address",
                 "address-line1": "address", "url": "website", "honorific-prefix": "civility"}
# label / name / id / placeholder words, on the folded text; the first that matches wins (the most specific first)
_RULES = [
    ("salary", r"salair|salary|pretention|remuneration (?:souhait|attendu|envisag)|compensation|expected (?:pay|package)|"
               r"package (?:souhaite|attendu)|desired pay"),
    ("availability", r"disponibilit|start date|date de (?:debut|demarrage|prise de poste)|when (?:can|could) you start|"
                     r"available to start|earliest start|notice period|preavis"),
    ("english", r"anglais|english"),
    ("relocation", r"relocat|demenag|based in|base(?:e)? a |vivez.vous a|habitez.vous a|resid(?:e|ez) a"),
    ("current_position", r"current (?:position|job title|job|role|title|occupation)|poste actuel|fonction actuelle|"
                         r"emploi actuel|dernier poste|last (?:position|job title)|(?:votre |your )?titre actuel"),
    ("phone_code", r"indicatif|country code|code pays|code du pays|dial(?:ling)? code|phone prefix|prefixe"),
    ("position", r"poste (?:recherche|souhaite|vise|convoite)|intitule du poste|position (?:applied|sought|desired)|"
                 r"desired (?:position|role|job)|job (?:applied|you are applying)|poste pour lequel"),
    ("cover_letter", r"cover letter|lettre de motivation|\bmotivation\b|message (?:to|au|pour le) recrut|"
                     r"^\W*(?:votre |your |un |a )?message\b|"
                     r"why (?:do you want|are you interested|would you like|you)|pourquoi (?:souhaitez|voulez|vous)|"
                     r"tell us (?:about yourself|why|more about you)|parlez.nous de vous|presentez.vous"),
    ("how_heard", r"how did you (?:\w+ )?(?:hear|find|learn|discover|come across)|how (?:did|do) you know about|comment avez.vous (?:connu|entendu|trouve|decouvert)|"
                  r"ou avez.vous (?:vu|trouve|connu)|\bsource\b|referral source|canal"),
    ("sponsorship", r"sponsor|\bvisa\b"),
    ("work_authorization", r"authori[sz]|right to work|legally (?:able|eligible|allowed)|eligible to work|"
                           r"autorise.{0,25}travailler|autorisation de travail|permis de travail|titre de sejour"),
    ("nationality", r"nationalit|citizenship"),
    ("email", r"e.?mail|courriel|adresse electronique"),
    ("linkedin", r"linked.?in"),
    ("website", r"website|site (?:web|internet|personnel)|portfolio|github|gitlab|personal (?:site|page|url)"),
    # both names in one field ('Nom et prénom'), before the rules for each name
    ("full_name", r"full.?name|nom complet|nom (?:et|&|,)? ?prenom|prenom (?:et|&|,)? ?nom|first and last name"),
    ("first_name", r"first.?name|given.?name|fore.?name|\bprenom|\bfname\b"),
    ("last_name", r"last.?name|family.?name|sur.?name|nom de famille|\blname\b|^nom\b(?! complet)|\bnom\s*\*?$"),
    ("full_name", r"full.?name|nom complet|nom et prenom|prenom et nom|^name\b|^your name|votre nom"),
    ("phone", r"phone|telephone|\bmobile\b|portable|\btel\b|cellulaire"),
    ("postal_code", r"\bpostal\b|code postal|post ?code|\bzip\b|\bcp\b"),  # never 'adresse postale' (an address)
    ("city", r"\bcity\b|\bville\b|\btown\b|localite|commune|\blocation\b|localisation|lieu de residence|"
             r"where are you (?:based|located)|ou (?:etes|habitez).vous"),
    ("country", r"country|\bpays\b"),
    ("address", r"address|adresse|\bstreet\b|\brue\b"),
    ("civility", r"civilite|salutation|\bgenre\b(?!\s+d)|\bgender\b|\bsexe\b|(?<!job )(?<!position )(?<!poste )(?<!post )\btitle\b(?! (?:of|du|de))|"
                 r"(?<!job )(?<!poste )\btitre\b(?! (?:du|de|of))"),
    ("language", r"^langue|\blangue ?\d|\blangues?\b|language|langueniveau"),
    ("grad_year", r"graduation|annee d.obtention|annee de (?:diplome|sortie)|year of (?:graduation|completion)|promotion"),
    ("field_of_study", r"field of study|\bmajor\b|discipline|specialit|domaine d.etudes|filiere"),
    ("school", r"school|universit|\becole\b|etablissement|institution|college"),
    ("degree", r"degree|diplome|niveau d.etudes|niveau de formation|education level|highest (?:level|qualification)"),
]
_RULES = [(k, re.compile(rx)) for k, rx in _RULES]
_QUESTIONS = 12  # the first rules are questions: checked before _OTHER
# a field about someone else, or a company: never filled with the user's details
_OTHER = re.compile(r"entreprise|company|societe|employ(?:er|eur|e|ee)\b|recruteur|\breferent|manager|\breference|parrain|"
                    r"\br[eé]f[eé]r[eé]|\breferred|\breferral|coopt|"
                    r"\bref\b|code promo|captcha|search|recherch|code pays|country code|indicatif|password|mot de passe|"
                    r"utilisateur|user.?name|identifiant|\blogin\b")
_PLACEHOLDER = re.compile(r"^$|^-+|select|choisi|selectionn|choose|veuillez|please|^\.\.\.")
_YES, _NO = re.compile(r"^\s*(oui|yes)\b", re.I), re.compile(r"^\s*(non|no)\b", re.I)


def _kinds_said(text: str, tag: str) -> list:
    """What a field's words ask for: every detail rule that matches, in the rules' order (the first is the answer); []
    when they say nothing known; [False] for a field about someone else or a company (never the user's)."""
    if tag == "textarea":  # a free text box: the cover letter, or a yes / no question asked there (Algolia: sponsorship)
        r = dict(_RULES)
        return ["cover_letter"] if r["cover_letter"].search(text) else ["sponsorship"] if r["sponsorship"].search(text) else \
            ["work_authorization"] if r["work_authorization"].search(text) else []
    for kind, rx in _RULES[:_QUESTIONS]:  # questions may name the company ('how did you hear about our company?')
        if rx.search(text):
            return [kind]
    if _OTHER.search(text):
        return [False]
    out = []
    for kind, rx in _RULES[_QUESTIONS:]:
        if rx.search(text) and not (kind == "address" and re.search(r"e.?mail|courriel|electronique", text)):
            out.append(kind)
    return out


def field_kind(f: dict) -> str | None:
    """A field's description (from the page) -> what it asks for, or None."""
    ac = (f.get("autocomplete") or "").lower().split()
    for token in ac:
        if token in _AUTOCOMPLETE:
            return _AUTOCOMPLETE[token]
    if f.get("type") in ("radio", "checkbox"):
        said = _kinds_said(_fold(f.get("group")), f.get("tag"))
        if not said and f.get("type") == "radio":  # Chronopost: 'Madame*' 'Monsieur*' named 'id_civilite'
            said = _kinds_said(_fold(f"{f.get('name') or ''} {f.get('elid') or ''}"), f.get("tag"))
        return (said[0] or None) if said else None
    # the words a person reads first (label, placeholder), the page's technical names second: 'Prénom' with a technical
    # name ending in 'Nom' is no 'Prénom Nom' field
    for keys in (("label", "aria", "placeholder"), ("name", "elid")):
        own = _fold(" ".join(f.get(k) or "" for k in keys))
        said = _kinds_said(own, f.get("tag")) if own else []
        if said:
            return said[0] or None  # False (someone else's field) -> None
    near = _fold(f.get("near"))  # no label: the words beside the field ('Full name *' above an input whose placeholder
    if near and len(near) <= 60:  # is 'John Doe'), only when they name one thing (a row 'Prénom Nom' names two)
        said = _kinds_said(near, f.get("tag"))
        if len(set(said)) == 1:
            return said[0] or None
    if f.get("tag") == "input":  # the field's own type, when no word said anything
        return {"email": "email", "tel": "phone"}.get(f.get("type"))
    return None


def pick_option(options: list[str], kind: str, value: str) -> int | None:
    """The index of the select option that says the value, or None. Never the empty first option."""
    want = _fold(value)
    if not want:
        return None
    opts = [_fold(o) for o in options]
    if kind == "civility":
        rx = re.compile(r"^(m\.?|mr\.?|monsieur|mister|homme|masculin|male|man)$") if want in ("m.", "m", "monsieur", "mr") else \
            re.compile(r"^(mme\.?|mrs\.?|ms\.?|madame|mlle|femme|feminin|female|woman)$")
        return next((i for i, o in enumerate(opts) if rx.match(o)), None)
    if kind in ("work_authorization", "sponsorship", "relocation"):
        rx = _YES if want == "yes" else _NO
        return next((i for i, o in enumerate(options) if rx.match(o)), None)
    if kind == "english":  # 'fluent in English?' yes / no, or a level list (C2, 'Bilingue', 'Courant', 'Native')
        yes = next((i for i, o in enumerate(options) if _YES.match(o)), None)
        if yes is not None:
            return yes if re.search(r"c1|c2|courant|bilingue|fluent|bilingual|native", want) else None
        lvl = re.search(r"\b([abc][12])\b", want)
        order = [r"\bc2\b|bilingu|natif|native|maternel", r"\bc1\b|courant|fluent|advanced|avance",
                 r"\bb2\b|professionn|upper.intermediate"]
        start = {"c2": 0, "c1": 1, "b2": 2}.get(lvl[1] if lvl else "", 0)
        for rx in order[start:]:
            hit = next((i for i, o in enumerate(opts) if i and re.search(rx, o)), None)
            if hit is not None:
                return hit
        return None
    if kind == "phone_code":  # 'France (+33)', '+33', 'FR +33'
        return next((i for i, o in enumerate(opts) if i and re.search(r"\+\s?33\b|^france\b|\bfr\b.{0,4}33", o)), None)
    if kind == "language":  # a level list ('Bilingue', 'Courant') or a language list ('Anglais')
        if any(re.search(r"bilingu|courant|fluent|native|natif|intermedi|debutant|notions|operationnel|\b[abc][12]\b", o)
               for o in opts):
            return pick_option(options, "english", value)
        return next((i for i, o in enumerate(opts) if i and re.match(r"(?:anglais|english)\b", o)), None)
    if kind == "availability":
        rx = {"immediatement": r"immediat|des que possible|asap|now|tout de suite|^0\b"}.get(want, re.escape(want.split()[-1]))
        return next((i for i, o in enumerate(opts) if i and re.search(rx, o)), None)
    if kind == "salary":  # a range list: the one holding the amount
        amount = int(want) if want.isdigit() else 0
        for i, o in enumerate(opts):
            nums = [int(x.replace(" ", "")) * (1000 if len(x.replace(" ", "")) <= 3 else 1)
                    for x in re.findall(r"\d[\d ]*", o) if x.strip()]
            if i and len(nums) >= 2 and nums[0] <= amount <= nums[1]:
                return i
        return None
    exact = next((i for i, o in enumerate(opts) if o == want), None)
    if exact is not None:
        return exact
    if kind == "how_heard":  # 'Site carrière de l'entreprise' / 'Company website' / 'Careers page'
        rx = re.compile(r"site.{0,20}(?:carriere|entreprise|societe)|career|company (?:website|site)|our website|site web")
        return next((i for i, o in enumerate(opts) if rx.search(o)), None)
    hits = [i for i, o in enumerate(opts) if o and (want in o or (len(o) > 3 and o in want))]
    return hits[0] if len(hits) == 1 else None  # only an unambiguous match


# --------------------------------------------------------------------------- the page side (Playwright)
_COLLECT = r"""() => {
  const fields = [], buttons = []; let n = 0, m = 0;
  // the marks of an earlier reading go first: a field it marked and this one skips (now hidden, read-only) would share
  // its number with another field, and the fill or click meant for one would find two
  for (const el of document.querySelectorAll('[data-jf]')) el.removeAttribute('data-jf');
  for (const el of document.querySelectorAll('[data-jb]')) el.removeAttribute('data-jb');
  for (const el of document.querySelectorAll('[data-jff]')) el.removeAttribute('data-jff');
  const textOf = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const shown = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  // file fields (hidden ones too: a styled 'upload' button hides its input): which one takes the CV
  const files = []; let q = 0;
  for (const el of document.querySelectorAll('input[type=file]')) {
    if (el.disabled) continue;
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = textOf(l); }
    if (!label && el.closest('label')) label = textOf(el.closest('label'));
    let near = ''; let p = el.parentElement;
    for (let i = 0; i < 4 && p && !near; i++, p = p.parentElement) { const t = textOf(p); if (t && t.length < 160) near = t; }
    const id = 'jff' + (q++);
    el.setAttribute('data-jff', id);
    files.push({id, label, near, name: el.name || '', elid: el.id || '', aria: el.getAttribute('aria-label') || '',
                accept: el.accept || '', has: !!(el.files && el.files.length)});
  }
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset','file','search'].includes(type)) continue;
    if (!shown(el) && type !== 'radio' && type !== 'checkbox') continue;
    if (el.disabled || el.readOnly) continue;
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = textOf(l); }
    if (!label && el.closest('label')) label = textOf(el.closest('label'));
    const lb = el.getAttribute('aria-labelledby');
    if (!label && lb) label = lb.split(/\s+/).map(i => textOf(document.getElementById(i))).join(' ');
    let near = '';
    if (!label) { let p = el.parentElement; for (let i = 0; i < 3 && p && !near; i++, p = p.parentElement) { const t = textOf(p); if (t && t.length < 120) near = t; } }
    let group = '';
    if (type === 'radio' || type === 'checkbox') {
      const fs = el.closest('fieldset, [role=radiogroup], [role=group]');
      if (fs) group = textOf(fs.querySelector('legend')) || textOf(fs).slice(0, 240);
      else {  // the first block around it that says more than its answers ('Yes' 'No'): the question
        const answers = el.name ? [...document.getElementsByName(el.name)].map(x => textOf(x.closest('label')) || '').join(' ') : '';
        for (let p = el.parentElement, i = 0; p && i < 6 && !group; p = p.parentElement, i++) {
          const t = textOf(p);
          if (t.length > answers.length + 8 && t.length < 300) group = t;
        }
        if (!group) group = textOf((el.closest('div') || {}).parentElement).slice(0, 240);
      }
    }
    const id = 'jf' + (n++);
    el.setAttribute('data-jf', id);
    fields.push({id, tag: el.tagName.toLowerCase(), type, name: el.name || '', elid: el.id || '', placeholder: el.placeholder || '',
                 aria: el.getAttribute('aria-label') || '', autocomplete: el.getAttribute('autocomplete') || '', label, near, group,
                 role: el.getAttribute('role') || '',
                 value: type === 'password' ? (el.value ? '•' : '') : (el.value || ''), checked: !!el.checked, required: !!el.required || el.getAttribute('aria-required') === 'true',
                 chosen: el.tagName === 'SELECT' && el.selectedIndex >= 0 ? (el.options[el.selectedIndex].text || '').trim() : '',
                 chosenIndex: el.tagName === 'SELECT' ? el.selectedIndex : -1,
                 options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()) : []});
  }
  // the fields a form would send: a button that submits a form holding them is never an 'apply' button to press
  const formFields = f => [...f.querySelectorAll('input, textarea, select')].filter(e => {
    const t = (e.getAttribute('type') || e.tagName).toLowerCase();
    return !['hidden','submit','button','image','reset','search'].includes(t) && (t === 'file' || shown(e)); }).length;
  for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], a[href], [role=button], [role=link]')) {
    if (!shown(el)) continue;
    const text = (textOf(el) || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 80) continue;
    const id = 'jb' + (m++);
    el.setAttribute('data-jb', id);
    const form = el.form || null, submits = !!form && (el.type === 'submit' || el.type === 'image');
    let href = '', up = false;
    if (el.tagName === 'A') { try { const u = new URL(el.href, location.href);
      // a link to a parent of this page (Michelin's menu 'Apply' -> the job list above the offer): back, never forward
      const lp = u.pathname.replace(/\/+$/, ''), here = location.pathname.replace(/\/+$/, '');
      up = u.host === location.host && lp.length > 1 && here.startsWith(lp + '/');
      if (u.protocol === 'mailto:') href = 'mailto:' + decodeURIComponent(u.pathname);
      else if (!(u.host === location.host && u.pathname === location.pathname && u.search === location.search))
        href = (u.host !== location.host ? u.host : '') + u.pathname + u.search;  // '' for a link within this page
    } catch (e) {} }
    buttons.push({id, tag: el.tagName.toLowerCase(), text, type: (el.getAttribute('type') || '').toLowerCase(),
                  nav: !!el.closest('nav, header, [role=navigation], [role=menubar], [role=menu]'),
                  hint: ['data-action', 'data-automation-id', 'data-testid', 'data-ph-at-id', 'data-qa', 'id']
                        .map(k => el.getAttribute(k) || '').join(' ').slice(0, 160),
                  href: href.slice(0, 160), submits, formFields: submits ? formFields(form) : 0,
                  // ASP.NET (Talentsoft): ONE form holds the whole page (header sign-in, search, the offer's 'Je postule')
                  wholePage: submits && !!form.querySelector('input[name="__VIEWSTATE"]'),
                  up,
                  formFile: submits ? !!form.querySelector('input[type=file]') : false});
  }
  const captcha = !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], ' +
                                           '.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]');
  return {fields, buttons, files, captcha, title: document.title || '', uploads: document.querySelectorAll('input[type=file]').length,
          heading: textOf(document.querySelector('h1')).slice(0, 160),
          mails: (() => { const t = document.body ? document.body.innerText || '' : '', out = [];
                          for (const m of t.matchAll(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g)) {
                            out.push({addr: m[0], before: t.slice(Math.max(0, m.index - 160), m.index).replace(/\s+/g, ' ')});
                            if (out.length >= 8) break; }
                          return out; })(),
          body: (document.body ? document.body.innerText || '' : '').replace(/\s+/g, ' ').slice(0, 700)};
}"""
_IDENTITY = {"email", "first_name", "last_name", "full_name", "phone"}
# the button that opens the offer's application, the surest first (apply_rank). Never one that SENDS a form, a sign-in
# with another website, a page about how to apply, another offer ('Apply now <its title>') or a spontaneous application.
_APPLY_MANUAL = re.compile(r"^\s*(?:apply manually|postuler manuellement|candidater manuellement|saisie manuelle|"
                           r"start (?:your |an? )?(?:new )?application|commencer (?:ma|votre|une) candidature|"
                           r"d[ée]marrer (?:ma|votre|une) candidature|enter (?:my |your )?(?:details |information )?manually|"
                           r"fill (?:in |out )?(?:the form )?manually|saisir manuellement|remplir manuellement|"
                           r"continue without (?:a )?(?:resume|cv)|continuer sans (?:cv|mon cv))\s*[→›»>]?\s*$", re.I)
_APPLY = re.compile(r"^\s*(?:je (?:postule|candidate)|postule[rz]|candidate[rz]|apply|d[ée]poser (?:ma|une|votre) candidature|"
                    r"faire acte de candidature|jetzt bewerben|bewerben|online bewerben|postular|aplicar|inscribirse|"
                    r"candidatar(?:se)?|candidati|candidatura)(?:\s+\S+){0,4}\s*[→›»>]?\s*$", re.I)
# the phrase anywhere in a short button ('S'inscrire et postuler à cette offre', Chronopost)
_APPLY_PHRASE = re.compile(r"postuler (?:[àa] )?(?:cette|l'|l’)\s?offre|candidater (?:[àa] )?cette offre|"
                           r"apply (?:to|for) this (?:job|offer|position|role|vacancy)", re.I)
_APPLY_HINT = re.compile(r"showformoverlay|apply[-_]?(?:button|btn|now|link|cta)|\bapply\b|adventurebutton|"
                         r"postuler|candidater|job-?apply", re.I)
_APPLY_WORD = re.compile(r"\b(?:postule[rz]|je postule|candidate[rz]|apply|bewerben)\b", re.I)  # 'send Postuler': an icon first
_APPLY_HREF = re.compile(r"/(?:apply|postuler|candidater|candidature|application)(?:[/?#]|\.html?|$)|applications/new|"
                         r"[?&](?:apply|postuler)=", re.I)  # a whole step of the address ('/application-engineering': no)
_INTERESTED = re.compile(r"^\s*(?:i'?m interested|je suis int[ée]ress[ée]e?|ça m'int[ée]resse)\s*[→›»>]?\s*$", re.I)
# a BUTTON saying 'join us' on an offer page (Teamtailor's own wording: 'Rejoins-nous !'); a link saying it is a menu
_JOIN = re.compile(r"^\s*(?:rejoins.nous|rejoignez.nous|join us|join (?:the|our) team)\s*[!→›»>]?\s*$", re.I)
_NOT_APPLY = re.compile(r"linkedin|indeed|google|facebook|apple|xing|viadeo|\bseek\b|github|comment postuler|how to apply|"
                        r"search (?:&|and) apply|recherch|spontan|unsolicited|\bhelp\b|\baide\b|submit|soumettre|envoyer|"
                        r"\bsend (?:my |your |the |an? )?(?:application|candidature|cv|resume)|valider|confirm|termin|"
                        r"finish|alert|partag|share|sauvegard|\bsave\b|enregistr|d[ée]connex|d[ée]connecter|log ?out|"
                        r"sign ?out|cookie|\bretour\b|\bback to\b|return to|en (?:un|1) clic|one[- ]click|1[- ]click|quick apply|candidature express", re.I)


def apply_rank(b: dict) -> int | None:
    """How surely a button opens the offer's application: 0 (Workday's 'Apply Manually') to 4 ('I'm interested'). None
    when it is not one, or when it would SEND a form: a submit button whose form has fields or a file field."""
    t = b.get("text") or ""
    href = b.get("href") or ""
    sends = b.get("submits") and (b.get("formFile") or (b.get("formFields", 0) >= 2 and not (
        b.get("wholePage") and (_APPLY.match(t) or (len(t.split()) <= 9 and _APPLY_PHRASE.search(t))))))
    if _NOT_APPLY.search(t) or sends or href.startswith("mailto:") or _DOC.search(href) or b.get("up"):
        return None  # an email address to write to (see by_email) or a document to read: no page to open
    if _APPLY_MANUAL.match(t):
        return 0
    if _APPLY.match(t) or (len(t.split()) <= 9 and _APPLY_PHRASE.search(t)):
        return 1
    if len(t.split()) <= 3 and _APPLY_WORD.search(t):
        return 2
    if _APPLY_HINT.search(b.get("hint") or "") and not b.get("nav"):
        return 2  # the platform marks it in its code, whatever it says (Teamtailor 'Join your next adventure')
    if _APPLY_HREF.search(href) and len(t.split()) <= 4:
        return 3
    if (_INTERESTED.match(t) or (_JOIN.match(t) and b.get("tag") != "a")) and not b.get("nav"):
        return 4  # (in the website's menu, 'Join us' opens the menu: never)
    return None


_BY_EMAIL = re.compile(r"postul|apply|candidat|cv|resume|bewerb|recrut|recruit|jobs?@|emploi|career|carriere|rh@|hr@", re.I)


_MAIL_WORDS = re.compile(r"candidat|\bcv\b|postul|apply|application|contact|envoy|send|lettre|motivation|\brh\b|recrut|"
                         r"recruit|r[ée]f[ée]rences?|questions?", re.I)
_NOT_A_MAILBOX = re.compile(r"^(?:no-?reply|ne-?pas-?repondre|support|dpo|privacy|rgpd|gdpr|webmaster|abuse|postmaster|"
                            r"cookies?|legal|donnees)[.@-]|^[^@]*(?:donnees-?personnelles|dpo|rgpd|gdpr|privacy|"
                            r"dataprotection|data-?protection|protection-?donnees|cnil)[^@]*@|@(?:o2switch|example)\.", re.I)


def by_email(scans, mine=()) -> str:
    """The address an offer asks to apply to by email ('Postuler par mail', a recruiting mailbox, or an address the
    offer's text gives beside 'contact', 'CV', 'candidature'...), or ''. Never a support or data-protection address, never
    one of the user's own (`mine`: signed in, the website shows the account's email on the page)."""
    own = {x.strip().lower() for x in mine if x}
    for _, sc in scans:
        for b in sc["buttons"]:
            h = b.get("href") or ""
            addr = h[7:].split("?")[0].strip()
            if h.startswith("mailto:") and "@" in h and _BY_EMAIL.search(f"{b.get('text', '')} {h}") \
                    and not _NOT_A_MAILBOX.search(addr) and addr.lower() not in own:
                return addr
    for _, sc in scans:
        for m in sc.get("mails") or []:
            addr = m["addr"].rstrip(".")
            if _MAIL_WORDS.search(m.get("before", "")[-120:]) and not _NOT_A_MAILBOX.search(addr) and addr.lower() not in own:
                return addr
    return ""


def _button_id(b: dict) -> tuple:
    """A button as the next scan of the same page sees it (its data-jb number may change)."""
    return b.get("tag", ""), b.get("text", ""), b.get("href", "")


def apply_candidates(scans, tried=()) -> list[tuple]:
    """[(frame, button)] that may open the application, surest first, in any frame (iCIMS shows the offer in one),
    without those already pressed on this page (a 'Postuler' that opens a menu is not pressed again)."""
    shown = sum(1 for _, sc in scans for f in sc["fields"] if f["type"] not in ("checkbox", "radio"))
    form_here = shown >= 2 and any(sc.get("uploads") for _, sc in scans)  # fields AND a file field: an application form
    # the user's details asked on the page (not a header sign-in or a search): its submit buttons may send them
    details = any(field_kind(f) in ("first_name", "last_name", "full_name", "phone", "address") for _, sc in scans
                  for f in sc["fields"])
    out = []
    for fr, sc in scans:
        for b in sc["buttons"]:
            r = apply_rank(b)
            if r is None or _button_id(b) in tried:
                continue
            if form_here and r > 0 and b["tag"] != "a":
                continue  # its 'Postuler' button may be the one that sends it: only a link may still be followed
            if b.get("wholePage") and b.get("submits") and (details or form_here):
                continue  # a page-wide form asking the user's details: its 'Postuler' may send them
            out.append((r, fr, b))
    out.sort(key=lambda x: (x[0], bool(x[2].get("nav"))))  # the page's own order within a rank, its menu last
    return [(fr, b) for _, fr, b in out]


# a cookie banner over the page: its 'refuse' choice (never 'accept'), so the page's own buttons can be pressed
_REJECT = re.compile(
    r"^\s*(?:tout refuser|refuser tout|tout rejeter|rejeter tout|refuser(?: et fermer)?|je refuse|rejeter|"
    r"refuser (?:tous )?les cookies(?: (?:facultatifs|optionnels|non essentiels|non n[ée]cessaires|d'analyse))?|"
    r"continuer sans accepter|continue without accepting|non merci|"
    r"reject(?: all)?(?: (?:optional |non-essential |non-necessary |unnecessary |additional )?cookies)?|"
    r"decline(?: all)?(?: (?:optional |non-essential |non-necessary |unnecessary |additional )?cookies)?|deny(?: all)?|"
    r"disagree(?: and close)?|refuser les cookies non (?:essentiels|n[ée]cessaires)|"
    r"(?:use |allow |accept )?(?:only )?(?:strictly )?(?:necessary|essential|required) (?:cookies )?only|"
    r"only (?:necessary|essential|required)(?: cookies)?|"
    r"(?:utiliser |accepter )?(?:uniquement |seulement )?les cookies (?:strictement )?n[ée]cessaires(?: uniquement)?|"
    r"n[ée]cessaires uniquement)\s*[→›»>]?\s*$", re.I)
_BANNER = re.compile(r"cookie|consent|didomi|onetrust|axeptio|tarteaucitron|\bcmp|gdpr|rgpd|privacy|usercentrics|truste|"
                     r"cookiebot|sp_message", re.I)


_BANNER_BOX = ", ".join(f"[{a}*={w} i]" for w in ("cookie", "consent", "rgpd", "gdpr", "didomi", "onetrust", "axeptio",
                                                   "tarteaucitron", "cmp", "privacy") for a in ("id", "class", "aria-label"))


def refuse_cookies(page) -> bool:
    """Press a cookie banner's 'refuse' choice (its open shadow trees and frames included): a button or a link, or any
    element saying it inside the banner (a plain 'REFUSER' text the banner's script listens to). -> True when one was."""
    for fr in page.frames[:8]:
        finds = [lambda: fr.get_by_role("button", name=_REJECT), lambda: fr.get_by_role("link", name=_REJECT),
                 lambda: fr.locator(_BANNER_BOX).get_by_text(_REJECT)]
        for find in finds:
            try:
                loc = find()
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible():
                        loc.nth(i).click(timeout=3000)
                        page.wait_for_timeout(500)
                        return True
            except Exception:
                continue
    return False


# pages with nothing to fill: the offer is gone, or the website turned the window away (never got around: the user decides)
_GONE = re.compile(r"\b404\b|page (?:not found|introuvable|non trouv[ée]e)|n'existe (?:pas|plus)|n.est plus (?:disponible|en ligne)|"
                   r"(?:offre|annonce|poste|job|position|vacancy)s? (?:is no longer|has expired|a expir[ée]|expir[ée]e?|"
                   r"not found|introuvable|d[ée]j[aà] pourvue?)|no longer (?:available|accepting applications|exists)|"
                   r"has been (?:filled|closed|removed)|a [ée]t[ée] (?:pourvue?|supprim[ée]e?|cl[ôo]tur[ée]e?)", re.I)
_BLOCKED = re.compile(r"acc[eè]s (?:temporairement )?(?:restreint|refus[ée]|bloqu[ée]|interdit)|access (?:temporarily )?"
                      r"(?:restricted|denied|blocked|forbidden)|unusual traffic|trafic inhabituel|request (?:was |has been )?"
                      r"blocked|you have been blocked|vous avez [ée]t[ée] bloqu|attention required|\b403\b\W{0,3}forbidden|"
                      r"^\s*forbidden\b|error 403|erreur 403|acc[eè]s refus[ée]", re.I)
# a robot check served in a frame of its own (DataDome, Cloudflare, Radware, hCaptcha, reCAPTCHA's challenge)
_CHECK_FRAME = re.compile(r"captcha-delivery\.com|challenges\.cloudflare\.com|perfdrive\.com|hcaptcha\.com/captcha|"
                          r"recaptcha/api2/bframe|recaptcha/enterprise/bframe", re.I)
# a website checking that a person is there (a check the user passes in the window; the app never does)
_CHECKING = re.compile(r"cloudflare|just a moment|checking (?:your browser|if the site connection)|verify(?:ing)? (?:that )?"
                       r"you(?:'re| are) (?:a )?human|v[ée]rifi\w* que vous [eê]tes|are you a (?:robot|human)|"
                       r"[eê]tes.vous (?:un )?(?:robot|humain)|testing whether you are a human|what code is in the image|"
                       r"tiger ?protect|v[ée]rification de s[ée]curit[ée]|security check|ddos protection", re.I)


def fill_frame(frame, values: dict, letter: str, scan=None, plan_roles=None) -> list[str]:
    """Fill one frame's empty fields with the user's details. -> the kinds filled. plan_roles: {field_key: role} that
    Claude gave for fields the rules do not know."""
    done = []
    for f in (scan or frame.evaluate(_COLLECT))["fields"]:
        if f["type"] == "password":
            continue  # only the account steps fill a password
        role = (plan_roles or {}).get(claude_forms.field_key(f))
        kind = role if role is not None else field_kind(f)  # Claude read the whole page: its answer comes first
        if kind not in values and kind != "cover_letter":
            continue
        if not kind:
            continue
        value = letter if kind == "cover_letter" else values.get(kind, "")
        asked = _fold(f"{_text(f)} {f.get('group') or ''}")  # a radio's question is its group's text
        if kind == "relocation" and not (values.get("city") and _fold(values["city"]) in asked):
            value = ""  # 'based in Paris or ready to relocate?': yes; any other move is the user's to answer
        if kind == "language" and (f["tag"] != "select" or re.search(
                r"(?:^|\D)[2-9](?:\D|$)|second|correspondan|communication|preferred|maternelle|native", asked)):
            value = ""  # only a list, the first language: never a second one, nor the language to write to the user in
        if kind == "salary" and re.search(r"actuel|current|dernier|last|previous|present|brut actuel", asked):
            value = ""  # the salary they have now: never the expectation
        if kind == "availability" and re.search(r"deplacement|travel|week.?end|horaire|shift|astreinte|interview|entretien",
                                                asked):
            value = ""  # available to travel, for shifts, for an interview: never the start date
        if kind == "salary" and value and f["tag"] != "select" and f["type"] not in ("number", "radio"):
            value = f"{int(value):,} € brut annuel".replace(",", " ")
        if not value:
            continue
        el = frame.locator(f'[data-jf="{f["id"]}"]')
        try:
            if f["type"] == "radio":  # yes / no questions and civility: the radio whose label says the answer
                if kind in ("work_authorization", "sponsorship", "civility", "relocation", "english", "availability", "salary") \
                        and not f["checked"] and pick_option(["", f.get("label") or ""], kind, value) == 1:
                    el.check(force=True, timeout=3000)
                    done.append(kind)
                continue
            if f["type"] == "checkbox":
                continue  # consents, newsletters, terms: always the user's
            if f["tag"] == "select":
                i = pick_option(f["options"], kind, value)
                if i is not None and i != f["chosenIndex"] and (f["chosenIndex"] <= 0 or _PLACEHOLDER.search(_fold(f["chosen"]))):
                    el.select_option(index=i, timeout=3000)
                    try:  # a styled list over it (bootstrap-select: SNCF, select2, chosen) shows the choice too
                        el.evaluate(_REDRAW_LIST)
                    except Exception:
                        pass
                    done.append(kind)
                continue
            if (f["value"] or "").strip():
                continue  # already filled: never replaced
            if kind in ("work_authorization", "sponsorship", "relocation") and value in ("yes", "no"):
                english = re.search(r"\b(?:you|are|will|do|the|your)\b", _text(f) + " " + (f.get("group") or ""), re.I)
                value = {"yes": "Yes" if english else "Oui", "no": "No" if english else "Non"}[value]
            if f.get("role") == "combobox":  # Greenhouse, react-select: its list's option, never typed text alone
                if _combo(frame, el, lambda texts: _combo_pick(texts, kind, value),
                          typed=value if kind in ("country", "city", "nationality", "school") else ""):
                    done.append(kind)
                continue
            el.fill(value, timeout=3000)
            done.append(kind)
        except Exception:
            continue  # a field the page hides or rebuilds: the others are still filled
    return done


_CV_FILE = re.compile(r"\bcv\b|curriculum|resume|r[ée]sum[ée]|lebenslauf|curr[ií]culo", re.I)
_NOT_CV_FILE = re.compile(r"lettre|cover|motivation|letter|photo|portrait|dipl[oô]m|transcript|relev[ée]|autres?\b|other|"
                          r"additional|compl[ée]mentaire|identit|passport|passeport", re.I)


_YES_NO_KINDS = ("work_authorization", "sponsorship", "relocation", "english", "civility")
_AGREE = re.compile(r"^\W*(?:i |je )?(?:agree|accept|consent|j.accepte|oui|yes|i have read|j.ai lu)", re.I)
_COMBO_CONSENT = re.compile(r"personal data|resume data|donn[ée]es personnelles|gdpr|rgpd|privacy|confidentialit|consent|"
                            r"data (?:processing|protection|retention)|traitement de (?:mes|vos) donn", re.I)


def _combo_pick(texts, kind, value):
    """1-based: the option of a combobox's list that says the value ('France +33' for France, 'Paris, France' for Paris)."""
    i = pick_option([""] + texts, kind, value)
    if i is None and kind in ("country", "city", "nationality"):
        want = _fold(value)
        i = next((k + 1 for k, t in enumerate(texts) if _fold(t).startswith(want)), None)
    return i


def _combo(frame, el, choose, typed="") -> bool:
    """A combobox (Greenhouse, react-select): opened, the option `choose(texts)` gives (1-based; 0 / None: none) clicked.
    -> True when one was chosen. Nothing chosen: closed again."""
    try:
        el.click(timeout=3000)
        if typed:
            el.fill(typed, timeout=3000)
        texts, opts = [], None
        for _ in range(6):  # its options come at once, or after a search (Greenhouse's city: about 1.5 s)
            frame.wait_for_timeout(500)
            own = el.get_attribute("aria-controls") or el.get_attribute("aria-owns") or ""
            # the list it opened, never another one of the page (Greenhouse: the phone's hidden list of countries)
            opts = frame.locator(f'[id="{own}"] [role=option]') if own and frame.locator(f'[id="{own}"]').count() else \
                frame.locator("[role=option]:visible")
            texts = [t.strip() for t in opts.all_inner_texts()][:200]
            if texts:
                break
        i = choose(texts) if texts else None
        if i:
            opts.nth(i - 1).click(timeout=3000)
            return True
        el.press("Escape")
        if typed:
            el.fill("", timeout=2000)
    except Exception:
        pass
    return False


_REDRAW_LIST = """e => { try { const $ = window.jQuery; if (!$) return; const j = $(e);
  if (j.data('selectpicker')) j.selectpicker('refresh'); if (j.data('select2')) j.trigger('change.select2');
  if (j.data('chosen')) j.trigger('chosen:updated'); } catch (x) {} }"""


_LETTER_FILE = re.compile(r"lettre|cover|motivation|letter|anschreiben", re.I)


def attach_letter(scans, letter_path, note=lambda m: None, name: str = "") -> int:
    """The offer's cover letter (its PDF, written in the app) into the form's letter field: a file field that says
    'Lettre de motivation' / 'Cover letter', never a CV field, never one already holding a file. Never sent."""
    if not letter_path or not Path(letter_path).exists():
        return 0
    done = 0
    for fr, sc in scans:
        for f in sc.get("files", []):
            text = " ".join(f.get(k) or "" for k in ("label", "aria", "name", "elid", "near"))
            own = f.get("label") or f.get("aria") or text
            if f.get("has") or not _LETTER_FILE.search(own) or (_CV_FILE.search(own) and not _LETTER_FILE.search(f.get("label") or "")):
                continue
            try:
                fr.locator(f'[data-jff="{f["id"]}"]').set_input_files(
                    {"name": name or Path(letter_path).name, "mimeType": "application/pdf",
                     "buffer": Path(letter_path).read_bytes()}, timeout=5000)
                done += 1
            except Exception:
                continue
    if done:
        note("cover letter attached")
    return done


def cv_filename(values: dict) -> str:
    """The name the recruiter sees: 'CV_Jean_Dupont.pdf' (the stored file is named by its fingerprint)."""
    who = "_".join(re.sub(r"[^\w-]+", "-", values.get(k) or "").strip("-") for k in ("first_name", "last_name"))
    return f"CV_{who.strip('_')}.pdf" if who.strip("_") else "CV.pdf"


def attach_cv(scans, cv_path, note=lambda m: None, name: str = "") -> int:
    """The CV chosen for the offer into the form's CV field (the user's request, 19 Nov 2026): a field that says CV /
    résumé, or the page's only file field when it says nothing else. Never a letter, photo or diploma field, never one
    already holding a file. The form is never sent. -> the number of fields that took it."""
    if not cv_path or not Path(cv_path).exists():
        return 0
    done = 0
    every = [(fr, f) for fr, sc in scans for f in sc.get("files", [])]
    for fr, f in every:
        text = " ".join(f.get(k) or "" for k in ("label", "aria", "name", "elid", "near"))
        named = _CV_FILE.search(text) and not _NOT_CV_FILE.search(f.get("label") or f.get("aria") or text)
        alone = len(every) == 1 and not _NOT_CV_FILE.search(text)
        if f.get("has") or not (named or alone):
            continue
        try:
            data = Path(cv_path).read_bytes()
            fr.locator(f'[data-jff="{f["id"]}"]').set_input_files(
                {"name": name or Path(cv_path).name, "mimeType": "application/pdf", "buffer": data}, timeout=5000)
            done += 1
        except Exception:
            continue
    if done:
        note("CV attached")
    return done


def fill_page(page, values: dict, letter: str) -> list[str]:
    done = []
    for fr in page.frames:
        try:
            done += fill_frame(fr, values, letter)
        except Exception:
            continue  # a frame from another website that refuses scripts, or gone
    return done


def has_form(page) -> bool:
    kinds = set()
    for fr in page.frames:
        try:
            kinds |= {field_kind(f) for f in fr.evaluate(_COLLECT)["fields"]}
        except Exception:
            continue
    return len(kinds & _IDENTITY) >= 2


# --------------------------------------------------------------------------- accounts on the websites
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
_ACCOUNTS_LOCK = threading.Lock()
_SYMBOLS = "!#%*-_@"


def site_of(url: str) -> str:
    """The website an account belongs to: its host ('sanofi.wd3.myworkdayjobs.com'), without 'www.'."""
    u = urlparse(url or "")
    if u.scheme == "file":  # a page on this computer (tests): its folder stands for the website
        return "file:" + u.path.rsplit("/", 1)[0]
    h = (u.hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


_SHARED_ATS = re.compile(r"^(?:career|performancemanager|hcm)\d*\.(?:successfactors|sapsf)\.(?:eu|com)$")


def account_site(url: str) -> str:
    """Where an account lives: the website of its sign-in page; on a host many companies share (SAP SuccessFactors'
    career5.successfactors.eu...), that host and the company ('career5.successfactors.eu/atos'): each has its own accounts."""
    site = site_of(url)
    if _SHARED_ATS.match(site):
        m = re.search(r"[?&]company=([A-Za-z0-9_.-]+)", url or "")
        if m:
            return f"{site}/{m[1].lower()}"
    return site


# websites where the user has (or makes) a personal account themselves: the app never makes one there, nor signs in
_OWN_ACCOUNT = re.compile(r"(?:^|\.)(?:linkedin\.com|indeed\.(?:com|fr)|glassdoor\.(?:com|fr)|welcometothejungle\.com|"
                          r"hellowork\.com|apec\.fr|francetravail\.fr|pole-emploi\.fr|monster\.(?:com|fr)|jobteaser\.com|"
                          r"talent\.com|google\.com|facebook\.com|apple\.com|microsoft(?:online)?\.com|live\.com)$")


def own_account_site(url: str) -> bool:
    return bool(_OWN_ACCOUNT.search(site_of(url)))


def new_password(n: int = 16) -> str:
    """A strong password for one website: at least 2 lower-case and 2 upper-case letters, 2 digits and 2 of the symbols
    most websites accept (Sorbonne Université's rule, 20 Nov 2026: 12 characters, 2 digits, 2 non-alphanumeric)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n - 2)) + "".join(secrets.choice(_SYMBOLS) for _ in range(2))
        p = "".join(secrets.SystemRandom().sample(p, len(p)))
        if sum(c.islower() for c in p) >= 2 and sum(c.isupper() for c in p) >= 2 and sum(c.isdigit() for c in p) >= 2:
            return p


def load_accounts(path=None) -> dict:
    try:
        return json.loads(Path(path or ACCOUNTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_accounts(d: dict, path=None):
    p = Path(path or ACCOUNTS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    p.chmod(0o600)  # this computer's user only


def account(site: str, email: str, create: bool = False, path=None) -> dict | None:
    """The account the app holds on a website, or a new one (a password made for that website alone) when create."""
    with _ACCOUNTS_LOCK:
        d = load_accounts(path)
        if site in d or not create:
            return d.get(site)
        d[site] = {"email": email, "password": new_password(), "state": "creating", "created": _now()}
        _save_accounts(d, path)
        return d[site]


def set_account(site: str, path=None, **kw):
    with _ACCOUNTS_LOCK:
        d = load_accounts(path)
        if site in d:
            d[site].update(kw)
            _save_accounts(d, path)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- what kind of page, which button
_SIGNUP_TXT = re.compile(r"cr[ée]er (?:un |mon |votre )?compte|(?:me |vous )?cr[ée]e[rz]? (?:un |mon |votre )?(?:compte|espace|"
                         r"profil)|s'inscrire|je m'inscris|inscrivez.vous|\binscription\b|"
                         r"create (?:an |a |my |your )?(?:new )?account|sign ?up|\bregister\b|nouveau candidat|new (?:user|candidate)",
                         re.I)
_SIGNIN_BTN = re.compile(r"^\s*(?:se connecter|connexion|me connecter|je me connecte|sign ?in|log ?in|login|connect)\s*[→›»>]?\s*$",
                         re.I)
_CREATE_BTN = re.compile(r"^\s*(?:cr[ée]er (?:un |mon |votre )?(?:compte|espace(?: candidat)?|profil)|cr[ée]er|s'inscrire|"
                         r"je m'inscris|inscription|(?:s'|m')?enregistrer|je m'enregistre|valider (?:mon |l')?inscription|"
                         r"ouvrir (?:un |mon )?compte|create (?:an |a |my |your )?(?:new )?(?:account|profile)|sign ?up|"
                         r"register(?: now)?|valider|continuer|continue|suivant|next|submit|envoyer|save|terminer|"
                         r"modifier(?: mon| le)?(?: mot de passe)?|r[ée]initialiser(?: (?:mon |le )?mot de passe)?|changer(?: (?:mon |le )?"
                         r"mot de passe)?|(?:reset|update|change|set|save) (?:my |the )?(?:new )?password|confirmer|confirm|"
                         r"enregistrer (?:mon |le |votre )?(?:nouveau )?mot de passe|valider (?:mon |le )?(?:nouveau )?mot de passe)"
                         r"\s*[→›»>]?\s*$", re.I)  # (on an account form only: 'Enregistrer' there makes the account)
_VERIFY_BTN = re.compile(r"^\s*(?:v[ée]rifier|valider|confirmer|verify|confirm|submit|envoyer|continuer|continue|suivant|next)\b",
                         re.I)
_CODE_FIELD = re.compile(r"\bcode\b|\botp\b|v[ée]rification|verification|one.time|\bpin\b", re.I)
_USERNAME = re.compile(r"user.?name|identifiant|nom d.utilisateur|\blogin\b", re.I)
_TERMS = re.compile(r"(?:accepte|agree|accept|lu et|read and|consent).{0,90}(?:conditions|terms|cgu|politique|privacy|"
                    r"mentions|confidentialit|policy|donn[ée]es (?:personnelles|[àa] caract[eè]re personnel)|personal "
                    r"(?:data|information)|traitement)|conditions g[ée]n[ée]rales|terms (?:of use|and conditions|of service)|"
                    r"privacy policy|politique de confidentialit", re.I)
# terms to read and accept through a link (SuccessFactors' privacy statement), then the window's 'accept' button
_TERMS_LINK = re.compile(r"^\s*(?:lire et accepter|read and accept|accepter|accept)\b.{0,60}(?:d[ée]claration|politique|"
                         r"privacy|confidentialit|conditions|terms|data|donn[ée]es)", re.I)
_TERMS_OK = re.compile(r"^\s*(?:accepter|j'accepte|accept|i accept|i agree|agree|j'ai lu et j'accepte|accept and close)\s*$",
                       re.I)
# 'create an account using any of these options' (Siemens): the one without a CV, the user's details typed in
_SELF_FILL = re.compile(r"^\s*(?:self[- ]?complete|fill (?:it )?in manually|enter (?:your )?(?:details|information) manually|"
                        r"manually|saisie manuelle|saisir manuellement|remplir manuellement|manuellement|without (?:a )?(?:cv|"
                        r"resume)|sans cv)\b", re.I)
# an application shown once signed in, and its identity section
_APP_OPEN = re.compile(r"vous candidatez|votre candidature|formulaire de candidature|your application|apply(?:ing)? for this "
                       r"(?:job|position)|candidater? (?:a|à) l.offre", re.I)
_SECTION = re.compile(r"^\W*(?:ouvrir |open |afficher |show |compl[ée]ter |edit )?(?:vos |your |mes |my )?(?:informations? (?:d.identit[ée]|personnelles)|identit[ée]|coordonn[ée]es|"
                      r"[ée]tat civil|personal (?:details|information|data)|contact (?:details|information))\b", re.I)
_RESEND = re.compile(r"(?:envoyer|renvoyer) (?:le |l'|un )?(?:e-?mail|mail|lien|courriel)? ?(?:[àa] nouveau|de nouveau)|"
                     r"^\s*renvoyer|resend|send (?:the |a )?(?:new )?(?:e-?mail|link)? ?again|send (?:a )?new (?:link|e-?mail)", re.I)
_TERMS_SHOWN = re.compile(r"\bCGU\b|conditions g[ée]n[ée]rales|terms (?:of use|and conditions|of service)|privacy policy|"
                          r"politique de confidentialit|donn[ée]es personnelles|personal data", re.I)
# a required box about the website's information notice ('prendre connaissance des mentions d'information... cocher')
_CONSENT = re.compile(r"mentions|information|donn[ée]es|cocher la case|consent|accept|politique|cgu|conditions|privacy|"
                      r"confidentialit|i have read|j'ai lu|acknowledg|i understand|i certify|i confirm|je certifie|"
                      r"je confirme|je reconnais|j.ai pris connaissance|j.accepte de transmettre|donn[ée]es de candidature", re.I)
_MARKETING = re.compile(r"newsletter|offres? (?:commerciales|partenaires)|marketing|partenaires|partners|promotion|"
                        r"alertes?|job alerts?|recevoir|receive|communications? (?:commerciales|marketing)", re.I)


def _text(f: dict) -> str:
    return " ".join(f.get(k) or "" for k in ("label", "aria", "placeholder", "name", "elid", "near"))


def code_fields(fields: list[dict]) -> list[dict]:
    """Fields asking for a code received by email (never a postal code, a promo code or a country code)."""
    return [f for f in fields if f["type"] in ("text", "number", "tel", "input") and _CODE_FIELD.search(_text(f))
            and not re.search(r"postal|zip|promo|pays|country|indicatif", _text(f), re.I) and field_kind(f) is None]


def page_kind(scan: dict) -> str:
    """application_form | sign_up | sign_in | email_code | offer_page | other, from the page's fields and buttons."""
    fields, buttons = scan["fields"], scan["buttons"]
    pw = [f for f in fields if f["type"] == "password"]
    kinds = {field_kind(f) for f in fields if f["type"] != "password"}
    if code_fields(fields) and not pw and len([f for f in fields if f["type"] not in ("checkbox", "radio")]) <= 3:
        return "email_code"
    signup = any(_SIGNUP_TXT.search(b["text"]) for b in buttons if b["tag"] != "a")
    identity = kinds & _IDENTITY
    if len(pw) >= 2 or (pw and signup and identity & {"first_name", "last_name", "full_name"}):
        return "sign_up"  # a password and its confirmation, or a 'create my account' button beside the details
    if pw and len(identity) <= 2:
        return "sign_in"  # an email (or user name) and a password
    # one password field among many details: an application form; its password stays the user's
    if len(kinds & _IDENTITY) >= 2:
        return "application_form"
    if any(apply_rank(b) is not None for b in buttons):
        return "offer_page"
    return "other"


def pick_button(buttons: list[dict], rx, prefer_submit=True) -> dict | None:
    """The page's button or link whose text says the action; a real button before a link."""
    hits = [b for b in buttons if rx.search(b["text"])]
    if prefer_submit:
        hits.sort(key=lambda b: (b["tag"] == "a", b["type"] != "submit"))
    return hits[0] if hits else None


def _el(frame, f: dict):
    return frame.locator(f'[data-jf="{f["id"]}"]')


_SEC_Q = re.compile(r"question (?:de s[ée]curit[ée]|secr[eè]te)|security question|secret question", re.I)
_SEC_A = re.compile(r"r[ée]ponse|answer", re.I)
SECURITY_QUESTION = "Code Job Finder ?"


def _tick(el):
    """A checkbox ticked: clicked, or (hidden under a styled box: Phenom, Orange) its own click() in the page, which the
    website's scripts see too."""
    try:
        el.check(force=True, timeout=3000)
    except Exception:
        el.evaluate("e => { if (!e.checked) e.click(); }")


_NEW_PW = re.compile(r"nouveau|nouvel|new|confirm|ressaisi|r[ée]p[ée]t|retype|repeat|again|v[ée]rif", re.I)


def fill_account(frame, scan: dict, acct: dict, allow_terms: bool, roles=None, reset=False) -> dict:
    """A sign-in or sign-up form: the account's email (or user name) and password in every password field, and a security
    question the website asks (a fixed question, an answer made for the website, kept with the password). -> what was
    done, whether a terms box is left to the user, and the security answer to keep ('security')."""
    done, terms_left, security = [], False, {}
    asks_q = any(_SEC_Q.search(_text(f)) for f in scan["fields"])
    fields = scan["fields"]
    if reset and any(f["type"] == "password" and _NEW_PW.search(_text(f)) for f in fields):
        # a page setting a new password (Arkéa): its 'new' and 'confirm' fields only, never the sign-in in its header
        fields = [f for f in fields if f["type"] == "password" and _NEW_PW.search(_text(f))]
    for f in fields:
        role = (roles or {}).get(claude_forms.field_key(f))
        try:
            if asks_q and f["type"] != "password" and (_SEC_Q.search(_text(f)) or _SEC_A.search(_text(f))):
                if f["value"] and f["tag"] != "select":
                    continue
                if _SEC_Q.search(_text(f)):
                    if f["tag"] == "select":  # the website's own questions: its first one
                        i = next((i for i, o in enumerate(f.get("options") or []) if i and not _PLACEHOLDER.search(_fold(o))), None)
                        if i is not None:
                            _el(frame, f).select_option(index=i, timeout=3000)
                            security["security_question"] = f["options"][i]
                    else:
                        _el(frame, f).fill(acct.get("security_question") or SECURITY_QUESTION, timeout=3000)
                        security["security_question"] = acct.get("security_question") or SECURITY_QUESTION
                else:
                    answer = acct.get("security_answer") or new_password(12).replace("@", "x").replace("!", "x")
                    _el(frame, f).fill(answer, timeout=3000)
                    security["security_answer"] = answer
                done.append("security question")
                continue
            if f["type"] == "password" or role in ("password", "password_confirm"):
                if not f["value"]:
                    _el(frame, f).fill(acct["password"], timeout=3000)
                    done.append("password")
            elif f["type"] == "checkbox":
                text = f.get("label") or f.get("group") or f.get("near") or ""
                required = f.get("required") or re.search(r"\*\s*$|^\W*\*", text.strip())  # Phenom: '* J'accepte...'
                consent = _TERMS.search(text) or (required and _CONSENT.search(text))
                if role == "terms" or (consent and not _MARKETING.search(text)):
                    if not f["checked"]:
                        if allow_terms:
                            _tick(_el(frame, f))
                            done.append("terms")
                        else:
                            terms_left = True
            elif role in ("username", "email") or field_kind(f) == "email" or _USERNAME.search(_text(f)):
                if not f["value"]:
                    _el(frame, f).fill(acct["email"], timeout=3000)
                    done.append("email")
        except Exception:
            continue
    return {"done": done, "terms_left": terms_left, "security": security}


_DOC = re.compile(r"\.(?:pdf|docx?|odt|rtf)(?:$|[?#])", re.I)


# what a page shows, in short: the same after a click that did nothing
_LOOK = "() => [...document.querySelectorAll('a, button, input, [role=button], [role=menu], [role=dialog]')].filter(e => " \
        "{ const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).length + ':' + " \
        "(document.body ? document.body.innerText.length : 0)"


def settle(page, idle=5000):
    """Let a page finish loading, and a page built by its scripts (Workday, Ashby...) show its content."""
    for state, t in (("domcontentloaded", 15000), ("networkidle", idle)):
        try:
            page.wait_for_load_state(state, timeout=t)
        except Exception:
            pass
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass


def _click(frame, b: dict, page):
    """Click a button or link; follow the tab it opens, if any. -> the page now shown. When something covers the button:
    the website's own click layer (Workday) takes a real click at its place; a banner or a scrolling box gets the click
    sent to the button itself."""
    ctx = page.context
    before, url0 = list(ctx.pages), page.url
    loc = frame.locator(f'[data-jb="{b["id"]}"]')
    try:
        href, blank = loc.evaluate("e => (e.tagName === 'A' && /^(https?|file):/i.test(e.href)) ? [e.href, e.target === '_blank']"
                                   " : ['', false]", timeout=2000)
    except Exception:
        href, blank = "", False
    look = _LOOK
    try:
        before_look = page.evaluate(look) if href else None
    except Exception:
        before_look = None
    try:
        loc.click(timeout=4000)
    except Exception as e:
        cover = next((ln for ln in str(e).splitlines() if "intercepts pointer events" in ln), "")
        try:
            if cover and not _BANNER.search(cover):
                loc.click(timeout=3000, force=True)
            else:
                loc.dispatch_event("click", timeout=3000)
        except Exception:
            pass
    try:
        for _ in range(6):  # a new tab opens within a moment, or not at all
            new = [p for p in ctx.pages if p not in before and not p.is_closed()]
            # a tab showing a document (a privacy policy PDF the website opens beside) is not the application
            new = [p for p in new if not _DOC.search(p.url or "")]
            if new:
                page = new[-1]
                break
            page.wait_for_timeout(300)
        else:
            # a link to another page whose click opened nothing (a new tab the browser held back as a popup, Cornerstone
            # at Hardis): its address is opened here
            if href and page.url == url0 and href.split("#")[0] != url0.split("#")[0] and not _DOC.search(href) \
                    and (blank or (before_look is not None and page.evaluate(look) == before_look)):
                # (only when the click changed nothing at all: a 'Postuler »' that opens a menu is no dead link, Alstom)
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    settle(page)
    return page


def scan_page(page) -> list[tuple]:
    """[(frame, scan)] for the page itself (always: its title and text say 'check your email', 'page not found') and its
    frames that hold fields or buttons."""
    out = []
    for fr in page.frames:
        try:
            sc = fr.evaluate(_COLLECT)
        except Exception:
            continue
        if sc["fields"] or sc["buttons"] or fr == page.main_frame:
            out.append((fr, sc))
    return out


def _shape(scans) -> tuple:
    return tuple((len(sc["fields"]), len(sc["buttons"])) for _, sc in scans)


def _still_scan(page, tries=6, pause=700) -> list[tuple]:
    """The page once it holds still: a page its scripts are building (Workday, SuccessFactors) is read again until two
    readings in a row show the same fields and buttons."""
    scans = scan_page(page)
    for _ in range(tries):
        try:
            page.wait_for_timeout(pause)
        except Exception:
            break
        again = scan_page(page)
        if _shape(again) == _shape(scans):
            return again
        scans = again
    return scans


def _main(scans):
    """The frame that matters: the one whose fields are a form (the user's details, a password), else the one with the
    most fields (a search box in the page does not outweigh a sign-in form in a frame), else the page itself."""
    def weight(x):
        fs = x[1]["fields"]
        return sum(1 for f in fs if f["type"] == "password" or field_kind(f) in _IDENTITY), len(fs)
    return max(scans, key=weight) if scans else (None, {"fields": [], "buttons": [], "captcha": False, "title": "",
                                                       "uploads": 0, "heading": "", "body": ""})


_MEMORABLE = {"first_name", "last_name", "full_name", "email", "phone", "address", "postal_code", "city", "country", "linkedin",
              "website", "civility", "nationality", "degree", "school", "grad_year", "field_of_study", "work_authorization",
              "sponsorship", "how_heard", "cover_letter", "username", "email_code"}  # never a password: none is ever saved there
def _saved_button(scan, memory, step, skip=()):
    """The button that worked at this step on this website before, when the page shows it again ('apply': any of the
    buttons pressed in turn there, 'Postuler' then 'Postuler manuellement')."""
    want = (memory.get("buttons") or {}).get(step)
    texts = want if isinstance(want, list) else [want] if want else []
    hits = [b for b in scan["buttons"] if b["text"] in texts and _button_id(b) not in skip]
    if step != "apply":  # Inria: the header's 'Se connecter' link and the form's own 'Se connecter': the form's button
        hits.sort(key=lambda b: (not b.get("submits"), b["tag"] == "a"))
    return hits[0] if hits else None


_LEARN_ON = ("filled", "nothing", "dry_run", "needs_code", "needs_link", "account_ready")  # visits that went the right way


def drive(page, job: dict, note=lambda m: None, accounts_path=None, max_steps: int = 10):
    """From the offer's page to its filled application form: Postuler / Apply, signing in with the account the app holds
    on the website or creating it, the code received by email (job['code'], given by the user). Stops when the form is
    filled, or when the user has to act (captcha, terms, a code, a page nobody understands). -> (page now shown, status)
    What worked is kept for the next visits, each step under the website it happened on, only when the visit went the
    right way (a button pressed on a page that led nowhere is never learned)."""
    start = site_of(page.url)
    page, status = _drive(page, job, note, accounts_path, max_steps)
    ok = status.get("state") in _LEARN_ON
    if ok:
        for site, d in (job.get("_learn") or {}).items():
            claude_forms.learn(site, buttons=d.get("buttons"), fields=d.get("fields"))
    claude_forms.learn(start, route=job.get("_route") if ok else None, result=status.get("state", ""))
    memory = claude_forms.learned(start)
    if memory.get("route"):
        status["route"] = " → ".join(memory["route"])
    status.setdefault("site", account_site(page.url))
    return page, status


# a choice of ways to sign in (Workday: Apple, Google, email): the email one, never another website's account
_EMAIL_WAY = re.compile(r"^\s*(?:(?:sign in|log ?in|continue|connect|se connecter|connexion|continuer|s'identifier|"
                        r"postuler|apply)\s+(?:with|using|via|avec|par)\s+(?:(?:an?|my|your|votre|mon|une?)\s+)?"
                        r"(?:e-?mail|adresse e-?mail|courriel)(?: address)?|(?:use|utiliser)\s+(?:my |mon |an? |une? )?"
                        r"(?:e-?mail|adresse e-?mail))\s*[→›»>]?\s*$", re.I)
# a sign-in that asks the email first (iCIMS, Oracle, IBM): only on a page that says it is one, never a newsletter box
_LOGIN_PAGE = re.compile(r"log.?in|sign.?in|signin|auth|account|compte|connexion|identif|apply/email|candidate.?(?:home|portal)|"
                         r"hcmui/candidateexperience/.*/apply", re.I)  # (Oracle: the email, a code by email, no password)
_LOGIN_WORDS = re.compile(r"sign in|log in|login|connexion|connectez|se connecter|identifi|your account|votre compte|"
                          r"espace candidat|candidate (?:home|portal|account)|enter your e-?mail|saisissez votre (?:adresse )?e-?mail",
                          re.I)
_NEXT_BTN = re.compile(r"^\s*(?:next|continue|continuer|suivant|valider|sign in|se connecter|log ?in|connexion|ok|"
                       r"let's go|c'est parti|go)\s*[→›»>]?\s*$", re.I)


def email_first(scan, url: str) -> dict | None:
    """The 'next' button of a sign-in page that asks only the email first, or None."""
    fs = [f for f in scan["fields"] if f["type"] not in ("checkbox", "radio") and f["tag"] != "select"]  # (Soitec: a
    if len(fs) != 1 or any(f["type"] == "password" for f in scan["fields"]):  # consent's own list beside the email)
        return None
    if field_kind(fs[0]) != "email" and not re.search(r"e-?mail|ibm ?id|user ?id|username|identifiant|login", _text(fs[0]), re.I):
        return None  # (the one field is the account's email or user name: IBM's 'IBMid')
    if not (_LOGIN_PAGE.search(url or "") or _LOGIN_WORDS.search(f"{scan.get('title', '')} {scan.get('heading', '')}")):
        return None
    return pick_button(scan["buttons"], _NEXT_BTN)


_CHECK_MAIL = re.compile(r"check your (?:e-?mail|inbox|mailbox)|verify your (?:e-?mail|account|address)|confirm your "
                         r"(?:e-?mail|account|address|registration)|(?:e-?mail|link) (?:has been |was )?sent|activate your account|"
                         r"v[ée]rifi(?:ez|er) votre (?:e-?mail|adresse|compte|bo[iî]te)|confirm(?:ez|er) votre (?:e-?mail|adresse|"
                         r"compte|inscription)|(?:e-?mail|courriel|lien) (?:vous )?a [ée]t[ée] envoy[ée]|consultez votre (?:bo[iî]te|"
                         r"messagerie)|activ(?:ez|er) votre compte|lien d.activation|activation link|n.a pas (?:encore )?[ée]t[ée] "
                         r"activ[ée]|(?:is |has )?not (?:yet )?(?:been )?activated|email vous a [ée]t[ée] envoy[ée]|n.a pas (?:encore )?[ée]t[ée] "
                         r"valid[ée]e?|(?:e-?mail|address) (?:is |has )?not (?:yet )?(?:been )?(?:verified|validated|confirmed)|"
                         r"cliquer sur le lien contenu dans", re.I)
# the website says the account is ready (after its activation link, or at once)
_ACCOUNT_OK = re.compile(r"votre compte est (?:maintenant )?actif|compte (?:a [ée]t[ée] |est )(?:activ[ée]|cr[ée][ée]|valid[ée])|"
                         r"inscription est (?:termin[ée]e|finalis[ée]e|valid[ée]e)|account (?:is |has been )?(?:now )?"
                         r"(?:activated|active|confirmed|verified|created)|registration (?:is )?complete|mot de passe a (?:bien )?"
                         r"[ée]t[ée] (?:modifi[ée]|r[ée]initialis[ée]|chang[ée]|enregistr[ée])|(?:modification|changement) (?:de votre "
                         r"mot de passe )?a bien [ée]t[ée] (?:prise en compte|enregistr[ée]e)|password (?:has been |was )?(?:changed|"
                         r"reset|updated)", re.I)
_CV_FIRST = ("This website starts the application with your CV: drop your CV in the window (the website reads it and fills "
             "the form), then click 'Continue' and the app fills what is left.")
_SUBMIT_SMALL = 4  # a sign-in form's own submit button: a form of a few fields (email, password, a box)


def _sign_in_button(scan, memory, planned, plan):
    b = _saved_button(scan, memory, "sign_in") or (planned if plan and plan.button_role == "sign_in" else None) or \
        pick_button(scan["buttons"], _SIGNIN_BTN)
    if b:
        return b
    # 'Valider', 'Continuer'...: the submit button of the small form that holds the password
    small = [x for x in scan["buttons"] if x.get("submits") and x.get("formFields", 0) <= _SUBMIT_SMALL
             and not _SIGNUP_TXT.search(x["text"])]
    return small[0] if len(small) == 1 else None


_ERRORS = r"""() => {
  const shown = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const out = [];
  const sel = '[role=alert], [aria-live=assertive], .error, .errors, .invalid-feedback, .field-error, .help-block, ' +
              '[class*="error" i], [class*="erreur" i], [class*="invalid" i], [id*="error" i]';
  for (const el of document.querySelectorAll(sel)) {
    if (!shown(el) || el.querySelector('input, select, textarea')) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length > 3 && t.length < 200 && !out.includes(t)) out.push(t);
  }
  for (const el of document.querySelectorAll('[aria-invalid=true]')) {
    const d = el.getAttribute('aria-describedby'), m = d && document.getElementById(d.split(' ')[0]);
    const t = m ? (m.innerText || '').replace(/\s+/g, ' ').trim() : '';
    if (t && t.length < 200 && !out.includes(t)) out.push(t);
  }
  return out.slice(0, 6);
}"""


def page_errors(page) -> list[str]:
    """What the website says is wrong (its error messages beside the fields), from every frame: shown to the user."""
    out = []
    for fr in page.frames[:6]:
        try:
            out += [t for t in fr.evaluate(_ERRORS) if t not in out]
        except Exception:
            continue
    return out[:4]


_BY_HAND = re.compile(r"(?:^|\.)(?:myworkdayjobs\.com|myworkdaysite\.com)$")  # account forms only the user can send
# a page setting a new password (a reset link from an email): its form is the account's, never a sign-in
_RESET_PAGE = re.compile(r"password-reset|reset-?password|r[ée]initialis\w* (?:du |de votre |votre )?mot de passe|"
                         r"modifier mon mot de passe|nouveau mot de passe|new password|choose a (?:new )?password", re.I)
_REFUSED = re.compile(r"(?:mot de passe|password|identifiant|login|e-?mail).{0,60}(?:incorrect|invalide|invalid|non reconnu|n.(?:est|sont) pas valides?|"
                      r"pas [ée]t[ée] reconnu|wrong|erron[ée])|(?:incorrect|invalid|wrong).{0,30}(?:password|credentials|"
                      r"login)|impossible de se connecter|identifiants? (?:incorrects?|invalides?)", re.I)
# a sign-in page's 'forgotten password' link, and the button of the page it opens that sends the reset email
_LOST_LINK = re.compile(r"mot de passe (?:perdu|oubli[ée])|oubli[ée] (?:de )?(?:votre |mon |le )?mot de passe|forgot(?:ten)? "
                        r"(?:your |my )?password|reset (?:your |my )?password|lost (?:your |my )?password|identifiants? "
                        r"(?:perdus?|oubli[ée]s?)", re.I)
_LOST_SEND = re.compile(r"recevoir|r[ée]initialis|r[ée]cup[ée]rer|retrouver|envoyer|send|reset|recover|valider|submit|"
                        r"continuer|continue|^\s*ok\s*$", re.I)
# a new-password page's own button (Sorbonne: never the 'create an account' button remembered on the website)
_RESET_BTN = re.compile(r"(?:enregistrer|modifier|changer|r[ée]initialiser|valider|d[ée]finir|save|set|reset|change|update|"
                        r"confirm)\w*\b.{0,30}(?:mot de passe|password)", re.I)
_SENDS = re.compile(r"postul|apply|candidat|submit|soumettre|envoyer|send|bewerb|transmettre", re.I)


def _cv_first(page, scans, job, note) -> bool:
    """A website that starts with the CV (Taleez, Softy, Phenom): the CV chosen for the offer, once; the website reads it.
    -> True when it was given (the page is read again)."""
    if (job.get("dry_run") and not job.get("dry_cv")) or job.get("_cv_done") or not job.get("cv_path"):
        return False
    if not attach_cv(scans, job["cv_path"], note, cv_filename(job["values"])):
        return False
    job["_cv_done"] = True
    if job.get("letter_path") and attach_letter(scans, job["letter_path"], note,
                                                cv_filename(job["values"]).replace("CV", "Lettre", 1)):
        job["_letter_done"] = True  # CNRS: 'Votre CV *' and 'Votre Lettre de Motivation *', files both
    page.wait_for_timeout(4000)
    return True


def _files_given(job) -> dict:
    """The page asks only files, and the app gave them (the CV, the letter): the user checks and submits."""
    got = ["cv"] + (["letter"] if job.get("_letter_done") else [])
    return {"state": "filled", "filled": got, "message": "Your CV" + (" and your cover letter are" if "letter" in got else
                                                                         " is") + " attached: check the form and submit it."}


def _form_submit(scan, uploads) -> dict | None:
    """An account form's own submit button whatever it says (SAP SuccessFactors: 'Postuler' on the page that makes the
    account and the candidate's profile): the one submit button of the form holding the password fields, never one of a
    form with a file field (an application being sent)."""
    subs = [b for b in scan["buttons"] if b.get("submits") and not b.get("formFile") and b.get("formFields", 0) >= 3
            and not _SIGNIN_BTN.match(b["text"]) and not re.search(r"annuler|cancel|retour|back", b["text"], re.I)
            and not _SENDS.search(b["text"])]  # never a button that sends an application, whatever the page
    return subs[0] if len(subs) == 1 and not uploads else None


def _drive(page, job, note, accounts_path, max_steps):
    seen, memories = {}, {}
    job["_route"], job["_learn"] = [], {}
    job.setdefault("_start", page.url.split("#")[0])  # the offer: where a sign-in that leads elsewhere comes back to
    tried = job.setdefault("_tried", {})  # per page address: the 'apply' buttons already pressed there
    answered, waited, steps = set(), {}, 0

    def learning(key):  # what this visit learns on the website of the page shown now
        return job["_learn"].setdefault(site_of(page.url), {}).setdefault(key, {})

    def clicked(step, b):  # this website's button for this step, remembered for the next visits
        d = learning("buttons")
        if step == "apply":
            d["apply"] = d.get("apply", []) + ([b["text"]] if b["text"] not in d.get("apply", []) else [])
        else:
            d[step] = b["text"]

    def refused(fr, sc, pg, said=()):  # the website refuses the app's password: its 'forgotten password' once, or said
        set_account(site, accounts_path, state="wrong_password")
        job["_signed"] = False  # not signed in: never 'back to the offer' as if it were
        lost = None if job.get("_lost_asked") or job.get("dry_run") else \
            pick_button(sc["buttons"], _LOST_LINK, prefer_submit=False)
        if lost:  # a link by email then sets the app's password
            job["_lost_asked"] = job["_resetting"] = True
            note("the website refuses the password: opening its 'forgotten password'")
            clicked("lost_password", lost)
            return _click(fr, lost, pg), None
        return pg, {"state": "wrong_password", "errors": list(said), "message": "The website refuses the password the "
                    "app holds for it (it may have made its own and emailed it). Type the password from its email "
                    "in the box below, or use its 'Mot de passe perdu' and set the one shown in Settings."}

    while steps < max_steps:
        url = page.url.split("#")[0]
        site = account_site(page.url)  # the website of the page shown now (its accounts, what was learned there)
        if site_of(page.url) not in memories:
            memories[site_of(page.url)] = claude_forms.learned(site_of(page.url))
        memory = memories[site_of(page.url)]
        if url not in answered:
            answered.add(url)
            if refuse_cookies(page):
                note("cookie banner: optional cookies refused")
        scans = _still_scan(page)
        frame, scan = _main(scans)
        captcha = any(sc["captcha"] for _, sc in scans) or any(_CHECK_FRAME.search(f.url or "") for f in page.frames)
        lost_form = None
        if job.get("_resetting") and not job.get("dry_run"):  # the 'forgotten password' form opened after a refused sign-in
            for fr, sc in sorted(scans, key=lambda x: not _LOST_LINK.search(f"{x[1]['title']} {x[1].get('heading', '')} "
                                                                             f"{x[1].get('body', '')[:500]}")):
                send = pick_button(sc["buttons"], _LOST_SEND)
                ids = [f for f in sc["fields"] if not f["value"] and f["type"] not in ("password", "checkbox", "radio", "file")
                       and (field_kind(f) == "email" or _USERNAME.search(_text(f)))]
                if send and ids:
                    lost_form = fr, send, ids
                    break
        if lost_form:
            fr, send, ids = lost_form
            job["_resetting"] = False
            acct = account(site, job.get("email", ""), path=accounts_path) or {}
            for f in ids:
                try:
                    _el(fr, f).fill(acct.get("email") or job.get("email", ""), timeout=3000)
                except Exception:
                    pass
            note("asking the website for a new password (its 'forgotten password' form, your account email)")
            clicked("lost_password", send)
            page = _click(fr, send, page)
            set_account(site, accounts_path, state="waiting_link")
            return page, {"state": "needs_link", "message": "The website refused the password the app holds for it, so "
                          "the app asked it for a new one: it emails you a link. Paste that link in 'Link from the "
                          "email' (the app opens it and sets the password kept in Settings), then 'Continue'."}
        if job.get("allow_terms") and not job.get("dry_run") and url not in job.setdefault("_terms_ok", []) \
                and (job.get("_created") or page_kind(scan) == "other"):
            # the website's terms to accept: after the account form (SNCF: 'J'accepte' under its CGU, in a frame), or a
            # privacy statement before it (3DS, Taleo: 'I Accept' / 'I Decline'); once a page, the user allowed it
            ok = next(((fr, x) for fr, sc in scans for x in sc["buttons"] if _TERMS_OK.search(x["text"])
                       and _TERMS_SHOWN.search(f"{sc['title']} {sc.get('heading', '')} {sc.get('body', '')[:2000]}")), None)
            if ok:
                job["_terms_ok"].append(url)
                note("the website's terms accepted (you allowed it for your accounts)")
                clicked("terms_ok", ok[1])
                page = _click(ok[0], ok[1], page)
                continue
        if job.pop("_signing", False):
            hit = next(((fr, sc) for fr, sc in scans if any(f["type"] == "password" for f in sc["fields"])
                        and _REFUSED.search(sc.get("body", "")[:4000])), None)
            if hit:
                page, st = refused(hit[0], hit[1], page, page_errors(page))
                if st:
                    return page, st
                continue
        uploads = any(sc.get("uploads") for _, sc in scans)
        # signed in, the application opened in a frame over the offer (SNCF: a pop-up 'Vous candidatez à l'offre
        # suivante'): that frame is the page now, the offer's 'Postuler' behind it is never pressed again
        app_open = next(((fr, sc) for fr, sc in scans if job.get("_signed") and fr is not page.main_frame and _APP_OPEN.search(
            f"{sc['title']} {sc.get('heading', '')} {sc.get('body', '')[:1500]}")), None)
        if app_open:
            frame, scan = app_open
        kind = page_kind(scan)
        here = tried.setdefault(url, set())
        cands = [] if app_open else apply_candidates(scans, here)
        saved_apply = _saved_button(scan, memory, "apply", here)
        if kind == "other" and (cands or saved_apply):
            kind = "offer_page"  # its 'apply' button is in a frame (iCIMS), or is this website's own, learned before
        if kind == "sign_in" and cands and steps == 0:
            # an offer page with a sign-in form in its header (Talentsoft): the offer's own 'Postuler' first, the website
            # then asks to sign in inside the application and keeps the offer (signed in from the header, it shows 'Mon espace')
            kind = "offer_page"
        pressed_all = kind == "offer_page" and not cands and not (saved_apply and _button_id(saved_apply) not in here)
        way = pick_button([b for b in scan["buttons"] if _button_id(b) not in here], _EMAIL_WAY) \
            if kind == "other" or pressed_all else None  # (an offer's own 'Apply' first)
        nxt = email_first(scan, f"{page.url} {getattr(frame, 'url', '')}") if kind == "other" else None  # iCIMS: a frame
        if way or nxt:
            if job.get("dry_run") and nxt:
                return page, {"state": "dry_run", "wall": "sign_in", "message": "dry run: a sign-in page asking the email "
                              "first (no account used)"}
            if way:  # 'Sign in with email' (never with Google, Apple or LinkedIn)
                here.add(_button_id(way))
                note(f"'{way['text'][:40]}'")
                clicked("sign_in_way", way)
                page = _click(frame, way, page)
                steps += 1
                continue
            for f in scan["fields"]:
                if f["type"] not in ("checkbox", "radio") and not (f["value"] or "").strip():
                    try:
                        _el(frame, f).fill(job.get("email", ""), timeout=3000)
                    except Exception:
                        pass
            if fill_account(frame, {"fields": [f for f in scan["fields"] if f["type"] == "checkbox"]}, {"email": "", "password": ""},
                            job.get("allow_terms", False), roles=None)["terms_left"]:
                return page, {"state": "needs_user", "message": "Your account email is filled. Tick the website's terms box "
                              "in the window - you did not allow the app to accept terms for you - then click 'Continue'."}
            if captcha:
                return page, {"state": "needs_user", "message": "Your account email is filled. Solve the captcha in the "
                              "window (the app never does), then click 'Continue'."}
            note("account email given (this website asks it first)")
            clicked("email_first", nxt)
            page = _click(frame, nxt, page)
            steps += 1
            continue
        if kind == "other" and job.get("_signed") and not job.get("_back") and url != job["_start"] \
                and not job.get("_applied"):  # (CNRS: the page its 'Postuler' opened once signed in is the application)
            job["_back"] = True  # signed in, and the website showed its own page ('Mon espace'): back to the offer, once
            note("signed in: back to the offer")
            try:
                page.goto(job["_start"], wait_until="domcontentloaded", timeout=45000)
                settle(page)
            except Exception:
                pass
            continue
        if kind == "other" and (app_open or job.get("_applied")) and not job.get("dry_run"):
            # an application in sections to open one by one (SNCF: 'Vos informations d'identité INCOMPLET'): its identity
            # section first, filled like any form; the others are the user's (education, CV)
            sec = next((x for x in scan["buttons"] if _SECTION.search(x["text"]) and _button_id(x) not in here), None)
            if sec:
                here.add(_button_id(sec))
                note(f"opening the application's section '{sec['text'][:40]}'")
                clicked("section", sec)
                page = _click(frame, sec, page)
                steps += 1
                continue
        loading = not scan["fields"] and re.search(r"\b(?:loading|chargement)\s*(?:\.\.\.|…)|please wait|veuillez patienter",
                                                   (scan.get("heading", "") + " " + scan.get("body", ""))[:400], re.I)
        if (kind == "other" or pressed_all) and waited.get(url, 0) < (8 if loading else 3):
            # a page its scripts are still building (Workday, Ashby), or a form opening after 'Apply' (Teamtailor): a moment
            waited[url] = waited.get(url, 0) + 1
            page.wait_for_timeout(1200)
            continue
        resetting = _RESET_PAGE.search(f"{page.url} {scan['title']} {scan.get('heading', '')} {scan.get('body', '')[:300]}")
        if kind == "sign_up" and pick_button(scan["buttons"], _SIGNIN_BTN) and not resetting:
            known = account(site, job.get("email", ""), path=accounts_path)
            if known and known.get("state") != "creating":
                kind = "sign_in"  # a page with both blocks (Talentsoft): the account exists, so its sign-in, never a new one
        if kind == "sign_up" and (uploads or any(field_kind(f) == "cover_letter" for f in scan["fields"])):
            kind = "application_form"  # an application form that also makes the account: filled, never sent
        if kind == "other" and _ACCOUNT_OK.search(f"{scan['title']} {scan.get('heading', '')} {scan.get('body', '')[:400]}"):
            if account(site, job.get("email", ""), path=accounts_path):
                set_account(site, accounts_path, state="created")
            return page, {"state": "account_ready", "message": f"Your account on {site_of(page.url)} is active. Open the "
                          "offer again ('Open and fill the application'): the app signs in with it."}
        if (job.get("_created") or job.get("_signed")) and kind not in ("email_code", "application_form") \
                and _CHECK_MAIL.search(f"{scan['title']} {scan.get('heading', '')} {scan.get('body', '')}"):
            set_account(site, accounts_path, state="waiting_link")  # 'an email has been sent', 'not activated yet'
            again = pick_button(scan["buttons"], _RESEND) if job.get("_signed") and not job.get("_resent") else None
            if again and not job.get("dry_run"):  # signed in, its email never came (Amundi): sent again, once
                job["_resent"] = True
                note("the website's confirmation email asked again")
                clicked("resend", again)
                page = _click(frame, again, page)
            return page, {"state": "needs_link", "message": "Your account is made: the website sent an email to confirm "
                          "it. Click the link in that email, then 'Continue' here."}
        roles, plan = dict(memory.get("fields") or {}), None  # the details Claude named on this website before
        if kind == "other" and scan["fields"] + scan["buttons"]:
            plan, how = claude_forms.plan_page(job.get("ai_key", ""), site_of(page.url), page.url, scan["title"], scan["fields"],
                                               scan["buttons"], note, free=job.get("ai_free"))
            if plan:
                kind = plan.page_kind if plan.page_kind in ("application_form", "sign_in", "sign_up", "email_code",
                                                            "offer_page") else "other"
                if kind == "email_code" and not [f for f in scan["fields"] if f["type"] not in ("checkbox", "radio")]:
                    kind = "other"  # a reading that sees a code to type on a page without any field: not believed
                named = {x.key: x.role for x in plan.fields if x.role != "other"}
                roles.update(named)
                learning("fields").update({k: v for k, v in named.items() if v in _MEMORABLE})
                note(f"page read by {how}: {kind}")
        job["_route"].append(kind)
        key = (page.url, claude_forms.signature(scan["fields"], scan["buttons"]))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 2:  # the same page again and again: an action that does not go through
            said = page_errors(page)
            if kind == "sign_in" and job.get("_signed") and _REFUSED.search(" ".join(said) + " " + scan.get("body", "")[:600]):
                page, st = refused(frame, scan, page, said)
                if st:
                    return page, st
                continue
            return page, {"state": "needs_user", "message": "This page does not move on by itself (a refused sign-in, a "
                          "field the website requires): finish it in the window, then click 'Continue'."
                          + (f" The website says: {' | '.join(said)}" if said else ""), "errors": said}
        steps += 1
        planned = None
        if plan and plan.button:
            planned = next((b for b in scan["buttons"] if claude_forms.button_key(b) == plan.button), None)

        if kind == "application_form":
            pw = [f for f in scan["fields"] if f["type"] == "password"]
            acct = account(site, job.get("email", ""), path=accounts_path)
            if job.get("accounts_only"):  # making the accounts once: signed in, nothing filled on an offer not chosen
                if acct:
                    if acct.get("state") != "created":
                        set_account(site, accounts_path, state="created")
                    return page, {"state": "account_ready", "message": "Signed in: this website's application form opens "
                                  "with the account."}
                if pw:
                    return page, {"state": "account_at_first_application", "message": "This website makes the account "
                                  "with the first application sent (its form holds the password fields)."}
                return page, {"state": "no_account_needed", "message": "This website needs no account: its application "
                              "form opens directly."}
            values, extra = dict(job["values"]), ""
            if pw and not job.get("dry_run") and job.get("email"):
                acct = acct or account(site, job["email"], create=True, path=accounts_path)
                values["email"] = acct["email"]  # the account this form makes (or signs in with) is the app's
            filled = []
            if job.get("cv_path") and (not job.get("dry_run") or job.get("dry_cv")) and not job.get("_cv_done"):
                if attach_cv(scans, job["cv_path"], note, cv_filename(job["values"])):
                    job["_cv_done"] = True
                    filled.append("cv")
                    page.wait_for_timeout(2500)  # a website reading the CV fills some fields itself
                    scans = _still_scan(page)
                    frame, scan = _main(scans)
            if job.get("letter_path") and (not job.get("dry_run") or job.get("dry_cv")) and not job.get("_letter_done") \
                    and attach_letter(scans, job["letter_path"], note, cv_filename(job["values"]).replace("CV", "Lettre", 1)):
                job["_letter_done"] = True
                filled.append("letter")
            for fr, sc in scans:
                filled += fill_frame(fr, values, job.get("letter", ""), sc, roles)
            if job.get("allow_terms"):  # its required consent box (data processing, privacy policy): the user allowed it
                for fr, sc in scans:
                    for f in sc["fields"]:
                        if f.get("role") == "combobox" and not (f["value"] or "").strip() and \
                                _COMBO_CONSENT.search(f"{f.get('label') or ''} {f.get('aria') or ''}") and \
                                _combo(fr, _el(fr, f), lambda texts: next((k + 1 for k, t in enumerate(texts)
                                                                           if _AGREE.match(t)), None)):
                            filled.append("consent")
                    boxes = {"fields": [f for f in sc["fields"] if f["type"] == "checkbox"]}
                    if boxes["fields"] and "terms" in fill_account(fr, boxes, {"email": "", "password": ""}, True, roles)["done"]:
                        filled.append("consent")
            if pw and not job.get("dry_run") and acct:
                if "password" in fill_account(frame, scan, acct, False, roles)["done"]:  # its terms box: the user's
                    extra = (" The form also makes your account on this website: the password made for it is filled in "
                             "(kept in Settings).")
            elif acct and acct.get("state") != "created":
                set_account(site, accounts_path, state="created")
            if filled:
                todo = ("your CV and cover letter are attached: check them and submit" if "letter" in filled else
                        "your CV is attached: check them, add your letter if the form takes one, and submit") if "cv" in filled \
                    else "check them, attach your CV and letter, and submit"
                return page, {"state": "filled", "filled": sorted(set(filled)), "message": f"{len(set(filled))} kind(s) of "
                              f"field filled: {todo}." + extra}
            if uploads and _cv_first(page, scans, job, note):
                continue
            if uploads:
                return page, (_files_given(job) if job.get("_cv_done") else {"state": "needs_user", "filled": [], "message": _CV_FIRST})
            return page, {"state": "nothing", "filled": [], "message": "The application form is open; it had no empty field "
                          "the app knows. Fill it in the window." + extra}

        if kind == "offer_page":
            pick = next(((frame, b) for b in (saved_apply, planned) if b and _button_id(b) not in here), None) or \
                (cands[0] if cands else None)
            if pick is None:
                if uploads and _cv_first(page, scans, job, note):
                    continue
                if uploads:
                    return page, (_files_given(job) if job.get("_cv_done") else {"state": "needs_user", "message": _CV_FIRST})
                mail = by_email(scans, (job.get("email", ""), job["values"].get("email", "")))
                if mail:
                    return page, {"state": "by_email", "message": f"This offer is applied to by email: write to {mail} with "
                                  "your CV and cover letter."}
                return page, {"state": "needs_user", "message": "The app pressed this page's 'Postuler / Apply' and the "
                              "application did not open: open it in the window, then click 'Continue'."}
            fr, b = pick
            here.add(_button_id(b))
            note(f"'{b['text'][:40]}'")
            clicked("apply", b)
            job["_applied"] = bool(job.get("_signed"))  # pressed once signed in: what opens is the application
            page = _click(fr, b, page)
            continue

        if kind in ("sign_in", "sign_up") and own_account_site(page.url):
            return page, {"state": "needs_user", "message": f"{site_of(page.url)} asks you to sign in: use your own account "
                          "there in the window (the app never makes one, nor signs in, on a job board or a social "
                          "network), then click 'Continue'."}

        if kind == "sign_in":
            acct = account(site, job.get("email", ""), path=accounts_path)
            if job.get("dry_run"):  # a test on a real website: never signs in, never creates an account
                return page, {"state": "dry_run", "wall": "sign_in", "message": "dry run: a sign-in page (no account used)"}
            if acct and acct.get("state") == "creating":
                acct = None  # its form was filled but never sent: no account there yet (the sign-up reuses its password)
            if acct:
                fill_account(frame, scan, acct, job.get("allow_terms", False), roles)
                b = _sign_in_button(scan, memory, planned, plan)
                if not b:
                    return page, {"state": "needs_user", "message": "Your sign-in details are filled: click the website's "
                                  "sign-in button in the window, then 'Continue'."}
                note("signing in with the account created on this website")
                clicked("sign_in", b)
                job["_signed"] = job["_signing"] = True
                tried.clear()  # signed in, the offer's 'Postuler' leads further now (CNRS): it may be pressed again
                page = _click(frame, b, page)
                continue
            b = _saved_button(scan, memory, "go_to_sign_up") or \
                (planned if plan and plan.button_role == "go_to_sign_up" else None) or \
                pick_button(scan["buttons"], _SIGNUP_TXT, prefer_submit=False) or \
                (pick_button(scan["buttons"], _SELF_FILL, prefer_submit=False)
                 if _SIGNUP_TXT.search(f"{scan.get('heading', '')} {scan.get('body', '')}") else None)
            if not b:
                return page, {"state": "needs_user", "message": "This website asks you to sign in and the app found no "
                              "'create an account' link: create it in the window, then click 'Continue'."}
            note("no account on this website yet: opening 'create an account'")
            clicked("go_to_sign_up", b)
            page = _click(frame, b, page)
            continue

        if kind == "sign_up":
            if job.get("dry_run"):
                return page, {"state": "dry_run", "wall": "sign_up", "message": "dry run: an account form (not created)"}
            if job.get("_created"):  # pressed once already: a second press makes the website refuse ('création bloquée')
                set_account(site, accounts_path, state="waiting_link")
                return page, {"state": "needs_link", "message": "Your account form is sent (once). The website emails you "
                              "a link, a code or your login: use it (a code or a password goes in the box here), then "
                              "click 'Continue'."}
            acct = account(site, job.get("email", ""), create=True, path=accounts_path)
            for fr, sc in ([] if resetting else scans):  # a new-password page takes the password only
                fill_frame(fr, {**job["values"], "email": acct["email"]}, "", sc, roles)
            r = fill_account(frame, scan, acct, job.get("allow_terms", False), roles, reset=bool(resetting))
            if r["security"]:
                set_account(site, accounts_path, **r["security"])  # kept with the password: needed if the website asks
            if r["terms_left"]:
                return page, {"state": "needs_user", "message": "The account form is filled (password made for this "
                              "website, kept in Settings). Tick the website's terms box in the window - you did not allow "
                              "the app to accept terms for you - then click 'Continue'."}
            if captcha:
                return page, {"state": "needs_user", "message": "The account form is filled. Solve the captcha in the "
                              "window (the app never does), then click 'Continue'."}
            tl = pick_button(scan["buttons"], _TERMS_LINK, prefer_submit=False) if job.get("allow_terms") else None
            if tl and not job.get("_terms_read"):  # read and accepted once, as the user allowed for their accounts
                job["_terms_read"] = True
                note("the website's privacy statement opened and accepted (you allowed terms)")
                page = _click(frame, tl, page)
                for fr2, sc2 in _still_scan(page):
                    ok = pick_button(sc2["buttons"], _TERMS_OK)
                    if ok:
                        page = _click(fr2, ok, page)
                        break
                continue
            b = (pick_button(scan["buttons"], _RESET_BTN) if resetting else None) or \
                _saved_button(scan, memory, "create_account") or \
                (planned if plan and plan.button_role in ("create_account", "continue") else None) or \
                pick_button(scan["buttons"], _CREATE_BTN) or \
                pick_button([x for x in scan["buttons"] if x["tag"] != "a"], _SIGNUP_TXT) or _form_submit(scan, uploads)
            if not b and any(x.get("submits") and _SENDS.search(x["text"]) for x in scan["buttons"]):
                # its only button sends an application (SAP SuccessFactors): the account comes with the first application
                if job.get("accounts_only"):
                    return page, {"state": "account_at_first_application", "message": "This website makes the account "
                                  "with the first application sent (its form asks the CV): filled, never sent."}
                cv = bool(job.get("cv_path") and not job.get("dry_run") and attach_cv(scans, job["cv_path"], note,
                                                                                    cv_filename(job["values"])))
                return page, {"state": "filled", "filled": ["cv"] if cv else [], "message": "The application form is filled, "
                              "with the account part (password made for this website, kept in Settings). "
                              + ("Your CV is attached: check, " if cv else "Attach your CV, ") + "add your letter if you wish "
                              "and send it: the website makes your account then."}
            if not b:
                return page, {"state": "needs_user", "message": "The account form is filled: click the website's button "
                              "that creates the account, then 'Continue'."}
            if _BY_HAND.search(site_of(page.url)):
                # 19 Nov 2026: Workday takes no 'Create Account' from a window a program drives (nothing is sent, even with
                # a real mouse click): its robot check, never got around. The user makes it, with this password.
                return page, {"state": "needs_user", "message": "The account form is filled (the password made for this "
                              "website is in Settings). Press its 'Create Account' yourself. If nothing happens, this website "
                              "takes accounts only from your own browser: create it there with the same email and password, "
                              "then 'Continue' here (the app signs in)."}
            note(f"creating the account (password made for this website alone): '{b['text'][:40]}'")
            clicked("create_account", b)
            job["_created"] = True
            set_account(site, accounts_path, state="sent")  # its form is sent: from now on the website knows it
            page = _click(frame, b, page)
            continue

        if kind == "email_code":
            set_account(site, accounts_path, state="waiting_code")
            code = (job.get("code") or "").strip()
            if not code:
                return page, {"state": "needs_code", "message": "The website sent a code to your email: type it below "
                              "(or in the window), then send it."}
            job["code"] = ""  # used once
            fs = [f for f in scan["fields"] if roles.get(claude_forms.field_key(f)) == "email_code"] or code_fields(scan["fields"])
            try:
                _el(frame, fs[0]).fill(code, timeout=3000)
            except Exception:
                return page, {"state": "needs_code", "message": "The code field could not be filled: type the code in the "
                              "window, then click 'Continue'."}
            b = _saved_button(scan, memory, "submit_code") or \
                (planned if plan and plan.button_role in ("submit_code", "continue") else None) or \
                pick_button(scan["buttons"], _VERIFY_BTN)
            if b:
                clicked("submit_code", b)
                page = _click(frame, b, page)
            set_account(site, accounts_path, state="created")
            continue

        said = f"{scan['title']} {scan.get('heading', '')}"
        if _GONE.search(said):
            return page, {"state": "gone", "message": "This offer's page no longer exists on the website: the offer is "
                          "probably filled or closed."}
        small = len(scan["fields"]) <= 1 and len(scan["buttons"]) <= 15  # a check or a block page holds little
        if captcha:
            return page, {"state": "needs_user", "message": "Solve the captcha in the window (the app never does), then "
                          "click 'Continue'."}
        if _CHECKING.search(said) or (small and _CHECKING.search(f"{scan.get('body', '')} {' '.join(b['text'] for b in scan['buttons'])}")):
            return page, {"state": "needs_user", "message": "The website checks that a person is there (a robot check, a "
                          "code in an image): pass it in the window (the app never does), then click 'Continue'."}
        if job.get("_created") and _CHECK_MAIL.search(f"{said} {scan.get('body', '')}"):
            set_account(site, accounts_path, state="waiting_link")
            return page, {"state": "needs_link", "message": "Your account is made: the website sent an email to confirm it. "
                          "Click the link in that email, then 'Continue' here."}
        if re.search(r"[?&](?:returnurl|redirect(?:url|_uri)?|next)=", page.url, re.I) and not job.get("_to_sign_in"):
            go = pick_button(scan["buttons"], _SIGNIN_BTN, prefer_submit=False)
            if go:  # CNRS: 'Candidater' unsigned gives its own 403 page with a return address: it means 'sign in first'
                job["_to_sign_in"] = True
                note("this website asks to sign in first")
                page = _click(frame, go, page)
                continue
        if _BLOCKED.search(said) or (small and _BLOCKED.search(scan.get("body", ""))):
            return page, {"state": "blocked", "message": "This website turned the app's window away (it takes it for a robot); "
                          "the app never gets around that. Open the offer in your own browser ('Open the offer')."}
        if uploads and _cv_first(page, scans, job, note):
            continue
        if uploads:
            return page, (_files_given(job) if job.get("_cv_done") else {"state": "needs_user", "message": _CV_FIRST})
        mail = by_email(scans, (job.get("email", ""), job["values"].get("email", "")))
        if mail:
            return page, {"state": "by_email", "message": f"This offer is applied to by email: write to {mail} with your CV "
                          "and cover letter."}
        if job.get("_created"):  # the account form was just sent and this page says nothing known: the email decides
            return page, {"state": "needs_link", "message": "Your account form is sent. If the website emails you a link or "
                          "a code, use it (a code goes in the box here), then click 'Continue'."}
        break
    return page, {"state": "no_form", "filled": [], "message": "No application form found on this page: open it in the window "
                  "(sign in, click 'Postuler / Apply'), then click 'Continue'."}


# --------------------------------------------------------------------------- the window
_HIDDEN = os.environ.get("JOBFINDER_APPLY_HIDDEN") == "1"  # tests of the whole app only: the window is not shown


def can_open_window() -> tuple[bool, str]:
    """A visible window needs a screen: not on a server or in Docker."""
    if _HIDDEN:
        return True, ""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, ("No screen here: the application window opens only when the app runs on your own computer "
                       "(not on a server or in Docker). Use 'Open the offer' instead.")
    return True, ""


def _open_failed(err: str) -> str:
    """Why an offer's page could not be opened, in the user's words."""
    if "Download is starting" in err:
        return ("This offer is a document (a PDF), not a web page: there is no form to fill. Read it ('Open the offer') "
                "and apply as it says (often by email).")
    if "ERR_NAME_NOT_RESOLVED" in err or "ERR_CONNECTION" in err or "ERR_ADDRESS" in err:
        return "This offer's website cannot be reached (it may be gone, or offline for a while)."
    if "ERR_CERT" in err or "SSL" in err:
        return ("This website's security certificate is invalid or expired, so the window does not open it (your details "
                "are never typed into a website that cannot prove who it is).")
    if "Timeout" in err:
        return "This offer's page did not load in time: try again in a moment."
    return f"This offer's page could not be opened: {err.splitlines()[0][:160]}"


class ApplyWindow:
    """One browser window for applications, owned by one worker thread. status[uid]: what happened to each offer."""

    def __init__(self, profile_dir=PROFILE_DIR, headless=False, accounts_path=None):  # headless, accounts_path: tests
        self.profile_dir, self.headless, self.q, self.status = str(profile_dir), headless, queue.Queue(), {}
        self.accounts_path = accounts_path
        self.lock, self.thread, self.pages = threading.Lock(), None, {}

    def _set(self, uid, **kw):
        with self.lock:
            self.status[uid] = {**self.status.get(uid, {}), **kw}

    def open(self, uid, url, job: dict):
        self._set(uid, state="opening", message="Opening the offer in the application window…", filled=[])
        self._start()
        self.q.put(("open", uid, url, job))

    def open_link(self, uid, link: str, job: dict):
        """A link from the website's email (activate the account, set its password), pasted by the user: opened in the
        offer's tab, where the app goes on (a password page gets the password made for the website)."""
        self._set(uid, state="working", message="Opening the link from the email in the window…")
        self._start()
        self.q.put(("link", uid, link, job))

    def resume(self, uid, job: dict):
        """'Continue' after the user acted in the window (signed in, solved a captcha, ticked a box, next step)."""
        self._set(uid, state="working", message="Going on from the page shown in the window…")
        self._start()
        self.q.put(("resume", uid, None, job))

    def _start(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="apply-window")
            self.thread.start()

    def _page(self, pw, ctx):
        """-> (context, a tab for a new offer). The user may have closed the window: it is opened again."""
        for _ in range(2):
            try:
                if ctx is None:
                    Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
                    self.pages = {}
                    ctx = pw.chromium.launch_persistent_context(self.profile_dir, headless=self.headless, viewport=None,
                                                                args=["--start-maximized"])
                    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())  # its first, blank tab
                return ctx, ctx.new_page()
            except Exception:
                try:
                    ctx.close()
                except Exception:
                    pass
                ctx = None
        raise RuntimeError("the application window could not be opened")

    def _run(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = None
            while True:
                kind, uid, url, job = self.q.get()
                try:
                    if kind == "open":
                        ctx, page = self._page(pw, ctx)
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as e:
                            self._set(uid, state="no_form", filled=[], message=_open_failed(str(e)), site=site_of(url))
                            continue
                        settle(page, idle=10000)
                    elif kind == "link":
                        page = self.pages.get(uid)
                        if page is None or page.is_closed():
                            ctx, page = self._page(pw, ctx)
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as e:
                            self._set(uid, state="error", message=_open_failed(str(e)))
                            continue
                        settle(page, idle=8000)
                        job = {**job, "_back": True}  # signed in afterwards: never sent back to the link (used once)
                    else:
                        page = self.pages.get(uid)
                        if page is None or page.is_closed():
                            self._set(uid, state="error", message="The application window was closed: click 'Open and fill' again.")
                            continue
                    notes = []
                    page, status = drive(page, job, notes.append, self.accounts_path)
                    self.pages[uid] = page
                    page.bring_to_front()
                    self._set(uid, **{"filled": [], **status, "steps": notes[-8:]})
                except Exception as e:
                    ctx = None if "closed" in str(e).lower() or "target" in str(e).lower() else ctx
                    self._set(uid, state="error", message=f"The window could not do it: {str(e)[:160]}")


WINDOW = ApplyWindow(headless=_HIDDEN)


# --------------------------------------------------------------------------- the form's cover-letter box
MAX_LINES, LINE_CHARS = 5, 220  # the form's cover-letter box: 5 lines at most (the user's rule), each a readable line


def message_path(uid: str):
    """Where the short message written for an offer is kept (written once, reused at every 'Open and fill')."""
    return DATA_DIR / "letters" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', uid)}_form.txt"


def _one_line(text: str, limit=LINE_CHARS) -> str:
    """One sentence on one line, cut at a word (with '…') when the AI wrote a longer one."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= limit else t[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def form_message(applicant: dict, lines: list[str]) -> str:
    """The text pasted into an application form's cover-letter box: the greeting, at most 3 sentences, the signature with
    the name; never more than MAX_LINES lines, whatever the AI returned."""
    name = applicant.get("name") or " ".join(x for x in (applicant.get("first_name"), applicant.get("last_name")) if x)
    body = [_one_line(x) for x in lines if (x or "").strip()][:MAX_LINES - 2]
    return "\n".join(["Madame, Monsieur,", *body, f"Cordialement, {name}".strip().rstrip(",")])


def clamp_lines(text: str, n=MAX_LINES) -> str:
    """Any text, kept to its first n non-empty lines (each cut to a readable length)."""
    return "\n".join([_one_line(x) for x in (text or "").splitlines() if x.strip()][:n])
