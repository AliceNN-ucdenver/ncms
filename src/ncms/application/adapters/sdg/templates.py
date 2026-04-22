"""Typed SDG templates — v7 rewrite.

Single path: every template declares the typed slot it fills, drawn
from a typed :class:`SlotPool`.  The expander walks
``DomainTemplates.templates`` × ``slot_pools`` and emits
:class:`GoldExample` rows that populate **every** declared slot in
the domain's ``SLOT_TAXONOMY``.

Design rationale (see ``docs/slm-entity-extraction-deep-audit.md``):
the pre-v7 SDG funneled every primary value into the ``object`` slot
(renamed to ``library`` for software_dev / ``medication`` for
clinical).  Only one slot ever saw training signal; the adapter's
other declared slots stayed dead.  v7 replaces that with typed
pools (``library`` vs ``language`` vs ``pattern`` vs ``tool``) and
per-slot templates that keep intent / state_change / topic
consistent across phrasings.

Each template knows:
  - the slot it fills (``slot_name``)
  - the intent it expresses (``intent``)
  - whether it's a state-change declaration / retirement (``state_change``)
  - the pattern itself (uses ``{primary}`` + shared auxiliaries)

Auxiliary placeholders drawn from the domain's shared vocab:
  - ``{verb}``   — drawn from ``positive_verbs`` / ``negative_verbs``
  - ``{alt}``    — drawn from the ``alternative`` SlotPool
  - ``{freq}``   — drawn from the ``frequency`` SlotPool
  - ``{phrase}`` — drawn from ``difficulty_phrasings``
  - ``{area}``   — drawn from ``areas`` (optional)
  - ``{aside}``  — drawn from ``asides`` (optional)
  - ``{role}``   — drawn from ``roles`` (optional)

The renderer lives in :mod:`template_expander`.  Deterministic for
a given seed so the SDG corpus is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ncms.application.adapters.schemas import (
    Domain,
    Intent,
    StateChange,
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotPool:
    """Typed vocabulary pool for one slot.

    ``slot_name`` must be a member of the domain's ``SLOT_TAXONOMY``.
    ``topic`` is the label carried by every example emitted from this
    pool — the topic head learns it directly from the pool without
    needing an ``object_to_topic`` side table.
    """

    slot_name: str
    topic: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class SlotTemplate:
    """One template that fills a typed slot.

    The pattern string uses ``{primary}`` for the typed slot value
    and any of ``{verb}``, ``{alt}``, ``{freq}``, ``{phrase}``,
    ``{area}``, ``{aside}``, ``{role}`` for auxiliaries drawn from
    the domain's shared vocab.
    """

    pattern: str
    slot_name: str
    intent: Intent = "none"
    state_change: StateChange = "none"


@dataclass(frozen=True)
class DomainTemplates:
    """Everything needed to generate SDG rows for one domain.

    ``slot_pools`` covers every slot declared in the domain's
    ``SLOT_TAXONOMY``.  ``templates`` covers every intent × slot
    combination we want represented.  Shared vocab pools feed the
    auxiliary placeholders.
    """

    slot_pools: tuple[SlotPool, ...]
    templates: tuple[SlotTemplate, ...]

    # Shared auxiliary vocabulary.
    positive_verbs: tuple[str, ...] = field(default_factory=tuple)
    negative_verbs: tuple[str, ...] = field(default_factory=tuple)
    difficulty_phrasings: tuple[str, ...] = field(default_factory=tuple)

    # Filler pools used by state-change / narrative templates.
    areas: tuple[str, ...] = field(default_factory=tuple)
    asides: tuple[str, ...] = field(default_factory=tuple)
    roles: tuple[str, ...] = field(default_factory=tuple)


# ===========================================================================
# Conversational
#
# Slot taxonomy: object / frequency / alternative.  Small + flat.
# Templates carry ``slot_name="object"`` for the primary slot.
# ===========================================================================


_CONVO_OBJECT_POOL = SlotPool(
    slot_name="object",
    topic="food_pref",
    values=(
        "sushi", "ramen", "dark chocolate", "pizza", "tacos",
        "bubble tea", "cold brew coffee", "matcha lattes", "kombucha",
        "craft beer", "sparkling water",
    ),
)
_CONVO_HOBBY_POOL = SlotPool(
    slot_name="object",
    topic="activity_pref",
    values=(
        "running", "cycling", "rock climbing", "yoga", "hiking trails",
        "stand-up comedy", "live jazz", "board games", "podcasts",
        "e-books", "sketching", "photography", "journalling",
        "meal prepping", "gardening",
    ),
)
_CONVO_TRAVEL_POOL = SlotPool(
    slot_name="object",
    topic="travel_pref",
    values=(
        "beach vacations", "city breaks", "farmer's markets",
        "craft breweries", "weekend getaways", "road trips",
        "backcountry camping", "train journeys",
    ),
)
_CONVO_FREQ_POOL = SlotPool(
    slot_name="frequency",
    topic="habit",
    values=(
        "every morning", "every weekend", "on Saturdays", "nightly",
        "once a week", "every day", "almost never", "rarely",
        "every Tuesday", "before dinner", "after work",
    ),
)
_CONVO_ALT_POOL = SlotPool(
    slot_name="alternative",
    topic="other",
    values=(
        "the aisle seat", "sparkling water", "the vegetarian option",
        "Uber", "the express line", "decaf", "eating out",
        "the scenic route",
    ),
)

_CONVO_TEMPLATES: tuple[SlotTemplate, ...] = (
    # ── Positive / negative / difficulty / choice (single-slot) ──
    SlotTemplate("I {verb} {primary}.",          "object", "positive"),
    SlotTemplate("My favorite thing is {primary}.", "object", "positive"),
    SlotTemplate("Nothing beats {primary}.",     "object", "positive"),
    SlotTemplate("Couldn't live without {primary}.", "object", "positive"),
    SlotTemplate("I'm obsessed with {primary}.", "object", "positive"),

    SlotTemplate("I {verb} {primary}.",          "object", "negative"),
    SlotTemplate("{primary} drives me crazy.",   "object", "negative"),
    SlotTemplate("I'd rather skip {primary}.",   "object", "negative"),
    SlotTemplate("I can't get into {primary}.",  "object", "negative"),

    SlotTemplate("{primary} {phrase}.",          "object", "difficulty"),
    SlotTemplate("Honestly, {primary} {phrase}.", "object", "difficulty"),

    # Habitual uses the frequency slot as the primary typed slot —
    # the object slot is filled as an auxiliary.  Expander sees
    # slot_name="frequency" and draws ``{primary}`` from the
    # frequency pool; ``{object}`` is drawn from the object pool
    # as a side slot.
    SlotTemplate("{primary} I go running.",      "frequency", "habitual"),
    SlotTemplate("I drink coffee {primary}.",    "frequency", "habitual"),
    SlotTemplate("I hit the gym {primary}.",     "frequency", "habitual"),

    # Choice fills alternative as primary-slot; object mentioned as
    # side reference in the pattern.
    SlotTemplate("I picked coffee over {primary}.",   "alternative", "choice"),
    SlotTemplate("We went with pizza instead of {primary}.", "alternative", "choice"),

    # ── Neutral / none templates ─────────────────────────────────
    SlotTemplate("Can you tell me more about {primary}?", "object", "none"),
    SlotTemplate("What's the best way to get into {primary}?", "object", "none"),
    SlotTemplate("How does {primary} work for beginners?", "object", "none"),
    SlotTemplate("Here are some great {primary} options to consider.", "object", "none"),
    SlotTemplate("I'd be happy to help you with {primary}.",   "object", "none"),
    SlotTemplate("The {primary} community is pretty active online.", "object", "none"),
    SlotTemplate("Most beginners start with {primary} at an introductory level.", "object", "none"),
    SlotTemplate("I'm planning to look into {primary} next week.", "object", "none"),
    SlotTemplate("Thanks for the info about {primary}.",       "object", "none"),
    SlotTemplate("I'll keep {primary} in mind for later.",     "object", "none"),
)

CONVERSATIONAL_TEMPLATES = DomainTemplates(
    slot_pools=(
        _CONVO_OBJECT_POOL,
        _CONVO_HOBBY_POOL,
        _CONVO_TRAVEL_POOL,
        _CONVO_FREQ_POOL,
        _CONVO_ALT_POOL,
    ),
    templates=_CONVO_TEMPLATES,
    positive_verbs=(
        "love", "adore", "enjoy", "really like", "am obsessed with",
        "can't get enough of", "am a huge fan of", "swear by",
    ),
    negative_verbs=(
        "can't stand", "hate", "despise", "really don't like",
        "am not a fan of", "steer clear of", "try to avoid", "dread",
    ),
    difficulty_phrasings=(
        "was really hard", "was a nightmare", "took forever",
        "was exhausting", "was a grind",
    ),
)


# ===========================================================================
# Software development
#
# Slot taxonomy: library / language / pattern / tool / alternative /
# frequency.  The v6 adapter only ever emitted library + alternative
# because the old SDG only filled those slots.  v7 covers every slot.
#
# State-change declaration templates deliberately mirror REAL ADR
# decision language we observed being mis-labelled `none` by the v6
# adapter at 0.96+ confidence on the MSEB softwaredev mini corpus:
#   "After considering the pros and cons, we have decided to use X..."
#   "Decided on X. Open to new choices as they arrive."
#   "The decision to adopt X as our standard..."
# ===========================================================================


_SWE_LIBRARY_POOL = SlotPool(
    slot_name="library",
    topic="framework",
    values=(
        # Web / API frameworks
        "FastAPI", "Django", "Flask", "Rails", "Express", "Koa",
        "NestJS", "Spring Boot", "Phoenix", "Sinatra", "Gin",
        # Front-end frameworks / libs
        "React", "Vue", "Svelte", "SvelteKit", "Next.js", "Angular",
        "Remix", "Solid", "Astro", "HTMX",
        # Data / ORM / validation
        "SQLAlchemy", "ActiveRecord", "Prisma", "Ecto", "TypeORM",
        "Pydantic", "Zod", "Marshmallow", "GORM",
        # Styling / UI component libs
        "Tailwind", "Bootstrap", "Chakra UI", "Material UI",
        "shadcn/ui",
        # HTTP / async client
        "Requests", "httpx", "Axios", "Got", "aiohttp",
        # Messaging / background work
        "Celery", "RQ", "Sidekiq", "BullMQ",
    ),
)

_SWE_LANGUAGE_POOL = SlotPool(
    slot_name="language",
    topic="language_runtime",
    values=(
        "Python", "Rust", "Go", "TypeScript", "JavaScript",
        "Ruby", "Java", "Kotlin", "Swift", "C++", "C#",
        "Elixir", "Scala", "Clojure", "Haskell", "OCaml",
        "PHP", "Perl", "Lua", "Zig", "Nim",
    ),
)

_SWE_PATTERN_POOL = SlotPool(
    slot_name="pattern",
    topic="language_runtime",
    values=(
        "async/await", "event-loop concurrency", "threads", "fibers",
        "callback-style code", "promise-based flow", "reactive streams",
        "pub/sub", "CQRS", "event sourcing", "hexagonal architecture",
        "clean architecture", "dependency injection", "observer pattern",
        "strategy pattern", "repository pattern", "unit of work",
        "microservices", "monolith-first", "server-side rendering",
        "client-side rendering", "static site generation",
    ),
)

_SWE_TOOL_POOL = SlotPool(
    slot_name="tool",
    topic="tooling",
    values=(
        # Linters / formatters / type checkers
        "Ruff", "Black", "mypy", "ESLint", "Prettier", "Biome",
        "pyright", "dprint", "rubocop",
        # Package / build tools
        "Poetry", "uv", "pip-tools", "pnpm", "npm", "yarn", "Cargo",
        "Bundler", "Go modules", "Maven", "Gradle",
        # Editors / IDEs
        "VS Code", "Neovim", "JetBrains IDEs", "Cursor", "Emacs",
        # Testing frameworks
        "pytest", "Playwright", "Jest", "Vitest", "Cypress", "JUnit",
        "RSpec",
        # Bundlers / transpilers
        "Webpack", "Vite", "esbuild", "Rollup", "Parcel", "SWC",
        "Turbopack",
        # CI / CD
        "GitHub Actions", "Jenkins", "CircleCI", "GitLab CI",
        "Buildkite", "ArgoCD", "Drone",
    ),
)

# Infra / data / orchestration — surfaced under the ``tool`` slot
# with ``topic='infra'`` so the topic head learns the distinction
# without expanding the slot schema.
_SWE_INFRA_POOL = SlotPool(
    slot_name="tool",
    topic="infra",
    values=(
        # Data stores
        "Postgres", "MySQL", "SQLite", "MongoDB", "Cassandra",
        "DynamoDB", "CockroachDB", "SQL Server", "Oracle",
        # Caches / queues
        "Redis", "Memcached", "Kafka", "RabbitMQ", "NATS", "SQS",
        # Orchestration
        "Docker", "Docker Swarm", "Kubernetes", "Nomad", "ECS",
        # Cloud platforms
        "AWS", "GCP", "Azure", "Vercel", "Fly.io", "Heroku",
        "Cloudflare Workers", "DigitalOcean",
        # Observability
        "Prometheus", "Grafana", "Datadog", "New Relic",
        "OpenTelemetry", "Sentry",
    ),
)

_SWE_FREQUENCY_POOL = SlotPool(
    slot_name="frequency",
    topic="tooling",
    values=(
        "before every commit", "on save", "every morning", "every sprint",
        "in every PR", "during stand-up", "every release",
        "nightly in CI", "on each push", "after every migration",
        "at feature-flag rollout", "at deploy time", "weekly",
    ),
)

_SWE_ALTERNATIVE_POOL = SlotPool(
    slot_name="alternative",
    topic="other",
    values=(
        "threads", "gevent", "raw CSS", "Webpack", "MySQL",
        "Memcached", "subversion", "callback-style code",
        "JSON over HTTP", "REST", "GraphQL", "SOAP",
        "Vue", "Angular", "React", "Svelte",
        "MongoDB", "Redis", "Postgres",
        "Django", "Rails", "Flask", "Express",
        "Jenkins", "CircleCI", "GitHub Actions",
        "VS Code", "Neovim", "JetBrains IDEs",
        "Docker Swarm", "Nomad", "ECS",
    ),
)

_SWE_TEMPLATES: tuple[SlotTemplate, ...] = (
    # ── Positive preference, per slot type ────────────────────────
    SlotTemplate("I {verb} {primary}.",                  "library",  "positive"),
    SlotTemplate("{primary} has saved me countless hours.", "library", "positive"),
    SlotTemplate("I {verb} writing {primary}.",          "language", "positive"),
    SlotTemplate("Writing {primary} makes me productive.", "language", "positive"),
    SlotTemplate("I {verb} {primary} for concurrency.",  "pattern",  "positive"),
    SlotTemplate("{primary} has the cleanest mental model.", "pattern", "positive"),
    SlotTemplate("I {verb} {primary} for our dev loop.", "tool",     "positive"),
    SlotTemplate("{primary} is fantastic for our workflow.", "tool", "positive"),

    # ── Negative ─────────────────────────────────────────────────
    SlotTemplate("I {verb} {primary}.",                  "library",  "negative"),
    SlotTemplate("{primary} is the worst part of my day.", "library", "negative"),
    SlotTemplate("I {verb} {primary} as a language.",    "language", "negative"),
    SlotTemplate("I {verb} debugging {primary}.",        "pattern",  "negative"),
    SlotTemplate("{primary} wastes half my sprint.",     "tool",     "negative"),

    # ── Difficulty ───────────────────────────────────────────────
    SlotTemplate("{primary} {phrase}.",                  "library",  "difficulty"),
    SlotTemplate("Debugging {primary} {phrase}.",        "library",  "difficulty"),
    SlotTemplate("Setting up {primary} {phrase}.",       "tool",     "difficulty"),
    SlotTemplate("Migrating away from {primary} {phrase}.", "tool",  "difficulty"),

    # ── Choice (uses alternative pool for {alt}) ─────────────────
    SlotTemplate("We chose {primary} over {alt}.",       "library",  "choice"),
    SlotTemplate("I went with {primary} instead of {alt}.", "library", "choice"),
    SlotTemplate("Our team picked {primary} over {alt} for {area}.", "library", "choice"),
    SlotTemplate("We standardised on {primary} instead of {alt}.", "tool", "choice"),
    SlotTemplate("We moved from {alt} to {primary} last quarter.", "tool", "choice"),

    # ── Habitual (frequency slot primary) ────────────────────────
    SlotTemplate("I run {primary} in every PR.",         "tool",     "habitual"),
    SlotTemplate("We run {primary} {freq}.",             "tool",     "habitual"),
    SlotTemplate("I lint with {primary} on save.",       "tool",     "habitual"),

    # ── Neutral (none) narratives per slot ───────────────────────
    SlotTemplate("The {primary} documentation is comprehensive.", "library", "none"),
    SlotTemplate("{primary} is widely used in modern services.",  "library", "none"),
    SlotTemplate("This document describes the context and trade-offs for {primary}.", "library", "none"),
    SlotTemplate("See the {primary} documentation for configuration details.", "library", "none"),
    SlotTemplate("The {primary} ecosystem has grown significantly over the past year.", "library", "none"),
    SlotTemplate("{primary} is a popular choice for new backend services.", "language", "none"),
    SlotTemplate("The team is evaluating {primary} alongside other candidates.", "language", "none"),
    SlotTemplate("{primary} simplifies concurrent IO significantly.", "pattern", "none"),
    SlotTemplate("There are documented patterns for combining {primary} with existing services.", "pattern", "none"),
    SlotTemplate("{primary} is one of several options under consideration.", "tool", "none"),
    SlotTemplate("We plan to benchmark {primary} against our baseline next sprint.", "tool", "none"),
    SlotTemplate("The {primary} module lives under the core package.", "tool", "none"),

    # ── Declaration — REAL ADR phrasings we observed the v6 adapter
    #    mislabel as intent=none state_change=none at 0.96+ conf.
    SlotTemplate(
        "We have decided to use {primary} for our {area}.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "After considering the pros and cons, we have decided to use {primary}.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "Decided on {primary}. {aside}.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "The decision to adopt {primary} as our {role} was unanimous.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "Our team went with {primary}.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "Status: Accepted. We hereby adopt {primary} for all new services.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "Going forward, all new modules will use {primary}.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "Effective immediately, {primary} is the recommended pattern.",
        "library", "none", "declaration",
    ),
    SlotTemplate(
        "We have decided to use {primary} as our primary {role}.",
        "language", "none", "declaration",
    ),
    SlotTemplate(
        "Final decision: the backend will be written in {primary}.",
        "language", "none", "declaration",
    ),
    SlotTemplate(
        "We are standardising on {primary} for {area}.",
        "language", "none", "declaration",
    ),
    SlotTemplate(
        "The architecture will follow {primary} going forward.",
        "pattern", "none", "declaration",
    ),
    SlotTemplate(
        "We will adopt {primary} for all new services.",
        "pattern", "none", "declaration",
    ),
    SlotTemplate(
        "We have decided to use {primary} as our {role}.",
        "tool", "none", "declaration",
    ),
    SlotTemplate(
        "After careful consideration, {primary} is the right choice for {area}.",
        "tool", "none", "declaration",
    ),
    SlotTemplate(
        "The decision is to proceed with {primary} as our orchestration platform.",
        "tool", "none", "declaration",
    ),
    SlotTemplate(
        "Decided: {primary} over all alternatives considered.",
        "tool", "none", "declaration",
    ),

    # ── Retirement — supersession / deprecation / migration ──────
    SlotTemplate(
        "This decision supersedes the previous approach using {primary}.",
        "library", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} is deprecated in favor of {alt}.",
        "library", "none", "retirement",
    ),
    SlotTemplate(
        "We are migrating away from {primary} to {alt}.",
        "library", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} has been superseded by {alt}.",
        "library", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} is sunset and will be removed in the next major release.",
        "library", "none", "retirement",
    ),
    SlotTemplate(
        "We have stopped writing new code in {primary}.",
        "language", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} is deprecated for new services.",
        "language", "none", "retirement",
    ),
    SlotTemplate(
        "Our team is migrating off {primary}.",
        "language", "none", "retirement",
    ),
    SlotTemplate(
        "The prior {primary} approach was abandoned.",
        "pattern", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} is no longer recommended for new code.",
        "tool", "none", "retirement",
    ),
    SlotTemplate(
        "The prior {primary} implementation will be retired over the next quarter.",
        "tool", "none", "retirement",
    ),
    SlotTemplate(
        "We are replacing {primary} with {alt} across the stack.",
        "tool", "none", "retirement",
    ),
)

SOFTWARE_DEV_TEMPLATES = DomainTemplates(
    slot_pools=(
        _SWE_LIBRARY_POOL,
        _SWE_LANGUAGE_POOL,
        _SWE_PATTERN_POOL,
        _SWE_TOOL_POOL,
        _SWE_INFRA_POOL,
        _SWE_FREQUENCY_POOL,
        _SWE_ALTERNATIVE_POOL,
    ),
    templates=_SWE_TEMPLATES,
    positive_verbs=(
        "love", "prefer", "swear by", "reach for", "default to",
        "am productive in", "trust", "am enjoying",
    ),
    negative_verbs=(
        "can't stand", "avoid", "dread", "don't trust",
        "steer clear of", "tolerate",
    ),
    difficulty_phrasings=(
        "is surprisingly fiddly", "is brutal to debug",
        "takes forever to set up", "is a rabbit hole",
        "always breaks in weird ways", "has a steep learning curve",
    ),
    areas=(
        "payments service", "auth service", "ingest pipeline",
        "analytics layer", "billing engine", "notification stack",
        "search API", "public API", "internal tooling",
        "observability stack", "data platform",
    ),
    asides=(
        "Open to new choices as they arrive",
        "Team aligned on the approach",
        "ADR written up with full context",
        "Implementation starts next sprint",
        "Migration plan will follow",
    ),
    roles=(
        "primary framework", "default language", "canonical stack",
        "standard toolchain", "chosen approach", "reference implementation",
        "orchestration platform", "primary datastore",
    ),
)


# ===========================================================================
# Clinical
#
# Slot taxonomy: medication / procedure / symptom / severity /
# alternative / frequency.  v7 covers every slot; pre-v7 only
# medication ever got populated.
# ===========================================================================


_CLIN_MEDICATION_POOL = SlotPool(
    slot_name="medication",
    topic="medication",
    values=(
        "metformin", "lisinopril", "atorvastatin", "levothyroxine",
        "ibuprofen", "acetaminophen", "amoxicillin", "sertraline",
        "fluoxetine", "albuterol", "insulin", "warfarin",
        "ondansetron", "empagliflozin", "losartan", "clopidogrel",
        "amlodipine", "omeprazole", "azithromycin", "prednisone",
        "hydrochlorothiazide", "gabapentin", "duloxetine",
    ),
)
_CLIN_PROCEDURE_POOL = SlotPool(
    slot_name="procedure",
    topic="surgery",
    values=(
        "arthroscopic surgery", "arthroscopic meniscectomy",
        "open repair", "stenting", "bypass surgery",
        "ventilator weaning", "colonoscopy", "endoscopy",
        "lumbar puncture", "thoracentesis", "appendectomy",
        "cholecystectomy", "cardiac catheterisation",
        "angioplasty", "MRI scan", "CT scan",
        "echocardiogram", "coronary bypass graft",
    ),
)
_CLIN_SYMPTOM_POOL = SlotPool(
    slot_name="symptom",
    topic="symptom",
    values=(
        "chest pain", "shortness of breath", "nausea", "vomiting",
        "headache", "dizziness", "palpitations", "fatigue",
        "fever", "abdominal pain", "weight loss", "swelling",
        "rash", "joint pain", "back pain", "confusion",
        "weakness", "blurred vision",
    ),
)
_CLIN_SEVERITY_POOL = SlotPool(
    slot_name="severity",
    topic="symptom",
    values=(
        "mild", "moderate", "severe", "acute", "chronic",
        "intermittent", "persistent", "refractory", "progressive",
        "worsening", "improving", "stable",
    ),
)
_CLIN_FREQUENCY_POOL = SlotPool(
    slot_name="frequency",
    topic="medication",
    values=(
        "every 6 hours", "twice daily", "once daily", "as needed",
        "every morning", "at bedtime", "with meals", "weekly",
        "monthly", "every 8 hours", "three times a day",
    ),
)
_CLIN_ALTERNATIVE_POOL = SlotPool(
    slot_name="alternative",
    topic="other",
    values=(
        "warfarin", "apixaban", "metformin", "insulin",
        "lisinopril", "losartan", "ibuprofen", "acetaminophen",
        "open surgery", "laparoscopic approach", "conservative management",
        "physical therapy", "surgical evaluation",
    ),
)

_CLIN_TEMPLATES: tuple[SlotTemplate, ...] = (
    # ── Positive / choice on medication ──────────────────────────
    SlotTemplate("Patient tolerates {primary} well.", "medication", "positive"),
    SlotTemplate("Response to {primary} was excellent.", "medication", "positive"),
    SlotTemplate("{primary} achieved target levels within weeks.", "medication", "positive"),
    SlotTemplate("We preferred {primary} over {alt} for this case.", "medication", "choice"),

    # ── Negative / difficulty ────────────────────────────────────
    SlotTemplate("Patient did not tolerate {primary}.", "medication", "negative"),
    SlotTemplate("{primary} caused significant side effects.", "medication", "negative"),
    SlotTemplate("Compliance with {primary} was poor.", "medication", "difficulty"),

    # ── Procedure (declaration voice; these often ARE state changes)
    SlotTemplate("Patient underwent {primary} without complications.", "procedure", "none"),
    SlotTemplate("{primary} was performed on hospital day 3.", "procedure", "none"),
    SlotTemplate("The {primary} was uneventful.", "procedure", "none"),

    # ── Symptom + severity ───────────────────────────────────────
    SlotTemplate("Presenting complaint was {primary}.", "symptom", "none"),
    SlotTemplate("Patient reports {primary} for the past week.", "symptom", "none"),
    SlotTemplate("{primary} worsened overnight.", "symptom", "none"),
    SlotTemplate("{primary} was documented on arrival.", "severity", "none"),
    SlotTemplate("The condition is now {primary}.", "severity", "none"),

    # ── Frequency on regimens ────────────────────────────────────
    SlotTemplate("Medication dosed {primary}.", "frequency", "habitual"),
    SlotTemplate("Vitals checked {primary}.",   "frequency", "habitual"),

    # ── Declaration — new diagnosis / regimen ────────────────────
    SlotTemplate(
        "Primary diagnosis established as {primary}.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "Treatment was initiated with {primary}.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "Patient was admitted with {primary} as the working diagnosis.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "A new regimen of {primary} was started on admission.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "{primary} was added to the current medication list.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "Final diagnosis documented as {primary}.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "Confirmed diagnosis of {primary} after additional workup.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "We now adopt {primary} as the primary treatment.",
        "medication", "none", "declaration",
    ),
    SlotTemplate(
        "{primary} was scheduled for next week.",
        "procedure", "none", "declaration",
    ),
    SlotTemplate(
        "The care team decided to proceed with {primary}.",
        "procedure", "none", "declaration",
    ),

    # ── Retirement — discontinuation / revision ──────────────────
    SlotTemplate(
        "The prior treatment with {primary} was discontinued.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "Initial diagnosis of {primary} was ruled out in favor of {alt}.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} was stopped due to side effects.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "Previous diagnosis of {primary} was corrected to {alt}.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} therapy was tapered off over the following weeks.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "We discontinued {primary} in favor of {alt}.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "The {primary} regimen was revised after reassessment.",
        "medication", "none", "retirement",
    ),
    SlotTemplate(
        "{primary} was abandoned after failure to progress.",
        "procedure", "none", "retirement",
    ),
)

CLINICAL_TEMPLATES = DomainTemplates(
    slot_pools=(
        _CLIN_MEDICATION_POOL,
        _CLIN_PROCEDURE_POOL,
        _CLIN_SYMPTOM_POOL,
        _CLIN_SEVERITY_POOL,
        _CLIN_FREQUENCY_POOL,
        _CLIN_ALTERNATIVE_POOL,
    ),
    templates=_CLIN_TEMPLATES,
    positive_verbs=("tolerates well", "responded to", "benefited from"),
    negative_verbs=("did not tolerate", "failed to respond to", "reacted badly to"),
    difficulty_phrasings=(
        "was difficult to titrate", "required close monitoring",
        "had persistent side effects",
    ),
)


# ===========================================================================
# SWE diff
#
# Slot taxonomy: file_path / function / symbol / test_path / issue_ref /
# alternative.  Distinct from software_dev — this adapter handles raw
# code-shaped content (diffs, test patches, issue references).
# ===========================================================================


_SWE_DIFF_FILE_PATH_POOL = SlotPool(
    slot_name="file_path",
    topic="patch",
    values=(
        "astropy/modeling/separable.py",
        "django/core/validators.py",
        "django/db/models/fields/__init__.py",
        "sklearn/linear_model/_logistic.py",
        "sympy/core/numbers.py",
        "src/server/auth.ts",
        "src/handlers/webhook.py",
        "pkg/config/loader.go",
        "lib/worker/scheduler.rb",
        "internal/cache/lru.go",
    ),
)
_SWE_DIFF_FUNCTION_POOL = SlotPool(
    slot_name="function",
    topic="patch",
    values=(
        "_cstack", "URLValidator", "compile_sql", "normalize_path",
        "handle_webhook", "resolve_address", "flush_cache",
        "build_dag", "parse_payload", "encode_token",
    ),
)
_SWE_DIFF_SYMBOL_POOL = SlotPool(
    slot_name="symbol",
    topic="patch",
    values=(
        "CompoundModel", "MAX_RETRIES", "Encoder", "UserSerializer",
        "QueryContext", "RunnerMode", "Policy", "Manifest",
        "RateLimit", "ShardKey",
    ),
)
_SWE_DIFF_TEST_POOL = SlotPool(
    slot_name="test_path",
    topic="test",
    values=(
        "astropy/modeling/tests/test_separable.py",
        "django/tests/validators/test_url.py",
        "sklearn/tests/test_logistic_regression.py",
        "tests/unit/test_auth.py",
        "tests/integration/test_webhook.py",
        "tests/e2e/test_scheduler.py",
    ),
)
_SWE_DIFF_ISSUE_POOL = SlotPool(
    slot_name="issue_ref",
    topic="issue",
    values=(
        "#12345", "#42", "#999", "#1234", "astropy/astropy#12907",
        "django/django#12345", "sklearn/sklearn#9876",
    ),
)
_SWE_DIFF_ALT_POOL = SlotPool(
    slot_name="alternative",
    topic="other",
    values=(
        "the prior implementation", "the v1 algorithm",
        "the legacy code path", "the old heuristic",
        "the existing approach",
    ),
)

_SWE_DIFF_TEMPLATES: tuple[SlotTemplate, ...] = (
    # Neutral diff-prose / descriptive
    SlotTemplate("The change touches {primary}.", "file_path", "none"),
    SlotTemplate("This patch modifies {primary}.", "file_path", "none"),
    SlotTemplate("{primary} is the entry point for this flow.", "function", "none"),
    SlotTemplate("We updated {primary} to handle the edge case.", "function", "none"),
    SlotTemplate("The new class {primary} replaces the old helper.", "symbol", "none"),
    SlotTemplate("Coverage is enforced via {primary}.", "test_path", "none"),
    SlotTemplate("See {primary} for the full regression suite.", "test_path", "none"),
    SlotTemplate("This resolves {primary}.", "issue_ref", "none"),
    SlotTemplate("Upstream ticket: {primary}.", "issue_ref", "none"),

    # ── Declaration — resolving patch establishes new invariant ──
    SlotTemplate(
        "This patch introduces {primary} as the canonical implementation.",
        "function", "none", "declaration",
    ),
    SlotTemplate(
        "We now use {primary} as the single source of truth.",
        "symbol", "none", "declaration",
    ),
    SlotTemplate(
        "The new {primary} test encodes the required invariant.",
        "test_path", "none", "declaration",
    ),

    # ── Retirement — patch removes prior impl ────────────────────
    SlotTemplate(
        "{primary} is superseded by the new helper.",
        "function", "none", "retirement",
    ),
    SlotTemplate(
        "We removed {primary} in this patch.",
        "symbol", "none", "retirement",
    ),
    SlotTemplate(
        "The prior {primary} no longer applies.",
        "symbol", "none", "retirement",
    ),
    SlotTemplate(
        "Migrated off {alt} to the new approach.",
        "function", "choice", "retirement",
    ),
)

SWE_DIFF_TEMPLATES = DomainTemplates(
    slot_pools=(
        _SWE_DIFF_FILE_PATH_POOL,
        _SWE_DIFF_FUNCTION_POOL,
        _SWE_DIFF_SYMBOL_POOL,
        _SWE_DIFF_TEST_POOL,
        _SWE_DIFF_ISSUE_POOL,
        _SWE_DIFF_ALT_POOL,
    ),
    templates=_SWE_DIFF_TEMPLATES,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TEMPLATE_REGISTRY: dict[Domain, DomainTemplates] = {
    "conversational": CONVERSATIONAL_TEMPLATES,
    "software_dev": SOFTWARE_DEV_TEMPLATES,
    "clinical": CLINICAL_TEMPLATES,
    "swe_diff": SWE_DIFF_TEMPLATES,
}
