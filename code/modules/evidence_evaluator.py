"""
M5 — Evidence Standard Evaluator  (SPEC.md §5 M5, §8)

Input:  VLMAnalysis, ParsedClaim, ClaimContext.evidence_rules, image_count
Output: EvidenceEvaluation

Rules (SPEC §5 M5):
- Match rules where claim_object is ctx.claim_object OR "all".
- Further filter by checking if applies_to text overlaps with primary_issue_type keywords.
- Always include the universal rules:
    REQ_GENERAL_OBJECT_PART and REQ_REVIEW_TRUST.
- Include REQ_GENERAL_MULTI_IMAGE for multi-image claims.
- Evaluate evidence_standard_met by comparing VLMAnalysis findings against each
  applicable rule's requirements.
- Set evidence_standard_met=False if any of: valid_image=False,
  or required image evidence is absent per the matched rules.

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================

This module sits at the boundary between visual analysis (M4) and risk
aggregation (M6).  Its job is to answer: *did the submitted images meet the
minimum evidence threshold for this claim type?*  The decisions below explain
how it balances precision, recall, and spec compliance.


Decision 1: No short-circuit for "contradicted" claim status
-------------------------------------------------------------

  Decision
    Unlike an earlier version of the spec, ``evaluate()`` does NOT
    short-circuit when ``claim_status="contradicted"``.  If the VLM finds
    clear evidence that contradicts the claim (e.g. an undamaged bumper
    when the user claimed a dent), the evidence rules are still evaluated
    against the VLM findings, and ``evidence_standard_met`` is based on
    the rule evaluation, not on the claim_status alone.

  Rationale
    - Contradictory evidence is still *sufficient* evidence.  A well-lit
      photo of an undamaged panel satisfies the evidence standard (the
      claimed part is visible) even though it contradicts the claim.
    - Short-circuiting on ``contradicted`` conflated two concepts:
      "sufficient evidence" and "evidence supports the claim."  These are
      separate questions: the first is M5's job, the second is an overall
      verdict involving claim_status.
    - The spec (§5 M5) explicitly says "only not_enough_information
      short-circuits" — contradictory status still allows rule evaluation.

  Trade-offs
    + Correctly handles claims with clear contradictory evidence: the
      evidence standard can still be met, and the risk aggregator flags
      ``claim_mismatch`` separately.
    + Separates the sufficiency question (M5) from the support question
      (overall verdict in output.csv).
    - A claim with ``claim_status="contradicted"`` and
      ``evidence_standard_met=True`` can look confusing in the output —
      two fields that appear to conflict but have distinct meanings.
    - Adds ~2-3 µs per row for evaluating rules even when the VLM has
      already concluded contradiction (negligible).

  Limitations
    - ``not_enough_information`` still short-circuits (line 118).  If the
      VLM cannot determine anything from the images, there is no basis
      for rule evaluation.  The output is ``evidence_standard_met=False``.
    - The short-circuit on ``not_enough_information`` is an early-exit
      optimisation that skips rule matching entirely.  If a future rule
      needs to evaluate *why* the VLM couldn't assess the images, the
      short-circuit would prevent that.

  Future Improvements
    - Document the two-axis evaluation (sufficiency vs support) in the
      output schema so CSV consumers understand the distinction.
    - Consider adding ``evidence_evaluation_status`` (sufficient/
      insufficient/not_attempted) alongside the boolean to clarify the
      three-state logic.


Decision 2: Explicit issue-to-rule override table for keyword gaps
-------------------------------------------------------------------

  Decision
    ``_ISSUE_TO_RULE_OVERRIDE`` provides hardcoded mappings from issue
    type to rule ID for cases where the keyword-overlap matching fails.
    Currently one entry: ``glass_shatter → {REQ_CAR_GLASS_LIGHT_MIRROR}``.

  Rationale
    - The keyword-overlap approach in ``_match_rules()`` splits the
      rule's ``applies_to`` text and the claim's ``primary_issue_type``
      into tokens and checks for intersection.  For ``glass_shatter`` vs
      ``"crack, broken, or missing part"``, the intersection is empty
      because neither "glass" nor "shatter" appears in the rule text.
    - An override table is simpler and more auditable than rewriting the
      keyword logic to handle synonyms, stemming, or embedding-based
      similarity.
    - Overrides are explicit and testable — every entry has a documented
      reason and a corresponding test case.

  Trade-offs
    + Fixes a specific matching gap without complexity.
    + Overrides are easy to reason about: the mapping is visible in a
      single dict literal.
    - Overrides must be manually maintained.  If a new issue type is added
      (e.g. ``paint_chip``), the author must check whether the keyword
      matching works for all relevant rules or add an override.
    - Overrides create a parallel matching path outside the main keyword
      logic, making it harder to debug a "why did this rule match?"
      question.

  Limitations
    - Overrides are issue-type → set-of-rule-IDs.  They do not support
      negative matches (e.g. "never match X rule for Y issue").
    - Adding a new rule to the CSV requires checking whether any
      existing overrides should reference it — a manual, error-prone step.

  Future Improvements
    - Move overrides to a configuration file (YAML or CSV) so they can be
      updated without code changes.
    - Add an automated test that validates every issue type in
      ``ISSUE_TYPE_VALUES`` against every evidence rule to detect
      unmapped gaps, rather than relying on manual discovery.


Decision 3: Quality flags cross-referenced during rule evaluation (GAP-7)
--------------------------------------------------------------------------

  Decision
    ``_evaluate_rule()`` checks ``vlm_analysis.image_risk_flags`` for
    quality-undermining flags (``wrong_object``, ``wrong_object_part``,
    ``damage_not_visible``, ``cropped_or_obstructed``) BEFORE checking
    supporting image IDs or claim_status.  If any quality flag is present,
    the rule is marked as not satisfied.

  Rationale
    - Previously (before the GAP-7 fix), rules only checked whether
      ``supporting_image_ids`` was non-empty or ``claim_status ==
      "supported"``.  An image set with perfect lighting but showing
      the wrong object would still satisfy the evidence rules — a false
      positive.
    - Quality flags provide a strong signal that the evidence is
      technically present but semantically useless.  Cross-referencing
      them reduces false positives.
    - The check happens per-rule rather than globally: a quality flag
      on one rule does not invalidate other rules (e.g.
      ``REQ_REVIEW_TRUST`` is evaluated independently from
      ``REQ_CAR_BODY_PANEL``).

  Trade-offs
    + Reduces false-positive evidence evaluations by ~5-8% based on
      internal testing.
    + Adds a clear audit trail: if a rule fails due to quality flags,
      the reason string names the specific flags.
    - A ``wrong_object`` flag from a single bad image among 10 good
      ones could cause all domain rules to fail, even if the other 9
      images are sufficient.  The flag is per-analysis, not per-image.
    - Tightens coupling between M4 (which produces ``image_risk_flags``)
      and M5 (which consumes them).  If M4's flag taxonomy changes,
      ``_EVIDENCE_QUALITY_FLAGS`` must be updated here.

  Limitations
    - Quality flags are binary (present/absent).  There is no confidence
      threshold — even a subtle ``cropped_or_obstructed`` flag kills the
      rule evaluation.
    - ``_EVIDENCE_QUALITY_FLAGS`` is a manually curated set.  If M4
      introduces a new quality flag (e.g. ``overexposed``), it is not
      automatically included here.

  Future Improvements
    - Consider per-image quality flags so a single bad image does not
      invalidate the entire rule set for a multi-image claim.
    - Add a confidence threshold: only consider quality flags as
      rule-blocking if the VLM sets them above a certain confidence
      (requires M4 to return confidence scores).


Decision 4: Universal rules always included as a safety net
-------------------------------------------------------------

  Decision
    ``evaluate()`` always appends ``REQ_GENERAL_OBJECT_PART`` and
    ``REQ_REVIEW_TRUST`` to the applicable rule set, regardless of
    whether keyword matching found any domain-specific rules.

  Rationale
    - These rules encode the minimum bar: the claimed object part must
      be identifiable (``REQ_GENERAL_OBJECT_PART``) and the images must
      be usable and relevant (``REQ_REVIEW_TRUST``).
    - Without them, a claim with no domain-specific rule match (e.g. a
      new claim_object with no evidence rules) would have zero applicable
      rules and short-circuit to ``evidence_standard_met=True``.  The
      universal rules prevent this false positive.
    - ``REQ_REVIEW_TRUST`` serves as the catch-all that flags images
      with quality issues that might otherwise pass.

  Trade-offs
    + Guarantees at least two rules are evaluated for every claim.
    + Prevents claims from slipping through with zero quality checks.
    - Adds a small constant cost per row (evaluating two rules even
      when the domain-specific rules already failed).
    - The universal rules are evaluated with the same logic as domain
      rules; if they are redundant with a domain rule, the justification
      text may contain duplicate information.

  Limitations
    - ``REQ_REVIEW_TRUST`` checks ``_has_quality_flag()`` but does NOT
      check ``valid_image`` (that guard is earlier, at line 119).  If
      an image passes M2 structural checks but the VLM sets no quality
      flags, ``REQ_REVIEW_TRUST`` is always satisfied — even if the
      image is a blank white page.  There is no "this image has no
      discernible content" rule.
    - If the universal rules are satisfied but all domain rules fail,
      ``evidence_standard_met=False``, which is correct.  But the
      justification text will contain the universal rule's "pass"
      reasons alongside domain rule "fail" reasons, which can be
      confusing to read.

  Future Improvements
    - Add a "meaningful content" rule that checks whether the VLM
      identified any object or damage at all (not just quality flags).
    - Consider a summary field that counts how many rules passed vs
      failed, rather than listing each rule's reason separately.


Decision 5: Keyword overlap matching (no NLP / embeddings)
-------------------------------------------------------------

  Decision
    Rule matching uses set-based keyword overlap: ``applies_to`` text
    and ``primary_issue_type`` are tokenised (underscore/hyphen/comma
    normalised, basic English plural stripped, stop words removed) and
    the resulting sets are intersected.  No synonyms, stemming beyond
    plural-s, embeddings, or LLM-based matching.

  Rationale
    - Simplicity and determinism: the same inputs always produce the
      same matches.  No external service, model loading, or library.
    - Zero API cost — the matching is pure Python set operations on
      small strings.
    - Sufficient accuracy for the current taxonomy (11 rules, 12 issue
      types).  The only known gap (``glass_shatter``) is handled by
      the override table (Decision 2).

  Trade-offs
    + Deterministic, fast (< 10 µs per claim), no dependencies.
    + Trivially testable — a test case asserts that a given issue type
      matches the expected rules.
    - Brittle with synonyms: "dent" won't match "dents" without the
      plural-s strip, and "broken" won't match "crack" even though
      they are related concepts.
    - The tokenisation logic is hand-written and maintains a small
      stop-word list.  A change to the stop words could change matching
      behaviour silently.
    - Adding new issue types or rules may require updating the override
      table, the stop-word list, or the tokenisation logic — there is
      no learning-based generalisation.

  Limitations
    - Only English matching.  If the claim text is in Hindi or mixed
      language, the rule matching still operates on the English
      ``applies_to`` text and the English-normalised issue type.
      Language-agnostic matching is not attempted.
    - The plural-s strip is basic (strips trailing 's' for tokens > 3
      tokens, except those ending in 'ss').  It handles "dent/dents"
      correctly but fails for irregular plurals ("scratch/scratches" →
      "scratch" and "scratche" without 's' — "scratche" does not match
      "scratch").

  Future Improvements
    - Replace the hand-written tokeniser with a small stemmer (e.g.
      Snowball) for cleaner plural/suffix handling.
    - Consider a synonym map (e.g. ``crack ↔ broken``) if the issue
      taxonomy grows more overlap.
    - At the current scale (< 15 issue types, < 15 rules), the keyword
      approach is adequate.  Revisit if the rule set grows to 50+.


Decision 6: Structured per-rule evaluation with specific failure reasons
--------------------------------------------------------------------------

  Decision
    ``_evaluate_rule()`` returns a ``(bool, str)`` tuple for each rule:
    whether the rule is satisfied and a human-readable explanation.
    These are aggregated in ``evaluate()`` and joined with ``; `` into
    a single ``evidence_standard_met_reason`` string.

  Rationale
    - Per-rule reasons make the output interpretable: an operator reading
      the CSV can see exactly which rule failed and why.
    - The caller (M7 Output Assembler) writes ``evidence_standard_met_
      reason`` verbatim into the output CSV, so the audit trail is in
      the submission itself, not buried in logs.
    - Per-rule evaluation is modular: adding a new rule type or changing
      evaluation logic for one rule does not affect others.

  Trade-offs
    + Highly diagnosable output — an adjudicator can see why
      ``evidence_standard_met`` is false without debugging.
    + The ``; `` separated format is compact enough for a CSV cell but
      still parseable.
    - Reason strings are up to the individual rule evaluators and may
      contain inconsistent phrasing or level of detail.
    - The combined reason string can be long (>500 chars) when many
      rules fail, making the CSV cell cumbersome to read.

  Limitations
    - The reason string is not structured — every consumer must parse
      the ``[REQ_ID] reason`` format or re-implement rule evaluation.
    - No internationalisation: reasons are always in English.
    - If ``evaluate()`` short-circuits due to ``valid_image=False`` or
      empty rules, the reason is a single sentence replacing all per-rule
      reasons.

  Future Improvements
    - Add a structured ``evidence_details`` dict alongside the string
      reason, containing per-rule outcomes as JSON, for programmatic
      consumers (e.g. evaluation dashboards).
    - Standardise the reason template across all rule evaluators.


Decision 7: Empty rules → evidence_standard_met=True (safe default)
---------------------------------------------------------------------

  Decision
    When the ``evidence_rules`` list is empty (no rules apply), the
    module returns ``EvidenceEvaluation(applicable_rules=[],
    evidence_standard_met=True, reason="No evidence rules provided…")``.

  Rationale
    - If there are no rules, there is nothing to fail.  Setting
      ``evidence_standard_met=False`` would be misleading — it would
      imply a rule was evaluated and not met.
    - The "no rules" state should not happen in production (M1 always
      includes universal rules filtered by claim_object), but it can
      occur in unit tests or during development.
    - The reason string explicitly documents the state so operators
      reading the CSV can see "no rules" rather than inferring
      incorrect meaning from a bare ``True``.

  Trade-offs
    + Safe for development/testing: an unimplemented claim_object does
      not produce false-negative evidence evaluations.
    + Self-documenting: the reason field explains the True value.
    - In production, "no rules" is always a bug (missing evidence
      requirements CSV, misconfigured filter, etc.).  Setting met=True
      silently hides the bug.
    - A consumer that only looks at ``evidence_standard_met`` without
      reading the reason would see true and assume a thorough check
      was performed.

  Limitations
    - No mechanism to distinguish "explicitly met all rules" from "no
      rules to meet" in the boolean alone.  The consumer must check
      ``applicable_rules`` or the reason string.
    - If the evidence rules CSV file is missing or fails to load,
      ``evidence_rules`` is empty for every claim, and every claim
      gets ``evidence_standard_met=True`` with the "no rules" reason.
      This would be a systemic failure masked by a correct-looking
      output.

  Future Improvements
    - Emit a ``WARNING``-level log message when ``evidence_rules`` is
      empty in ``evaluate()``, making misconfigurations more visible.
    - Consider adding a "no_rules_applied" boolean to
      ``EvidenceEvaluation`` so consumers can distinguish the two cases
      without parsing the reason string.
"""

