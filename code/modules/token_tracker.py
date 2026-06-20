"""
Token Tracker — aggregated per-call metrics (tokens, cost, latency).

Populated transparently by ``llm_client.call_llm()``.  All metrics are
read from the API response — no hardcoded pricing tables as primary source.

Usage
-----

    from modules.token_tracker import token_tracker

    # (llm_client automatically records each call)
    summary = token_tracker.get_summary()
    print(summary.total_cost)           # total USD across all calls
    print(summary.by_module["M4"])      # per-module breakdown

    # Reset between evaluation runs
    token_tracker.reset()

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================


Decision 1: Singleton pattern shared across all modules
---------------------------------------------------------

  Decision
    ``token_tracker = TokenTracker()`` is a module-level singleton
    created at import time.  Every module imports the same instance via
    ``from modules.token_tracker import token_tracker``.  Calls are
    recorded by ``llm_client.call_llm()`` and read by ``main()`` for
    the pipeline summary.

  Rationale
    - All API calls in a single pipeline run should be aggregated into
      one summary.  A singleton makes this automatic — every module that
      calls ``call_llm()`` contributes to the same tracker without
      explicit wiring.
    - The singleton is a simple pattern with no dependency injection
      needed.  Every module that needs it just imports it.
    - ``main.py`` calls ``token_tracker.get_summary()`` once at the end
      to produce the "Summary" block.  No wiring or aggregation logic
      is needed in the caller.

  Trade-offs
    + Zero wiring: any module that imports it gets the shared instance.
    + Transparent: ``call_llm()`` records automatically; no module calls
      ``record()`` manually.
    - Singleton pattern makes testing harder: tests must explicitly reset
      the tracker between runs or risk cross-test contamination.
    - If two independent pipeline runs share a Python process (e.g. in
      M8, which runs 27 configs sequentially), the tracker must be reset
      between configs.  The evaluation pipeline (M8) calls
      ``token_tracker.reset()`` per config for this reason.

  Limitations
    - The singleton is process-scoped only.  Distributed pipelines
      (multiple machines) cannot share a single tracker.
    - If the import order changes and a module records a call before
      ``main()`` is ready to read the summary, the tracker still
      accumulates correctly — but the summary may include calls from
      unexpected modules.

  Future Improvements
    - Make the tracker instance injectable (e.g. via a context object)
      so that tests can pass a fresh instance without global state
      reset.
    - Consider a ``with``-based pattern for evaluation runs that
      automatically resets the tracker when entering an evaluation
      context.


Decision 2: Running-average latency calculation
-------------------------------------------------

  Decision
    ``TokenSummary.merge()`` maintains a running average of latency
    (``avg_latency``) rather than storing all individual latencies and
    computing the mean on request.

  Rationale
    - Storing all latencies for N calls would use O(N) memory.  The
      running average uses O(1).
    - The running average formula (``avg = (avg * n + new) / (n + 1)``)
      is numerically stable for the expected call counts (< 1000).
    - The per-module breakdown (``by_module``) re-implements the same
      running average independently for each module key.

  Trade-offs
    + O(1) memory regardless of call count.
    + Numerically stable for 100-500 calls (hackathon batch size).
    - Discards variance information — only the mean survives.
      Min/max/p95 latency cannot be computed.
    - The running average in per-module summaries is computed independently
      and may differ slightly from the global average due to floating-
      point precision in the module-level division.

  Limitations
    - No median, p95, p99, or standard deviation.  A single very slow
      call (e.g. 90s timeout on first attempt) affects the average but
      its impact on the median cannot be assessed.
    - The running average includes ALL attempts (including retries).
      If the primary model fails twice and the fallback succeeds, all
      three attempts' latencies are averaged together.

  Future Improvements
    - Track min/max latency alongside the mean for a modest O(1) memory
      increase.
    - Track latency per attempt (primary vs fallback) separately so the
      aggregate can distinguish "expected latency" from "retry overhead".


Decision 3: Per-model and per-module breakdowns
-------------------------------------------------

  Decision
    ``TokenSummary`` tracks both ``by_model`` (model ID → call count)
    and ``by_module`` (module name → nested TokenSummary).  The
    per-module summary re-computes totals independently rather than
    sharing the global counters.

  Rationale
    - ``by_model`` enables cost attribution per model — you can see
      how many calls went to gpt-4o vs gpt-4o-mini in a run.
    - ``by_module`` isolates the cost of M3 (claim parsing) vs M4
      (VLM analysis) vs evaluation overhead, enabling module-level
      cost optimisation.
    - The per-module summary is a full TokenSummary (calls, tokens,
      cost, latency, model breakdown), so consumers can drill into
      any module with the same interface.

  Trade-offs
    + Rich breakdown: operators can see exactly which module and model
      consumed how many tokens.
    + Consistent interface — a per-module summary is the same type as
      the global summary.
    - Each per-module summary independently computes its own running
      average, which is O(number-of-modules) work per call.  With
      3-4 modules, this is negligible.
    - ``by_model`` only stores call counts per model, not token totals
      or cost.  To get per-model token totals, the consumer must
      iterate per-module summaries.

  Limitations
    - Module names are strings set by the caller (``call_llm(module_name=
      "M4")``).  A typo (e.g. ``"M4 "`` with trailing space) creates a
      separate module entry silently.
    - The per-model count does not distinguish between primary and
      fallback attempts.  Both are counted under the model ID that
      actually served the response.

  Future Improvements
    - Expand ``by_model`` to store token/cost totals alongside call
      counts, eliminating the need to aggregate per-module data.
    - Add a ``by_module_by_model`` nested breakdown for the full
      matrix.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class PerCallMetrics:
    """Metrics recorded for a single LLM/VLM call."""

    model: str  # model ID, e.g. "openai/gpt-4o"
    input_tokens: int  # from response.usage.prompt_tokens
    output_tokens: int  # from response.usage.completion_tokens
    latency_s: float  # wall-clock seconds
    cost_usd: float  # from OpenRouter response cost extension, or fallback
    module: str  # e.g. "M3", "M4"
    model_tier: str  # e.g. "budget", "premium", "fallback"
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenSummary:
    """Aggregated metrics across one or more calls."""

    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    avg_latency: float = 0.0
    by_model: Dict[str, int] = field(default_factory=dict)  # model → call count
    by_module: Dict[str, "TokenSummary"] = field(default_factory=dict)

    def merge(self, metrics: PerCallMetrics) -> None:
        """Merge a single call's metrics into this summary."""
        n = self.total_calls
        self.total_calls += 1
        self.total_input_tokens += metrics.input_tokens
        self.total_output_tokens += metrics.output_tokens
        self.total_cost += metrics.cost_usd
        # Running average
        self.avg_latency = (self.avg_latency * n + metrics.latency_s) / (n + 1)

        self.by_model[metrics.model] = self.by_model.get(metrics.model, 0) + 1

        # Per-module tracking (flat update — avoids infinite recursion
        # that would happen if merge() called itself on the child summary).
        if metrics.module not in self.by_module:
            self.by_module[metrics.module] = TokenSummary()
        child = self.by_module[metrics.module]
        child.total_calls += 1
        child.total_input_tokens += metrics.input_tokens
        child.total_output_tokens += metrics.output_tokens
        child.total_cost += metrics.cost_usd
        child.avg_latency = (
            child.avg_latency * (child.total_calls - 1) + metrics.latency_s
        ) / child.total_calls
        child.by_model[metrics.model] = child.by_model.get(metrics.model, 0) + 1


