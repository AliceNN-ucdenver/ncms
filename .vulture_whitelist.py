"""Vulture whitelist — names that look unused but have load-bearing signatures.

Vulture flags these because they're unused inside the function body.  They
cannot be removed or renamed because the call site dictates the signature.

Run vulture with::

    uv run --with vulture vulture src/ncms/ .vulture_whitelist.py \\
        --min-confidence 80
"""

# Signal handler callbacks: the (signum, frame) signature is fixed by
# Python's `signal` module.  We ignore `frame` in every handler.
frame  # noqa: F821  (in bus_agent.py, api.py, dashboard.py)

# Prometheus metric no-op shims: `inc(amount=1)` and `dec(amount=1)` must
# accept `amount` even when the null implementation ignores it, to match
# the real prometheus_client.Counter/Gauge signatures.
amount  # noqa: F821  (in NullMetric.inc / NullMetric.dec)
