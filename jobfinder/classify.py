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
from difflib import SequenceMatcher
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

from .web import norm


_UI_WORDS = re.compile(r"\s+(?:t[ée]l[ée]charger|download|postuler|apply|partager|share|imprimer|print|voir|see|plus d|"
                       r"en savoir|itin[ée]raire|map|carte)\b.*$", re.I)
_STREET = re.compile(r"\b\d{1,4}\s*(?:bis|ter)?,?\s+(?:rue|avenue|av\.|boulevard|bd|place|quai|all[ée]e|chemin|route|impasse|cours|"
                     r"square|esplanade|parvis)\b", re.I)
_POSTCODE_CITY = re.compile(r"\b(\d{5})\s+([A-ZÀ-Ý][\wÀ-ÿ'’-]*(?:[ -](?:sur|sous|en|le|la|les|de|du|d'|[A-ZÀ-Ý][\wÀ-ÿ'’-]*))*)")


def tidy_place(v) -> str:
    """'PSL 60 rue Mazarine 75006 Paris Télécharger en' -> '75006 Paris': no buttons, no street, the postcode and city."""
    v = _UI_WORDS.sub("", re.sub(r"\s+", " ", v or "")).strip(" ,;-")
    m = _POSTCODE_CITY.search(v) if _STREET.search(v) else None
    return re.sub(r"\s+cedex\b.*$", "", f"{m[1]} {m[2]}", flags=re.I) if m else v


def html_location(soup) -> str:
    """Location from microdata, ATS markup (data-careersite-propertyid, jobGeoLocation...) when JSON-LD is absent."""
    out = []
    add = lambda v: out.append(tidy_place(v)) if v and v.strip() and tidy_place(v) else None
    for t in soup.find_all(attrs={"itemprop": re.compile("^(streetAddress|addressLocality|addressRegion|addressCountry)$", re.I)}):
        add(t.get("content") or t.get_text(" ", strip=True))
    loc_class = re.compile(r"job-?location|jobgeolocation", re.I)  # one pass ('[class*=...]' selectors are slow)
    for t in soup.find_all(lambda tag: tag.get("data-careersite-propertyid") == "location"
                           or any(loc_class.search(c) for c in tag.get("class") or ())):
        add(t.get_text(" ", strip=True)[:120])
    seen, res = set(), []
    for v in out:
        if norm(v) not in seen:
            seen.add(norm(v))
            res.append(v)
    return " ; ".join(res)[:200]


# a label starts with a capital ('Lieu du stage :', 'Ville :'): 'mobility location: Evidence from Kunming' in a
# bibliography is a sentence, not a label
_LOC_LABEL = re.compile(r"(?:\b(?:Localisation|LOCALISATION|Lieu|LIEU|Location|LOCATION|Ville|VILLE|City|CITY|Site|SITE|"
                        r"Implantation|Affectation)(?i:\s+(?:de travail|du stage|de stage|de la th[eè]se|du poste|"
                        r"d'affectation|d'exercice))?\s*:|"
                        r"(?i:localisation du poste|lieu du poste|lieu de travail|job locations?|work locations?)\s*[:|]?)"
                        r"\s*[•\-–]?\s*([^\n|;•]{2,120})")
# a search form's prompt after its label ('Localisation ▼ Veuillez sélectionner une valeur'), not a place
_FORM_PROMPT = re.compile(r"(?i)^\W*(?:veuillez|s[ée]lectionne|select|choisi|choose|tou(?:te)?s?\b|all\b|any\b|rechercher|search)")
# a sentence after the words of a label ('un lieu de travail où équité F/H, inclusion et diversité sont la norme'), not a place
_SENTENCE = re.compile(r"(?i)^\W*(?:où|qui|que|dont|et|dans|pour|avec|o[uù] |where|which|that|and|with|for|is|are)\b")
# where a label's value ends: two spaces, a separator, the next label ('Contrat :'), 'Ville'
_LABEL_END = re.compile(r"\s{2,}|\s*[|;•\n]|\s+[A-Z][a-zé]+(?:\s+[A-Za-zé]+)?\s*:|\s+Ville\b")


def _label_value(v):
    m = _LABEL_END.search(v, 2)
    return (v[:m.start()] if m else v)[:80].strip(" .-,")
