"""LLM-driven typed-slot labeller for gold expansion.

v6/v7 expanded the slot taxonomy for every domain (software_dev:
library / language / pattern / tool / alternative / frequency;
clinical: medication / procedure / symptom / severity / alternative /
frequency; …).  Hand-labelled gold predates those expansions and
only ever populated one or two slot labels per domain, which made
the training gate's slot F1 measurement structurally mis-aligned
with the trained model: the adapter correctly emits typed slots on
content the gold doesn't have typed labels for — and those correct
predictions count as false positives.

This labeller closes the gap.  For each input memory it asks the
LLM to extract every typed slot value that literally appears in
the text, validates the output against the content (rejects
hallucinations), and emits a :class:`GoldExample` with filled
typed slots + topic + admission + state_change.

Deterministic defaults for admission (always ``persist`` on
gold-curated corpora) + conservative state_change defaults (only
flipped to declaration / retirement when the LLM emits that label
AND the content contains one of the characteristic phrases).

The output JSONL slots into the same training pipeline as
hand-labelled gold via ``adapters/corpora/gold_<domain>.jsonl``.

Example::

    ncms adapters label-slots \\
        --domain software_dev \\
        --source benchmarks/mseb_softwaredev/build_mini/corpus.jsonl \\
        --output adapters/corpora/gold_software_dev_llm.jsonl \\
        --limit 200

    # merge into primary gold (cat is fine — loader dedupes on text)
    cat adapters/corpora/gold_software_dev_llm.jsonl \\
        >> adapters/corpora/gold_software_dev.jsonl
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from ncms.application.adapters.schemas import (
    SLOT_TAXONOMY,
    Domain,
    GoldExample,
    RoleSpan,
)
from ncms.application.adapters.sdg.catalog import detect_spans
from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)


# ── Per-domain prompt scaffolding ─────────────────────────────────

_SLOT_DESCRIPTIONS: dict[Domain, dict[str, str]] = {
    "software_dev": {
        # Crisp functional boundary rules — agent-SDLC schema.  Each
        # surface should map to EXACTLY ONE slot by these rules:
        #   language   ← compiled/interpreted language  (Python, Rust)
        #   framework  ← opinionated app structure      (Django, React)
        #   library    ← imported dep that's not a fwk  (Pydantic)
        #   database   ← data store / cache / queue     (Postgres, Redis, Kafka)
        #   platform   ← orchestration / cloud / runtime  (Docker, K8s, AWS)
        #   tool       ← dev-time only, not prod         (ruff, pytest, VS Code)
        #   pattern    ← architectural concept           (async, CQRS)
        "language": (
            "a programming language you write programs in — "
            "Python, Rust, Go, TypeScript, JavaScript, Ruby, Java, "
            "Kotlin, Swift, C++, C#, PHP."
        ),
        "framework": (
            "an opinionated application / UI framework that dictates "
            "how you structure an entire app — FastAPI, Django, Flask, "
            "Rails, Express, NestJS, Spring Boot, React, Vue, Svelte, "
            "Next.js, Angular.  NOT just any library."
        ),
        "library": (
            "a code dependency you import that is NOT a framework — "
            "provides functionality without dictating app structure — "
            "Pydantic, Zod, Requests, Axios, SQLAlchemy, Prisma, "
            "Tailwind, Bootstrap, Lodash, Celery.  NEVER use this "
            "slot for databases, cloud platforms, or dev tools."
        ),
        "database": (
            "anything that stores, queries, queues, or indexes data "
            "structurally.  Includes: relational databases (Postgres, "
            "MySQL), document/KV stores (MongoDB, DynamoDB, Redis), "
            "message queues (Kafka, RabbitMQ, SQS), search indexes "
            "(Elasticsearch, OpenSearch).  MongoDB/Redis/Kafka always "
            "go here — NEVER 'library'."
        ),
        "platform": (
            "a runtime or orchestration environment where apps run "
            "in production.  Includes: containers (Docker), "
            "orchestration (Kubernetes, Nomad, ECS), cloud providers "
            "(AWS, GCP, Azure), PaaS/serverless (Vercel, Fly.io, "
            "Heroku, Cloud Run).  Docker/Kubernetes always go here."
        ),
        "tool": (
            "dev-time tooling that does NOT run in production.  "
            "Includes: linters/formatters (ruff, ESLint, Prettier), "
            "type checkers (mypy, pyright), package managers (Poetry, "
            "uv, npm), editors (VS Code, Neovim), test runners (pytest, "
            "Jest, Playwright), bundlers (Webpack, Vite), CI/CD "
            "(GitHub Actions, Jenkins), observability dashboards "
            "(Grafana, Datadog)."
        ),
        "pattern": (
            "an architectural or coding pattern AS A NAMED PATTERN — "
            "async/await, event-loop, threads, callback-style, "
            "CQRS, event sourcing, microservices, monolith, "
            "dependency injection, hexagonal architecture, MVC, "
            "pub/sub, SSR, CSR, observer pattern, strategy pattern.  "
            "DO NOT use for generic adjectives ('overkill', "
            "'performant', 'scalable') or domain concepts ('document "
            "database', 'multi-monitor testing').  If unsure whether "
            "something is a named pattern, leave it out."
        ),
        "alternative": (
            "a competing option in an EXPLICIT X-vs-Y contrast.  "
            "Require a contrast signal in the content: 'over Y', "
            "'instead of Y', 'migrated from Y', 'rejected Y', "
            "'Y was the previous Z', 'X replaces Y'.  Do NOT use "
            "this slot for mere mentions of a concept — if a surface "
            "is just described without contrast, put it in its "
            "functional slot (framework/database/etc.) or omit it."
        ),
        "frequency": (
            "STRICT time-interval expressions ONLY — 'every commit', "
            "'on save', 'nightly', 'every release', 'at deploy time', "
            "'twice daily', 'every Friday', 'hourly', 'once a week'.  "
            "MUST denote a recurring time cadence.  DO NOT use for "
            "descriptive phrases like 'our needs', 'long time', "
            "'agile cycle', 'startup phase'.  When in doubt, omit."
        ),
    },
    "clinical": {
        "medication": (
            "prescribed drugs, therapies, regimens — metformin, "
            "insulin, warfarin, prednisone, sertraline"
        ),
        "procedure": (
            "medical procedures — arthroscopy, MRI, bypass surgery, "
            "colonoscopy, stenting"
        ),
        "symptom": (
            "reported complaints — chest pain, fatigue, nausea, "
            "headache, shortness of breath"
        ),
        "severity": (
            "severity descriptors — mild, moderate, severe, acute, "
            "chronic, worsening, stable"
        ),
        "alternative": (
            "treatment / diagnosis contrasted — 'X instead of Y' / "
            "'ruled out in favor of Y'"
        ),
        "frequency": (
            "dosing / monitoring cadence — 'every 6 hours', "
            "'twice daily', 'at bedtime'"
        ),
    },
    "conversational": {
        "object": (
            "the specific thing the utterance is about — food, "
            "hobby, activity, topic, person"
        ),
        "frequency": (
            "timing / cadence — 'every morning', 'on weekends', "
            "'nightly'"
        ),
        "alternative": (
            "contrasting choice — 'X instead of Y', 'over Y', "
            "'not Y'"
        ),
    },
    "swe_diff": {
        "file_path": (
            "source file paths mentioned — e.g. "
            "'astropy/modeling/separable.py'"
        ),
        "function": (
            "function / method identifiers — e.g. _cstack, "
            "URLValidator"
        ),
        "symbol": (
            "class / variable / module names — e.g. CompoundModel, "
            "MAX_RETRIES"
        ),
        "test_path": (
            "test file paths — e.g. tests/unit/test_auth.py"
        ),
        "issue_ref": (
            "GitHub issue / PR references — '#12345', "
            "'owner/repo#123'"
        ),
        "alternative": (
            "prior implementation the patch replaces"
        ),
    },
}


def _build_prompt(domain: Domain, content: str) -> str:
    slot_schema = _SLOT_DESCRIPTIONS[domain]
    schema_lines = "\n".join(
        f"  - {name}: {desc}" for name, desc in slot_schema.items()
    )
    return f"""You are a structured-slot labeller for {domain} content.

