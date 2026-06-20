"""
M7 — Output Assembler & Schema Validator  (SPEC.md §5 M7, §6, §7)

Input:  All module outputs for one row
Output: dict with exact schema ready to write to CSV

Key rules:
- Column order is FIXED (see OUTPUT_COLUMNS / SPEC §6).
- Validate every value against allowed enums before writing (SPEC §7).
- Invalid enum → coerce: "unknown" for types/parts, "none" for flags/severity,
  "not_enough_information" for claim_status.
- evidence_standard_met and valid_image → lowercase string "true"/"false" (not bool).
- risk_flags and supporting_image_ids → semicolons, no spaces; "none" if empty.
- Write output.csv with quoting=csv.QUOTE_ALL to handle commas in justification text.

Safe default row (used on any unhandled exception):
  evidence_standard_met="false", evidence_standard_met_reason="Processing error",
  risk_flags="manual_review_required", issue_type="unknown", object_part="unknown",
  claim_status="not_enough_information",
  claim_status_justification="Automated processing failed; manual review required.",
  supporting_image_ids="none", valid_image="false", severity="unknown"

╔═══════════════════════════════════════════════════════════════════════════════╗
║              DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS                   ║
╚═══════════════════════════════════════════════════════════════════════════════╝

This section documents every significant design decision in this module so that
a developer unfamiliar with the codebase can understand why the current approach
was chosen, what alternatives were considered, what is gained and sacrificed,
and under what conditions this implementation may need to be revisited.


────────────────────────────────────────────────────────────────────────────────
  1.  ENUM COERCION OVER STRICT REJECTION
────────────────────────────────────────────────────────────────────────────────

Decision
    When a field value is not in its allowed enum set, the module silently
    coerces it to a safe default and logs a warning, rather than raising a
    validation error, asserting, or propagating an exception to the caller.

Rationale
    - The pipeline processes hundreds of claims per run. Rejecting an entire row
      because one VLM output contains an unexpected issue_type would halve the
      usable output for marginal benefit.
    - A "tolerate and log" design means the evaluation pipeline (M8) can compare
      prompt variants on raw accuracy without failing partway through every run
      due to a single out-of-enum prediction.
    - The spec (§7) explicitly mandates coercion, so this design is
      spec-compliant by definition.

Trade-offs
    + Pipeline keeps running even when upstream modules produce noisy output.
    + Evaluation runs to completion, accumulating more data in aggregate.
    + Simple mental model: every output row is structurally valid.
    - Silently wrong data can propagate. If the VLM outputs "fire_damage" and
      it is coerced to "unknown", a downstream consumer (e.g. the scoring tool)
      sees "unknown" and cannot distinguish it from a genuinely unknown case.
    - Warnings are easy to overlook in a long run; a silent regression where a
      model starts producing out-of-enum values for 80 % of rows would only show
      as degraded accuracy at evaluation time.

Limitations
    - No mechanism to "quarantine" coerced rows for manual review at scale.
      The safe-default row mechanism (§6) only fires on unhandled exceptions,
      not on enum coercion itself.
    - Coercion decisions are per-field; there is no aggregate quality gate
      (e.g. "if more than 10 % of rows had any coercion, flag the whole run").

Future Improvements
    - Add a coercion counter that is logged at the end of a pipeline run so
      operators see "X rows had Y coerced fields" without grepping warnings.
    - Consider an opt-in strict mode (env var or CLI flag) for QA runs where
      any coercion should fail the run early.
    - Store the original (pre-coercion) value side-by-side for auditing while
      writing the cleaned value to the main column.

────────────────────────────────────────────────────────────────────────────────
  2.  STRING-TYPED BOOLEANS FOR CSV COMPATIBILITY
────────────────────────────────────────────────────────────────────────────────

Decision
    The boolean fields evidence_standard_met and valid_image are stored as
    lowercase strings "true" / "false" rather than Python bools. Conversion
    happens via _bool_to_str() in assemble_row and _coerce_bool_str() in
    _validate_row.

Rationale
    - CSV has no native boolean type; the Python csv module writes bools as
      "True" / "False" (title-case), which would produce inconsistent output
      with the rest of the schema, which is already all lowercase.
    - Using lowercase strings ("true"/"false") keeps the output consistent
      and makes it directly comparable via string matching in evaluation.
    - The coercer accepts "true", "1", and "yes" as truthy and everything
      else as falsy, providing tolerance for upstream variations.

Trade-offs
    + Output is consistent: every value is a lowercase string, so consumers
      do not need to handle mixed Python types in dict values.
    + Evaluation comparison is a simple string equality check.
    - Consumers that expect native Python bools must convert, creating a minor
      friction point.
    - The mapping is lossy in one direction: Python True → "true" is exact,
      but "true" ← True requires the consumer to know the convention.
    - The coercion from string input is lenient ("1"/"yes" → truthy) but
      asymmetric — CSV output is always "true"/"false", never "1" or "yes".

Limitations
    - Three-valued logic is not supported. If a future requirement calls for
      "unsure" or "unverifiable" as a distinct state, both fields would need
      to be promoted from boolean-like strings to three-value enums.

Future Improvements
    - No change needed unless a three-state boolean requirement emerges.

────────────────────────────────────────────────────────────────────────────────
  3.  SEMICOLONS AS LIST SEPARATOR WITH "none" SENTINEL
────────────────────────────────────────────────────────────────────────────────

Decision
    Multi-value fields (risk_flags, supporting_image_ids, image_paths) use
    semicolons as the delimiter with no surrounding spaces, and "none" as the
    sentinel value for empty. Normalisation is handled by valid_flags_str()
    and valid_ids_str().

Rationale
    - Commas already appear inside justification text and claim transcripts, so
      using a comma-separated list would collide with CSV quoting and require
      consumers to know which columns use which quoting rules.
    - "none" is a single, unambiguous sentinel. Empty string, whitespace-only
      strings, and "none" itself all collapse to "none" after normalisation.
    - No trailing or leading spaces around items means simple string splitting
      by ";" works without further clean-up.

Trade-offs
    + Semicolons never appear in the data values themselves, so there is no
      ambiguity between delimiter and content.
    + "none" is self-documenting in the output CSV — an operator reading the
      file immediately understands "nothing was flagged".
    - Semicolons are unconventional in CSV-like data; most data analysis tools
      expect commas or tabs, so opening the output in a spreadsheet requires
      an extra split step.
    - The sentinel "none" could conceivably clash with a risk flag named "none"
      that means "explicitly evaluated and found to have no issues" — currently
      "none" is a member of RISK_FLAG_VALUES, and valid_flags_str() will
      preserve it, but valid_ids_str() treats it as empty. This asymmetry is
      intentional (risk flags are additive; image IDs are enumerative) but may
      confuse future maintainers.

Limitations
    - Items that naturally contain semicolons (e.g. multi-word descriptions)
      would break. No current field uses semicolon-containing values.
    - valid_flags_str silently drops unknown flags; if an upstream module
      introduces a new risk flag without updating RISK_FLAG_VALUES, the flag
      disappears without warning.

Future Improvements
    - Add a warning counter when valid_flags_str drops flags, similar to the
      coercion logger in _coerce_enum.
    - If the project adds a new multi-value column, consider whether to use
      the same semicolon convention or adopt a different separator documented
      per column.

────────────────────────────────────────────────────────────────────────────────
  4.  FIXED COLUMN ORDER
────────────────────────────────────────────────────────────────────────────────

Decision
    OUTPUT_COLUMNS defines an immutable ordered list of 14 columns.
    write_csv() writes these exact columns in this exact order, using
    csv.DictWriter with extrasaction="ignore".

Rationale
    - The specification (§6) requires a deterministic output schema so the
      evaluator can read and compare results across submissions.
    - An ordered list (rather than a set or dict) makes the order explicit
      and reviewable in version control.
    - DictWriter with extrasaction="ignore" means that if a row accidentally
      contains extra keys (e.g. from an unmodified upstream dataclass), those
      keys are silently dropped rather than causing an error.

Trade-offs
    + Column order is reviewable at a glance and cannot drift.
    + Any row dict with the correct keys can be written regardless of key
      iteration order (which varied across Python versions).
    - Adding or removing columns requires a deliberate code change, which
      is desirable for this module (it is the output contract) but can feel
      rigid.
    - extrasaction="ignore" means bugs where extra keys are mistakenly passed
      will not be caught at write time.

Limitations
    - The fixed order means columns cannot be rearranged without updating
      every consumer (evaluator, scoring, data warehouse schema).
    - OUTPUT_COLUMNS and SAFE_DEFAULT_ROW must be kept in sync manually;
      there is no programmatic enforcement that SAFE_DEFAULT_ROW contains
      all columns or only the columns in OUTPUT_COLUMNS.

Future Improvements
    - Add a consistency assertion at module import time that SAFE_DEFAULT_ROW
      has exactly the same keys as OUTPUT_COLUMNS.
    - Consider a typed NamedTuple or dataclass for the row so adding/removing
      columns is a single-point change.

────────────────────────────────────────────────────────────────────────────────
  5.  PASS-THROUGH ASSEMBLY (DUMB MAPPER)
────────────────────────────────────────────────────────────────────────────────

Decision
    assemble_row() is a straightforward field-to-field mapper. It receives
    processed objects from modules M1–M6 and copies values into the output
    dict without applying cross-field business logic or derived computations.

Rationale
    - Each upstream module (VLM Engine, Claim Parser, Evidence Evaluator) is
      responsible for its own analysis. Duplicating that logic in the assembler
      would create coupling and violate the single-responsibility principle.
    - Keeping the assembler "dumb" makes it easy to test: given known inputs,
      the output is purely composition, not computation.
    - It creates a clear ownership boundary: if a value is wrong, the bug is
      almost certainly in the module that produced it, not in the assembler.

Trade-offs
    + Minimises the assembler's surface area for bugs.
    + Makes it trivial to trace any output column back to its source module.
    + No risk of domain knowledge leaking into the assembly layer.
    - Cannot perform cross-field sanity checks: e.g. if claim_status is
      "supported" but evidence_standard_met is "false", the assembler still
      writes both values as-is and does not flag the contradiction.
    - Cannot handle "if this field is X, then field Y should be Z" logic
      that could improve output quality.

Limitations
    - Cross-field consistency is entirely the consumer's responsibility.
    - The assembler cannot retroactively fix upstream errors.
    - If a derived field is needed later (e.g. "combined_score"), it would
      require a new module or a change in this module's role.

Future Improvements
    - Introduce an optional consistency-check step (called after _validate_row)
      that applies known cross-field rules (e.g. "supported claims must have
      evidence_standard_met=true") and logs warnings for violations without
      modifying the data.

────────────────────────────────────────────────────────────────────────────────
  6.  SAFE DEFAULT ROW FOR GRACEFUL DEGRADATION
────────────────────────────────────────────────────────────────────────────────

Decision
    When any exception occurs during processing of a claim (e.g. file not
    found, API timeout, parsing crash), the pipeline catches it and calls
    create_safe_default_row(context) to produce a fallback row instead of
    aborting the entire run.

Rationale
    - Batch processing of hundreds of claims means a single bad row should
      not waste the entire run. This is specified in the project contract (§6).
    - The safe default populates identity fields (user_id, image_paths, etc.)
      from the ClaimContext so the row can still be matched back to input.
    - Using a copy of SAFE_DEFAULT_ROW (via dict() copy) ensures the global
      constant is never mutated by repeated calls.

Trade-offs
    + The pipeline always completes and produces N output rows for N input
      claims, making it trivially verifiable that every claim was attempted.
    + Safe defaults are recognisable in output (risk_flags="manual_review_required"
      is the tell), so downstream filters can find and review them.
    - A claim with a transient error gets a safe-default row that looks like a
      genuine "failed to process" decision, indistinguishable from a claim that
      legitimately needs manual review.
    - The safe default is a flat dict, not a richer structure, so it carries
      no error details (stack trace, which module failed, etc.).

Limitations
    - Only per-row exceptions are caught. A bug in the assembler itself
      (e.g. _validate_row raising an unhandled exception) would still crash
      the entire run.
    - The safe default is generated from ClaimContext alone; if M1 itself
      fails to produce a ClaimContext, there is no fallback at all.
    - No retry mechanism. A timeout from the VLM API on one row is final.

Future Improvements
    - Add an optional error_detail column to the schema (or a sidecar JSON
      file) that records the exception class, message, and originating module.
    - Implement per-row retries with exponential backoff for transient API
      errors before falling through to the safe default.

────────────────────────────────────────────────────────────────────────────────
  7.  TWO-PHASE BUILD-THEN-VALIDATE
────────────────────────────────────────────────────────────────────────────────

Decision
    assemble_row() first builds the raw row dict (lines 117-132) and then
    passes it through _validate_row() for enum coercion (line 135). These are
    two distinct phases even though they happen in the same function call.

Rationale
    - Separation of concerns: the build phase is responsible for mapping data
      from upstream modules; the validation phase is responsible for ensuring
      every value fits the schema.
    - Testing each phase independently is easier — unit tests for value mapping
      (e.g. "image_paths joined with semicolons") can be written without
      worrying about enum sets, and vice versa.
    - If validation rules change (e.g. a new enum value is added), only
      _validate_row() needs updating, not the mapping logic.

Trade-offs
    + Readability: a reader can scan the build dict to see "what goes where"
      and scan the validation block to see "what is enforced".
    + Modifyability: adding a new column requires a change in exactly two
      places (the dict literal and a _validate_row clause), which is minimal
      but explicit.
    - Double iteration: the dict is built and then copied again inside
      _validate_row (which calls `dict(row)` on line 187). For hundreds of
      rows the overhead is negligible, but it is a code smell.
    - Coercion happens on every field even when all values are valid, which is
      the common case — performance optimisation was not prioritised.

Limitations
    - The validation phase cannot reject a row cleanly (e.g. "this field is
      so far out of bounds that we should not write it") — it can only coerce.
    - If a new column is added to the build dict but forgotten in _validate_row,
      it bypasses validation entirely.

Future Improvements
    - Make _validate_row operate on an immutable Mapping (or an interface) to
      make the copy explicit and intentional.
    - Add a check that every key in the input row is handled by _validate_row,
      catching columns that would bypass validation.

────────────────────────────────────────────────────────────────────────────────
  8.  claim_object-SCOPED PART VALIDATION
────────────────────────────────────────────────────────────────────────────────

Decision
    The object_part field is validated against a claim_object-specific set
    of allowed values (from models.OBJECT_PART_VALUES). If the VLM produces
    "screen" for a "car" claim, it is coerced to "unknown". Validation uses
    `OBJECT_PART_VALUES.get(claim_object, set()) | {"unknown"}` as the
    allowed set.

Rationale
    - Different claim objects have different valid parts: "screen" is valid
      for a laptop but nonsensical for a car. A per-object enum prevents
      spurious predictions from one domain leaking into another.
    - The union with {"unknown"} ensures that "unknown" is always accepted
      regardless of whether the per-object set explicitly includes it — this
      is a safety net in case the sets drift.

Trade-offs
    + Catches a realistic error class (wrong object part for claim type).
    + No changes needed when adding a new claim_object: just add the entry to
      OBJECT_PART_VALUES in models.py.
    - Relies on the claim_object being already set correctly by M1. If M1
      misclassifies a car claim as "laptop", parts are validated against the
      wrong set and legitimate predictions get coerced.
    - Adding a valid part that is a substring of a currently-invalid part
      is not a problem (exact match), but the set membership test is case-
      sensitive, so "Windshield" fails.

Limitations
    - Only object_part uses claim_object-scoped validation. issue_type and
      severity are global across all claim objects, which is a lossy
      abstraction — "crack" makes sense for a windshield but not for a
      keyboard. No per-object issue_type sets exist yet.
    - OBJECT_PART_VALUES contains "unknown" inside each per-object set AND
      the union adds it again. This is idempotent but redundant.

Future Improvements
    - Extend per-object validation to issue_type if the taxonomy diverges
      by object type (e.g. adding "dented_packaging" for packages only).
    - Add an assertion at import or test time that every per-object part set
      contains "unknown" explicitly, making the union with {"unknown"} a
      documented double-safety rather than a silent redundancy.

────────────────────────────────────────────────────────────────────────────────
  9.  csv.QUOTE_ALL QUOTING
────────────────────────────────────────────────────────────────────────────────

Decision
    write_csv() opens the output file with csv.QUOTE_ALL, meaning every field
    in every row is double-quoted, not just those containing delimiters.

Rationale
    - Several fields contain free text (user_claim, justification, reason)
      that may include commas, newlines, or double-quote characters.
      QUOTE_ALL guarantees these values are never misinterpreted by CSV
      parsers.
    - The output is consumed by an automated evaluator; human-readability
      of the raw CSV is not a priority.

Trade-offs
    + Completely eliminates CSV parsing errors from content characters.
    + No need to escape individual fields based on content inspection.
    - Every field is wrapped in quotes, making the file larger (~15-25 %
      overhead depending on average field length).
    - Less readable when opened in a plain text editor.
    - Some tools (e.g. command-line `csvcut` or `xsv`) handle quoted fields
      correctly, but lightweight text-processing scripts may trip on them.

Limitations
    - csv.QUOTE_ALL does not handle fields that contain embedded double-quote
      characters — Python's csv module will double them (escape them), but
      not all consumers handle that correctly.
    - If a downstream system expects unquoted numeric-like fields (unlikely
      here — all columns are strings), QUOTE_ALL would be surprising.

Future Improvements
    - Consider csv.QUOTE_NONNUMERIC if the schema ever gains purely numeric
      columns. For the current all-string schema, QUOTE_ALL is appropriate.

────────────────────────────────────────────────────────────────────────────────
  10.  CROSS-FIELD VALIDATION GAP (LACK OF CONSISTENCY CHECKS)
────────────────────────────────────────────────────────────────────────────────

Decision
    Each field in _validate_row() is coerced independently. No function enforces
    relationships between fields (e.g. "if claim_status is 'supported', then
    evidence_standard_met should be 'true' and severity should not be 'unknown').

Rationale
    - Adding cross-field rules couples the assembler to domain logic that
      properly belongs in the upstream modules (M5 Evidence Evaluator for
      evidence_standard_met, M4 VLM Engine for claim_status and severity).
    - The spec (§7) defines per-field enum validation; it does not mandate
      cross-field consistency.
    - Every cross-field rule is an assumption about model behaviour that
      may not hold for all prompt variants. Enforcing cross-field rules
      could mask how different prompt formulations behave.

Trade-offs
    + Output faithfully reflects what the upstream modules decided, even if
      those decisions are internally inconsistent.
    + Evaluation metrics are not conflated with post-hoc repair logic.
    - Downstream scoring may penalise inconsistent predictions that a simple
      rule could have fixed (e.g. claiming "supported" with no evidence).
    - The output might contain logically impossible combinations that erode
      trust when the CSV is reviewed by a human.

Limitations
    - No safety rail for logically contradictory output.
    - The assembler cannot be patched to fix a systematic consistency problem
      without taking on domain responsibility that belongs elsewhere.

Future Improvements
    - Add an optional consistency-check step (see §5 "Pass-through assembly"
      above) that is off by default for evaluation runs and on by default for
      production runs, logging warnings for known contradictions without
      modifying the data.

────────────────────────────────────────────────────────────────────────────────
  11.  LENIENT STRING COERCION WITH NO TYPE ENFORCEMENT AT BOUNDARIES
────────────────────────────────────────────────────────────────────────────────

Decision
    _coerce_enum() accepts any value and performs a simple set membership
    test (string equality). There is no type check at the module boundary
    — if an upstream module passes an int, a None, or a bytes object, the
    set membership simply returns False for most inputs, triggering coercion
    to the default.

Rationale
    - Python is dynamically typed, and the upstream modules use dataclasses
      with typed fields. In practice, those fields are always strings.
    - Adding isinstance checks before every coercion would increase code
      complexity and test surface for a case that "should not happen".

Trade-offs
    + Minimal code; easy to read and maintain.
    + Coercion still works correctly for the common case (valid string,
      invalid string, empty string).
    - If an upstream bug sends None instead of a string, the set membership
      raises `TypeError: argument of type 'NoneType' is not iterable` only
      in certain edge cases (None in a set-of-strings check always returns
      False, so it silently coerces — but other types may crash).
    - No defence-in-depth at the module boundary; a non-string type that
      happens to be in the allowed set (impossible for current value sets)
      would pass through without coercion.

Limitations
    - Not all possible type errors are surfaced. Some crash the pipeline,
      some silently coerce, depending on the type.
    - Adding type narrowing (e.g. `mypy` strict mode) would catch most of
      these at development time, but the module itself has no runtime guard.

Future Improvements
    - Add a simple type guard at the top of _validate_row that raises
      a clear TypeError if any value is not a str, making failures
      deterministic and debuggable.

────────────────────────────────────────────────────────────────────────────────
  12.  CROSS-MODULE IMPORT MISMATCH (KNOWN STALE ALIAS)
────────────────────────────────────────────────────────────────────────────────

Decision
    The evaluation pipeline (code/evaluation/main.py, line 59) imports the
    assembler function as `assemble_output`:
        from modules.output_assembler import assemble_output
    However, the actual function is named `assemble_row`. The modules/__init__.py
    also exports `assemble_row`, not `assemble_output`.

    At the time of writing, this import will fail at runtime when the evaluation
    pipeline is executed, because `assemble_output` is not defined in this module
    or re-exported from anywhere.

    This is a known import inconsistency that must be resolved before evaluation
    is run.

Rationale
    The original specification may have used "assemble_output" as the working
    name; the code was inadvertently finalised under "assemble_row" without
    updating the evaluation module's import. This is tracked here rather than
    in commit messages to make it visible at the code level.

Resolution options (pick one):
    A. Rename assemble_row → assemble_output and add a deprecated alias.
    B. Fix the import in evaluation/main.py to use assemble_row.
    C. Add `assemble_output = assemble_row` as a module-level alias here.

Trade-offs
    + Option C (alias) is the smallest diff and keeps both names working.
    + Option A is cleanest semantically but breaks any external code that
      already imports assemble_row.
    + Option B fixes the caller without touching this module, which is the
      narrowest fix but leaves the discrepancy in naming mental models.

Future Improvement
    Resolve this before running evaluation — the alias (option C) has been
    added (see below) as a low-risk bridge. Remove the alias once the
    evaluation pipeline is updated.
"""