_BOILER = re.compile(r"^(error|erreur|404|page not found|page introuvable|not found|mentions legales|politique (?:des|de) "
                     r"(?:cookies|confidentialite)|cookies?|privacy|privacy policy|confidentialite|cgu|conditions generales|"
                     r"contact|contactez-nous|accessibilite|accessibility|plan du site|sitemap|faq|login|log in|sign in|connexion|"
                     r"se connecter|accueil|home|candidatures? spontanees?|spontaneous applications?|gestion des cookies|donnees personnelles|"
                     r"protection des donnees|data protection|legal notice|terms of use|cookie policy|cookie settings)\b")


def is_boilerplate_title(title) -> bool:
    """'Mentions légales', 'Error', 'Politique des cookies'...: a site page, not an offer. '<<INTERNAL JOB DESCRIPTION>>':
    a template's placeholder, not a title."""
    t = norm(title or "").strip(" -:|")
    return bool(_BOILER.match(t)) or not re.search(r"[a-z]{2}", t) or bool(re.search(r"<<|>>|\{\{|\}\}", t))


_DATE_LIKE = re.compile(r"(?i)^\W*(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|"
                        r"friday|saturday|sunday)\b|\d{1,2}(?:er)?\s+(?:janv|f[ée]vr|mars|avr|mai|juin|juil|ao[uû]t|sept?|oct|"
                        r"nov|d[ée]c|jan|feb|apr|may|jun|jul|aug|dec)\w*\.?,?\s+\d{4}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\d{4}-\d{2}-\d{2})")


def looks_like_date(text) -> bool:
    """'Jeudi, 5 novembre, 2026 - Vendredi...' written in a location field: a date, not a place."""
    return bool(_DATE_LIKE.search(text or ""))


_KINDS = {"internship": r"internships?|stages?|stagiaires?|interns?", "phd": r"ph\s?ds?|th[eè]ses?|thesis|doctora\w*",
          "postdoc": r"post\s?docs?|postdoctora\w*", "apprentice": r"apprentice\w*|apprenti\w*|alternances?|vie"}
_KIND_FILLER = set("""open opened available current new our nos les des de du la le et and or ou for pour of in en a au aux the
position positions poste postes job jobs emploi emplois offer offers offre offres vacancy vacancies opportunity
opportunities opening openings sujet sujets proposal proposals proposition propositions recrutement recruitment master m1 m2
research recherche call appel candidature candidatures application applications""".split())


def offer_kinds_heading(title) -> bool:
    """'Postdoc, PhD or Master 2', 'Apprenticeships, internships, thesis and VIE', 'Open positions for 2026
    internships/PhDs': a page about offers, not one offer: only kinds of offers, several of them or in the plural, and no
    subject. 'PhD position' or 'Stage M2' alone may be one offer and stay."""
    t = norm(title or "")
    words = re.findall(r"[a-z0-9]+", t.replace("ph.d", "phd").replace("post-doc", "postdoc"))
    kind_rx = re.compile(r"^(?:" + "|".join(_KINDS.values()) + r")$")
    if not words or any(not (kind_rx.match(w) or w in _KIND_FILLER or w.isdigit()) for w in words):
        return False
    kinds = {k for k, rx in _KINDS.items() if any(re.fullmatch(rx, w) for w in words)}
    plural = any(w in {"positions", "offers", "offres", "postes", "jobs", "emplois", "vacancies", "opportunities", "openings",
                       "internships", "stages", "theses", "phds", "postdocs", "sujets", "propositions"} for w in words)
    return len(kinds) >= 2 or (plural and bool(kinds))


_PLACE = r"[A-ZÀ-Ý][\wÀ-ÿ'’-]*(?:[ -](?:sur|sous|en|le|la|les|de|du|des|d'|l'|lès|lez|aux|[A-ZÀ-Ý][\wÀ-ÿ'’-]*)){0,5}"
# the place of an offer written without a label: an icon name kept as text ('location_on Lyon'), 'notre agence de Lille',
# 'poste basé à Toulouse', 'based in Paris', 'Centre Toulouse', 'une équipe sur Bordeaux', or the place written after the
# title's gender mark ('(H/F) à Lyon', 'F/H Paris'). Every candidate must be a place of the official list (or a known
# foreign one): 'sur Python', 'à Data' name nothing.
_LOC_CUE = re.compile(r"(?:\b(?:location_on|pin_drop)\s+|"
                      r"(?i:\b(?:agence|bureaux?|locaux|si[eè]ge|campus|implantation|centre de recherche)s?\s+(?:de|d'|du|des|à|a)\s*)|"
                      r"(?i:\b(?:bas[ée]e?s?|situ[ée]e?s?|localis[ée]e?s?|implant[ée]e?s?)\s+(?:à|a|au|sur|dans)\s+(?:la\s+r[ée]gion\s+(?:de\s+)?)?)|"
                      r"(?i:\b(?:based|located|location)\s+(?:in|at)\s+)|"
                      r"(?i:\bcentre\s+(?:de\s+|d')?)|(?<![\w'])(?:à|au|sur)\s+|"
                      r"\b[HFMW]\s*/\s*[HFMWXD](?:\s*/\s*[HFMWXDN])?\s*\)?\s*(?:[-–—|,]\s*|(?:à|a)\s+)?)"
                      r"(" + _PLACE + ")")
