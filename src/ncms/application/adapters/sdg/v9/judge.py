"""v9 corpus quality gate — content-fidelity LLM judge.

Phase B'.5 of the v9 plan.  Samples rows from a generated
:class:`GoldExample` corpus (written by :func:`generate_domain`)
and asks a judge LLM **whether the row's text faithfully expresses
the archetype's scenario** — NOT whether the labels are right.

Why content-fidelity, not label-correctness:

For SDG corpora the labels are *archetype-determined by
construction*.  Every row from ``positive_medication_start``
carries ``intent=positive, admission=persist,
state_change=declaration`` BY DESIGN.  ``role_spans`` are
deterministically assigned from the archetype's
``role_spans:`` declaration plus the gazetteer scan.  Asking
"is the intent label right for this prose?" is reverse-
engineering the SDG pipeline — and a judge that does that
flags every row whose archetype intentionally puts a
temporal-marker entity into a primary role (e.g. clinical
``positive_medication_start`` deliberately marks BOTH
medication AND frequency as primary so the role head learns
both).  An earlier draft of this prompt graded labels and
returned 0% / 8% / 17% pct_correct across the three domains —
all false positives caused by the prompt-design mismatch.

What the judge actually verifies:

* **Required entities are present.**  The archetype told the
  generator to include specific surfaces; the row must mention
  every one.
* **Scenario fidelity.**  The archetype's description names a
  scenario ("clinician starts a patient on a new medication");
  the prose must express that scenario, not a different one.
* **Coherence.**  The text reads as natural prose; not nonsense,
  not contradictory, not template-leaking.
* **Stylistic register.**  Clinical archetype → clinical voice;
  conversational → casual first-person; software_dev →
  engineering-team voice.

A row's verdict is ``faithful`` when all four checks pass,
``partial`` when one minor issue surfaces but the row is still
recognisable as the archetype's scenario, ``unfaithful`` when
the row contradicts the archetype or is incoherent.

The judge is offline / manually triggered via
``ncms adapters judge-v9``.  Output is a per-domain summary
plus a ``failures`` list the operator reviews before committing
corpora.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ncms.application.adapters.corpus.loader import load_jsonl
from ncms.application.adapters.schemas import Domain, GoldExample
from ncms.infrastructure.llm.caller import call_llm_json

if TYPE_CHECKING:
    from ncms.application.adapters.sdg.v9.archetypes import ArchetypeSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_JUDGE_PROMPT = """You are grading whether a generated training row faithfully expresses
its ARCHETYPE'S SCENARIO.  You are NOT grading the labels — the labels are
deterministic outputs of the generator pipeline (archetype + gazetteer) and
are correct by construction.  You are judging the TEXT.

ARCHETYPE: {archetype_name}

SCENARIO (the row must clearly express ALL of this):
{archetype_description}

REFERENCE EXAMPLES (style + scenario, not exact wording):
{reference_examples}

REQUIRED ENTITIES (every one must appear in the text, inflected naturally):
{required_entities}

GENERATED ROW:
\"\"\"{content}\"\"\"

Your four checks (in order):

CHECK 1 — Entity presence.
  Does every required entity surface above appear in the row, allowing
  for natural inflection (e.g. "metformin" → "metformin 500mg")?  An
  entity that's MISSING or REPLACED with a different one is a fail.

CHECK 2 — Scenario fidelity.
  Does the prose clearly express the archetype's scenario described
  above?  A row that uses the right entity but tells a different story
  (e.g. discontinuation when the archetype is initiation) fails this
  check.

CHECK 3 — Coherence.
  Is the text natural, internally consistent, and free of placeholder
  leaks (literal ``{{name}}`` tokens) or contradictory clauses?

CHECK 4 — Register / domain voice.
  Clinical → clinician-note voice (third-person, observational).
  Conversational → casual first-person preference.
  Software_dev → engineering-team voice (we / the team / our).
  A row that mixes registers (e.g. casual chitchat in a clinical row)
  fails this check.

