"""What "understanding a city" means for a CARDIO4Cities engagement.

The taxonomy is the backbone of the system: the planner generates queries per dimension, the
extractor tags every claim with a dimension, the coverage judge measures sufficiency per dimension,
and the report is laid out by dimension. Keeping it explicit (rather than letting an LLM decide
what matters) makes coverage measurable and gaps nameable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    key: str
    title: str
    why: str
    key_questions: tuple[str, ...]
    min_supported_claims: int = 3


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        "city_context",
        "City context & governance",
        "Who governs health in the city and at what scale the programme would operate.",
        (
            "What is the city's population and administrative structure (city vs. metropolitan area)?",
            "Which government body is responsible for health services in the city?",
            "Who currently leads the city government and the city/municipal health department?",
        ),
        min_supported_claims=2,
    ),
    Dimension(
        "cvd_burden",
        "Cardiovascular disease burden",
        "The size of the problem: mortality and morbidity from CVD, stroke and ischaemic heart disease.",
        (
            "What share of deaths is attributable to cardiovascular disease?",
            "What are recent CVD, stroke or ischaemic heart disease mortality or incidence figures?",
        ),
    ),
    Dimension(
        "risk_factors",
        "Risk factors (hypertension, diabetes, dyslipidaemia, lifestyle)",
        "The CARDIO4Cities levers: prevalence, awareness, treatment and control of key risk factors.",
        (
            "What is the prevalence of hypertension, and what share is aware, treated and controlled?",
            "What is the prevalence of type 2 diabetes?",
            "What is known about dyslipidaemia / high cholesterol?",
            "What are the levels of obesity, tobacco use, physical inactivity and salt intake?",
        ),
        min_supported_claims=4,
    ),
    Dimension(
        "health_system",
        "Health system & primary care",
        "Where patients are screened, diagnosed and managed; capacity and access constraints.",
        (
            "How is primary care organised and who provides it (public/private)?",
            "What is known about health workforce, facilities, insurance coverage and medicine access?",
            "Are there digital health or data systems relevant to NCD management?",
        ),
    ),
    Dimension(
        "programmes",
        "Existing health programmes",
        "What is already running, so the City Lead can build on it rather than duplicate it.",
        (
            "Which hypertension, diabetes, NCD or CVD prevention programmes operate in the city?",
            "Who runs them, since when, and with what reported results?",
        ),
    ),
    Dimension(
        "policies",
        "Policy initiatives",
        "The policy environment that enables or constrains action (city, state and national).",
        (
            "Which city-level health or NCD policies, strategies or plans exist?",
            "Which national or state policies (e.g. NCD plans, tobacco/salt/sugar measures) apply to the city?",
        ),
    ),
    Dimension(
        "stakeholders",
        "Stakeholders & organisations",
        "Who to meet: named officials, institutions, academic partners, NGOs and private actors.",
        (
            "Which named officials hold relevant health roles in the city?",
            "Which hospitals, universities, NGOs, professional societies or companies are active on CVD/NCDs?",
        ),
    ),
)

DIMENSION_KEYS = [d.key for d in DIMENSIONS]
DIMENSION_BY_KEY = {d.key: d for d in DIMENSIONS}

# Opportunities, risks and gaps are *analysis*, not facts: produced by the synthesis agent from
# verified claims and explicitly labelled as inference in the UI and the report.
ANALYSIS_SECTIONS = ("opportunities", "risks")

# A controlled vocabulary for statistics. Normalising metric names lets us detect conflicts
# (same metric, different values) deterministically instead of asking an LLM to spot them.
METRIC_KEYS = (
    "population",
    "cvd_mortality_share",
    "cvd_mortality_rate",
    "stroke_mortality_rate",
    "ihd_mortality_rate",
    "hypertension_prevalence",
    "hypertension_awareness",
    "hypertension_treatment",
    "hypertension_control",
    "diabetes_prevalence",
    "high_cholesterol_prevalence",
    "obesity_prevalence",
    "overweight_prevalence",
    "tobacco_use_prevalence",
    "physical_inactivity_prevalence",
    "salt_intake",
    "primary_care_facilities",
    "health_workforce_density",
    "health_insurance_coverage",
    "other",
)

GEOGRAPHY_LEVELS = ("city", "metro", "district", "subnational", "national", "regional", "global", "unknown")
CITY_LEVELS = {"city", "metro", "district"}