# a French postcode before its town ('Campus Pasteur 28 avenue Valombrose 06107 Nice'), and the country, region and town of
# a breadcrumb ('CDI / FRANCE Occitanie / Montpellier')
_POSTCODE_PLACE = re.compile(r"\b(?:F-)?(?:0[1-9]|[1-8]\d|9[0-5]|97)\d{3}\s+(" + _PLACE + ")")
_FR_TRAIL = re.compile(r"\b(?:FRANCE|France)\s*[/>|,–-]?\s*(" + _PLACE + r")(?:\s*[/>|,–-]\s*(" + _PLACE + "))?")
# a French street address with its postcode ('11 boulevard d'Alembert, 78280')
_FR_STREET = re.compile(r"(?i)\b(?:rue|avenue|boulevard|bd|all[ée]e|chemin|impasse|quai|place|route|cours|square|esplanade|"
                        r"parvis)\b[^,;\n]{1,60}[,\s]\s*(?:F-)?(?:0[1-9]|[1-8]\d|9[0-5]|97)\d{3}\b")


def location_from_text(text, title="") -> str:
    """'Localisation : Rueil-Malmaison (92)' / 'Localisation du poste France, Occitanie, Gard (30)' written in a job
    text. A search form can also say 'Localisation ...': a value that names a real place is preferred. Without a label,
    a place written after a cue ('location_on Lyon', 'notre agence de Lille', '(H/F) à Lyon') counts only when it is a real
    French place of the official list (or a known foreign place), in the first lines of the text (not in 'other offers')."""
    t = str(text or "")[:8000]
    found = [v for v in (_label_value(m.group(1)) for m in _LOC_LABEL.finditer(t))
             if not _FORM_PROMPT.search(v) and not _SENTENCE.search(v)]
    verified = next((x for x in found if classify_location(x)["location_verified"]), "")
    if verified:
        return verified
    head = t[:3000]
    for m in _LOC_CUE.finditer(head):
        place = fr_place(m.group(1))
        if place:
            return place
        if classify_location(m.group(1))["location_verified"]:  # 'based in London', 'location in France Dans un...'
            return "France" if _FR_MARK_RX.match(norm(m.group(1))) else m.group(1).strip(" .-,")
    for rx in (_POSTCODE_PLACE, _FR_TRAIL):  # '06107 Nice', 'CDI / FRANCE Occitanie / Montpellier'
        for m in rx.finditer(head):
            place = fr_place(" ".join(x for x in m.groups() if x))
            if place:
                return place
    for m in _LOC_CUE.finditer(title or ""):  # 'Formateur à Saint Avertin (Tours) : animation d'un atelier'
        place = fr_place(m.group(1))
        if place:
            return place
    words = re.findall(r"\w+", clean_title(title)) if title else []
    if words:  # the offer's title followed by its town at the top of its page ('SOFTWARE CRAFTSMAN Paris Il y a 7 jours')
        after = re.compile(r"\W+".join(map(re.escape, words)) + r"\W{0,4}(?:[HFMW]\s*/\s*[HFMWXD](?:\s*/\s*\w{1,2})?\W{0,3})?(" +
                           _PLACE + ")", re.I)
        for m in after.finditer(t[:1500]):
            place = fr_place(m.group(1))
            if place:
                return place
    return next((fr_place(x) for x in found if fr_place(x)), "") or (found[0] if found else "")


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
             "centre-val de loire", "bourgogne-franche-comte",
             # research and higher-education sites written as the place of an offer ('Dauphine - PSL', 'AgroParisTech')
             "psl", "dauphine", "agroparistech", "sorbonne", "jussieu", "centralesupelec", "ensae", "ensta", "upsaclay",
             "sciences po", "mines paristech", "telecom sudparis", "telecom paristech", "institut pasteur", "institut curie",
             "ens ulm", "ens paris-saclay", "universite paris cite", "universite paris-saclay",
             "ecole nationale des ponts et chaussees", "ecole des ponts", "enpc",
             # French national research institutes written as the place ('INRAE', 'CNRS'): their sites abroad are named
             # with their country ('Inria Chile'), which is checked first
             "cnrs", "inria", "inrae", "inserm", "onera", "cea", "cnes", "ifremer", "ird", "brgm", "ineris", "ifpen",
             "roissy", "roissy charles de gaulle", "aeroport charles de gaulle", "paris cdg", "paris-charles de gaulle"]
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
# every other country (English and French names, a few local ones), states and large cities: 'Riga, Latvia', 'Montevideo,
# Uruguay', 'Pristina, Kosovo' are outside France, not 'unknown'. ('maurice' is left out: 'Saint-Maurice' is in France.)
OTHER_PLACES += [w.strip() for w in """afghanistan, albania, andorra, angola, armenia, azerbaijan, bahrain, bangladesh, belarus, bhutan, bolivia,
bosnia, botswana, brunei, burkina faso, burundi, cambodia, cameroon, cape verde, chad, comoros, congo,
costa rica, cuba, cyprus, czechia, djibouti, dominican republic, ecuador, el salvador, estonia, eswatini,
ethiopia, fiji, gabon, gambia, georgia, ghana, guatemala, guinea, guyana, haiti, honduras, hong kong, iceland,
iran, iraq, ivory coast, jamaica, jordan, kazakhstan, kenya, kosovo, kuwait, kyrgyzstan, laos, latvia, lebanon,
liberia, libya, liechtenstein, lithuania, macau, madagascar, malawi, maldives, mali, malta, mauritania,
mauritius, moldova, monaco, mongolia, montenegro, mozambique, myanmar, namibia, nepal, new zealand, nicaragua,
niger, nigeria, north macedonia, macedonia, oman, pakistan, panama, papua, paraguay, puerto rico, russia, rwanda,
senegal, sierra leone, slovenia, somalia, south korea, sri lanka, sudan, suriname, syria, taiwan, tajikistan,
tanzania, togo, trinidad, turkiye, turkmenistan, uganda, united arab emirates, uruguay, uzbekistan, venezuela,
viet nam, yemen, zambia, zimbabwe, northern ireland, albanie, andorre, arabie saoudite, argentine, armenie,
australie, azerbaidjan, bahrein, bielorussie, birmanie, bolivie, bosnie, cambodge, cameroun, cap vert, chili,
chypre, colombie, comores, coree, cote d'ivoire, croatie, egypte, emirats arabes, equateur, estonie, ethiopie,
finlande, georgie, grece, guinee, hongrie, indonesie, irak, islande, israel, jamaique, jordanie, koweit,
lettonie, liban, libye, lituanie, malaisie, malte, ile maurice, mauritanie, moldavie, mongolie, nouvelle-zelande,
ouganda, ouzbekistan, perou, republique tcheque, tchequie, russie, serbie, slovaquie, slovenie, somalie, soudan,
syrie, tanzanie, tchad, thailande, ukraine, polska, espana, italia, nederland, belgie, schweiz, osterreich,
sverige, norge, danmark, suomi, magyarorszag, ceska, slovensko, hellas, california, massachusetts, new jersey,
pennsylvania, illinois, michigan, ohio, virginia, north carolina, florida, colorado, oregon, arizona, utah,
minnesota, maryland, connecticut, wisconsin, indiana, tennessee, missouri, ontario, quebec, british columbia,
alberta, karnataka, maharashtra, telangana, tamil nadu, haryana, uttar pradesh, kerala, gujarat, guangdong,
bavaria, bayern, new orleans, washington, vancouver, ottawa, calgary, waterloo, boston, chicago, austin, denver,
atlanta, dallas, houston, miami, los angeles, san diego, san jose, santa clara, sunnyvale, mountain view,
palo alto, redmond, cupertino, menlo park, pittsburgh, philadelphia, detroit, phoenix, raleigh, minneapolis,
baltimore, frankfurt, francfort, hamburg, hambourg, koln, stuttgart, dusseldorf, dresden, leipzig, hannover,
nuremberg, nurnberg, bremen, karlsruhe, aachen, heidelberg, mannheim, bonn, essen, dortmund, rotterdam,
eindhoven, utrecht, the hague, delft, antwerp, anvers, ghent, gand, liege, namur, charleroi, leuven, louvain,
basel, bale, bern, lugano, luzern, zug, winterthur, graz, linz, salzburg, innsbruck, milano, rome, roma, turin,
torino, naples, napoli, bologna, genoa, venice, venezia, padova, pisa, trieste, seville, sevilla, valencia,
malaga, bilbao, zaragoza, edinburgh, glasgow, manchester, birmingham, bristol, leeds, oxford, cambridge, belfast,
cardiff, riga, tallinn, vilnius, kyiv, kiev, minsk, belgrade, zagreb, ljubljana, bratislava, sofia, athens,
athenes, nicosia, valletta, reykjavik, oslo, copenhagen, copenhague, gothenburg, malmo, aarhus, tampere, espoo,
krakow, cracovie, wroclaw, gdansk, poznan, lodz, brno, ostrava, cluj, timisoara, iasi, braga, coimbra, haifa,
jerusalem, herzliya, riyadh, jeddah, doha, abu dhabi, manama, muscat, amman, beirut, ankara, izmir, osaka, kyoto,
yokohama, busan, shenzhen, guangzhou, hangzhou, chengdu, wuhan, nanjing, suzhou, taipei, hsinchu, melbourne,
brisbane, adelaide, auckland, wellington, mumbai, delhi, new delhi, noida, gurgaon, gurugram, kolkata, ahmedabad,
kochi, coimbatore, karachi, lahore, dhaka, colombo, kathmandu, cebu, kuala lumpur, penang, jakarta, ho chi minh,
hanoi, bangkok, marrakech, tanger, sfax, sousse, alger, algiers, oran, dakar, abidjan, lagos, nairobi, accra,
kigali, johannesburg, cape town, cairo, le caire, montevideo, buenos aires, sao paulo, rio de janeiro, santiago,
bogota, lima, mexico city, londres, barcelone, edimbourg, varsovie, moscou, pekin, bombay, nouvelle-delhi, florence, bellevue, erlangen, karlstein, karlstein am main, offenbach, lingen,
berne, venise""".split(",")]
_FR_DEPT = re.compile(r"\((?:0[1-9]|[1-8]\d|9[0-5]|2a|2b|97[1-6])\)")  # 'Rueil-Malmaison (92)': French department number


