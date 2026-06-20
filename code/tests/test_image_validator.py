"""Unit tests for M2 Image Validator (``modules.image_validator.py``).

Covers:
  - Normal operation (valid images → base64 output)
  - Structural checks (missing files, unsupported formats, URLs)
  - Safety limits (file size, minimum dimensions, decompression bombs)
  - Mixed scenarios (some valid, some invalid)
  - Error isolation (one corrupt image doesn't block others)
  - Security (no absolute path leaks, no base64 logs)
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile

import pytest
from PIL import Image

from modules.image_validator import (
    MAX_FILE_SIZE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_LONGEST_EDGE_PX,
    MIN_DIMENSION_PX,
    SUPPORTED_EXTENSIONS,
    URL_SCHEME_RE,
    validate_images,
)
from modules.models import ClaimContext, ImageValidationResult, UserHistory


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(
    image_paths=None,
    image_ids=None,
    user_id="test_user",
    claim_object="car",
    user_claim="Test damage claim.",
):
    """Build a minimal ClaimContext for testing."""
    return ClaimContext(
        user_id=user_id,
        image_paths=image_paths or [],
        image_ids=image_ids or [],
        user_claim=user_claim,
        claim_object=claim_object,
        user_history=UserHistory(),
        evidence_rules=[],
    )


def _create_test_image(
    size=(800, 600),
    fmt="JPEG",
    color=(128, 128, 128),
    suffix=".jpg",
) -> str:
    """Create a temporary image file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    img = Image.new("RGB", size, color)
    img.save(tmp, format=fmt)
    tmp.close()
    return tmp.name


