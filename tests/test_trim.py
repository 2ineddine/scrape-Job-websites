"""What the AI receives: the offer without its boilerplate, every fact kept (jobfinder/trim.py)."""
from jobfinder.trim import essential_text

PITCH = ("Créé en France en 1988, Exemplia est un acteur mondial de l'ingénierie et du conseil en technologies. "
         "Exemplia réunit 10 000 collaborateurs dans 20 pays, avec un chiffre d'affaires de 730M€ en 2025. "
         "Élue Great Place To Work pendant 4 ans, Exemplia est aussi une B Corp. ")
OFFER = (
    "Accueil / Développez votre carrière / Nos offres / Ingénieur Vision H/F " + PITCH +
    "En résumé Type de contrat CDI / Temps plein Expérience 3 ans minimum Localisation Lyon (69), FR "
    "Salaire Entre 45 et 55 000€ Télétravail possible 2 jours/semaine Publié le 3 novembre 2026 "
    "Vos missions : Concevoir et entraîner des modèles de détection d'objets pour des caméras embarquées. "
    "Vous participerez à l'industrialisation des modèles avec l'équipe logiciel. "
    "Environnement technique : Python / PyTorch / Docker / Linux / ROS2 "
    "Profil recherché Vous êtes diplômé d'une école d'ingénieur ou d'un master en vision par ordinateur. "
    "Anglais courant. "
    "Avantages Mutuelle, tickets restaurant, afterwork mensuel et une ambiance conviviale. "
    "Nous encourageons les candidatures de personnes en situation de handicap. " + PITCH +
    "Ce poste vous intéresse ? Postulez Nom (Nécessaire) Prénom (Nécessaire) E-mail (Nécessaire) CV Types de fichiers "
    "acceptés : pdf, doc. J'accepte la politique de confidentialité. Envoyer Nos offres du même secteur Data Engineer Paris")


def test_the_boilerplate_goes_and_every_fact_stays():
    out = essential_text(OFFER, 9000, "Exemplia")
    for gone in ("Great Place To Work", "acteur mondial", "10 000 collaborateurs", "Accueil / Développez",
                 "situation de handicap", "Mutuelle", "Nom (Nécessaire)", "Nos offres du même secteur"):
        assert gone not in out, gone
    for kept in ("CDI", "3 ans minimum", "Lyon (69)", "45 et 55 000€", "2 jours/semaine", "Publié le 3 novembre 2026",
                 "Concevoir et entraîner des modèles", "Vous participerez", "Python / PyTorch / Docker / Linux / ROS2",
                 "master en vision par ordinateur", "Anglais courant"):
        assert kept in out, kept
    assert len(out) < 0.6 * len(OFFER)


def test_a_company_presentation_repeated_at_the_bottom_is_sent_once_at_most():
    out = essential_text(OFFER, 9000, "Exemplia")
    assert out.count("Créé en France en 1988") <= 1  # kept once: it states a date


def test_a_short_offer_is_sent_as_it_is():
    short = "Stage - Vision par ordinateur. Vous développerez un prototype de suivi d'objets. Paris, 6 mois."
    assert essential_text(short, 9000) == short


def test_an_offer_written_only_as_company_text_is_not_emptied():
    text = ("Exemplia accompagne les industriels dans leur transformation numérique depuis vingt ans. " * 12).strip()
    assert len(essential_text(text, 9000, "Exemplia")) >= 400  # nothing recognisable left: the page is kept


def test_the_length_limit_keeps_the_facts_and_the_work_first():
    filler = "Nous aimons les projets ambitieux et les équipes soudées. " * 60
    text = filler + "Vos missions : Concevoir des modèles de vision. Type de contrat CDI. Salaire 50k€. " + filler
    out = essential_text(text, 300)
    assert len(out) <= 300 and "CDI" in out and "50k€" in out and "Concevoir des modèles" in out


def test_the_job_summary_before_its_first_heading_is_kept():
    """CGI: 'POSTE : Consultant Data Analyst H/F DESCRIPTION : ... Description de poste Big Data, Data Science...' was dropped
    as a company introduction."""
    text = ("POSTE : Consultant Data Analyst H/F Big Data, Data Science, Data analyse, Data architecture au service de "
            "projets industriels. Nous sommes un leader du conseil, présents dans 40 pays avec 90 000 collaborateurs. "
            "Vos missions : Concevoir des tableaux de bord et des modèles prédictifs pour nos clients industriels. "
            "Vous participerez aux ateliers avec les équipes métier. " * 6 + "Profil recherché : Master en data science. " * 6)
    out = essential_text(text, 9000, "CGI")
    assert "Big Data, Data Science" in out and "90 000 collaborateurs" not in out
