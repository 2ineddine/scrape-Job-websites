"""trim.py - the part of a job offer worth sending to the AI.

Career pages wrap a short offer in a lot of text the AI does not need: the company's presentation (often twice), its
labels and values, the diversity statement, the benefits, the recruitment process, breadcrumbs, the application form,
cookie and reCAPTCHA notices, a list of similar offers. That text costs tokens and time on every call and can blur the
answer. Only what is SENT to the AI is trimmed: the stored offer, the date checks and the evidence checks use the full text.

The stored descriptions are flat (no line breaks), so the text is cut into sentences and bullets, and each one is judged:
  - a sentence that states a FACT (contract, salary, remote work, place, a date, experience, degree, languages, duration,
    start) is always kept: the analysis and the statistics read them;
  - a passage already seen earlier in the offer is dropped (companies repeat their presentation at the top and bottom);
  - boilerplate (company pitch, labels, diversity, legal, forms, social links, breadcrumbs) is dropped, and so is every
    sentence of a boilerplate SECTION ('À propos', 'Avantages', 'Processus de recrutement'...) until the next job section;
  - everything else (missions, profile, context, the subject) is kept, in its order.
"""
from __future__ import annotations

import re
import unicodedata

from .dates import find_dates
from .web import html_to_text


def _n(x: str) -> str:
    x = unicodedata.normalize("NFKD", x.lower())
    return re.sub(r"\s+", " ", "".join(c for c in x if not unicodedata.combining(c))).strip()


# ---------------------------------------------------------------------------- the end of the offer
# the application form, the 'similar offers' list and the page footer: everything after the first of these markers, once
# the offer itself is over (in its last 45%), is not the offer
_TAIL = re.compile(
    r"ce poste vous interesse|postulez (?:en ligne|maintenant|des maintenant)|nom \(necessaire\)|types de fichiers acceptes|"
    r"nos offres du meme secteur|offres similaires|offres d'emploi similaires|ces offres pourraient|vous aimerez aussi|"
    r"voir toutes nos offres|similar (?:jobs|positions|offers)|you may also like|other jobs you|related jobs|"
    r"protege par recaptcha|protected by recaptcha|this site is protected|apply for this job|submit (?:your )?application|"
    r"envoyer ma candidature|deposer (?:ma|votre) candidature|candidature spontanee")

# ---------------------------------------------------------------------------- section headings
_CORE_HEAD = (r"(?:les |vos |tes |ta |votre |la )?missions?(?: du poste| principales)?|le poste|description d[eu] poste|"
              r"poste|description|"
              r"descriptif du poste|ce que (?:vous ferez|tu feras)|vos responsabilites|responsabilites|responsibilities|"
              r"(?:your|the) role|about the (?:role|job|position)|job description|what you(?:'ll| will) do|"
              r"(?:votre |le )?profil(?: recherche| souhaite)?|about you|vos atouts|competences(?: requises| attendues)?|qualifications|"
              r"requirements|what we(?:'re| are) looking for|who you are|skills|prerequis|en resume|"
              r"informations? cles|key information|details? du poste|sujet(?: de (?:these|stage))?|"
              r"contexte|context|environnement technique|stack technique|tech stack|objectifs?|activites|"
              r"job requirements|offre disponible")
_BOILER_HEAD = (r"qui sommes[- ]nous|a propos(?: de nous| de l'entreprise| de la societe)?|about us|about the company|"
                r"(?:l'|notre |votre future )entreprise|description de l'entreprise|la societe|presentation de (?:l'entreprise|la societe)|"
                r"company description|our company|who we are|pourquoi (?:nous )?rejoindre|why (?:join|work)|"
                r"(?:nos )?avantages|benefits|perks|what we offer|ce que nous (?:vous )?(?:offrons|proposons)|"
                r"processus de recrutement|recruitment process|hiring process|(?:notre|le) processus|"
                r"deroulement du recrutement|nos valeurs|our values|diversite(?: et inclusion)?|diversity|"
                r"equal opportunit\w*|egalite des chances|engagements?(?: rse)?|mais aussi\.*|additional information|"
                r"informations? (?:supplementaires|complementaires)|our commitment|notre engagement|work authorization")
_HEAD = re.compile(rf"^(?:(?P<core>{_CORE_HEAD})|(?P<boiler>{_BOILER_HEAD}))\b\s*:?\s*")

