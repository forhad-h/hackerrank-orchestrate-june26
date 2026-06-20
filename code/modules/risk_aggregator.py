"""
M6 — Risk Flag Aggregator  (SPEC.md §5 M6, §9, §10)

Input:  VLMAnalysis.image_risk_flags, ParsedClaim, ClaimContext.user_history
Output: str — final semicolon-joined flags or "none"

Flag sources (SPEC §9):
  VLM image_risk_flags:
    blurry_image, cropped_or_obstructed, low_light_or_glare, wrong_angle,
    wrong_object, wrong_object_part, damage_not_visible, claim_mismatch,
    possible_manipulation, non_original_image, text_instruction_present

  User history (§10):
    user_history_risk        → rejected_claim >= 2 OR last_90_days_claim_count >= 3
    manual_review_required   → history_flags != "none" OR manual_review_claim >= 2
                               OR any manipulation flag present

Rules:
  - Deduplicate flags (ordered set).
  - Sort alphabetically for determinism.
  - If any manipulation flag present → always also add manual_review_required.
  - Return "none" if final set is empty.
  - History flags add risk context but must NOT override clear visual evidence.

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================

This section documents the significant design decisions made in this module,
the rationale behind them, trade-offs, known limitations, and conditions under
which the implementation may need to be revisited.


Decision 1: Pure function with no I/O or side effects
------------------------------------------------------

  Decision
    The module is a single pure function ``aggregate()`` that takes typed
    dataclass inputs and returns a string. It performs no I/O, no API calls,
    and has no mutable module-level state.

  Rationale
    - Makes the function trivially testable — every test is a simple
      "prepare inputs → assert output" without mocking, fixtures, or
      async orchestration.
    - Eliminates an entire class of bugs (file-not-found, permission,
      network, concurrency) in a module whose logic is pure combinatorics.
    - Separates risk-flat composition (this module) from flag generation
      (M2 structural, M5 VLM, user history), keeping each stage its own
      responsibility.
    - Enables the caller (M7 Output Assembler) to call aggregate()
      synchronously without worrying about error handling — the function
      cannot fail in a way that needs recovery (no network, no disk,
      no external services).

  Trade-offs
    + Maximally testable and deterministic.
    + Zero operational risk (no timeouts, retries, rate limits).
    - Cannot emit warnings or diagnostic information through the return
      value — all logging must go through the ``logging`` module, which
      must be explicitly consumed by the caller.
    - Cannot dynamically fetch additional user context at aggregation
      time (e.g., real-time fraud scores). All context must be pre-loaded
      and passed in.

  Limitations
    - If the system were extended with real-time risk scoring (e.g., a
      live API call to a fraud-detection service), this function would need
      to either become async or delegate that call upstream.
    - Pure functions composed inside a batch pipeline have no visibility
      into global state (e.g., "this user has 5 pending claims across
      different batches") — the function sees only the single row it is
      called with.

  Future Improvements
    - None needed for the current scope. If real-time or cross-row
      aggregation is needed, the function's signature should not change;
      the caller should inject pre-computed cross-row context as an
      additional field on UserHistory or a new dataclass.


Decision 2: Ordered-set deduplication with alphabetical sort
-------------------------------------------------------------

  Decision
    Flags are deduplicated using an ordered set (``seen: set`` with a
    linear scan preserving first-encounter order), then the result is
    sorted alphabetically before joining.

  Rationale
    - Deduplication is necessary because VLM output may list the same
      flag multiple times (e.g., when multiple images produce the same
      risk flag), and history rules may add a flag the VLM already set.
    - Alphabetical sorting ensures deterministic output independent of
      processing order (VLM-first vs history-first), import order of
      source data, or parallelism in the upstream pipeline.
    - Sorting also makes the output easy to compare across runs and
      trivial to parse for downstream consumers.

  Trade-offs
    + Fully deterministic; two runs with identical inputs always produce
      identical output, regardless of internal ordering changes.
    + Cheap — O(n log n) with n ≤ 14 (the total number of possible flags),
      which is negligible even at 44-row batch scale.
    - Sorting destroys the semantic priority ordering of flags. A critical
      flag like ``possible_manipulation`` appears interleaved with a minor
      one like ``blurry_image``. Downstream consumers that care about
      priority must re-parse and rank the output themselves.
    - The ordered-set pattern (``seen: set`` + ``deduped: list``) is more
      verbose than ``dict.fromkeys()`` (Python 3.7+), though equivalent.

  Limitations
    - If the flag vocabulary grows beyond ~30 entries, alphabetical sort
      becomes misleading — "z_priority_flag" sorts before "a_minor_flag"
      but carries far more weight. A future version may need separate
      severity buckets (e.g., "critical", "warning", "info").

  Future Improvements
    - Consider returning a structured object (e.g., ``List[RiskFlag]``
      with severity and category) instead of a semicolon-joined string
      if downstream consumers need priority-aware ordering.
    - The deduplication logic is trivially replaceable with
      ``dict.fromkeys(flags).keys()`` if Python version guarantees are
      formalized.


Decision 3: Manipulation flags unconditionally trigger manual_review_required
------------------------------------------------------------------------------

  Decision
    If any flag in MANIPULATION_FLAGS (possible_manipulation,
    non_original_image, text_instruction_present) is present, the
    manual_review_required flag is always appended, even if no other
    condition (history or VLM) triggered it.

  Rationale
    - Per SPEC §9 rule 1: manipulation indicators are a strong signal
      that an automated decision cannot be trusted and a human adjudicator
      must review the evidence.
    - This is a fail-safe design: even if every other module succeeds
      without flags, a manipulation flag alone is sufficient to escalate.
    - Captures the case where a clean user with no history submits a
      manipulated image — the manipulation flag comes from M5 VLM, not
      from user history, so without this rule the claim could proceed
      without manual review.

  Trade-offs
    + Strong security guarantee: no automated path through the pipeline
      can ignore manipulation evidence.
    + Simple, auditable, one-line check (lines 77–79).
    - Creates an implicit coupling between M5 (VLM Engine) and this
      module: if a new manipulation flag is added to the VLM prompt,
      MANIPULATION_FLAGS in models.py must also be updated, or the rule
      silently does not apply to the new flag.
    - Increases manual-review workload for borderline cases where the
      VLM might be over-sensitive (e.g., flagging a legitimate screenshot
      as "non_original_image").

  Limitations
    - The rule is binary (manual review yes/no). There is no tiering —
      a subtle crop is treated the same as an obvious deepfake.
    - If ``manual_review_required`` is already present from user history
      (lines 72–74), the deduplication step (lines 82–88) removes the
      duplicate. The function is idempotent with respect to this rule.
    - There is no mechanism for a downstream module to override this
      decision (e.g., an automated second-opinion VLM call that clears
      the manipulation flag). The caller (M7) could implement this, but
      the aggregator itself has no overrides.

  Future Improvements
    - Replace the binary rule with a confidence threshold. If VLM returns
      a confidence score for manipulation, only escalate when confidence
      > threshold.
    - Add a second-opinion hook: if a manipulation flag is set, pass the
      image to a secondary VLM model (different provider) for confirmation
      before adding manual_review_required.
    - Expose MANIPULATION_FLAGS as an importable constant so the VLM
      prompt authoring system can warn when flags fall out of sync.


Decision 4: User history flags do not override visual evidence
---------------------------------------------------------------

  Decision
    History-derived flags (user_history_risk, manual_review_required) are
    *appended* to the flag list, but they never modify VLM-derived flags
    (image_risk_flags), and they never affect claim_status or other fields
    outside this module's scope.

  Rationale
    - Per SPEC §10: "History flags add risk context but must NOT override
      clear visual evidence." A visually well-supported claim from a
      high-risk user still gets ``claim_status=supported`` — the history
      flags surface the concern separately.
    - Enforcing this separation in the aggregator (rather than leaving it
      to the caller) ensures every caller automatically obeys the rule.
    - Keeps the risk-aggregation layer focused on flag composition, not
      claim-verdict modification.

  Trade-offs
    + Clear separation of concerns: history flags are informational, not
      verdict-changing.
    + Safe default: a clean visual analysis from a risky user produces
      flags but does not flip the verdict.
    - The flag set can be confusing to read: ``user_history_risk`` appears
      alongside ``blurry_image`` in the same field, but they have different
      semantics (one is an action recommendation, the other is a quality
      observation). Consumers must understand the distinction.
    - If a future requirement demands that certain history thresholds
      override visual evidence (e.g., "rejected_claim >= 5 → always set
      claim_status=contradicted"), this module cannot enforce that —
      the change would need to happen in M5 (VLM) or M7 (assembler).

  Limitations
    - The aggregator cannot distinguish "no manipulation" from "VLM
      did not check for manipulation" (e.g., because the model failed
      or valid_image=False). In both cases, image_risk_flags is empty,
      and no manipulation rule fires. The safe-default path in the
      pipeline (M7) handles the failure case separately.
    - History flags are boolean thresholds, not Bayesian priors. The
      rule ``rejected_claim >= 2`` is a flat cutoff with no gradation;
      a user with 20 rejections produces the same flag as a user with 2.

  Future Improvements
    - Consider adding a confidence or recency weight to history flags
      (e.g., "3 rejections in the last month" is more concerning than
      "3 rejections over 5 years").
    - If multi-source risk scores (history + VLM + behavioral) become
      necessary, this module could be promoted to a weighted aggregator
      that produces a combined risk score alongside the flag list, but
      the current spec explicitly prohibits score-based overriding.


Decision 5: Single semicolon-delimited string return type
----------------------------------------------------------

  Decision
    The function returns a single ``str`` — flags joined by semicolons,
    or the literal string ``"none"`` when the set is empty.

  Rationale
    - Matches the output schema in SPEC §6 exactly, so M7 (Output
      Assembler) can write the return value directly into the ``risk_flags``
      CSV column without post-processing.
    - The "none" sentinel is consistent with other fields in the output
      schema (supporting_image_ids, history_flags) and makes empty-state
      CSV parsing unambiguous (empty string vs "none" vs missing field).
    - A string return is trivially serializable (no custom JSON encoder),
      hashable (for caching), and comparable across runs.

  Trade-offs
    + Zero-copy into the output CSV column.
    + Universally parseable — every CSV consumer can split on ";".
    - Loses type safety: a consumer that splits and expects valid enum
      values must re-validate against RISK_FLAG_VALUES.
    - The "none" sentinel is a string, not ``None`` or an empty sentinel,
      so callers must compare with ``== "none"`` rather than ``is None``
      or ``not result`` — a potential source of bugs if consumers forget.

  Limitations
    - If the flag vocabulary grows large (e.g., 50+ flags), the
      semicolon-joined string becomes unwieldy to parse and display in
      structured views (tables, dashboards).
    - No metadata about which module generated each flag (VLM vs history)
      survives in the output — the flag value alone encodes the source,
      which is a fragile convention.

  Future Improvements
    - If the output schema is ever revised to support structured risk
      data (e.g., JSON or separate columns per flag), this function should
      return a dataclass or dict, and M7 should handle the serialization.
      Until then, the string return is the simplest correct design.


Decision 6: Linear pipeline with no short-circuit
--------------------------------------------------

  Decision
    The function always executes all flag-collection steps (VLM flags,
    history flags, manipulation override, dedup/sort) regardless of which
    flags have already been set. There is no early exit even if the
    final result is already known (e.g., "manipulation detected → always
    include manual_review_required, but still run history checks").

  Rationale
    - The computational cost of the remaining steps is negligible (a few
      list appends and set operations per row). Early-exit optimization
      would add complexity for zero measurable gain.
    - Running all steps ensures that if a future condition is added that
      overrides an earlier one, the override logic sees the complete
      intermediate state.
    - Consistency: the function always produces the same output for the
      same inputs regardless of ordering — alphabetical sort guarantees
      this.

  Trade-offs
    + Simple, predictable control flow — every line is executed on every
      call, making debugging and auditing easy.
    + No branches that could introduce subtle skip bugs (e.g., "if X
      skip Y but should not have").
    - The user_history_risk and manual_review_required checks are
      evaluated even when the input VLM flags already imply them, wasting
      a handful of CPU cycles per call (immeasurable for 44 rows).
    - If a flag source were expensive (e.g., a lazy-loaded database call),
      linear evaluation would be the wrong pattern. But all sources here
      are pre-loaded dataclass fields.

  Limitations
    - The linear pipeline offers no isolation between flag sources: a
      bug in the history-flags step (e.g., a wrong threshold) could
      contaminate the flag list in a way that is harder to trace than
      with fully independent sub-computations.
    - Each flag source is committed to the shared list as it runs;
      there is no "rollback" — if a source produces an incorrect flag,
      subsequent steps cannot remove it (only deduplication can suppress
      duplicates).

  Future Improvements
    - If flag sources grow in number or complexity, consider a
      producer/consumer pattern where each flag source yields flags
      independently and a collector step merges/filters them.
    - The investment is not justified at the current scale (≤14 flags,
      ≤44 rows).


Decision 7: manual_review_required from multiple independent sources
----------------------------------------------------------------------

  Decision
    The ``manual_review_required`` flag can be set by any of three
    independent conditions:
      1. User history has non-none history_flags (line 72).
      2. User history manual_review_claim >= 2 (line 73).
      3. Any MANIPULATION_FLAGS is present (line 78).
    These are evaluated independently; a duplicate is removed by the
    deduplication step.

  Rationale
    - Each condition represents a distinct risk signal: past flagged
      history, past manual-review claims, and current-claim manipulation
      evidence. Any one of these independently warrants human review.
    - Independent evaluation is simpler than a combined decision tree
      and avoids false negatives from incorrectly ordered conditions
      (e.g., "if condition 1 true, skip condition 2").
    - Duplicate suppression (step 4) naturally handles overlap: if
      condition 1 and condition 3 both fire, the second occurrence is
      discarded, and the final output correctly contains the flag once.

  Trade-offs
    + Each condition can be tested independently (see
      test_risk_aggregator.py: TestUserHistoryFlags,
      TestManipulationFlags).
    + Adding a new condition is a one-line change — no modification to
      the dedup or sort logic needed.
    - The three conditions share no common abstraction, so if the trigger
      logic becomes more complex (e.g., weighted scores), the structure
      may need refactoring.
    - A consumer reading the output cannot tell *why*
      manual_review_required was set — the flag value alone does not
      distinguish between "past flagged history" and "current
      manipulation evidence."

  Limitations
    - If the same user has multiple claims in a batch, the
      manual_review_required flag is evaluated per-claim and may appear
      on some claims but not others, which could confuse downstream
      processing that expects the flag to be stable per user in a batch.

  Future Improvements
    - Consider a structured risk breakdown if the evaluation pipeline
      (M8) needs to attribute manual_review_required to specific causes.
      This would require a richer return type than a flat string.


=============================================================================
EDGE CASES AND KNOWN GAPS
=============================================================================

The following edge cases are intentionally not handled or produce behavior
that a downstream consumer should be aware of:

  1. Empty risk_flags from VLM with history on the boundary
     - If rejected_claim == 1 and last_90_days_claim_count == 2 (one below
       each threshold), no history flags fire. The user is treated identically
       to a first-time claimant even though their activity is non-trivial.
     - This is by design (SPEC §10 thresholds) but worth flagging if the
       evaluation reveals a cluster of near-miss boundary cases.

  2. manual_review_required from history_flags alone
     - If history_flags is "previous_discrepancy" but manual_review_claim is
       0 and no manipulation flags are present, manual_review_required still
       fires. There is no "expiry" on history_flags — a discrepancy from
       2 years ago still triggers manual review today.

  3. VLM returns an unrecognized flag
     - The function does not validate each entry in image_risk_flags against
       RISK_FLAG_VALUES. If M5 returns a flag not in the known set (e.g.,
       after a prompt change), it passes through to the output. The caller
       (M7 Output Assembler) is responsible for schema-level validation
       against RISK_FLAG_VALUES.

  4. Claim with no images (valid_image=False)
     - When VLM analysis is skipped entirely (no valid images), the
       VLMAnalysis object has image_risk_flags=[] and valid_image=False.
       The aggregator produces no flags from the VLM source in this case.
       The caller (M7) handles the safe-default path separately; this module
       does not duplicate that logic.

  5. Batch consistency
     - Each call to aggregate() is independent. A user's flag set on
       claim #7 is computed without knowledge of claim #12. If cross-claim
       consistency is required (e.g., "if any of this user's claims has a
       manipulation flag, flag all of this user's claims for review"), that
       logic belongs in M7 or a new post-processing module, not here.
"""