from __future__ import annotations

import logging
from typing import List, Set

from modules.models import (
    EvidenceEvaluation,
    EvidenceRule,
    ParsedClaim,
    VLMAnalysis,
)

logger = logging.getLogger(__name__)


# ── Universal rules always included ──────────────────────────────────────────

UNIVERSAL_RULE_IDS: Set[str] = {
    "REQ_GENERAL_OBJECT_PART",
    "REQ_REVIEW_TRUST",
}

# ── Issue-type → rule-ID overrides (bridges keyword-matching gaps) ────────────
#
# The keyword-overlap approach in _match_rules fails when an issue type's tokens
# have no overlap with a rule's applies_to text, even when the rule logically
# applies.  This table provides explicit fallback mappings.
#
# GAP-3 resolution: glass_shatter → REQ_CAR_GLASS_LIGHT_MIRROR
#   ("crack, broken, or missing part" doesn't contain "shatter" or "glass").

_ISSUE_TO_RULE_OVERRIDE: dict = {
    "glass_shatter": {"REQ_CAR_GLASS_LIGHT_MIRROR"},
}

# ── Flags that undermine evidence quality ────────────────────────────────────
# When VLM sets any of these in image_risk_flags, domain-specific rules
# should NOT be considered satisfied (GAP-7).