from __future__ import annotations

import csv
import logging
from typing import Any, Dict, List, Optional

from modules.models import (
    CLAIM_OBJECT_VALUES,
    CLAIM_STATUS_VALUES,
    ISSUE_TYPE_VALUES,
    OBJECT_PART_VALUES,
    RISK_FLAG_VALUES,
    SEVERITY_VALUES,
    ClaimContext,
    EvidenceEvaluation,
    ParsedClaim,
    VLMAnalysis,
)

logger = logging.getLogger(__name__)


# ── Output Schema ─────────────────────────────────────────────────────────

OUTPUT_COLUMNS = [
    "user_id",
    "image_paths",
    "user_claim",
    "claim_object",
    "evidence_standard_met",
    "evidence_standard_met_reason",
    "risk_flags",
    "issue_type",
    "object_part",
    "claim_status",
    "claim_status_justification",
    "supporting_image_ids",
    "valid_image",
    "severity",
]

SAFE_DEFAULT_ROW: Dict[str, str] = {
    "user_id": "",
    "image_paths": "",
    "user_claim": "",
    "claim_object": "",
    "evidence_standard_met": "false",
    "evidence_standard_met_reason": "Processing error",
    "risk_flags": "manual_review_required",
    "issue_type": "unknown",
    "object_part": "unknown",
    "claim_status": "not_enough_information",
    "claim_status_justification": (
        "Automated processing failed; manual review required."
    ),
    "supporting_image_ids": "none",
    "valid_image": "false",
    "severity": "unknown",
}


