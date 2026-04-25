"""``ncms adapters`` — manage SLM LoRA adapters as first-class NCMS artifacts.

Subcommands:

  list          show every registered domain, source-tree versions,
                and the deployed version under ``~/.ncms/adapters/``.
  generate-sdg  run the typed-slot template expander for a domain;
                output lands at ``adapters/corpora/sdg_<domain>.jsonl``.
  train         full train loop (SDG + gold + adversarial → LoRA
                checkpoint + gate).  Delegates to
                :mod:`ncms.application.adapters.train`.
  deploy        copy a source-tree checkpoint into the runtime path
                (``~/.ncms/adapters/<domain>/<version>/``) so the
                LoRA adapter loader picks it up at next service boot.
  status        show which adapter version is currently deployed per
                domain + a quick manifest sanity check.

All commands operate on the :class:`DomainManifest` registry in
:mod:`ncms.application.adapters.schemas` so adapter ↔ corpus ↔
taxonomy wiring stays consistent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click


@click.group()
def adapters() -> None:
    """Manage SLM LoRA adapters (SDG → training → deploy).

    Adapters live in two places:

      adapters/                         — repo-local artifacts:
          corpora/<domain>/*.jsonl      corpora (gold + sdg + adversarial)
          taxonomies/<domain>.yaml      topic / admission / state_change label sets
          checkpoints/<d>/<v>/...       trained LoRA weights + manifest

      ~/.ncms/adapters/<d>/<v>/         — runtime deployment root:
          what the ``IntentSlotExtractor`` chain loads at service boot

    ``train`` produces a checkpoint under the repo-local
    ``adapters/checkpoints/<domain>/<version>/`` path.  ``deploy``
    copies that checkpoint into the runtime path.
    """


@adapters.command("list")
def adapters_list() -> None:
    """List every registered domain + its source-tree and deployed versions."""
    from rich.console import Console
    from rich.table import Table

    from ncms.application.adapters.schemas import (
        DOMAIN_MANIFESTS,
        _ensure_hydrated,
    )

    # Iterating DOMAIN_MANIFESTS directly bypasses the lazy-hydration
    # path in get_domain_manifest, so trigger hydration explicitly
    # here to surface the v9 default_version + v9 paths.
    _ensure_hydrated()

    console = Console()
    table = Table(title="NCMS adapters")
    table.add_column("domain", style="cyan")
    table.add_column("default", justify="center")
    table.add_column("source-tree versions", overflow="fold")
    table.add_column("deployed versions", overflow="fold")

    for name, m in sorted(DOMAIN_MANIFESTS.items()):
        src_versions = sorted(
            p.name for p in m.adapter_output_root.glob("*")
            if p.is_dir() and not p.name.startswith(".")
        ) if m.adapter_output_root.exists() else []
        dep_versions = sorted(
            p.name for p in m.deployed_adapter_root.glob("*")
            if p.is_dir() and not p.name.startswith(".")
        ) if m.deployed_adapter_root.exists() else []
        src_mark = [
            f"[green]{v}[/]" if v == m.default_version else v
            for v in src_versions
        ]
        dep_mark = [
            f"[green]{v}[/]" if v == m.default_version else v
            for v in dep_versions
        ]
        table.add_row(
            name,
            m.default_version,
            ", ".join(src_mark) or "[dim]—[/]",
            ", ".join(dep_mark) or "[dim](none)[/]",
        )
    console.print(table)
    console.print(
        "\n[dim]source-tree root:[/] "
        f"{DOMAIN_MANIFESTS['conversational'].adapter_output_root.parent}",
    )
    console.print(
        "[dim]deployed root:[/]    "
        f"{DOMAIN_MANIFESTS['conversational'].deployed_adapter_root.parent}",
    )


@adapters.command("generate-sdg")
@click.option("--domain", required=True,
              help="Domain name (see `ncms adapters list`)")
@click.option("--split", type=click.Choice(["gold", "sdg"]), default="sdg",
              help="Which archetype row target to use: n_gold or n_sdg "
                   "(default: sdg)")
@click.option("--backend", type=click.Choice(["template", "spark"]),
              default="template",
              help="Generation backend: 'template' (deterministic, no LLM) "
                   "or 'spark' (live vLLM endpoint).  Default: template.")
@click.option("--model", default=None,
              help="litellm model id for --backend spark "
                   "(e.g. openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16)")
@click.option("--api-base", default=None,
              help="API base URL for --backend spark "
                   "(e.g. http://spark-ee7d.local:8000/v1)")
@click.option("--temperature", type=float, default=0.8,
              help="LLM sampling temperature (spark backend only)")
@click.option("--archetype", "archetype_filter", multiple=True,
              help="Restrict generation to specific archetype(s) "
                   "(repeatable).  Useful for spot-checking a single "
                   "archetype with --backend spark before a full run.")
@click.option("--limit", type=int, default=None,
              help="Override the archetype's n_gold/n_sdg target with "
                   "this row count.  Combined with --archetype, enables "
                   "tiny probes (e.g. --archetype positive_medication_start "
                   "--limit 5 for a 5-row Spark spot-check).")
@click.option("--seed", type=int, default=17)
@click.option("--output", type=click.Path(path_type=Path), default=None,
              help="Output JSONL (defaults to the domain's sdg_jsonl path "
                   "when --split=sdg, or gold_jsonl when --split=gold)")
def adapters_generate_sdg(
    domain: str, split: str, backend: str,
    model: str | None, api_base: str | None, temperature: float,
    archetype_filter: tuple[str, ...], limit: int | None,
    seed: int, output: Path | None,
) -> None:
    """Run the v9 stratified-archetype SDG generator for one domain.

    The generator consumes the domain's YAML plugin
    (``adapters/domains/<domain>/``) — archetypes, diversity taxonomy,
    and (optional) gazetteer — and emits :class:`GoldExample` rows
    honoring each archetype's ``n_gold`` / ``n_sdg`` target.

    Backends:

    * ``--backend template`` (default) — deterministic, no LLM spend.
      Good for dry-runs + CI smoke tests.  Surface quality is limited
      to the archetype's ``phrasings`` + canned fillers.
    * ``--backend spark`` — live generation via an OpenAI-compatible
      endpoint (vLLM on DGX Spark, Ollama, etc.).  Requires
      ``--model`` and ``--api-base``.

    Spot-check workflow:

    \b
      1. Probe ONE archetype with a tiny row count via Spark:
         ncms adapters generate-sdg --domain clinical --backend spark \\
             --model openai/nvidia/... --api-base http://spark.../v1 \\
             --archetype positive_medication_start --limit 5 \\
             --output /tmp/probe.jsonl
      2. Eyeball /tmp/probe.jsonl for quality.
      3. If acceptable, drop --archetype / --limit for the full run.
    """
    from ncms.application.adapters.corpus.loader import dump_jsonl
    from ncms.application.adapters.domain_loader import load_domain
    from ncms.application.adapters.schemas import get_domain_manifest
    from ncms.application.adapters.sdg.v9 import (
        SparkBackend,
        TemplateBackend,
        generate_domain,
        generate_for_archetype,
    )

    # Resolve the domain directory.  We could keep a parallel registry
    # of (name → source_dir), but walking the adapters/domains/ root
    # at CLI time is cheap and avoids another source of truth.
    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    domain_dir = _find_domain_source_dir(domain)
    if domain_dir is None:
        raise click.ClickException(
            f"domain {domain!r}: YAML plugin not found under "
            "adapters/domains/ — did you run `ncms adapters list`?",
        )

    spec = load_domain(domain_dir)

    # Pick the backend.
    be: object
    if backend == "template":
        be = TemplateBackend()
    elif backend == "spark":
        if not model or not api_base:
            raise click.ClickException(
                "--backend spark requires --model and --api-base",
            )
        be = SparkBackend(
            model=model, api_base=api_base, temperature=temperature,
        )
    else:  # unreachable — click validates
        raise click.ClickException(f"unknown backend {backend!r}")

    # If --archetype was passed, validate the names early so the
    # operator doesn't burn Spark compute on a typo.
    if archetype_filter:
        arch_names = {a.name for a in spec.archetypes}
        unknown = [n for n in archetype_filter if n not in arch_names]
        if unknown:
            raise click.ClickException(
                f"unknown archetype(s) {unknown} for domain {domain!r}.  "
                f"Known: {sorted(arch_names)}",
            )

    # Output path — respect the manifest defaults for each split.
    out = output or (
        manifest.sdg_jsonl if split == "sdg" else manifest.gold_jsonl
    )

    # Fast path: no filter and no limit → use the one-shot
    # ``generate_domain`` helper.
    use_fast_path = not archetype_filter and limit is None
    if use_fast_path:
        rows, stats_by = generate_domain(
            spec, backend=be, split=split, seed=seed,  # type: ignore[arg-type]
        )
    else:
        # Filtered / limited path — iterate archetypes manually so we
        # can apply the overrides without mutating the frozen spec.
        rows = []
        stats_by = {}
        selected = [
            a for a in spec.archetypes
            if not archetype_filter or a.name in archetype_filter
        ]
        for i, arch in enumerate(selected):
            n = limit if limit is not None else (
                arch.n_gold if split == "gold" else arch.n_sdg
            )
            if n <= 0:
                continue
            arch_seed = seed + i * 101
            arch_rows, arch_stats = generate_for_archetype(
                spec, arch,
                n=n, backend=be,  # type: ignore[arg-type]
                split=split,  # type: ignore[arg-type]
                seed=arch_seed,
            )
            rows.extend(arch_rows)
            stats_by[arch.name] = arch_stats

    dump_jsonl(rows, out)

    # Summary.
    total_req = sum(s.requested for s in stats_by.values())
    total_gen = sum(s.generated for s in stats_by.values())
    total_acc = sum(s.accepted for s in stats_by.values())
    total_dup = sum(s.duplicates for s in stats_by.values())
    yield_pct = (100.0 * total_acc / total_gen) if total_gen else 0.0
    click.echo(
        f"[v9 sdg] domain={domain} split={split} backend={backend} "
        f"archetypes={len(stats_by)}/{len(spec.archetypes)}"
        + (f" filter={list(archetype_filter)}" if archetype_filter else "")
        + (f" limit={limit}" if limit is not None else ""),
    )
    click.echo(
        f"         requested={total_req} generated={total_gen} "
        f"accepted={total_acc} duplicates={total_dup} "
        f"yield={yield_pct:.1f}%",
    )
    # Per-archetype lines help the operator see under-performers at a
    # glance without trawling logs.
    for name, s in stats_by.items():
        click.echo(
            f"         · {name}: req={s.requested} acc={s.accepted} "
            f"yield={s.yield_rate * 100.0:.0f}% rejects={dict(s.rejections)}",
        )
    click.echo(f"         → {out}")


def _find_domain_source_dir(domain: str) -> Path | None:
    """Walk up from this file looking for ``adapters/domains/<domain>/``.

    Mirrors the heuristic used by :mod:`schemas` to hydrate
    ``DOMAIN_MANIFESTS`` from YAML — first ancestor containing
    ``pyproject.toml`` is the repo root.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            candidate = parent / "adapters" / "domains" / domain
            return candidate if candidate.is_dir() else None
    return None


@adapters.command("judge-v9")
@click.option("--domain", required=True,
              help="Domain name (see `ncms adapters list`)")
@click.option("--corpus-path", type=click.Path(path_type=Path),
              default=None,
              help="Corpus JSONL to judge (defaults to the domain's "
                   "sdg_jsonl path — the output of `generate-sdg`).")
@click.option("--n-samples", type=int, default=50,
              help="How many rows to judge (default 50).  Stratified "
                   "across archetypes unless --no-stratify.")
@click.option("--no-stratify", "no_stratify", is_flag=True,
              help="Uniform random sample across the whole corpus "
                   "instead of balanced per-archetype.")
@click.option("--model", required=True,
              help="litellm judge model id — recommend a DIFFERENT "
                   "model family from the corpus generator so any "
                   "systemic blind spot is visible.")
@click.option("--api-base", default=None,
              help="API base URL for the judge LLM.")
@click.option("--seed", type=int, default=42)
@click.option("--report-path", type=click.Path(path_type=Path),
              default=None,
              help="Optional path to write the full JSON judgment; "
                   "terminal summary is printed either way.")
@click.option("--threshold", type=float, default=80.0,
              help="Fail exit code if pct_faithful < threshold "
                   "(default 80.0).  Set to 0 to never fail.")
def adapters_judge_v9(
    domain: str, corpus_path: Path | None,
    n_samples: int, no_stratify: bool,
    model: str, api_base: str | None,
    seed: int, report_path: Path | None, threshold: float,
) -> None:
    """v9 corpus quality gate — LLM-as-judge on all five heads + role_spans.

    Samples rows from the v9 SDG corpus and asks a judge LLM to
    verify intent / admission / state_change / topic /role_span
    labels against the row's text.  Use BEFORE committing the
    corpus or starting a training run.

    Prints a terminal summary plus per-archetype verdict counts.
    Exits non-zero when ``pct_correct < --threshold`` so CI can
    gate on the verdict.

    Example (judge clinical with Spark Nemotron — same endpoint
    that generated the corpus, which is acceptable for a
    first-pass gate but NOT for publication):

    \b
        ncms adapters judge-v9 --domain clinical \\
            --model openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \\
            --api-base http://spark-ee7d.local:8000/v1 \\
            --n-samples 50 \\
            --report-path /tmp/clinical_judge.json

    For a "different-model" judge (stricter), swap ``--model`` /
    ``--api-base`` to another provider (Ollama / OpenAI).
    """
    import json as _json

    from ncms.application.adapters.domain_loader import load_domain
    from ncms.application.adapters.schemas import get_domain_manifest
    from ncms.application.adapters.sdg.v9.judge import (
        format_report,
        sync_judge_corpus,
    )

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    path = corpus_path or manifest.sdg_jsonl
    if not path.is_file():
        raise click.ClickException(f"corpus not found: {path}")

    domain_dir = _find_domain_source_dir(domain)
    if domain_dir is None:
        raise click.ClickException(
            f"domain {domain!r}: YAML plugin not found under adapters/domains/",
        )
    spec = load_domain(domain_dir)

    click.echo(
        f"[v9 judge] domain={domain} corpus={path} "
        f"n_samples={n_samples} model={model}",
    )
    archetype_lookup = {a.name: a for a in spec.archetypes}
    result = sync_judge_corpus(
        domain=domain,  # type: ignore[arg-type]
        corpus_path=path,
        archetype_lookup=archetype_lookup,
        n_samples=n_samples,
        model=model,
        api_base=api_base,
        seed=seed,
        stratified=not no_stratify,
    )
    click.echo(format_report(result))

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _json.dumps(result.as_dict(), indent=2),
            encoding="utf-8",
        )
        click.echo(f"         → full report: {report_path}")

    if threshold > 0 and result.pct_faithful < threshold:
        raise click.ClickException(
            f"quality gate FAILED: pct_faithful "
            f"{result.pct_faithful:.1f}% < threshold {threshold:.1f}%",
        )


@adapters.command("sanity-v9")
@click.option("--domain", required=True,
              help="Domain name (see `ncms adapters list`)")
@click.option("--corpus-path", type=click.Path(path_type=Path),
              default=None,
              help="Corpus JSONL to check (defaults to the domain's "
                   "sdg_jsonl path from DOMAIN_MANIFESTS).")
@click.option("--report-path", type=click.Path(path_type=Path),
              default=None,
              help="Optional path to write the full JSON sanity report; "
                   "terminal summary is printed either way.")
@click.option("--fail-on-violation/--no-fail-on-violation",
              default=True,
              help="Exit non-zero when any invariant violation is found "
                   "(default: true).  Pass --no-fail-on-violation to "
                   "run the check for reporting only.")
def adapters_sanity_v9(
    domain: str,
    corpus_path: Path | None,
    report_path: Path | None,
    fail_on_violation: bool,
) -> None:
    """Offline v9 corpus sanity check — no LLM cost.

    Validates every row against the invariants the trainer + judge
    both assume: non-None labels across all five heads, non-empty
    slots + role_spans where the archetype declared them,
    role-span composition matches the archetype, primary /
    alternative surfaces appear in text, no placeholder leakage,
    length envelope.

    Runs in <1s on a ~3k-row corpus.  Use BEFORE the (expensive)
    LLM judge — cheap tests first, expensive ones on already-clean
    data.
    """
    from ncms.application.adapters.domain_loader import load_domain
    from ncms.application.adapters.schemas import get_domain_manifest
    from ncms.application.adapters.sdg.v9 import (
        format_sanity_report,
        sanity_check,
        write_report_json,
    )

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    path = corpus_path or manifest.sdg_jsonl
    if not path.is_file():
        raise click.ClickException(f"corpus not found: {path}")

    domain_dir = _find_domain_source_dir(domain)
    if domain_dir is None:
        raise click.ClickException(
            f"domain {domain!r}: YAML plugin not found under adapters/domains/",
        )
    spec = load_domain(domain_dir)

    report = sanity_check(path, spec)
    click.echo(format_sanity_report(report))

    if report_path is not None:
        write_report_json(report, report_path)
        click.echo(f"         → full report: {report_path}")

    if fail_on_violation and not report.ok:
        raise click.ClickException(
            f"sanity check FAILED: {report.summary()}",
        )


@adapters.command("train")
@click.option("--domain", required=True)
@click.option("--version", required=True, help="Target adapter version (e.g. v9)")
@click.option("--target-size", type=int, default=3000,
              help="SDG pre-dedup target when --regenerate-sdg (default 3000)")
@click.option("--adversarial-size", type=int, default=300,
              help="Adversarial examples to generate when --adversarial")
@click.option("--epochs", type=int, default=None)
@click.option("--batch-size", type=int, default=None)
@click.option("--learning-rate", type=float, default=None)
@click.option("--device", default=None,
              help="Force device: cpu / cuda / mps (auto by default)")
@click.option("--regenerate-sdg/--use-existing-sdg", default=False,
              help="Regenerate SDG via the legacy template expander "
                   "vs. reuse the existing corpus at "
                   "manifest.sdg_jsonl.  Default: use existing — "
                   "v9 corpora are pre-generated + sanity/judge gated; "
                   "regenerate triggers the legacy expander which "
                   "doesn't speak the v9 archetype schema.")
@click.option("--adversarial/--no-adversarial", default=False,
              help="Run pattern-based adversarial augmentation.  "
                   "Default off for v9 first-training — adversarial "
                   "generator predates the v9 slot vocabulary "
                   "(missing 'framework' for software_dev) and is "
                   "scheduled for B-prime.6 polish.")
def adapters_train(
    domain: str, version: str,
    target_size: int, adversarial_size: int,
    epochs: int | None, batch_size: int | None,
    learning_rate: float | None,
    device: str | None,
    regenerate_sdg: bool, adversarial: bool,
) -> None:
    """Full training loop: SDG → gold/adversarial merge → LoRA → gate.

    Default v9 mode (no flags):

      Phase 1 — load training rows from the manifest's sdg_jsonl
                (gold-tier when curated; SDG-tier as fallback for
                v9 first-training).
      Phase 2 — SKIPPED.  Reuses the existing v9 SDG corpus.
      Phase 3 — SKIPPED.  Adversarial augmentation deferred to
                B-prime.6 (the v6 generator's slot vocabulary is
                stale for v9 software_dev).
      Phase 4 — train LoRA + run promotion gate.

    The adapter checkpoint lands at
    ``adapters/checkpoints/<domain>/<version>/``.  Use
    ``ncms adapters deploy`` after training to promote it into
    ``~/.ncms/adapters/``.
    """
    from ncms.application.adapters.train import run_training

    run_training(
        domain=domain,
        version=version,
        target_size=target_size,
        adversarial_size=adversarial_size,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        device=device,
        skip_sdg=not regenerate_sdg,
        skip_adversarial=not adversarial,
    )


@adapters.command("label-slots")
@click.option("--domain", required=True)
@click.option("--source", type=click.Path(path_type=Path, exists=True),
              required=True,
              help="JSONL file whose rows have a content field")
@click.option("--output", type=click.Path(path_type=Path), required=True,
              help="Output JSONL (GoldExample rows)")
@click.option("--limit", type=int, default=None,
              help="Max rows to label (useful for smoke tests)")
@click.option("--text-field", default="content",
              help="Name of the content-bearing field in source rows")
@click.option("--topic", default=None,
              help="Seed topic label for every row (leave unset to keep None)")
@click.option("--model", default="ollama_chat/qwen3.5:35b-a3b",
              help="LLM model (litellm identifier)")
@click.option("--api-base", default=None,
              help="Optional LLM API base (for vLLM / Spark etc.)")
def adapters_label_slots(
    domain: str, source: Path, output: Path, limit: int | None,
    text_field: str, topic: str | None, model: str, api_base: str | None,
) -> None:
    """Use an LLM to typed-slot label real corpus content.

    Produces :class:`GoldExample` JSONL rows with typed slots
    matching the domain's ``SLOT_TAXONOMY``.  Useful for closing
    the gold-coverage gap after a slot schema expansion: feed real
    domain content (MSEB corpus, LongMemEval dialog, case reports)
    and get typed-slot labels out.

    Hallucinations are rejected post-hoc: slot values not present
    in the content (even with fuzzy prefix matching) are dropped.
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from ncms.application.adapters.corpus.llm_slot_labeler import (
        sync_label_corpus,
    )

    n = sync_label_corpus(
        source=source, domain=domain, output=output,  # type: ignore[arg-type]
        model=model, api_base=api_base, limit=limit,
        text_field=text_field, topic=topic,
    )
    click.echo(f"[adapters label-slots] wrote {n} GoldExample rows → {output}")


@adapters.command("judge-gold")
@click.option("--domain", required=True)
@click.option("--gold-path", type=click.Path(path_type=Path),
              default=None,
              help="Override gold path (default: manifest's gold_jsonl)")
@click.option("--n", "n_samples", type=int, default=25,
              help="Rows to sample for judging (default 25)")
@click.option("--seed", type=int, default=42)
@click.option("--model", default="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
              help="Judge LLM (defaults to Spark Nemotron)")
@click.option("--api-base", default="http://spark-ee7d.local:8000/v1",
              help="Judge LLM endpoint")
@click.option("--min-pct-correct", type=float, default=90.0,
              help="Fail (exit 1) if pct_correct below this threshold")
@click.option("--show-failures", type=int, default=10,
              help="Max failing rows to print (default 10)")
def adapters_judge_gold(
    domain: str, gold_path: Path | None, n_samples: int,
    seed: int, model: str, api_base: str,
    min_pct_correct: float, show_failures: int,
) -> None:
    """Validate gold-label quality BEFORE training to avoid doom-loop retrains.

    Samples ``--n`` rows from the domain's gold JSONL, asks a judge
    LLM to grade each row's slots / state_change / intent, and
    reports:

      - Aggregate verdict counts (correct / partially_wrong / severely_wrong).
      - Top issue types (histogram of issues seen across failing rows).
      - Sample failing rows with LLM-suggested corrections.

    Exits 1 when pct_correct < ``--min-pct-correct`` so the operator
    can gate training scripts on clean data.
    """
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from ncms.application.adapters.corpus.gold_judge import (
        sync_judge_gold,
    )
    from ncms.application.adapters.schemas import get_domain_manifest

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    path = gold_path or manifest.gold_jsonl

    click.echo(f"[judge-gold] domain={domain} gold={path} n={n_samples}")
    report = sync_judge_gold(
        domain=domain,  # type: ignore[arg-type]
        gold_path=path,
        n_samples=n_samples,
        model=model,
        api_base=api_base,
        seed=seed,
    )

    click.echo()
    click.echo("=== Aggregate ===")
    click.echo(f"  sampled: {report['n_sampled']}")
    for k, v in report['verdicts'].items():
        pct = v / report['n_sampled'] * 100 if report['n_sampled'] else 0.0
        click.echo(f"  {k:20s}: {v:3d}  ({pct:4.1f}%)")
    click.echo(f"  pct_correct: {report['pct_correct']:.1f}%  "
               f"(threshold: {min_pct_correct:.1f}%)")

    if report['issue_histogram']:
        click.echo()
        click.echo("=== Top issue types ===")
        for issue, n in list(report['issue_histogram'].items())[:10]:
            click.echo(f"  {n:3d}×  {issue}")

    if report['failures'] and show_failures > 0:
        click.echo()
        click.echo(f"=== Sample failures (first {min(show_failures, len(report['failures']))}) ===")
        for f in report['failures'][:show_failures]:
            click.echo(f"\n  [{f['verdict']}] slots={f['slots']}")
            click.echo(f"    content[:140]: {f['text'][:140]!r}")
            for issue in f['issues']:
                click.echo(f"    - {issue}")
            if f['corrections']:
                click.echo(f"    corrections: {f['corrections']}")

    if report['pct_correct'] < min_pct_correct:
        click.echo()
        click.echo(
            f"[judge-gold] FAIL: {report['pct_correct']:.1f}% < {min_pct_correct:.1f}% — "
            f"fix gold / labeller / canonical map before retraining.",
            err=True,
        )
        sys.exit(1)
    click.echo()
    click.echo(
        f"[judge-gold] PASS: {report['pct_correct']:.1f}% ≥ {min_pct_correct:.1f}%",
    )


@adapters.command("deploy")
@click.option("--domain", required=True)
@click.option("--version", required=True,
              help="Checkpoint version to deploy (must exist under adapters/checkpoints/)")
@click.option("--force", is_flag=True,
              help="Overwrite an existing deployed directory without confirmation")
def adapters_deploy(domain: str, version: str, force: bool) -> None:
    """Copy a trained checkpoint into the runtime path.

    Source: ``adapters/checkpoints/<domain>/<version>/``
    Target: ``~/.ncms/adapters/<domain>/<version>/``

    At runtime the extractor chain resolves the adapter directory
    via ``NCMS_SLM_CHECKPOINT_DIR`` or the per-domain deployment
    helper; either way it reads from the target path after deploy.
    """
    from ncms.application.adapters.schemas import get_domain_manifest

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    src = manifest.adapter_output_root / version
    dst = manifest.deployed_path(version)

    if not src.is_dir():
        raise click.ClickException(
            f"source checkpoint not found: {src}",
        )
    if dst.exists():
        if not force:
            raise click.ClickException(
                f"deployed path already exists: {dst}\n"
                f"re-run with --force to overwrite",
            )
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    click.echo(
        f"[adapters deploy] {domain}/{version} → {dst}",
    )


@adapters.command("status")
@click.option("--domain", default=None,
              help="Limit to one domain (default: all)")
def adapters_status(domain: str | None) -> None:
    """Show deployed adapter version + manifest sanity per domain."""
    import json

    from ncms.application.adapters.schemas import DOMAIN_MANIFESTS

    names = [domain] if domain else sorted(DOMAIN_MANIFESTS)
    for name in names:
        if name not in DOMAIN_MANIFESTS:
            click.echo(f"[adapters status] unknown domain: {name}")
            continue
        m = DOMAIN_MANIFESTS[name]  # type: ignore[index]
        click.echo(f"=== {name} ===")
        dep = m.deployed_path()
        click.echo(
            f"  deployed: {dep}  "
            f"(exists={dep.exists()}, default_version={m.default_version})",
        )
        manifest_path = dep / "manifest.json"
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text())
                click.echo(
                    f"    encoder: {data.get('encoder')}  "
                    f"version: {data.get('version')}  "
                    f"domain: {data.get('domain')}",
                )
                heads = data.get("head_labels") or {}
                for h, labels in heads.items():
                    click.echo(
                        f"    head[{h}]: {len(labels) if labels else 0} labels",
                    )
            except Exception as exc:
                click.echo(f"    (manifest unreadable: {exc})")
        else:
            click.echo("    (no manifest.json at deployed path)")
        click.echo()


__all__ = ["adapters"]
