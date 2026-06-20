# Damage Claim Evidence Reviewer

A multi-modal pipeline that verifies damage claims by analysing submitted images, claim conversations, user history, and evidence requirements. For each row in `claims.csv` it produces a structured verdict: **supported**, **contradicted**, or **not_enough_information**.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [API key](#3-api-key)
4. [Dataset setup](#4-dataset-setup)
5. [Running the solution](#5-running-the-solution)
6. [Running evaluation](#6-running-evaluation)
7. [Output](#7-output)
8. [Environment variables reference](#8-environment-variables-reference)
9. [Project layout](#9-project-layout)

---

## 1. Prerequisites

- Python ≥ 3.10
- An [OpenRouter](https://openrouter.ai) API key (models are accessed via OpenRouter's OpenAI-compatible API)

---

## 2. Installation

```bash
cd code                          # the directory you are reading this from
pip install openai pillow pandas python-dotenv tqdm
```

---

## 3. API key

Create a `.env` file **inside `code/`** (never commit it):

```env
OPENROUTER_API_KEY=sk-or-...
```

The key is read at startup via `python-dotenv`. All model calls go through
`https://openrouter.ai/api/v1`.

---

## 4. Dataset setup

The system expects four CSV files and an image directory. Where they live
depends on how you are running the code.

### Scenario A — Full repository clone (default)

If you cloned the entire repo the dataset is already in the right place:

```
hackerrank-orchestrate-june26/      ← repo root
├── code/                           ← you are here
│   └── main.py
└── dataset/                        ← default dataset root
    ├── claims.csv
    ├── sample_claims.csv
    ├── user_history.csv
    ├── evidence_requirements.csv
    └── images/
        ├── sample/
        └── test/
```

No extra configuration needed — `DATASET_DIR` defaults to `../dataset/`
(one level above `code/`).

### Scenario B — Standalone `code.zip` (submission extracted without the repo)

The hackathon submission is `code.zip` (this `code/` folder) uploaded separately
from `output.csv`. The dataset is **not bundled in the zip** because:

- The evaluator already holds the canonical dataset on their side.
- Including ~GB of images would bloat the submission unnecessarily.
- `output.csv` (your final predictions) is submitted as a separate artefact, so the
  evaluator does not need to re-run the full pipeline to check results.

**If you do need to run the code from the extracted zip** (e.g. to reproduce or
test locally without the rest of the repo), copy or symlink the dataset next to
the `code/` folder so the default path works:

```bash
# After extracting code.zip:
cp -r /path/to/original/dataset ../dataset    # places it at ../dataset relative to code/
python main.py
```

Or point `DATASET_DIR` at wherever the dataset lives:

```bash
DATASET_DIR=/absolute/path/to/dataset python main.py
```

`DATASET_DIR` accepts both absolute and relative paths. See
[§8 Environment variables](#8-environment-variables-reference) for details.

---

## 5. Running the solution

All commands are run from inside the `code/` directory.

```bash
# Default run — reads dataset/claims.csv, writes ../output.csv, uses premium model set
python main.py

# Use the budget model set (faster, cheaper)
python main.py --model-set budget

# Balanced model set
python main.py --model-set balanced

# Custom paths
python main.py \
  --input  /path/to/claims.csv \
  --output /path/to/output.csv \
  --model-set balanced
```

| Model set | VLM | Secondary (eval only) | Notes |
|---|---|---|---|
| `budget` | `google/gemini-2.5-flash` | `openai/gpt-4o-mini` | Fastest, lowest cost |
| `balanced` | `google/gemini-2.5-pro` | `anthropic/claude-3.5-haiku` | Good accuracy/cost trade-off |
| `premium` | `openai/gpt-4o` | `anthropic/claude-sonnet-4-5` | Highest accuracy |

---

## 6. Running evaluation

The evaluation pipeline runs all three model sets against `sample_claims.csv`
(which ships with ground-truth labels) and writes a report.

```bash
# From the repo root
python code/evaluation/main.py

# Or from inside code/
cd code && python evaluation/main.py
```

Output: `code/evaluation/evaluation_report.md`

Metrics reported per model set: `claim_status` accuracy, `evidence_standard_met`
accuracy, `issue_type` accuracy, `severity` accuracy (with adjacent tolerance),
`valid_image` accuracy, `risk_flags` Jaccard similarity, plus operational
numbers (API calls, tokens, images processed, estimated cost, latency).

To evaluate against a different labeled dataset:

```bash
DATASET_DIR=/path/to/custom python code/evaluation/main.py
```

---

## 7. Output

`output.csv` is written to the **repo root** by default (`../output.csv`
relative to `code/`). Override with `--output`.

Required columns (in order):

| Column | Meaning |
|---|---|
| `user_id` | User from the claim row |
| `image_paths` | Original image paths from input |
| `user_claim` | Original claim transcript |
| `claim_object` | `car`, `laptop`, or `package` |
| `evidence_standard_met` | `true` / `false` |
| `evidence_standard_met_reason` | Short reason |
| `risk_flags` | Semicolon-separated flags, or `none` |
| `issue_type` | Visible issue (`dent`, `scratch`, `crack`, …) |
| `object_part` | Relevant part of the object |
| `claim_status` | `supported` / `contradicted` / `not_enough_information` |
| `claim_status_justification` | Concise image-grounded explanation |
| `supporting_image_ids` | Semicolon-separated image IDs, or `none` |
| `valid_image` | `true` / `false` |
| `severity` | `none`, `low`, `medium`, `high`, or `unknown` |

If a row fails during processing a safe-default row is written and the error
is printed to stderr — the pipeline never aborts the batch.

---

## 8. Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | OpenRouter API key for all model calls |
| `DATASET_DIR` | `../dataset` (relative to `code/`) | Root directory that contains `claims.csv`, `user_history.csv`, `evidence_requirements.csv`, and `images/`. Accepts absolute or relative paths. |

### Why `DATASET_DIR` exists as a module-level variable

`data_loader.py` resolves `DATASET_DIR` once at import time and exposes it as
a module-level constant. Every path inside the module (`claims_csv`,
`user_history_csv`, `evidence_csv`, `images_dir`) derives from that single
constant, so there is one place to change and no risk of paths drifting out of
sync.

The env variable itself exists for three concrete reasons:

1. **Testing with a small dataset** — point at a synthetic folder without
   touching the original data:
   `DATASET_DIR=/tmp/mini_dataset python main.py`

2. **Evaluation vs production** — the evaluation script can target
   `dataset/sample/` (which has ground-truth labels) while `main.py` keeps
   using `dataset/` with no code changes:
   `DATASET_DIR=dataset/sample python code/evaluation/main.py`

3. **Extracted `code.zip` or custom mount** — anyone running the code outside
   the original repo tree can set the variable once rather than editing source
   files.

---

## 9. Tests

The `tests/` directory contains unit tests for modules where pure logic is
worth guarding against regressions:

| Test file | Module | Tests |
|---|---|---|
| `test_claim_parser.py` | `claim_parser` | Pre/post-processing, prompt building, JSON extraction |
| `test_data_loader.py` | `data_loader` | CSV reading, image path resolution, security, context building |
| `test_evidence_evaluator.py` | `evidence_evaluator` | Rule matching, keyword extraction, per-rule evaluation, quality flags |
| `test_image_validator.py` | `image_validator` | Format/URL detection, structural flags, size limits, security |
| `test_output_assembler.py` | `output_assembler` | Enum validation, row assembly, CSV writing, coercion |
| `test_prompt_guard.py` | `prompt_guard` | Sanitization, injection detection, profanity, data leakage |
| `test_risk_aggregator.py` | `risk_aggregator` | Flag dedup/sorting, user history triggers, manipulation rules |
| `test_token_tracker.py` | `token_tracker` | Recording, per-model/module breakdown, reset |

**Modules intentionally skipped** (`vlm_engine`, `llm_client`):

These modules' core logic requires actual LLM/VLM calls, and unit-testing
them would mean mocking the OpenAI client — testing implementation details
rather than behaviour. The project relies on the **evaluation pipeline**
(`evaluation/main.py`) instead, which runs the full pipeline against
`sample_claims.csv` with real model calls. This is more robust and pragmatic
than integration tests that would consume API tokens without catching
meaningfully different failure modes.

---

## 10. Project layout

```text
code/
├── README.md                   ← you are here
├── SPEC.md                     ← full architectural spec & module contracts
├── main.py                     ← M9: CLI entry point
├── modules/
│   ├── __init__.py
│   ├── models.py               ← shared dataclasses & enums
│   ├── data_loader.py          ← M1: reads CSVs + resolves image paths
│   ├── image_validator.py      ← M2: structural image checks + base64 encoding
│   ├── claim_parser.py         ← M3: extracts damage claim from conversation
│   ├── vlm_engine.py           ← M4: visual analysis via VLM
│   ├── evidence_evaluator.py   ← M5: checks evidence requirements
│   ├── risk_aggregator.py      ← M6: assembles risk flags
│   └── output_assembler.py     ← M7: validates and writes output row
└── evaluation/
    ├── main.py                 ← M8: runs all model sets on sample_claims.csv
    └── evaluation_report.md   ← generated by evaluation/main.py
```

Pipeline per claim row:

```
M1 DataLoader
    ↓
M2 ImageValidator ──┐   (run concurrently)
M3 ClaimParser    ──┤
                    ↓
              M4 VLM Engine
                    ↓
              M5 Evidence Evaluator
                    ↓
              M6 Risk Aggregator
                    ↓
              M7 Output Assembler
```
