"""
Shared dataclasses, enums, and model configurations.

Contracts (SPEC.md §5 / §7):
- CLAIM_STATUS_VALUES: "supported" | "contradicted" | "not_enough_information"
- ISSUE_TYPE_VALUES: dent, scratch, crack, glass_shatter, broken_part,
    missing_part, torn_packaging, crushed_packaging, water_damage, stain,
    none, unknown
- SEVERITY_VALUES: none | low | medium | high | unknown
- OBJECT_PART_VALUES: per claim_object (car / laptop / package)
- RISK_FLAG_VALUES: none | blurry_image | cropped_or_obstructed |
    low_light_or_glare | wrong_angle | wrong_object | wrong_object_part |
    damage_not_visible | claim_mismatch | possible_manipulation |
    non_original_image | text_instruction_present | user_history_risk |
    manual_review_required
- MANIPULATION_FLAGS: possible_manipulation | non_original_image |
    text_instruction_present

Dataclasses:
  UserHistory       — user risk context from user_history.csv
  EvidenceRule      — one row from evidence_requirements.csv
  ClaimContext      — full context for one claim row (output of M1)
  ImageValidationResult — output of M2
  ParsedClaim       — output of M3
  EvidenceContext   — output of M4 (injected into M5 prompt)
  ModelSet          — named team of role-tagged models (budget/balanced/premium)
  VLMAnalysis       — output of M5

Model sets (each set is a Dict[role, model_id]; add a role to extend without new variables):
  MODEL_SETS        — {"budget": ModelSet, "balanced": ModelSet, "premium": ModelSet}

Roles defined so far:
  "vlm"     — visual analysis model used by M5
  "compare" — secondary model for evaluation comparison (M8 only)

=============================================================================
DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS
=============================================================================

This module is a dependency of every other module in the pipeline.  It
defines the shared vocabulary (enums), the data contracts (dataclasses),
and the model selection configuration.  Decisions here have system-wide
impact.


Decision 1: Single-source-of-truth enums used across all modules
------------------------------------------------------------------

  Decision
    Enum value sets (``CLAIM_STATUS_VALUES``, ``ISSUE_TYPE_VALUES``,
    etc.) are defined once as plain Python ``set`` objects.  Every module
    that validates or coerces these values (M3 post-processing, M4
    post-processing, M7 validation) imports and uses the same set.

  Rationale
    - A single definition prevents drift between modules.  If ``dent``
      is added to the allowed issue types, it becomes valid everywhere
      immediately.
    - Plain sets are simpler than ``enum.Enum`` subclasses: they are
      trivially serialisable, hashable, and compatible with ``set``
      operations used in validation (``value in ISSUE_TYPE_VALUES``).
    - String-based enums match the CSV representation directly, avoiding
      a conversion step when reading sample_claims.csv ground truth or
      writing output.csv.

  Trade-offs
    + Single definition prevents cross-module inconsistency.
    + Simple membership check: ``value in my_set`` is O(1) and readable.
    - Plain sets lack the type safety of ``enum.Enum``.  A typo like
      ``issu_type_values`` creates a new variable, not a compile-time
      error (mypy can catch this if the flag is enabled).
    - No automatic exhaustiveness checking.  A ``match`` statement over
      issue types cannot be verified as covering all cases.

  Limitations
    - Object parts are scoped by claim object (``OBJECT_PART_VALUES``
      is a ``Dict[str, set]``), while issue types and severity are
      global.  If the taxonomy becomes object-specific in the future
      (e.g. laptop-specific issue types), the scoping pattern would
      need to change.
    - The sets are mutable at runtime (``ISSUE_TYPE_VALUES.add("new")``
      works).  An errant module modifying the set would affect all other
      modules.  This is accepted for simplicity; using ``frozenset``
      would prevent mutation but complicate dynamic updates.

  Future Improvements
    - Replace plain sets with ``Final[frozenset]`` for immutability.
    - Consider ``StrEnum`` (Python 3.11+) for type-checker support
      while retaining string compatibility.


Decision 2: Dataclass safe defaults throughout
------------------------------------------------------------------

  Decision
    Every dataclass (``UserHistory``, ``EvidenceEvaluation``, etc.)
    defines sensible default values for all fields.  Callers never
    receive ``None`` fields — if data is missing, the default is used.

  Rationale
    - Downstream modules never check ``if x is None`` before accessing
      a field.  This eliminates an entire class of ``AttributeError``
      and ``TypeError`` bugs.
    - The safe defaults are chosen to be non-committal (``"none"`` for
      flags, ``0`` for counts, ``False`` for booleans) so they propagate
      correctly through the pipeline without creating false positives.
    - ``UserHistory(user_id="unknown")`` is valid even when the user_id
      is missing from ``user_history.csv``, and all count fields default
      to 0, which correctly represents "no history" for the risk
      aggregation thresholds.

  Trade-offs
    + Eliminates None-checks throughout the pipeline.
    + Safe defaults propagate correctly — a missing user history record
      behaves identically to a user with zero claim history.
    - A caller that forgets to populate a required field (e.g. not
      setting ``primary_issue_type`` on a ``ParsedClaim``) gets a silent
      default of ``"unknown"`` instead of a clear error.  This can mask
      programming mistakes.
    - Default ``False`` for ``evidence_standard_met`` is conservative
      (fail closed), but a bug that creates an ``EvidenceEvaluation``
      without setting the field will mark the claim as failing evidence
      requirements — a false negative.

  Limitations
    - ``VLMAnalysis`` does not have a convenient default constructor
      (the first 5 fields are required).  The
      ``SAFE_DEFAULT_VLM_ANALYSIS`` constant exists to fill this gap,
      but it is a separate constant rather than a class method.
    - The ``field(default_factory=list)`` pattern for list fields means
      each instance gets its own list (good — avoids the mutable-default
      trap).  But the per-instance allocation is slightly more expensive
      than ``None`` + lazy init.

  Future Improvements
    - Add ``@classmethod`` constructors like ``VLMAnalysis.safe_default()``
      that return the safe default, replacing the module-level constant.
    - Consider a validator (``__post_init__``) that warns if a field
      is left at its default value when it should have been explicitly set.


Decision 3: Role-based ModelSet design
------------------------------------------------------------------

  Decision
    A ``ModelSet`` is a named collection of role-tagged model IDs
    (``models: Dict[str, str]``).  Each role corresponds to a pipeline
    function (``vlm``, ``text``, ``compare``, ``fallback``).  Adding a
    new role to a set does not require any new variables or classes.

  Rationale
    - Role tagging decouples the model selection from the pipeline stage.
      M3 queries ``model_set.get("text")``, M4 queries
      ``model_set.get("vlm")`` — each module picks the right model by
      its function, not its name.
    - Adding a new model for a specific stage (e.g. an OCR model) is a
      one-line addition to each model set: ``"ocr": "google/gemini-flash-lite"``.
      No new top-level variables, no new classes.
    - The three-tier structure (budget / balanced / premium) lets
      participants trade off cost vs accuracy without touching code.
      The evaluation pipeline (M8) compares all three automatically.

  Trade-offs
    + Extensible: adding a new role is a dict update per set, not a new
      variable.
    + Self-documenting: a model set is a flat dict with role keys that
      describe what each model is for.
    - The role key string is the contract between modules and model sets.
      If a module looks up a role that doesn't exist (e.g.
      ``model_set.get("ocr")`` when no ``ocr`` role is defined), it
      raises ``KeyError``.  Callers must handle this gracefully (most
      do, falling back to a default model).
    - The model set cannot have two models for the same role (e.g. for
      ensemble or voting).  Each role maps to exactly one model ID.

  Limitations
    - The ``compare`` role is only used by M8 (evaluation pipeline).
      It exists in the model sets but is unused in production.  This is
      a minor maintenance tax.
    - The ``fallback`` role is resolved by ``llm_client.get_fallback_model()``.
      If a model set omits the ``fallback`` role, the global
      ``FALLBACK_BY_ROLE`` dict is used.  This fallback chain (set →
      global dict → hardcoded) is complex but resilient.

  Future Improvements
    - Consider merging ``FALLBACK_BY_ROLE`` into the ``ModelSet`` as a
      required field so every set explicitly declares its fallback model.
    - Support multiple models per role for ensemble voting (requires a
      corresponding change in the calling modules).


Decision 4: MANIPULATION_FLAGS as an importable constant (cross-module coupling)
----------------------------------------------------------------------------------

  Decision
    ``MANIPULATION_FLAGS`` is defined here (not in M6, where it is used)
    because it is referenced by multiple modules: M6 (risk aggregator)
    checks it for the "always add manual_review_required" rule, and M4
    (VLM engine) should ideally list manipulation flags in its prompt.

  Rationale
    - Placing it in ``models.py`` ensures it is equally accessible to
      all modules without circular imports or duplication.
    - It serves as a documented contract: any VLM prompt author knows
      exactly which flags are considered "manipulation" and must be
      added to ``MANIPULATION_FLAGS`` if a new one is introduced.
    - The separation is clean: enums live in the shared model layer;
      logic that uses them lives in the module layer.

  Trade-offs
    + Easy to find and update: one dict, one location.
    + All modules have equal access without import gymnastics.
    - No enforcement: if a new manipulation flag is added to the VLM
      prompt but forgotten in ``MANIPULATION_FLAGS``, the aggregator
      silently does not escalate it.  This is a runtime error, not a
      compile-time error.
    - The constant is a set literal, not encapsulated in any class.
      A future refactoring could move it into a class, but that would
      break all existing imports.

  Limitations
    - The set is manually maintained and there is no test that checks
      consistency between ``MANIPULATION_FLAGS``, ``RISK_FLAG_VALUES``,
      and the VLM prompt's flag list.
    - ``MANIPULATION_FLAGS`` is a subset of ``RISK_FLAG_VALUES``.  There
      is no automated verification that every flag in
      ``MANIPULATION_FLAGS`` is also in ``RISK_FLAG_VALUES``.

  Future Improvements
    - Add an assertion at import time that ``MANIPULATION_FLAGS`` is a
      subset of ``RISK_FLAG_VALUES``.
    - Add a test that loads each VLM prompt variant and checks that any
      manipulation flag mentioned in the prompt text exists in
      ``MANIPULATION_FLAGS``.


Decision 5: FALLBACK_BY_ROLE — universal fallback model selection
------------------------------------------------------------------

  Decision
    ``FALLBACK_BY_ROLE`` maps each role to a fallback model ID
    (all point to ``openai/gpt-4o-mini``).  ``llm_client.get_fallback_
    model()`` uses this dict when a ``ModelSet`` does not provide an
    explicit ``fallback`` role.

  Rationale
    - ``gpt-4o-mini`` is available on almost every OpenRouter provider,
      has the widest model availability across providers, and is vision-
      capable (can serve as VLM or text fallback).
    - A single fallback model simplifies reasoning: "if the primary
      model fails, use gpt-4o-mini."
    - The role-keyed dict allows per-role fallback in the future (e.g.
      VLM fallback could use a different model than text fallback)
      without changing the caller API.

  Trade-offs
    + Universal coverage: gpt-4o-mini works for text and VLM roles.
    + Simple mental model: one fallback for everything.
    - A fallback model that handles both text and images may not be
      optimal for either.  A specialised text fallback (e.g.
      ``gpt-4o-mini``) and VLM fallback (e.g. ``gemini-2.5-flash``)
      could provide better resilience.
    - If gpt-4o-mini is also the primary model for the ``budget`` set,
      the fallback offers no architectural diversity — it is the same
      model, just a retry with fresh state.

  Limitations
    - The fallback model is hardcoded.  If gpt-4o-mini experiences a
      prolonged outage on OpenRouter, the entire pipeline loses its
      fallback safety net.
    - The fallback model is unconditionally cheaper than the primary for
      ``premium`` (gpt-4o-mini vs gpt-4o) but may be more expensive
      than the primary for ``budget`` (both gpt-4o-mini, same cost).
      The cost impact is asymmetric.

  Future Improvements
    - Make the fallback model configurable per model set, eliminating
      the need for ``FALLBACK_BY_ROLE``.
    - Consider a fallback chain (try model B, then model C) rather than
      a single fallback.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Allowed Value Sets (for validation at module boundaries) ──────────────

CLAIM_STATUS_VALUES = {
    "supported",
    "contradicted",
    "not_enough_information",
}

ISSUE_TYPE_VALUES = {
    "dent",
    "scratch",
    "crack",
    "glass_shatter",
    "broken_part",
    "missing_part",
    "torn_packaging",
    "crushed_packaging",
    "water_damage",
    "stain",
    "none",
    "unknown",
}

SEVERITY_VALUES = {
    "none",
    "low",
    "medium",
    "high",
    "unknown",
}

OBJECT_PART_VALUES: Dict[str, set] = {
    "car": {
        "front_bumper",
        "rear_bumper",
        "door",
        "hood",
        "windshield",
        "side_mirror",
        "headlight",
        "taillight",
        "fender",
        "quarter_panel",
        "body",
        "unknown",
    },
    "laptop": {
        "screen",
        "keyboard",
        "trackpad",
        "hinge",
        "lid",
        "corner",
        "port",
        "base",
        "body",
        "unknown",
    },
    "package": {
        "box",
        "package_corner",
        "package_side",
        "seal",
        "label",
        "contents",
        "item",
        "unknown",
    },
}

RISK_FLAG_VALUES = {
    "none",
    "blurry_image",
    "cropped_or_obstructed",
    "low_light_or_glare",
    "wrong_angle",
    "wrong_object",
    "wrong_object_part",
    "damage_not_visible",
    "claim_mismatch",
    "possible_manipulation",
    "non_original_image",
    "text_instruction_present",
    "user_history_risk",
    "manual_review_required",
}

MANIPULATION_FLAGS = {
    "possible_manipulation",
    "non_original_image",
    "text_instruction_present",
}

CLAIM_OBJECT_VALUES = {"car", "laptop", "package"}

LANGUAGE_VALUES = {"en", "hi", "mixed", "other"}


# ── Dataclasses ───────────────────────────────────────────────────────────

@dataclass
class UserHistory:
    """User risk context loaded from ``user_history.csv``.

    Every field has a safe default so callers never deal with ``None``.
    """

    past_claim_count: int = 0
    accept_claim: int = 0
    manual_review_claim: int = 0
    rejected_claim: int = 0
    last_90_days_claim_count: int = 0
    history_flags: str = "none"
    history_summary: str = ""


@dataclass
class EvidenceRule:
    """One row from ``evidence_requirements.csv``."""

    requirement_id: str
    claim_object: str
    applies_to: str
    minimum_image_evidence: str


@dataclass
class ClaimContext:
    """Full context for one claim row — the output of M1 (Data Ingestion).

    All downstream modules (M2–M7) receive this object, so keep it stable.
    """

    user_id: str
    image_paths: List[str]  # resolved absolute local paths
    image_ids: List[str]  # filename stems, e.g. ["img_1", "img_2"]
    user_claim: str  # raw claim transcript
    claim_object: str  # "car" | "laptop" | "package"
    user_history: UserHistory  # safe-defaulted if missing from history file
    evidence_rules: List[EvidenceRule]  # filtered to this claim_object + "all"


@dataclass
class ImageValidationResult:
    """Output of M2 (Image Validator) — per claim, not per image."""

    valid_image: bool
    structural_flags: List[str]  # e.g. "file_missing", "unsupported_format"
    images_b64: Dict[str, str]  # image_id → base64-encoded JPEG/PNG data


@dataclass
class ParsedClaim:
    """Output of M3 (Claim Parser) — structured interpretation of the claim conversation."""

    primary_issue_type: str
    primary_object_part: str
    secondary_parts: List[str] = field(default_factory=list)
    damage_description: str = ""
    language_detected: str = "en"


@dataclass
class EvidenceEvaluation:
    """Output of M5 (Evidence Evaluator) — whether the image set meets evidence standards."""

    applicable_rules: List[EvidenceRule] = field(default_factory=list)
    evidence_standard_met: bool = False
    evidence_standard_met_reason: str = ""


@dataclass
class ModelSet:
    """A named set of role-tagged models.

    Each role corresponds to a pipeline stage (e.g. ``vlm``, ``compare``).
    Add new roles by extending the ``models`` dict — no new variables needed.
    """

    name: str
    models: Dict[str, str]
    temperature: float = 0.0
    max_tokens: int = 1024

    def get(self, role: str, default: Optional[str] = None) -> str:
        """Return the model ID for *role*.

        Raises ``KeyError`` if *role* is missing and no *default* is given.
        """
        if role in self.models:
            return self.models[role]
        if default is not None:
            return default
        raise KeyError(f"ModelSet '{self.name}' has no role '{role}'")


# ── Model Sets ────────────────────────────────────────────────────────────

MODEL_SETS: Dict[str, ModelSet] = {
    "budget": ModelSet(
        name="budget",
        models={
            "vlm": "google/gemini-2.5-flash",
            "compare": "openai/gpt-4o-mini",
            "text": "openai/gpt-4o-mini",
            "fallback": "openai/gpt-4o-mini",
        },
    ),
    "balanced": ModelSet(
        name="balanced",
        models={
            "vlm": "google/gemini-2.5-pro",
            "compare": "anthropic/claude-3.5-haiku",
            "text": "openai/gpt-4o-mini",
            "fallback": "openai/gpt-4o-mini",
        },
    ),
    "premium": ModelSet(
        name="premium",
        models={
            "vlm": "openai/gpt-4o",
            "compare": "anthropic/claude-sonnet-4-5",
            "text": "openai/gpt-4o",
            "fallback": "openai/gpt-4o-mini",
        },
    ),
}

#: Fallback models by role (used when a model set's fallback role is missing).
#: ``openai/gpt-4o-mini`` is chosen as the universal fallback because it has
#: the highest availability across OpenRouter providers, is vision-capable,
#: and has the lowest rate-limit contention.
FALLBACK_BY_ROLE: Dict[str, str] = {
    "vlm": "openai/gpt-4o-mini",
    "text": "openai/gpt-4o-mini",
    "compare": "openai/gpt-4o-mini",
}


@dataclass
class VLMAnalysis:
    """Output of M5 (VLM Engine) — the model's visual verdict on one claim."""

    object_part: str
    claim_status: str
    claim_status_justification: str
    supporting_image_ids: str  # semicolon-separated, or "none"
    severity: str
    valid_image: bool
    issue_type: str = "unknown"  # visible issue type from VLM analysis
    image_risk_flags: List[str] = field(default_factory=list)
    raw_response: str = ""