# ── Public API ────────────────────────────────────────────────────────────


def assemble_row(
    context: ClaimContext,
    vlm_analysis: VLMAnalysis,
    parsed_claim: ParsedClaim,
    evidence_eval: EvidenceEvaluation,
    risk_flags: str,
) -> Dict[str, str]:
    """Assemble all module outputs into a single output row dict.

    Parameters
    ----------
    context :
        Original claim context from M1.
    vlm_analysis :
        Visual analysis from M4.
    parsed_claim :
        Structured claim from M3.
    evidence_eval :
        Evidence evaluation from M5.
    risk_flags :
        Final risk flags string from M6 (semicolons or "none").

    Returns
    -------
    Dict[str, str]
        Row dict matching OUTPUT_COLUMNS order, with all values coerced
        to valid enums.
    """
    # ── Build row ─────────────────────────────────────────────────────────
    row: Dict[str, str] = {
        "user_id": context.user_id,
        "image_paths": ";".join(context.image_paths),
        "user_claim": context.user_claim,
        "claim_object": context.claim_object,
        "evidence_standard_met": _bool_to_str(evidence_eval.evidence_standard_met),
        "evidence_standard_met_reason": evidence_eval.evidence_standard_met_reason,
        "risk_flags": risk_flags if risk_flags else "none",
        "issue_type": vlm_analysis.issue_type,
        "object_part": vlm_analysis.object_part,
        "claim_status": vlm_analysis.claim_status,
        "claim_status_justification": vlm_analysis.claim_status_justification,
        "supporting_image_ids": vlm_analysis.supporting_image_ids or "none",
        "valid_image": _bool_to_str(vlm_analysis.valid_image),
        "severity": vlm_analysis.severity,
    }

    # ── Validate and coerce ───────────────────────────────────────────────
    row = _validate_row(context.claim_object, row)
    return row