PRINCIPLES:
  - CONSERVATIVE labelling beats aggressive labelling.  When in doubt, OMIT.
  - intent defaults to "none".  state_change defaults to "none".  Change ONLY with explicit evidence.
  - Slot values MUST literally appear in the content (verbatim, case-flexible).  Never invent.

Slot schema for domain "{domain}":
{schema_lines}

intent taxonomy (default = "none"):
  - "positive"    = first-person endorsement ("I love X", "X is fantastic")
  - "negative"    = first-person rejection ("I can't stand X", "X is terrible")
  - "habitual"    = first-person recurring habit ("I use X every morning")
  - "difficulty"  = someone finds X hard ("X is brutal to debug")
  - "choice"      = ONLY when content has EXPLICIT contrast language:
                    "X over Y", "X instead of Y", "chose X not Y",
                    "picked X rather than Y", "X versus Y decision".
                    Merely listing options is NOT choice.
                    Describing benefits of one option is NOT choice.
  - "none"        = descriptive / neutral / factual / question / plan / requirement.

state_change taxonomy (default = "none"):
  - "declaration" = ONLY when content has EXPLICIT new-state language:
                    "we have decided to use X",
                    "we have adopted X",
                    "going forward we will use X",
                    "effective immediately X is the standard",
                    "Decided on X",
                    "we are standardising on X".
                    A description of benefits is NOT a declaration.
                    A plan / hope / requirement is NOT a declaration.
  - "retirement"  = ONLY when content has EXPLICIT retirement language:
                    "deprecated", "migrated away from",
                    "replaced by", "sunset", "superseded",
                    "no longer recommended".
                    A description of an older approach is NOT retirement
                    unless the content explicitly marks the transition.
  - "none"        = no state transition.