from __future__ import annotations

import logging
from typing import List

from modules.models import (
    MANIPULATION_FLAGS,
    ParsedClaim,
    UserHistory,
    VLMAnalysis,
)

logger = logging.getLogger(__name__)


def aggregate(
    vlm_analysis: VLMAnalysis,
    parsed_claim: ParsedClaim,
    user_history: UserHistory,
) -> str:
    """Combine VLM-assessed and user-history risk flags into a final string.

    Parameters
    ----------
    vlm_analysis :
        M4 output containing ``image_risk_flags``.
    parsed_claim :
        M3 output (used for context — not directly for flags).
    user_history :
        User risk context from ``user_history.csv``.

    Returns
    -------
    str
        Semicolon-joined flags (e.g. ``"blurry_image;manual_review_required"``),
        or ``"none"`` if no flags.
    """
    flags: List[str] = []

    # ── 1. VLM-assessed flags ─────────────────────────────────────────────
    flags.extend(vlm_analysis.image_risk_flags)

    # ── 2. User history flags ─────────────────────────────────────────────
    if user_history.rejected_claim >= 2 or user_history.last_90_days_claim_count >= 3:
        flags.append("user_history_risk")

    if (user_history.history_flags != "none"
            or user_history.manual_review_claim >= 2):
        flags.append("manual_review_required")

    # ── 3. Manipulation → always manual_review_required ───────────────────
    has_manipulation = any(f in MANIPULATION_FLAGS for f in flags)
    if has_manipulation and "manual_review_required" not in flags:
        flags.append("manual_review_required")

    # ── 4. Deduplicate (ordered), sort alphabetically ─────────────────────
    seen: set = set()
    deduped: List[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    deduped.sort()

    # ── 5. Return ─────────────────────────────────────────────────────────
    if not deduped:
        return "none"
    return ";".join(deduped)


__all__ = ["aggregate"]