#: Alias for the evaluation pipeline (M8).  Remove after evaluation/main.py
#: is updated to import assemble_row directly.  See Design Decision 12 above.
assemble_output = assemble_row


def write_csv(
    rows: List[Dict[str, str]],
    output_path: str,
) -> None:
    """Write assembled rows to a CSV file.

    Parameters
    ----------
    rows :
        List of row dicts (from ``assemble_row()``).
    output_path :
        Absolute or relative path for the output CSV.
    """
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def create_safe_default_row(context: ClaimContext) -> Dict[str, str]:
    """Create a safe default row for a claim that encountered a processing error.

    Parameters
    ----------
    context :
        Original claim context (used for identity fields).
    """
    row = dict(SAFE_DEFAULT_ROW)
    row["user_id"] = context.user_id
    row["image_paths"] = ";".join(context.image_paths)
    row["user_claim"] = context.user_claim
    row["claim_object"] = context.claim_object
    return row


# ── Validation & coercion ─────────────────────────────────────────────────


def _validate_row(claim_object: str, row: Dict[str, str]) -> Dict[str, str]:
    """Validate every value against allowed enums; coerce invalid values.

    Parameters
    ----------
    claim_object :
        The claim object type ("car", "laptop", "package") to select
        the appropriate object_part enum set.
    """
    validated = dict(row)

    # claim_object (pass-through, set by M1)
    validated["claim_object"] = _coerce_enum(
        validated.get("claim_object", ""),
        CLAIM_OBJECT_VALUES,
        "unknown",
    )

    # evidence_standard_met
    validated["evidence_standard_met"] = _coerce_bool_str(
        validated.get("evidence_standard_met", "false"),
    )

    # risk_flags (already assembled by M6, but validate individual flags)
    validated["risk_flags"] = valid_flags_str(validated.get("risk_flags", "none"))

    # issue_type
    validated["issue_type"] = _coerce_enum(
        validated.get("issue_type", "unknown"),
        ISSUE_TYPE_VALUES,
        "unknown",
    )

    # object_part (use claim_object-specific set)
    allowed_parts = OBJECT_PART_VALUES.get(claim_object, set()) | {"unknown"}
    validated["object_part"] = _coerce_enum(
        validated.get("object_part", "unknown"),
        allowed_parts,
        "unknown",
    )

    # claim_status
    validated["claim_status"] = _coerce_enum(
        validated.get("claim_status", "not_enough_information"),
        CLAIM_STATUS_VALUES,
        "not_enough_information",
    )

    # valid_image
    validated["valid_image"] = _coerce_bool_str(
        validated.get("valid_image", "false"),
    )

    # severity
    validated["severity"] = _coerce_enum(
        validated.get("severity", "unknown"),
        SEVERITY_VALUES,
        "unknown",
    )

    # supporting_image_ids (validate individual IDs don't have spaces)
    validated["supporting_image_ids"] = valid_ids_str(
        validated.get("supporting_image_ids", "none"),
    )

    return validated