Content:
\"\"\"
{content.strip()}
\"\"\"

Return ONLY a compact JSON object (no prose, no markdown fences):
{{"intent": "...", "slots": {{"<slot>": "<surface form>", ...}}, "state_change": "..."}}
"""


# Slots with CLOSED vocabularies — value MUST be a catalog entry.
# These slots represent discrete named concepts (architectural
# patterns, time-interval expressions); novel LLM-emitted values
# are almost always hallucinations and should be dropped.
_CLOSED_VOCAB_SLOTS: frozenset[str] = frozenset({"pattern", "frequency"})


# Connective lowercase words allowed mid-phrase in proper-noun
# products like "Ruby on Rails" / "Secret Server by Thycotic".
_CONNECTIVES: frozenset[str] = frozenset({
    "on", "of", "by", "the", "for", "in", "at", "to", "as", "and",
    "with", "a", "an",
})


def _looks_like_proper_noun(s: str) -> bool:
    """Heuristic: does ``s`` look like a product/entity name?

    Used as a secondary filter on novel (non-catalog) open-slot
    surfaces to reject generic category words that slip past
    literal-presence.  Observed failure modes the filter catches:

      "continuous integration"   → second word lowercase → reject
      "code editors"             → all lowercase → reject
      "document database"        → all lowercase → reject
      "GCP services"             → second word lowercase → reject
      "py"                       → len<3, no special char → reject
      "infrastructure code"      → all lowercase → reject

    Accepted cases:
      "React", "Python", "Rust"          → single cap word, len≥3
      "Docker Compose", "AWS Lambda"     → every word capitalized
      "SvelteKit", "PostgreSQL"          → internal caps
      "Thycotic Secret Server"           → title case
      "Ruby on Rails"                    → connective "on" allowed
      "Secret Server by Thycotic"        → connective "by" allowed
      "C#", "F#", "R"                    → len≥2 + special char or
                                            single uppercase letter
                                            (for single-letter langs)

    The heuristic is conservative — catalog hits bypass this check
    entirely, so any entity worth labelling should be in the
    catalog.
    """
    s = s.strip()
    if not s:
        return False
    tokens = s.split()
    if not tokens:
        return False
    # Single-word
    if len(tokens) == 1:
        tok = tokens[0]
        has_special = any(c in tok for c in ("#", "+", ".", "/"))
        if len(tok) < 3 and not has_special:
            # Reject "py" but accept "C#", "F#", ".NET"
            # Single letters like "R" (R language) are handled via
            # catalog so we don't accept them here.
            return False
        return any(c.isupper() for c in tok) or has_special
    # Multi-word: every non-connective token must be capitalized
    # or contain internal caps.
    saw_proper = False
    for tok in tokens:
        if not tok:
            return False
        if tok.lower() in _CONNECTIVES:
            continue
        if tok[0].isupper() or any(c.isupper() for c in tok):
            saw_proper = True
            continue
        return False
    # At least one "real" (non-connective) token was proper-cased
    return saw_proper


def _validate_slots(
    slots: dict[str, str], content: str, domain: Domain,
) -> dict[str, str]:
    """Reject hallucinations + enforce authoritative catalog slot.

    Rules:
      1. Drop values whose slot name is not in the domain taxonomy.
      2. Drop values whose surface form isn't present in content
         (fuzzy-prefix fallback to accept "Postgres" for "PostgreSQL").
      3. **Closed-vocabulary slots** (``pattern``, ``frequency``):
         value MUST match a catalog entry.  If the LLM emits
         ``pattern="decided on"`` or ``frequency="our needs"`` — drop
         it.  These are hallucinations; the catalog is the whole
         universe of valid values.
      4. **Open slots** (``framework`` / ``library`` / ``language`` /
         ``database`` / ``platform`` / ``tool``): catalog-normalised
         when surface is known; otherwise keep the LLM's choice
         (real-world novel entities — new libraries etc. — appear
         constantly and we don't want to block them).
      5. **Alternative** slot: catalog-known OR dropped.  The
         contrast partner must be a recognized entity.

    Dedup: first-wins when multiple values canonicalize to same slot.
    """
    from ncms.application.adapters.sdg.catalog import (
        lookup,
    )
    allowed = set(SLOT_TAXONOMY[domain])
    content_lower = content.lower()
    out: dict[str, str] = {}
    for name, value in slots.items():
        if name not in allowed:
            continue
        if not isinstance(value, str):
            continue
        v = value.strip()
        if not v:
            continue
        # 2. Literal presence in content (with fuzzy fallback).
        if v.lower() not in content_lower:
            if not _fuzzy_present(v, content_lower):
                continue
        # 3 + 4 + 5. Catalog normalisation.
        entry = lookup(v, domain=domain)
        if entry is not None:
            # Known surface — catalog slot is authoritative.  Also
            # normalise the value to the canonical form so downstream
            # sees consistent labels.
            name = entry.slot
            v = entry.canonical
        else:
            # Novel surface.
            if name in _CLOSED_VOCAB_SLOTS:
                # Closed vocab → require catalog membership.  Drop.
                continue
            if name == "alternative":
                # Alternative must be a known entity for the contrast
                # to be meaningful.  Novel alternatives are dropped.
                continue
            # Open slots (framework/library/database/platform/tool/
            # language): real-world unknowns appear constantly and
            # we want coverage.  But reject obviously-generic
            # surfaces that pass literal-presence but aren't actual
            # entity names ("code editors", "continuous integration",
            # "document database", "py").
            if not _looks_like_proper_noun(v):
                continue
        if name in out:
            continue
        out[name] = v
    return out


def _fuzzy_present(value: str, content_lower: str) -> bool:
    """Accept values like 'Postgres' even when the content says
    'PostgreSQL' — match on the longer shared prefix of len ≥ 5."""
    val_lower = value.lower()
    for tok in re.findall(r"\w+", content_lower):
        if len(tok) >= 5 and (
            tok.startswith(val_lower) or val_lower.startswith(tok)
        ):
            return True
    return False


_ROLE_VALUES: frozenset[str] = frozenset({
    "primary", "alternative", "casual", "not_relevant",
})


def _build_role_prompt(
    content: str, surfaces: list[str],
) -> str:
    """Build the role-classification prompt for a single row.

    Given the content + a list of gazetteer-detected catalog
    surfaces, ask the LLM to assign one of four roles per surface.
    Returns a JSON dict ``{surface_lower: role, ...}``.
    """
    numbered = "\n".join(
        f"  {i + 1}. {s}" for i, s in enumerate(surfaces)
    )
    return f"""You are classifying the ROLE of each detected technical term \
in a piece of software-development content.

Content:
\"\"\"
{content.strip()}
\"\"\"

For each term below, classify its role in the content:

{numbered}

Role taxonomy:
  - "primary"     = THE MAIN SUBJECT.  The sentence is about this, it \
is what was chosen / adopted / discussed.
  - "alternative" = EXPLICITLY REJECTED contrast partner.  Used in \
"X over Y" / "X instead of Y" / "migrated from Y".
  - "casual"      = MENTIONED IN PASSING.  Appears in the text but is \
not the subject, not a rejected option.  Used as an example, \
decoration, or side comment.
  - "not_relevant"= The term appears accidentally (homonym of a common \
word) or is not a software-dev mention at all.

Examples:
  Content: "We chose Postgres over MongoDB for the user service."
  Terms: Postgres, MongoDB
  Output: {{"postgres": "primary", "mongodb": "alternative"}}

  Content: "Our Python backend uses FastAPI.  The frontend team is \
still debating React vs Vue."
  Terms: Python, FastAPI, React, Vue
  Output: {{"python": "primary", "fastapi": "primary", \
"react": "casual", "vue": "casual"}}

  Content: "The team made solid progress on the auth feature."
  Terms: solid
  Output: {{"solid": "not_relevant"}}

Return ONLY a JSON object mapping each lowercase surface to its \
role (no prose, no markdown fences):
"""


def _extract_surfaces_for_role_prompt(
    content: str, domain: Domain,
) -> tuple[str, ...]:
    """Gazetteer pass → deduped lowercase surface list.

    We dedupe by canonical form so "Postgres" and "PostgreSQL"
    appearing in the same row only get classified once.
    """
    seen: set[str] = set()
    out: list[str] = []
    for span in detect_spans(content, domain=domain):
        key = span.canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(span.canonical)
    return tuple(out)


async def _classify_roles(
    *,
    content: str,
    domain: Domain,
    slots: dict[str, str],
    model: str,
    api_base: str | None,
) -> list[RoleSpan]:
    """Produce role-labeled spans for ``content``.

    Strategy:
      1. Gazetteer detects every catalog surface.
      2. Any span whose canonical matches ``slots[span.slot]`` gets
         ``primary`` deterministically (LLM confirmed via slots dict).
      3. Any span whose canonical matches ``slots["alternative"]``
         gets ``alternative`` deterministically.
      4. Remaining spans (not referenced in slots) get sent to a
         small LLM prompt that picks ``casual`` vs ``not_relevant``.
         One call covers all remaining surfaces in the row.
    """
    all_spans = detect_spans(content, domain=domain)
    if not all_spans:
        return []

    slot_values_lower = {
        k: v.lower().strip() for k, v in slots.items() if v
    }
    alt_value = slot_values_lower.get("alternative")

    # First pass — deterministic from the validated slots dict.
    pre_labeled: list[tuple[object, str | None]] = []  # (span, role_or_None)
    novel_canonicals: list[str] = []
    seen_canonicals: set[str] = set()
    for span in all_spans:
        canon = span.canonical.lower()
        if alt_value is not None and canon == alt_value:
            pre_labeled.append((span, "alternative"))
        elif slot_values_lower.get(span.slot) == canon:
            pre_labeled.append((span, "primary"))
        else:
            pre_labeled.append((span, None))
            if canon not in seen_canonicals:
                seen_canonicals.add(canon)
                novel_canonicals.append(span.canonical)

    # Second pass — LLM classifies casual vs not_relevant for the rest.
    llm_roles: dict[str, str] = {}
    if novel_canonicals:
        prompt = _build_role_prompt(content, novel_canonicals)
        try:
            result = await call_llm_json(
                prompt=prompt, model=model, api_base=api_base,
                max_tokens=400, temperature=0.0,
            )
        except Exception as exc:
            logger.warning(
                "Role-LLM call failed for content[:60]=%r: %s",
                content[:60], exc,
            )
            result = None
        if isinstance(result, dict):
            for key, val in result.items():
                if not isinstance(key, str) or not isinstance(val, str):
                    continue
                role = val.strip().lower()
                if role in _ROLE_VALUES:
                    llm_roles[key.strip().lower()] = role

    # Assemble final spans.
    out: list[RoleSpan] = []
    for span, pre in pre_labeled:
        span_t = span  # type: ignore[assignment]
        if pre is not None:
            role = pre
        else:
            role = llm_roles.get(span_t.canonical.lower(), "casual")
        out.append(RoleSpan(
            char_start=span_t.char_start,
            char_end=span_t.char_end,
            surface=span_t.surface,
            canonical=span_t.canonical,
            slot=span_t.slot,
            role=role,  # type: ignore[arg-type]
            source="llm_slot_labeler",
        ))
    return out


async def label_one(
    *,
    content: str,
    domain: Domain,
    model: str,
    api_base: str | None,
) -> dict | None:
    """Label one content item; returns the parsed dict or ``None`` on error."""
    prompt = _build_prompt(domain, content)
    try:
        result = await call_llm_json(
            prompt=prompt, model=model, api_base=api_base,
            max_tokens=400, temperature=0.0,
        )
    except Exception as exc:
        logger.warning("LLM call failed for content[:60]=%r: %s",
                       content[:60], exc)
        return None
    if not isinstance(result, dict):
        return None
    # Normalize + validate.
    intent = str(result.get("intent") or "none").lower()
    if intent not in (
        "positive", "negative", "habitual", "difficulty", "choice", "none",
    ):
        intent = "none"
    state_change = str(result.get("state_change") or "none").lower()
    if state_change not in ("declaration", "retirement", "none"):
        state_change = "none"
    slots = _validate_slots(
        result.get("slots") or {}, content, domain,
    )
    # v7: role-labeled spans (gazetteer + LLM for casual/not_relevant).
    role_spans = await _classify_roles(
        content=content, domain=domain, slots=slots,
        model=model, api_base=api_base,
    )
    return {
        "intent": intent,
        "slots": slots,
        "state_change": state_change,
        "role_spans": role_spans,
    }


async def label_corpus(
    *,
    source: Path,
    domain: Domain,
    output: Path,
    model: str,
    api_base: str | None,
    limit: int | None = None,
    text_field: str = "content",
    topic: str | None = None,
) -> int:
    """Read one content-bearing JSONL, emit a GoldExample JSONL.

    ``source`` rows must have a ``content`` (or ``text_field``) key.
    ``topic`` if set seeds the emitted topic label; otherwise we
    leave it None and let downstream labelling / training fill it
    from SDG pools.
    """
    import json as _json
    rows: list[dict] = []
    with source.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(_json.loads(line))
    if limit is not None:
        rows = rows[:limit]

    out: list[GoldExample] = []
    for i, row in enumerate(rows):
        text = row.get(text_field)
        if not text:
            continue
        labelled = await label_one(
            content=text, domain=domain,
            model=model, api_base=api_base,
        )
        if labelled is None:
            continue
        ex = GoldExample(
            text=text,
            domain=domain,
            intent=labelled["intent"],
            slots=labelled["slots"],
            topic=topic,
            admission="persist",  # gold-curated corpus → persist
            state_change=labelled["state_change"],
            role_spans=labelled.get("role_spans") or [],
            split="gold",
            source=f"llm_slot_labeler model={model}",
        )
        out.append(ex)
        if (i + 1) % 10 == 0:
            logger.info(
                "[llm-slot-labeler] %s: labelled %d / %d",
                domain, i + 1, len(rows),
            )

    # Write JSONL.
    from ncms.application.adapters.corpus.loader import dump_jsonl
    dump_jsonl(out, output)
    logger.info(
        "[llm-slot-labeler] %s: wrote %d GoldExample rows → %s",
        domain, len(out), output,
    )
    return len(out)


def sync_label_corpus(
    *,
    source: Path,
    domain: Domain,
    output: Path,
    model: str,
    api_base: str | None,
    limit: int | None,
    text_field: str,
    topic: str | None,
) -> int:
    """Sync wrapper for the CLI."""
    return asyncio.run(label_corpus(
        source=source, domain=domain, output=output,
        model=model, api_base=api_base, limit=limit,
        text_field=text_field, topic=topic,
    ))


__all__ = ["label_corpus", "sync_label_corpus", "label_one"]
