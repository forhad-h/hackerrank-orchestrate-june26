"""Tests for risk_aggregator.py — M6 Risk Flag Aggregator.

Pure function logic (no I/O, no API calls), fully deterministic.
"""
import pytest
from modules.risk_aggregator import aggregate
from modules.models import (
    MANIPULATION_FLAGS,
    ParsedClaim,
    UserHistory,
    VLMAnalysis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_vlm(risk_flags=None, claim_status="supported"):
    return VLMAnalysis(
        object_part="door",
        claim_status=claim_status,
        claim_status_justification="Visible damage.",
        supporting_image_ids="img_1",
        severity="medium",
        valid_image=True,
        image_risk_flags=risk_flags or [],
    )


@pytest.fixture
def clean_user():
    return UserHistory(
        past_claim_count=0,
        accept_claim=0,
        manual_review_claim=0,
        rejected_claim=0,
        last_90_days_claim_count=0,
        history_flags="none",
    )


@pytest.fixture
def high_risk_user():
    return UserHistory(
        past_claim_count=10,
        accept_claim=3,
        manual_review_claim=0,
        rejected_claim=3,  # >= 2 → user_history_risk
        last_90_days_claim_count=5,  # >= 3 → also user_history_risk
        history_flags="previous_discrepancy",
    )


@pytest.fixture
def default_claim():
    return ParsedClaim(primary_issue_type="dent", primary_object_part="door")


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestNoFlags:
    def test_no_vlm_flags_and_clean_user(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, clean_user)
        assert result == "none"

    def test_vlm_none_is_treated_as_empty(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, clean_user)
        assert result == "none"


class TestVlmFlags:
    def test_single_flag_returned(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["blurry_image"])
        result = aggregate(vlm, default_claim, clean_user)
        assert "blurry_image" in result

    def test_multiple_flags_deduplicated(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["blurry_image", "blurry_image", "wrong_angle"])
        result = aggregate(vlm, default_claim, clean_user)
        assert result.count("blurry_image") == 1
        assert "wrong_angle" in result

    def test_flags_sorted_alphabetically(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["wrong_angle", "blurry_image"])
        result = aggregate(vlm, default_claim, clean_user)
        parts = result.split(";")
        assert parts == sorted(parts)

    def test_all_risk_flags_accepted(self, clean_user, default_claim):
        all_flags = [
            "blurry_image", "cropped_or_obstructed", "low_light_or_glare",
            "wrong_angle", "wrong_object", "wrong_object_part",
            "damage_not_visible", "claim_mismatch", "possible_manipulation",
            "non_original_image", "text_instruction_present",
        ]
        vlm = make_vlm(risk_flags=all_flags)
        result = aggregate(vlm, default_claim, clean_user)
        for flag in all_flags:
            assert flag in result, f"Missing flag: {flag}"


class TestUserHistoryFlags:
    def test_history_risk_added_when_rejected_claim_ge_2(self, default_claim):
        user = UserHistory(
            rejected_claim=2, last_90_days_claim_count=0, history_flags="none",
        )
        vlm = make_vlm(risk_flags=["blurry_image"])
        result = aggregate(vlm, default_claim, user)
        assert "user_history_risk" in result

    def test_history_risk_added_when_90_day_claim_ge_3(self, default_claim):
        user = UserHistory(
            rejected_claim=0, last_90_days_claim_count=3, history_flags="none",
        )
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, user)
        assert "user_history_risk" in result

    def test_history_risk_not_added_for_low_activity(self, default_claim):
        user = UserHistory(
            rejected_claim=1, last_90_days_claim_count=2, history_flags="none",
        )
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, user)
        assert "user_history_risk" not in result

    def test_manual_review_added_when_history_flags_not_none(self, default_claim):
        user = UserHistory(
            rejected_claim=0, last_90_days_claim_count=0,
            history_flags="previous_discrepancy",
        )
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, user)
        assert "manual_review_required" in result

    def test_manual_review_added_when_manual_review_claim_ge_2(self, default_claim):
        user = UserHistory(
            rejected_claim=0, last_90_days_claim_count=0,
            history_flags="none", manual_review_claim=2,
        )
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, user)
        assert "manual_review_required" in result


class TestManipulationFlags:
    def test_manipulation_adds_manual_review(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["possible_manipulation"])
        result = aggregate(vlm, default_claim, clean_user)
        assert "possible_manipulation" in result
        assert "manual_review_required" in result

    def test_non_original_image_adds_manual_review(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["non_original_image"])
        result = aggregate(vlm, default_claim, clean_user)
        assert "non_original_image" in result
        assert "manual_review_required" in result

    def test_text_instruction_present_adds_manual_review(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["text_instruction_present"])
        result = aggregate(vlm, default_claim, clean_user)
        assert "text_instruction_present" in result
        assert "manual_review_required" in result

    def test_no_duplicate_manual_review(self, clean_user, default_claim):
        """When manual_review_required already present, don't add a duplicate."""
        # Already has manual_review_required from VLM (unusual but handled)
        vlm = make_vlm(risk_flags=["possible_manipulation", "manual_review_required"])
        result = aggregate(vlm, default_claim, clean_user)
        assert result.count("manual_review_required") == 1

    def test_all_manipulation_flags_are_valid_risk_flags(self):
        """Every MANIPULATION_FLAGS entry should be in RISK_FLAG_VALUES."""
        from modules.models import RISK_FLAG_VALUES
        for flag in MANIPULATION_FLAGS:
            assert flag in RISK_FLAG_VALUES


class TestIntegration:
    def test_complex_scenario(self, high_risk_user, default_claim):
        """Multiple VLM flags + high risk user → combined output."""
        vlm = make_vlm(risk_flags=["blurry_image", "possible_manipulation", "wrong_angle"])
        result = aggregate(vlm, default_claim, high_risk_user)
        assert "blurry_image" in result
        assert "possible_manipulation" in result
        assert "wrong_angle" in result
        assert "user_history_risk" in result
        assert "manual_review_required" in result
        # Verify deterministic ordering
        parts = result.split(";")
        assert parts == sorted(parts)

    def test_clean_scenario_returns_none(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=[])
        result = aggregate(vlm, default_claim, clean_user)
        assert result == "none"

    def test_deterministic(self, clean_user, default_claim):
        vlm = make_vlm(risk_flags=["blurry_image", "wrong_angle"])
        r1 = aggregate(vlm, default_claim, clean_user)
        r2 = aggregate(vlm, default_claim, clean_user)
        assert r1 == r2
