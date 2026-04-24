"""v9 corpus quality gate — LLM-as-judge across all five heads.

Phase B'.5 of the v9 plan.  Samples rows from a generated
:class:`GoldExample` corpus (written by :func:`generate_domain`)
and asks a judge LLM to grade each row against:

* **intent** — does the speaker's stance match the labelled intent?
* **admission** — does the content's persistence level match
  (persist / ephemeral / discard)?
* **state_change** — is a new-state / retirement / ongoing label
  faithful to the text?
* **topic** — does the topic label match the content domain?
* **role_spans** — do the role assignments (primary / alternative /
  casual / not_relevant) match how the entity is actually used?

Distinct from the legacy :mod:`corpus.gold_judge`: that module
grades intent + state_change + slots (three heads).  v9 has five
heads and role-labelled spans, so a v9-specific judge is cleaner
than stretching the three-head prompt.

The judge is offline / manually triggered via
``ncms adapters judge-v9``.  Output is a per-domain summary plus
a ``failures`` list the operator reviews before committing
corpora.

Threshold convention: a corpus is "ship-ready" when
``pct_correct >= 0.80`` AND no single archetype has
``> 30%`` severe failures.  The caller enforces the threshold
(this module only reports).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from ncms.application.adapters.corpus.loader import load_jsonl
from ncms.application.adapters.schemas import Domain, GoldExample
from ncms.infrastructure.llm.caller import call_llm_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_JUDGE_PROMPT = """You are a strict but fair data-quality judge for v9 SLM training rows.

Judge the content against the PROPOSED LABELS — do NOT invent complaints; flag
only labels that are demonstrably wrong or clearly absent from the content.

Label vocabularies:
  intent: positive / negative / habitual / choice / difficulty / none
  admission: persist / ephemeral / discard
  state_change: declaration / retirement / none
  role (inside role_spans): primary / alternative / casual / not_relevant

Allowed topics for this domain: {topics}

CONTENT:
\"\"\"{content}\"\"\"

PROPOSED LABELS:
  intent: {intent}
  admission: {admission}
  state_change: {state_change}
  topic: {topic}
  role_spans: {role_spans_summary}

Per-head judging rules:

INTENT
  - positive: content expresses approval, adoption, enthusiasm, or commitment
  - negative: content expresses disapproval, rejection, frustration, or rollback
  - habitual: content describes a recurring routine with no state change
  - choice: content contrasts two named alternatives with a clear winner
  - difficulty: content expresses struggle, friction, or trouble
  - none: neutral factual observation — no preference / emotion

ADMISSION
  - persist: long-term content (decisions, facts, observations worth weeks later)
  - ephemeral: transient / time-bounded — relevant now, not in a month
  - discard: noise — chitchat, filler, a memory system should drop it

STATE_CHANGE
  - declaration: explicit new-state language ("started on", "adopted", "decided to use")
  - retirement: explicit removal language ("stopped", "deprecated", "migrated away")
  - none: ongoing / neutral / purely observational

TOPIC
  - Must be one of the allowed topics above.  Judge whether the content's
    subject matter matches the assigned topic label.

ROLE_SPANS
  - primary: the entity is the SUBJECT of the speaker's preference / action
  - alternative: the entity is a CONTRAST partner ("X over Y": Y is the alternative)
  - casual: the entity is mentioned in passing, not the subject of a preference
  - not_relevant: the gazetteer detected a surface that the archetype did NOT ask for
    (e.g. "persistent" matched the severity slot in a medication row)
  - Flag role spans that are plainly mislabeled.

Verdict levels:
  - "correct":          every head's label is faithful to the content
  - "partially_wrong":  one or two heads are wrong; others are fine
  - "severely_wrong":   three or more heads are wrong, or the row is
                        incoherent / incompatible with its labels

