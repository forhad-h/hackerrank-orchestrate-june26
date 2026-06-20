"""
M2 — Image Validity Checker  (SPEC.md §5 M2)

Input:  ClaimContext
Output: ImageValidationResult (per claim, not per image)

Security & safety guards:
  - URL-scheme paths → flagged ``file_missing`` (no download attempted).
  - 20 MB file-size cap → silently skipped (decompression-bomb / OOM defence).
  - 64×64 minimum dimensions → silently skipped (no useful signal for VLM).
  - ~2.46 MP (1568²) maximum pixel count → silently skipped (tiny-header bomb).
  - Corrupt / truncated images → flagged ``unsupported_format``.
  - EXIF orientation stripped (auto-rotate) so the VLM always sees upright images.
  - EXIF metadata naturally removed during RGB→JPEG re-encode.
  - Absolute paths never written to logs — only basenames.

Per-image error isolation:
  One corrupt image does not block the other images in the same claim.

Key rules:
  - Base64-encode each valid image (convert to RGB, resize max 1568px longest edge).
  - valid_image=False only if ALL images are missing/invalid (mixed set still usable).
  - VLM quality flags (blurry_image, low_light_or_glare, etc.) are returned by M5.

╔═══════════════════════════════════════════════════════════════════════════════╗
║              DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS                   ║
╚═══════════════════════════════════════════════════════════════════════════════╝

────────────────────────────────────────────────────────────────────────────────
  1.  SILENT SKIP VS STRUCTURAL FLAGGING — QUALITY VS STRUCTURAL DEFECTS
────────────────────────────────────────────────────────────────────────────────

Decision
    File-size violations, undersized dimensions, and decompression bombs are
    silently dropped (no structural flag appended), while missing files and
    unsupported formats are flagged ``file_missing`` / ``unsupported_format``.

Rationale
    - Size/dimension issues are quality thresholds that a slightly different
      pipeline configuration (e.g., higher file cap, smaller min dimension)
      might accept.  They are not intrinsic defects in the image.
    - Missing files and format errors are structural problems the caller
      must know about — they indicate a failed upload or an invalid asset.

Trade-offs
    + Clear separation of concerns: structural issues are explicit in the
      output; quality issues are handled by downstream VLM risk_flags.
    - Silent skips can mask user error: "claimant submitted no images" and
      "all images silently dropped as too large" produce identical output.
    - No skipped_too_large flag exists, so the caller cannot distinguish
      the two cases.

Limitations
    - The threshold-based distinction is arbitrary at the edge: a 20.1 MB
      image is silently skipped, but a 19.9 MB image with a broken header
      is flagged ``unsupported_format``.
    - There is no per-image counter in ImageValidationResult for the number
      of silently dropped images.

Future Improvements
    - Add an optional skip_reasons field to ImageValidationResult, or
      emit a counter for dropped images that the Output Assembler can
      include in the CSV.

────────────────────────────────────────────────────────────────────────────────
  2.  valid_image = any(usable) — LOW BAR, BY DESIGN
────────────────────────────────────────────────────────────────────────────────

Decision
    ``valid_image=True`` is set if *at least one* image passes all structural
    checks, even when multiple others fail.  A claim that has a mix of valid
    and invalid images still proceeds to the VLM.

Rationale
    - This keeps the claim alive so the VLM can still make a best-effort
      assessment from whatever partial evidence is available.
    - A nearly-blank valid image (solid color, 64x64) passes the gate while
      a highly informative 63x63 image fails silently.  This asymmetry is
      acceptable because the downstream VLM and risk aggregator catch
      quality and content issues via risk_flags.

Trade-offs
    + Maximum recall: claims with partial evidence still get evaluated.
    + Consistent with the project's "graceful degradation" principle.
    - A single marginal image (e.g. 64x64 solid color) triggers a VLM API
      call that will almost certainly return not_enough_information.
    - No lower bound on image *quality* for triggering the VLM call.

Limitations
    - valid_image=True is a weak signal — it says "at least one file opened
      and was the right size," not "at least one image is informative."
    - The downstream M5 treats valid_image=True as a gate for evidence
      evaluation; false positives waste evaluation effort.

Future Improvements
    - Revisit if M5 starts treating valid_image=True as a strong signal
      of evidence quality.  Currently it only gates the evaluation start,
      so the low bar is appropriate.

────────────────────────────────────────────────────────────────────────────────
  3.  EXTENSION-BASED PRE-FILTER BEFORE PILLOW DECODE
────────────────────────────────────────────────────────────────────────────────

Decision
    Supported extensions (``.jpg``, ``.jpeg``, ``.png``, ``.webp``) are
    checked before any file I/O or Pillow decoding.  Unsupported extensions
    are flagged ``unsupported_format`` without attempting decode.

Rationale
    - Avoids expensive decode attempts on files that are obviously
      unsupported (``.gif``, ``.bmp``, ``.tiff``).
    - Keeps the fast path (check extension ~= 0.01 ms) much cheaper than
      the decode path (Pillow open ~= 10-100 ms).

Trade-offs
    + Fast pre-filter saves significant I/O for obviously unsupported files.
    + Simple, zero-dependency check.
    - A misnamed file (e.g. PNG saved as .jpg) passes the extension check
      but may or may not decode cleanly with Pillow.
    - Conversely, a .jpg renamed to .png is rejected even though Pillow
      could decode it.

Limitations
    - The extension list is hardcoded.  Adding a new format (e.g. ``.avif``)
      requires a code change.
    - Content-based MIME sniff (libmagic) would be more accurate but adds
      a system dependency and an extra file read per image.

Future Improvements
    - Switch to content sniffing if ingestion from untrusted sources
      (where misnamed files are expected) becomes a requirement.

────────────────────────────────────────────────────────────────────────────────
  4.  UNIFIED JPEG RE-ENCODE AT QUALITY 85
────────────────────────────────────────────────────────────────────────────────

Decision
    Every valid image is converted to RGB and saved as JPEG (quality 85)
    before base64 encoding, regardless of original format.

Rationale
    - Provides the VLM with a consistent input format across all images.
    - Strips EXIF metadata naturally (EXIF is not carried through Pillow's
      RGB-to-JPEG pipeline) — important for privacy.
    - JPEG at quality 85 produces smaller payloads than PNG, reducing
      VLM token counts and API costs.

Trade-offs
    + Consistent input format for the VLM across all image types.
    + Small payloads, lower API cost.
    - Already-compressed JPEG images incur generational loss from
      re-compression.
    - RGBA images lose their alpha channel; palette-indexed images are
      expanded to 24-bit RGB.
    - For damage assessment the quality loss at 85 is negligible, but it
      could obscure fine cracks or printed text.

Limitations
    - The re-encode is always JPEG quality 85 regardless of original
      format quality.  A high-quality PNG is degraded to JPEG compression.

Future Improvements
    - Consider quality=95 or passthrough for already-JPEG images if VLM
      providers charge significantly more per image token (larger JPEG
      = more tokens).
    - Consider WebP output if VLM providers support it at lower token
      counts.

────────────────────────────────────────────────────────────────────────────────
  5.  1568 PX LONGEST-EDGE RESIZE — TOKEN BUDGET VS SPATIAL DETAIL
────────────────────────────────────────────────────────────────────────────────

Decision
    Images larger than 1568 px on the longest edge are downscaled with
    Lanczos resampling before encoding.  This keeps the image at roughly
    2.46 MP, fitting in one VLM tile for most providers.

Rationale
    - Bounding image size keeps VLM API costs predictable by limiting the
      number of image tiles (each tile incurs a fixed token cost).
    - 1568 px is a reasonable middle ground: larger than most VLM minimums
      but small enough to avoid multi-tile pricing for typical photos.

Trade-offs
    + Predictable token usage per image (~1 tile for most providers).
    + Lanczos resampling preserves edge sharpness well.
    - Fine-grained damage (small cracks, scratches, serial numbers, text)
      can become unreadable after downscaling.
    - 1568 px is optimised for current provider pricing; a change in
      tile-size pricing would require revisiting this value.

Limitations
    - The resize is applied uniformly — it does not adapt to image content
      (e.g., keeping a region of interest at higher resolution).
    - Lanczos is computationally heavier than bilinear or nearest-neighbour
      (negligible at 50 images, matters at 1000+).

Future Improvements
    - Revisit if the VLM provider changes its tile-size pricing.
    - Consider content-adaptive downscaling (detect damage regions and
      keep them at higher resolution).

────────────────────────────────────────────────────────────────────────────────
  6.  EXIF ORIENTATION STRIPPING — NORMALISATION VS DATA LOSS
────────────────────────────────────────────────────────────────────────────────

Decision
    ``ImageOps.exif_transpose()`` applies the EXIF Orientation tag before
    the VLM sees the image, so mobile-phone photos (which commonly embed
    a rotation tag) always appear upright.  The re-encode strips all EXIF
    metadata (camera model, GPS, timestamp).

Rationale
    - Mobile-phone photos with EXIF rotation tags would appear rotated
      to the VLM if not transposed, causing incorrect orientation analysis.
    - Privacy: an evidence review system should not leak geotags, camera
      model, or timestamps to the VLM provider.
    - Stripping EXIF during the RGB-to-JPEG conversion is free (Pillow
      does not carry EXIF through the convert+save pipeline).

Trade-offs
    + Consistent orientation — all images appear upright to the VLM.
    + Privacy — no geotag, timestamp, or camera-model leakage.
    - The VLM cannot use camera metadata (e.g. flash vs no flash, focal
      length) as an additional analytical signal.
    - EXIF creator/copyright information is also stripped, which could
      matter in a forensic context.

Limitations
    - exif_transpose() returns None if no EXIF orientation tag is present;
      the code handles this correctly.
    - EXIF is stripped during re-encode for all images, not just those
      with orientation tags.

Future Improvements
    - Consider a per-claim privacy setting that controls whether EXIF is
      stripped (e.g., "trusted" sources retain EXIF for forensic analysis).

────────────────────────────────────────────────────────────────────────────────
  7.  PER-IMAGE EXCEPTION ISOLATION — RESILIENCE VS BUG VISIBILITY
────────────────────────────────────────────────────────────────────────────────

Decision
    ``_process_single_image`` is called inside a generic try/except in
    ``validate_images()``.  An unexpected exception on one image does not
    abort processing of the remaining images in the same claim.

Rationale
    - A single corrupt file (OSError, Pillow bug, memory error) should
      not discard the entire claim's evidence.
    - Batch evaluation processes hundreds of claims; one flaky image
      should not lose results for every other image in the same row.

Trade-offs
    + Pipeline resilience: one corrupt image never blocks others.
    + The exception is logged with full stack trace for debugging.
    - Broad exception swallowing masks bugs during development.  A new
      code path that always raises on a specific condition may go
      unnoticed for days.
    - The remaining images continue processing normally, creating a
      potentially misleading partial result.

Limitations
    - Exception is caught generically; KeyboardInterrupt and SystemExit
      are also swallowed (though unlikely in normal usage).
    - No dev-mode strict flag exists to re-raise exceptions for debugging.

Future Improvements
    - Add a dev-mode strict flag (env var) that re-raises exceptions
      instead of swallowing them, for use during development.

────────────────────────────────────────────────────────────────────────────────
  8.  _safe_path() — SECURITY VS DEBUGGABILITY
────────────────────────────────────────────────────────────────────────────────

Decision
    The ``_safe_path`` helper extracts only ``os.path.basename()`` for log
    messages.  Absolute filesystem paths are never written to logs.

Rationale
    - Logs may be submitted as part of the challenge transcript or shared
      for debugging.  Absolute paths can leak temporary-directory
      structures, user IDs embedded in file paths, or internal directory
      layouts.
    - Basenames are sufficient for cross-referencing with the original
      input data (the CSV image_paths column).

Trade-offs
    + No sensitive path information leaks into logs.
    + Consistent with the project's security-first approach.
    - When debugging a "file not found" error, the log shows only the
      basename, making it impossible to determine which subdirectory was
      searched.
    - Correlating log entries with specific input rows requires manual
      matching of basenames (not unique across directories).

Limitations
    - Only path values are redacted.  Other sensitive data in the image
      (PII in the image content itself) is not redacted by this helper.
    - The helper only covers log messages from this module; other modules
      have their own logging conventions.

Future Improvements
    - Augment with a once-per-session logged warning that states the image
      search root, keeping per-image logs path-free while providing
      debugging context.

────────────────────────────────────────────────────────────────────────────────
  9.  URL REGEX DUPLICATION — DEFENCE IN DEPTH
────────────────────────────────────────────────────────────────────────────────

Decision
    Both M1 (``data_loader.py``) and M2 define the same ``URL_SCHEME_RE``
    pattern to detect network paths.  M1 filters URLs during path
    resolution; M2 re-checks as a defence-in-depth measure.

Rationale
    - If the pipeline is used with a custom DataLoader implementation that
      does not filter URLs, M2 still catches them before attempting file I/O.
    - The regex is small (one line) and unlikely to change; duplication is
      a minor maintenance hazard.

Trade-offs
    + Defence in depth: a misconfigured or custom M1 does not expose M2
      to network paths.
    + Each module is independently secure — M2's URL guard works even in
      isolation.
    - The regex is duplicated, creating a maintenance hazard if the scheme
      list needs updating (e.g. adding ``ftp://``).

Limitations
    - Only URL schemes are caught.  Other non-file paths (e.g. Windows
      named pipes ``\\.\pipe\``) are not checked.
    - The regex is not configurable; adding a custom scheme requires
      editing both modules.

Future Improvements
    - Centralise the regex in ``models.py`` or a shared ``constants.py``
      so that updates propagate to all consumers automatically.  This is
      a minor refactoring pending a dedicated constants module.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from typing import Dict, List, Optional

from PIL import Image, UnidentifiedImageError, ImageOps

from modules.models import ClaimContext, ImageValidationResult

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS: set = {".jpg", ".jpeg", ".png", ".webp"}

#: Maximum allowed file size in bytes.  Images larger than this are rejected
#: before any decoding attempt to prevent memory exhaustion.
MAX_FILE_SIZE_BYTES: int = 20 * 1024 * 1024  # 20 MB

#: Minimum allowed dimension on both axes.  Images below this threshold have
#: insufficient visual information for any meaningful damage assessment.
MIN_DIMENSION_PX: int = 64

#: Longest-edge resize target for VLM submission (controls token usage).
MAX_LONGEST_EDGE_PX: int = 1568

#: Maximum total pixel count.  Beyond this an image is considered a
#: decompression bomb (tiny file header → huge memory on decode).  Set to
#: 50 MP (~8000 × 6000), well beyond any plausible consumer camera, but
#: still protective against absurdly large headers (e.g. 100 000 × 100 000).
MAX_IMAGE_PIXELS: int = 50_000_000

#: Regex matching common URL schemes — used to detect non-file paths early.
URL_SCHEME_RE: re.Pattern = re.compile(r"^[a-z][a-z0-9+\-.]*://", re.IGNORECASE)


# ── Public API ───────────────────────────────────────────────────────────────


def validate_images(context: ClaimContext) -> ImageValidationResult:
    """Validate and optionally base64-encode all images for a single claim.

    Parameters
    ----------
    context :
        Fully hydrated claim context from M1 (uses ``image_paths`` and
        ``image_ids``; other fields are unused here).

    Returns
    -------
    ImageValidationResult
        Per-claim aggregate of structural validity and base64-encoded images.
    """
    structural_flags: List[str] = []
    images_b64: Dict[str, str] = {}

    for path, image_id in zip(context.image_paths, context.image_ids):
        try:
            result = _process_single_image(path, image_id, structural_flags)
            if result is not None:
                images_b64[image_id] = result
        except Exception:
            logger.exception(
                "Unexpected error processing image %s (%s)",
                image_id,
                _safe_path(path),
            )

    # Deduplicate while preserving insertion order.
    structural_flags = list(dict.fromkeys(structural_flags))

    return ImageValidationResult(
        valid_image=len(images_b64) > 0,
        structural_flags=structural_flags,
        images_b64=images_b64,
    )


# ── Per-Image Processing ────────────────────────────────────────────────────


def _process_single_image(
    path: str,
    image_id: str,
    structural_flags: List[str],
) -> Optional[str]:
    """Validate and encode one image.

    Parameters
    ----------
    path :
        Absolute path to the image file (or a URL string forwarded from M1).
    image_id :
        Filename stem used as the key in ``images_b64``.
    structural_flags :
        Mutable list; known structural issues are appended as side effects.

    Returns
    -------
    Optional[str]
        Base64-encoded JPEG string, or ``None`` if the image is unusable.
    """
    # ── URL guard ───────────────────────────────────────────────────────
    if URL_SCHEME_RE.match(path):
        logger.debug("URL path (cannot fetch) — marking missing: %s", image_id)
        structural_flags.append("file_missing")
        return None

    # ── File existence ──────────────────────────────────────────────────
    if not os.path.isfile(path):
        logger.debug("File not found: %s (%s)", image_id, _safe_path(path))
        structural_flags.append("file_missing")
        return None

    # ── File size guard ─────────────────────────────────────────────────
    try:
        file_size = os.path.getsize(path)
    except OSError:
        logger.debug("Cannot read file size: %s (%s)", image_id, _safe_path(path))
        structural_flags.append("file_missing")
        return None

    if file_size > MAX_FILE_SIZE_BYTES:
        logger.debug(
            "File too large (%d bytes): %s (%s)",
            file_size,
            image_id,
            _safe_path(path),
        )
        return None

    # ── Extension-based format check ────────────────────────────────────
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.debug("Unsupported format '%s' for image %s", ext, image_id)
        structural_flags.append("unsupported_format")
        return None

    # ── Pillow decode ───────────────────────────────────────────────────
    img: Optional[Image.Image] = None
    try:
        img = Image.open(path)
    except (UnidentifiedImageError, OSError) as e:
        logger.debug(
            "Cannot decode image %s (%s): %s",
            image_id,
            _safe_path(path),
            e,
        )
        structural_flags.append("unsupported_format")
        return None

    try:
        # ── Dimension checks ────────────────────────────────────────────
        width, height = img.size

        if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
            logger.debug(
                "Image too small (%dx%d): %s",
                width,
                height,
                image_id,
            )
            return None

        if width * height > MAX_IMAGE_PIXELS:
            logger.debug(
                "Image exceeds max pixel count (%dx%d = %d): %s",
                width,
                height,
                width * height,
                image_id,
            )
            return None

        # ── Normalize ───────────────────────────────────────────────────
        # Auto-rotate based on EXIF orientation tag so the VLM sees the
        # image upright (critical for mobile-photo claims).
        rotated = ImageOps.exif_transpose(img)
        if rotated is not None:
            img = rotated

        # Convert to RGB — handles RGBA (drop alpha), CMYK, grayscale,
        # and palette-indexed colour spaces in one call.
        img = img.convert("RGB")

        # Resize longest edge to stay within VLM token budget.
        if max(img.size) > MAX_LONGEST_EDGE_PX:
            img.thumbnail((MAX_LONGEST_EDGE_PX, MAX_LONGEST_EDGE_PX), Image.LANCZOS)

        # ── Base64 encode ───────────────────────────────────────────────
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        return b64

    finally:
        if img is not None:
            img.close()


# ── Logging Helpers ─────────────────────────────────────────────────────────


def _safe_path(path: str) -> str:
    """Return only the filename for logging (never log absolute paths)."""
    return os.path.basename(path)


# ── Exports ──────────────────────────────────────────────────────────────────

__all__ = [
    "MAX_FILE_SIZE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_LONGEST_EDGE_PX",
    "MIN_DIMENSION_PX",
    "SUPPORTED_EXTENSIONS",
    "validate_images",
]
