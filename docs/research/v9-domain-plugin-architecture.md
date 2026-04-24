# v9 Domain Plugin Architecture

> Status: **design + active refactor** (Phase B'.0b.5).
> v9 lands this pattern; v10+ assumes it.

## Problem

Pre-v9 NCMS had ad-hoc handling for each of its four SLM domains
(`conversational`, `clinical`, `software_dev`, `swe_diff`).  Each
domain's definition was scattered across:

  * `src/ncms/application/adapters/schemas.py::SLOT_TAXONOMY` — hand-
    written slot lists per domain
  * `src/ncms/application/adapters/schemas.py::DOMAIN_MANIFESTS` —
    hand-written path + adapter-root bindings per domain
  * `adapters/taxonomies/<domain>.yaml` — topic labels + admission +
    state_change vocabs for that domain
  * `src/ncms/application/adapters/sdg/catalog/<domain>.py` — Python
    module with inline `CatalogEntry` lists (software_dev only — the
    others never got catalogs)
  * `src/ncms/application/adapters/sdg/templates.py::SOFTWARE_DEV_TEMPLATES`
    etc. — per-domain SDG template pools

Adding a new domain meant editing six places + hoping you got the
naming consistent.  Removing `swe_diff` in Phase B'.0a touched 11
files.  Users wanting their own domain had no extension point — they
had to fork and patch the package.

## Goal

**A domain is one directory**.  Drop it in, NCMS picks it up.

```
adapters/domains/<domain_name>/
  domain.yaml         # required — slots, topics, metadata, file refs
  gazetteer.yaml      # optional — inference-time lookup catalog
  diversity.yaml      # required — generator-time type taxonomy
  archetypes.yaml     # required — stratified generation archetypes
  phrasings/          # optional — per-archetype surface banks
    <archetype>.txt
```

Core domains (`conversational`, `clinical`, `software_dev`) ship
alongside the package in the same layout.  No special casing.

## Design principle: one job per YAML file

Each YAML file has one responsibility.  When a new requirement
arrives, resist the urge to add a feature to a file already
doing something else.

| File | Single responsibility |
|---|---|
| `gazetteer.yaml` | Enumerable entities + intrinsic attributes (slot, topic, aliases, source).  Ground truth for `detect_spans` at inference. |
| `diversity.yaml` | **Breadth** for the generator — named pools grouped by whatever natural structure the domain offers.  For gazetteer-backed domains: one pool per slot.  For open-vocab domains: hierarchical by type (foods/cuisines, foods/dishes) since the flat `slot=object` can't partition further. |
| `archetypes.yaml` | Joint labels + **semantic context**.  Specialty differentiation ("cardiovascular note" vs "diabetes management") belongs here, in the archetype's `description` + `example_utterances` + `phrasings` — NOT in diversity filter metadata. |

Anti-pattern: partitioning entities by specialty inside
diversity.yaml via filter metadata (tags, specialty tags,
sub-slot filters).  That conflates breadth-of-pool with
semantic-context.  Instead: one pool node per structural
partition (slot), and multiple archetypes per specialty
each using the same broad pool with specialty-aware prompts.

## Separation of concerns: gazetteer vs diversity taxonomy

Two different jobs, currently conflated:

**Gazetteer catalog** (inference-time):
- Purpose: `detect_spans("we migrated to postgres")` → `{postgres: database}`
- Required when: slot vocabulary is **closed or closed-ish** (software
  frameworks, medications, procedures — enumerable)
- Optional when: slot vocabulary is **open** (conversational `object`
  could be any noun — gazetteer would be incomplete + bias training
  toward its narrow set)
- Used by: the role head (via `catalog.detect_spans`) at inference

**Diversity taxonomy** (generation-time):
- Purpose: steer the LLM generator to produce diverse training rows
  across all expected entity types
- Required for: every domain (otherwise generation skews toward
  whatever's easiest for the LLM — pizza/coffee/tennis 100 times)
- Structure: hierarchical types (top-level + subcategories) with
  example members per subcategory
- Used by: the v9 corpus generator at training-data-creation time

Domain shapes:

| Domain | Gazetteer? | Diversity taxonomy? | Why |
|---|---|---|---|
| software_dev | Yes (760 entries) | Yes (groups existing gazetteer) | Closed-ish tech vocab |
| clinical | Yes (~500 entries) | Yes (specialties + care settings) | Closed-ish clinical vocab |
| conversational | **No** | Yes (~1000 entries across types) | Open-vocab objects |

## File schemas

### `domain.yaml` — required

```yaml
name: software_dev                  # must match directory name
description: "One-liner for `ncms adapters list` / human docs."
intended_content: |
  Multi-line prose describing what content this domain is for.
  Read by humans (docs), not code.

slots:
  - language
  - framework
  - library
  - database
  - platform
  - tool
  - pattern
  - alternative              # "X over Y" role placeholder — standard
  - frequency                # "every commit" role placeholder — standard

# Per-domain topic taxonomy (topic_head vocabulary).
topics:
  - framework
  - language_runtime
  - infra
  - testing
  - tooling
  - package_mgmt
  - editor
  - other

# Optional file references (defaults to these names next to domain.yaml):
gazetteer_path: gazetteer.yaml        # optional, omit for open-vocab
diversity_path: diversity.yaml        # required
archetypes_path: archetypes.yaml      # required

# Optional adapter-training paths.  Loader fills in defaults when omitted:
#   gold:        adapters/corpora/v9/<name>/gold.jsonl
#   sdg:         adapters/corpora/v9/<name>/sdg.jsonl
#   adversarial: adapters/corpora/v9/<name>/adv.jsonl
#   output:      adapters/checkpoints/<name>/
#   deployed:    ~/.ncms/adapters/<name>/
paths:
  gold_jsonl: adapters/corpora/v9/software_dev/gold.jsonl
  sdg_jsonl:  adapters/corpora/v9/software_dev/sdg.jsonl

default_adapter_version: v9
```

### `gazetteer.yaml` — optional

```yaml
# Flat list; loader groups by slot internally.  Every entry must have
# canonical + slot + topic + source.  Aliases optional.
entries:
  - canonical: postgres
    slot: database
    topic: infra
    aliases: [postgresql, postgres sql, pg]
    source: wikidata:Q192490
    notes: ""                  # optional reviewer notes
  - canonical: rust
    slot: language
    topic: language_runtime
    aliases: []
    source: wikidata:Q575650
```

### `diversity.yaml` — required

Two modes for populating the sampler — `from_gazetteer` (software_dev,
clinical) or `inline` (conversational).  A node can mix:

```yaml
# software_dev/diversity.yaml  — gazetteer-backed
frameworks_and_libs:
  description: "Web frameworks, app frameworks, imported libraries."
  topic_hint: framework
  from_gazetteer:
    filter_slots: [framework, library]
  n_examples_per_batch: 8     # how many entries to rotate through per
                              # generator batch

languages_and_runtimes:
  description: "Programming languages + runtime-level patterns."
  topic_hint: language_runtime
  from_gazetteer:
    filter_slots: [language]
  n_examples_per_batch: 6
```

```yaml
# conversational/diversity.yaml  — fully inline
people:
  celebrities:
    description: "Pop-culture figures users express preferences about."
    topic_hint: entertainment_pref
    source: inline
    examples:
      - Taylor Swift
      - Kendrick Lamar
      - Zendaya
      # ~30 per subcategory

foods:
  cuisines:
    topic_hint: food_pref
    source: inline
    examples: [Italian, Japanese, Oaxacan, ...]
  dishes:
    topic_hint: food_pref
    source: inline
    examples: [carbonara, bibimbap, pho, ...]

activities:
  sports:
    topic_hint: sports_pref
    source: inline
    examples: [bouldering, pickleball, gravel cycling, ...]
```

Hybrid (some nodes gazetteer-backed, others inline) is allowed.

### `archetypes.yaml` — required

One file per domain, listing the stratified generation archetypes
(see `docs/research/v9-corpus-generation-design.md` for archetype
semantics).  YAML-native version of the Python `ArchetypeSpec`
dataclass:

```yaml
archetypes:
  - name: positive_adoption_with_alternative
    intent: positive
    admission: persist
    state_change: declaration
    topic: null                       # null = sample from domain.topics
    role_spans:
      - {role: primary, slot: framework, count: 1}
      - {role: alternative, slot: framework, count: 1}
    n_gold: 40
    n_sdg: 150
    target_min_chars: 30
    target_max_chars: 160
    batch_size: 10
    description: "User adopted a new framework over an alternative."
    example_utterances:
      - "We moved from Django to FastAPI for the new service."
      - "Decided on Next.js over Remix for the marketing site."
    # Phrasings can be inline or loaded from phrasings/<name>.txt
    phrasings_path: phrasings/positive_adoption.txt

  - name: neutral_observation
    intent: none
    admission: persist
    state_change: none
    role_spans:
      - {role: casual, slot: framework, count: 1}
    # ...
```

## Python loading model

```python
# Public API
from ncms.application.adapters.domain_loader import (
    DomainSpec,        # dataclass — the loaded, validated domain
    load_domain,       # (dir: Path) -> DomainSpec
    load_all_domains,  # (root: Path) -> dict[str, DomainSpec]
)

spec = load_domain(Path("adapters/domains/software_dev"))

spec.name                # "software_dev"
spec.slots               # ("language", "framework", ...)
spec.topics              # ("framework", "language_runtime", ...)
spec.gazetteer           # CatalogEntry tuple (empty when no gazetteer)
spec.diversity           # DiversityTaxonomy (hierarchical)
spec.archetypes          # tuple[ArchetypeSpec, ...]
spec.gold_jsonl_path     # Path
spec.adapter_output_root # Path
```

`load_all_domains()` is called once at package import time from
`schemas.py`; it populates the legacy `SLOT_TAXONOMY` and
`DOMAIN_MANIFESTS` registries from the YAML specs so existing code
paths keep working without modification.

Validation happens at load time:
- Every archetype's `role_spans` slot must exist in `domain.slots`
- Every `topic_hint` in diversity must exist in `domain.topics`
- Every gazetteer entry's slot must exist in `domain.slots`
- Archetypes must pass `validate_archetype_coverage` per-head floors
- Gazetteer canonicals must be unique

Invalid domains fail loudly at import — better than silently
wrong training runs.

## Migration path

1. **Write loader + validation + tests** (this doc + code, Phase B'.0b.5a)
2. **Migrate software_dev**: port the Python catalog module to
   `adapters/domains/software_dev/gazetteer.yaml`; build the matching
   `diversity.yaml` as a thin wrapper around the gazetteer groups;
   verify coverage audit unchanged (Phase B'.0b.5b)
3. **Build conversational natively**: no gazetteer, just diversity +
   archetypes (Phase B'.0b.5c)
4. **Build clinical natively**: gazetteer (curated RxNorm / ICD-10
   subsets) + diversity + archetypes (Phase B'.0b.5d)
5. **Delete legacy paths**: remove `sdg/catalog/software_dev.py`
   Python module, remove `SLOT_TAXONOMY` + `DOMAIN_MANIFESTS` inline
   dicts (they're auto-populated now), remove
   `sdg/templates.py::TEMPLATE_REGISTRY` (archetypes replace it)
6. **Write "How to add a custom domain" tutorial** pointing at the
   three built-in domains as examples (Phase D')

## What this enables

Immediate:
- v9 corpus generator is **one code path** that iterates
  `load_all_domains()` — no per-domain branching
- Coverage audit already works uniformly; just needs to read from
  DomainSpec instead of hardcoded paths
- Tests can build synthetic DomainSpecs in fixtures without mocking
  imports

Future:
- User-defined domains: drop a directory, run
  `ncms adapters validate --domain mydomain`, run
  `ncms adapters generate --domain mydomain`
- Community-shared domain packs: a domain can be published as a
  standalone repo + `pip install ncms-domain-finance` that drops
  a directory in
- Multi-tenant deployments: different clients get different domain
  registries without code changes

## Out of scope for v9

- Full dynamic `Domain = Literal[...]` runtime expansion.  The
  `Literal` stays fixed to the 3 core domains for type-safety in
  existing code paths.  User-defined domains get registered but
  pass through `Domain = "user_defined"` or similar.  Phase D' can
  consider fully dynamic if real users want it.
- Schema evolution / versioning.  The YAML schema is v1; changes
  get a `schema_version: 2` field + a migration pass later.
- Validation severity levels — everything is a hard error for now.
  If the diversity of real user domains surfaces soft-warning needs,
  add them when concrete.