def _words_rx(words):
    """One compiled pattern for a list of places (whole words): hundreds of separate searches would overflow re's cache."""
    alts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


_FR_MARK_RX, _OTHER_RX, _FR_RX = _words_rx(FR_MARKERS + ["fr", "fra"]), _words_rx(OTHER_PLACES), _words_rx(FR_PLACES)
_PLACES_FILE = Path(__file__).with_name("data") / "fr_places.txt"


_ABBREV = {"lez": "les", "st": "saint", "ste": "sainte"}


def _place_key(s: str) -> str:
    """'St-Paul-lez-Durance' = 'saint paul les durance' = the official 'Saint-Paul-lès-Durance'."""
    k = re.sub(r"\s+", " ", re.sub(r"[-_.]", " ", norm(s))).strip()
    return re.sub(r"(?<![a-z0-9'])(lez|st|ste)(?![a-z0-9'])", lambda m: _ABBREV[m[1]], k)


@lru_cache(maxsize=1)
def _fr_gazetteer():
    """{key: official name} of the French communes, departments and regions (data/fr_places.txt), and the names of several
    words by their first word (for the one-typo match)."""
    names = {}
    try:
        with _PLACES_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip() and not line.startswith("#"):
                    names.setdefault(_place_key(line), line.strip())
    except OSError:  # the list is missing: the hand-written places above still work
        pass
    by_first = {}
    for k in names:
        if " " in k:
            by_first.setdefault(k.split(" ", 1)[0], []).append(k)
    return names, by_first


