"""Pattern-based adversarial augmentation.

Extends a gold or SDG corpus with hard cases that exercise seven
known failure modes observed in the prototype evaluation:

1. **Quoted speech** — the subject of the sentence is someone else,
   not the speaker.  Intent should be ``none`` regardless of the
   inner verb.

2. **Negation of positive verb** — "don't really prefer X anymore"
   looks positive to naive classifiers; intent should be
   ``negative`` or ``none``.

3. **Past positive → present negative** — "I used to love X but
   now I can't stand it".  Present intent dominates.

4. **Third-person + first-person contrast** — "my friend hates X
   but I love them" — first-person clause wins.

5. **Double negation + comparative** — "it's not that I don't
   like X, I just prefer Y" — net intent is ``choice`` on Y.

6. **Sarcasm / disfluency** — "oh yeah I just ~love~ X" (sarcastic)
   → intent ``negative``; "uh, y'know, I kinda like the, uh, Y
   thing" → intent ``positive`` with preserved slots.

7. **Empty / minimal** — "ok", "sure", "" — intent ``none``.

Each augmentation is a deterministic template that takes one seed
example and produces one adversarial variant with the "correct"
multi-head labels attached.  The generator doesn't try to be
clever — quantity over cleverness is the lesson from the earlier
SDG experiments, and the hard cases can be hand-reviewed after.

The generator deliberately does NOT collide with the reserved
:class:`adversarial.jsonl` test set.  That file is the held-out
evaluation set; its patterns are covered here so the *training*
gets diverse coverage, but generated examples go into a separate
``adversarial_train.jsonl`` file keyed ``split="adversarial"`` so
eval tooling doesn't double-count them.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from ncms.application.adapters.corpus.loader import (
    dump_jsonl,
    load_all,
)
from ncms.application.adapters.schemas import (
    DOMAINS,
    Domain,
    GoldExample,
)

# ---------------------------------------------------------------------------
# Per-domain primary slot name
# ---------------------------------------------------------------------------
#
# Adversarial templates read the seed's primary noun-phrase to
# rebuild the adversarial variant.  Conversational uses the generic
# ``object`` slot; software_dev gold uses ``library`` / ``language``
# / ``tool``; clinical uses ``medication`` / ``procedure``.  This map
# lets every generator call ``_primary(seed)`` and get the right
# surface form regardless of domain — closing the gap that caused
# Phase 3 to silently skip on software_dev and clinical in the
# initial Sprint-3 runs.

_DOMAIN_PRIMARY_SLOTS: dict[Domain, tuple[str, ...]] = {
    "conversational": ("object",),
    "software_dev": (
        "library", "language", "pattern", "tool", "object",
    ),
    "clinical": (
        "medication", "procedure", "symptom", "object",
    ),
}


def _primary(seed: GoldExample) -> tuple[str, str] | None:
    """Return the primary ``(slot_name, surface)`` from a seed.

    Walks :data:`_DOMAIN_PRIMARY_SLOTS` in order for the seed's
    domain and returns the first matched slot name + value.  The
    slot NAME is returned too so generated adversarial rows emit
    slots with the right key for their domain (e.g. ``medication``
    for clinical rather than ``object``) — otherwise the evaluator
    compares (key, value) tuples and slot F1 drops due to key
    mismatches.
    """
    for slot_name in _DOMAIN_PRIMARY_SLOTS.get(seed.domain, ("object",)):
        value = seed.slots.get(slot_name)
        if value:
            return slot_name, value
    return None


# ---------------------------------------------------------------------------
# Mode 1 — quoted speech
# ---------------------------------------------------------------------------

_QUOTED_SPEAKERS = (
    "My manager", "The professor", "My doctor", "A colleague",
    "The waiter", "My friend", "My neighbour", "Someone on Twitter",
    "A random stranger", "My roommate", "The CEO", "An intern",
)

_QUOTED_FRAMES = (
    "{speaker} said '{original}'",
    "Someone told me '{original}'",
    "I overheard {speaker} say '{original}'",
    "My boss keeps saying '{original}'",
    "{speaker} once told me '{original}' — weird, right?",
    "On the podcast, {speaker} said '{original}'",
    "The email from {speaker} read '{original}'",
    "I got a text that said '{original}'",
    "The review quoted a customer: '{original}'",
    "{speaker} texted me '{original}' last night",
)


def _quoted_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample:
    speaker = rng.choice(_QUOTED_SPEAKERS)
    frame = rng.choice(_QUOTED_FRAMES).format(
        speaker=speaker, original=seed.text.rstrip(".!?"),
    )
    return GoldExample(
        text=frame + ".",
        domain=seed.domain,
        intent="none",  # quoted speech is not a speaker preference
        slots={},
        topic="other",
        admission="ephemeral",  # not about the speaker → low persist value
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/quoted seed={seed.text[:30]!r}",
        note="quoted_speech",
    )


# ---------------------------------------------------------------------------
# Mode 2 — negation of positive verb
# ---------------------------------------------------------------------------

_NEG_POS_FRAMES = (
    "I don't really {verb} {object} anymore",
    "Honestly, I don't {verb} {object} that much",
    "{object}? Not my thing anymore",
    "I used to {verb} {object} but honestly, not anymore",
    "Look, I don't {verb} {object}, just being honest",
    "I'm not gonna pretend I {verb} {object}",
    "Stopped {verb}-ing {object} a while back",
    "Between you and me, I don't {verb} {object}",
    "{object} — I wouldn't say I {verb} it",
    "I can't honestly say I {verb} {object}",
)


def _neg_pos_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    verbs = ["love", "like", "enjoy", "prefer"]
    text = rng.choice(_NEG_POS_FRAMES).format(
        verb=rng.choice(verbs), object=obj,
    )
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="negative",
        slots={slot_name: obj},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/neg-pos seed={seed.text[:30]!r}",
        note="negated_positive",
    )


# ---------------------------------------------------------------------------
# Mode 3 — past positive → present negative
# ---------------------------------------------------------------------------

_PAST_FLIP_FRAMES = (
    "I used to love {object} but now I can't stand it",
    "{object} was a favorite, now I avoid it",
    "Back when I liked {object}, I couldn't get enough — not anymore",
    "{object} was my go-to; these days I steer clear",
    "I was into {object} for years. Not anymore.",
    "Honestly {object} used to be great but it's lost the magic",
    "Fell out of love with {object} last year",
    "Remember when I was obsessed with {object}? That was a phase.",
    "{object} — yeah that was me five years ago, not now",
    "Not gonna lie, {object} doesn't do it for me anymore",
)


def _past_flip_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    text = rng.choice(_PAST_FLIP_FRAMES).format(object=obj)
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="negative",  # present intent wins
        slots={slot_name: obj},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/past-flip seed={seed.text[:30]!r}",
        note="past_flip",
    )


# ---------------------------------------------------------------------------
# Mode 4 — third-person + first-person contrast
# ---------------------------------------------------------------------------

_THIRD_FIRST_FRAMES = (
    "My friend hates {object}, but I love them",
    "My partner can't stand {object}, I adore it",
    "Everyone else avoids {object}; I go for it",
    "My family never touches {object} but it's my favorite",
    "Most of my team dislikes {object}, I'm a fan though",
    "My coworkers rag on {object}, but honestly I love it",
    "Nobody in my house likes {object} — I eat the whole thing myself",
    "My mom won't touch {object} — me, I'm all in",
    "Reviewers panned {object}, I thought it was great",
    "They all told me to skip {object}; I loved it",
)


def _third_first_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    text = rng.choice(_THIRD_FIRST_FRAMES).format(object=obj)
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="positive",  # first-person clause wins
        slots={slot_name: obj},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/third-first seed={seed.text[:30]!r}",
        note="third_first_contrast",
    )


# ---------------------------------------------------------------------------
# Mode 5 — double negation + comparative preference
# ---------------------------------------------------------------------------

_DBL_NEG_FRAMES = (
    "It's not that I don't like {alt}, I just prefer {object}",
    "I don't dislike {alt}, but I'd go with {object}",
    "I wouldn't say no to {alt}, but {object} wins",
    "Not to knock {alt}, but {object} is what I pick",
    "{alt}'s fine, don't get me wrong — I still choose {object}",
    "Nothing against {alt}, but put me down for {object}",
    "I'm not anti-{alt}, just pro-{object}",
)


def _double_neg_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    alt = seed.slots.get("alternative") or "the other option"
    text = rng.choice(_DBL_NEG_FRAMES).format(object=obj, alt=alt)
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="choice",
        slots={slot_name: obj, "alternative": alt},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/dbl-neg seed={seed.text[:30]!r}",
        note="double_negation",
    )


# ---------------------------------------------------------------------------
# Mode 6 — sarcasm / disfluency
# ---------------------------------------------------------------------------

_SARCASM_FRAMES = (
    "Oh yeah, I just ~love~ {object}",
    "{object}, yeah, super excited about that",
    "I'm *thrilled* about {object}, obviously",
    "Can't wait for more {object}, said nobody ever",
    "{object} — a real highlight of my day /s",
    "Who wouldn't love {object} at 3am on a Tuesday",
    "Living for {object}, clearly",
    "Nothing says joy like {object}",
    "{object} has really blessed my week",
)

_DISFLUENCY_FRAMES = (
    "Uh, y'know, I kinda like the, uh, {object} thing",
    "So yeah I, um, I do {verb} {object}, mostly",
    "{object}, yeah, like, that's the one I go for",
    "I mean, I — I {verb} {object}, probably",
    "Like, the {object} thing, I'm — yeah, into that",
    "So I sort of, um, {verb} {object}? if that makes sense",
    "Y'know, {object}, that's — that's what I'd pick",
    "The {object}, yeah, I'd say I {verb} it",
)


def _sarcasm_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    text = rng.choice(_SARCASM_FRAMES).format(object=obj)
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="negative",  # sarcasm flips positive verb
        slots={slot_name: obj},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/sarcasm seed={seed.text[:30]!r}",
        note="sarcasm",
    )


def _disfluency_variant(
    rng: random.Random, seed: GoldExample,
) -> GoldExample | None:
    primary = _primary(seed)
    if primary is None:
        return None
    slot_name, obj = primary
    text = rng.choice(_DISFLUENCY_FRAMES).format(
        object=obj, verb=rng.choice(["like", "love", "enjoy"]),
    )
    return GoldExample(
        text=text + ".",
        domain=seed.domain,
        intent="positive",
        slots={slot_name: obj},
        topic=seed.topic,
        admission="persist",
        state_change="none",
        split="adversarial",
        source=f"adversarial-v1/disfluency seed={seed.text[:30]!r}",
        note="disfluency",
    )


# ---------------------------------------------------------------------------
# Mode 7 — empty / minimal
# ---------------------------------------------------------------------------

_MINIMAL_TEXTS = (
    "ok", "sure", "yeah", "maybe", "idk",
    "whatever", "fine", "no comment", "n/a",
    "k", "kk", "lol", "+1", "same", "done",
    "ttyl", "brb", "ok thx", "noted", "cool",
    "agreed", "yep", "nope",
)


def _minimal_variant(
    rng: random.Random, domain: Domain,
) -> GoldExample:
    return GoldExample(
        text=rng.choice(_MINIMAL_TEXTS) + ".",
        domain=domain,
        intent="none",
        slots={},
        topic="other",
        admission="discard",  # empty-ish → discard
        state_change="none",
        split="adversarial",
        source="adversarial-v1/minimal",
        note="empty_minimal",
    )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

_GENERATORS = [
    ("quoted", _quoted_variant),
    ("neg_pos", _neg_pos_variant),
    ("past_flip", _past_flip_variant),
    ("third_first", _third_first_variant),
    ("double_neg", _double_neg_variant),
    ("sarcasm", _sarcasm_variant),
    ("disfluency", _disfluency_variant),
]


def generate_adversarial(
    seeds: list[GoldExample],
    *,
    target: int = 200,
    seed: int = 17,
    minimal_fraction: float = 0.05,
) -> list[GoldExample]:
    """Generate ``target`` adversarial variants from a seed pool.

    Picks seeds and generators round-robin so every failure mode gets
    coverage.  Mode-7 (minimal) is sampled at ``minimal_fraction``
    since too many "ok" / "yeah" rows distort the intent distribution.
    """
    if not seeds:
        raise ValueError("seeds list is empty")
    rng = random.Random(seed)
    out: list[GoldExample] = []
    attempts = 0
    max_attempts = target * 6

    while len(out) < target and attempts < max_attempts:
        attempts += 1
        if rng.random() < minimal_fraction:
            out.append(_minimal_variant(rng, seeds[0].domain))
            continue
        seed_ex = rng.choice(seeds)
        _, generator = rng.choice(_GENERATORS)
        example = generator(rng, seed_ex)
        if example is not None:
            out.append(example)
    return out


def _load_seeds(
    domain: Domain, splits: list[str], corpus_dir: Path,
) -> list[GoldExample]:
    """Pull seeds from the requested splits, filtered to ``domain``.

    Adversarial augmentation reads gold + SDG as the seed pool —
    hand-labeled adversarial.jsonl is reserved as held-out eval.
    Seeds are filtered through :func:`_primary` so any seed with a
    domain-recognised primary slot (not only ``object``) feeds the
    generator.
    """
    out: list[GoldExample] = []
    for split in splits:
        if split == "adversarial":
            # Don't recurse into the held-out set.
            continue
        out.extend(
            ex for ex in load_all(corpus_dir, split=split)
            if ex.domain == domain
        )
    return [ex for ex in out if _primary(ex) is not None]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate pattern-based adversarial examples",
    )
    parser.add_argument("--domain", required=True, choices=list(DOMAINS))
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--seed-splits", default="gold,sdg",
        help="Comma-separated seed splits (default: gold,sdg).",
    )
    parser.add_argument(
        "--corpus-dir", type=Path,
        default=Path(__file__).parent.parent / "corpus",
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="JSONL output path (split will be set to 'adversarial').",
    )
    parser.add_argument(
        "--minimal-fraction", type=float, default=0.05,
    )
    args = parser.parse_args()

    seeds = _load_seeds(
        args.domain,
        [s.strip() for s in args.seed_splits.split(",") if s.strip()],
        args.corpus_dir,
    )
    if not seeds:
        parser.error(
            f"no seeds found for domain={args.domain!r} splits="
            f"{args.seed_splits!r}",
        )
    examples = generate_adversarial(
        seeds,
        target=args.target,
        seed=args.seed,
        minimal_fraction=args.minimal_fraction,
    )
    dump_jsonl(examples, args.output)
    # Mode breakdown summary.
    modes: dict[str, int] = {}
    for ex in examples:
        modes[ex.note or "?"] = modes.get(ex.note or "?", 0) + 1
    print(
        f"[adversarial] domain={args.domain} seeds={len(seeds)} "
        f"generated={len(examples)} → {args.output}",
    )
    for mode, n in sorted(modes.items()):
        print(f"  {mode:<25} {n}")


if __name__ == "__main__":
    main()
