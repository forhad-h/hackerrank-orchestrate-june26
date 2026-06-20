"""Tests for output_assembler.py — M7 Output Assembler & Schema Validator.

All functions are pure logic (string coercion, enum validation).
No I/O except write_csv, which we test with tmp_path.
"""
import csv
import pytest
from modules.output_assembler import (
    OUTPUT_COLUMNS,
    SAFE_DEFAULT_ROW,
    assemble_row,
    write_csv,
    create_safe_default_row,
    _validate_row,
    _coerce_enum,
    _coerce_bool_str,
    _bool_to_str,
    valid_flags_str,
    valid_ids_str,
)
from modules.models import (
    ClaimContext,
    EvidenceEvaluation,
    EvidenceRule,
    ParsedClaim,
    UserHistory,
    VLMAnalysis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_context():
    return ClaimContext(
        user_id="user_001",
        image_paths=["dataset/images/test/img_1.jpg", "dataset/images/test/img_2.jpg"],
        image_ids=["img_1", "img_2"],
        user_claim="My car door has a dent.",
        claim_object="car",
        user_history=UserHistory(),
        evidence_rules=[
            EvidenceRule("REQ_CAR_EXTERIOR_DENT", "car", "dent assessment", "photo of dent"),
        ],
    )


@pytest.fixture
def sample_vlm():
    return VLMAnalysis(
        object_part="door",
        claim_status="supported",
        claim_status_justification="Visible dent on door.",
        supporting_image_ids="img_1",
        severity="medium",
        valid_image=True,
        image_risk_flags=["blurry_image"],
    )


@pytest.fixture
def sample_parsed():
    return ParsedClaim(
        primary_issue_type="dent",
        primary_object_part="door",
        secondary_parts=[],
        damage_description="Door has a visible dent",
    )


@pytest.fixture
def sample_evidence_eval():
    return EvidenceEvaluation(
        applicable_rules=[
            EvidenceRule("REQ_CAR_EXTERIOR_DENT", "car", "dent", "photo of dent"),
        ],
        evidence_standard_met=True,
        evidence_standard_met_reason="[REQ_CAR_EXTERIOR_DENT] Supporting images (img_1) satisfy 'photo of dent'.",
    )


# ── _coerce_enum ──────────────────────────────────────────────────────────────


class TestCoerceEnum:
    def test_valid_value_passes(self):
        assert _coerce_enum("dent", {"dent", "scratch"}, "unknown") == "dent"

    def test_invalid_value_coerces_to_default(self):
        assert _coerce_enum("alien", {"dent", "scratch"}, "unknown") == "unknown"

    def test_case_sensitive(self):
        assert _coerce_enum("Dent", {"dent"}, "unknown") == "unknown"  # case mismatch

    def test_whitespace_not_stripped(self):
        # _coerce_enum does NOT strip — caller's responsibility
        assert _coerce_enum(" dent", {"dent"}, "unknown") == "unknown"

    def test_empty_string(self):
        assert _coerce_enum("", {"dent"}, "unknown") == "unknown"


# ── _coerce_bool_str / _bool_to_str ───────────────────────────────────────────


class TestCoerceBoolStr:
    def test_true_variants(self):
        assert _coerce_bool_str("true") == "true"
        assert _coerce_bool_str("True") == "true"
        assert _coerce_bool_str("TRUE") == "true"
        assert _coerce_bool_str("1") == "true"
        assert _coerce_bool_str("yes") == "true"

    def test_false_variants(self):
        assert _coerce_bool_str("false") == "false"
        assert _coerce_bool_str("False") == "false"
        assert _coerce_bool_str("0") == "false"
        assert _coerce_bool_str("no") == "false"
        assert _coerce_bool_str("") == "false"
        assert _coerce_bool_str("garbage") == "false"


class TestBoolToStr:
    def test_true(self):
        assert _bool_to_str(True) == "true"

    def test_false(self):
        assert _bool_to_str(False) == "false"


# ── valid_flags_str ───────────────────────────────────────────────────────────


class TestValidFlagsStr:
    def test_empty_becomes_none(self):
        assert valid_flags_str("") == "none"

    def test_none_input(self):
        assert valid_flags_str("none") == "none"

    def test_valid_single_flag(self):
        assert valid_flags_str("blurry_image") == "blurry_image"

    def test_multiple_valid_flags(self):
        result = valid_flags_str("blurry_image;wrong_angle")
        assert "blurry_image" in result
        assert "wrong_angle" in result

    def test_invalid_flag_removed(self):
        assert valid_flags_str("blurry_image;fake_flag") == "blurry_image"

    def test_all_invalid_returns_none(self):
        assert valid_flags_str("fake_flag;another_fake") == "none"

    def test_whitespace_around_flags_stripped(self):
        result = valid_flags_str(" blurry_image ; wrong_angle ")
        assert "blurry_image" in result
        assert "wrong_angle" in result


# ── valid_ids_str ─────────────────────────────────────────────────────────────


class TestValidIdsStr:
    def test_empty_becomes_none(self):
        assert valid_ids_str("") == "none"

    def test_none_string(self):
        assert valid_ids_str("none") == "none"

    def test_single_id(self):
        assert valid_ids_str("img_1") == "img_1"

    def test_multiple_ids(self):
        assert valid_ids_str("img_1;img_2") == "img_1;img_2"

    def test_spaces_around_ids_removed(self):
        assert valid_ids_str(" img_1 ; img_2 ") == "img_1;img_2"

    def test_only_spaces_returns_none(self):
        assert valid_ids_str("   ") == "none"

    def test_semicolons_only_returns_none(self):
        assert valid_ids_str(";;;") == "none"


# ── assemble_row ──────────────────────────────────────────────────────────────


class TestAssembleRow:
    def test_basic_row_shape(self, sample_context, sample_vlm, sample_parsed,
                             sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        assert isinstance(row, dict)
        assert set(row.keys()) == set(OUTPUT_COLUMNS)

    def test_user_id_carried(self, sample_context, sample_vlm, sample_parsed,
                             sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        assert row["user_id"] == "user_001"

    def test_evidence_standard_met_as_string(self, sample_context, sample_vlm,
                                              sample_parsed, sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        assert row["evidence_standard_met"] == "true"
        assert isinstance(row["evidence_standard_met"], str)

    def test_valid_image_as_string(self, sample_context, sample_vlm, sample_parsed,
                                    sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        assert row["valid_image"] == "true"
        assert isinstance(row["valid_image"], str)

    def test_risk_flags_empty_becomes_none(self, sample_context, sample_vlm,
                                            sample_parsed, sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="",
        )
        assert row["risk_flags"] == "none"

    def test_issue_type_validated(self, sample_context, sample_vlm, sample_parsed,
                                   sample_evidence_eval):
        parsed = ParsedClaim(primary_issue_type="alien_invasion", primary_object_part="door")
        row = assemble_row(
            sample_context, sample_vlm, parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["issue_type"] == "unknown"  # coerced

    def test_object_part_validated_per_claim_object(self, sample_context, sample_vlm,
                                                     sample_parsed, sample_evidence_eval):
        vlm = sample_vlm
        vlm.object_part = "windshield"  # valid for car
        row = assemble_row(
            sample_context, vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["object_part"] == "windshield"

    def test_object_part_invalid_for_claim_object(self, sample_context, sample_vlm,
                                                   sample_parsed, sample_evidence_eval):
        vlm = sample_vlm
        vlm.object_part = "screen"  # valid for laptop, NOT for car
        row = assemble_row(
            sample_context, vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["object_part"] == "unknown"  # coerced

    def test_claim_status_validated(self, sample_context, sample_vlm, sample_parsed,
                                     sample_evidence_eval):
        vlm = sample_vlm
        vlm.claim_status = "unknown_thing"
        row = assemble_row(
            sample_context, vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["claim_status"] == "not_enough_information"  # coerced

    def test_severity_validated(self, sample_context, sample_vlm, sample_parsed,
                                 sample_evidence_eval):
        vlm = sample_vlm
        vlm.severity = "critical"
        row = assemble_row(
            sample_context, vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["severity"] == "unknown"  # coerced

    def test_supporting_ids_normalized(self, sample_context, sample_vlm, sample_parsed,
                                        sample_evidence_eval):
        vlm = sample_vlm
        vlm.supporting_image_ids = " img_1 ; img_2 "
        row = assemble_row(
            sample_context, vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert row["supporting_image_ids"] == "img_1;img_2"

    def test_image_paths_semicolon_joined(self, sample_context, sample_vlm,
                                           sample_parsed, sample_evidence_eval):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        assert ";" in row["image_paths"]
        assert len(row["image_paths"].split(";")) == 2


# ── create_safe_default_row ───────────────────────────────────────────────────


class TestCreateSafeDefaultRow:
    def test_identity_fields_copied(self, sample_context):
        row = create_safe_default_row(sample_context)
        assert row["user_id"] == "user_001"
        assert "img_1" in row["image_paths"]
        assert row["claim_object"] == "car"

    def test_has_all_columns(self, sample_context):
        row = create_safe_default_row(sample_context)
        assert set(row.keys()) == set(OUTPUT_COLUMNS)

    def test_safe_values(self, sample_context):
        row = create_safe_default_row(sample_context)
        assert row["evidence_standard_met"] == "false"
        assert row["risk_flags"] == "manual_review_required"
        assert row["claim_status"] == "not_enough_information"
        assert row["severity"] == "unknown"
        assert row["valid_image"] == "false"

    def test_default_row_not_mutated_by_multiple_calls(self, sample_context):
        """create_safe_default_row should not mutate SAFE_DEFAULT_ROW."""
        row1 = create_safe_default_row(sample_context)
        row2 = create_safe_default_row(sample_context)
        assert row1 == row2


# ── write_csv ─────────────────────────────────────────────────────────────────


class TestWriteCsv:
    def test_writes_header_and_rows(self, sample_context, sample_vlm, sample_parsed,
                                     sample_evidence_eval, tmp_path):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        output = tmp_path / "test_output.csv"
        write_csv([row], str(output))

        assert output.exists()
        with open(output, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["user_id"] == "user_001"

    def test_multiple_rows(self, sample_context, sample_vlm, sample_parsed,
                            sample_evidence_eval, tmp_path):
        row1 = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        row2 = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="blurry_image",
        )
        output = tmp_path / "multi.csv"
        write_csv([row1, row2], str(output))

        with open(output, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2

    def test_empty_rows_list(self, tmp_path):
        output = tmp_path / "empty.csv"
        write_csv([], str(output))
        assert output.exists()
        with open(output, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 0

    def test_columns_match_output_columns(self, sample_context, sample_vlm,
                                           sample_parsed, sample_evidence_eval,
                                           tmp_path):
        row = assemble_row(
            sample_context, sample_vlm, sample_parsed,
            sample_evidence_eval, risk_flags="none",
        )
        output = tmp_path / "cols.csv"
        write_csv([row], str(output))

        with open(output, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert list(rows[0].keys()) == OUTPUT_COLUMNS


# ── SAFE_DEFAULT_ROW invariants ───────────────────────────────────────────────


class TestSafeDefaultRowInvariants:
    def test_all_columns_present(self):
        assert set(SAFE_DEFAULT_ROW.keys()) == set(OUTPUT_COLUMNS)

    def test_evidence_standard_met_is_string(self):
        assert isinstance(SAFE_DEFAULT_ROW["evidence_standard_met"], str)

    def test_valid_image_is_string(self):
        assert isinstance(SAFE_DEFAULT_ROW["valid_image"], str)

    def test_risk_flags_is_not_empty(self):
        assert SAFE_DEFAULT_ROW["risk_flags"] != ""