# ---------------------------------------------------------------------------- what a sentence says
# FACTS: always kept (the analysis reads them, the statistics count them)
_FACT = re.compile(
    r"\bcdi\b|\bcdd\b|\bstage\b|stagiaire|\bintern(?:ship)?s?\b|alternance|apprenti|\bthese\b|\bthesis\b|\bph ?d\b|"
    r"doctora|post-?doc|freelance|\bv\.?i\.?e\b|\bcontrat|\bcontract\b|full[- ]time|part[- ]time|temps (?:plein|partiel|complet)|"
    r"salaire|remuneration|salary|compensation|\d+ ?k ?€|\d ?k€|€ ?(?:brut|bruts|/ ?an|par an|annuel)|\bk€|"
    r"\d{2,3}[ .\u202f]?\d{3} ?€|€ ?\d{2,3}[ .,]?\d{3}|\d+ ?(?:€|eur) ?(?:/|par) ?(?:an|mois|jour)|"
    r"teletravail|\bremote\b|\bhybrid|work from home|travail a domicile|\d jours? (?:de teletravail|/ ?semaine|par semaine)|"
    r"\bsur site\b|\bon[- ]site\b|"
    r"\blocalisation\b|\blocation\b|\blieu\b|\bbase[e]?s? (?:a|en|in)\b|\blocated\b|offre disponible|available in|"
    r"\d+ ?(?:\+ )?(?:ans?|years?) (?:d'experience|d'exp|minimum|of experience|or more)|(?:au moins|minimum|plus de|"
    r"at least) \d+ ?(?:ans?|years?)|experience (?:de \d|d'au moins|minimum|requise|significative|of \d)|"
    r"years? of (?:professional |relevant )?experience|"
    r"\bjunior\b|\bsenior\b|debutant|diplome|\bdegree\b|\bbac ?\+|\bmaster\b|ecole d'ingenieur|engineering degree|"
    r"anglais|english|francais|\bfrench\b|\blangues?\b|\blanguages?\b|"
    r"\bduree\b|\bduration\b|\d+ mois\b|\d+ months?\b|\bdebut\b|start date|starting|a pourvoir|des que possible|\basap\b|"
    r"\bpublie|\bposted\b|\bdate\b|reference de l'offre|\bref(?:erence)?\.? ?:")
# the work itself, addressed to the candidate (company pages use the same technical words about themselves)
_CORE = re.compile(
    r"\bvous (?:serez|aurez|allez|devrez|travaillerez|interviendrez|participerez|integrerez|rejoindrez l'equipe|"
    r"contribuerez|prendrez|assurerez|developperez|concevrez|realiserez|etes (?:titulaire|diplome)|maitrisez|avez)|"
    r"\bvous (?!beneficierez)\w+(?:erez|irez|drez)\b|\btu (?!beneficieras)\w+(?:eras|iras|dras)\b|"
    r"\bvotre (?:maitrise|experience|expertise|connaissance|mission|role)\b|\bvous disposez|\bvous collaborez|"
    r"\bvous analysez|\bvous (?:concevez|developpez|participez|intervenez|travaillez|accompagnez|pilotez)\b|"
    r"\btu (?:seras|auras|vas|as)\b|\byou(?:'ll| will| would| have| are (?:a|an|familiar|comfortable))\b|"
    r"\byour (?:role|mission|responsibilit|tasks)|\bnous recherchons|\bnous cherchons|we(?:'re| are) looking|"
    r"\ble candidat|\ble profil (?:recherche|souhaite|ideal)|\bthe (?:candidate|successful)|au sein de (?:l'equipe|notre equipe|la direction)|"
    r"within (?:the|our) team|rattache|\bmissions? (?:principales|du poste|:)|\bresponsibilities\b|\brequirements?\b|"
    r"\bpython\b|c\+\+|pytorch|tensorflow|kubernetes|docker|\bjava\b|\bsql\b|matlab|\bros2?\b|\blinux\b|"
    r"machine learning|deep learning|computer vision|vision par ordinateur|\bllms?\b|traitement du signal")
# boilerplate, always dropped unless the sentence states a fact: page furniture, legal text, labels, diversity statement
_STRONG = re.compile(
    r"great place to work|top ?employer|\bb ?corp|ecovadis|happy at work|happy clients|choose my company|best of the world|"
    r"handicap|disabilit|egalite des chances|equal opportunit|diversite|diversity|\binclusi|sans distinction|"
    r"regardless of|discriminat|"
    r"cookie|\brgpd\b|\bgdpr\b|recaptcha|politique de confidentialite|privacy|conditions d'utilisation|mentions legales|"
    r"suivez-nous|nous suivre|follow us|\bblog\b|linkedin|twitter|instagram|facebook|youtube|https?://|www\.|"
    r"\bpartager\b|share this|\(necessaire\)|\(required\)|ce champ|je decouvre|toutes nos offres|"
    r"let's move forward|rencontrons-nous|apply now|"
    r"bien-etre|well-being|teambuilding|afterwork|tickets? restaurant|mutuelle|carte swile|epargne salariale|"
    r"\bambiance\b|convivial|bienveillan")
