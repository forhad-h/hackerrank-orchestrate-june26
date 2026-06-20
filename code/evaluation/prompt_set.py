"""
Prompt Set Evaluator — M8 calibration tool for comparing prompt variants.

Lives in ``evaluation/`` (not ``modules/``) because it is an evaluation-pipeline
concern, not a production runtime component.  Production modules use a single
prompt; this module compares multiple variants and selects the best one.

Usage
-----
    from evaluation.prompt_set import evaluate_prompt_set
    from modules.models import PromptSet, PromptVariant

    prompt_set = PromptSet(
        name="claim-parser",
        variants=[
            PromptVariant(name="concise", system_prompt="...", user_prompt="..."),
            PromptVariant(name="detailed", system_prompt="...", user_prompt="..."),
        ],
        selection_strategy="first_valid_json",
    )

    result = evaluate_prompt_set(prompt_set, model="openai/gpt-4o-mini")
    print(result.selected_variant)   # "concise"
    print(result.best_result)         # parsed JSON from the winning variant
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from modules.llm_client import call_llm, extract_json_from_markdown, get_fallback_model
from modules.models import ModelSet, PromptSet, PromptSetResult
from modules.prompt_guard import sanitize_prompt
from modules.token_tracker import token_tracker

logger = logging.getLogger(__name__)


def evaluate_prompt_set(
    prompt_set: PromptSet,
    model: str,
    model_set: Optional[ModelSet] = None,
    images_b64: Optional[Dict[str, str]] = None,
    module_name: str = "",
    max_retries_per_variant: int = 1,
) -> PromptSetResult:
    """Evaluate all variants in a ``PromptSet`` and select the best result.

    Parameters
    ----------
    prompt_set :
        The set of prompt variants to evaluate.
    model :
        OpenRouter model ID to use for all variants.
    model_set :
        Optional ``ModelSet`` for fallback model resolution.
    images_b64 :
        Optional base64-encoded images (for VLM prompt variants).
    module_name :
        Module identifier for tracking (e.g. "M3", "M4").
    max_retries_per_variant :
        Maximum retries per variant (default 1 — minimal; PromptSet is already
        expensive since it runs multiple variants).

    Returns
    -------
    PromptSetResult
        The selected variant, all results, and selection reason.
    """
    if not prompt_set.variants:
        raise ValueError(f"PromptSet '{prompt_set.name}' has no variants")

    all_results: List[Dict[str, Any]] = []
    best_result: Dict[str, Any] = {}
    selected_variant = prompt_set.variants[0].name if prompt_set.variants else ""
    selection_reason = ""

    # Track aggregated usage across all variants
    total_cost = 0.0
    total_latency = 0.0

    for variant in prompt_set.variants:
        logger.info(
            "Evaluating variant '%s' for PromptSet '%s'",
            variant.name, prompt_set.name,
        )

        # Sanitize prompts for consistency with the rest of the pipeline.
        # System prompts are pre-authored, so sanitization is belt-and-
        # suspenders, but it ensures a uniform security posture across
        # all model callers.
        sanitized_system = sanitize_prompt(
            variant.system_prompt,
            context_id=f"{prompt_set.name}/{variant.name}/system",
        )
        sanitized_user = sanitize_prompt(
            variant.user_prompt,
            context_id=f"{prompt_set.name}/{variant.name}/user",
        )

        if sanitized_system.text is None or sanitized_user.text is None:
            logger.error(
                "Prompt sanitization produced empty prompts for variant '%s' — skipping",
                variant.name,
            )
            all_results.append(variant_result)
            continue

        variant_result: Dict[str, Any] = {
            "variant_name": variant.name,
            "success": False,
            "content": "",
            "parsed_json": None,
            "usage": None,
        }

        try:
            for attempt in range(max_retries_per_variant + 1):
                llm_result = call_llm(
                    system_prompt=sanitized_system.text,
                    user_prompt=sanitized_user.text,
                    model=model,
                    model_set=model_set,
                    response_format={"type": "json_object"},
                    max_tokens=variant.max_tokens,
                    temperature=variant.temperature,
                    images_b64=images_b64,
                    module_name=f"{module_name}_promptset_{variant.name}" if module_name else "",
                )

                variant_result["content"] = llm_result.content
                variant_result["usage"] = llm_result.usage
                total_cost += llm_result.usage.cost_usd
                total_latency += llm_result.usage.latency_seconds

                # Try to parse JSON
                parsed = _try_parse_json(llm_result.content)
                if parsed is not None:
                    variant_result["success"] = True
                    variant_result["parsed_json"] = parsed
                    break

                logger.warning(
                    "Variant '%s' attempt %d: JSON parse failed, content=%.80s",
                    variant.name, attempt + 1, llm_result.content,
                )

        except Exception as e:
            logger.error(
                "Variant '%s' failed after %d attempt(s): %s",
                variant.name, max_retries_per_variant + 1, e,
            )

        all_results.append(variant_result)

    # ── Select best result based on strategy ──────────────────────────────
    if prompt_set.selection_strategy == "first_valid_json":
        selected_variant, best_result, selection_reason = _select_first_valid(
            prompt_set, all_results,
        )
    elif prompt_set.selection_strategy == "longest_valid":
        selected_variant, best_result, selection_reason = _select_longest_valid(
            prompt_set, all_results,
        )
    elif prompt_set.selection_strategy == "majority_vote":
        selected_variant, best_result, selection_reason = _select_majority_vote(
            prompt_set, all_results,
        )
    else:
        # Fallback: first valid
        selected_variant, best_result, selection_reason = _select_first_valid(
            prompt_set, all_results,
        )

    return PromptSetResult(
        selected_variant=selected_variant,
        all_results=all_results,
        best_result=best_result,
        selection_reason=selection_reason,
        total_cost=total_cost,
        total_latency=total_latency,
    )


# ── Selection Strategies ──────────────────────────────────────────────────────


def _select_first_valid(
    prompt_set: PromptSet,
    all_results: List[Dict[str, Any]],
) -> tuple[str, Dict[str, Any], str]:
    """Return the first variant that produced valid JSON."""
    for result in all_results:
        if result["success"] and result["parsed_json"]:
            return (
                result["variant_name"],
                result["parsed_json"],
                f"first_valid_json: variant '{result['variant_name']}' succeeded",
            )
    # Fallback: no variant succeeded — return first variant's raw content
    first = all_results[0]
    return (
        first["variant_name"],
        {"_raw": first.get("content", "")},
        "no_variant_produced_valid_json",
    )


def _select_longest_valid(
    prompt_set: PromptSet,
    all_results: List[Dict[str, Any]],
) -> tuple[str, Dict[str, Any], str]:
    """Return the variant with the most non-empty JSON fields."""
    best_variant = all_results[0]["variant_name"]
    best_json: Dict[str, Any] = {}
    best_count = -1
    reason = ""

    for result in all_results:
        if result["success"] and result["parsed_json"]:
            field_count = len([v for v in result["parsed_json"].values()
                              if v is not None and v != "" and v != []])
            if field_count > best_count:
                best_variant = result["variant_name"]
                best_json = result["parsed_json"]
                best_count = field_count

    if best_json:
        reason = f"longest_valid: variant '{best_variant}' with {best_count} non-empty fields"
    else:
        best_variant = all_results[0]["variant_name"]
        best_json = {"_raw": all_results[0].get("content", "")}
        reason = "no_variant_produced_valid_json"

    return best_variant, best_json, reason


def _select_majority_vote(
    prompt_set: PromptSet,
    all_results: List[Dict[str, Any]],
) -> tuple[str, Dict[str, Any], str]:
    """Return the most common JSON output among successful variants.

    Uses a simple hash of the JSON string for comparison.
    """
    from collections import Counter

    votes: Counter[str] = Counter()
    variant_map: Dict[str, str] = {}  # json_hash -> variant_name
    json_map: Dict[str, Dict[str, Any]] = {}  # json_hash -> parsed_json

    for result in all_results:
        if result["success"] and result["parsed_json"]:
            json_str = json.dumps(result["parsed_json"], sort_keys=True)
            json_hash = str(hash(json_str))
            votes[json_hash] += 1
            variant_map[json_hash] = result["variant_name"]
            json_map[json_hash] = result["parsed_json"]

    if votes:
        winner_hash = votes.most_common(1)[0][0]
        return (
            variant_map[winner_hash],
            json_map[winner_hash],
            f"majority_vote: variant '{variant_map[winner_hash]}' won with {votes[winner_hash]}/{len(all_results)} votes",
        )

    # Fallback
    first = all_results[0]
    return (
        first["variant_name"],
        {"_raw": first.get("content", "")},
        "no_variant_produced_valid_json",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _try_parse_json(content: str) -> Optional[Dict[str, Any]]:
    """Try to parse *content* as JSON, falling back to markdown extraction."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        parsed = extract_json_from_markdown(content)
        if parsed is not None:
            return parsed
    return None


# ── Exports ───────────────────────────────────────────────────────────────────

__all__ = [
    "evaluate_prompt_set",
]