_NO_PREFIX = {"vienne", "orange", "bologne", "bruges", "cologne", "montreal", "montreux", "treves", "venise", "fribourg",
              "martigny", "ardennes", "centre", "remote", "sainte", "grande", "petite", "chateau", "vallee", "plaine", "region",
              "campus", "france", "nouvelle", "saint martin", "saint pierre"}


@lru_cache(maxsize=8192)
def fr_place(text) -> str:
    """The official name of the French commune / department / region a short location text names ('' if none). Hyphens,
    accents and case do not matter ('Gif sur Yvette', 'SAINT-PAUL-LES-DURANCE'); one wrong letter is forgiven in a name of
    several words ('GIF SUY YVETTE'). The longest name wins: 'Saint-Paul-lès-Durance' rather than 'Saint-Paul'."""
    names, by_first = _fr_gazetteer()
    toks = re.findall(r"[a-z0-9']+", _place_key(text or ""))
    best = ""
    for i in range(len(toks)):
        for j in range(min(len(toks), i + 8), i, -1):
            cand = " ".join(toks[i:j])
            if cand in names:
                best = max(best, cand, key=len)
                break
    if best:
        return names[best]
    for i, t in enumerate(toks):
        for k in by_first.get(t, ()):
            n = k.count(" ") + 1
            cand = " ".join(toks[i:i + n])
            if len(cand) >= 10 and len(toks) - i >= n and SequenceMatcher(None, cand, k).ratio() >= 0.92:
                return names[k]
    # the start of a commune's name, written alone ('Montbonnot' for Montbonnot-Saint-Martin, 'Le Chesnay' for
    # Le Chesnay-Rocquencourt). Never a foreign town written in French, a generic word or a short word.
    key = " ".join(toks)
    if toks and len(toks) <= 3 and len(key.replace(" ", "")) >= 6 and key not in _NO_PREFIX and not _OTHER_RX.search(key):
        longer = [k for k in by_first.get(toks[0], ()) if k.startswith(key + " ")]
        if len(longer) == 1:
            return names[longer[0]]
        if longer:  # 'BONNEVILLE', 'Champagne': French, but which commune is not known: the text itself
            return " ".join(w.capitalize() for w in key.split())
    return ""


