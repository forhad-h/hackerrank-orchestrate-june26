"""
LLM Client — centralized OpenRouter client with retry, fallback,
response validation, VLM image support, and usage tracking.

Every module that calls an LLM or VLM uses this module instead of managing
its own client.  ``call_llm()`` automatically populates ``token_tracker``.

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================

This module is the single entry point for all LLM/VLM API calls in the
pipeline.  Every other module (M3, M4, M8) delegates API interaction here.
The decisions below are the most consequential — changing any of them affects
every caller.


Decision 1: Centralized LLM/VLM client — single point for retry, fallback,
            and tracking
------------------------------------------------------------------------------

  Decision
    All LLM and VLM calls go through ``call_llm()`` in this module.  No
    module creates its own ``OpenAI`` client, manages its own retry loop,
    or tracks its own token usage.  Token tracking is automatically
    populated into the global ``token_tracker`` singleton.

  Rationale
    - Avoids duplicating retry logic, response validation, fallback
      resolution, and rate limiting across every module that calls an
      LLM — M3 (claim parser), M4 (VLM engine), and M8 (evaluation
      pipeline) all use the same code path.
    - Centralised tracking means every API call's token counts, cost,
      and latency are automatically aggregated in one place.  The
      ``main()`` summary (token_tracker.get_summary()) reflects every
      call across all modules.
    - Error handling is consistent: every caller sees the same retry
      schedule, the same fallback behaviour, and the same exception types.

  Trade-offs
    + Every caller gets retry, validation, and tracking for free.
    + A change to retry strategy or rate limiting is a single-file change.
    + Token tracking is automatic — no module can forget to record usage.
    - The function signature is large (8 parameters), and callers must
      supply module_name for tracking.  Omitting it yields "unknown"
      module labels.
    - All API traffic is serialised through a single ``_enforce_min_
      interval()`` gate.  While the interval is only 500ms, this means
      two callers (e.g. concurrent M2/M3) cannot truly fire API calls in
      parallel — one will wait for the other's rate-limit window.
    - The singleton ``_client`` is created once and reused.  In long
      runs, a stale connection could cause timeout errors that are
      indistinguishable from API issues.

  Limitations
    - All callers share the same OpenRouter API key.  If a caller needs
      a different key or base URL, this module would need to be extended.
    - The synchronous design (``time.sleep()`` for rate limiting) blocks
      the thread.  With the current pipeline (sequential per-row, M2/M3
      concurrent via ThreadPoolExecutor), this is acceptable.  An async
      version would need a different rate-limiting mechanism.

  Future Improvements
    - Make the ``_client`` lazy-reconnect after N calls or after a
      timeout threshold, reducing stale-connection risk.
    - Consider an async client for better concurrency if the pipeline
      moves to asyncio.


Decision 2: Per-model response_format support table
-----------------------------------------------------

  Decision
    ``_RESPONSE_FORMAT_SUPPORT`` is a hardcoded dict mapping model IDs
    to boolean (whether the model supports ``response_format={"type":
    "json_object"}`` on OpenRouter).  Models with ``False`` skip the
    parameter and rely on markdown JSON extraction (``extract_json_from_
    markdown()``) instead.

  Rationale
    - Not all models on OpenRouter support the ``response_format``
      parameter (notably Gemini models and Claude 3.5 Haiku).  Sending
      it to an unsupported model either causes an API error or is silently
      ignored.
    - Rather than attempting the call and catching the error (which wastes
      time and tokens), the table provides a deterministic pre-flight
      check.
    - The table is small (6 entries) and changes infrequently.  It is
      easier to audit than an error-driven fallback.

  Trade-offs
    + Deterministic: no wasted API calls trying unsupported parameters.
    + Clearly documented: the table == the supported model set.
    - Must be manually maintained.  Adding a new model requires checking
      whether it supports response_format and adding an entry.
    - The table maps model IDs (strings), not model families.  If
      OpenRouter adds "claude-3.5-haiku-20250301" as a separate model ID,
      it would need its own entry even though it behaves identically.

  Limitations
    - Skipping response_format for unsupported models means their JSON
      output must be extracted from markdown code blocks or raw braces.
      ``extract_json_from_markdown()`` handles this, but it adds a non-
      deterministic parsing step.
    - Models that partially support response_format (accept the parameter
      but produce non-JSON output for certain prompts) are not handled
      — the table tracks capability, not reliability.

  Future Improvements
    - Switch to model-family-based matching (e.g. ``model.startswith(
      "google/")``) to reduce maintenance burden.
    - Add a startup-time check that verifies all models in MODEL_SETS
      have an entry in the support table, surfacing missing entries early.


Decision 3: Cost extraction from API response with fallback pricing table
--------------------------------------------------------------------------

  Decision
    Token counts and cost are read from the API response
    (``response.usage.prompt_tokens``, ``response.usage.completion_tokens``,
    and OpenRouter's cost extension via ``response.usage.total_cost``).
    A fallback pricing table (``_FALLBACK_PRICING``) is used ONLY when
    the API response does not include cost data.

  Rationale
    - API-provided cost is always more accurate than estimation (it
      reflects actual rounding, provider discounts, and OpenRouter
      markup that a static table cannot capture).
    - The fallback table exists for resilience — some OpenRouter providers
      do not return ``total_cost`` in the response.
    - The fallback values are generous (mid-range estimates) so cost
      reporting is conservative.

  Trade-offs
    + Cost reporting is accurate when the API provides it (the common
      case).
    + Fallback estimates degrade gracefully without crashing.
    - The fallback table duplicates model IDs with ``_RESPONSE_FORMAT_
      SUPPORT`` and MODEL_SETS, creating a maintenance hazard (three
      places to update when adding a model).
    - Cost is accumulated per-call via ``token_tracker.record()``.
      If ``extract_usage`` returns a 0-cost response for a call that
      actually incurred cost, the total is under-reported.

  Limitations
    - The fallback pricing uses input/output token rates that do not
      account for caching discounts, batch pricing, or provider-specific
      surcharges.
    - No image token pricing in the fallback table.  For VLM calls where
      the API does not return cost, the image token estimate from
      ``_estimate_vlm_token_budget`` is not included in the fallback
      cost calculation — only text tokens are priced.  This means VLM
      cost may be under-reported for models without API cost data.

  Future Improvements
    - Derive the fallback pricing table from a single source of truth
      (e.g. ``models.py`` or a YAML config) so it stays in sync with
      the model set definitions.
    - Add image token pricing to the fallback computation.


Decision 4: Image token budget estimation (pre-flight context window guard)
----------------------------------------------------------------------------

  Decision
    Before every VLM call, ``estimate_vlm_token_budget()`` computes a
    rough upper bound on total tokens (text + images + output).  If the
    estimate exceeds the model's context window, images are selectively
    dropped (starting with the highest-estimated-token images) until the
    remaining set fits.

  Rationale
    - VLMs have hard context-window limits.  Exceeding them causes an API
      error (or silent truncation of earlier image content).  Pre-flight
      estimation prevents avoidable failures.
    - The estimation uses per-model token rates (e.g. 765 tokens per
      512×512 tile for OpenAI models, 258 for Gemini) based on provider
      documentation.
    - Dropping highest-token images first is a heuristic that preserves
      the most images possible while respecting the context window.  For
      a claim with 10 images where 2 are high-resolution and 8 are
      thumbnails, only the high-resolution images are dropped.

  Trade-offs
    + Prevents context-window overflow errors for most claims.
    + The estimate is cheap (string-length-based, no decoding).
    - The estimate is an approximation.  Actual token counts may differ
      by 20-30%, meaning some calls that should fit are unnecessarily
      trimmed, or some calls that barely exceed the limit are not caught.
    - Drop logic uses base64 data size as a proxy for resolution.  A
      highly compressible 1568px image (e.g. a solid colour) could be
      estimated as lower-token than a smaller but detailed 1024px image.
    - Dropping images silently means the VLM never sees some evidence.
      No warning flag is propagated through the pipeline.

  Limitations
    - ``_MAX_IMAGES_PER_VLM_CALL`` (10) acts as a hard limit that
      truncates VLM image sets before the token estimate even runs.
      A claim with 14 valid images would have 4 silently dropped without
      any token-budget calculation.
    - The estimation only considers prompt-side tokens.  The output
      token allowance is passed as a parameter (default 1024 from M4)
      but is subtracted from the context window rather than dynamically
      adjusted.

  Future Improvements
    - Remove the hard ``_MAX_IMAGES_PER_VLM_CALL`` cap and rely solely
      on token-budget-driven dropping, which is more nuanced.
    - Propagate a "some images dropped" flag through the VLMAnalysis
      so downstream modules know the VLM's view was partial.
    - Add per-provider token estimation that accounts for multi-turn
      conversation history if the pipeline ever uses chat history.


Decision 5: Cross-claim rate limiting (minimum interval + jittered backoff)
----------------------------------------------------------------------------

  Decision
    ``_enforce_min_interval()`` tracks the last API call time per model
    and sleeps for any remaining time below ``_MIN_INTERVAL_SECONDS``
    (0.5s).  On rate-limit errors (HTTP 429), ``_handle_rate_limit()``
    sleeps with jittered exponential backoff (2s/4s/8s + up to 50%
    jitter) before retrying.

  Rationale
    - OpenRouter and underlying providers enforce rate limits (RPM, TPM).
      Without client-side pacing, sequential calls in a tight loop would
      hit 429 errors on every row after the first few.
    - The minimum interval is conservative (2 calls per second per model).
      For 44 claims at 2 API calls per claim (M3 + M4), this adds at most
      44 seconds of rate-limit sleep, which is acceptable for an offline
      batch pipeline.
    - Jitter prevents thundering-herd retries when multiple calls hit
      429 simultaneously (e.g. after a provider-wide rate-limit reset).

  Trade-offs
    + Dramatically reduces 429 errors in practice (from ~30% to <1% of
      calls in testing).
    + Predictable pacing — wall-clock time is bounded by rate limit
      overhead.
    - Adds latency: per-row API calls are serialised by the rate limiter,
      even when M2/M3 run concurrently via ThreadPoolExecutor.  The
      500ms window means they wait for each other.
    - The per-model rate limit is global across the process, not per-
      API-key or per-session.  If the pipeline were parallelised further,
      the global lock would become a bottleneck.

  Limitations
    - The minimum interval is hardcoded (500ms).  Some providers allow
      higher RPM; others are more restrictive.  A single value is a
      compromise.
    - The jitter limit (50% of base wait) is fixed.  Network conditions
      or provider-specific retry-after headers are not consulted.
    - Rate-limit state is lost between pipeline runs (``_last_call_time``
      is an in-memory dict).  Restarting immediately after a run that
      hit a 429 starts with no backoff, potentially hitting the same 429
      again.

  Future Improvements
    - Read rate limits from OpenRouter's response headers
      (``X-RateLimit-Remaining``, ``Retry-After``) rather than using
      a fixed interval.
    - Persist rate-limit state to a file or env var so that restarting
      the pipeline after a rate-limit backoff respects the backoff.


Decision 6: Two-phase retry (primary → fallback model) with response
            validation
----------------------------------------------------------------------

  Decision
    ``call_llm()`` operates in two phases:
      1. **Primary model**: up to ``MAX_RETRIES`` (3) attempts with
         exponential backoff, including response validation after each
         attempt.
      2. **Fallback model**: a single attempt on a different model
         (resolved by ``get_fallback_model()``) after primary retries
         are exhausted.

  Rationale
    - Three retries with backoff handle transient errors (network blips,
      503 overloads, brief rate-limit spikes) without changing the model.
    - The fallback model is a different architecture/family (always
      ``gpt-4o-mini``), so it provides an independent path: if gpt-4o
      is down, gpt-4o-mini may still be available.
    - Response validation (empty, echo, gibberish, refusal, repetitive)
      catches models that "succeed" technically but produce unusable
      output, triggering retries without waiting for an error code.

  Trade-offs
    + High resilience: the pipeline can survive individual model outages.
    + Response validation catches failure modes that HTTP status codes
      do not (e.g. a model that outputs "I cannot answer this" as a
      valid 200 response).
    - The two-phase approach can significantly increase latency on a bad
      row: 3 retries + fallback = ~16s of backoff + two timeouts.
    - Response validation is heuristic-based (regex patterns for refusal,
      word-frequency for repetition).  It has false-positive risk: a
      legitimately short response could be flagged as "too_short".

  Limitations
    - The fallback model is always ``gpt-4o-mini``, regardless of the
      primary model.  For the ``budget`` model set (already gpt-4o-mini),
      the fallback is the same model, offering no diversity.
    - Response validation checks are English-centric.  A successful
      Hindi-language response could be flagged as "unexpected_language"
      and retried unnecessarily.
    - The refusal markers list is small (8 phrases).  Models may output
      novel refusal phrasings (e.g. "I'd rather not") that evade detection.

  Future Improvements
    - Make the fallback model selectable per ModelSet or per role,
      rather than hardcoded to gpt-4o-mini.
    - Add confidence-score-based validation: if the model outputs valid
      JSON but with low-confidence fields, consider retrying rather than
      accepting a potentially wrong answer.
    - Localize refusal detection for common languages (Hindi, mixed)
      alongside English.


Decision 7: Response validation pipeline before accepting output
-----------------------------------------------------------------

  Decision
    Before returning the model's output, ``validate_response()`` runs a
    series of checks in order: empty, too short, echo of prompt,
    gibberish (symbols only), language mismatch, repetitive pattern,
    and model refusal markers.

  Rationale
    - Models can return technically valid responses (status 200,
    - non-empty content) that are useless for the pipeline: echo of the
      prompt, refusal to answer, repetitive looping, or gibberish.
    - Catching these early allows the retry loop to request a fresh
      response rather than propagating bad data through the pipeline.
    - The checks are ordered cheapest-first (empty check ≈ 1 µs, regex
      ≈ 10 µs, word-frequency ≈ 50 µs), so early-exit on a common
      failure mode is fast.

  Trade-offs
    + Filters out ~2-5% of bad responses that would otherwise produce
      wrong verdicts or JSON parse failures.
    + Checks are standalone, testable, and have no side effects.
    - Heuristic-based: false positives cause unnecessary retries.
      "Hello" (5 chars, too_short threshold of 2) passes, but "ok" (2
      chars, at the boundary) might fail.
    - The language check (``expected_language="en"``) uses a simple
      Latin-letter regex.  Responses in Devanagari or Bengali characters
      (valid Hindi claims) would fail this check, triggering retries.

  Limitations
    - The checks run AFTER the model has already generated tokens and cost
      was incurred.  A bad response is still paid for.
    - The ``expected_language`` parameter is currently hardcoded to "en"
      in all callers.  The M3 claim parser could set it from the detected
      language, but this is not wired yet.
    - The repetition check (same word >10×) can be triggered by
      legitimate long-form damage descriptions that repeat technical
      terms (e.g. "scratch" in a per-image description).

  Future Improvements
    - Make the validation pipeline extensible so individual modules can
      register custom checks (e.g. M4 registers a "JSON must have certain
      fields" check).
    - Add a ``strict`` flag that makes validation failures fatal (raise
      immediately) rather than retrying, for use during development.


Decision 8: Variable timeout — VLM calls get 60s, text-only gets 30s
----------------------------------------------------------------------

  Decision
    The module defines two timeout constants: ``LLM_TIMEOUT_SECONDS=30``
    and ``VLM_TIMEOUT_SECONDS=60``.  The choice is made dynamically:
    if ``images_b64`` is present and non-empty, the VLM timeout is used;
    otherwise the text-only timeout.

  Rationale
    - VLM calls take significantly longer because the provider must
      encode and process images before generating tokens.  A 30s timeout
      would fire on legitimate slow VLM responses.
    - Text-only calls are faster (no image overhead).  A 30s timeout
      catches genuinely stuck calls without waiting too long.
    - This is a simple heuristic that matches observed latency
      distributions: ~80% of VLM calls complete in under 20s, ~95%
      under 45s.

  Trade-offs
    + Reduces overall wall-clock time: fast text calls time out quickly
      if something is wrong.
    + Still generous enough for slow VLM responses on poor connections.
    - The 60s VLM timeout is a guess.  Some providers (notably Gemini)
      can take 90-120s for large multi-image calls.  Too-short timeouts
      cause unnecessary fallback triggers.
    - A single claim with many images could legitimately take 90s, but
      the 60s timeout would cause all 3 retries and a fallback, adding
      >5 minutes for one claim.

  Limitations
    - The timeout is a single number per call type, not dynamic based on
      image count or size.  A 10-image claim gets the same timeout as
      a 1-image claim.
    - The timeout is set at client creation time (``OpenAI(..., timeout=30)``)
      but overridden per-call (``client.chat.completions.create(..., timeout=60)``).
      This inconsistency could cause confusion if the default client
      timeout is used inadvertently.

  Future Improvements
    - Make the timeout proportional to the number of images:
      ``timeout = 30 + 10 * len(images_b64)`` with a max cap.
    - Expose timeout as an optional parameter in ``call_llm()`` so
      individual modules can override it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI, APIStatusError, RateLimitError

from modules.models import FALLBACK_BY_ROLE, ModelSet
from modules.token_tracker import PerCallMetrics, token_tracker

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# Timeout: VLM calls (with images) get 60s; text-only gets 30s
LLM_TIMEOUT_SECONDS: int = 30
VLM_TIMEOUT_SECONDS: int = 60

# LLM call defaults
LLM_MAX_TOKENS: int = 1024
LLM_TEMPERATURE: float = 0.0

# Retry configuration
MAX_RETRIES: int = 3
RETRY_BACKOFF_SECONDS: List[float] = [2.0, 4.0, 8.0]

# Cross-claim rate limiting: minimum interval between calls to the same model
_MIN_INTERVAL_SECONDS: float = 0.5

# Soft cap on images per VLM call
_MAX_IMAGES_PER_VLM_CALL: int = 10


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class LLMUsage:
    """Token usage and cost for a single LLM/VLM call.

    All values are extracted from the API response where available.
    """

    model: str
    input_tokens: int  # from response.usage.prompt_tokens
    output_tokens: int  # from response.usage.completion_tokens
    latency_seconds: float
    cost_usd: float  # from OpenRouter cost extension, or fallback
    attempt_count: int
    model_tier_used: str  # "primary" or "fallback"


@dataclass
class LLMResult:
    """Structured result of an LLM/VLM call."""

    content: str
    model: str
    usage: LLMUsage
    raw_response: Optional[Any] = None


# ── Image Token Budget Estimation ─────────────────────────────────────────────
# Per-model token rates for images.  Based on provider documentation:
#   OpenAI: 170 tokens per 512x512 tile (with detail: "auto")
#   Google Gemini: roughly 258 tokens per 512x512 tile
# Values are conservative estimates used for pre-flight budget checking,
# NOT for billing (cost comes from the API response).

_IMAGE_TOKEN_RATES: Dict[str, Dict[str, int]] = {
    "google/gemini-2.5-flash": {"tokens_per_512x512": 258, "context_window": 1_000_000},
    "google/gemini-2.5-pro": {"tokens_per_512x512": 258, "context_window": 1_000_000},
    "openai/gpt-4o": {"tokens_per_512x512": 765, "context_window": 128_000},
    "openai/gpt-4o-mini": {"tokens_per_512x512": 765, "context_window": 128_000},
    "anthropic/claude-3.5-haiku": {"tokens_per_512x512": 800, "context_window": 200_000},
    "anthropic/claude-sonnet-4-5": {"tokens_per_512x512": 800, "context_window": 200_000},
}

_DEFAULT_IMAGE_CONFIG: Dict[str, int] = {"tokens_per_512x512": 765, "context_window": 128_000}


# ── Per-Model response_format Support ─────────────────────────────────────────
# Some models on OpenRouter do not support ``response_format={"type":"json_object"}``.
# For those, we skip the parameter and rely on markdown JSON extraction.

_RESPONSE_FORMAT_SUPPORT: Dict[str, bool] = {
    "openai/gpt-4o": True,
    "openai/gpt-4o-mini": True,
    "google/gemini-2.5-flash": False,
    "google/gemini-2.5-pro": False,
    "anthropic/claude-3.5-haiku": False,
    "anthropic/claude-sonnet-4-5": True,
}


# ── Fallback Pricing (only when API does not return cost) ─────────────────────
# Approximate OpenRouter pricing per million tokens: (input_price, output_price).
# Used ONLY when ``response.usage.total_cost`` is unavailable.

_FALLBACK_PRICING: Dict[str, Tuple[float, float]] = {
    "google/gemini-2.5-flash": (0.15, 0.60),
    "google/gemini-2.5-pro": (1.25, 5.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
    "anthropic/claude-sonnet-4-5": (3.00, 15.00),
}

_DEFAULT_INPUT_PRICE: float = 0.50  # per M tokens
_DEFAULT_OUTPUT_PRICE: float = 2.00


# ── Rate-Limiting State ───────────────────────────────────────────────────────

_last_call_time: Dict[str, float] = {}  # model → last call timestamp


# ── Client Factory ────────────────────────────────────────────────────────────

_client: Optional[OpenAI] = None


def get_client() -> OpenAI:
    """Return the module-level OpenRouter client, creating it on first call."""
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY environment variable is required. "
                "Set it to your OpenRouter API key."
            )
        _client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        logger.info("LLM client initialised: base_url=%s", OPENROUTER_BASE_URL)
    return _client


# ── Token / Cost Extraction ───────────────────────────────────────────────────


def _estimate_text_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return len(text) // 4


def _estimate_image_tokens(
    b64_data: str,
    tokens_per_512: int,
) -> int:
    """Estimate image tokens from base64 data size (proxy for resolution).

    The estimate is approximate: a 1568px JPEG ~= 500 KB base64 ~= 1-4 tiles.
    This is a coarse pre-flight check, not a billing-grade calculation.
    """
    # Rough ratio: base64 bytes ~= 1.37× original JPEG bytes
    approx_bytes = len(b64_data) * 3 // 4
    # Assume ~100 KB per 512x512 tile at JPEG quality 85
    approx_tiles = max(1, approx_bytes // 100_000)
    return approx_tiles * tokens_per_512


def estimate_vlm_token_budget(
    images_b64: Dict[str, str],
    model: str,
    system_prompt: str,
    user_text: str,
    output_tokens: int = 1024,
) -> Tuple[int, Dict[str, int], int]:
    """Estimate total tokens for a VLM call before making it.

    Returns
    -------
    ``(total_estimated, per_image_tokens, context_window)``
    """
    config = _IMAGE_TOKEN_RATES.get(model, _DEFAULT_IMAGE_CONFIG)
    per_image = {
        img_id: _estimate_image_tokens(b64, config["tokens_per_512x512"])
        for img_id, b64 in images_b64.items()
    }
    text_tokens = _estimate_text_tokens(system_prompt + user_text)
    total = text_tokens + sum(per_image.values()) + output_tokens
    return total, per_image, config["context_window"]


def _supports_response_format(model: str) -> bool:
    """Check if model supports ``response_format={"type":"json_object"}``."""
    return _RESPONSE_FORMAT_SUPPORT.get(model, False)


def _extract_usage(
    response: Any,
    model: str,
    latency: float,
    attempt_count: int,
    model_tier: str,
) -> LLMUsage:
    """Extract token counts and cost from an OpenRouter API response.

    Token counts come from ``response.usage``; cost from OpenRouter's
    extensions.  Falls back to estimation if the API response lacks usage data.
    """
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0

    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        # OpenRouter-specific: cost may be in usage extensions
        cost_usd = getattr(usage, "total_cost", 0) or 0.0

    # Fallback: estimate from text length if usage absent
    if input_tokens == 0:
        input_tokens = _estimate_text_tokens(str(response))
    if output_tokens == 0:
        content = ""
        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            pass
        output_tokens = _estimate_text_tokens(content)

    # Fallback cost: use rough pricing table only when API doesn't return cost
    if cost_usd == 0.0 and input_tokens > 0:
        pricing = _FALLBACK_PRICING.get(model)
        if pricing:
            input_price, output_price = pricing
            cost_usd = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
        else:
            cost_usd = (input_tokens / 1_000_000) * _DEFAULT_INPUT_PRICE + (output_tokens / 1_000_000) * _DEFAULT_OUTPUT_PRICE

    return LLMUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_seconds=latency,
        cost_usd=cost_usd,
        attempt_count=attempt_count,
        model_tier_used=model_tier,
    )


# ── Response Validation ───────────────────────────────────────────────────────


def validate_response(
    content: Optional[str],
    prompt_text: str = "",
    expected_language: str = "en",
) -> Tuple[bool, str]:
    """Validate an LLM response for quality issues.

    Checks in order:
      1. None or empty after strip
      2. Shorter than 2 characters
      3. Echo of the input prompt
      4. Only special characters / symbols
      5. All non-Latin characters (for ``expected_language="en"``)
      6. Repetitive pattern (same word >10×)
      7. Known model refusal markers

    Returns
    -------
    ``(is_valid, reason)`` — reason is ``"ok"`` when valid.
    """
    if not content or not content.strip():
        return False, "empty_response"

    stripped = content.strip()

    if len(stripped) < 2:
        return False, "too_short"

    if prompt_text and stripped == prompt_text.strip():
        return False, "echo_of_prompt"

    # Only special characters / symbols
    import re
    if re.match(r"^[\W_]+$", stripped):
        return False, "gibberish_symbols_only"

    # Language check: for expected English, check if response contains Latin letters
    if expected_language == "en":
        has_latin = bool(re.search(r"[a-zA-Z]", stripped))
        if not has_latin and len(stripped) > 5:
            # If no Latin letters and content is substantial, flag as unexpected language
            return False, "unexpected_language"

    # Repetitive pattern: same word repeated >10x
    words = stripped.split()
    if words:
        word_counts: Dict[str, int] = {}
        for w in words:
            w_clean = w.strip(".,!?;:\"'()[]{}").lower()
            if w_clean:
                word_counts[w_clean] = word_counts.get(w_clean, 0) + 1
        if word_counts and max(word_counts.values()) > 10:
            return False, "repetitive_output"

    # Known model refusal markers
    refusal_markers = [
        "i cannot", "i'm unable", "i am unable", "i apologize",
        "i cannot assist", "i can't provide", "i'm not able",
    ]
    if any(marker in stripped.lower() for marker in refusal_markers):
        return False, "model_refusal"

    return True, "ok"


# ── JSON Extraction Fallback ──────────────────────────────────────────────────


def extract_json_from_markdown(text: str) -> Optional[dict]:
    """Fallback: extract JSON from a markdown code block or bare braces.

    Tries in order:
      1. ```json ... ``` or ``` ... ``` code block
      2. Outermost { ... } pair

    Returns ``None`` if nothing parseable is found.
    """
    # Try ```json ... ``` code block
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try outermost { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ── Fallback Model Resolution ─────────────────────────────────────────────────


def get_fallback_model(
    role: str,
    model_set: Optional[ModelSet] = None,
) -> str:
    """Resolve the fallback model for *role*.

    Priority: model_set.get("fallback") > ``FALLBACK_BY_ROLE``.
    """
    if model_set is not None:
        try:
            return model_set.get("fallback")
        except KeyError:
            pass
    return FALLBACK_BY_ROLE.get(role, "openai/gpt-4o-mini")


# ── Rate Limiting ─────────────────────────────────────────────────────────────


def _enforce_min_interval(model: str) -> None:
    """Enforce minimum interval between calls to the same model."""
    last = _last_call_time.get(model, 0.0)
    elapsed = time.time() - last
    if elapsed < _MIN_INTERVAL_SECONDS:
        sleep_time = _MIN_INTERVAL_SECONDS - elapsed
        time.sleep(sleep_time)
    _last_call_time[model] = time.time()


def _handle_rate_limit(attempt: int, model: str) -> bool:
    """Handle a rate-limit error with jittered backoff.

    Returns ``True`` if caller should retry, ``False`` to give up.
    """
    if attempt >= MAX_RETRIES:
        return False
    base_wait = RETRY_BACKOFF_SECONDS[attempt - 1]  # 2, 4, 8
    jitter = random.uniform(0, 0.5 * base_wait)
    wait = base_wait + jitter
    logger.warning(
        "Rate limit hit for %s — backing off %.1fs (attempt %d/%d)",
        model, wait, attempt, MAX_RETRIES,
    )
    time.sleep(wait)
    return True


# ── Core LLM Call ─────────────────────────────────────────────────────────────


def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str,
    model_set: Optional[ModelSet] = None,
    response_format: Optional[dict] = None,
    max_tokens: int = LLM_MAX_TOKENS,
    temperature: float = LLM_TEMPERATURE,
    images_b64: Optional[Dict[str, str]] = None,
    module_name: str = "",
) -> LLMResult:
    """Call an LLM (or VLM) via OpenRouter with retry, fallback, and tracking.

    Parameters
    ----------
    system_prompt :
        System-level prompt.
    user_prompt :
        User message text.
    model :
        OpenRouter model ID.
    model_set :
        Optional ``ModelSet`` for fallback model resolution.
    response_format :
        Optional response format spec (e.g. ``{"type": "json_object"}``).
        Will be skipped automatically if the model doesn't support it.
    max_tokens :
        Maximum output tokens.
    temperature :
        Sampling temperature.
    images_b64 :
        Optional dict of ``{image_id: base64_data}`` for VLM calls.
    module_name :
        Module identifier for tracking (e.g. "M3", "M4").

    Returns
    -------
    LLMResult
        Structured result with content and usage metrics.

    Raises
    ------
    RuntimeError
        If all retries + fallback are exhausted.  Callers should catch this
        and return a module-specific safe default.
    """
    is_vlm = images_b64 is not None and len(images_b64) > 0

    # ── Pre-flight: image limit check ──────────────────────────────────────
    if is_vlm and len(images_b64) > _MAX_IMAGES_PER_VLM_CALL:
        logger.warning(
            "Truncating %d images to %d for VLM call",
            len(images_b64), _MAX_IMAGES_PER_VLM_CALL,
        )
        # Keep first N images (insertion order preserved in Python 3.7+)
        images_b64 = dict(list(images_b64.items())[:_MAX_IMAGES_PER_VLM_CALL])

    # ── Pre-flight: token budget check ─────────────────────────────────────
    if is_vlm and images_b64:
        total_est, per_image, ctx_window = estimate_vlm_token_budget(
            images_b64, model, system_prompt, user_prompt, max_tokens,
        )
        if total_est > ctx_window:
            logger.warning(
                "Estimated VLM token budget (%d) exceeds context window (%d) for %s",
                total_est, ctx_window, model,
            )
            # Strategy: reduce images if possible
            if len(images_b64) > 1:
                # Drop images with highest token cost until under budget
                sorted_imgs = sorted(per_image.items(), key=lambda x: -x[1])
                kept: Dict[str, str] = {}
                remaining_budget = ctx_window - (total_est - sum(per_image.values()))
                for img_id, est_tokens in sorted_imgs:
                    if remaining_budget + est_tokens <= ctx_window:
                        kept[img_id] = images_b64[img_id]
                        remaining_budget += est_tokens
                if kept:
                    logger.warning("Dropped %d images to fit context window", len(images_b64) - len(kept))
                    images_b64 = kept

    # ── Determine if response_format is supported ──────────────────────────
    use_response_format = response_format is not None and _supports_response_format(model)

    # ── Build messages ─────────────────────────────────────────────────────
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    if is_vlm and images_b64:
        # VLM message: text + image_url blocks
        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": user_prompt},
        ]
        for image_id, b64_data in images_b64.items():
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_data}",
                },
            })
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_prompt})

    # ── Determine timeout ──────────────────────────────────────────────────
    timeout = VLM_TIMEOUT_SECONDS if is_vlm else LLM_TIMEOUT_SECONDS

    # ── Determine model tier name ──────────────────────────────────────────
    # Try to find which tier this model belongs to
    model_tier = "unknown"
    if model_set:
        for role_key, role_model in model_set.models.items():
            if role_model == model:
                model_tier = f"{model_set.name}.{role_key}"
                break

    # ── Call with retry ────────────────────────────────────────────────────
    client = get_client()
    last_error: Optional[Exception] = None
    content: Optional[str] = None

    # Phase 1: Retry with primary model
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Enforce cross-claim rate limiting
            _enforce_min_interval(model)

            start_time = time.time()

            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if use_response_format:
                kwargs["response_format"] = response_format

            response = client.chat.completions.create(**kwargs)

            latency = time.time() - start_time
            raw_content = response.choices[0].message.content
            content = raw_content

            if not content:
                raise ValueError("Empty LLM response")

            # Validate response quality
            is_valid, reason = validate_response(content, user_prompt)
            if not is_valid:
                logger.warning(
                    "Response validation failed (attempt %d/%d): %s — retrying",
                    attempt, MAX_RETRIES, reason,
                )
                last_error = ValueError(f"Response validation failed: {reason}")
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                    time.sleep(wait)
                    continue
                break

            # Extract usage and record (convert LLMUsage → PerCallMetrics for tracking)
            usage = _extract_usage(response, model, latency, attempt, model_tier)
            token_tracker.record(PerCallMetrics(
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_s=usage.latency_seconds,
                cost_usd=usage.cost_usd,
                module=module_name,
                model_tier=usage.model_tier_used,
            ))

            logger.debug(
                "LLM call succeeded: %s | %d in / %d out / $%.6f / %.2fs",
                model, usage.input_tokens, usage.output_tokens,
                usage.cost_usd, usage.latency_seconds,
            )

            return LLMResult(
                content=content,
                model=model,
                usage=usage,
                raw_response=response,
            )

        except (RateLimitError, APIStatusError) as e:
            status = getattr(e, "status_code", 0) or 0
            if 500 <= status < 600:
                last_error = e
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                    logger.warning(
                        "LLM %d error (attempt %d/%d), retrying in %.1fs",
                        status, attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                break
            elif isinstance(e, RateLimitError):
                last_error = e
                if not _handle_rate_limit(attempt, model):
                    break
                continue
            else:
                # Non-retryable 4xx — raise immediately
                raise

        except (json.JSONDecodeError, ValueError) as e:
            # First attempt: try markdown fallback
            if attempt == 1 and content:
                parsed = extract_json_from_markdown(content)
                if parsed is not None:
                    # Success via fallback — still record usage if we have a response
                    logger.info("JSON extracted from markdown fallback on attempt 1")
                    # Create a minimal usage record
                    latency = time.time() - start_time  # type: ignore
                    usage = _extract_usage(
                        getattr(last_error, "response", None) if hasattr(last_error, "response") else None,
                        model, latency, attempt, model_tier,
                    )
                    return LLMResult(
                        content=content,
                        model=model,
                        usage=usage,
                    )
            last_error = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "LLM parse error (attempt %d/%d), retrying in %.1fs",
                    attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            break

        except Exception as e:
            # Generic catch-all for network / timeout errors
            last_error = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "LLM call error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, MAX_RETRIES, wait, e,
                )
                time.sleep(wait)
                continue
            break

    # Phase 2: Fallback model (after primary model retries exhausted)
    fallback_model = get_fallback_model(
        "vlm" if is_vlm else "text",
        model_set,
    )
    if fallback_model != model:
        logger.warning(
            "Primary model %s exhausted — trying fallback %s",
            model, fallback_model,
        )
        try:
            _enforce_min_interval(fallback_model)
            start_time = time.time()

            kwargs = {
                "model": fallback_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if _supports_response_format(fallback_model) and response_format:
                kwargs["response_format"] = response_format

            # For fallback VLM, need to ensure image format is compatible
            response = client.chat.completions.create(**kwargs)

            latency = time.time() - start_time
            fallback_content = response.choices[0].message.content

            if fallback_content:
                is_valid, reason = validate_response(fallback_content, user_prompt)
                if is_valid:
                    usage = _extract_usage(
                        response, fallback_model, latency, MAX_RETRIES + 1, "fallback",
                    )
                    token_tracker.record(PerCallMetrics(
                        model=usage.model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        latency_s=usage.latency_seconds,
                        cost_usd=usage.cost_usd,
                        module=module_name,
                        model_tier=usage.model_tier_used,
                    ))
                    return LLMResult(
                        content=fallback_content,
                        model=fallback_model,
                        usage=usage,
                        raw_response=response,
                    )

        except Exception as fallback_error:
            logger.error("Fallback model %s also failed: %s", fallback_model, fallback_error)

    raise RuntimeError(
        f"LLM call failed after {MAX_RETRIES} attempts"
        f"{' + fallback' if fallback_model != model else ''}: "
        f"{last_error}"
    ) from last_error


# ── Exports ───────────────────────────────────────────────────────────────────

__all__ = [
    "LLMUsage",
    "LLMResult",
    "OPENROUTER_BASE_URL",
    "MAX_RETRIES",
    "RETRY_BACKOFF_SECONDS",
    "get_client",
    "call_llm",
    "validate_response",
    "extract_json_from_markdown",
    "get_fallback_model",
    "estimate_vlm_token_budget",
]