def _coerce_enum(value: str, allowed: set, default: str) -> str:
    """Return *value* if it's in *allowed*, otherwise return *default*."""
    if value in allowed:
        return value
    logger.warning("Coercing '%s' -> '%s' (not in %s)", value, default, allowed)
    return default


def _coerce_bool_str(value: str) -> str:
    """Return lowercase 'true' or 'false'."""
    return "true" if value.lower().strip() in ("true", "1", "yes") else "false"


def _bool_to_str(value: bool) -> str:
    """Convert a Python bool to lowercase string."""
    return "true" if value else "false"


def valid_flags_str(flags: str) -> str:
    """Validate individual risk flags; remove any that aren't in RISK_FLAG_VALUES."""
    if not flags or flags.strip() == "":
        return "none"
    parts = [f.strip() for f in flags.split(";")]
    valid = [f for f in parts if f in RISK_FLAG_VALUES]
    if not valid:
        return "none"
    return ";".join(valid)


def valid_ids_str(ids: str) -> str:
    """Normalize supporting_image_ids: no spaces, 'none' if empty."""
    if not ids or ids.strip() == "" or ids.strip() == "none":
        return "none"
    # Remove any spaces around semicolons
    parts = [p.strip() for p in ids.split(";")]
    parts = [p for p in parts if p]  # drop empties
    if not parts:
        return "none"
    return ";".join(parts)


__all__ = [
    "OUTPUT_COLUMNS",
    "SAFE_DEFAULT_ROW",
    "assemble_output",
    "assemble_row",
    "create_safe_default_row",
    "write_csv",
]