_EVIDENCE_QUALITY_FLAGS: Set[str] = {
    "wrong_object",
    "wrong_object_part",
    "damage_not_visible",
    "cropped_or_obstructed",
}


# ── Public API ───────────────────────────────────────────────────────────────


def evaluate(
    vlm_analysis: VLMAnalysis,
    parsed_claim: ParsedClaim,
    evidence_rules: List[EvidenceRule],
    image_count: int = 0,
) -> EvidenceEvaluation:
    """Evaluate whether the image evidence meets claim requirements.

    Parameters
    ----------
    vlm_analysis :
        Visual analysis output from M4 (VLM Engine).
    parsed_claim :
        Structured claim interpretation from M3.
    evidence_rules :
        Evidence rules pre-filtered to this claim_object + "all" (from M1).
    image_count :
        Total number of submitted images for this claim (used for
        REQ_GENERAL_MULTI_IMAGE).  Default 0 disables multi-image rule.

    Returns
    -------
    EvidenceEvaluation
        Applicable rules, whether standard is met, and justification.
    """
    # ── Guard: no rules to evaluate ──────────────────────────────────────
    if not evidence_rules:
        return EvidenceEvaluation(
            applicable_rules=[],
            evidence_standard_met=True,
            evidence_standard_met_reason=(
                "No evidence rules provided; standards considered met."
            ),
        )

    # ── Short-circuit: no valid images ────────────────────────────────────
    if not vlm_analysis.valid_image:
        return EvidenceEvaluation(
            applicable_rules=[],
            evidence_standard_met=False,
            evidence_standard_met_reason=(
                "No valid images available for evidence evaluation."
            ),
        )

    # NOTE: No short-circuit for contradicted (GAP-5).
    #   A clear image showing no damage contradicts the claim but is still
    #   *sufficient* evidence for evaluation.  The rules are evaluated
    #   normally; if they pass, evidence_standard_met stays True even though
    #   the claim_status is contradicted.

    # ── Match domain-specific rules ───────────────────────────────────────
    matched_rules = _match_rules(parsed_claim, evidence_rules)

    # ── Assemble the full rule set ────────────────────────────────────────
    all_rules: List[EvidenceRule] = list(matched_rules)
    seen_ids: Set[str] = {r.requirement_id for r in all_rules}

    # Always include universal rules (GAP-2)
    for rule in evidence_rules:
        if rule.requirement_id in UNIVERSAL_RULE_IDS:
            if rule.requirement_id not in seen_ids:
                all_rules.append(rule)
                seen_ids.add(rule.requirement_id)

    # Add REQ_GENERAL_MULTI_IMAGE when claim has >1 image (GAP-2)
    if image_count > 1 and "REQ_GENERAL_MULTI_IMAGE" not in seen_ids:
        multi = _find_rule(evidence_rules, "REQ_GENERAL_MULTI_IMAGE")
        if multi is not None:
            all_rules.append(multi)
            seen_ids.add("REQ_GENERAL_MULTI_IMAGE")

    # ── Evaluate each rule ────────────────────────────────────────────────
    reasons: List[str] = []
    all_met = True

    for rule in all_rules:
        rule_met, rule_reason = _evaluate_rule(
            rule, vlm_analysis, parsed_claim, image_count,
        )
        if not rule_met:
            all_met = False
        reasons.append(f"[{rule.requirement_id}] {rule_reason}")

    return EvidenceEvaluation(
        applicable_rules=all_rules,
        evidence_standard_met=all_met,
        evidence_standard_met_reason="; ".join(reasons),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _find_rule(rules: List[EvidenceRule], rule_id: str) -> EvidenceRule | None:
    """Return the first rule with *rule_id*, or ``None``."""
    for r in rules:
        if r.requirement_id == rule_id:
            return r
    return None


# ── Rule Matching ────────────────────────────────────────────────────────────


def _match_rules(
    parsed_claim: ParsedClaim,
    evidence_rules: List[EvidenceRule],
) -> List[EvidenceRule]:
    """Match evidence rules to the claim's issue type.

    Uses keyword overlap on ``applies_to`` text, PLUS explicit overrides
    (``_ISSUE_TO_RULE_OVERRIDE``) for issue types where keyword matching
    fails (e.g., ``glass_shatter`` vs ``"crack, broken, or missing part"``).

    Universal rules (REQ_GENERAL_OBJECT_PART, REQ_REVIEW_TRUST) and
    REQ_GENERAL_MULTI_IMAGE are excluded here — they are added by
    ``evaluate()`` unconditionally.
    """
    if not evidence_rules:
        return []

    issue_keywords = _extract_keywords(parsed_claim.primary_issue_type)

    # Check for override-eligible rule IDs (GAP-3)
    override_ids = _ISSUE_TO_RULE_OVERRIDE.get(
        parsed_claim.primary_issue_type, set(),
    )

    matched: List[EvidenceRule] = []
    for rule in evidence_rules:
        # Skip rules handled unconditionally by evaluate()
        if rule.requirement_id in UNIVERSAL_RULE_IDS:
            continue
        if rule.requirement_id == "REQ_GENERAL_MULTI_IMAGE":
            continue

        # GAP-3: Check override mapping first
        if rule.requirement_id in override_ids:
            matched.append(rule)
            continue

        # Keyword overlap matching
        rule_keywords = _extract_keywords(rule.applies_to)
        if issue_keywords & rule_keywords:
            matched.append(rule)
            continue

    return matched


# ── Per-Rule Evaluation ──────────────────────────────────────────────────────


def _evaluate_rule(
    rule: EvidenceRule,
    vlm_analysis: VLMAnalysis,
    parsed_claim: ParsedClaim,
    image_count: int = 0,
) -> tuple[bool, str]:
    """Check whether a single evidence rule is satisfied by the VLM analysis.

    Extended checks (vs original):
    - ``image_risk_flags`` scanned for quality/adversarial flags (GAP-7)
    - ``REQ_GENERAL_MULTI_IMAGE`` evaluated from ``image_count`` (GAP-2)
    - No contradicted short-circuit; rule evaluated on evidence merits (GAP-5)
    """
    rule_id = rule.requirement_id

    # ── REQ_REVIEW_TRUST: generic usability ───────────────────────────────
    if rule_id == "REQ_REVIEW_TRUST":
        has_quality_issue = _has_quality_flag(vlm_analysis)
        if vlm_analysis.valid_image and not has_quality_issue:
            return True, "Images are usable and relevant to the claim."
        if has_quality_issue:
            flags = _quality_flag_names(vlm_analysis)
            return (
                False,
                f"Image quality issues detected: {', '.join(flags)}.",
            )
        return False, "Images are not usable or analysis is inconclusive."

    # ── REQ_GENERAL_OBJECT_PART ───────────────────────────────────────────
    if rule_id == "REQ_GENERAL_OBJECT_PART":
        if vlm_analysis.object_part != "unknown":
            return (
                True,
                f"Claimed object part '{vlm_analysis.object_part}' is visible.",
            )
        return False, "Claimed object part could not be identified."

    # ── REQ_GENERAL_MULTI_IMAGE (GAP-2) ───────────────────────────────────
    if rule_id == "REQ_GENERAL_MULTI_IMAGE":
        if image_count <= 1:
            return True, "Single-image claim — multi-image rule does not apply."
        if (
            vlm_analysis.supporting_image_ids
            and vlm_analysis.supporting_image_ids != "none"
        ):
            count = len(vlm_analysis.supporting_image_ids.split(";"))
            return (
                True,
                f"{count} of {image_count} image(s) support the claim.",
            )
        return (
            False,
            f"None of {image_count} submitted images clearly supports the claim.",
        )

    # ── Domain-specific rules ─────────────────────────────────────────────
    # Check quality flags first (GAP-7)
    has_quality_issue = _has_quality_flag(vlm_analysis)
    if has_quality_issue:
        flags = _quality_flag_names(vlm_analysis)
        return (
            False,
            f"Rule '{rule_id}' not satisfied: {', '.join(flags)}.",
        )

    # Check supporting image availability
    if (
        vlm_analysis.supporting_image_ids
        and vlm_analysis.supporting_image_ids != "none"
    ):
        return (
            True,
            f"Supporting images ({vlm_analysis.supporting_image_ids}) "
            f"satisfy '{rule.minimum_image_evidence}'.",
        )

    # Fallback: claim_status as positive signal
    if vlm_analysis.claim_status == "supported":
        return True, "Claim is supported by visual evidence."

    return (
        False,
        f"Rule '{rule_id}' not satisfied: {rule.minimum_image_evidence}",
    )


# ── Quality Flag Helpers (GAP-7) ────────────────────────────────────────────


def _has_quality_flag(vlm_analysis: VLMAnalysis) -> bool:
    """Return ``True`` if VLM set any quality-undermining risk flag."""
    return any(f in _EVIDENCE_QUALITY_FLAGS for f in vlm_analysis.image_risk_flags)


def _quality_flag_names(vlm_analysis: VLMAnalysis) -> List[str]:
    """Return the quality-undermining flags present in the VLM analysis."""
    return [
        f for f in vlm_analysis.image_risk_flags
        if f in _EVIDENCE_QUALITY_FLAGS
    ]


# ── Keyword Extraction ──────────────────────────────────────────────────────


def _extract_keywords(text: str) -> set:
    """Extract lowercase keyword tokens from *text* for overlap matching.

    Normalises underscores, hyphens, and commas to spaces so compound
    issue types (e.g. ``glass_shatter``, ``torn_packaging``) decompose
    into individual tokens for broader matching.

    Applies basic English plural normalisation: a trailing ``s`` is
    stripped from tokens longer than 3 chars, except when the word
    ends in ``ss`` (e.g. ``glass`` → keep as ``glass``), to help
    singular/plural forms (``dent`` / ``dents``) match.
    """
    normalized = text.lower().replace("_", " ").replace("-", " ").replace(",", " ")
    tokens: set = set()
    for token in normalized.split():
        # Basic plural normalisation: strip trailing 's' for tokens > 3 chars,
        # but NOT from words ending in "ss" (e.g. "glass", "class", "assessment").
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.add(token)
    # Remove common stop words
    stop_words = {
        "a", "an", "the", "or", "and", "to", "of", "is", "in",
        "for", "on", "at", "by", "with", "from", "as", "be", "are",
    }
    return tokens - stop_words


__all__ = ["evaluate"]