def _part_country(p: str):
    if _FR_MARK_RX.search(p):
        return "FR"
    place = fr_place(p)
    # a French name of several words is checked first: 'Porto-Vecchio' and 'Saint-Maurice' contain a foreign word
    rest = _place_key(p).replace(_place_key(place), " ") if place and " " in _place_key(place) else p
    if _OTHER_RX.search(rest) or _OTHER_RX.search(rest.replace(" ", "-")):
        return "OTHER"
    if _FR_STREET.search(p):
        return "FR"
    m = re.search(r",\s*([a-z]{2})\s*$", rest)  # "City, XX" with a foreign ISO country / state code
    if m and m[1] not in ("fr", "eu"):
        return "OTHER"
    if _FR_RX.search(p) or _FR_DEPT.search(p) or place:
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
    elif "OTHER" in kinds:  # 'United States ; New York ; 130 West 42nd Street': the street is no place, the country is
        r.update(location_verified=True, location_evidence=f"Listing location '{original}' is outside France")
    return r


# --------------------------------------------------------------------------- title analysis
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/.,:;()\[\]|–—·]", " ", norm(s))).strip()


def _kw(k: str, t: str) -> bool:  # whole-word match, tolerant to plurals
    return bool(k) and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?:s|es|x)?(?![a-z0-9])", t) is not None