# the company talking about itself: dropped unless the sentence is about the work or states a fact
_WEAK = re.compile(
    r"cree en \d{4}|creee en \d{4}|fonde[e]? en \d{4}|founded in \d{4}|depuis (?:plus de )?\d+ ans|depuis \d{4}|"
    r"\d[\d\s.,]* (?:collaborateurs|consultants|salaries|employes|employees|people|personnes|ingenieurs|experts)\b|"
    r"\d+ (?:pays|countries|implantations|agences|sites)\b|chiffre d'affaires|revenue of|\dm€|\bmd€|"
    r"acteur (?:majeur|mondial|de reference|incontournable|cle)|leader (?:mondial|europeen|francais|du|de|in|of)|"
    r"\blabels?\b|certifie|certified|filiales|nos offres|rejoignez[- ]nous|join us|\bpostuler\b|offre disponible :|"
    r"nos valeurs|our values|notre mission est|our mission is|engage(?:e|s)? (?:en faveur|pour)|\brse\b")
# a mission bullet starts with a task verb ('Concevoir, développer et optimiser des pipelines...')
_TASK = re.compile(
    r"(?:concevoir|developper|participer|realiser|assurer|mettre en|rediger|analyser|optimiser|maintenir|contribuer|"
    r"piloter|definir|implementer|integrer|tester|valider|animer|gerer|collaborer|travailler|etudier|evaluer|proposer|"
    r"construire|deployer|superviser|coordonner|encadrer|automatiser|modeliser|identifier|garantir|preparer|documenter|"
    r"design|develop|build|implement|maintain|collaborate|lead|own|drive|conduct|perform|analy[sz]e|write|create|"
    r"deploy|test|evaluate|ensure|manage|participate|contribute|investigate|train|benchmark)\b")


# headings written inside the flat text ('... H/F En résumé Type de contrat CDI'): a new unit starts there
_HEAD_INLINE = re.compile(
    r"(?<=\s)(?=(?:En résumé|Les missions|Vos missions|Votre mission|Missions principales|Le poste|Description du poste|"
    r"Description de poste|POSTE :|DESCRIPTION :|"
    r"Descriptif du poste|Profil recherché|Votre profil|Compétences requises|Qualifications|Responsibilities|"
    r"Requirements|About the role|About you|What you'll do|What you will do|Who you are|Your role|The role|"
    r"Qui sommes-nous|À propos|A propos|About us|Avantages|Nos avantages|Benefits|What we offer|"
    r"Pourquoi nous rejoindre|Pourquoi rejoindre|Why join|Processus de recrutement|Recruitment process|Hiring process|"
    r"Informations complémentaires|Environnement technique|Mais aussi|Company Description|Job Description|"
    r"Additional Information|Je découvre|Offre disponible|Nos offres du même secteur|Description de l'entreprise|"
    r"Informations supplémentaires)\b|Rejoindre [A-Z]\w+, c)")
_NAV = re.compile(r"(?:accueil|menu|partager|postuler|retour|connexion|se connecter|envoyer|candidater|imprimer|"
                  r"voir l'offre|home|share|apply|print|back)\W*")


def _units(text: str) -> list[str]:
    """Sentences and bullets of a flat text."""
    t = re.sub(r"\s+", " ", text).strip()
    t = _HEAD_INLINE.sub("\n", t)
    t = re.sub(r"\s(?=(?:[•▪●◦·]|[–—]\s|-\s)[^\s])", "\n", t)  # bullets
    t = re.sub(r"(?<=[.!?…;])\s+(?=[\"«“(]?[A-ZÀ-Ý0-9])", "\n", t)  # sentence ends
    t = re.sub(r"(?<=[a-zà-ÿ0-9)])\.(?=[A-ZÀ-Ý])", ".\n", t)  # 'transformation digitale.Depuis' (glued sentences)
    return [u.strip() for u in t.split("\n") if u.strip()]


def _protected_spans(text: str):
    try:
        return [(h.start, h.start + len(h.text)) for h in find_dates(text)]
    except Exception:  # pragma: no cover - a date reader slip never blocks an analysis
        return []