VERDICT:
  - "faithful":  every check passes.  The row is a clean exemplar of
                 the archetype.
  - "partial":   one minor issue (awkward phrasing, weak signal,
                 register slip) but the archetype's scenario is still
                 recognisable.
  - "unfaithful": at least one check fails badly — required entity
                  missing, wrong scenario, incoherent text, wrong
                  register.

Return ONLY a JSON object — no prose, no markdown fences:
{{
  "verdict": "faithful" | "partial" | "unfaithful",
  "issues": ["short one-line description of each problem; empty list if faithful"],
  "failed_checks": ["entity_presence" | "scenario_fidelity" | "coherence" | "register", ...]
}}
"""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DomainJudgeResult:
    """Aggregated judging output for one domain corpus.

    The verdict vocabulary is ``faithful / partial / unfaithful`` —
    grading content fidelity to the archetype's scenario, not label
    correctness.  ``pct_faithful`` is the headline figure;
    ``failed_check_counts`` breaks down WHICH of the four checks
    (entity_presence / scenario_fidelity / coherence / register)
    fails most often, so operators can target prompt or archetype
    fixes instead of treating every failure as the same.
    """

    domain: Domain
    n_sampled: int
    verdicts: dict[str, int] = field(default_factory=dict)
    pct_faithful: float = 0.0
    failed_check_counts: dict[str, int] = field(default_factory=dict)
    per_archetype: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    # Backward-compat alias — older callers / docs reference
    # ``pct_correct``.  Maps to the same value.
    @property
    def pct_correct(self) -> float:
        return self.pct_faithful

    def as_dict(self) -> dict:
        return {
            "domain": self.domain,
            "n_sampled": self.n_sampled,
            "verdicts": self.verdicts,
            "pct_faithful": self.pct_faithful,
            "failed_check_counts": self.failed_check_counts,
            "per_archetype": self.per_archetype,
            "failures": self.failures,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_VALID_VERDICTS = ("faithful", "partial", "unfaithful")
_VALID_CHECKS = (
    "entity_presence",
    "scenario_fidelity",
    "coherence",
    "register",
)


def _required_entities_block(ex: GoldExample) -> str:
    """Render the row's role_spans as a required-entity checklist.

    We pull surfaces from ``ex.role_spans`` (which are populated
    by the generator from the gazetteer / open-vocab fallback), not
    from ``ex.slots`` — role_spans preserve the role context
    (primary / alternative / casual) so the judge sees how each
    surface is meant to be used in the row's scenario.
    Skips ``not_relevant`` spans (those are unrequested gazetteer
    hits, not archetype-required surfaces).
    """
    required = [rs for rs in (ex.role_spans or []) if rs.role != "not_relevant"]
    if not required:
        return "(no required entities — judge purely on scenario fit)"
    lines = []
    for rs in required:
        lines.append(f"  - {rs.surface!r} (role={rs.role}, slot={rs.slot})")
    return "\n".join(lines)


def _archetype_of(ex: GoldExample) -> str:
    """Extract the archetype name from the source-provenance string.

    Format: ``"sdg-v9 archetype=<name> seed=<n>"``.  Falls back to
    the literal source string when the format doesn't match.
    """
    src = ex.source or ""
    marker = "archetype="
    idx = src.find(marker)
    if idx < 0:
        return src or "unknown"
    tail = src[idx + len(marker) :]
    end = tail.find(" ")
    return tail if end < 0 else tail[:end]


def _archetype_for_row(
    ex: GoldExample,
    archetype_lookup: dict,
) -> ArchetypeSpec | None:
    """Resolve the row's :class:`ArchetypeSpec` from its source string.

    Returns None when the archetype name in the row's source isn't
    known to the spec — judge then has to fall back to a generic
    "no archetype context" prompt.
    """
    return archetype_lookup.get(_archetype_of(ex))


async def _judge_one(
    ex: GoldExample,
    *,
    archetype_lookup: dict,
    model: str,
    api_base: str | None,
) -> dict | None:
    """Call the judge LLM on one row; return parsed verdict or None.

    The prompt embeds the row's archetype's description + reference
    examples so the judge has the same "what does this scenario
    look like?" context the generator had.  Without that context,
    the judge would have to re-derive intent from prose — which is
    exactly the failure mode that produced the 0% / 8% / 17%
    pct_correct false-positive sweep on an earlier draft of this
    prompt.
    """
    arch = _archetype_for_row(ex, archetype_lookup)
    if arch is None:
        # Unknown archetype — skip rather than judging blind.  The
        # caller's per-archetype bucket will count this in ``failed``.
        logger.warning(
            "v9 judge: row archetype %r not in spec; skipping",
            _archetype_of(ex),
        )
        return None

    refs = "\n".join(
        f"  - {ex_text.strip()}"
        for ex_text in (arch.example_utterances[:3] or ("(none provided)",))
    )
    prompt = _JUDGE_PROMPT.format(
        archetype_name=arch.name,
        archetype_description=arch.description.strip(),
        reference_examples=refs,
        required_entities=_required_entities_block(ex),
        content=ex.text.strip()[:800],
    )
    try:
        result = await call_llm_json(
            prompt=prompt,
            model=model,
            api_base=api_base,
            max_tokens=400,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — judge non-fatal
        logger.warning(
            "v9 judge failed on text[:60]=%r: %s",
            ex.text[:60],
            exc,
        )
        return None
    if not isinstance(result, dict):
        logger.warning(
            "v9 judge returned non-dict: %r",
            type(result).__name__,
        )
        return None
    verdict = str(result.get("verdict") or "").lower()
    if verdict not in _VALID_VERDICTS:
        # Unclassifiable → treat as unfaithful (conservative).
        verdict = "unfaithful"
    failed_checks = [
        str(c).lower()
        for c in (result.get("failed_checks") or [])
        if isinstance(c, str) and str(c).lower() in _VALID_CHECKS
    ]
    return {
        "verdict": verdict,
        "issues": [str(i) for i in (result.get("issues") or [])],
        "failed_checks": failed_checks,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def judge_corpus(
    *,
    domain: Domain,
    corpus_path: Path,
    archetype_lookup: dict,
    n_samples: int,
    model: str,
    api_base: str | None,
    seed: int = 42,
    stratified: bool = True,
) -> DomainJudgeResult:
    """Judge ``n_samples`` rows from ``corpus_path`` for content fidelity.

    When ``stratified`` is true (default), sampling is balanced
    across archetypes — pick ``ceil(n_samples / num_archetypes)``
    rows per archetype so under-represented archetypes still get
    judged.  When false, uniform random across the full file.

    Parameters
    ----------
    domain
        Domain name, used for reporting.
    corpus_path
        JSONL file produced by :func:`generate_domain`.
    archetype_lookup
        Map of ``archetype.name → ArchetypeSpec`` from
        ``DomainSpec.archetypes``.  The judge embeds each row's
        archetype description + reference examples into the prompt
        so the verdict is content-fidelity, not label-correctness.
    n_samples
        Total rows to judge.  Actual count can be lower when
        ``corpus_path`` has fewer rows.
    model, api_base
        litellm args for the judge LLM.  Recommend a DIFFERENT
        model from whatever generated the corpus — using the same
        model for generate + judge means any systemic blind spot
        is invisible.
    seed
        Sampling seed for reproducibility.
    stratified
        When true, balance sample count per archetype.
    """
    if not corpus_path.is_file():
        raise FileNotFoundError(f"corpus file not found: {corpus_path}")
    rows = load_jsonl(corpus_path)
    rows = [r for r in rows if r.text.strip()]
    if not rows:
        return DomainJudgeResult(
            domain=domain,
            n_sampled=0,
            pct_faithful=0.0,
        )

    rng = random.Random(seed)
    if stratified:
        by_arch: dict[str, list[GoldExample]] = {}
        for r in rows:
            by_arch.setdefault(_archetype_of(r), []).append(r)
        per_arch_target = max(1, n_samples // max(1, len(by_arch)))
        sample: list[GoldExample] = []
        for arch_rows in by_arch.values():
            sample.extend(
                rng.sample(arch_rows, min(per_arch_target, len(arch_rows))),
            )
        # Trim to n_samples if stratification overshot.
        if len(sample) > n_samples:
            rng.shuffle(sample)
            sample = sample[:n_samples]
    else:
        sample = rng.sample(rows, min(n_samples, len(rows)))

    result = DomainJudgeResult(
        domain=domain,
        n_sampled=len(sample),
    )
    result.verdicts = {v: 0 for v in _VALID_VERDICTS}

    for i, ex in enumerate(sample, 1):
        arch_name = _archetype_of(ex)
        arch_bucket = result.per_archetype.setdefault(
            arch_name,
            {**{v: 0 for v in _VALID_VERDICTS}, "failed": 0},
        )
        j = await _judge_one(
            ex,
            archetype_lookup=archetype_lookup,
            model=model,
            api_base=api_base,
        )
        if j is None:
            # Judge LLM unavailable / unknown archetype — count as
            # unfaithful but record in a separate "failed" bucket
            # so the operator can distinguish judge errors from
            # genuinely-bad rows.
            result.verdicts["unfaithful"] += 1
            arch_bucket["failed"] += 1
            continue
        result.verdicts[j["verdict"]] += 1
        arch_bucket[j["verdict"]] += 1
        for c in j["failed_checks"]:
            result.failed_check_counts[c] = result.failed_check_counts.get(c, 0) + 1
        if j["verdict"] != "faithful":
            result.failures.append(
                {
                    "text": ex.text[:200],
                    "archetype": arch_name,
                    "labels": {
                        "intent": ex.intent,
                        "admission": ex.admission,
                        "state_change": ex.state_change,
                        "topic": ex.topic,
                        "slots": ex.slots,
                        "role_spans": _role_spans_summary_from_ex(ex),
                    },
                    "verdict": j["verdict"],
                    "issues": j["issues"],
                    "failed_checks": j["failed_checks"],
                }
            )
        if i % 20 == 0:
            logger.info(
                "[v9 judge] %s: judged %d/%d (faithful=%d)",
                domain,
                i,
                len(sample),
                result.verdicts["faithful"],
            )

    total = sum(result.verdicts.values()) or 1
    result.pct_faithful = 100.0 * result.verdicts["faithful"] / total
    return result


def _role_spans_summary_from_ex(ex: GoldExample) -> str:
    """Compact role-spans repr for the failures list (cosmetic)."""
    if not ex.role_spans:
        return "(none)"
    return "[" + ", ".join(f"{rs.role}:{rs.slot}={rs.canonical!r}" for rs in ex.role_spans) + "]"


def sync_judge_corpus(**kwargs) -> DomainJudgeResult:
    """Sync wrapper for :func:`judge_corpus`."""
    return asyncio.run(judge_corpus(**kwargs))


def format_report(result: DomainJudgeResult) -> str:
    """Render a short human-readable report of ``result``."""
    lines: list[str] = []
    lines.append(f"=== v9 judge: domain={result.domain} sampled={result.n_sampled} ===")
    lines.append(f"  pct_faithful: {result.pct_faithful:.1f}%")
    lines.append(f"  verdicts:     {result.verdicts}")
    if result.failed_check_counts:
        lines.append(
            "  failed checks: "
            f"{dict(sorted(result.failed_check_counts.items(), key=lambda x: -x[1]))}",
        )
    lines.append("  per-archetype:")
    for arch, counts in sorted(result.per_archetype.items()):
        total_arch = sum(counts.values()) or 1
        pct = 100.0 * counts.get("faithful", 0) / total_arch
        lines.append(
            f"    · {arch:40s} faithful={pct:5.1f}%  {counts}",
        )
    if result.failures:
        lines.append(
            f"  sample failures ({len(result.failures)} total, showing first 5):",
        )
        for f in result.failures[:5]:
            lines.append(
                f"    [{f['verdict']}] {f['archetype']}: {f['text'][:100]}…",
            )
            if f["issues"]:
                lines.append(f"      issues: {f['issues']}")
    return "\n".join(lines)


__all__ = [
    "DomainJudgeResult",
    "format_report",
    "judge_corpus",
    "sync_judge_corpus",
]
