"""Tests for evidence_evaluator.py — M5 Evidence Standard Evaluator.

All functions are pure logic (no I/O, no API calls), so unit tests are
cheap and fully deterministic.
"""
import pytest
from modules.evidence_evaluator import (
    evaluate,
    _find_rule,
    _match_rules,
    _evaluate_rule,
    _has_quality_flag,
    _quality_flag_names,
    _extract_keywords,
    _ISSUE_TO_RULE_OVERRIDE,
    _EVIDENCE_QUALITY_FLAGS,
)
from modules.models import (
    EvidenceEvaluation,
    EvidenceRule,
    ParsedClaim,
    VLMAnalysis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_rules():
    return [
        EvidenceRule("REQ_GENERAL_OBJECT_PART", "all", "object part must be identified", "object part visible"),
        EvidenceRule("REQ_REVIEW_TRUST", "all", "image usability", "usable images"),
        EvidenceRule("REQ_GENERAL_MULTI_IMAGE", "all", "multi-image claims", "at least 2 images support claim"),
        EvidenceRule("REQ_CAR_EXTERIOR_DENT", "car", "dents and scratches", "clear photo of dent area"),
        EvidenceRule("REQ_CAR_GLASS_LIGHT_MIRROR", "car", "crack or broken glass", "clear photo of glass damage"),
        EvidenceRule("REQ_CAR_BUMPER_CRACK", "car", "bumper crack assessment", "photo of cracked area"),
        EvidenceRule("REQ_LAPTOP_SCREEN_CRACK", "laptop", "screen crack", "clear photo of screen damage"),
        EvidenceRule("REQ_PACKAGE_TORN", "package", "torn packaging", "photo of torn area"),
    ]


@pytest.fixture
def parsed_claim_dent():
    return ParsedClaim(
        primary_issue_type="dent",
        primary_object_part="door",
        secondary_parts=[],
        damage_description="Door has a visible dent",
    )


@pytest.fixture
def parsed_claim_glass():
    return ParsedClaim(
        primary_issue_type="glass_shatter",
        primary_object_part="windshield",
        secondary_parts=["side_mirror"],
        damage_description="Windshield shattered",
    )


def make_vlm(
    object_part="door",
    claim_status="supported",
    justification="Damage visible in images.",
    supporting_ids="img_1",
    severity="medium",
    valid_image=True,
    risk_flags=None,
):
    return VLMAnalysis(
        object_part=object_part,
        claim_status=claim_status,
        claim_status_justification=justification,
        supporting_image_ids=supporting_ids,
        severity=severity,
        valid_image=valid_image,
        image_risk_flags=risk_flags or [],
    )


# ── _extract_keywords ─────────────────────────────────────────────────────────


class TestExtractKeywords:
    def test_single_word_lowercased(self):
        assert _extract_keywords("Dent") == {"dent"}

    def test_underscore_split(self):
        result = _extract_keywords("glass_shatter")
        assert "glass" in result
        assert "shatter" in result

    def test_hyphen_split(self):
        result = _extract_keywords("torn-packaging")
        assert "torn" in result
        assert "packaging" in result

    def test_comma_split(self):
        result = _extract_keywords("crack, broken")
        assert "crack" in result
        assert "broken" in result

    def test_stop_words_removed(self):
        result = _extract_keywords("the dent on the car")
        assert "the" not in result
        assert "on" not in result
        assert "dent" in result
        assert "car" in result

    def test_empty_string(self):
        assert _extract_keywords("") == set()

    def test_all_stop_words(self):
        assert _extract_keywords("a an the or") == set()


# ── _find_rule ────────────────────────────────────────────────────────────────


class TestFindRule:
    def test_finds_existing_rule(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_CAR_EXTERIOR_DENT")
        assert rule is not None
        assert rule.requirement_id == "REQ_CAR_EXTERIOR_DENT"

    def test_returns_none_for_missing(self, sample_rules):
        assert _find_rule(sample_rules, "NONEXISTENT") is None

    def test_returns_first_match(self, sample_rules):
        # Add duplicate ID
        dup_rules = sample_rules + [
            EvidenceRule("REQ_CAR_EXTERIOR_DENT", "all", "duplicate", "dup")
        ]
        rule = _find_rule(dup_rules, "REQ_CAR_EXTERIOR_DENT")
        assert rule.applies_to == "dents and scratches"  # first match, not dup

    def test_empty_rules(self):
        assert _find_rule([], "ANY") is None


# ── _match_rules ──────────────────────────────────────────────────────────────


class TestMatchRules:
    def test_matches_on_keyword_overlap(self, sample_rules, parsed_claim_dent):
        matched = _match_rules(parsed_claim_dent, sample_rules)
        matched_ids = {r.requirement_id for r in matched}
        assert "REQ_CAR_EXTERIOR_DENT" in matched_ids
        # Universal rules are excluded from match_rules
        assert "REQ_GENERAL_OBJECT_PART" not in matched_ids
        assert "REQ_REVIEW_TRUST" not in matched_ids

    def test_glass_shatter_uses_override(self, sample_rules, parsed_claim_glass):
        """GAP-3: glass_shatter → REQ_CAR_GLASS_LIGHT_MIRROR via override table."""
        matched = _match_rules(parsed_claim_glass, sample_rules)
        matched_ids = {r.requirement_id for r in matched}
        assert "REQ_CAR_GLASS_LIGHT_MIRROR" in matched_ids

    def test_unknown_issue_type_matches_no_domain_rules(self, sample_rules):
        claim = ParsedClaim(
            primary_issue_type="alien_attack",
            primary_object_part="unknown",
        )
        matched = _match_rules(claim, sample_rules)
        assert len(matched) == 0

    def test_empty_rules_returns_empty(self, parsed_claim_dent):
        matched = _match_rules(parsed_claim_dent, [])
        assert matched == []

    def test_excludes_universal_and_multi_image(self, sample_rules, parsed_claim_dent):
        matched = _match_rules(parsed_claim_dent, sample_rules)
        matched_ids = {r.requirement_id for r in matched}
        assert "REQ_GENERAL_OBJECT_PART" not in matched_ids
        assert "REQ_REVIEW_TRUST" not in matched_ids
        assert "REQ_GENERAL_MULTI_IMAGE" not in matched_ids

    def test_laptop_issue_matches_laptop_rule(self, sample_rules):
        claim = ParsedClaim(
            primary_issue_type="crack",
            primary_object_part="screen",
        )
        matched = _match_rules(claim, sample_rules)
        matched_ids = {r.requirement_id for r in matched}
        assert "REQ_LAPTOP_SCREEN_CRACK" in matched_ids

    def test_override_ids_in_ISSUE_TO_RULE_OVERRIDE(self):
        """Every override ID should be a valid issue type in ISSUE_TYPE_VALUES."""
        for issue_type in _ISSUE_TO_RULE_OVERRIDE:
            from modules.models import ISSUE_TYPE_VALUES
            assert issue_type in ISSUE_TYPE_VALUES, (
                f"Override key '{issue_type}' not in ISSUE_TYPE_VALUES"
            )


# ── _has_quality_flag / _quality_flag_names ───────────────────────────────────


class TestQualityFlagHelpers:
    def test_no_flags(self):
        vlm = make_vlm(risk_flags=[])
        assert _has_quality_flag(vlm) is False
        assert _quality_flag_names(vlm) == []

    def test_non_quality_flags_ignored(self):
        vlm = make_vlm(risk_flags=["blurry_image", "low_light_or_glare"])
        # blurry_image and low_light_or_glare are NOT in _EVIDENCE_QUALITY_FLAGS
        assert _has_quality_flag(vlm) is False

    def test_quality_flag_detected(self):
        vlm = make_vlm(risk_flags=["wrong_object", "blurry_image"])
        assert _has_quality_flag(vlm) is True

    def test_quality_flag_names_returns_only_quality_flags(self):
        vlm = make_vlm(risk_flags=["wrong_object", "blurry_image", "damage_not_visible"])
        names = _quality_flag_names(vlm)
        assert "wrong_object" in names
        assert "damage_not_visible" in names
        assert "blurry_image" not in names

    def test_quality_flags_match_definition(self):
        """Verify _EVIDENCE_QUALITY_FLAGS is a subset of RISK_FLAG_VALUES."""
        from modules.models import RISK_FLAG_VALUES
        for flag in _EVIDENCE_QUALITY_FLAGS:
            assert flag in RISK_FLAG_VALUES, f"'{flag}' not in RISK_FLAG_VALUES"


# ── _evaluate_rule ────────────────────────────────────────────────────────────


class TestEvaluateRule:
    def test_REQ_REVIEW_TRUST_passes_with_valid_image_no_quality_issues(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_REVIEW_TRUST")
        vlm = make_vlm(valid_image=True, risk_flags=[])
        ok, reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is True
        assert "usable" in reason.lower()

    def test_REQ_REVIEW_TRUST_fails_with_quality_flags(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_REVIEW_TRUST")
        vlm = make_vlm(valid_image=True, risk_flags=["wrong_object"])
        ok, reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is False
        assert "quality" in reason.lower()

    def test_REQ_REVIEW_TRUST_fails_when_invalid(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_REVIEW_TRUST")
        vlm = make_vlm(valid_image=False)
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is False

    def test_REQ_GENERAL_OBJECT_PART_passes_with_known_part(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_GENERAL_OBJECT_PART")
        vlm = make_vlm(object_part="door")
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is True

    def test_REQ_GENERAL_OBJECT_PART_fails_with_unknown(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_GENERAL_OBJECT_PART")
        vlm = make_vlm(object_part="unknown")
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is False

    def test_REQ_GENERAL_MULTI_IMAGE_skipped_for_single_image(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_GENERAL_MULTI_IMAGE")
        vlm = make_vlm(supporting_ids="none")
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"), image_count=1)
        assert ok is True  # single-image → not applicable, passes vacuously

    def test_REQ_GENERAL_MULTI_IMAGE_passes_with_supporting_ids(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_GENERAL_MULTI_IMAGE")
        vlm = make_vlm(supporting_ids="img_1;img_2")
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"), image_count=3)
        assert ok is True

    def test_REQ_GENERAL_MULTI_IMAGE_fails_without_supporting_ids(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_GENERAL_MULTI_IMAGE")
        vlm = make_vlm(supporting_ids="none")
        ok, reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"), image_count=3)
        assert ok is False
        assert "none of" in reason.lower()

    def test_domain_rule_fails_with_quality_flag(self, sample_rules):
        """GAP-7: quality flags make domain-specific rules fail."""
        rule = _find_rule(sample_rules, "REQ_CAR_EXTERIOR_DENT")
        vlm = make_vlm(risk_flags=["wrong_object"])
        ok, reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is False
        assert "not satisfied" in reason

    def test_domain_rule_passes_with_supporting_ids(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_CAR_EXTERIOR_DENT")
        vlm = make_vlm(supporting_ids="img_1", risk_flags=[])
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is True

    def test_domain_rule_passes_with_supported_status_fallback(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_CAR_EXTERIOR_DENT")
        vlm = make_vlm(supporting_ids="none", claim_status="supported", risk_flags=[])
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is True

    def test_domain_rule_fails_on_all_counts(self, sample_rules):
        rule = _find_rule(sample_rules, "REQ_CAR_EXTERIOR_DENT")
        vlm = make_vlm(
            supporting_ids="none", claim_status="not_enough_information",
            risk_flags=[],
        )
        ok, _reason = _evaluate_rule(rule, vlm, ParsedClaim("dent", "door"))
        assert ok is False


# ── evaluate (public API) ─────────────────────────────────────────────────────


class TestEvaluate:
    def test_basic_supported_claim(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1", risk_flags=[])
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=2)
        assert isinstance(result, EvidenceEvaluation)
        assert result.evidence_standard_met is True
        assert len(result.applicable_rules) >= 2

    def test_no_valid_images_short_circuits(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(valid_image=False)
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=2)
        assert result.evidence_standard_met is False
        assert "no valid images" in result.evidence_standard_met_reason.lower()
        assert result.applicable_rules == []

    def test_contradicted_does_not_short_circuit(self, sample_rules, parsed_claim_dent):
        """GAP-5: contradicted is still sufficient evidence — evaluate normally."""
        vlm = make_vlm(
            claim_status="contradicted", supporting_ids="img_1",
            object_part="door", risk_flags=[],
        )
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=2)
        # Rules can still be met even if claim is contradicted
        assert len(result.applicable_rules) >= 2
        assert isinstance(result.evidence_standard_met, bool)

    def test_always_includes_universal_rules(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1", risk_flags=[])
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=1)
        rule_ids = {r.requirement_id for r in result.applicable_rules}
        assert "REQ_GENERAL_OBJECT_PART" in rule_ids
        assert "REQ_REVIEW_TRUST" in rule_ids

    def test_includes_multi_image_rule_when_count_gt_1(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1;img_2", risk_flags=[])
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=3)
        rule_ids = {r.requirement_id for r in result.applicable_rules}
        assert "REQ_GENERAL_MULTI_IMAGE" in rule_ids

    def test_skips_multi_image_rule_when_count_le_1(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1", risk_flags=[])
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=1)
        rule_ids = {r.requirement_id for r in result.applicable_rules}
        assert "REQ_GENERAL_MULTI_IMAGE" not in rule_ids

    def test_empty_rules_list(self, parsed_claim_dent):
        vlm = make_vlm()
        result = evaluate(vlm, parsed_claim_dent, [], image_count=1)
        assert result.evidence_standard_met is True  # no rules to fail
        assert result.applicable_rules == []

    def test_evidence_not_met_when_rule_fails(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(
            object_part="unknown", supporting_ids="none",
            claim_status="not_enough_information", risk_flags=[],
        )
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=1)
        assert result.evidence_standard_met is False

    def test_handles_none_evidence_rules(self, parsed_claim_dent):
        vlm = make_vlm()
        result = evaluate(vlm, parsed_claim_dent, None, image_count=1)
        # Should handle None gracefully
        assert result.evidence_standard_met is True

    def test_deterministic_output(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1", risk_flags=[])
        result1 = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=2)
        result2 = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=2)
        assert result1.evidence_standard_met == result2.evidence_standard_met
        assert result1.evidence_standard_met_reason == result2.evidence_standard_met_reason

    def test_reason_includes_all_rule_results(self, sample_rules, parsed_claim_dent):
        vlm = make_vlm(object_part="door", supporting_ids="img_1", risk_flags=[])
        result = evaluate(vlm, parsed_claim_dent, sample_rules, image_count=1)
        for rule in result.applicable_rules:
            assert rule.requirement_id in result.evidence_standard_met_reason
