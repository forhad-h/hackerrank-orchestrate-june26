# HackerRank Orchestrate — Project Source of Truth

> **Challenge deadline:** 2026-06-20 11:00 IST (UTC+5:30)  
> **Submission:** code.zip + output.csv + chat_transcript (log.txt)
>
> **Model Sets (pick after eval):**
>
> Each set is a named team of role-tagged models. Add a new role key to the set to introduce a model for a specific task.
>
> | Set | `vlm` (visual analysis) | `compare` (eval only) |
> |---|---|---|
> | **budget** | `google/gemini-2.5-flash` | `openai/gpt-4o-mini` |
> | **balanced** | `google/gemini-2.5-pro` | `anthropic/claude-3.5-haiku` |
> | **premium** | `openai/gpt-4o` | `anthropic/claude-sonnet-4-5` |
>
> All models accessed via **OpenRouter** (`https://openrouter.ai/api/v1`, OpenAI-compatible).  
> API key: env var `OPENROUTER_API_KEY`

---

## Table of Contents

1. [Repo Layout](#1-repo-layout)
2. [Environment Setup](#2-environment-setup)
3. [Architecture — 9 Modules](#3-architecture--9-modules)
4. [Build Order & Checklist](#4-build-order--checklist)
5. [Module Contracts](#5-module-contracts)
6. [Output Schema (exact)](#6-output-schema-exact)
7. [Allowed Enum Values](#7-allowed-enum-values)
8. [Evidence Requirements Reference](#8-evidence-requirements-reference)
9. [Risk Flag Logic](#9-risk-flag-logic)
10. [User History Risk Rules](#10-user-history-risk-rules)
11. [Multi-Part Claim Handling](#11-multi-part-claim-handling)
12. [Evaluation Requirements](#12-evaluation-requirements)
13. [Submission Checklist](#13-submission-checklist)

---

## 1. Repo Layout

```text
.
├── AGENTS.md
├── problem_statement.md
├── README.md
├── code/                          ← ALL your code lives here
│   ├── SPEC.md                    ← this file (source of truth)
│   ├── README.md                  ← user-facing setup & run guide (write last)
│   ├── main.py                    ← CLI entry point
│   ├── modules/
│   │   ├── __init__.py
│   │   ├── data_loader.py         ← M1
│   │   ├── image_validator.py     ← M2
│   │   ├── claim_parser.py        ← M3
│   │   ├── vlm_engine.py          ← M4
│   │   ├── evidence_evaluator.py  ← M5
│   │   ├── risk_aggregator.py     ← M6
│   │   ├── output_assembler.py    ← M7
│   │   └── models.py              ← shared dataclasses & enums
│   └── evaluation/
│       ├── main.py                ← M8 evaluation runner
│       └── evaluation_report.md  ← written by eval pipeline
├── dataset/
│   ├── claims.csv                 ← 44 test rows (no labels)
│   ├── sample_claims.csv          ← 20 labeled rows (ground truth)
│   ├── user_history.csv
│   ├── evidence_requirements.csv
│   └── images/
│       ├── sample/               ← images for sample_claims.csv
│       └── test/                 ← images for claims.csv
└── output.csv                    ← final predictions (written to repo root)
```

---

## 2. Environment Setup

```bash
# .env  (never commit this file)
OPENROUTER_API_KEY=sk-or-...
```

```bash
pip install openai pillow pandas python-dotenv tqdm
```

Run solution:

```bash
cd code
python main.py                                      # uses dataset/claims.csv → ../output.csv, model-set premium
python main.py --model-set budget                   # budget run
python main.py --input dataset/claims.csv --output output.csv --model-set balanced
```

Run evaluation:

```bash
python code/evaluation/main.py          # runs all 3 sets on sample_claims.csv
```

---

## 3. Architecture — 9 Modules

```
M1: Data Ingestion
    ↓
M2: Image Validator ──┐
M3: Claim Parser   ───┤
                      ↓
               M4: VLM Visual Analysis Engine
                   (budget / balanced / premium)
                      ↓
               M5: Evidence Evaluator
                      ↓
M6: Risk Flag Aggregator ←── (also uses M3 + user_history)
                      ↓
               M7: Output Assembler & Schema Validator
                   ↓               ↓
          M8: Evaluation       M9: Entry Point + README
              Pipeline
```

**Parallelism:** M2 and M3 can run concurrently (both depend only on M1).  
**Per-row error isolation:** If any module fails on a row, write a safe-default row and continue — never abort the batch.

---

## 4. Build Order & Checklist

### Phase 1 — Foundation

- [ ] **M1** `modules/data_loader.py` — Data Ingestion & Context Loader
- [ ] **models.py** — Shared dataclasses, enums, ModelSet

### Phase 2 — Input Processing (can build in parallel)

- [ ] **M2** `modules/image_validator.py` — Image Validity Checker
- [ ] **M3** `modules/claim_parser.py` — Claim Parser (NLP, text-only)

### Phase 3 — Evidence & Core Reasoning

- [ ] **M4** `modules/vlm_engine.py` — VLM Visual Analysis Engine
- [ ] **M5** `modules/evidence_evaluator.py` — Evidence Standard Evaluator

### Phase 4 — Output

- [ ] **M6** `modules/risk_aggregator.py` — Risk Flag Aggregator
- [ ] **M7** `modules/output_assembler.py` — Output Assembler & Schema Validator

### Phase 5 — Entry Points & Evaluation

- [ ] **M8** `evaluation/main.py` + `evaluation_report.md` — Evaluation Pipeline
- [ ] **M9** `main.py` + `README.md` — Entry Point & Docs

---

## 5. Module Contracts

### M1 — Data Ingestion (`modules/data_loader.py`)

**Input:** file paths (local CSV files + local image directory)
**Output:** `List[ClaimContext]`

#### Scope — local files only

This system assumes **all inputs are locally available on disk** before the
pipeline runs. The data loader does *not* fetch remote resources:

- HTTP/HTTPS image URLs are **not** fetched. If an `image_path` column contains
  a URL, it is treated as an unresolvable path and flagged as `file_missing`.
- No S3, GCS, or other object-storage downloads.
- No API calls to retrieve claim records or user history.

How the dataset was originally produced (scraping, a third-party service,
another pipeline, synthetic generation, etc.) is irrelevant to this system.
The only contract is: **CSVs and images are present locally in `dataset/`
before `main.py` is invoked.**

#### Extensibility

`DataLoader` is designed as an **abstract base** so future input variants can
be added without touching downstream modules:

```python
class DataLoader(ABC):
    """Abstract base — subclass to support different input sources."""

    @abstractmethod
    def load(self) -> List[ClaimContext]:
        """Return a fully-hydrated list of ClaimContext objects."""
        ...
```

The concrete implementation shipped here is `LocalCSVDataLoader`, which reads
from local CSV files and a local image directory. To add a new source (e.g.
a database, an API, or a cloud bucket) implement a new subclass of `DataLoader`
and return the same `List[ClaimContext]` — no other module needs to change.

#### `ClaimContext` dataclass

```python
@dataclass
class ClaimContext:
    user_id: str
    image_paths: List[str]          # resolved absolute local paths
    image_ids: List[str]            # filename stem, e.g. "img_1"
    user_claim: str                 # raw claim transcript
    claim_object: str               # "car" | "laptop" | "package"
    user_history: UserHistory       # safe-defaulted if missing
    evidence_rules: List[EvidenceRule]  # filtered to this claim_object
```

Key rules:
- Resolve image paths relative to **repo root** (parent of `code/`), not CWD.
- Parse `image_paths` by splitting on `;` and stripping whitespace.
- Reject any entry that looks like a URL scheme (`http://`, `https://`, `s3://`,
  etc.) — log a warning and mark it `file_missing`; do not attempt a download.
- If `user_id` not in `user_history.csv`, return `UserHistory` with all
  counts=0, `history_flags="none"`.
- Filter evidence rules to `claim_object` + `"all"` rows.

---

### M2 — Image Validator (`modules/image_validator.py`)

**Input:** `ClaimContext`  
**Output:** `ImageValidationResult` (per claim, not per image)

```python
@dataclass
class ImageValidationResult:
    valid_image: bool               # True if ≥1 image is structurally usable
    structural_flags: List[str]     # file_missing, unsupported_format
    images_b64: Dict[str, str]      # image_id → base64 string (for VLM)
```

Key rules:
- Check file existence; add `"file_missing"` structural flag if absent.
- Supported formats: `.jpg`, `.jpeg`, `.png`, `.webp`.
- Base64-encode each valid image (RGB, resize to max 1568px on longest edge to control tokens).
- `valid_image=False` only if **all** images are missing/invalid — a mixed set is still usable.
- VLM-assessed quality flags (`blurry_image`, `low_light_or_glare`, etc.) are returned by M5, not here.

---

### M3 — Claim Parser (`modules/claim_parser.py`)

**Input:** `ClaimContext` (uses `user_claim`, `claim_object`)  
**Output:** `ParsedClaim`

```python
@dataclass
class ParsedClaim:
    primary_issue_type: str         # from allowed issue_type enum
    primary_object_part: str        # from allowed object_part enum for claim_object
    secondary_parts: List[str]      # other parts mentioned (for justification coverage)
    damage_description: str         # 1-2 sentence plain-English summary
    language_detected: str          # e.g. "en", "hi", "mixed"
```

Key rules:
- Use text-only LLM call (cheapest available tier). No image needed here.
- System prompt must explicitly say: "Respond in English regardless of input language."
- Output must be **strict JSON** with `response_format={"type": "json_object"}`.
- If LLM returns a value not in the allowed enum, coerce to `"unknown"`.
- Prompt must list ALL allowed values for `issue_type` and `object_part` as the only valid choices.

---

### M4 — VLM Visual Analysis Engine (`modules/vlm_engine.py`)

**Input:** `ClaimContext`, `ImageValidationResult`, `ParsedClaim`, `ModelSet`  
**Output:** `VLMAnalysis`

```python
@dataclass
class ModelSet:
    name: str                       # "budget" | "balanced" | "premium"
    models: Dict[str, str]          # role → OpenRouter model ID
    temperature: float = 0.0
    max_tokens: int = 1024

    def get(self, role: str) -> str:
        """Return model ID for the given role. Raises KeyError if role not in set."""
        return self.models[role]
```

Defined roles (extend freely without creating new top-level variables):
- `vlm` — visual analysis, used by M4
- `compare` — secondary model for evaluation comparison (M8 only)

```python
MODEL_SETS: Dict[str, ModelSet] = {
    "budget": ModelSet(
        name="budget",
        models={
            "vlm":     "google/gemini-2.5-flash",
            "compare": "openai/gpt-4o-mini",
        },
    ),
    "balanced": ModelSet(
        name="balanced",
        models={
            "vlm":     "google/gemini-2.5-pro",
            "compare": "anthropic/claude-3.5-haiku",
        },
    ),
    "premium": ModelSet(
        name="premium",
        models={
            "vlm":     "openai/gpt-4o",
            "compare": "anthropic/claude-sonnet-4-5",
        },
    ),
}
```

To add a new model for a specific task (e.g. an OCR step), add a new role key to each set:
```python
"budget": ModelSet(..., models={"vlm": "...", "compare": "...", "ocr": "google/gemini-flash-lite"}),
```

```python
@dataclass
class VLMAnalysis:
    object_part: str                # primary part only
    claim_status: str               # "supported" | "contradicted" | "not_enough_information"
    claim_status_justification: str # covers ALL claimed parts (primary + secondary)
    supporting_image_ids: str       # semicolons, or "none"
    severity: str
    valid_image: bool
    image_risk_flags: List[str]     # VLM-assessed quality/authenticity flags
    raw_response: str               # for debugging
```

Key rules:
- Use `openai` Python SDK with `base_url="https://openrouter.ai/api/v1"`.
- Pass all images in a single message (`role: user`, content array with text + image_url blocks).
- **Prompt structure** (in order):
  1. Role: "You are an expert damage claim assessor..."
  2. Claim context: object type, parsed damage description, ALL claimed parts
  3. Task: assess each image, then give final structured visual verdict
  4. Multi-part instruction: "For `claim_status_justification`, assess each claimed part separately, then give an overall verdict."
  5. Output format: strict JSON schema (list every field, list every allowed value)
- `response_format={"type": "json_object"}` where supported; fallback: parse JSON from markdown block.
- Retry strategy: max 3 attempts, exponential backoff (2s, 4s, 8s), on `RateLimitError` or `APIStatusError(5xx)`.
- If `valid_image=False` (all images unusable): skip VLM call, return safe defaults immediately.
- This module does **not** make the `evidence_standard_met` decision — that is M5's job after reviewing the visual findings against evidence rules.

---

### M5 — Evidence Evaluator (`modules/evidence_evaluator.py`)

**Input:** `VLMAnalysis`, `ParsedClaim`, `ClaimContext.evidence_rules`  
**Output:** `EvidenceEvaluation`

```python
@dataclass
class EvidenceEvaluation:
    applicable_rules: List[EvidenceRule]   # matched REQ rows
    evidence_standard_met: bool
    evidence_standard_met_reason: str
```

Key rules:
- Match rules where `claim_object` is `claim_context.claim_object` OR `"all"`.
- Further filter by checking if `applies_to` text overlaps with `primary_issue_type` keywords.
- When no specific rule matches, always include the two universal rules:
  `REQ_GENERAL_OBJECT_PART` and `REQ_REVIEW_TRUST`.
- Evaluate `evidence_standard_met` by comparing `VLMAnalysis` findings (claim_status, supporting_image_ids, image_risk_flags) against each applicable rule's requirements.
- Set `evidence_standard_met=False` if any of: `valid_image=False`, `claim_status="contradicted"`, or required image types are absent per the matched rules.

---

### M6 — Risk Flag Aggregator (`modules/risk_aggregator.py`)

**Input:** `VLMAnalysis.image_risk_flags`, `ParsedClaim`, `ClaimContext.user_history`  
**Output:** `str` — final semicolon-joined flags or `"none"`

**Risk flag sources and rules:**

| Source | Flag | Trigger |
|---|---|---|
| M2 structural | `blurry_image`, `cropped_or_obstructed`, `low_light_or_glare`, `wrong_angle` | VLM image quality assessment |
| M4 VLM | `wrong_object`, `wrong_object_part`, `damage_not_visible`, `claim_mismatch` | VLM visual reasoning |
| M4 VLM | `possible_manipulation`, `non_original_image`, `text_instruction_present` | VLM authenticity check |
| User history | `user_history_risk` | `rejected_claim >= 2` OR `last_90_days_claim_count >= 3` |
| User history | `manual_review_required` | `history_flags != "none"` OR `manual_review_claim >= 2` OR any manipulation flag present |

Rules:
- Deduplicate flags (use ordered set).
- Sort alphabetically for determinism.
- If any manipulation flag is present, always also add `manual_review_required`.
- Return `"none"` if the final set is empty.

---

### M7 — Output Assembler (`modules/output_assembler.py`)

**Input:** All module outputs for one row  
**Output:** `dict` with exact schema, ready to write to CSV

Key rules:
- Column order is **fixed** — see §6.
- Validate every value against allowed enums before writing (see §7).
- Invalid enum → coerce: prefer `"unknown"` for types/parts, `"none"` for flags/severity, `"not_enough_information"` for claim_status.
- **Safe default row** (used on any unhandled exception):

```python
SAFE_DEFAULT = {
    "evidence_standard_met": "false",
    "evidence_standard_met_reason": "Processing error",
    "risk_flags": "manual_review_required",
    "issue_type": "unknown",
    "object_part": "unknown",
    "claim_status": "not_enough_information",
    "claim_status_justification": "Automated processing failed; manual review required.",
    "supporting_image_ids": "none",
    "valid_image": "false",
    "severity": "unknown",
}
```

- Write `output.csv` with `quoting=csv.QUOTE_ALL` to handle commas in justification text.

---

### M8 — Evaluation Pipeline (`evaluation/main.py`)

Runs all model sets (all roles per set) against `sample_claims.csv`. For each set, uses the `vlm` role for primary analysis and the `compare` role for secondary comparison.

**Metrics per config:**

| Field | Metric |
|---|---|
| `claim_status` | Accuracy (exact match) |
| `evidence_standard_met` | Accuracy |
| `issue_type` | Accuracy (exact match) |
| `severity` | Accuracy + adjacent tolerance (e.g. low↔medium counts as partial) |
| `valid_image` | Accuracy |
| `risk_flags` | Jaccard similarity (set overlap) |

**Operational metrics per config:** total API calls, total input tokens, total output tokens, total images, wall-clock time, estimated cost (use OpenRouter pricing).

**Output:** `evaluation/evaluation_report.md` — see §12.

---

### M9 — Entry Point (`main.py`)

```bash
python code/main.py [--input PATH] [--output PATH] [--model-set {budget,balanced,premium}]
```

Defaults: `--input dataset/claims.csv`, `--output output.csv`, `--model-set premium`

Uses `tqdm` progress bar. Prints per-row errors to stderr without stopping. Reports summary at end (rows processed, errors, model set used, approx cost).

---

## 6. Output Schema (exact)

Column order is **mandatory**. Any deviation corrupts automated evaluation.

```
user_id, image_paths, user_claim, claim_object,
evidence_standard_met, evidence_standard_met_reason,
risk_flags, issue_type, object_part,
claim_status, claim_status_justification,
supporting_image_ids, valid_image, severity
```

Types:
- `evidence_standard_met`, `valid_image` → lowercase string `"true"` / `"false"` (not boolean)
- `risk_flags`, `supporting_image_ids` → semicolons, no spaces; `"none"` if empty
- All other fields → string from allowed enum

---

## 7. Allowed Enum Values

**`claim_status`**  
`supported` · `contradicted` · `not_enough_information`

**`issue_type`**  
`dent` · `scratch` · `crack` · `glass_shatter` · `broken_part` · `missing_part` · `torn_packaging` · `crushed_packaging` · `water_damage` · `stain` · `none` · `unknown`

**`severity`**  
`none` · `low` · `medium` · `high` · `unknown`

**`object_part` — car**  
`front_bumper` · `rear_bumper` · `door` · `hood` · `windshield` · `side_mirror` · `headlight` · `taillight` · `fender` · `quarter_panel` · `body` · `unknown`

**`object_part` — laptop**  
`screen` · `keyboard` · `trackpad` · `hinge` · `lid` · `corner` · `port` · `base` · `body` · `unknown`

**`object_part` — package**  
`box` · `package_corner` · `package_side` · `seal` · `label` · `contents` · `item` · `unknown`

**`risk_flags`**  
`none` · `blurry_image` · `cropped_or_obstructed` · `low_light_or_glare` · `wrong_angle` · `wrong_object` · `wrong_object_part` · `damage_not_visible` · `claim_mismatch` · `possible_manipulation` · `non_original_image` · `text_instruction_present` · `user_history_risk` · `manual_review_required`

---

## 8. Evidence Requirements Reference

| requirement_id | claim_object | applies_to | minimum_image_evidence |
|---|---|---|---|
| REQ_GENERAL_OBJECT_PART | all | general claim review | Claimed object and relevant part visible clearly enough to inspect condition. |
| REQ_GENERAL_MULTI_IMAGE | all | multi-image rows | Each image considered separately; ≥1 must show claimed object/part clearly. |
| REQ_CAR_BODY_PANEL | car | dent or scratch | Claimed panel/bumper visible from angle to assess surface marks or deformation. |
| REQ_CAR_GLASS_LIGHT_MIRROR | car | crack, broken, or missing part | Claimed glass/light/mirror visible clearly to inspect cracks, breakage, or missing parts. |
| REQ_CAR_IDENTITY_OR_SIDE | car | vehicle identity or orientation | Image set shows enough context to match claimed vehicle and part. |
| REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD | laptop | screen, keyboard, or trackpad | Claimed area visible clearly to inspect cracks, stains, missing keys, or surface damage. |
| REQ_LAPTOP_BODY_HINGE_PORT | laptop | hinge, lid, corner, body, or port | Claimed part visible with enough context to identify it. |
| REQ_PACKAGE_EXTERIOR | package | crushed, torn, or seal damage | Package exterior and claimed side/corner/flap/seal visible clearly. |
| REQ_PACKAGE_LABEL_OR_STAIN | package | water, stain, or label damage | Affected surface or label visible to assess stain/water/label damage. |
| REQ_PACKAGE_CONTENTS | package | contents or inner item | Opened package and contents visible to assess missing or damaged items. |
| REQ_REVIEW_TRUST | all | reviewability | Images are usable, relevant to claim, and grounded in claimed object. |

---

## 9. Risk Flag Logic

**Priority rules (applied after VLM returns flags):**

1. If VLM sets `possible_manipulation` or `non_original_image` → always also add `manual_review_required`.
2. If all images are structurally invalid (M2) → set `valid_image=false`, add `damage_not_visible`, short-circuit M5.
3. Flags are deduplicated using ordered set, then sorted alphabetically.
4. Final output: semicolons with no spaces. If set is empty → `"none"`.

**VLM prompt must ask explicitly about:**
- Image quality: blurriness, lighting, obstruction, angle
- Object identity: is this the claimed object/part?
- Authenticity: signs of editing, screenshots, stock images, text overlays
- Claim alignment: does the visible damage match what the user described?

---

## 10. User History Risk Rules

Evaluated by M6 against `user_history.csv`:

```python
def get_history_flags(history: UserHistory) -> List[str]:
    flags = []
    if history.rejected_claim >= 2 or history.last_90_days_claim_count >= 3:
        flags.append("user_history_risk")
    if history.history_flags != "none" or history.manual_review_claim >= 2:
        flags.append("manual_review_required")
    return flags
```

> **Important:** History flags add risk context but must **not** override clear visual evidence.  
> A visually well-supported claim from a risky user still gets `claim_status=supported` — the risk flags surface the concern separately.

---

## 11. Multi-Part Claim Handling

When a user claims multiple parts (e.g., "front bumper and left headlight"):

| Field | Rule |
|---|---|
| `object_part` | Primary/most-damaged part (single enum value; schema compliant) |
| `issue_type` | Issue on the primary part |
| `claim_status` | Overall verdict across ALL parts |
| `claim_status_justification` | **Must explicitly assess each claimed part separately**, then give overall verdict. Example: *"Front bumper shows a visible dent (img_1) [supported]. Left headlight appears intact with no visible damage (img_2) [contradicted]. Overall: partially supported."* |
| `supporting_image_ids` | Include IDs relevant to any claimed part |

**Rationale:** Without per-part coverage in the justification, an adjudicator reading only `object_part` may authorize repair for only one part, leaving the user with a partial fix even for a fully-validated claim.

---

## 12. Evaluation Requirements

`evaluation/evaluation_report.md` must contain:

### 12.1 Metric Table

Per model set × role, per field:

| Model Set | Role | Model | claim_status acc | evidence_standard_met acc | issue_type acc | severity acc | valid_image acc | risk_flags Jaccard |
|---|---|---|---|---|---|---|---|---|
| budget | vlm | gemini-2.5-flash | | | | | | |
| budget | compare | gpt-4o-mini | | | | | | |
| balanced | vlm | gemini-2.5-pro | | | | | | |
| balanced | compare | claude-3.5-haiku | | | | | | |
| premium | vlm | gpt-4o | | | | | | |
| premium | compare | claude-sonnet-4-5 | | | | | | |

### 12.2 Strategy Comparison

Narrative: what worked, what didn't, surprising differences between models.

### 12.3 Selected Strategy

Which config is used for final `output.csv` and why.

### 12.4 Operational Analysis

| Metric | sample_claims.csv run | full claims.csv run (est.) |
|---|---|---|
| Model calls | | |
| Input tokens | | |
| Output tokens | | |
| Images processed | | |
| Estimated cost (USD) | | |
| Wall-clock runtime | | |
| TPM/RPM headroom | | |

Pricing assumptions:
- `google/gemini-2.5-flash`: ~$0.075/M input, $0.30/M output (via OpenRouter)
- `openai/gpt-4o`: ~$2.50/M input, $10/M output (via OpenRouter)
- Image tokens: approximately 765–1105 tokens per 512×512 tile (OpenAI formula)

### 12.5 Retry / Rate-Limit Strategy

Document: max retries, backoff timing, caching approach, parallel vs sequential per-row processing.

---

## 13. Submission Checklist

Before submitting, verify each item:

- [ ] `output.csv` exists at repo root
- [ ] `output.csv` has exactly 44 rows (one per row in `dataset/claims.csv`, excluding header)
- [ ] `output.csv` columns match exact order in §6
- [ ] All values in `output.csv` are from allowed enums (§7)
- [ ] `code/evaluation/evaluation_report.md` exists and is populated
- [ ] Evaluation compares ≥2 strategies
- [ ] `code/README.md` explains setup, env vars, and how to run
- [ ] No secrets or API keys committed to git
- [ ] `.env` is in `.gitignore`
- [ ] `$HOME/hackerrank_orchestrate/log.txt` is ready for upload as chat transcript
- [ ] code.zip built from `code/` (exclude virtualenvs, `__pycache__`, `.env`)