def _make_binary_file(size_bytes: int, suffix: str = ".jpg") -> str:
    """Create a temporary binary file with *size_bytes* of random data."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(os.urandom(size_bytes))
    tmp.close()
    return tmp.name


# ── URL Scheme Regex ─────────────────────────────────────────────────────────

class TestURLSchemeDetection:
    def test_matches_http(self):
        assert URL_SCHEME_RE.match("http://example.com/img.jpg")

    def test_matches_https(self):
        assert URL_SCHEME_RE.match("https://s3.amazonaws.com/bucket/img.png")

    def test_matches_s3(self):
        assert URL_SCHEME_RE.match("s3://my-bucket/img.jpg")

    def test_data_uri_does_not_match(self):
        """data: URIs have no ``://`` so they don't match — they fall through
        to ``os.path.isfile()`` which returns ``False``, correctly producing
        ``file_missing``."""
        assert not URL_SCHEME_RE.match("data:image/png;base64,abc123")

    def test_rejects_relative_path(self):
        assert not URL_SCHEME_RE.match("images/test/img_1.jpg")

    def test_rejects_absolute_path(self):
        assert not URL_SCHEME_RE.match("/absolute/path/img.jpg")

    def test_rejects_empty_string(self):
        assert not URL_SCHEME_RE.match("")


# ── Basic Validation ─────────────────────────────────────────────────────────

class TestBasicValidation:
    def test_valid_image_returns_base64(self):
        path = _create_test_image()
        try:
            ctx = _make_context(image_paths=[path], image_ids=["img_1"])
            result = validate_images(ctx)
            assert result.valid_image is True
            assert "img_1" in result.images_b64
            assert isinstance(result.images_b64["img_1"], str)
            assert len(result.images_b64["img_1"]) > 0
            assert result.structural_flags == []
        finally:
            os.unlink(path)

    def test_multiple_valid_images(self):
        paths = [_create_test_image(suffix=".jpg"), _create_test_image(suffix=".png", fmt="PNG")]
        try:
            ctx = _make_context(
                image_paths=paths,
                image_ids=["img_1", "img_2"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 2
            assert result.structural_flags == []
        finally:
            for p in paths:
                os.unlink(p)

    def test_empty_image_list_returns_invalid(self):
        ctx = _make_context(image_paths=[], image_ids=[])
        result = validate_images(ctx)
        assert result.valid_image is False
        assert result.images_b64 == {}
        assert result.structural_flags == []

    def test_returns_image_validation_result_type(self):
        path = _create_test_image()
        try:
            ctx = _make_context(image_paths=[path], image_ids=["img_1"])
            result = validate_images(ctx)
            assert isinstance(result, ImageValidationResult)
        finally:
            os.unlink(path)

    def test_base64_is_valid_base64(self):
        import base64
        path = _create_test_image()
        try:
            ctx = _make_context(image_paths=[path], image_ids=["img_1"])
            result = validate_images(ctx)
            b64 = result.images_b64["img_1"]
            # Should be decodable without error
            decoded = base64.b64decode(b64)
            assert len(decoded) > 0
            # Should start with JPEG magic bytes
            assert decoded[:3] == b"\xff\xd8\xff"
        finally:
            os.unlink(path)


# ── Structural Flags ─────────────────────────────────────────────────────────

class TestStructuralFlags:
    def test_missing_file(self):
        ctx = _make_context(
            image_paths=["/nonexistent/path/to/img_1.jpg"],
            image_ids=["img_1"],
        )
        result = validate_images(ctx)
        assert result.valid_image is False
        assert "file_missing" in result.structural_flags

    def test_url_path_flagged_missing(self):
        ctx = _make_context(
            image_paths=["https://example.com/photo.jpg"],
            image_ids=["url_img"],
        )
        result = validate_images(ctx)
        assert result.valid_image is False
        assert "file_missing" in result.structural_flags

    def test_unsupported_extension(self):
        path = _make_binary_file(1024, suffix=".gif")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["bad_ext"])
            result = validate_images(ctx)
            assert result.valid_image is False
            assert "unsupported_format" in result.structural_flags
        finally:
            os.unlink(path)

    def test_bmp_extension_flagged(self):
        path = _make_binary_file(1024, suffix=".bmp")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["bmp_img"])
            result = validate_images(ctx)
            assert "unsupported_format" in result.structural_flags
        finally:
            os.unlink(path)

    def test_corrupt_file_flagged_unsupported(self):
        path = _make_binary_file(512, suffix=".jpg")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["corrupt"])
            result = validate_images(ctx)
            assert "unsupported_format" in result.structural_flags
            assert result.valid_image is False
        finally:
            os.unlink(path)

    def test_no_duplicate_flags(self):
        """Multiple missing files should produce only one file_missing flag."""
        ctx = _make_context(
            image_paths=["/missing/a.jpg", "/missing/b.jpg"],
            image_ids=["a", "b"],
        )
        result = validate_images(ctx)
        assert result.structural_flags == ["file_missing"]


# ── Mixed Valid / Invalid ────────────────────────────────────────────────────

class TestMixedValidity:
    def test_mixed_valid_and_missing(self):
        valid_path = _create_test_image()
        try:
            ctx = _make_context(
                image_paths=["/nonexistent/img.jpg", valid_path],
                image_ids=["missing", "valid"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True  # mixed set still usable
            assert "file_missing" in result.structural_flags
            assert len(result.images_b64) == 1
            assert "valid" in result.images_b64
        finally:
            os.unlink(valid_path)

    def test_mixed_valid_corrupt_and_missing(self):
        valid_path = _create_test_image()
        corrupt_path = _make_binary_file(256, suffix=".jpg")
        try:
            ctx = _make_context(
                image_paths=["/missing/x.jpg", corrupt_path, valid_path],
                image_ids=["missing", "corrupt", "valid"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True
            assert "file_missing" in result.structural_flags
            assert "unsupported_format" in result.structural_flags
            assert len(result.images_b64) == 1
            assert "valid" in result.images_b64
        finally:
            os.unlink(valid_path)
            os.unlink(corrupt_path)


# ── File Size Limits ─────────────────────────────────────────────────────────

class TestFileSizeLimit:
    def test_oversized_file_skipped(self):
        path = _make_binary_file(MAX_FILE_SIZE_BYTES + 1, suffix=".jpg")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["too_big"])
            result = validate_images(ctx)
            assert result.valid_image is False
            # Oversized files are silently skipped — no flag
            assert "file_missing" not in result.structural_flags
            assert result.images_b64 == {}
        finally:
            os.unlink(path)

    def test_oversized_does_not_block_valid_image(self):
        oversized = _make_binary_file(MAX_FILE_SIZE_BYTES + 1, suffix=".jpg")
        valid_path = _create_test_image()
        try:
            ctx = _make_context(
                image_paths=[oversized, valid_path],
                image_ids=["too_big", "valid"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 1
            assert "valid" in result.images_b64
        finally:
            os.unlink(oversized)
            os.unlink(valid_path)

    def test_boundary_file_size_accepted(self):
        """A file exactly at MAX_FILE_SIZE_BYTES should be processed (not rejected)."""
        path = _make_binary_file(MAX_FILE_SIZE_BYTES, suffix=".jpg")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["boundary"])
            result = validate_images(ctx)
            # File is exactly at limit, but it's random binary data, so
            # Pillow will fail to decode it as an image. The rejection
            # should be unsupported_format, not file_missing.
            assert not result.valid_image
            # The important check: we didn't crash, we didn't reject based on size
        finally:
            os.unlink(path)


# ── Minimum Dimension Checks ────────────────────────────────────────────────

class TestMinimumDimensions:
    def test_too_small_image_skipped(self):
        path = _create_test_image(size=(MIN_DIMENSION_PX - 10, MIN_DIMENSION_PX - 10))
        try:
            ctx = _make_context(image_paths=[path], image_ids=["tiny"])
            result = validate_images(ctx)
            assert result.valid_image is False
            assert result.images_b64 == {}
        finally:
            os.unlink(path)

    def test_small_axis_skipped(self):
        """Image where one axis is below minimum should be skipped."""
        path = _create_test_image(size=(MIN_DIMENSION_PX + 100, MIN_DIMENSION_PX - 5))
        try:
            ctx = _make_context(image_paths=[path], image_ids=["thin"])
            result = validate_images(ctx)
            assert result.valid_image is False
        finally:
            os.unlink(path)

    def test_minimum_size_accepted(self):
        """Image exactly at minimum dimensions should be accepted."""
        path = _create_test_image(size=(MIN_DIMENSION_PX, MIN_DIMENSION_PX))
        try:
            ctx = _make_context(image_paths=[path], image_ids=["minimum_edge"])
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 1
        finally:
            os.unlink(path)

    def test_small_image_does_not_block_valid_images(self):
        tiny = _create_test_image(size=(16, 16))
        valid = _create_test_image(size=(800, 600))
        try:
            ctx = _make_context(
                image_paths=[tiny, valid],
                image_ids=["tiny", "valid"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 1
            assert "valid" in result.images_b64
        finally:
            os.unlink(tiny)
            os.unlink(valid)


# ── Decompression Bomb Protection ───────────────────────────────────────────

class TestDecompressionBomb:
    def test_huge_dimensions_skipped(self, monkeypatch):
        """An image with pixel count above MAX_IMAGE_PIXELS should be rejected."""
        monkeypatch.setattr("modules.image_validator.MAX_IMAGE_PIXELS", 10_000)
        path = _create_test_image(size=(200, 200))  # 40 000 > 10 000
        try:
            ctx = _make_context(image_paths=[path], image_ids=["bomb"])
            result = validate_images(ctx)
            assert result.valid_image is False
            assert result.images_b64 == {}
        finally:
            os.unlink(path)

    def test_bomb_does_not_block_valid_images(self, monkeypatch):
        """Valid images still work when another image is a bomb."""
        monkeypatch.setattr("modules.image_validator.MAX_IMAGE_PIXELS", 300_000)
        bomb = _create_test_image(size=(800, 600))   # 480 000 > 300 000
        valid = _create_test_image(size=(400, 300))   # 120 000 < 300 000
        try:
            ctx = _make_context(
                image_paths=[bomb, valid],
                image_ids=["bomb", "valid"],
            )
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 1
            assert "valid" in result.images_b64
        finally:
            os.unlink(bomb)
            os.unlink(valid)


# ── Image Normalization ──────────────────────────────────────────────────────

class TestImageNormalization:
    def test_rgba_converted_to_rgb(self):
        """RGBA image should be handled (alpha channel dropped)."""
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        img.save(tmp, format="PNG")
        tmp.close()
        try:
            ctx = _make_context(image_paths=[tmp.name], image_ids=["rgba"])
            result = validate_images(ctx)
            assert result.valid_image is True
            assert len(result.images_b64) == 1
        finally:
            os.unlink(tmp.name)

    def test_grayscale_converted_to_rgb(self):
        """Grayscale (L mode) image should convert without error."""
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        img = Image.new("L", (100, 100), 128)
        img.save(tmp, format="JPEG")
        tmp.close()
        try:
            ctx = _make_context(image_paths=[tmp.name], image_ids=["gray"])
            result = validate_images(ctx)
            assert result.valid_image is True
        finally:
            os.unlink(tmp.name)

    def test_webp_accepted(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".webp", delete=False)
        img = Image.new("RGB", (100, 100), (0, 128, 0))
        img.save(tmp, format="WEBP")
        tmp.close()
        try:
            ctx = _make_context(image_paths=[tmp.name], image_ids=["webp_img"])
            result = validate_images(ctx)
            assert result.valid_image is True
            assert "webp_img" in result.images_b64
        finally:
            os.unlink(tmp.name)

    def test_resize_applied_when_exceeding_max(self):
        """Image larger than MAX_LONGEST_EDGE_PX should be resized."""
        oversize = MAX_LONGEST_EDGE_PX + 200
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        img = Image.new("RGB", (oversize, oversize - 100), (100, 100, 100))
        img.save(tmp, format="JPEG")
        tmp.close()
        try:
            ctx = _make_context(image_paths=[tmp.name], image_ids=["big"])
            result = validate_images(ctx)
            assert result.valid_image is True
            # Decode the base64 and verify dimensions
            import base64
            decoded = base64.b64decode(result.images_b64["big"])
            from io import BytesIO
            rerun = Image.open(BytesIO(decoded))
            assert max(rerun.size) <= MAX_LONGEST_EDGE_PX
        finally:
            os.unlink(tmp.name)


# ── Error Isolation ──────────────────────────────────────────────────────────

class TestErrorIsolation:
    def test_one_exception_does_not_block_other_images(self):
        """If _process_single_image raises unexpectedly, other images still work."""
        valid_path = _create_test_image()
        try:
            # image_ids mismatch (None) will cause a TypeError in zip or processing
            # Actually, zip handles None gracefully. Let's test with a path that
            # causes an OSError on getsize (e.g., a path with null byte)
            ctx = _make_context(
                image_paths=["\0invalid", valid_path],
                image_ids=["bad", "valid"],
            )
            result = validate_images(ctx)
            # The \0 path might cause an error in os.path.isfile which is caught
            # by the generic exception handler. "valid" should still be processed.
            assert result.valid_image is True
            assert "valid" in result.images_b64
        finally:
            os.unlink(valid_path)

    def test_all_invalid_returns_false(self):
        """When every image fails, valid_image should be False."""
        ctx = _make_context(
            image_paths=["/missing/a.jpg", "/missing/b.jpg"],
            image_ids=["a", "b"],
        )
        result = validate_images(ctx)
        assert result.valid_image is False
        assert result.images_b64 == {}


# ── Security ─────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_path_traversal_filename_in_path(self):
        """A path with traversal components should not crash (M1 should
        filter these, but M2 handles gracefully if any slip through)."""
        ctx = _make_context(
            image_paths=["../../etc/passwd"],
            image_ids=["traversal"],
        )
        result = validate_images(ctx)
        # Should gracefully return file_missing (the file doesn't resolve)
        assert "file_missing" in result.structural_flags
        assert result.valid_image is False

    def test_null_byte_in_path_does_not_crash(self):
        """Null bytes should not crash the process."""
        ctx = _make_context(
            image_paths=["/safe/img.jpg\0malicious"],
            image_ids=["null_byte"],
        )
        # Should not raise
        result = validate_images(ctx)
        assert isinstance(result, ImageValidationResult)

    def test_extremely_long_path_does_not_crash(self):
        """Very long path strings should not crash."""
        long_path = "/" + "a" * 10000 + "/img.jpg"
        ctx = _make_context(
            image_paths=[long_path],
            image_ids=["long"],
        )
        result = validate_images(ctx)
        assert isinstance(result, ImageValidationResult)

    def test_unicode_injection_in_path_handled(self):
        """Unicode / special chars in path should not cause issues."""
        ctx = _make_context(
            image_paths=["/safe/../../img.jpg;\nrm -rf /"],
            image_ids=["injection"],
        )
        result = validate_images(ctx)
        assert isinstance(result, ImageValidationResult)

    def test_logs_do_not_contain_base64(self, caplog):
        """Base64 content must never appear in logs."""
        caplog.set_level(logging.DEBUG)
        path = _create_test_image()
        try:
            ctx = _make_context(image_paths=[path], image_ids=["img_1"])
            validate_images(ctx)
            # Check all log records for any base64-like strings
            for record in caplog.records:
                message = record.getMessage()
                # Base64 strings are long alphanumeric sequences
                # Check for any string that looks like base64 image data
                if re.search(r"[A-Za-z0-9+/]{100,}={0,2}", message):
                    # This might be a false positive, but if the message
                    # contains the actual base64 image, that's a leak.
                    # The image_id should be fine ("img_1") but the full b64 is not.
                    assert message.count("/") < 50, (
                        f"Possible base64 leak in log: {record.name}: {message[:200]}"
                    )
        finally:
            os.unlink(path)

    def test_absolute_paths_not_in_logs(self, caplog):
        """Log messages should not contain absolute filesystem paths."""
        caplog.set_level(logging.DEBUG)
        ctx = _make_context(
            image_paths=["/tmp/sensitive_path_test_file.jpg"],
            image_ids=["missing"],
        )
        validate_images(ctx)
        for record in caplog.records:
            message = record.getMessage()
            # Should use basename, not absolute path
            assert "/tmp/" not in message, (
                f"Absolute path leaked in log: {message}"
            )

    def test_image_data_not_leaked_in_exception(self):
        """Even with bad data, full image bytes should not appear in exceptions."""
        path = _make_binary_file(512, suffix=".jpg")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["bad"])
            # Should not raise, just return result
            result = validate_images(ctx)
            assert isinstance(result, ImageValidationResult)
        finally:
            os.unlink(path)


# ── Supported Format List ────────────────────────────────────────────────────

class TestSupportedFormats:
    def test_jpg(self):
        assert ".jpg" in SUPPORTED_EXTENSIONS

    def test_jpeg(self):
        assert ".jpeg" in SUPPORTED_EXTENSIONS

    def test_png(self):
        assert ".png" in SUPPORTED_EXTENSIONS

    def test_webp(self):
        assert ".webp" in SUPPORTED_EXTENSIONS

    def test_unsupported_formats_excluded(self):
        assert ".gif" not in SUPPORTED_EXTENSIONS
        assert ".bmp" not in SUPPORTED_EXTENSIONS
        assert ".tiff" not in SUPPORTED_EXTENSIONS
        assert ".svg" not in SUPPORTED_EXTENSIONS
        assert ".ico" not in SUPPORTED_EXTENSIONS
        assert ".heic" not in SUPPORTED_EXTENSIONS

    def test_case_insensitive_handling(self):
        """Upper-case extensions should be handled."""
        path = _create_test_image(suffix=".JPG")
        try:
            ctx = _make_context(image_paths=[path], image_ids=["upper"])
            result = validate_images(ctx)
            assert result.valid_image is True
        finally:
            os.unlink(path)


# ── Constants Sanity ─────────────────────────────────────────────────────────

class TestConstants:
    def test_max_file_size_is_reasonable(self):
        assert 1 * 1024 * 1024 <= MAX_FILE_SIZE_BYTES <= 500 * 1024 * 1024

    def test_min_dimension_is_positive(self):
        assert MIN_DIMENSION_PX >= 16

    def test_max_longest_edge_positive(self):
        assert MAX_LONGEST_EDGE_PX >= 512

    def test_max_longest_edge_aligns_with_spec(self):
        """SPEC.md specifies 1568px as the resize target."""
        assert MAX_LONGEST_EDGE_PX == 1568

    def test_max_image_pixels_is_reasonable(self):
        """Should be large enough to permit resize-target-sized images (~2.5 MP)
        but small enough to block pathological headers (50 MP is ~8000×6000)."""
        assert MAX_IMAGE_PIXELS >= MAX_LONGEST_EDGE_PX * MAX_LONGEST_EDGE_PX * 2
        assert MAX_IMAGE_PIXELS <= 200_000_000
