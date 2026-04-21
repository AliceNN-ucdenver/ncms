"""MSEB-SoftwareDev — prose-native state-evolution benchmark for software.

Rebalances the benchmark away from raw git diffs (MSEB-SWE / SWE-bench
Verified) toward the artefacts the ``software_dev`` adapter was
actually trained on: ADRs, RFCs, design docs, post-mortems, threat
models.  See ``benchmarks/mseb_softwaredev/README.md`` for the full
methodology.

Different from MSEB-SWE which focuses on code-diff state evolution.
See :mod:`benchmarks.mseb_swe` for that sibling domain (uses a
different adapter — ``swe_diff`` — trained separately).
"""
