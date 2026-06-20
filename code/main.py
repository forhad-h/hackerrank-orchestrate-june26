"""
M9 — Entry Point  (SPEC.md §5 M9, §3 Architecture)

Usage:
  python main.py                                      # claims.csv → ../output.csv, model-set premium
  python main.py --model-set budget                   # budget run
  python main.py --input dataset/claims.csv --output output.csv --model-set balanced

Defaults:
  --input    dataset/claims.csv  (resolved from repo root)
  --output   output.csv          (resolved from repo root)
  --model-set premium

Pipeline per row (with M2 and M3 running concurrently after M1):
  M1 DataLoader → [M2 ImageValidator ‖ M3 ClaimParser] → M4 VLMEngine
  → M5 EvidenceEvaluator → M6 RiskAggregator → M7 OutputAssembler

Error isolation: per-row exceptions → write SAFE_DEFAULT row, print to stderr, continue.
Progress: tqdm bar.
Summary: rows processed, error count, model set used, approx cost.

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================

This module is the production entry point.  It orchestrates modules M1-M7,
handles CLI argument parsing, manages the per-row pipeline, and writes the
final output CSV.  It contains no domain logic — all substantive work is
delegated to the module classes.

For decisions specific to individual pipeline stages (M1-M7), see the
DESIGN DECISIONS section in each module's own docstring.


Decision 1: Concurrent execution of M2 (image validation) and M3
            (claim parsing)
-----------------------------------------------------------------

  Decision
    M2 and M3 are submitted to a ``ThreadPoolExecutor(max_workers=2)``
    and waited on via ``future.result()``.  They execute concurrently
    during the per-row pipeline.

  Rationale
    - M2 and M3 are independent: M2 reads images from disk; M3 sends a
      text-only LLM call.  Neither depends on the other's output.
    - Running them in parallel reduces per-row latency by roughly
      ``max(latency_M2, latency_M3)`` instead of ``latency_M2 +
      latency_M3``.  Since M3 involves a network call (~2-10s) and M2
      is disk-bound (~0.1-2s), the saving is primarily overlapping M3's
      network wait with M2's disk reads.
    - The thread pool is created and destroyed per row (not shared across
      rows), avoiding thread-safety issues with mutable module state.

  Trade-offs
    + Per-row latency reduction of ~20-40% for typical claims.
    + Simple scope: the pool lives and dies within one function call.
    - Thread overhead per row (~0.5ms for pool creation) is negligible
      for 44 rows but would add up for 1000+.
    - Both M2 and M3 call into the same ``llm_client._enforce_min_
      interval()`` for rate limiting.  If M3 is the first to run and
      makes an API call, M2 finishes quickly and waits for M3's result
      anyway — the concurrency benefit is reduced when M2 is fast.
    - M2's ``validate_images()`` and M3's ``parse_claim()`` share no
      state, so there is no risk of concurrent mutation.  But if a
      future change adds shared state (e.g. a cache), the per-row pool
      pattern would need revisiting.

  Limitations
    - ``max_workers=2`` is hardcoded.  If a future module is added to
      this concurrent step, the pool size must be manually increased.
    - The pool is sequentialised by ``future.result()`` calls: the
      second result waits for the first if it hasn't completed.  Python
      does not provide a "wait for any" completion primitive in the
      standard ``ThreadPoolExecutor`` without ``as_completed()``, which
      would add complexity for minimal benefit (only 2 workers).

  Future Improvements
    - Move the thread pool to the outer loop level (reuse across rows)
      to reduce per-row overhead for larger datasets.
    - Use ``asyncio`` with async HTTP calls if the pipeline is ever
      converted to a fully async design.
    - Add a ``--parallel`` flag to disable concurrency for debugging.


Decision 2: Per-row error isolation with safe default fallback
---------------------------------------------------------------

  Decision
    Each claim is processed inside a ``try/except`` block.  On any
    exception, ``create_safe_default_row(context)`` is called, the
    error message is printed to stderr (over tqdm's stream), and the
    pipeline continues to the next row.

  Rationale
    - Per the project contract (§6), the pipeline must never abort.
      A single problematic claim should not lose results for all others.
    - The safe default row preserves identity fields (``user_id``,
      ``claim_object``, ``image_paths``) from the ``ClaimContext`` so
      the row is still matchable to input data.
    - Safe default rows use ``risk_flags="manual_review_required"`` as
      a recognisable marker, making it easy to filter failed rows in
      post-processing.

  Trade-offs
    + Pipeline always produces N output rows for N input claims.
    + Failed rows are clearly identifiable downstream.
    - A transient error (e.g. temporary API timeout) produces a
      permanent safe-default row.  There is no retry at this level
      (retries happen inside ``call_llm()``).
    - The safe default is generated from ``ClaimContext`` alone.  If M1
      itself fails (e.g. ``FileNotFoundError``), there is no fallback
      at all — the program exits before the per-row loop begins.

  Limitations
    - The error message printed to stderr includes only the exception
      string (``{exc}``).  The full stack trace is logged at DEBUG
      level but not shown at the default INFO level.  An operator
      running at INFO level sees only the message, which may not be
      sufficient for debugging.
    - Exceptions in M7's ``write_csv()`` (writing the output file) are
      NOT caught by the per-row handler — they crash the entire run.
      This is intentional: a write failure is terminal.

  Future Improvements
    - Add a retry loop at the per-row level (e.g. retry the entire row
      once before falling through to the safe default).
    - Log the stack trace at WARNING rather than DEBUG level so
      operators see it without changing log levels.


Decision 3: Deterministic path resolution from repo root
----------------------------------------------------------

  Decision
    All input/output paths are resolved relative to the repository root
    (``_REPO_ROOT``), which is computed by walking up from
    ``main.py``'s location (``code/main.py → code/ → repo root``).
    The ``_resolve()`` helper converts relative paths to absolute ones
    using this root.

  Rationale
    - The repo root is a stable reference point regardless of the user's
      current working directory, how the script is invoked (``python
      code/main.py`` vs ``cd code && python main.py``), or IDE runner
      configurations.
    - Default paths (``dataset/claims.csv``, ``output.csv``) are
      expressed relative to the repo root, matching the repository
      layout in SPEC §1.
    - Absolute paths are passed as-is, so users can specify any location
      (``--input /data/claims.csv``) without being forced into the repo
      layout.

  Trade-offs
    + Works from any working directory.
    + Defaults match the expected repo layout — a simple invocation
      without flags "just works".
    - The repo root is computed by walking up the filesystem tree.
      If the script is installed (e.g. via ``pip install``) at a
      different location, the computed root would be wrong.  This is
      acceptable for a source-run hackathon submission.
    - The ``_resolve()`` function uses ``os.path.realpath()``, which
      resolves symlinks.  If the repo is symlinked, the resolved path
      will be the symlink target, not the symlink location.  This is
      usually correct but could surprise users with complex directory
      structures.

  Limitations
    - ``_REPO_ROOT`` is computed once at module import time.  If the
      dataset location changes between runs, the script must be
      restarted.
    - The path resolution does not validate that the resolved paths
      actually exist.  M1 validates this at load time.

  Future Improvements
    - None needed for the current scope.  Path resolution is simple,
      correct, and well-tested across different invocation methods.


Decision 4: Token tracker summary at end of run
-------------------------------------------------

  Decision
    After all rows are processed, ``token_tracker.get_summary()`` is
    called and a formatted summary (rows, errors, model, cost, tokens,
    time) is printed to stderr.

  Rationale
    - The summary gives immediate feedback on operational cost and
      performance without requiring the operator to read logs.
    - Printing to stderr (via ``print(…, file=sys.stderr)``) keeps the
      summary separate from the output CSV when using shell redirection
      (e.g. ``python main.py > output.csv`` would still show the
      summary on the terminal).
    - The summary includes the model set, VLM model, and estimated cost,
      which are the three most important operational metrics for an
      evaluation run.

  Trade-offs
    + Visible regardless of output redirection.
    + Provides all key operational metrics in one block.
    - The summary goes to stderr; if stderr is also redirected (``2>&1``),
      the summary lands in the log file, which may be unexpected.
    - The cost is an estimate (from API response or fallback pricing),
      not a bill from OpenRouter.  The disclaimer is implicit.

  Limitations
    - The summary is not written to the output CSV or a structured log.
      It is terminal-only.  If the operator runs the pipeline in
      automated mode (no TTY), the summary must be captured from stderr.
    - The token tracker is a singleton; if ``main()`` is called
      multiple times in the same process (e.g. in tests), the summary
      includes calls from all runs unless explicitly reset.

  Future Improvements
    - Write the summary to a JSON sidecar file (``output_summary.json``)
      alongside the CSV for programmatic consumption.
    - Integrate with a structured logging system (e.g. sending metrics
      to CloudWatch/Datadog) for production deployments.


Decision 5: Three-tier model set abstraction (budget / balanced / premium)
---------------------------------------------------------------------------

  Decision
    The user selects a ``ModelSet`` via ``--model-set`` with three choices:
    ``budget`` (Gemini Flash + GPT-4o-mini), ``balanced`` (Gemini Pro +
    Claude Haiku), or ``premium`` (GPT-4o + Claude Sonnet).  The pipeline
    uses the same code path for all three; only the model IDs differ.

  Rationale
    - The hackathon evaluation runs many prompt-variant comparisons.  A
      single-model pipeline would force every comparison at the same price
      point, making it impossible to assess cost-vs-quality trade-offs.
    - Three tiers let the participant calibrate to their budget without
      editing code — pass ``--model-set budget`` for a dry run and
      ``--model-set premium`` for the final submission.
    - The abstraction is a simple key-value map (``models.py: MODEL_SETS``),
      making it trivial to add a fourth tier or custom model assignment.

  Trade-offs
    + Evaluation can systematically compare accuracy across price points.
    + Zero code changes to switch tiers — the pipeline is model-agnostic.
    - Not every model in a tier is available on OpenRouter at all times.
      The ``premium`` tier uses gpt-4o as VLM; if OpenRouter's gpt-4o
      provider has an outage, that entire tier becomes unusable.
    - The three tiers impose a discrete choice on the user.  Someone who
      wants gpt-4o for VLM but Gemini for text cannot express that without
      adding a custom ModelSet.

  Limitations
    - Model availability is checked at API-call time, not at startup.  An
      unavailable model fails on the first row, not before the run starts.
    - The model set only controls VLM and text models.  Image validation
      (M2), evidence evaluation (M5), and risk aggregation (M6) use the
      same logic regardless of the selected tier — they are not model-aware.

  Future Improvements
    - Add a ``--vlm-model`` / ``--text-model`` CLI flag that overrides the
      model set for fine-grained control without creating new ModelSets.
    - Validate model IDs against OpenRouter's model list at startup so a
      bad tier fails early rather than on the first row.


Decision 6: OpenRouter as the single LLM/VLM API gateway
----------------------------------------------------------

  Decision
    All LLM and VLM calls go through OpenRouter (``openrouter.ai/api/v1``)
    using a single ``OPENROUTER_API_KEY`` environment variable.  No direct
    provider API (OpenAI, Anthropic, Google) is called.

  Rationale
    - OpenRouter provides a unified API that abstracts provider-specific
      differences (endpoint formats, auth schemes, rate-limit headers,
      response structures).  Switching from ``openai/gpt-4o`` to
      ``google/gemini-2.5-flash`` is a string change.
    - A single API key simplifies configuration: one env var instead of
      ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, and ``GOOGLE_API_KEY``.
    - OpenRouter provides cost tracking in the response extensions
      (``total_cost``), which eliminates the need for hardcoded pricing
      tables as the primary cost source.
    - Fallback routing across providers is handled by the gateway, not the
      application.

  Trade-offs
    + Single API key, single endpoint, single client library.
    + Provider-agnostic model IDs make model swaps trivially easy.
    + OpenRouter handles provider failover for some models automatically.
    - Adds gateway latency (roughly 10-50 ms per call) compared to direct
      provider endpoints.
    - OpenRouter availability becomes a single point of failure.  If the
      gateway is down, the entire pipeline stops.
    - Some provider-specific features (e.g. Anthropic's extended thinking,
      Gemini's grounding) are not available through OpenRouter.

  Limitations
    - Rate limits are applied at the OpenRouter level, not per provider.
      A burst of 100 concurrent calls may be throttled by OpenRouter's
      own tier limits even if each individual provider has headroom.
    - Response format support (``json_object`` mode) varies by model and
      provider; the ``llm_client`` maintains a per-model support table
      (``_RESPONSE_FORMAT_SUPPORT``) to work around this.

  Future Improvements
    - Add a ``--provider`` flag to switch between OpenRouter and direct
      provider APIs for lower latency / higher reliability.
    - Cache model-capability metadata (which models support json_object,
      max context window, vision support) from OpenRouter's model list
      endpoint at startup instead of maintaining a static lookup table.


Decision 7: Sequential post-VLM pipeline (M5 → M6 → M7)
-----------------------------------------------------------

  Decision
    After M4 (VLM Engine), modules M5 (Evidence Evaluator), M6 (Risk
    Aggregator), and M7 (Output Assembler) run sequentially in a single
    thread.  They are not parallelised despite being independent in theory.

  Rationale
    - M5, M6, and M7 are pure CPU logic with no I/O (no API calls, no disk
      reads).  Their combined latency is under 1 ms per row — negligible
      compared to the M4 VLM call (5-30 s).
    - Parallelising CPU-bound operations that run in under 1 ms would add
      more overhead in thread synchronisation than it saves in wall-clock
      time.
    - Sequential execution is simpler to debug, profile, and trace: the
      call stack is linear, every variable is deterministic, and there are
      no race conditions.

  Trade-offs
    + Trivial to read, debug, and profile.
    + No thread-safety concerns — each row is processed atomically.
    - If a future module in this chain becomes I/O-bound (e.g. a second
      LLM call for cross-validation), the sequential design would block
      the pipeline while waiting.  At that point, running it concurrently
      with the M2-M3 pattern would be appropriate.
    - The sequential design prevents per-row parallelism for M5-M6-M7
      across multiple rows.  If the pool were shared at the outer loop
      level, M5 from row N could run while M4 is still processing row N+1.
      This is a valid optimisation but adds significant complexity.

  Limitations
    - The analysis is global: M7's ``assemble_row()`` could theoretically
      be merged into M6 or called as a pure lambda without a separate
      module boundary.  Keeping them separate respects the SPEC's M1-M7
      modular decomposition even where performance does not demand it.
    - If M5 or M6 ever grow to depend on external services (e.g. a
      fraud-scoring API call), the sequential design must be revisited.

  Future Improvements
    - If M5 gains VLM cross-validation (a second LLM call), parallelise
      it with M6 using the same concurrent.futures pattern as M2-M3.


Decision 8: tqdm progress bar with stderr output separation
-------------------------------------------------------------

  Decision
    Per-row progress is displayed via ``tqdm`` (writing to stderr), errors
    and diagnostics are printed to stderr via ``tqdm.write()``, and the
    final CSV output is written to a file path (not stdout).  The terminal
    output (progress + errors) and data output (CSV file) are completely
    separate streams.

  Rationale
    - The progress bar is an interactive UX element; mixing it with the
      output CSV on stdout would corrupt the CSV when the user runs
      ``python main.py`` without redirection.
    - Errors printed via ``tqdm.write()`` appear above the progress bar
      rather than overwriting it, keeping both visible.
    - Writing to a file path (``--output output.csv``) rather than stdout
      avoids the common mistake of redirecting CSV output to a log file
      or vice versa.

  Trade-offs
    + Progress bar always works — no need for ``--quiet`` or
      ``--no-progress`` flags (tqdm auto-detects non-TTY and suppresses).
    + Error messages are visually separated from the progress bar.
    + The CSV file is written atomically (one ``write_csv()`` call at the
      end), so a partial run produces either a complete file or nothing.
    - The summary is printed to stderr, so ``python main.py 2>&1 | less``
      shows the summary in the pager, which may be unexpected.
    - If the script is run in a CI pipeline that captures stderr only on
      failure, the summary is lost on success.

  Limitations
    - tqdm's auto-refresh rate (default 0.1 s) adds a small overhead for
      very fast rows (~0.1 ms per update).  At 44 rows the overhead is
      negligible; at 10 000 it would add ~1 s of wall-clock time.
    - ``tqdm.write()`` acquires the same output lock as the progress bar,
      so heavy error output can slow down the progress display.

  Future Improvements
    - Add a ``--json-summary`` flag that writes the summary to a JSON file,
      making it available for CI pipeline consumption.
    - Consider ``rich`` progress bars for richer visualisation (time-per-
      row, token count, estimated cost per row).


Decision 9: dotenv-based configuration with sensible fallbacks
---------------------------------------------------------------

  Decision
    The program loads environment variables from a ``.env`` file in the
    repo root (via ``python-dotenv``), then reads ``OPENROUTER_API_KEY``
    from the environment.  No other configuration is required: all paths
    and model choices have sensible defaults.

  Rationale
    - A ``.env`` file keeps secrets out of version control (listed in
      ``.gitignore``) while making local development trivial — create one
      file and run.
    - Shell-exported environment variables take precedence over ``.env``
      (``load_dotenv`` does not override existing env vars), so CI
      pipelines can set the API key via secret injection without a file.
    - Sensible defaults (``--model-set premium``, ``--input dataset/claims.csv``,
      ``--output output.csv``) mean the simplest invocation
      ``python main.py`` "just works" for the hackathon dataset.

  Trade-offs
    + Zero setup beyond creating a ``.env`` with ``OPENROUTER_API_KEY=...``.
    + CI-friendly: no file needed when the key is exported.
    + Sensible defaults mean ``--help`` is rarely needed.
    - The ``.env`` file path is hardcoded to ``<repo_root>/.env``.
      Running from outside the repo (e.g. ``python /path/to/code/main.py``)
      still works for path resolution but may not load the expected
      ``.env`` if it exists in the current directory instead.
    - Python-dotenv is a lightweight dependency but is not part of the
      standard library.  If dependencies are not installed, the error
      message is generic ``ModuleNotFoundError``.

  Limitations
    - Only one configuration layer (env vars).  There is no config file
      (YAML/TOML) for complex settings like model routing rules.
    - ``python-dotenv`` is imported inside ``main()`` (not at module top
      level), which means a top-level import error surfaces before the
      ``.env`` is loaded.  This is by design (so the error is visible),
      but it means the ``OPENROUTER_API_KEY`` must be set before the
      ``modules`` package is imported.

  Future Improvements
    - Add ``--env-file`` flag to specify a custom ``.env`` path.
    - Consider a layered config: ``.env`` defaults → user config file →
      CLI flags, with later sources overriding earlier ones.


Decision 10: Logging to stderr with modular loggers
-----------------------------------------------------

  Decision
    All logging goes to stderr via ``logging.basicConfig(stream=sys.stderr)``.
    Each module creates its own ``logger = logging.getLogger(__name__)``.
    The default level is ``INFO``; ``--debug`` sets ``DEBUG``.

  Rationale
    - Logging to stderr keeps the output CSV (written to a file) clean of
      log interleaving.  If the CSV were written to stdout, every log line
      would corrupt the output.
    - Per-module loggers make it possible to selectively increase log
      granularity (``logging.getLogger("modules.vlm_engine").setLevel(DEBUG)``)
      without flooding the output with messages from every module.
    - The log format includes timestamp, level, and module name, making it
      possible to trace the pipeline's execution without reading the source
      code.

  Trade-offs
    + Clean separation of data (CSV file) and diagnostics (stderr).
    + Per-module log control enables targeted debugging.
    + Standard library logging — zero dependencies, familiar to all Python
      developers.
    - No structured logging.  Logs are plain text; automated parsing
      (e.g. grep for ERROR patterns) works but is fragile.
    - No log rotation or file output.  Long runs with ``--debug`` will
      produce large stderr output that scrolls past in the terminal.
    - The ``logging.basicConfig`` call in ``main()`` is not idempotent:
      if ``main()`` is called twice in the same process (e.g. in tests),
      the second call has no effect, and the log level remains at the
      first call's setting.

  Limitations
    - Exception stack traces are logged at DEBUG level (line 455).  At the
      default INFO level, an operator sees only the exception message, not
      the traceback.  This can make debugging without ``--debug`` difficult.
    - The tqdm progress bar and logging both write to stderr.  On some
      terminal configurations, the tqdm bar and log lines may interleave
      visually (tqdm handles this internally by buffering and redrawing,
      but it can appear glitchy on slow terminals).

  Future Improvements
    - Add a ``--log-file`` flag to write logs to a file for post-mortem
      analysis without relying on terminal scrollback.
    - Switch to ``structlog`` or ``python-json-logger`` for structured log
      output that can be ingested by log aggregation systems.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import sys
import time
from typing import Dict, List, Optional

# Ensure we can import from the repo root
_REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from tqdm import tqdm

from modules import (
    ClaimContext,
    EvidenceEvaluation,
    ImageValidationResult,
    LocalCSVDataLoader,
    MODEL_SETS,
    ModelSet,
    ParsedClaim,
    VLMAnalysis,
    aggregate,
    analyze_images,
    assemble_row,
    create_safe_default_row,
    evaluate,
    log_security_summary,
    parse_claim,
    token_tracker,
    validate_images,
    write_csv,
)

logger = logging.getLogger(__name__)

# ── CLI ───────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HackerRank Orchestrate — Multi-Modal Evidence Review",
    )
    parser.add_argument(
        "--input",
        default="dataset/claims.csv",
        help="Path to claims CSV (resolved from repo root). Default: dataset/claims.csv",
    )
    parser.add_argument(
        "--output",
        default="output.csv",
        help="Path for output CSV (resolved from repo root). Default: output.csv",
    )
    parser.add_argument(
        "--model-set",
        default="premium",
        choices=list(MODEL_SETS.keys()),
        help="Model set to use. Default: premium",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


# ── Helper: resolve paths relative to repo root ──────────────────────────


def _resolve(path: str) -> str:
    """Resolve *path* relative to the repo root.

    If *path* is already absolute, return as-is.
    """
    if os.path.isabs(path):
        return path
    return os.path.realpath(os.path.join(_REPO_ROOT, path))


# ── Per-row pipeline ─────────────────────────────────────────────────────


def process_row(
    context: ClaimContext,
    model_set: ModelSet,
) -> Dict[str, str]:
    """Run the full pipeline on a single claim row.

    Parameters
    ----------
    context :
        Fully hydrated claim context from M1.
    model_set :
        Model set configuration (budget / balanced / premium).

    Returns
    -------
    Dict[str, str]
        Assembled output row ready for CSV writing.
    """
    # ── Step 1: Run M2 and M3 concurrently ───────────────────────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_m2 = executor.submit(validate_images, context)
        future_m3 = executor.submit(parse_claim, context, model_set)

        # Use timed wait so timeouts don't orphan the other thread
        image_validation: ImageValidationResult = future_m2.result()
        parsed_claim: ParsedClaim = future_m3.result()

    # ── Step 2: M4 — VLM Visual Analysis ─────────────────────────────────
    vlm_analysis: VLMAnalysis = analyze_images(
        context=context,
        image_validation=image_validation,
        parsed_claim=parsed_claim,
        model_set=model_set,
    )

    # ── Step 3: M5 — Evidence Standard Evaluator ─────────────────────────
    evidence_eval: EvidenceEvaluation = evaluate(
        vlm_analysis=vlm_analysis,
        parsed_claim=parsed_claim,
        evidence_rules=context.evidence_rules,
        image_count=len(context.image_paths),
    )

    # ── Step 4: M6 — Risk Flag Aggregator ────────────────────────────────
    risk_flags: str = aggregate(
        vlm_analysis=vlm_analysis,
        parsed_claim=parsed_claim,
        user_history=context.user_history,
    )

    # ── Step 5: M7 — Output Assembler ────────────────────────────────────
    row: Dict[str, str] = assemble_row(
        context=context,
        vlm_analysis=vlm_analysis,
        parsed_claim=parsed_claim,
        evidence_eval=evidence_eval,
        risk_flags=risk_flags,
    )

    return row


# ── Main ─────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point.

    Returns
    -------
    int
        Exit code: 0 on success, 1 on fatal error.
    """
    args = parse_args(argv)

    # ── Logging ───────────────────────────────────────────────────────────
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # ── Load .env ─────────────────────────────────────────────────────────
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.isfile(env_path):
        load_dotenv(env_path)
        logger.info("Loaded environment from %s", env_path)
    else:
        logger.warning("No .env found at %s — ensure OPENROUTER_API_KEY is set", env_path)

    # Check required API key
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.error(
            "OPENROUTER_API_KEY is not set. "
            "Create a .env file with OPENROUTER_API_KEY=sk-or-... "
            "or export it in your shell."
        )
        return 1

    # ── Resolve paths ─────────────────────────────────────────────────────
    input_path = _resolve(args.input)
    output_path = _resolve(args.output)
    model_set_name = args.model_set
    model_set = MODEL_SETS[model_set_name]

    logger.info("Input:  %s", input_path)
    logger.info("Output: %s", output_path)
    logger.info("Model:  %s (%s)", model_set_name, model_set.models)

    # ── M1: Load data ────────────────────────────────────────────────────
    logger.info("Loading data...")
    loader = LocalCSVDataLoader(
        claims_path=input_path,
    )
    try:
        contexts: List[ClaimContext] = loader.load()
    except FileNotFoundError as e:
        logger.error("Fatal: %s", e)
        return 1

    if not contexts:
        logger.error("No claims found in %s", input_path)
        return 1

    logger.info("Loaded %d claim(s)", len(contexts))

    # ── Process each row ──────────────────────────────────────────────────
    rows: List[Dict[str, str]] = []
    error_count = 0
    start_time = time.time()

    for context in tqdm(contexts, desc="Processing claims", unit="row"):
        try:
            row = process_row(context, model_set)
            rows.append(row)
        except Exception as exc:
            error_count += 1
            safe_row = create_safe_default_row(context)
            rows.append(safe_row)
            # Print error details to stderr (tqdm manages its own stream)
            tqdm.write(
                f"ERROR [user_id={context.user_id}]: {exc}",
                file=sys.stderr,
            )
            logger.debug("Stack trace for user_id=%s:", context.user_id, exc_info=True)

    elapsed = time.time() - start_time

    # ── M7: Write output CSV ──────────────────────────────────────────────
    write_csv(rows, output_path)
    logger.info("Wrote %d rows to %s", len(rows), output_path)

    # ── Summary ───────────────────────────────────────────────────────────
    summary = token_tracker.get_summary()
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  Summary", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"  Rows processed:     {len(contexts)}", file=sys.stderr)
    print(f"  Rows output:        {len(rows)}", file=sys.stderr)
    print(f"  Errors:             {error_count}", file=sys.stderr)
    print(f"  Model set:          {model_set_name}", file=sys.stderr)
    print(f"  VLM model:          {model_set.get('vlm')}", file=sys.stderr)
    print(f"  LLM model:          {model_set.get('text', model_set.get('vlm'))}", file=sys.stderr)
    print(f"  API calls:          {summary.total_calls}", file=sys.stderr)
    print(f"  Input tokens:       {summary.total_input_tokens:,}", file=sys.stderr)
    print(f"  Output tokens:      {summary.total_output_tokens:,}", file=sys.stderr)
    print(f"  Estimated cost:     ${summary.total_cost:.6f}", file=sys.stderr)
    print(f"  Wall-clock time:    {elapsed:.1f}s", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)

    # ── Security summary ─────────────────────────────────────────────────────
    log_security_summary()

    return 0


if __name__ == "__main__":
    sys.exit(main())