def _company_rx(company: str):
    """The employer's own name ('Davidson', 'Sia Partners' -> 'sia'): sentences about it are its presentation."""
    words = [w for w in re.findall(r"[a-z0-9]+", _n(company or "")) if len(w) >= 3 and w not in
             ("groupe", "group", "the", "les", "sas", "inc", "ltd", "gmbh", "france", "partners", "consulting", "labs")]
    return re.compile(rf"\b{re.escape(words[0])}") if words else None


def _place_rx(place: str):
    """The offer's own places ('Lille', 'Bruxelles ; Île-de-France', 'Lyon (69), FR'): a sentence naming one is kept, the
    AI may need it to confirm where the job is."""
    names = [_n(re.split(r"[,(]", x)[0]).strip() for x in re.split(r"\s*[;|/]\s*", place or "")]
    names = [x for x in names if len(x) >= 3 and x not in ("france", "remote", "teletravail", "hybride", "hybrid")]
    return re.compile("|".join(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])" for x in names)) if names else None


def essential_text(text: str, limit: int = 9000, company: str = "", place: str = "") -> str:
    """The offer without its boilerplate, at most `limit` characters (see the module docstring). company: the employer,
    whose own presentation is dropped; place: the offer's location, whose sentences are kept."""
    if re.search(r"</?(?:p|span|div|li|ul|br|strong)\b[^>]*>", text or "", re.I):
        text = html_to_text(text)  # an offer stored with its page's markup (kept from an earlier scan)
    text = re.sub(r"\s+", " ", text or "").strip()
    about, where = _company_rx(company), _place_rx(place)
    if len(text) <= 900:  # a short offer: nothing to gain
        return text[:limit]
    low = _n(text)
    tail = _TAIL.search(low, int(len(low) * 0.55))
    body = text[:tail.start()] if tail and len(low) == len(text) else text  # same length: positions match
    dates = _protected_spans(body)
    kept, seen, section, pos = [], set(), "intro", 0  # intro: before any heading
    for u in _units(body):
        start = body.find(u, pos)
        pos = start + len(u) if start >= 0 else pos
        nu = _n(u)
        h = _HEAD.match(nu)
        if h:
            section = "core" if h.group("core") else "boiler"
            if not nu[h.end():].strip():
                continue  # a heading alone
        has_date = start >= 0 and any(a < start + len(u) and b > start for a, b in dates)
        fact = bool(_FACT.search(nu)) or has_date or bool(where and where.search(nu))
        body_u = nu[h.end():] if h else nu
        core = bool(h and h.group("core")) or bool(_CORE.search(nu)) or bool(_TASK.match(body_u.lstrip("-–—•·▪● ")))
        key = re.sub(r"[^a-z0-9]", "", nu)
        if len(key) >= 40 and key in seen and not has_date:
            continue  # the same passage again (a company presentation repeated at the bottom)
        seen.add(key)
        if fact:
            kept.append((u, True, section))
            continue
        if not core and (_STRONG.search(nu) or (nu.count(" / ") >= 3 and len(u) < 250)):
            continue  # page furniture; breadcrumbs: 'Accueil / Nos offres / ...' (not a skill list 'Scrum / Kanban')
        if not core and (section == "boiler" or _WEAK.search(nu)):
            continue
        if about and about.search(nu) and not core:
            continue  # the company about itself ('Davidson accompagne ses clients...', 'Sia is a next-generation...')
        if _NAV.fullmatch(nu):
            continue  # a menu word alone ('Accueil', 'Partager')
        kept.append((u, core, section))
    # an offer with a real job section ('Vos missions', 'Profil', 'Job Description'): the text BEFORE it is the company's
    # introduction ('Parce que notre développement repose sur des principes forts...'); only its facts and its sentences
    # about the work stay
    if sum(len(u) for u, _, sec in kept if sec == "core") >= 300:
        # only the company talking about itself goes (its name, 'nous / notre / we / our'): a neutral summary of the job
        # before its first heading ('Consultant Data Analyst - Big Data, Data Science...') stays
        talk = lambda u: (about and about.search(_n(u))) or re.search(r"\b(?:nous|notre|nos|we|our|us)\b", _n(u))
        kept = [x for x in kept if x[2] != "intro" or x[1] or not talk(x[0])]
    out = " ".join(u for u, _, _ in kept)
    if len(out) < 400:  # almost nothing left: the rules did not fit this page, keep it (without repeats)
        out = body
    if len(out) > limit:  # still too long: the facts and the work first, the rest only if there is room
        must = " ".join(u for u, strong, _ in kept if strong)
        out = must if len(must) <= limit else must[:limit]
    return out[:limit]
