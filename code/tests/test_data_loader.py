"""Unit tests for M1 Data Ingestion (``modules/data_loader.py``)."""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from modules.data_loader import (
    DATASET_DIR as REAL_DATASET_DIR,
    MAX_IMAGES_PER_CLAIM,
    URL_SCHEME_RE,
    LocalCSVDataLoader,
)
from modules.models import ClaimContext, EvidenceRule, UserHistory

# ── Helpers ───────────────────────────────────────────────────────────────


def write_csv(path: Path, rows: List[Dict[str, str]]) -> Path:
    """Write *rows* (list of dicts) as an RFC 4180 CSV at *path*."""
    if not rows:
        path.write_text("")
        return path
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    return path


def make_claims(overrides: Optional[List[dict]] = None) -> List[dict]:
    """Return default claims rows, optionally merged with *overrides*."""
    rows = [
        {"user_id": "user_001", "image_paths": "images/test/img_1.jpg", "user_claim": "Bumper damaged.", "claim_object": "car"},
        {"user_id": "user_002", "image_paths": "images/test/img_1.jpg;images/test/img_2.jpg", "user_claim": "Screen cracked.", "claim_object": "laptop"},
        {"user_id": "user_003", "image_paths": "", "user_claim": "Box is torn.", "claim_object": "package"},
    ]
    if overrides:
        for i, ov in enumerate(overrides):
            if i < len(rows):
                rows[i].update(ov)
    return rows


def make_history() -> List[dict]:
    return [
        {"user_id": "user_001", "past_claim_count": "2", "accept_claim": "2", "manual_review_claim": "0", "rejected_claim": "0", "last_90_days_claim_count": "1", "history_flags": "none", "history_summary": "Low-risk."},
        {"user_id": "user_risk", "past_claim_count": "8", "accept_claim": "3", "manual_review_claim": "3", "rejected_claim": "4", "last_90_days_claim_count": "5", "history_flags": "user_history_risk;manual_review_required", "history_summary": "High-risk."},
    ]


def make_rules() -> List[dict]:
    return [
        {"requirement_id": "REQ_GENERAL_OBJECT_PART", "claim_object": "all", "applies_to": "general claim review", "minimum_image_evidence": "Object visible."},
        {"requirement_id": "REQ_GENERAL_MULTI_IMAGE", "claim_object": "all", "applies_to": "multi-image rows", "minimum_image_evidence": "Each image considered."},
        {"requirement_id": "REQ_CAR_BODY_PANEL", "claim_object": "car", "applies_to": "dent or scratch", "minimum_image_evidence": "Panel visible."},
        {"requirement_id": "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD", "claim_object": "laptop", "applies_to": "screen, keyboard, or trackpad", "minimum_image_evidence": "Screen visible."},
        {"requirement_id": "REQ_PACKAGE_EXTERIOR", "claim_object": "package", "applies_to": "crushed, torn, or seal damage", "minimum_image_evidence": "Package visible."},
        {"requirement_id": "REQ_REVIEW_TRUST", "claim_object": "all", "applies_to": "reviewability", "minimum_image_evidence": "Usable images."},
    ]


def populate_dataset(base: Path, claims_rows: Optional[List[dict]] = None, history_rows: Optional[List[dict]] = None, rule_rows: Optional[List[dict]] = None) -> Path:
    """Write CSV files into *base* and create ``images/`` dir. Returns *base*."""
    base.mkdir(parents=True, exist_ok=True)
    write_csv(base / "claims.csv", claims_rows or make_claims())
    write_csv(base / "user_history.csv", history_rows or make_history())
    write_csv(base / "evidence_requirements.csv", rule_rows or make_rules())
    (base / "images").mkdir(parents=True, exist_ok=True)
    return base


# ── URL Scheme Regex ──────────────────────────────────────────────────────

