"""
modules package — re-exports all public types and classes.
"""

# ── M1: Data Loader ───────────────────────────────────────────────────────
from modules.data_loader import (
    DATASET_DIR,
    MAX_IMAGES_PER_CLAIM,
    DataLoader,
    LocalCSVDataLoader,
)

# ── M2: Image Validator ──────────────────────────────────────────────────
from modules.image_validator import (
    MAX_FILE_SIZE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_LONGEST_EDGE_PX,
    MIN_DIMENSION_PX,
    SUPPORTED_EXTENSIONS,
    validate_images,
)

# ── M3: Claim Parser ─────────────────────────────────────────────────────
from modules.claim_parser import (
    MAX_CLAIM_LENGTH,
    SAFE_DEFAULT_PARSED_CLAIM,
    TEXT_MODEL,
    log_security_summary,
    parse_claim,
)

# ── Prompt Guard ─────────────────────────────────────────────────────────
from modules.prompt_guard import (
    MAX_PROMPT_LENGTH,
    MIN_PROMPT_LENGTH,
    SanitizationResult,
    sanitize_prompt,
    strip_invisible_chars,
)

# ── LLM Client ────────────────────────────────────────────────────────────
from modules.llm_client import (
    LLMResult,
    LLMUsage,
    call_llm,
    extract_json_from_markdown,
    get_client,
    get_fallback_model,
    validate_response,
)

# ── Token Tracker ─────────────────────────────────────────────────────────
from modules.token_tracker import (
    PerCallMetrics,
    TokenSummary,
    TokenTracker,
    token_tracker,
)

# ── M4: VLM Engine ──────────────────────────────────────────────────────
from modules.vlm_engine import (
    analyze_images,
)

# ── M5: Evidence Evaluator ─────────────────────────────────────────────
from modules.evidence_evaluator import (
    evaluate,
)

# ── M6: Risk Flag Aggregator ───────────────────────────────────────────
from modules.risk_aggregator import (
    aggregate,
)

# ── M7: Output Assembler ───────────────────────────────────────────────
from modules.output_assembler import (
    OUTPUT_COLUMNS,
    SAFE_DEFAULT_ROW,
    assemble_row,
    create_safe_default_row,
    write_csv,
)

# ── Shared models / enums ─────────────────────────────────────────────────
from modules.models import (
    CLAIM_OBJECT_VALUES,
    CLAIM_STATUS_VALUES,
    ISSUE_TYPE_VALUES,
    LANGUAGE_VALUES,
    MANIPULATION_FLAGS,
    OBJECT_PART_VALUES,
    RISK_FLAG_VALUES,
    SEVERITY_VALUES,
    ClaimContext,
    EvidenceEvaluation,
    EvidenceRule,
    FALLBACK_BY_ROLE,
    ImageValidationResult,
    ModelSet,
    MODEL_SETS,
    ParsedClaim,
    PromptSet,
    PromptSetResult,
    PromptVariant,
    SAFE_DEFAULT_VLM_ANALYSIS,
    UserHistory,
    VLMAnalysis,
)

__all__ = [
    # M1: Data loader
    "DATASET_DIR",
    "MAX_IMAGES_PER_CLAIM",
    "DataLoader",
    "LocalCSVDataLoader",
    # M2: Image validator
    "MAX_FILE_SIZE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_LONGEST_EDGE_PX",
    "MIN_DIMENSION_PX",
    "SUPPORTED_EXTENSIONS",
    "validate_images",
    # M3: Claim Parser
    "MAX_CLAIM_LENGTH",
    "SAFE_DEFAULT_PARSED_CLAIM",
    "TEXT_MODEL",
    "log_security_summary",
    "parse_claim",
    # M4: VLM Engine
    "analyze_images",
    # M5: Evidence Evaluator
    "evaluate",
    # M6: Risk Aggregator
    "aggregate",
    # M7: Output Assembler
    "OUTPUT_COLUMNS",
    "SAFE_DEFAULT_ROW",
    "assemble_row",
    "create_safe_default_row",
    "write_csv",
    # Prompt Guard
    "MAX_PROMPT_LENGTH",
    "MIN_PROMPT_LENGTH",
    "SanitizationResult",
    "sanitize_prompt",
    "strip_invisible_chars",
    # LLM Client
    "LLMResult",
    "LLMUsage",
    "call_llm",
    "extract_json_from_markdown",
    "get_client",
    "get_fallback_model",
    "validate_response",
    # Token Tracker
    "PerCallMetrics",
    "TokenSummary",
    "TokenTracker",
    "token_tracker",
    # Enums
    "CLAIM_OBJECT_VALUES",
    "CLAIM_STATUS_VALUES",
    "ISSUE_TYPE_VALUES",
    "LANGUAGE_VALUES",
    "MANIPULATION_FLAGS",
    "OBJECT_PART_VALUES",
    "RISK_FLAG_VALUES",
    "SEVERITY_VALUES",
    # Model sets
    "FALLBACK_BY_ROLE",
    "ModelSet",
    "MODEL_SETS",
    "PromptSet",
    "PromptVariant",
    "SAFE_DEFAULT_VLM_ANALYSIS",
    # Dataclasses
    "ClaimContext",
    "EvidenceEvaluation",
    "EvidenceRule",
    "ImageValidationResult",
    "ParsedClaim",
    "UserHistory",
    "VLMAnalysis",
]