#: Safe default returned when VLM analysis cannot be performed (e.g. no valid
#: images, or all retries exhausted).
SAFE_DEFAULT_VLM_ANALYSIS: VLMAnalysis = VLMAnalysis(
    object_part="unknown",
    claim_status="not_enough_information",
    claim_status_justification="Images could not be analyzed.",
    supporting_image_ids="none",
    severity="unknown",
    valid_image=False,
    issue_type="unknown",
    image_risk_flags=[],
    raw_response="",
)


@dataclass
class PromptVariant:
    """One variant in a ``PromptSet`` — a specific prompt formulation to test.

    Used during M8 evaluation to compare different prompt phrasings and select
    the best-performing one for production.
    """

    name: str  # e.g. "concise", "detailed", "structured"
    system_prompt: str
    user_prompt: str
    temperature: float = 0.0
    max_tokens: int = 1024


@dataclass
class PromptSet:
    """A set of prompt variants for the same LLM call.

    Lives in ``evaluation/`` not ``modules/`` because it is an M8 calibration
    tool.  Production modules use a single prompt; the evaluation pipeline
    uses PromptSet to compare variants and select the best.

    Selection strategies:
      - ``first_valid_json``: call sequentially, return first that parses as JSON
      - ``longest_valid``: call all, return variant with most non-empty JSON fields
      - ``majority_vote``: call all, return most common JSON output
    """

    name: str  # e.g. "claim-parser", "vlm-analysis"
    variants: List[PromptVariant]
    selection_strategy: str = "first_valid_json"


@dataclass
class PromptSetResult:
    """Result of evaluating all variants in a ``PromptSet``.

    Used by the evaluation pipeline (M8) to compare prompt variants and
    select the best performer.
    """

    selected_variant: str
    all_results: List[dict]
    best_result: dict
    selection_reason: str
    total_cost: float = 0.0
    total_latency: float = 0.0