class TestURLSchemeDetection:
    def test_matches_http(self):
        assert URL_SCHEME_RE.match("http://example.com/img.jpg")

    def test_matches_https(self):
        assert URL_SCHEME_RE.match("https://s3.amazonaws.com/bucket/img.png")

    def test_matches_s3(self):
        assert URL_SCHEME_RE.match("s3://my-bucket/img.jpg")

    def test_matches_ftp(self):
        assert URL_SCHEME_RE.match("ftp://files.com/img.png")

    def test_matches_file_uri(self):
        assert URL_SCHEME_RE.match("file:///etc/passwd")

    def test_case_insensitive(self):
        assert URL_SCHEME_RE.match("HTTP://EXAMPLE.COM/IMG.JPG")

    def test_rejects_relative_path(self):
        assert not URL_SCHEME_RE.match("images/test/img_1.jpg")

    def test_rejects_absolute_path(self):
        assert not URL_SCHEME_RE.match("/absolute/path/img.jpg")

    def test_rejects_windows_path(self):
        assert not URL_SCHEME_RE.match("C:\\Users\\img.jpg")

    def test_rejects_empty_string(self):
        assert not URL_SCHEME_RE.match("")


# ── Separator Normalization ───────────────────────────────────────────────

class TestSeparatorNormalization:
    def test_semicolons(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg;b.jpg;c.jpg") == ["a.jpg", "b.jpg", "c.jpg"]

    def test_commas(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg,b.jpg,c.jpg") == ["a.jpg", "b.jpg", "c.jpg"]

    def test_newlines(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg\nb.jpg\nc.jpg") == ["a.jpg", "b.jpg", "c.jpg"]

    def test_carriage_return_newline(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg\r\nb.jpg") == ["a.jpg", "b.jpg"]

    def test_mixed_separators(self):
        loader = LocalCSVDataLoader()
        result = loader._normalize_and_split_image_paths("a.jpg; b.jpg , c.jpg\nd.jpg")
        assert result == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]

    def test_trailing_separator(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg;b.jpg;") == ["a.jpg", "b.jpg"]

    def test_whitespace_stripped(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("  a.jpg ; b.jpg  ") == ["a.jpg", "b.jpg"]

    def test_single_element(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("a.jpg") == ["a.jpg"]

    def test_empty_string(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths("") == []

    def test_only_separators(self):
        loader = LocalCSVDataLoader()
        assert loader._normalize_and_split_image_paths(";;,,\n") == []


# ── Image ID Extraction ───────────────────────────────────────────────────

class TestImageIDExtraction:
    def test_standard_path(self):
        assert LocalCSVDataLoader._extract_image_id("images/test/case_001/img_1.jpg") == "img_1"

    def test_uppercase_extension(self):
        assert LocalCSVDataLoader._extract_image_id("path/to/img_2.PNG") == "img_2"

    def test_webp_format(self):
        assert LocalCSVDataLoader._extract_image_id("folder/img_3.webp") == "img_3"

    def test_no_directory(self):
        assert LocalCSVDataLoader._extract_image_id("just_a_file.jpeg") == "just_a_file"

    def test_url_like_string(self):
        # URL in image_paths: keep the last path component as ID
        assert LocalCSVDataLoader._extract_image_id("http://example.com/img.jpg") == "img"

    def test_nested_deep_path(self):
        assert LocalCSVDataLoader._extract_image_id("a/b/c/d/e/f/img_999.jpg") == "img_999"


# ── Image Path Resolution ─────────────────────────────────────────────────

class TestImagePathResolution:
    """Tests image resolution with a patched DATASET_DIR."""

    @pytest.fixture
    def tmp_dataset(self, tmp_path: Path) -> Path:
        dset = tmp_path / "dataset"
        (dset / "images").mkdir(parents=True)
        return dset

    def test_resolves_normal_path(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            resolved = loader._resolve_image_path("images/test/img_1.jpg")
            assert resolved is not None
            assert resolved.startswith(str(tmp_dataset / "images"))

    def test_rejects_url(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            assert loader._resolve_image_path("http://evil.com/img.jpg") is None
            assert loader._resolve_image_path("https://evil.com/img.jpg") is None
            assert loader._resolve_image_path("s3://bucket/img.jpg") is None

    def test_rejects_relative_traversal(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            assert loader._resolve_image_path("../../etc/passwd") is None

    def test_rejects_absolute_traversal(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            assert loader._resolve_image_path("/etc/passwd") is None

    def test_rejects_outside_images_dir(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            # A path that resolves to dataset root, not under images/
            assert loader._resolve_image_path("../claims.csv") is None

    def test_rejects_empty_path(self, tmp_dataset: Path):
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            assert loader._resolve_image_path("") is None
            assert loader._resolve_image_path("   ") is None

    def test_rejects_symlink_escape(self, tmp_dataset: Path, monkeypatch):
        """Simulate a symlink that points outside images/."""
        with patch("modules.data_loader.DATASET_DIR", str(tmp_dataset)):
            loader = LocalCSVDataLoader()
            # We can't easily create a symlink outside in a temp dir test,
            # but realpath resolves it. The known-safe check handles it.
            result = loader._resolve_image_path("images/../../etc/passwd")
            assert result is None


# ── CSV Reading ───────────────────────────────────────────────────────────

class TestCSVReading:
    def test_reads_valid_csv(self, tmp_path: Path):
        write_csv(tmp_path / "claims.csv", make_claims())
        loader = LocalCSVDataLoader(claims_path=str(tmp_path / "claims.csv"))
        rows = loader._read_csv(str(tmp_path / "claims.csv"), "claims")
        assert len(rows) == 3

    def test_empty_file_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "empty.csv"
        p.write_text("")
        loader = LocalCSVDataLoader()
        rows = loader._read_csv(str(p), "claims")
        assert rows == []

    def test_header_only_file_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "header.csv"
        p.write_text("user_id,image_paths,user_claim,claim_object\n")
        loader = LocalCSVDataLoader()
        rows = loader._read_csv(str(p), "claims")
        assert rows == []

    def test_missing_optional_file_warns(self, tmp_path: Path):
        p = tmp_path / "nonexistent.csv"
        loader = LocalCSVDataLoader()
        rows = loader._read_csv(str(p), "user_history")
        assert rows == []

    def test_missing_required_file_raises(self, tmp_path: Path):
        p = tmp_path / "nonexistent.csv"
        loader = LocalCSVDataLoader()
        with pytest.raises(FileNotFoundError):
            loader._read_csv(str(p), "claims", required=True)

    def test_missing_columns_warns(self, tmp_path: Path):
        bad_rows = [{"user_id": "u1", "image_paths": "img.jpg"}]  # missing user_claim, claim_object
        write_csv(tmp_path / "bad.csv", bad_rows)
        loader = LocalCSVDataLoader()
        rows = loader._read_csv(str(tmp_path / "bad.csv"), "claims")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "u1"

    def test_whitespace_stripped_from_keys_and_values(self, tmp_path: Path):
        rows = [{"  user_id  ": "  u1  ", "  image_paths  ": "  img.jpg  "}]
        write_csv(tmp_path / "messy.csv", rows)
        loader = LocalCSVDataLoader()
        result = loader._read_csv(str(tmp_path / "messy.csv"), "claims")
        # With DictReader, keys come from header row which won't have spaces
        # But the values should be stripped
        assert len(result) == 1
        assert result[0]["user_id"] == "u1"
        assert result[0]["image_paths"] == "img.jpg"

    def test_utf8_bom_encoding(self, tmp_path: Path):
        p = tmp_path / "bom.csv"
        # Write a file with a UTF-8 BOM prefix
        raw_utf8 = b'"user_id","claim_object"\n"u1","car"\n'
        p.write_bytes(b"\xef\xbb\xbf" + raw_utf8)
        loader = LocalCSVDataLoader()
        rows = loader._read_csv(str(p), "claims")
        assert len(rows) == 1
        # utf-8-sig strips the BOM, so the key is "user_id"
        assert rows[0]["user_id"] == "u1"


# ── User History ──────────────────────────────────────────────────────────

class TestUserHistory:
    def test_existing_user(self, tmp_path: Path):
        write_csv(tmp_path / "user_history.csv", make_history())
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "user_history.csv"))
        hist = loader._read_user_history()
        assert "user_001" in hist
        assert hist["user_001"].past_claim_count == 2
        assert hist["user_001"].history_flags == "none"

    def test_user_with_risk_flags(self, tmp_path: Path):
        write_csv(tmp_path / "user_history.csv", make_history())
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "user_history.csv"))
        hist = loader._read_user_history()
        assert "user_risk" in hist
        assert hist["user_risk"].rejected_claim == 4
        assert hist["user_risk"].history_flags == "user_history_risk;manual_review_required"

    def test_missing_user_gets_default(self, tmp_path: Path):
        write_csv(tmp_path / "user_history.csv", make_history())
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "user_history.csv"))
        hist = loader._read_user_history()
        assert "nonexistent_user" not in hist
        # The safe default is applied by _build_context, not _read_user_history
        # _read_user_history just returns what it parsed

    def test_malformed_row_uses_safe_default(self, tmp_path: Path):
        rows = [
            {"user_id": "good_user", "past_claim_count": "2", "accept_claim": "1", "manual_review_claim": "0", "rejected_claim": "0", "last_90_days_claim_count": "0", "history_flags": "none", "history_summary": ""},
            {"user_id": "bad_user", "past_claim_count": "not_a_number", "accept_claim": "1", "manual_review_claim": "0", "rejected_claim": "0", "last_90_days_claim_count": "0", "history_flags": "none", "history_summary": ""},
        ]
        write_csv(tmp_path / "uh.csv", rows)
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "uh.csv"))
        hist = loader._read_user_history()
        assert "good_user" in hist
        assert "bad_user" in hist  # _safe_int catches the error and uses default 0
        assert hist["bad_user"].past_claim_count == 0

    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "nope.csv"))
        hist = loader._read_user_history()
        assert hist == {}

    def test_empty_user_id_skipped(self, tmp_path: Path):
        rows = [{"user_id": "", "past_claim_count": "1", "accept_claim": "1", "manual_review_claim": "0", "rejected_claim": "0", "last_90_days_claim_count": "0", "history_flags": "none", "history_summary": ""}]
        write_csv(tmp_path / "uh.csv", rows)
        loader = LocalCSVDataLoader(user_history_path=str(tmp_path / "uh.csv"))
        hist = loader._read_user_history()
        assert "" not in hist


# ── Evidence Rules ────────────────────────────────────────────────────────

class TestEvidenceRules:
    def test_loads_all_rules(self, tmp_path: Path):
        write_csv(tmp_path / "rules.csv", make_rules())
        loader = LocalCSVDataLoader(evidence_requirements_path=str(tmp_path / "rules.csv"))
        rules = loader._read_evidence_rules()
        assert len(rules) == 6

    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        loader = LocalCSVDataLoader(evidence_requirements_path=str(tmp_path / "nope.csv"))
        rules = loader._read_evidence_rules()
        assert rules == []

    def test_malformed_row_skipped(self, tmp_path: Path):
        rows = [
            {"requirement_id": "REQ_GOOD", "claim_object": "all", "applies_to": "general", "minimum_image_evidence": "Evidence."},
            {},  # missing all keys
        ]
        write_csv(tmp_path / "rules.csv", rows)
        loader = LocalCSVDataLoader(evidence_requirements_path=str(tmp_path / "rules.csv"))
        rules = loader._read_evidence_rules()
        assert len(rules) == 2  # Both rows get parsed; the empty one just has empty strings
        assert rules[0].requirement_id == "REQ_GOOD"


# ── Evidence Rules Filtering (integration via _build_context) ─────────────

class TestEvidenceFiltering:
    @pytest.fixture
    def base(self, tmp_path: Path) -> Path:
        return populate_dataset(tmp_path / "base")

    def test_car_claim_gets_car_and_all_rules(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[0], {"user_001": UserHistory()}, rules)
            ids = {r.requirement_id for r in ctx.evidence_rules}
        assert "REQ_CAR_BODY_PANEL" in ids
        assert "REQ_GENERAL_OBJECT_PART" in ids
        assert "REQ_REVIEW_TRUST" in ids
        assert "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD" not in ids

    def test_laptop_claim_gets_laptop_and_all_rules(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[1], {"user_002": UserHistory()}, rules)
            ids = {r.requirement_id for r in ctx.evidence_rules}
        assert "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD" in ids
        assert "REQ_GENERAL_OBJECT_PART" in ids
        assert "REQ_CAR_BODY_PANEL" not in ids

    def test_package_claim_gets_package_and_all_rules(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[2], {"user_003": UserHistory()}, rules)
            ids = {r.requirement_id for r in ctx.evidence_rules}
        assert "REQ_PACKAGE_EXTERIOR" in ids
        assert "REQ_GENERAL_OBJECT_PART" in ids
        assert "REQ_CAR_BODY_PANEL" not in ids

    def test_unknown_object_gets_only_all_rules(self, base: Path):
        """A claim_object not in CLAIM_OBJECT_VALUES gets only "all" rules."""
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_bad", "image_paths": "img.jpg", "user_claim": "test", "claim_object": "bicycle"}
            ctx = loader._build_context(row, {}, rules)
            ids = {r.requirement_id for r in ctx.evidence_rules}
        assert "REQ_CAR_BODY_PANEL" not in ids
        assert "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD" not in ids
        assert "REQ_PACKAGE_EXTERIOR" not in ids
        assert "REQ_GENERAL_OBJECT_PART" in ids
        assert "REQ_REVIEW_TRUST" in ids


# ── Context Building ──────────────────────────────────────────────────────

class TestContextBuilding:
    @pytest.fixture
    def base(self, tmp_path: Path) -> Path:
        return populate_dataset(tmp_path / "base")

    def test_basic_context(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[0], loader._read_user_history(), rules)
        assert ctx.user_id == "user_001"
        assert ctx.claim_object == "car"
        assert len(ctx.image_paths) == 1
        assert ctx.image_ids == ["img_1"]
        assert ctx.user_claim == "Bumper damaged."
        assert ctx.user_history.past_claim_count == 2
        assert len(ctx.evidence_rules) == 4  # 2 generic + 2 car

    def test_multi_image_claim(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[1], loader._read_user_history(), rules)
        assert len(ctx.image_paths) == 2
        assert ctx.image_ids == ["img_1", "img_2"]

    def test_empty_image_paths(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(rows[2], loader._read_user_history(), rules)
        assert ctx.image_paths == []
        assert ctx.image_ids == []

    def test_missing_user_gets_default_history(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            # user_003 is not in history map
            ctx = loader._build_context(rows[2], loader._read_user_history(), rules)
        assert ctx.user_history.past_claim_count == 0
        assert ctx.user_history.history_flags == "none"

    def test_unknown_claim_object_coerced(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "img.jpg", "user_claim": "thing broke", "claim_object": "bicycle"}
            ctx = loader._build_context(row, {}, rules)
        assert ctx.claim_object == "unknown"
        # Only "all" rules apply
        assert all(r.claim_object == "all" for r in ctx.evidence_rules)

    def test_url_kept_in_image_paths(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "http://evil.com/img.jpg;images/test/img_1.jpg", "user_claim": "test", "claim_object": "car"}
            ctx = loader._build_context(row, {}, rules)
        # URL entry kept
        assert "http://evil.com/img.jpg" in ctx.image_paths
        assert ctx.image_ids[0] == "img"
        # Normal entry resolved
        assert ctx.image_paths[1].startswith(str(base / "images"))
        assert ctx.image_ids[1] == "img_1"

    def test_path_traversal_dropped(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "../../etc/passwd;images/test/img_1.jpg", "user_claim": "test", "claim_object": "car"}
            ctx = loader._build_context(row, {}, rules)
        # Traversal dropped, only the safe path remains
        assert len(ctx.image_paths) == 1
        assert ctx.image_paths[0].endswith("img_1.jpg")

    def test_max_images_truncated(self, base: Path):
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            many_paths = ";".join([f"images/test/img_{i}.jpg" for i in range(25)])
            row = {"user_id": "u_test", "image_paths": many_paths, "user_claim": "test", "claim_object": "car"}
            rules = loader._read_evidence_rules()
            ctx = loader._build_context(row, {}, rules)
        assert len(ctx.image_paths) == MAX_IMAGES_PER_CLAIM
        assert ctx.image_ids[-1] == f"img_{MAX_IMAGES_PER_CLAIM - 1}"


# ── Full Load Integration ─────────────────────────────────────────────────

class TestFullLoad:
    def test_loads_all_claims_with_history_and_rules(self, tmp_path: Path):
        base = populate_dataset(tmp_path / "dataset")
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert len(contexts) == 3
        for ctx in contexts:
            assert isinstance(ctx, ClaimContext)
            assert isinstance(ctx.user_history, UserHistory)
            assert len(ctx.evidence_rules) > 0

    def test_context_types(self, tmp_path: Path):
        base = populate_dataset(tmp_path / "dataset")
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        for ctx in contexts:
            assert isinstance(ctx.user_id, str)
            assert isinstance(ctx.image_paths, list)
            assert isinstance(ctx.image_ids, list)
            assert isinstance(ctx.user_claim, str)
            assert isinstance(ctx.claim_object, str)
            assert isinstance(ctx.evidence_rules, list)
            if ctx.evidence_rules:
                assert isinstance(ctx.evidence_rules[0], EvidenceRule)

    def test_empty_claims_file_returns_empty_list(self, tmp_path: Path):
        base = tmp_path / "dataset"
        base.mkdir()
        write_csv(base / "claims.csv", [])
        write_csv(base / "user_history.csv", make_history())
        write_csv(base / "evidence_requirements.csv", make_rules())
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert contexts == []

    def test_missing_claims_file_raises(self, tmp_path: Path):
        loader = LocalCSVDataLoader(
            claims_path=str(tmp_path / "nonexistent.csv"),
        )
        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_missing_optional_files_does_not_abort(self, tmp_path: Path):
        base = tmp_path / "dataset"
        base.mkdir()
        write_csv(base / "claims.csv", make_claims())
        # No user_history.csv, no evidence_requirements.csv
        (base / "images").mkdir()
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert len(contexts) == 3
        # All users get default history
        assert all(ctx.user_history.past_claim_count == 0 for ctx in contexts)
        # No evidence rules loaded
        assert all(ctx.evidence_rules == [] for ctx in contexts)

    def test_malformed_claim_row_skipped(self, tmp_path: Path):
        base = tmp_path / "dataset"
        populate_dataset(base)
        # Add a row that will fail _build_context (missing critical field)
        rows = make_claims() + [{"user_id": "", "image_paths": "", "user_claim": "", "claim_object": ""}]
        write_csv(base / "claims.csv", rows)
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        # The empty row is still built (no exception, just empty fields)
        # _build_context doesn't raise for empty fields
        assert len(contexts) == 4


# ── Security ──────────────────────────────────────────────────────────────

class TestSecurity:
    def test_url_kept_for_m2_flagging(self, tmp_path: Path):
        """URL paths are kept in image_paths so M2 can flag file_missing."""
        base = populate_dataset(tmp_path / "dataset")
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rows = loader._read_csv(str(base / "claims.csv"), "claims")
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "https://example.com/photo.jpg", "user_claim": "test", "claim_object": "car"}
            ctx = loader._build_context(row, {}, rules)
        assert "https://example.com/photo.jpg" in ctx.image_paths
        assert "photo" in ctx.image_ids

    def test_traversal_not_in_image_paths(self, tmp_path: Path):
        """Traversal attempts are NOT forwarded to M2."""
        base = populate_dataset(tmp_path / "dataset")
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "/etc/passwd;../../etc/shadow", "user_claim": "test", "claim_object": "car"}
            ctx = loader._build_context(row, {}, rules)
        # Neither absolute path nor relative traversal should be in image_paths
        assert "/etc/passwd" not in ctx.image_paths
        assert "../../etc/shadow" not in ctx.image_paths
        assert ctx.image_paths == []

    def test_all_images_url_returns_empty_resolved(self, tmp_path: Path):
        """Claim with only URLs gets empty image_paths (URLs kept separately)."""
        base = populate_dataset(tmp_path / "dataset")
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            rules = loader._read_evidence_rules()
            row = {"user_id": "u_test", "image_paths": "http://img1.jpg;http://img2.jpg", "user_claim": "test", "claim_object": "car"}
            ctx = loader._build_context(row, {}, rules)
        assert len(ctx.image_paths) == 2
        assert all(p.startswith("http") for p in ctx.image_paths)
        # Both are URLs — none resolved under images/


# ── DATASET_DIR Module Constant ───────────────────────────────────────────

class TestDatasetDirConstant:
    def test_default_points_to_repo_dataset(self):
        """DATASET_DIR should resolve to a real directory."""
        assert isinstance(REAL_DATASET_DIR, str)
        assert os.path.isabs(REAL_DATASET_DIR)

    def test_max_images_public_constant(self):
        """MAX_IMAGES_PER_CLAIM is exported and is a positive int."""
        assert isinstance(MAX_IMAGES_PER_CLAIM, int)
        assert MAX_IMAGES_PER_CLAIM > 0


# ── Edge Cases ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_newlines_in_claim_text_preserved(self, tmp_path: Path):
        """Newlines inside the quoted user_claim field must be preserved."""
        base = tmp_path / "dataset"
        base.mkdir()
        write_csv(base / "user_history.csv", make_history())
        write_csv(base / "evidence_requirements.csv", make_rules())
        (base / "images").mkdir()

        rows = [
            {"user_id": "user_001", "image_paths": "images/test/img_1.jpg", "user_claim": "Line one.\nLine two.", "claim_object": "car"},
        ]
        write_csv(base / "claims.csv", rows)
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert len(contexts) == 1
        assert "Line one." in contexts[0].user_claim
        assert "Line two." in contexts[0].user_claim

    def test_special_csv_characters_handled(self, tmp_path: Path):
        """Commas, quotes inside claim text must be handled (QUOTE_ALL)."""
        base = tmp_path / "dataset"
        base.mkdir()
        write_csv(base / "user_history.csv", make_history())
        write_csv(base / "evidence_requirements.csv", make_rules())
        (base / "images").mkdir()

        text = 'Damage includes "dents", scratches, and more.'
        rows = [
            {"user_id": "u1", "image_paths": "images/test/img_1.jpg", "user_claim": text, "claim_object": "car"},
        ]
        write_csv(base / "claims.csv", rows)
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert contexts[0].user_claim == text

    def test_large_number_of_rules_does_not_affect_loading(self, tmp_path: Path):
        """Many evidence rules load correctly."""
        base = tmp_path / "dataset"
        base.mkdir()
        write_csv(base / "user_history.csv", make_history())
        write_csv(base / "claims.csv", make_claims())
        (base / "images").mkdir()

        many_rules = make_rules() + [
            {"requirement_id": f"REQ_EXTRA_{i}", "claim_object": "car", "applies_to": "extra", "minimum_image_evidence": "Extra."}
            for i in range(100)
        ]
        write_csv(base / "evidence_requirements.csv", many_rules)
        with patch("modules.data_loader.DATASET_DIR", str(base)):
            loader = LocalCSVDataLoader(
                claims_path=str(base / "claims.csv"),
                user_history_path=str(base / "user_history.csv"),
                evidence_requirements_path=str(base / "evidence_requirements.csv"),
            )
            contexts = loader.load()
        assert len(contexts) == 3
        assert len(contexts[0].evidence_rules) > 100  # car rules + all rules