# ── Tracker ───────────────────────────────────────────────────────────────────


class TokenTracker:
    """Aggregator for LLM/VLM call metrics.  Populated by ``llm_client.call_llm()``."""

    def __init__(self) -> None:
        self._calls: List[PerCallMetrics] = []

    def record(self, metrics: PerCallMetrics) -> None:
        """Record a single call's metrics."""
        self._calls.append(metrics)
        logger.debug(
            "TokenTracker: %s %s | %d in / %d out / $%.6f / %.2fs",
            metrics.module,
            metrics.model,
            metrics.input_tokens,
            metrics.output_tokens,
            metrics.cost_usd,
            metrics.latency_s,
        )

    def get_summary(self) -> TokenSummary:
        """Aggregate all recorded calls into a summary."""
        summary = TokenSummary()
        for call in self._calls:
            summary.merge(call)
        return summary

    def get_module_summary(self, module: str) -> TokenSummary:
        """Aggregate calls for a specific module only."""
        summary = TokenSummary()
        for call in self._calls:
            if call.module == module:
                summary.merge(call)
        return summary

    def reset(self) -> None:
        """Clear all recorded metrics."""
        self._calls.clear()


#: Module-level singleton used by ``llm_client.call_llm()``.
token_tracker = TokenTracker()


# ── Exports ───────────────────────────────────────────────────────────────────

__all__ = [
    "PerCallMetrics",
    "TokenSummary",
    "TokenTracker",
    "token_tracker",
]
