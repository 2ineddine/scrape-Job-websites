"""Offline tests: bilingual title analysis (classic job titles and research-subject titles)."""
import pytest

from jobfinder.classify import analyze_title, classify_location, clean_title, expand_keywords, location_from_text
from jobfinder.config import PROFILE_DEFAULT

P = PROFILE_DEFAULT


@pytest.mark.parametrize("raw, clean", [
    ("Ingénieur Machine Learning H/F", "Ingénieur Machine Learning"),
    ("Senior Data Scientist (m/w/d)", "Senior Data Scientist"),
    ("2026-460 - Ingénieur Analyse de Cycle de Vie Produits H/F", "Ingénieur Analyse de Cycle de Vie Produits"),
    ("Machine Learning Engineer (REQ2026082281)", "Machine Learning Engineer"),
    ("Ingénieur(e) systèmes embarqués F/H", "Ingénieur systèmes embarqués"),
    ("Développeur·se Python - F/H/X", "Développeur Python"),
    ("Consultant Data / IA (F/H)", "Consultant Data / IA"),
])
def test_clean_title(raw, clean):
    assert clean_title(raw) == clean


@pytest.mark.parametrize("title, verdict, kind", [
    ("Apprentissage profond pour la détection d'anomalies dans les séries temporelles", "pass", "research_topic"),
    ("Robust speech recognition in noisy environments", "pass", "research_topic"),
    ("Vision par ordinateur et deep learning pour la robotique agricole", "pass", "research_topic"),
    ("Ingénieur de recherche – Méthodes bayésiennes pour la localisation de sources", "pass", "job"),
    ("Ingénieur Machine Learning H/F", "pass", "job"),
    ("Architecte Solution Data & IA", "pass", "job"),
    ("Chef de projet IA", "maybe", "job"),                  # management role on an AI topic: the AI decides
    ("Ingénieur systèmes embarqués F/H", "maybe", "job"),   # technical, topic unknown: the AI decides
    ("Méthodes d'optimisation stochastique pour réseaux électriques", "maybe", "research_topic"),
    ("Conseiller de vente en magasin", "reject", "job"),
    ("Sales Advisor", "reject", "job"),
    ("Stage - Ingénieur IA", "reject", "job"),
    ("Thèse : reconstruction 3D à partir d'images satellite", "reject", "research_topic"),
    ("Post-doctorant en vision par ordinateur", "reject", "research_topic"),
])
def test_analyze_title(title, verdict, kind):
    i = analyze_title(title, "", P)
    assert (i.verdict, i.kind) == (verdict, kind), i


def test_offer_types():
    on = {**P, "offer_types": ["job", "phd_postdoc"]}
    assert analyze_title("Thèse : reconstruction 3D à partir d'images satellite", "", on).verdict == "pass"
    assert analyze_title("Postdoc in speech enhancement", "", on).verdict == "pass"
    assert analyze_title("Stage - Ingénieur IA", "", on).verdict == "reject"
    stages = {**P, "offer_types": ["internship"]}
    assert analyze_title("Stage - Ingénieur IA", "", stages).verdict == "pass"
    assert analyze_title("[Stage M2] – Image Reconstruction for Structured Illumination", "", stages).verdict == "pass"
    assert analyze_title("Alternance Data Scientist", "", stages).verdict == "reject"
    assert analyze_title("Alternance Data Scientist", "", {**P, "offer_types": ["apprenticeship"]}).verdict == "pass"
    assert analyze_title("CDI Ingénieur Machine Learning", "", stages).verdict == "reject"
    # lab pages often omit the word 'stage': the AI checks the contract in the description
    assert analyze_title("Agents autonomes et systèmes multi-agents pour la robotique", "", stages).verdict == "maybe"
    assert analyze_title("Ingénieur IA", "", {**P, "include_phd_postdoc": True}).verdict == "pass"  # older saved profile


def test_bilingual_topics_and_keywords():
    assert analyze_title("Ingénieur traitement du signal radar", "", P).topics == ["signal processing"]
    assert "computer vision" in analyze_title("Computer Vision Engineer", "", P).topics
    assert "vision par ordinateur" in expand_keywords(["computer vision"])
    assert analyze_title("Ingénieur vision par ordinateur", "", {**P, "keywords": ["computer vision"]}).verdict == "pass"
    assert analyze_title("Data Scientist", "", P).lang == "en" and analyze_title("Ingénieur données", "", P).lang == "fr"


def test_location_from_text_and_french_codes():
    assert location_from_text("Type de contrat : CDI | Localisation : Rueil-Malmaison (92) | Salaire : 50k") == "Rueil-Malmaison (92)"
    assert classify_location("Rueil-Malmaison (92)")["france_eligible"]
    assert classify_location("FR-33-Merignac")["france_eligible"] and classify_location("FR-33-Merignac")["city"] == "Merignac"
    assert classify_location("Geneva, Switzerland")["location_verified"] and not classify_location("Geneva")["france_eligible"]
    assert classify_location("Casablanca, Maroc")["location_verified"] and not classify_location("Casablanca, Maroc")["france_eligible"]