# ---- 1. cleaning: what is not part of the title itself
_GENDER = re.compile(r"\s*[(\[]?\s*\b(?:[hfmwdx]\s*/\s*[hfmwdx](?:\s*/\s*(?:nb|[hfmwdxn]))?|homme\s*/\s*femme|femme\s*/\s*homme|"
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
                      "modele de diffusion", "representation learning", "self supervised", "auto supervise", "pytorch", "tensorflow",
                      "jax", "keras", "gan", "vae", "autoencoder", "auto encodeur", "vision transformer", "generative model",
                      "modele generatif", "nerf", "gaussian splatting"],
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
                        "imagerie computationnelle", "microscopy", "microscopie", "super resolution", "super-resolution",
                        "estimation de pose", "pose 6d", "debruitage", "denoising", "deblurring", "defloutage", "isp",
                        "image raw", "flux video", "video", "tracking", "suivi d'objets", "suivi d'objet", "depth estimation",
                        "estimation de profondeur", "hyperspectral", "multispectral", "reconnaissance d'images",
                        "image recognition", "camera", "cameras", "vision embarquee", "embedded vision"],
    "audio & speech": ["speech", "parole", "audio", "acoustic", "acoustique", "speech recognition", "reconnaissance vocale",
                       "reconnaissance de la parole", "asr", "text to speech", "tts", "synthese vocale", "speaker",
                       "locuteur", "voice", "voix", "music", "musique", "sound", "spatial audio"],
    "signal processing": ["signal processing", "traitement du signal", "traitement de signal", "traitement des signaux",
                          "dsp", "radar", "sonar", "time series", "series temporelles", "serie temporelle",
                          "compressed sensing", "inverse problem", "probleme inverse", "problemes inverses",
                          "sparse representation", "filtrage", "kalman", "array processing", "antenna processing", "beamforming",
                          "formation de voies", "antenne", "antennes", "antenna", "rf", "radiofrequence", "gnss", "gps",
                          "sensor fusion", "fusion de capteurs", "fusion de donnees", "navigation inertielle", "inertial",
                          "imu", "ultrasons", "ultrasound", "wavelet", "ondelettes", "sdr", "telecommunications", "5g", "6g"],
    "nlp": ["nlp", "natural language", "langage naturel", "traitement automatique des langues", "traitement automatique du langage",
            "tal", "text mining", "fouille de texte", "information extraction", "extraction d'information",
            "traduction automatique", "machine translation", "chatbot", "conversational ai", "ia conversationnelle"],
    "robotics": ["robotics", "robotique", "robot", "autonomous", "autonome", "autonomes", "drone", "drones", "uav",
                 "motion planning", "planification de trajectoire", "manipulation robotique", "cobot", "mechatronics",
                 "mecatronique", "perception", "self driving", "vehicule autonome", "adas", "ros", "ros2", "humanoid", "humanoide",
                 "legged", "manipulation", "navigation autonome", "autonomous navigation"],
    "data science": ["data science", "data scientist", "science des donnees", "data mining", "fouille de donnees",
                     "predictive", "predictif", "predictive maintenance", "maintenance predictive", "forecasting",
                     "recommendation system", "systeme de recommandation", "anomaly detection", "detection d'anomalies",
                     "detection d'anomalie", "bayesian", "bayesien", "bayesiens", "bayesienne", "bayesiennes", "causal inference", "inference causale",
                     "operations research", "recherche operationnelle", "statistical modeling", "modelisation statistique"],
    "mlops / deployment": ["mlops", "ml ops", "model deployment", "deploiement de modeles", "edge ai", "embedded ai",
                           "ia embarquee", "model serving", "tinyml", "quantization"],
    "embedded & hardware": ["fpga", "asic", "soc", "system on chip", "vhdl", "verilog", "systemverilog", "rtl", "uvm",
                            "dft", "design for testability", "microelectronique", "microelectronics", "electronique",
                            "electronics", "embarque", "embedded", "firmware", "microcontroleur", "microcontroller",
                            "linux embarque", "embedded linux", "rtos", "hardware", "semiconducteur", "semiconductor",
                            "circuits integres", "integrated circuit", "analogique", "analog", "mixed signal",
                            "photonique", "photonics", "verification d'ips", "ip digitale", "ips digitales"],
    "hpc & gpu": ["hpc", "high performance computing", "calcul haute performance", "gpu", "cuda", "parallel computing",
                  "calcul parallele", "calcul scientifique", "scientific computing", "simulation numerique",
                  "numerical simulation", "methodes numeriques", "numerical methods"],
    "data engineering": ["data engineer", "data engineering", "ingenieur data", "spark", "databricks", "hadoop", "kafka",
                         "airflow", "dbt", "etl", "big data", "snowflake", "data platform", "data pipeline", "datalake",
                         "data lake"],
    "software & cloud": ["kubernetes", "docker", "devops", "sre", "cloud", "aws", "azure", "gcp", "rust", "golang",
                         "temps reel", "real time", "backend", "full stack", "fullstack", "microservices", "logiciel embarque",
                         "embedded software"],
    "cybersecurity": ["cybersecurite", "cybersecurity", "cyber securite", "securite informatique", "pentest",
                      "cryptographie", "cryptography", "soc analyst", "analyste soc"],
    "control & estimation": ["automatique", "control theory", "theorie du controle", "commande", "loi de commande",
                             "controle moteur", "motor control", "observateur", "observer", "estimation d'etat",
                             "state estimation", "mpc", "model predictive control", "identification de systemes",
                             "system identification", "controle commande"],
}
# broad technical fields: never rejected, but read by the AI rather than passed on the title alone
TECH_FIELDS = {"embedded & hardware", "hpc & gpu", "data engineering", "software & cloud", "cybersecurity",
               "control & estimation"}
_TOPIC_TERMS = [(t, _clean(x)) for t, xs in TOPICS.items() for x in xs]


def title_topics(text: str) -> list[str]:
    t = _clean(text)
    return list(dict.fromkeys(topic for topic, x in _TOPIC_TERMS if _kw(x, t)))


def expand_keywords(keywords) -> list[str]:
    """A profile keyword that belongs to a topic brings that topic's other spellings ('computer vision' also finds
    'vision par ordinateur'). Generic words (ai / ia) are not expanded."""
    return list(_expand(tuple(keywords or ())))


