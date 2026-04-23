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

    from ncms.application.adapters.schemas import DOMAIN_MANIFESTS

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
@click.option("--domain", required=True, help="Domain name (see `ncms adapters list`)")
@click.option("--target", type=int, default=3000,
              help="Pre-dedup target row count (default 3000)")
@click.option("--seed", type=int, default=17)
@click.option("--output", type=click.Path(path_type=Path), default=None,
              help="Output JSONL (defaults to adapters/corpora/sdg_<domain>.jsonl)")
def adapters_generate_sdg(
    domain: str, target: int, seed: int, output: Path | None,
) -> None:
    """Run the typed-slot SDG template expander for one domain."""
    from ncms.application.adapters.schemas import get_domain_manifest
    from ncms.application.adapters.sdg.expander import (
        _dedupe,
        expand_domain,
    )
    from ncms.application.adapters.corpus.loader import dump_jsonl

    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    out = output or manifest.sdg_jsonl
    raw = expand_domain(domain, target=target, seed=seed)  # type: ignore[arg-type]
    deduped = _dedupe(raw)
    dump_jsonl(deduped, out)
    click.echo(
        f"[adapters generate-sdg] domain={domain} "
        f"raw={len(raw)} deduped={len(deduped)} → {out}",
    )


@adapters.command("train")
@click.option("--domain", required=True)
@click.option("--version", required=True, help="Target adapter version (e.g. v7)")
@click.option("--target-size", type=int, default=3000,
              help="SDG pre-dedup target (default 3000 — matches the v7 rewrite)")
@click.option("--adversarial-size", type=int, default=300,
              help="Adversarial examples to generate (default 300)")
@click.option("--epochs", type=int, default=None)
@click.option("--batch-size", type=int, default=None)
@click.option("--learning-rate", type=float, default=None)
@click.option("--device", default=None,
              help="Force device: cpu / cuda / mps (auto by default)")
@click.option("--skip-sdg", is_flag=True,
              help="Reuse existing adapters/corpora/sdg_<domain>.jsonl as-is")
@click.option("--skip-adversarial", is_flag=True,
              help="Skip adversarial augmentation phase")
def adapters_train(
    domain: str, version: str,
    target_size: int, adversarial_size: int,
    epochs: int | None, batch_size: int | None,
    learning_rate: float | None,
    device: str | None,
    skip_sdg: bool, skip_adversarial: bool,
) -> None:
    """Full training loop: SDG → gold/adversarial merge → LoRA → gate.

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
        skip_sdg=skip_sdg,
        skip_adversarial=skip_adversarial,
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
    click.echo(f"=== Aggregate ===")
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