Return ONLY a JSON object (no prose, no markdown fences):
{{
  "verdict": "correct" | "partially_wrong" | "severely_wrong",
  "issues": ["short one-line description of each mistake"],
  "wrong_heads": ["intent" | "admission" | "state_change" | "topic" | "role_spans", ...]
}}
"""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DomainJudgeResult:
    """Aggregated judging output for one domain corpus."""

    domain: Domain
    n_sampled: int
    verdicts: dict[str, int] = field(default_factory=dict)
    pct_correct: float = 0.0
    wrong_head_counts: dict[str, int] = field(default_factory=dict)
    per_archetype: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "domain": self.domain,
            "n_sampled": self.n_sampled,
            "verdicts": self.verdicts,
            "pct_correct": self.pct_correct,
            "wrong_head_counts": self.wrong_head_counts,
            "per_archetype": self.per_archetype,
            "failures": self.failures,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _role_spans_summary(ex: GoldExample) -> str:
    """Compact string used in the prompt — keeps tokens low."""
    if not ex.role_spans:
        return "(none)"
    parts = [
        f"{rs.role}:{rs.slot}={rs.canonical!r}"
        for rs in ex.role_spans
    ]
    return "[" + ", ".join(parts) + "]"


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
    tail = src[idx + len(marker):]
    end = tail.find(" ")
    return tail if end < 0 else tail[:end]


async def _judge_one(
    ex: GoldExample,
    *,
    topics: tuple[str, ...],
    model: str,
    api_base: str | None,
) -> dict | None:
    """Call the judge LLM on one row; return parsed verdict or None."""
    prompt = _JUDGE_PROMPT.format(
        topics=list(topics),
        content=ex.text.strip()[:800],
        intent=ex.intent,
        admission=ex.admission or "(unlabeled)",
        state_change=ex.state_change or "(unlabeled)",
        topic=ex.topic or "(unlabeled)",
        role_spans_summary=_role_spans_summary(ex),
    )
    try:
        result = await call_llm_json(
            prompt=prompt, model=model, api_base=api_base,
            max_tokens=400, temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — judge non-fatal
        logger.warning(
            "v9 judge failed on text[:60]=%r: %s",
            ex.text[:60], exc,
        )
        return None
    if not isinstance(result, dict):
        logger.warning(
            "v9 judge returned non-dict: %r", type(result).__name__,
        )
        return None
    verdict = str(result.get("verdict") or "").lower()
    if verdict not in ("correct", "partially_wrong", "severely_wrong"):
        # Unclassifiable → treat as severe.
        verdict = "severely_wrong"
    wrong_heads = [
        str(h).lower() for h in (result.get("wrong_heads") or [])
        if isinstance(h, str)
    ]
    return {
        "verdict": verdict,
        "issues": [str(i) for i in (result.get("issues") or [])],
        "wrong_heads": wrong_heads,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def judge_corpus(
    *,
    domain: Domain,
    corpus_path: Path,
    topics: tuple[str, ...],
    n_samples: int,
    model: str,
    api_base: str | None,
    seed: int = 42,
    stratified: bool = True,
) -> DomainJudgeResult:
    """Judge ``n_samples`` rows from ``corpus_path``.

    When ``stratified`` is true (default), sampling is balanced
    across archetypes — pick ``ceil(n_samples / num_archetypes)``
    rows per archetype so under-represented archetypes still get
    judged.  When false, uniform random across the full file.

    Parameters
    ----------
    domain
        Domain name, used for reporting + the allowed-topic vocabulary
        in the prompt.
    corpus_path
        JSONL file produced by :func:`generate_domain`.
    topics
        Full topic vocabulary for the domain (comes from
        ``DomainSpec.topics``).  Used in the prompt to tell the
        judge which labels are valid.
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
            domain=domain, n_sampled=0, pct_correct=0.0,
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
        domain=domain, n_sampled=len(sample),
    )
    result.verdicts = {
        "correct": 0, "partially_wrong": 0, "severely_wrong": 0,
    }

    for i, ex in enumerate(sample, 1):
        arch_name = _archetype_of(ex)
        arch_bucket = result.per_archetype.setdefault(
            arch_name,
            {"correct": 0, "partially_wrong": 0, "severely_wrong": 0, "failed": 0},
        )
        j = await _judge_one(
            ex, topics=topics, model=model, api_base=api_base,
        )
        if j is None:
            # Judge LLM unavailable for this row; count against
            # severe but preserve the judge-failed bucket.
            result.verdicts["severely_wrong"] += 1
            arch_bucket["failed"] += 1
            continue
        result.verdicts[j["verdict"]] += 1
        arch_bucket[j["verdict"]] += 1
        for h in j["wrong_heads"]:
            result.wrong_head_counts[h] = (
                result.wrong_head_counts.get(h, 0) + 1
            )
        if j["verdict"] != "correct":
            result.failures.append({
                "text": ex.text[:200],
                "archetype": arch_name,
                "labels": {
                    "intent": ex.intent,
                    "admission": ex.admission,
                    "state_change": ex.state_change,
                    "topic": ex.topic,
                    "slots": ex.slots,
                    "role_spans": _role_spans_summary(ex),
                },
                "verdict": j["verdict"],
                "issues": j["issues"],
                "wrong_heads": j["wrong_heads"],
            })
        if i % 20 == 0:
            logger.info(
                "[v9 judge] %s: judged %d/%d (correct=%d)",
                domain, i, len(sample), result.verdicts["correct"],
            )

    total = sum(result.verdicts.values()) or 1
    result.pct_correct = 100.0 * result.verdicts["correct"] / total
    return result


def sync_judge_corpus(**kwargs) -> DomainJudgeResult:
    """Sync wrapper for :func:`judge_corpus`."""
    return asyncio.run(judge_corpus(**kwargs))


def format_report(result: DomainJudgeResult) -> str:
    """Render a short human-readable report of ``result``."""
    lines: list[str] = []
    lines.append(
        f"=== v9 judge: domain={result.domain} "
        f"sampled={result.n_sampled} ==="
    )
    lines.append(f"  pct_correct: {result.pct_correct:.1f}%")
    lines.append(f"  verdicts:    {result.verdicts}")
    if result.wrong_head_counts:
        lines.append(
            f"  wrong heads: {dict(sorted(result.wrong_head_counts.items(), key=lambda x: -x[1]))}",
        )
    lines.append("  per-archetype:")
    for arch, counts in sorted(result.per_archetype.items()):
        total_arch = sum(counts.values()) or 1
        pct = 100.0 * counts.get("correct", 0) / total_arch
        lines.append(
            f"    · {arch:40s} correct={pct:5.1f}%  "
            f"{counts}",
        )
    if result.failures:
        lines.append(
            f"  sample failures ({len(result.failures)} total, "
            "showing first 5):",
        )
        for f in result.failures[:5]:
            lines.append(
                f"    [{f['verdict']}] {f['archetype']}: "
                f"{f['text'][:100]}…",
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
