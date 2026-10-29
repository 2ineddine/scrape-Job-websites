"""classify.py - France-location check and bilingual (French / English) analysis of job titles.

Titles come in two shapes:
  * classic job titles      'Ingénieur Machine Learning H/F', 'Senior Data Scientist (m/w/d)', '2026-460 - Ingénieur R&D'
  * research-topic titles   'Apprentissage profond pour la détection d'anomalies dans les séries temporelles',
                            'Thèse : reconstruction 3D à partir d'images satellite', 'Robust speech recognition in noise'
analyze_title() cleans the title (gender marks, reference codes), detects its language, its shape, the role, the contract
and the research topics it names (one bilingual vocabulary: an English keyword also finds the French title), then gives a
verdict: pass (clearly wanted) / maybe (the AI reads the description) / reject (clearly not wanted)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .web import _wb, norm


def html_location(soup) -> str:
    """Location from microdata, ATS markup (data-careersite-propertyid, jobGeoLocation...) when JSON-LD is absent."""
    out = []
    add = lambda v: out.append(re.sub(r"\s+", " ", v).strip()) if v and v.strip() else None
    for t in soup.find_all(attrs={"itemprop": re.compile("^(streetAddress|addressLocality|addressRegion|addressCountry)$", re.I)}):
        add(t.get("content") or t.get_text(" ", strip=True))
    for t in soup.select('[data-careersite-propertyid="location"], .jobGeoLocation, .job-location, .jobLocation, '
                         '[class*="job-location"], [class*="jobLocation"]'):
        add(t.get_text(" ", strip=True)[:120])
    seen, res = set(), []
    for v in out:
        if norm(v) not in seen:
            seen.add(norm(v))
            res.append(v)
    return " ; ".join(res)[:200]


_LOC_LABEL = re.compile(r"(?:(?:localisation|lieu(?: de travail)?|location|ville|city|site|implantation)\s*:|"
                        r"(?:localisation du poste|lieu du poste|lieu de travail|job locations?|work locations?)\s*[:|]?)"
                        r"\s*([^\n|;•]{2,60}?)(?=\s{2,}|\s*[|;•\n]|\s+[A-Z][a-zé]+(?:\s+[A-Za-zé]+)?\s*:|\s+Ville\b|$)", re.I)
_BOILER = re.compile(r"^(error|erreur|404|page not found|page introuvable|not found|mentions legales|politique (?:des|de) "
                     r"(?:cookies|confidentialite)|cookies?|privacy|privacy policy|confidentialite|cgu|conditions generales|"
                     r"contact|contactez-nous|accessibilite|accessibility|plan du site|sitemap|faq|login|log in|sign in|connexion|"
                     r"se connecter|accueil|home|candidatures? spontanees?|spontaneous applications?|gestion des cookies|donnees personnelles|"
                     r"protection des donnees|data protection|legal notice|terms of use|cookie policy|cookie settings)\b")


def is_boilerplate_title(title) -> bool:
    """'Mentions légales', 'Error', 'Politique des cookies'...: a site page, not an offer."""
    return bool(_BOILER.match(norm(title or "").strip(" -:|")))


def location_from_text(text) -> str:
    """'Localisation : Rueil-Malmaison (92)' / 'Localisation du poste France, Occitanie, Gard (30)' written in a job
    text. A search form can also say 'Localisation ...': a value that names a real place is preferred."""
    found = [m.group(1).strip(" .-,") for m in _LOC_LABEL.finditer(str(text or "")[:8000])]
    return next((x for x in found if classify_location(x)["location_verified"]), found[0] if found else "")


# --------------------------------------------------------------------------- location (France)
FR_PLACES = ["paris", "ile-de-france", "ile de france", "idf", "lyon", "toulouse", "grenoble", "bordeaux", "lille", "nantes",
             "sophia antipolis", "sophia-antipolis", "marseille", "montpellier", "strasbourg", "rennes", "nice",
             "saclay", "palaiseau", "massy", "boulogne-billancourt", "issy-les-moulineaux", "la defense", "courbevoie",
             "nanterre", "villeneuve-d'ascq", "meylan", "rouen", "clermont-ferrand", "dijon", "brest", "lannion",
             "aix-en-provence", "occitanie", "bretagne", "normandie", "auvergne-rhone-alpes", "grand est", "puteaux",
             "levallois-perret", "montrouge", "velizy", "velizy-villacoublay", "gif-sur-yvette", "orsay", "nancy", "metz",
             "tours", "caen", "rueil-malmaison", "rueil malmaison", "saint-denis", "montigny-le-bretonneux", "guyancourt",
             "elancourt", "merignac", "blagnac", "colomiers", "labege", "valbonne", "biot", "meudon", "clamart", "chatillon",
             "saint-cloud", "suresnes", "neuilly-sur-seine", "clichy", "saint-ouen", "montreuil", "ivry-sur-seine",
             "vitry-sur-seine", "creteil", "noisy-le-grand", "marne-la-vallee", "cergy", "cergy-pontoise", "evry", "les ulis",
             "courtaboeuf", "saint-quentin-en-yvelines", "versailles", "toulon", "angers", "le mans", "orleans", "limoges",
             "poitiers", "la rochelle", "pau", "bayonne", "perpignan", "nimes", "avignon", "annecy", "chambery",
             "saint-etienne", "besancon", "mulhouse", "reims", "amiens", "le havre", "valenciennes", "douai", "dunkerque",
             "niort", "vannes", "lorient", "saint-nazaire", "la ciotat", "aubagne", "istres", "vernon", "chartres",
             "troyes", "belfort", "sochaux", "villeurbanne", "ecully", "saint-priest", "venissieux", "pessac", "talence",
             "hauts-de-france", "nouvelle-aquitaine", "provence-alpes-cote d'azur", "paca", "pays de la loire",
             "centre-val de loire", "bourgogne-franche-comte"]
FR_MARKERS = ["france", "french republic", "republique francaise"]
OTHER_PLACES = ["united kingdom", "uk", "gb", "gbr", "great britain", "england", "scotland", "wales", "london", "germany",
                "deutschland", "berlin", "munich", "united states", "usa", "us", "new york", "san francisco", "seattle",
                "texas", "canada", "toronto", "montreal", "india", "bangalore", "bengaluru", "chennai", "pune", "hyderabad",
                "singapore", "japan", "tokyo", "china", "shanghai", "beijing", "spain", "madrid", "barcelona", "italy",
                "milan", "netherlands", "amsterdam", "ireland", "dublin", "switzerland", "zurich", "geneva", "geneve",
                "lausanne", "poland", "warsaw", "israel", "tel aviv", "australia", "sydney", "brazil", "mexico", "belgium",
                "brussels", "bruxelles", "sweden", "stockholm", "denmark", "norway", "portugal", "lisbon", "lisbonne",
                "porto", "austria", "vienna", "czech", "prague", "romania", "bucharest", "luxembourg", "finland",
                "helsinki", "korea", "seoul", "dubai", "uae", "morocco", "maroc", "casablanca", "rabat", "tunisia",
                "tunisie", "tunis", "algeria", "algerie", "egypt", "turkey", "turquie", "istanbul", "hungary", "budapest",
                "slovakia", "philippines", "manila", "vietnam", "thailand", "malaysia", "indonesia", "south africa",
                "argentina", "chile", "colombia", "peru", "greece", "croatia", "serbia", "bulgaria", "ukraine", "qatar",
                "saudi arabia", "allemagne", "royaume-uni", "royaume uni", "espagne", "italie", "belgique", "suisse",
                "pays-bas", "pays bas", "etats-unis", "etats unis", "inde", "chine", "pologne", "roumanie", "irlande",
                "suede", "danemark", "norvege", "autriche", "japon", "singapour", "bresil", "mexique"]
_FR_DEPT = re.compile(r"\((?:0[1-9]|[1-8]\d|9[0-5]|2a|2b|97[1-6])\)")  # 'Rueil-Malmaison (92)': French department number


def _part_country(p: str):
    if any(_wb(w, p) for w in FR_MARKERS) or _wb("fr", p) or _wb("fra", p):
        return "FR"
    if any(_wb(w, p) for w in OTHER_PLACES):
        return "OTHER"
    m = re.search(r",\s*([a-z]{2})\s*$", p)  # "City, XX" with a foreign ISO country / state code
    if m and m[1] not in ("fr", "eu"):
        return "OTHER"
    if any(_wb(w, p) for w in FR_PLACES) or _FR_DEPT.search(p):
        return "FR"
    return None


def classify_location(loc, country_hint=None, remote_hint=None, applicant_countries=None):
    """Evidence-based France eligibility. Never infers from company presence; missing data -> unverified."""
    original = (loc or "").strip()
    t = norm(original)
    remote = bool(remote_hint) or bool(re.search(r"remote|teletravail|telework|work from home|distanciel|full remote", t))
    hybrid = bool(re.search(r"hybrid|hybride", t))
    r = dict(location_original=original, country_code=None, location_verified=False, france_eligible=False,
             location_evidence="Location missing or ambiguous", city=None,
             remote_status="hybrid" if hybrid else "remote" if remote else ("onsite" if t else "unknown"))
    parts = [p.strip() for p in re.split(r"[;|/•\n]| or | ou ", t) if p.strip()]
    kinds = [_part_country(p) for p in parts]
    ch = norm(country_hint or "")
    ac = [norm(x) for x in (applicant_countries or [])]
    if "FR" in kinds:
        r.update(country_code="FR", location_verified=True, france_eligible=True,
                 location_evidence=f"Listing location '{original}' includes France")
        if not remote:
            r["city"] = re.sub(r"^fr-\w+-", "", original.split(",")[0].strip(), flags=re.I).title()
    elif ch in ("fr", "france", "fra"):
        r.update(country_code="FR", location_verified=True, france_eligible=True,
                 location_evidence="Structured country field = France")
    elif ac:
        fr = any(x in ("france", "fr", "fra") for x in ac)
        r.update(country_code="FR" if fr else None, location_verified=True, france_eligible=fr,
                 location_evidence="Applicant-location requirements: " + ", ".join(applicant_countries))
    elif ch:
        r.update(country_code=ch.upper()[:3], location_verified=True,
                 location_evidence=f"Structured country field = {country_hint}")
    elif kinds and all(k == "OTHER" for k in kinds):
        r.update(location_verified=True, location_evidence=f"Listing location '{original}' is outside France")
    return r


# --------------------------------------------------------------------------- title analysis
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/.,:;()\[\]|–—·]", " ", norm(s))).strip()


def _kw(k: str, t: str) -> bool:  # whole-word match, tolerant to plurals
    return bool(k) and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?:s|es|x)?(?![a-z0-9])", t) is not None


# ---- 1. cleaning: what is not part of the title itself
_GENDER = re.compile(r"\s*[(\[]?\s*\b(?:[hfmwdx]\s*/\s*[hfmwdx](?:\s*/\s*[hfmwdxn])?|homme\s*/\s*femme|femme\s*/\s*homme|"
                     r"all genders?|tous genres)\b\s*[)\]]?", re.I)
_INCLUSIVE = re.compile(r"(?<=[a-zé])(?:\(e\)|\(ne\)|\(se\)|[·.]e\b|[·.]ne\b|[·.]se\b|·rice\b)", re.I)  # Ingénieur(e), chercheur·se
_REF_LEAD = re.compile(r"^\s*(?:r[ée]f\.?\s*:?\s*)?(?=[\w-]*\d)[A-Z0-9][\w-]{2,}\s*[-–—:|]\s+")  # '2026-460 - ', 'REQ123 | '
_REF_TAIL = re.compile(r"\s*[(\[]\s*(?:r[ée]f\.?\s*:?\s*)?(?=[\w-]*\d{3})[A-Z0-9][\w-]{3,}\s*[)\]]\s*$")  # ' (REQ2026082281)'


def clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    t = _GENDER.sub(" ", t)
    t = _INCLUSIVE.sub("", t)
    t = _REF_TAIL.sub("", _REF_LEAD.sub("", t))
    return re.sub(r"\s+", " ", t).strip(" -–—|:,;/") or str(title or "").strip()


# ---- 2. language
_FR_W = {"de", "des", "du", "la", "le", "les", "pour", "et", "en", "au", "aux", "sur", "dans", "ingenieur", "chercheur",
         "responsable", "chef", "charge", "developpeur", "stage", "these", "alternance", "donnees", "apprentissage", "traitement"}
_EN_W = {"of", "for", "and", "the", "in", "on", "with", "engineer", "scientist", "researcher", "developer", "manager", "lead",
         "head", "intern", "senior", "junior", "learning", "data", "processing"}


def title_language(title: str) -> str:
    words = _clean(title).split()
    fr = sum(w in _FR_W for w in words) + 2 * len(re.findall(r"[éèêàçùâîô]", (title or "").lower()))
    en = sum(w in _EN_W for w in words)
    return "fr" if fr > en else "en" if en > fr else "?"


# ---- 3. research topics: ONE bilingual vocabulary (accent-free, lower case)
TOPICS = {
    "machine learning": ["machine learning", "apprentissage automatique", "apprentissage statistique", "statistical learning",
                         "ml", "reinforcement learning", "apprentissage par renforcement", "federated learning",
                         "apprentissage federe", "active learning", "transfer learning"],
    "deep learning": ["deep learning", "apprentissage profond", "neural network", "reseau de neurones", "reseaux de neurones",
                      "neuronal", "neuronaux", "transformer", "cnn", "gnn", "graph neural", "diffusion model",
                      "modele de diffusion", "representation learning", "self supervised", "auto supervise"],
    "generative ai / llm": ["llm", "large language model", "grand modele de langage", "generative ai", "genai",
                            "ia generative", "foundation model", "modele de fondation", "rag", "agentic", "ai agent",
                            "agent ia", "agents ia", "prompt engineering"],
    "artificial intelligence": ["artificial intelligence", "intelligence artificielle", "ai", "ia"],
    "computer vision": ["computer vision", "vision par ordinateur", "vision artificielle", "image processing",
                        "traitement d'image", "traitement d'images", "traitement de l'image", "traitement des images",
                        "analyse d'image", "analyse d'images", "image analysis", "imaging", "imagerie", "object detection",
                        "detection d'objets", "segmentation", "3d reconstruction", "reconstruction 3d", "point cloud",
                        "nuage de points", "nuages de points", "lidar", "video analytics", "analyse video", "pose estimation",
                        "slam", "optical flow", "remote sensing", "teledetection", "medical imaging", "imagerie medicale",
                        "ocr", "photogrammetrie", "photogrammetry", "stereo vision", "vision 3d", "3d vision",
                        "image reconstruction", "reconstruction d'image", "reconstruction d'images", "computational imaging",
                        "imagerie computationnelle", "microscopy", "microscopie", "super resolution", "super-resolution"],
    "audio & speech": ["speech", "parole", "audio", "acoustic", "acoustique", "speech recognition", "reconnaissance vocale",
                       "reconnaissance de la parole", "asr", "text to speech", "tts", "synthese vocale", "speaker",
                       "locuteur", "voice", "voix", "music", "musique", "sound", "spatial audio"],
    "signal processing": ["signal processing", "traitement du signal", "traitement de signal", "traitement des signaux",
                          "dsp", "radar", "sonar", "time series", "series temporelles", "serie temporelle",
                          "compressed sensing", "inverse problem", "probleme inverse", "problemes inverses",
                          "sparse representation", "filtrage", "kalman", "array processing", "antenna processing"],
    "nlp": ["nlp", "natural language", "langage naturel", "traitement automatique des langues", "traitement automatique du langage",
            "tal", "text mining", "fouille de texte", "information extraction", "extraction d'information",
            "traduction automatique", "machine translation", "chatbot", "conversational ai", "ia conversationnelle"],
    "robotics": ["robotics", "robotique", "robot", "autonomous", "autonome", "autonomes", "drone", "drones", "uav",
                 "motion planning", "planification de trajectoire", "manipulation robotique", "cobot", "mechatronics",
                 "mecatronique", "perception", "self driving", "vehicule autonome", "adas"],
    "data science": ["data science", "data scientist", "science des donnees", "data mining", "fouille de donnees",
                     "predictive", "predictif", "predictive maintenance", "maintenance predictive", "forecasting",
                     "recommendation system", "systeme de recommandation", "anomaly detection", "detection d'anomalies",
                     "detection d'anomalie", "bayesian", "bayesien", "bayesiens", "bayesienne", "bayesiennes", "causal inference", "inference causale",
                     "operations research", "recherche operationnelle", "statistical modeling", "modelisation statistique"],
    "mlops / deployment": ["mlops", "ml ops", "model deployment", "deploiement de modeles", "edge ai", "embedded ai",
                           "ia embarquee", "model serving", "tinyml", "quantization"],
}
_TOPIC_TERMS = [(t, _clean(x)) for t, xs in TOPICS.items() for x in xs]


def title_topics(text: str) -> list[str]:
    t = _clean(text)
    return list(dict.fromkeys(topic for topic, x in _TOPIC_TERMS if _kw(x, t)))


def expand_keywords(keywords) -> list[str]:
    """A profile keyword that belongs to a topic brings that topic's other spellings ('computer vision' also finds
    'vision par ordinateur'). Generic words (ai / ia) are not expanded."""
    out = [_clean(k) for k in keywords if _clean(k)]
    for k in list(out):
        for topic, xs in TOPICS.items():
            if len(k) > 3 and any(_clean(x) == k for x in xs):
                out += [_clean(x) for x in xs if len(_clean(x)) > 3]
    return list(dict.fromkeys(out))


# ---- 4. roles and contracts
ROLE_FAMILIES = {  # first match wins: specific multi-word roles first
    "research": ["research engineer", "research scientist", "ingenieur de recherche", "ingenieure de recherche",
                 "ingenieur recherche", "enseignant chercheur", "research fellow", "chercheur", "chercheuse", "researcher",
                 "scientist", "scientifique", "applied scientist", "r&d engineer", "ingenieur r&d", "ingenieur r d"],
    "engineering": ["engineer", "ingenieur", "ingenieure", "developer", "developpeur", "developpeuse", "dev", "programmer",
                    "architect", "architecte", "data engineer", "devops", "sre", "technical lead", "tech lead",
                    "lead developer", "technician", "technicien", "technicienne", "expert", "experte", "specialist",
                    "specialiste", "analyst", "analyste", "consultant", "consultante", "integrateur", "integrator"],
    "management": ["manager", "responsable", "chef de projet", "project manager", "program manager", "product owner",
                   "product manager", "head of", "director", "directeur", "directrice", "vp", "cto", "team lead"],
}
_ROLE_TERMS = [(fam, _clean(x)) for fam, xs in ROLE_FAMILIES.items() for x in xs]
_PHD = [re.compile(p) for p in (r"\bpost ?doc\w*", r"\bpost doctora\w*", r"\b(doctoral|doctorants?|doctorante|doctorat|doctorate|cifre)\b",
                                r"\bdoctor of philosophy\b", r"\b(thesis|these|theses)\b")]
_APPRENTICE = re.compile(r"\b(apprentices?|apprentis?|apprentie|apprenties|apprenticeships?|alternance|alternants?|alternante|"
                         r"work study|contrat de professionnalisation|contrat pro|duale?s? studium)\b")
_INTERN = [re.compile(p) for p in (
    r"\b(interns?|internships?|stagiaires?|stages?|trainees?|traineeship|praktikum|praktikant\w*|tirocinio)\b",
    r"\b(co ?op|working students?|werkstudent\w*|students?|etudiants?|etudiante|pfe|graduate program|fin d'etudes|"
    r"master thesis|master project|projet de master|m2 project|stage m2)\b")]
OFFER_TYPES = {"job": "Jobs (CDI / CDD)", "internship": "Internships (stage)", "apprenticeship": "Apprenticeships (alternance)",
               "phd_postdoc": "PhD / post-doc"}


def wanted_types(profile) -> set:
    """Kinds of offer the user looks for (older profiles: jobs, plus PhD / post-doc when that switch was on)."""
    if profile.get("offer_types"):
        return set(profile["offer_types"])
    return {"job"} | ({"phd_postdoc"} if profile.get("include_phd_postdoc") else set())
_CONTRACTS = (("cdi", r"\bcdi\b|permanent|unbefristet"), ("cdd", r"\bcdd\b|fixed term|temporary|befristet"),
              ("freelance", r"freelance|independant|contractor"), ("interim", r"\binterim\b"),
              ("alternance", r"alternance|apprenti|work study"), ("internship", r"\bstage\b|intern|stagiaire"),
              ("phd", r"\bthese\b|\bthesis\b|\bph ?d\b|doctora"), ("postdoc", r"post ?doc"), ("vie", r"\bv i e\b"))
_NON_RESEARCH = ("sales", "pre sales", "presales", "account manager", "account executive", "business development",
                 "business developer", "ingenieur d'affaires", "ingenieur commercial", "technico commercial", "recruiter",
                 "recruteur", "talent acquisition", "charge de recrutement", "accountant", "comptable", "assistant administratif",
                 "assistante administrative", "assistant de direction", "assistante de direction", "office manager",
                 "community manager", "chauffeur", "driver", "vendeur", "vendeuse", "magasinier", "sales advisor")
GENERIC_TECH = ("engineer", "ingenieur", "scientist", "scientifique", "chercheur", "researcher", "research",
                "recherche", "r&d", "data", "developer", "developpeur", "analyst", "architect", "model", "models",
                "modele", "modeles", "deploy", "deploying", "deployment", "deploiement", "inference", "pipeline",
                "algorithm", "algorithme", "mlops", "modelisation", "analytics", "python", "engineering", "ingenierie",
                "enhancement", "transformer", "simulation", "numerique", "computational", "calcul")

# ---- 5. research-topic titles: a subject stated as a problem rather than a job name
_SUBJECT_LEAD = re.compile(r"^(sujet|subject|topic|projet de recherche|research project|project|projet|offre de these|"
                           r"proposition de these|these|thesis|phd|ph d|post ?doc\w*|postdoctoral\w*|post doctora\w*|"
                           r"contrat doctoral|cdd de recherche|research position|chaire|chair)\b")
_CONNECTOR = re.compile(r"(?<![a-z])(for|pour|towards|toward|vers|using|via|based on|basee? sur|a base de|par|with|avec|"
                        r"applied to|appliquee?s? (?:a|au|aux)|application (?:of|de|a|aux?)|dans le cadre|in the context of|"
                        r"from|a partir d[eu']?)(?![a-z])")  # 'de' / 'of' alone are not enough: 'Conseiller de vente'
_PROBLEM = ("detection", "estimation", "reconstruction", "segmentation", "classification", "recognition", "reconnaissance",
            "prediction", "prevision", "modelisation", "modeling", "modelling", "simulation", "optimisation", "optimization",
            "analyse", "analysis", "apprentissage", "learning", "generation", "synthese", "synthesis", "tracking", "suivi",
            "localisation", "localization", "compression", "denoising", "debruitage", "restauration", "restoration",
            "fusion", "calibration", "inference", "explicabilite", "explainability", "robustesse", "robustness",
            "interpretabilite", "interpretability", "uncertainty", "incertitude", "methodes", "methods", "approche",
            "approaches", "approach", "algorithmes", "algorithms", "modeles", "models", "representation", "identification",
            "caracterisation", "characterization", "quantification", "diagnostic", "controle", "control")
_RESEARCH_MARK = re.compile(r"\b(research|recherche|r&d|r d|laborator\w*|labo|lab|scientifi\w*|chercheu\w+|innovation|phd|"
                            r"these|doctor\w*|post ?doc\w*|academic|academique|cnrs|inria|cea|onera|universit\w*)\b")


@dataclass
class TitleInfo:
    original: str
    clean: str
    lang: str
    kind: str                 # job | research_topic
    role: str = ""            # matched role words ('ingenieur de recherche')
    role_family: str = ""     # research | engineering | management | ''
    subject: str = ""         # research subject stated in the title (after 'Thèse :' / 'Ingénieur – ...')
    topics: list = field(default_factory=list)
    contract: str = ""        # cdi | cdd | freelance | internship | alternance | phd | postdoc | vie | ''
    training: str = ""        # internship | apprenticeship | phd_postdoc | '' (= a regular job, or not stated)
    verdict: str = "maybe"    # pass | maybe | reject
    reason: str = ""

    def as_dict(self):
        return {k: getattr(self, k) for k in ("clean", "lang", "kind", "role", "role_family", "subject", "topics",
                                              "contract", "training", "verdict", "reason")}


def _training(t, employment_type):
    et = _clean(employment_type)
    if _APPRENTICE.search(et):
        return "apprenticeship", f"employment type '{employment_type}'"
    if re.search(r"\b(interns?|internships?|stage|stagiaire|trainee|co ?op)\b", et):
        return "internship", f"employment type '{employment_type}'"
    if re.search(r"\b(doctor\w*|phd|post ?doc\w*)\b", et):
        return "phd_postdoc", f"employment type '{employment_type}'"
    for p in _PHD:
        m = p.search(t)
        if m:
            return "phd_postdoc", f"'{m.group(0)}'"
    if re.search(r"\bph ?d\b", t) and not re.search(r"\b(engineer|ingenieur|scientist|scientifique|researcher)\b", t):
        return "phd_postdoc", "'PhD' position"  # 'Research Scientist (PhD)': there PhD is a requirement
    m = _APPRENTICE.search(t)
    if m:
        return "apprenticeship", f"'{m.group(0)}'"
    for p in _INTERN:
        m = p.search(t)
        if m:
            return "internship", f"'{m.group(0)}'"
    return "", ""


def training_role(title, employment_type=""):
    """Internship / apprenticeship / PhD / post-doc (English + French) -> reason, or None."""
    return _training(_clean(clean_title(title)), employment_type)[1] or None


def analyze_title(title, dept="", profile=None, employment_type="") -> TitleInfo:
    from .config import PROFILE_DEFAULT
    profile = profile or PROFILE_DEFAULT
    clean = clean_title(title)
    t, d = _clean(clean), _clean(dept or "")
    info = TitleInfo(original=title or "", clean=clean, lang=title_language(clean), kind="job")
    for fam, x in _ROLE_TERMS:
        if _kw(x, t):
            info.role, info.role_family = x, fam
            break
    info.contract = next((name for name, rx in _CONTRACTS if re.search(rx, t + " " + _clean(employment_type))), "")
    # shape: 'Thèse : <subject>' | '<role> – <subject>' | a subject with no role at all
    parts = re.split(r"\s+[-–—:|]\s+|\s*:\s+", clean, maxsplit=1)
    lead = _SUBJECT_LEAD.match(t)
    words = t.split()
    if lead:
        info.kind, info.subject = "research_topic", (parts[1] if len(parts) > 1 else clean[lead.end():]).strip(" :-–")
    elif not info.role and len(words) >= 4 and (any(w in _PROBLEM for w in words) or title_topics(clean)
                                                or (_CONNECTOR.search(t) and _RESEARCH_MARK.search(t))):
        info.kind, info.subject = "research_topic", clean
    elif info.role and len(parts) > 1 and len(_clean(parts[1]).split()) >= 3 and (
            _CONNECTOR.search(_clean(parts[1])) or any(w in _PROBLEM for w in _clean(parts[1]).split())):
        info.subject = parts[1].strip()
    info.topics = title_topics(clean)

    def done(verdict, reason):
        info.verdict, info.reason = verdict, reason
        return info

    info.training, why = _training(t, employment_type)
    want = wanted_types(profile)
    if info.training and info.training not in want:
        return done("reject", f"{OFFER_TYPES[info.training].split(' (')[0].lower()} not wanted ({why})")
    if not info.training and "job" not in want and info.contract in ("cdi", "cdd", "freelance", "interim"):
        return done("reject", f"{info.contract.upper()} job: you look for {', '.join(sorted(want))}")
    info = _topic_verdict(info, t, d, profile)
    if info.verdict == "pass" and not info.training and "job" not in want:
        info.verdict, info.reason = "maybe", info.reason + " - contract not stated in the title: checked by the AI"
    return info


def _topic_verdict(info, t, d, profile):
    def done(verdict, reason):
        info.verdict, info.reason = verdict, reason
        return info

    for x in _NON_RESEARCH:
        if _kw(_clean(x), t):
            return done("reject", f"sales / business / admin role ('{x}')")
    for x in profile.get("exclude_titles", []):
        if _kw(_clean(x), t):
            return done("reject", f"excluded term '{x}'")
    hit = [k for k in expand_keywords(profile.get("keywords", [])) if _kw(k, t)]
    if hit and info.role_family != "management":
        return done("pass", f"title matches {hit[:3]}")
    if info.topics:
        if info.role_family in ("research", "engineering") or info.kind == "research_topic":
            return done("pass", f"{'research subject' if info.kind == 'research_topic' else 'title'} on {', '.join(info.topics[:3])}")
        return done("maybe", f"topic {', '.join(info.topics[:3])} with a {info.role_family or 'non-technical'} role: AI decides")
    if hit:
        return done("maybe", f"title matches {hit[:3]} but it is a management role: AI decides")
    if info.kind == "research_topic":
        return done("maybe", "research subject title: judged from the description")
    if _RESEARCH_MARK.search(t) or _RESEARCH_MARK.search(d):
        return done("maybe", "research / R&D position: judged from the description")
    dh = [k for k in expand_keywords(profile.get("keywords", [])) if _kw(k, d)]
    if dh:
        return done("maybe", f"department matches {dh[:2]}")
    if any(_kw(g, t) for g in GENERIC_TECH):
        return done("maybe", "technical title: judged from the description")
    return done("reject", "no research / technical term in the title")


def title_filter(title, dept, profile, employment_type=""):
    """Fast deterministic stage. -> ('pass'|'maybe'|'reject', reason)"""
    i = analyze_title(title, dept, profile, employment_type)
    return i.verdict, i.reason
