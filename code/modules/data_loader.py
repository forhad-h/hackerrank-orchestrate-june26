"""
M1 — Data Ingestion & Context Loader  (SPEC.md §5 M1)

Input:  local file paths (claims CSV, user_history CSV, evidence_requirements CSV,
        image directory)
Output: List[ClaimContext]

Scope — local files only
────────────────────────
All inputs MUST be locally available on disk before this module is called.
This loader does NOT fetch remote resources:
  - HTTP/HTTPS URLs in image_path columns are NOT downloaded; they are flagged
    as file_missing and a warning is logged.
  - No S3, GCS, or other object-storage downloads.
  - No API calls to retrieve records.

How the dataset was originally produced is irrelevant. The only contract is:
CSVs and images exist under dataset/ before main.py is invoked.

Extensibility
─────────────
DataLoader is an abstract base class. The concrete implementation here is
LocalCSVDataLoader. To support a new input source (database, API, cloud
bucket), subclass DataLoader and implement load() returning List[ClaimContext].
No other module needs to change.

Key rules (LocalCSVDataLoader)
──────────────────────────────
- Resolve image paths relative to DATASET_DIR (default: <repo_root>/dataset/).
- Parse image_paths by splitting on ";" after normalising "," and "\\n" to ";".
- Reject any path that starts with a URL scheme (http://, https://, s3://, etc.);
  log a warning and keep the path so M2 can flag it file_missing.
- Reject any path whose resolved absolute target lies outside
  DATASET_DIR/images/ (path traversal guard).
- image_id = filename stem (e.g. "img_1") without extension.
- If user_id not in user_history.csv return UserHistory with all counts=0,
  history_flags="none".
- Filter evidence rules to claim_object + "all" rows.
- Maximum of 20 images per claim (configurable via MAX_IMAGES_PER_CLAIM).

╔═══════════════════════════════════════════════════════════════════════════════╗
║              DESIGN DECISIONS, TRADE-OFFS, AND LIMITATIONS                   ║
╚═══════════════════════════════════════════════════════════════════════════════╝

────────────────────────────────────────────────────────────────────────────────
  1.  MONOLITHIC ClaimContext — COUPLING VS SIMPLICITY
────────────────────────────────────────────────────────────────────────────────

Decision
    All downstream modules (M2-M7) receive the same ClaimContext object
    containing every field loaded from the CSV, image paths, user history,
    and evidence rules.

Rationale
    - A single context object makes the data flow easy to trace: one
      constructor, one type for every module input.
    - Changing the schema requires editing one dataclass rather than N
      per-module interfaces.
    - Acceptable for hackathon scope (~44 rows, 9 fields); simplicity
      wins over strict ISP compliance.

Trade-offs
    + Trivially debuggable -- every module has the full claim at hand.
    + Adding a new field is a single-point change.
    - Violates the Interface Segregation Principle: M7 receives raw
      image paths it never uses, and M5 receives user_history it ignores.
    - A schema change requires auditing all 8 consumers, even if only
      1 is affected.

Limitations
    - No type-level guarantee that a module only accesses the fields it
      needs.  A future refactoring that removes a field from ClaimContext
      may break a module that was silently depending on it.
    - Serialization (e.g., caching ClaimContext to disk) carries all
      fields, wasting space on unused data.

Future Improvements
    - Split into role-specific view objects (ImageContext for M2,
      ClaimTextContext for M3, etc.) composed from a common base.
    - Use @dataclass with field(repr=False) for large fields
      (like evidence_rules) to keep debug output readable.

────────────────────────────────────────────────────────────────────────────────
  2.  EAGER IMAGE-PATH RESOLUTION -- DETERMINISTIC BUT LOCAL-ONLY
────────────────────────────────────────────────────────────────────────────────

Decision
    Image paths are resolved to absolute filesystem paths at load time
    via os.path.realpath().  URL-scheme paths (http://, s3://) are
    rejected with a logged warning; no download is attempted.

Rationale
    - The project contract specifies that all inputs are available locally
      before the pipeline starts.  Adding network I/O would introduce
      latency, failure modes (timeouts, DNS resolution), and security
      surface area (SSRF, malicious file download).
    - Resolving paths eagerly makes the module deterministic, testable,
      and side-effect free -- critical for reproducible evaluation.

Trade-offs
    + Deterministic: same CSV always produces identical ClaimContext list.
    + Zero runtime I/O -- no network, no latency, no rate limits.
    - Cannot ingest from remote sources (cloud buckets, HTTP endpoints)
      without a pre-fetch step outside this module.
    - The URL rejection loses the information about where the file
      should be fetched from; M2 only sees the path, not the source URI.

Limitations
    - If the evaluator streams images on demand (rather than pre-fetching),
      the caller must implement that streaming outside this module.
    - The path-traversal guard (os.path.realpath inside images/) works
      only on Unix-like systems; Windows path resolution differs.

Future Improvements
    - Abstract the path-resolution strategy behind an interface so a
      "remote-aware" variant can be swapped in without changing the rest
      of M1.

────────────────────────────────────────────────────────────────────────────────
  3.  SILENT claim_object COERCION -- GRACEFUL BUT HIDES TYPOS
────────────────────────────────────────────────────────────────────────────────

Decision
    An unrecognised claim_object value (e.g. "car " with trailing space,
    or "Car" with wrong case) is silently coerced to "unknown".  A warning
    is logged, but no signal reaches output.csv.

Rationale
    - The dataset may contain typos or case variations.  Crashing on a
      typo would lose the entire row's data, which is worse than coercing
      to a safe default.
    - "unknown" matches only generic evidence rules (REQ_GENERAL_*), so
      the claim still receives evaluation rather than being skipped.

Trade-offs
    + Pipeline continues -- every claim produces an output row.
    + The "unknown" object still gets generic evidence rules.
    - A typo like "car " (trailing space) produces a false-negative verdict
      without an obvious indicator in the CSV output.
    - Operators must grep WARNING log lines to detect the issue.

Limitations
    - Only claim_object is coerced.  A mis-typed image_paths column name
      silently results in empty image lists, not a coercion.
    - The coercion decision is made once per row; there is no downstream
      retry or correction mechanism.

Future Improvements
    - Normalise claim_object (strip, lowercase) before the enum check so
      that "car ", "Car", and "CAR" all map to "car".
    - Add a coerced_claim_object counter logged at the end of each run.

────────────────────────────────────────────────────────────────────────────────
  4.  HARD 20-IMAGE CAP -- PROTECTS BUDGET BUT TRUNCATES SILENTLY
────────────────────────────────────────────────────────────────────────────────

Decision
    MAX_IMAGES_PER_CLAIM = 20 caps the number of image paths per claim.
    Excess paths beyond 20 are silently truncated with only a log warning.

Rationale
    - Each image consumes VLM tokens and memory.  500 images would exhaust
      the context window and crash the VLM call.
    - The 20-image cap is generous for real-world claims (typical: 2-6)
      while preventing pathological cases.
    - Truncation preserves the first 20 images, which are most likely
      relevant.

Trade-offs
    + Prevents memory exhaustion and excessive VLM spending.
    + Simple, constant-memory data structure.
    - If the 21st+ image contains the only evidence of damage, it is
      silently lost.  No indicator in the output.
    - Claim-agnostic: a legitimate claim with 22 photos of different
      angles loses 2 images unfairly.

Limitations
    - Cap is applied at load time, not on actual image validity.  If
      images 1-20 are all missing, the cap has already been applied.
    - MAX_IMAGES_PER_CLAIM is a module-level constant; no per-run
      configuration override.

Future Improvements
    - Add a truncated_count field to ClaimContext so downstream modules
      can surface image truncation in the CSV output.
    - Consider a dynamic cap based on total estimated VLM tokens rather
      than a raw count.

────────────────────────────────────────────────────────────────────────────────
  5.  ALL-AT-ONCE LOADING -- SIMPLE BUT NOT INCREMENTAL
────────────────────────────────────────────────────────────────────────────────

Decision
    load() reads the entire dataset into memory (List[ClaimContext])
    before returning.  Processing happens in batch after all contexts are
    built.

Rationale
    - For the hackathon dataset size (~100-200 rows, ~500 KB), loading
      everything is fast, simple, and uses negligible memory.
    - The caller receives a complete list and can iterate, shuffle, split,
      or inspect it without worrying about generator state.
    - Batch loading allows parallel downstream processing without
      coordinating a shared generator.

Trade-offs
    + Simple API -- len(contexts) tells you exactly how many claims to
      expect.
    + Supports random access and reordering for evaluation.
    - Does not scale to hundreds of thousands of claims.
    - Pipeline startup latency increases with dataset size.

Limitations
    - A single claim with extremely large user_claim text still occupies
      memory for the entire pipeline run.
    - If the CSV contains encoding errors at row 199, the error only
      surfaces after rows 1-198 have been loaded.

Future Improvements
    - Add a load_lazy() generator yielding ClaimContext one-at-a-time
      for memory-constrained environments.

────────────────────────────────────────────────────────────────────────────────
  6.  EVIDENCE-RULE FILTERING IN M1 -- CONVENIENCE VS SEPARATION
────────────────────────────────────────────────────────────────────────────────

Decision
    Evidence rules are filtered by claim_object (plus the "all" catch-all)
    here in M1 rather than in M5 (Evidence Evaluator) where rules are
    consumed.

Rationale
    - Filtering early reduces data carried through M2-M4 in the
      ClaimContext.evidence_rules list.
    - M1 has access to claim_object after parsing; filtering here
      leverages data already in hand.
    - Keeps M5 focused on evaluation logic rather than filtering.

Trade-offs
    + Less data flows through intermediate modules.
    + M5's logic is simpler (iterates only applicable rules).
    - If cross-object rule analysis is needed, the unfiltered set is
      already discarded.
    - If filtering rules change, M1 must be updated even though the
      change is conceptually M5's concern.

Limitations
    - The "all" catch-all is applied here -- any new rule with
      claim_object="all" is automatically included.
    - Filtering is by exact string match; "CAR" (uppercase) matches
      nothing, mitigated by the coercion in Decision 3.

Future Improvements
    - Move filtering to M5 if evidence rules become dynamic or
      claim_object relationships grow complex.
    - Add lazy-loading for evidence rules if serializing the full set
      becomes a memory concern.

────────────────────────────────────────────────────────────────────────────────
  7.  MINIMAL INPUT VALIDATION -- FAST BUT FRAGILE ON BAD CSVs
────────────────────────────────────────────────────────────────────────────────

Decision
    There is no schema-level guard at the CSV boundary.  Missing columns
    surface as KeyError or type mismatch at the point of access rather
    than as a clear diagnostic at load time.

Rationale
    - Full schema validation would require defining column types, optional
      values, and cross-column constraints -- significant code for one-off
      ingestion.
    - The hackathon datasets are assumed well-formed.  Adding defensive
      validation would slow development velocity.

Trade-offs
    + Fast CSV parsing with no validation overhead.
    + Simple code -- no schema definition, no validation functions.
    - A renamed column (e.g. image_path vs image_paths) crashes with
      confusing KeyError, not a clear message.
    - The missing-column check in _parse_csv logs a warning but does not
      prevent downstream crashes.

Limitations
    - No type coercion at the boundary: integer columns read as strings
      pass through without conversion.
    - The _safe_int() helper converts per-field at access time, but this
      is ad-hoc rather than systematic.

Future Improvements
    - Add a 20-line required-columns assertion at the top of load() that
      fails early with a clear error message.
    - Use csv.Sniffer to auto-detect delimiters for non-standard CSVs.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set, Tuple

from modules.models import (
    CLAIM_OBJECT_VALUES,
    ClaimContext,
    EvidenceRule,
    UserHistory,
)

logger = logging.getLogger(__name__)

# ── Path Resolution ────────────────────────────────────────────────────────

def _find_repo_root() -> str:
    """Return the absolute path of the repository root (parent of ``code/``).

    Walks up from this module's location:
      ``modules/data_loader.py`` → ``code/`` → repo root
    """
    this_file = os.path.abspath(__file__)          # …/code/modules/data_loader.py
    modules_dir = os.path.dirname(this_file)        # …/code/modules/
    code_dir = os.path.dirname(modules_dir)         # …/code/
    return os.path.dirname(code_dir)                # …/ (repo root)


_REPO_ROOT = _find_repo_root()

#: Resolved absolute path to the dataset directory.
#:
#: Set via the ``DATASET_DIR`` env var (absolute or relative); defaults to
#: ``<REPO_ROOT>/dataset/``.  Relative paths are resolved from the repo root.
#: The value is **computed once at import time** and used as the single source
#: of truth by every method in this module.
_DATASET_DIR_RAW = os.environ.get("DATASET_DIR", os.path.join(_REPO_ROOT, "dataset"))

if not os.path.isabs(_DATASET_DIR_RAW):
    DATASET_DIR = os.path.join(_REPO_ROOT, _DATASET_DIR_RAW)
else:
    DATASET_DIR = _DATASET_DIR_RAW

DATASET_DIR = os.path.realpath(DATASET_DIR)

# ── Validation Constants ──────────────────────────────────────────────────

REQUIRED_CSV_COLUMNS: Dict[str, Set[str]] = {
    "claims": {"user_id", "image_paths", "user_claim", "claim_object"},
    "user_history": {
        "user_id",
        "past_claim_count",
        "accept_claim",
        "manual_review_claim",
        "rejected_claim",
        "last_90_days_claim_count",
        "history_flags",
        "history_summary",
    },
    "evidence_requirements": {
        "requirement_id",
        "claim_object",
        "applies_to",
        "minimum_image_evidence",
    },
}

#: Maximum number of images allowed per claim.  Beyond this, excess paths
#: are silently truncated (a warning is logged).  Prevents memory exhaustion
#: from claims with thousands of image references.
MAX_IMAGES_PER_CLAIM = 20

#: Regex that matches common URL schemes.  Used to reject remote-resource
#: paths that this loader (by design) cannot fetch.
URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+\-.]*://", re.IGNORECASE)

# ── Abstract Base ──────────────────────────────────────────────────────────


class DataLoader(ABC):
    """Abstract base for data ingestion.

    Subclass to support different input sources (local CSVs, database, API,
    cloud bucket).  Every subclass must return ``List[ClaimContext]`` so
    downstream modules (M2–M7) work unchanged.
    """

    @abstractmethod
    def load(self) -> List[ClaimContext]:
        """Load and return all claim contexts."""


# ── Concrete: Local CSV Loader ─────────────────────────────────────────────


class LocalCSVDataLoader(DataLoader):
    """Read claims, user history, and evidence requirements from local CSV files.

    Parameters
    ----------
    claims_path :
        Path to the claims CSV.  Default: ``<DATASET_DIR>/claims.csv``.
    user_history_path :
        Path to the user history CSV.  Default: ``<DATASET_DIR>/user_history.csv``.
    evidence_requirements_path :
        Path to the evidence requirements CSV.
        Default: ``<DATASET_DIR>/evidence_requirements.csv``.
    """

    def __init__(
        self,
        claims_path: Optional[str] = None,
        user_history_path: Optional[str] = None,
        evidence_requirements_path: Optional[str] = None,
    ) -> None:
        self.claims_path = claims_path or os.path.join(DATASET_DIR, "claims.csv")
        self.user_history_path = (
            user_history_path or os.path.join(DATASET_DIR, "user_history.csv")
        )
        self.evidence_requirements_path = (
            evidence_requirements_path
            or os.path.join(DATASET_DIR, "evidence_requirements.csv")
        )
        self._images_base = os.path.join(DATASET_DIR, "images")

    # ── Public API ──────────────────────────────────────────────────────

    def load(self) -> List[ClaimContext]:
        """Load all claim contexts from the configured CSV files.

        Returns
        -------
        List[ClaimContext]
            Fully hydrated claim contexts, one per row in ``claims.csv``.

        Raises
        ------
        FileNotFoundError
            If the claims CSV does not exist.
        """
        if not os.path.isfile(self.claims_path):
            raise FileNotFoundError(
                f"Claims file not found: {self.claims_path}. "
                "Set DATASET_DIR if your dataset is in a non-default location."
            )

        claims_rows = self._read_csv(self.claims_path, "claims", required=True)
        if not claims_rows:
            logger.warning("Claims file is empty: %s", self.claims_path)
            return []

        history_map = self._read_user_history()
        all_rules = self._read_evidence_rules()

        contexts: List[ClaimContext] = []
        row_errors = 0
        for i, row in enumerate(claims_rows, start=1):
            try:
                context = self._build_context(row, history_map, all_rules)
                contexts.append(context)
            except Exception:
                row_errors += 1
                logger.exception(
                    "Failed to build context for row %d (user_id=%s)",
                    i,
                    row.get("user_id", "?"),
                )

        logger.info(
            "Loaded %d claim(s) from %s (%d row(s) skipped due to errors).",
            len(contexts),
            self.claims_path,
            row_errors,
        )
        return contexts

    # ── CSV Reading ─────────────────────────────────────────────────────

    def _read_csv(
        self,
        path: str,
        label: str,
        required: bool = False,
    ) -> List[dict]:
        """Read a CSV file and return rows as a list of dicts.

        Parameters
        ----------
        path :
            Absolute path to the CSV file.
        label :
            Human-readable name for log messages (e.g. ``"claims"``).
        required :
            If ``True`` and the file is missing, raise ``FileNotFoundError``.
            If ``False`` and the file is missing, log a warning and return ``[]``.

        Returns
        -------
        List[dict]
            Rows with whitespace-stripped keys and values.  May be empty.
        """
        if not os.path.isfile(path):
            if required:
                raise FileNotFoundError(f"Required file not found: {path}")
            logger.warning("Optional file not found, skipping: %s", path)
            return []

        # Try common encodings in order.  utf-8-sig handles BOM; latin-1 is a
        # fallback for files that contain raw bytes outside the UTF-8 range
        # (e.g. certain Windows-generated CSVs).
        encodings = ["utf-8-sig", "utf-8", "latin-1"]
        last_error: Optional[Exception] = None
        for enc in encodings:
            try:
                return self._parse_csv(path, enc, label)
            except UnicodeDecodeError as e:
                last_error = e
                continue

        logger.error(
            "Could not decode %s with any supported encoding (%s).",
            path,
            ", ".join(encodings),
        )
        if last_error:
            raise last_error  # type: ignore[union-attr]
        return []

    def _parse_csv(self, path: str, encoding: str, label: str) -> List[dict]:
        """Open, validate, and parse a CSV into a list of dicts."""
        with open(path, encoding=encoding, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                logger.warning("Empty CSV file: %s", path)
                return []

            # Optional: warn about missing columns
            expected_cols = REQUIRED_CSV_COLUMNS.get(label)
            if expected_cols is not None:
                actual_cols = {c.strip() for c in reader.fieldnames}
                missing = expected_cols - actual_cols
                if missing:
                    logger.warning(
                        "File %s is missing expected column(s): %s. "
                        "Proceeding with available columns.",
                        path,
                        ", ".join(sorted(missing)),
                    )

            rows: List[dict] = []
            for row in reader:
                # Normalise: strip whitespace from every key and string value.
                cleaned: dict = {}
                for k, v in row.items():
                    key = k.strip() if k else ""
                    cleaned[key] = v.strip() if isinstance(v, str) else v
                rows.append(cleaned)
            return rows

    # ── User History ────────────────────────────────────────────────────

    def _read_user_history(self) -> Dict[str, UserHistory]:
        """Load user history into a ``user_id → UserHistory`` map.

        Malformed rows are skipped with a warning.
        """
        rows = self._read_csv(self.user_history_path, "user_history")
        history_map: Dict[str, UserHistory] = {}
        for row in rows:
            uid = row.get("user_id", "").strip()
            if not uid:
                continue
            try:
                history_map[uid] = UserHistory(
                    past_claim_count=_safe_int(row, "past_claim_count"),
                    accept_claim=_safe_int(row, "accept_claim"),
                    manual_review_claim=_safe_int(row, "manual_review_claim"),
                    rejected_claim=_safe_int(row, "rejected_claim"),
                    last_90_days_claim_count=_safe_int(
                        row, "last_90_days_claim_count"
                    ),
                    history_flags=(row.get("history_flags") or "none").strip(),
                    history_summary=(row.get("history_summary") or "").strip(),
                )
            except (ValueError, TypeError) as e:
                logger.warning(
                    "Skipping malformed user_history row for user_id=%s: %s", uid, e
                )
        logger.info("Loaded %d user history record(s).", len(history_map))
        return history_map

    # ── Evidence Rules ──────────────────────────────────────────────────

    def _read_evidence_rules(self) -> List[EvidenceRule]:
        """Load all evidence requirements from the CSV.

        Malformed rows are skipped with a warning.
        """
        rows = self._read_csv(
            self.evidence_requirements_path, "evidence_requirements"
        )
        rules: List[EvidenceRule] = []
        for row in rows:
            try:
                rules.append(
                    EvidenceRule(
                        requirement_id=(
                            row.get("requirement_id") or ""
                        ).strip(),
                        claim_object=(row.get("claim_object") or "").strip(),
                        applies_to=(row.get("applies_to") or "").strip(),
                        minimum_image_evidence=(
                            row.get("minimum_image_evidence") or ""
                        ).strip(),
                    )
                )
            except Exception as e:
                logger.warning("Skipping malformed evidence rule row: %s", e)
        return rules

    # ── Image Path Handling ─────────────────────────────────────────────

    def _normalize_and_split_image_paths(self, raw: str) -> List[str]:
        """Normalise separators and split ``image_paths`` into individual paths.

        Currently the dataset uses ``;`` as the separator, but in the wild
        paths may arrive delimited by ``,`` or ``\\n`` (or mixed).  This method
        collapses all known alternative separators into ``;`` before splitting
        so that all three cases are handled transparently.

        Returns a list of non-empty, stripped path strings.
        """
        if not raw:
            return []
        # Collapse known alternative separators into semicolon
        normalized = re.sub(r"[,\n\r]+", ";", raw)
        parts = [p.strip() for p in normalized.split(";")]
        return [p for p in parts if p]

    def _resolve_image_path(self, raw_path: str) -> Optional[str]:
        """Resolve a single image path to an absolute safe path.

        Returns ``None`` (with a logged warning) when the path:

        * Is empty.
        * Contains a URL scheme — this loader is local-file-only by design.
          The entry is still placed in ``image_paths`` so M2 can flag it
          ``file_missing``.
        * Resolves (after ``realpath``) to a location outside
          ``DATASET_DIR/images/`` — path-traversal guard.

        Parameters
        ----------
        raw_path :
            A single image path from the CSV (whitespace already stripped).

        Returns
        -------
        Optional[str]
            Resolved absolute path, or ``None`` if the path cannot be used.
        """
        if not raw_path:
            return None

        # URL detection — do not attempt download (design constraint).
        if URL_SCHEME_RE.match(raw_path):
            logger.warning(
                "Image path looks like a URL, marking as missing: %s", raw_path
            )
            return None

        # Resolve relative to DATASET_DIR.
        candidate = os.path.join(DATASET_DIR, raw_path)
        resolved = os.path.realpath(candidate)

        # Path-traversal guard: the resolved path must be under
        # DATASET_DIR/images/.
        images_canonical = os.path.realpath(self._images_base)
        if not images_canonical.endswith(os.sep):
            images_canonical += os.sep
        if not resolved.startswith(images_canonical):
            logger.warning(
                "Path resolves outside images directory, rejecting: %s → %s",
                raw_path,
                resolved,
            )
            return None

        return resolved

    @staticmethod
    def _extract_image_id(resolved_or_raw: str) -> str:
        """Extract the image ID (filename stem) from a path string.

        ``images/test/case_001/img_1.jpg`` → ``"img_1"``
        """
        return os.path.splitext(os.path.basename(resolved_or_raw))[0]

    # ── Context Building ────────────────────────────────────────────────

    def _build_context(
        self,
        row: dict,
        history_map: Dict[str, UserHistory],
        all_rules: List[EvidenceRule],
    ) -> ClaimContext:
        """Build a single ``ClaimContext`` from one CSV row and its lookups."""
        user_id = (row.get("user_id") or "").strip()
        claim_object = (row.get("claim_object") or "").strip().lower()

        # Validate claim_object — warn if unknown, coerce to "unknown" so
        # downstream evidence-rule filtering (which matches on claim_object)
        # only picks up the generic "all" rules instead of silently matching
        # nothing.
        if claim_object not in CLAIM_OBJECT_VALUES:
            logger.warning(
                "Unknown claim_object '%s' for user_id=%s — coercing to 'unknown'. "
                "Only generic evidence rules (REQ_GENERAL_*, REQ_REVIEW_TRUST) "
                "will apply.",
                claim_object,
                user_id,
            )
            claim_object = "unknown"

        # ── User history (safe-default when missing) ─────────────────
        user_history = history_map.get(user_id, UserHistory())
        if user_id and user_id not in history_map:
            logger.debug(
                "No history record for user_id=%s: using safe defaults.", user_id
            )

        # ── Image path resolution ────────────────────────────────────
        raw_paths = (row.get("image_paths") or "").strip()
        raw_segments = self._normalize_and_split_image_paths(raw_paths)

        # Enforce maximum — prevents memory / token exhaustion on
        # claims with an unreasonably large number of submitted images.
        if len(raw_segments) > MAX_IMAGES_PER_CLAIM:
            logger.warning(
                "Truncating %d image paths to %d for user_id=%s.",
                len(raw_segments),
                MAX_IMAGES_PER_CLAIM,
                user_id,
            )
            raw_segments = raw_segments[:MAX_IMAGES_PER_CLAIM]

        # Resolve each segment.  URL-like paths are kept (for M2 flagging);
        # path-traversal attempts are dropped for security.
        image_paths: List[str] = []
        image_ids: List[str] = []
        for seg in raw_segments:
            resolved = self._resolve_image_path(seg)
            if resolved is not None:
                image_paths.append(resolved)
                image_ids.append(self._extract_image_id(resolved))
            elif URL_SCHEME_RE.match(seg):
                # Keep URL entries so M2 can flag them as file_missing.
                image_paths.append(seg)
                image_ids.append(self._extract_image_id(seg))
            # else: path-traversal or other unsafe — drop silently (logged in
            # _resolve_image_path).

        # ── Evidence rules filtered to claim_object + "all" ──────────
        evidence_rules = [
            rule
            for rule in all_rules
            if rule.claim_object in (claim_object, "all")
        ]

        return ClaimContext(
            user_id=user_id,
            image_paths=image_paths,
            image_ids=image_ids,
            user_claim=(row.get("user_claim") or "").strip(),
            claim_object=claim_object,
            user_history=user_history,
            evidence_rules=evidence_rules,
        )


# ── Module-Level Helpers ──────────────────────────────────────────────────


def _safe_int(row: dict, key: str, default: int = 0) -> int:
    """Parse *row[key]* as an integer, returning *default* on failure."""
    raw = row.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.debug("Could not parse '%s'='%s' as int, using %d", key, raw, default)
        return default


# ── What the package exports ──────────────────────────────────────────────

__all__ = [
    "DATASET_DIR",
    "MAX_IMAGES_PER_CLAIM",
    "DataLoader",
    "LocalCSVDataLoader",
]
