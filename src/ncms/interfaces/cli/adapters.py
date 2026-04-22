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
@click.option("--epochs", type=int, default=None)
@click.option("--batch-size", type=int, default=None)
@click.option("--learning-rate", type=float, default=None)
@click.option("--device", default=None,
              help="Force device: cpu / cuda / mps (auto by default)")
@click.option("--skip-sdg", is_flag=True,
              help="Reuse existing adapters/corpora/sdg_<domain>.jsonl")
@click.option("--skip-adversarial", is_flag=True,
              help="Skip adversarial augmentation phase")
def adapters_train(
    domain: str, version: str,
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

    # Delegate to the training entry point (defined in train.py's
    # `run_training` — a thin wrapper around the research-style main()).
    run_training(
        domain=domain,
        version=version,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        device=device,
        skip_sdg=skip_sdg,
        skip_adversarial=skip_adversarial,
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
