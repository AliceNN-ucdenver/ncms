"""Run the 4-cell entity/SPLADE ablation matrix for MSEB.

Cells:
  1. GLiNER entities + SPLADE on
  2. SLM entities + SPLADE on
  3. GLiNER entities + SPLADE off
  4. SLM entities + SPLADE off

SPLADE-off is a true performance ablation: the harness receives
``--splade-off``, which disables engine construction, indexing, and search.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Cell:
    name: str
    entity_mode: str
    splade_off: bool


CELLS = (
    Cell("gliner_splade_on", "gliner_only", False),
    Cell("slm_splade_on", "slm_only", False),
    Cell("gliner_splade_off", "gliner_only", True),
    Cell("slm_splade_off", "slm_only", True),
)


def _latest_result(out_dir: Path) -> Path | None:
    results = sorted(
        out_dir.glob("*.results.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return results[0] if results else None


def _run_cell(args: argparse.Namespace, cell: Cell) -> dict[str, Any]:
    cell_out = args.out_dir / cell.name
    log_dir = args.out_dir / "logs"
    cell_out.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "benchmarks.mseb.harness",
        "--domain",
        args.domain,
        "--build-dir",
        str(args.build_dir),
        "--backend",
        "ncms",
        "--adapter-domain",
        args.adapter_domain,
        "--entity-extraction-mode",
        cell.entity_mode,
        "--out-dir",
        str(cell_out),
        "--top-k",
        str(args.top_k),
    ]
    if args.ctlg_adapter_domain:
        cmd.extend(["--ctlg-adapter-domain", args.ctlg_adapter_domain])
    if args.ctlg_adapter_version:
        cmd.extend(["--ctlg-adapter-version", args.ctlg_adapter_version])
    if cell.splade_off:
        cmd.append("--splade-off")
    if args.extra:
        cmd.extend(args.extra)

    log_path = log_dir / f"{cell.name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=args.repo_root, stdout=log, stderr=subprocess.STDOUT)

    result_path = _latest_result(cell_out)
    if proc.returncode != 0 or result_path is None:
        return {
            "cell": cell.name,
            "entity_mode": cell.entity_mode,
            "splade": "off" if cell.splade_off else "on",
            "status": f"failed exit={proc.returncode}",
            "log": str(log_path),
        }

    result = json.loads(result_path.read_text())
    overall = result.get("overall", {})
    return {
        "cell": cell.name,
        "entity_mode": cell.entity_mode,
        "splade": "off" if cell.splade_off else "on",
        "status": "ok",
        "r@1": overall.get("r@1"),
        "r@5": overall.get("r@5"),
        "mrr": overall.get("mrr"),
        "ingest_s": round(float(result.get("ingest_seconds", 0.0)), 2),
        "query_s": round(float(result.get("query_seconds", 0.0)), 2),
        "result": str(result_path),
        "log": str(log_path),
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    cols = ["cell", "entity_mode", "splade", "status", "r@1", "r@5", "mrr", "ingest_s", "query_s"]
    widths = {
        col: max(len(col), *(len(str(row.get(col, ""))) for row in rows))
        for col in cols
    }
    print(" | ".join(col.ljust(widths[col]) for col in cols), flush=True)
    print("-+-".join("-" * widths[col] for col in cols), flush=True)
    for row in rows:
        print(" | ".join(str(row.get(col, "")).ljust(widths[col]) for col in cols), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MSEB entity/SPLADE ablation matrix")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--adapter-domain", required=True)
    parser.add_argument("--ctlg-adapter-domain", default=None)
    parser.add_argument("--ctlg-adapter-version", default=None)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("benchmarks/results/mseb/entity_ablation"),
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Extra args passed to benchmarks.mseb.harness after '--'.",
    )
    args = parser.parse_args()
    if args.extra and args.extra[0] == "--":
        args.extra = args.extra[1:]

    rows = [_run_cell(args, cell) for cell in CELLS]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.out_dir / "entity_ablation_summary.json"
    summary_path.write_text(json.dumps(rows, indent=2, sort_keys=True))
    _print_table(rows)
    print(f"\nsummary: {summary_path}", flush=True)


if __name__ == "__main__":
    main()
