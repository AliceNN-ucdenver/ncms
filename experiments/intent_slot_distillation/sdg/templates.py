"""Template catalogues for synthetic data generation.

Each domain has:

* Per-intent phrasing templates (``"I {verb} {object}"`` etc.)
* A vocabulary pool (verbs / objects / frequencies / alternatives)
  drawn from realistic domain artefacts.

The expander (``template_expander.py``) walks the cross product of
template × vocabulary to emit ``GoldExample`` rows.  Deterministic
for a given seed so re-runs produce identical datasets — important
for reproducing experiment results.

**Scope.**  These templates are *seeds*.  A production-grade SDG
run would layer paraphrasing (Nemotron / Qwen) on top for
phrasing diversity.  The raw template output is what we use for
fast-iteration experiments; the paraphrased variant (see
``nemotron_generator.py``) is for final training data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from experiments.intent_slot_distillation.schemas import Domain, Intent


@dataclass(frozen=True)
class IntentTemplate:
    """One phrasing template for a given intent.

    ``pattern`` uses Python ``str.format`` placeholders matching
    slot names plus ``{verb}`` / ``{freq}`` / ``{alt}`` auxiliary
    variables drawn from the domain vocabulary pool.

    Example:
        ``"I {verb} {object}"`` with ``verb="love"`` and
        ``object="rock climbing"`` → ``"I love rock climbing"``.
    """

    pattern: str
    required_slots: tuple[str, ...]      # which slot fields the template fills


@dataclass(frozen=True)
class DomainTemplates:
    """All template + vocab data for one domain."""

    objects: tuple[str, ...]             # primary slot values (object/library/medication/…)
    alternatives: tuple[str, ...]        # for "X over Y" and "chose X not Y"
    positive_verbs: tuple[str, ...]
    negative_verbs: tuple[str, ...]
    habitual_freqs: tuple[str, ...]
    difficulty_phrasings: tuple[str, ...]
    intent_templates: dict[Intent, tuple[IntentTemplate, ...]] = field(
        default_factory=dict,
    )


# ---------------------------------------------------------------------------
# Conversational
# ---------------------------------------------------------------------------

_CONVO_OBJECTS = (
    "sushi", "ramen", "dark chocolate", "running", "cycling",
    "rock climbing", "yoga", "standing desks", "cold brew coffee",
    "board games", "standup comedy", "live jazz", "hiking trails",
    "podcasts", "e-books", "my morning routine", "beach vacations",
    "city breaks", "farmer's markets", "craft breweries",
)

_CONVO_ALTS = (
    "the aisle seat", "sparkling water", "the red one",
    "the vegetarian option", "Uber", "the express line",
    "the north face route", "decaf",
)

_CONVO_POS_VERBS = (
    "love", "adore", "enjoy", "really like", "am obsessed with",
    "can't get enough of", "am a huge fan of", "swear by",
)

_CONVO_NEG_VERBS = (
    "can't stand", "hate", "despise", "really don't like",
    "am not a fan of", "steer clear of", "try to avoid", "dread",
)

_CONVO_FREQS = (
    "every morning", "every weekend", "on Saturdays", "nightly",
    "once a week", "every day", "always", "usually", "almost never",
    "rarely", "every Tuesday",
)

_CONVO_DIFFICULTY = (
    "was really hard", "was a nightmare", "took forever",
    "was exhausting", "was a grind", "was more work than I bargained for",
)

CONVERSATIONAL_TEMPLATES = DomainTemplates(
    objects=_CONVO_OBJECTS,
    alternatives=_CONVO_ALTS,
    positive_verbs=_CONVO_POS_VERBS,
    negative_verbs=_CONVO_NEG_VERBS,
    habitual_freqs=_CONVO_FREQS,
    difficulty_phrasings=_CONVO_DIFFICULTY,
    intent_templates={
        "positive": (
            IntentTemplate("I {verb} {object}.", ("object",)),
            IntentTemplate("My favorite thing is {object}.", ("object",)),
            IntentTemplate("Nothing beats {object}.", ("object",)),
            IntentTemplate("Couldn't live without {object}.", ("object",)),
        ),
        "negative": (
            IntentTemplate("I {verb} {object}.", ("object",)),
            IntentTemplate("{object} drives me crazy.", ("object",)),
            IntentTemplate("I'd rather skip {object}.", ("object",)),
        ),
        "habitual": (
            IntentTemplate("I {freq} do {object}.", ("object", "frequency")),
            IntentTemplate(
                "{freq}, I spend time on {object}.",
                ("object", "frequency"),
            ),
        ),
        "difficulty": (
            IntentTemplate("{object} {phrase}.", ("object",)),
            IntentTemplate(
                "Honestly, {object} {phrase}.",
                ("object",),
            ),
        ),
        "choice": (
            IntentTemplate(
                "I picked {object} over {alt}.",
                ("object", "alternative"),
            ),
            IntentTemplate(
                "We went with {object} instead of {alt}.",
                ("object", "alternative"),
            ),
        ),
    },
)


# ---------------------------------------------------------------------------
# Software development
# ---------------------------------------------------------------------------

_SWE_OBJECTS = (
    "FastAPI", "Pydantic", "SQLAlchemy", "Redis", "Postgres",
    "Docker", "Kubernetes", "asyncio", "gevent", "Celery",
    "Django", "Flask", "React", "Vue", "Tailwind",
    "Poetry", "pip-tools", "uv", "Neovim", "VS Code",
    "pytest", "Playwright", "Ruff", "Black", "mypy",
)

_SWE_ALTS = (
    "threads", "gevent", "raw CSS", "Webpack", "MySQL",
    "Memcached", "subversion", "callback-style code",
)

_SWE_POS_VERBS = (
    "love", "prefer", "swear by", "reach for", "default to",
    "am productive in", "trust",
)

_SWE_NEG_VERBS = (
    "can't stand", "avoid", "dread", "don't trust", "steer clear of",
)

_SWE_FREQS = (
    "before every commit", "on save", "every morning", "every sprint",
    "in every PR", "during stand-up", "every release",
)

_SWE_DIFFICULTY = (
    "is surprisingly fiddly", "is brutal to debug", "takes forever to set up",
    "is a rabbit hole", "always breaks in weird ways",
)

SOFTWARE_DEV_TEMPLATES = DomainTemplates(
    objects=_SWE_OBJECTS,
    alternatives=_SWE_ALTS,
    positive_verbs=_SWE_POS_VERBS,
    negative_verbs=_SWE_NEG_VERBS,
    habitual_freqs=_SWE_FREQS,
    difficulty_phrasings=_SWE_DIFFICULTY,
    intent_templates={
        "positive": (
            IntentTemplate("I {verb} {object}.", ("object",)),
            IntentTemplate("{object} has saved me countless hours.", ("object",)),
        ),
        "negative": (
            IntentTemplate("I {verb} {object}.", ("object",)),
            IntentTemplate("{object} is the worst part of my day.", ("object",)),
        ),
        "habitual": (
            IntentTemplate(
                "I run {object} {freq}.",
                ("object", "frequency"),
            ),
            IntentTemplate(
                "{freq} I reach for {object}.",
                ("object", "frequency"),
            ),
        ),
        "difficulty": (
            IntentTemplate("{object} {phrase}.", ("object",)),
            IntentTemplate(
                "Debugging {object} {phrase}.",
                ("object",),
            ),
        ),
        "choice": (
            IntentTemplate(
                "We chose {object} over {alt}.",
                ("object", "alternative"),
            ),
            IntentTemplate(
                "I went with {object} instead of {alt}.",
                ("object", "alternative"),
            ),
        ),
    },
)


# ---------------------------------------------------------------------------
# Clinical
# ---------------------------------------------------------------------------

_CLINIC_OBJECTS = (
    "metformin", "lisinopril", "atorvastatin", "levothyroxine",
    "ibuprofen", "acetaminophen", "amoxicillin", "sertraline",
    "fluoxetine", "albuterol", "insulin", "warfarin",
    "physical therapy", "occupational therapy", "MRI", "CT scan",
    "arthroscopic surgery", "open repair", "stenting", "bypass",
)

_CLINIC_ALTS = (
    "fluoxetine", "aspirin", "open repair", "bypass", "conservative management",
)

_CLINIC_POS_VERBS = (
    "tolerates", "responds well to", "is improving with", "benefits from",
)

_CLINIC_NEG_VERBS = (
    "declines", "refuses", "does not tolerate", "is allergic to",
)

_CLINIC_FREQS = (
    "daily in the morning", "twice daily", "every night", "as needed",
    "weekly", "every four hours",
)

_CLINIC_DIFFICULTY = (
    "has been slow", "has been challenging", "is not progressing",
    "is taking longer than expected", "has been complicated",
)

CLINICAL_TEMPLATES = DomainTemplates(
    objects=_CLINIC_OBJECTS,
    alternatives=_CLINIC_ALTS,
    positive_verbs=_CLINIC_POS_VERBS,
    negative_verbs=_CLINIC_NEG_VERBS,
    habitual_freqs=_CLINIC_FREQS,
    difficulty_phrasings=_CLINIC_DIFFICULTY,
    intent_templates={
        "positive": (
            IntentTemplate("Patient {verb} {object}.", ("object",)),
        ),
        "negative": (
            IntentTemplate("Patient {verb} {object}.", ("object",)),
        ),
        "habitual": (
            IntentTemplate(
                "Patient takes {object} {freq}.",
                ("object", "frequency"),
            ),
        ),
        "difficulty": (
            IntentTemplate(
                "{object} {phrase}.",
                ("object",),
            ),
        ),
        "choice": (
            IntentTemplate(
                "We elected to proceed with {object} over {alt}.",
                ("object", "alternative"),
            ),
            IntentTemplate(
                "Started patient on {object} instead of {alt}.",
                ("object", "alternative"),
            ),
        ),
    },
)


TEMPLATE_REGISTRY: dict[Domain, DomainTemplates] = {
    "conversational": CONVERSATIONAL_TEMPLATES,
    "software_dev": SOFTWARE_DEV_TEMPLATES,
    "clinical": CLINICAL_TEMPLATES,
}