@lru_cache(maxsize=32)
def _expand(keywords: tuple) -> tuple:  # the profile does not change during a search: computed once
    topics = [[_clean(x) for x in xs] for xs in TOPICS.values()]
    out = [_clean(k) for k in keywords if _clean(k)]
    for k in list(out):
        for xs in topics:
            if len(k) > 3 and k in xs:
                out += [x for x in xs if len(x) > 3]
    return tuple(dict.fromkeys(out))


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
                 "community manager", "chauffeur", "driver", "vendeur", "vendeuse", "magasinier", "sales advisor",
                 # store, finance, purchasing, HR, legal, reception and customer-service roles: full role names only, so that
                 # 'Data Scientist Marketing' or 'Ingénieur achats data' stay ('Hôte de caisse CQP en alternance' does not)
                 "hote de caisse", "hotesse de caisse", "caissier", "caissiere", "employe libre service", "employee libre service",
                 "employe de rayon", "employee de rayon", "employe commercial", "employee commerciale", "chef de rayon",
                 "manager de rayon", "equipier", "equipiere", "assistant vente", "assistante vente", "conseiller de vente",
                 "conseillere de vente", "conseiller clientele", "conseillere clientele", "charge de clientele",
                 "chargee de clientele", "teleconseiller", "teleconseillere", "hote d'accueil", "hotesse d'accueil",
                 "agent d'accueil", "preparateur de commandes", "preparatrice de commandes", "boucher", "boulanger",
                 "poissonnier", "analyste financier", "analyste financiere", "controle de gestion", "controleur de gestion",
                 "controleuse de gestion", "acheteur", "acheteuse", "assistant ressources humaines",
                 "assistante ressources humaines", "charge de ressources humaines", "chargee de ressources humaines",
                 "gestionnaire de paie", "gestionnaire paie", "juriste", "assistant juridique", "assistante juridique",
                 "charge de contrats", "chargee de contrats", "gestionnaire administratif", "gestionnaire administrative",
                 "charge de communication", "chargee de communication", "chef de projet marketing", "commercial terrain",
                 # startup business roles, whatever their AI flavour ('Bras droit du CEO - Legaltech IA'); 'SDR / chargé de
                 # prospection' is a sales rep, while 'SDR' alone stays Software-Defined Radio ('CSM' is left out: it is also
                 # a 'Cyber Security Manager')
                 "prospection", "sales development", "bras droit", "founders associate", "founder associate",
                 "chief of staff", "product designer", "ux designer", "ui designer", "ux ui designer", "customer success")
GENERIC_TECH = ("engineer", "ingenieur", "scientist", "scientifique", "chercheur", "researcher", "research",
                "recherche", "r&d", "data", "developer", "developpeur", "analyst", "architect", "model", "models",
                "modele", "modeles", "deploy", "deploying", "deployment", "deploiement", "inference", "pipeline",
                "algorithm", "algorithme", "mlops", "modelisation", "analytics", "python", "engineering", "ingenierie",
                "enhancement", "transformer", "simulation", "numerique", "computational", "calcul", "software",
                "logiciel", "hardware", "embedded", "embarque", "systeme", "systemes", "system", "systems",
                "electronique", "verification", "validation", "conception", "design", "developpement", "development",
                "capteur", "capteurs", "sensor", "sensors", "physique", "physics", "mathematiques", "mathematics",
                "statistique", "statistics", "optique", "optics", "mecanique", "mechanical", "estimation", "technique",
                "technical", "prototype", "prototypage", "test", "tests", "integration")

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
    if info.training and len(t.split()) == 1:
        return done("maybe", "the title only says the kind of offer ('Stagiaire'): the offer itself tells the subject")
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
    core = [x for x in info.topics if x not in TECH_FIELDS]
    if info.topics and not core:  # FPGA, Spark, Kubernetes...: a technical offer outside the research topics
        return done("maybe", f"technical field {', '.join(info.topics[:3])}: AI decides")
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
    if info.training in ("internship", "phd_postdoc", "apprenticeship") and len(t.split()) >= 4:
        return done("maybe", "subject-like internship / thesis title with specialised terms: judged from the description")
    return done("reject", "no research / technical term in the title")


def title_filter(title, dept, profile, employment_type=""):
    """Fast deterministic stage. -> ('pass'|'maybe'|'reject', reason)"""
    i = analyze_title(title, dept, profile, employment_type)
    return i.verdict, i.reason
