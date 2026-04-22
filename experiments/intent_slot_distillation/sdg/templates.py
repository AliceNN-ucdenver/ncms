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
    # ── Neutral-voice templates for ``intent="none"`` ──────────────────
    # Questions, factual statements, assistant replies, and
    # descriptive/plan statements that DO NOT carry a preference.
    # These are the distractors the adapter needs to learn that
    # "this text is not a preference utterance".  Without them the
    # intent head over-fires on descriptive corpus content (93%+
    # non-none predictions on prose corpora, observed in v4).
    none_templates: tuple[IntentTemplate, ...] = field(default_factory=tuple)
    # ── State-change templates ───────────────────────────────────────
    # For clinical (diagnosis declaration/revision) and software_dev
    # (ADR supersession).  Emit ``intent="none"`` (state changes are
    # not preference utterances) and ``state_change`` set accordingly.
    # Conversational leaves both tuples empty — conversation doesn't
    # have "Redis upgraded to v7.4" state transitions.
    state_change_decl_templates: tuple[IntentTemplate, ...] = field(
        default_factory=tuple,
    )
    state_change_ret_templates: tuple[IntentTemplate, ...] = field(
        default_factory=tuple,
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
    # Neutral-voice — these MUST train intent=none so the adapter
    # learns that descriptive / question / assistant prose is not a
    # preference statement.  Target shapes cover the 3 failure modes
    # observed in v4 eval: (a) AI assistant replies, (b) questions,
    # (c) user descriptive / plan statements.
    none_templates=(
        # Questions (user asks about object)
        IntentTemplate("Can you tell me more about {object}?", ("object",)),
        IntentTemplate("What's the best way to get into {object}?", ("object",)),
        IntentTemplate("How does {object} work for beginners?", ("object",)),
        IntentTemplate("Are there any good resources for learning about {object}?", ("object",)),
        IntentTemplate("What should I know before trying {object}?", ("object",)),
        # Assistant-voice replies (most of our corpus is this shape)
        IntentTemplate(
            "Here are some great {object} options to consider:",
            ("object",),
        ),
        IntentTemplate(
            "I'd be happy to help you with {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Let me know if you have any other questions about {object}.",
            ("object",),
        ),
        IntentTemplate(
            "That's a great topic! {object} has a lot going for it.",
            ("object",),
        ),
        IntentTemplate(
            "You're welcome! Glad I could help with {object}.",
            ("object",),
        ),
        # Neutral / factual descriptive statements
        IntentTemplate(
            "There are many different styles of {object}.",
            ("object",),
        ),
        IntentTemplate(
            "The {object} community is pretty active online.",
            ("object",),
        ),
        IntentTemplate(
            "Most beginners start with {object} at an introductory level.",
            ("object",),
        ),
        # Plans / intentions without preference
        IntentTemplate(
            "I'm planning to look into {object} next week.",
            ("object",),
        ),
        IntentTemplate(
            "I'll check out {object} when I get a chance.",
            ("object",),
        ),
        IntentTemplate(
            "I might give {object} a try sometime.",
            ("object",),
        ),
        # Acknowledgments / time references
        IntentTemplate("Thanks for the info about {object}.", ("object",)),
        IntentTemplate(
            "Got it, I'll keep {object} in mind for later.",
            ("object",),
        ),
        IntentTemplate(
            "I'll think it over and let you know about {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Sounds good, I'll come back with more questions about {object}.",
            ("object",),
        ),
    ),
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
    # Neutral-voice ADR / design-doc / code-review prose that's not a
    # personal preference.  Primary failure mode in v4: ADR context
    # sections ("The team evaluated...") got tagged as ``choice``.
    none_templates=(
        # Questions (developer asks about object)
        IntentTemplate("What does {object} offer for our use case?", ("object",)),
        IntentTemplate("How does {object} compare to the alternatives?", ("object",)),
        IntentTemplate("What are the trade-offs of using {object}?", ("object",)),
        # ADR narrative context
        IntentTemplate(
            "The team evaluated several approaches including {object}.",
            ("object",),
        ),
        IntentTemplate(
            "This document describes the context and trade-offs for {object}.",
            ("object",),
        ),
        IntentTemplate(
            "{object} is one of several options under consideration.",
            ("object",),
        ),
        IntentTemplate(
            "The decision record will be updated as we learn more about {object}.",
            ("object",),
        ),
        # Neutral factual statements
        IntentTemplate(
            "{object} is widely used in modern Python services.",
            ("object",),
        ),
        IntentTemplate(
            "The {object} ecosystem has grown significantly over the past year.",
            ("object",),
        ),
        IntentTemplate(
            "There are documented patterns for integrating {object} with existing services.",
            ("object",),
        ),
        # Future work / planning (no preference)
        IntentTemplate(
            "Next steps involve benchmarking {object} against our baseline.",
            ("object",),
        ),
        IntentTemplate(
            "We plan to evaluate {object} in a proof of concept.",
            ("object",),
        ),
        # Documentation / reference
        IntentTemplate(
            "See the {object} documentation for configuration details.",
            ("object",),
        ),
        IntentTemplate(
            "{object} is tracked in issue #123 and discussed in the design review.",
            ("object",),
        ),
        IntentTemplate(
            "The {object} module lives under the core package.",
            ("object",),
        ),
    ),
    # ADR declaration — "Status: Accepted" / "We adopt X" language.
    state_change_decl_templates=(
        IntentTemplate(
            "Status: Accepted. We hereby adopt {object} for all new services.",
            ("object",),
        ),
        IntentTemplate(
            "The decision is to proceed with {object}.",
            ("object",),
        ),
        IntentTemplate(
            "{object} is now the standard for new internal APIs.",
            ("object",),
        ),
        IntentTemplate(
            "Going forward, all new modules will use {object}.",
            ("object",),
        ),
        IntentTemplate(
            "This ADR establishes {object} as the chosen approach.",
            ("object",),
        ),
        IntentTemplate(
            "We are introducing {object} as the default for the platform.",
            ("object",),
        ),
        IntentTemplate(
            "Effective immediately, {object} is the recommended pattern.",
            ("object",),
        ),
        IntentTemplate(
            "The team has adopted {object} after review.",
            ("object",),
        ),
    ),
    # ADR retirement / supersession — the exact vocabulary TLG needs
    # to detect for zone transitions ("Supersedes", "Deprecated by",
    # "migrated away from").
    state_change_ret_templates=(
        IntentTemplate(
            "This decision supersedes the previous approach using {object}.",
            ("object",),
        ),
        IntentTemplate(
            "{object} is deprecated in favor of {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "We are migrating away from {object} to {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "{object} is no longer recommended for new code.",
            ("object",),
        ),
        IntentTemplate(
            "The prior {object} implementation will be retired over the next quarter.",
            ("object",),
        ),
        IntentTemplate(
            "{object} has been superseded by {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "All existing uses of {object} should be migrated before the deprecation deadline.",
            ("object",),
        ),
        IntentTemplate(
            "{object} is sunset and will be removed in the next major release.",
            ("object",),
        ),
    ),
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
    # Neutral-voice case-report narrative prose.  v4 over-fired on
    # descriptive clinical text (48% `choice` on case reports).
    # These templates teach the adapter that narrative context,
    # demographic / history statements, and assessment prose are not
    # preference utterances.
    none_templates=(
        # Case-report narrative context
        IntentTemplate(
            "The patient presented with symptoms suggestive of {object}-related pathology.",
            ("object",),
        ),
        IntentTemplate(
            "Initial workup included {object} and standard lab panels.",
            ("object",),
        ),
        IntentTemplate(
            "The case report describes a patient evaluated for {object}.",
            ("object",),
        ),
        IntentTemplate(
            "History was notable for prior exposure to {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Review of systems was unremarkable except for {object}.",
            ("object",),
        ),
        # Assessment / plan (neutral voice)
        IntentTemplate(
            "Assessment pending further evaluation of {object} findings.",
            ("object",),
        ),
        IntentTemplate(
            "Follow-up scheduled to reassess response to {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Consultation requested to address {object} management.",
            ("object",),
        ),
        # Questions
        IntentTemplate(
            "What is the differential diagnosis for {object} presentation?",
            ("object",),
        ),
        IntentTemplate(
            "Are there established guidelines for {object} workup?",
            ("object",),
        ),
        # Demographic / factual
        IntentTemplate(
            "Patient is a 45-year-old with a history including {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Admission labs were consistent with {object}-related findings.",
            ("object",),
        ),
        IntentTemplate(
            "Imaging showed changes compatible with {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Vital signs stable on arrival; {object} unremarkable.",
            ("object",),
        ),
        IntentTemplate(
            "Discharge summary documented the course of {object} treatment.",
            ("object",),
        ),
    ),
    # Clinical state_change=declaration — new diagnosis / regimen.
    state_change_decl_templates=(
        IntentTemplate(
            "Primary diagnosis established as {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Treatment was initiated with {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Patient was admitted with {object} as the working diagnosis.",
            ("object",),
        ),
        IntentTemplate(
            "A new regimen of {object} was started on admission.",
            ("object",),
        ),
        IntentTemplate(
            "{object} was added to the current medication list.",
            ("object",),
        ),
        IntentTemplate(
            "Final diagnosis documented as {object}.",
            ("object",),
        ),
        IntentTemplate(
            "Confirmed diagnosis of {object} after additional workup.",
            ("object",),
        ),
        IntentTemplate(
            "We now adopt {object} as the primary treatment.",
            ("object",),
        ),
    ),
    # Clinical state_change=retirement — discontinuation / revision.
    state_change_ret_templates=(
        IntentTemplate(
            "The prior treatment with {object} was discontinued.",
            ("object",),
        ),
        IntentTemplate(
            "Initial diagnosis of {object} was ruled out in favor of {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "{object} was stopped due to side effects.",
            ("object",),
        ),
        IntentTemplate(
            "The earlier {object} approach was abandoned.",
            ("object",),
        ),
        IntentTemplate(
            "Previous diagnosis of {object} was corrected to {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "{object} therapy was tapered off over the following weeks.",
            ("object",),
        ),
        IntentTemplate(
            "We discontinued {object} in favor of {alt}.",
            ("object", "alternative"),
        ),
        IntentTemplate(
            "The {object} regimen was revised after reassessment.",
            ("object",),
        ),
    ),
)


TEMPLATE_REGISTRY: dict[Domain, DomainTemplates] = {
    "conversational": CONVERSATIONAL_TEMPLATES,
    "software_dev": SOFTWARE_DEV_TEMPLATES,
    "clinical": CLINICAL_TEMPLATES,
}
