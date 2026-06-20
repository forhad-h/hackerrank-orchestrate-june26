"""
Prompt Loader — versioned, template-variable–enabled prompt loader.

No external registry needed. Prompts are discovered by scanning directory
convention:

    prompts/<category>/v<version>_<slug>.md

``prompt_id`` = ``category/slug``  (e.g. ``dev/pre-implementation-arch-review``)

The **latest version** is loaded by default. Specific versions can be requested.

Usage
-----

    from prompts.loader import load_prompt

    # Load latest version with default module_name
    prompt, meta = load_prompt("dev/pre-implementation-arch-review")

    # Override {{MODULE_NAME}} for a different module
    prompt, meta = load_prompt(
        "dev/pre-implementation-arch-review",
        variables={"MODULE_NAME": "VLM Engine"},
    )

    # Load a specific version
    prompt, meta = load_prompt(
        "dev/pre-implementation-arch-review", version=1
    )

    # Inspect metadata
    print(meta["version"])          # 1
    print(meta["date"])             # "2026-06-20"
    print(meta["module_name"])      # "VLM Engine"  (resolved variable)

Front matter schema (YAML-like, in .md files between ``---`` delimiters)
------------------------------------------------------------------------
``prompt_id`` : str    — must match the directory-derived id
``version``   : int    — must match the ``v<N>_`` filename prefix
``title``     : str    — human-readable name
``description`` : str  — one-line summary
``author``    : str    — who wrote this version
``module_name`` : str  — default for ``{{MODULE_NAME}}`` (if applicable)
``variables`` : list   — each: {name, description, default}
``changelog`` : list   — each: {version, date, changes: […]}

Variables in the prompt body use ``{{VARIABLE_NAME}}`` syntax.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
PROMPTS_DIR = _HERE

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

# Matches filenames like "v3_some_prompt.md"
_VERSION_FILE_RE = re.compile(r"^v(\d+)_(.+)\.md$")


# ── Public API ─────────────────────────────────────────────────────────────────


def load_prompt(
    prompt_id: str,
    version: Optional[int] = None,
    variables: Optional[Dict[str, str]] = None,
) -> Tuple[str, dict]:
    """Load and render a versioned prompt template.

    Parameters
    ----------
    prompt_id :
        E.g. ``"dev/pre-implementation-arch-review"``.  Derived from the
        file path: ``<category>/<slug>``.
    version :
        Specific version to load.  ``None`` loads the latest.
    variables :
        Overrides for ``{{VARIABLE}}`` placeholders.  Falls back to the
        front-matter ``default``, or raises ``KeyError`` if none set.

    Returns
    -------
    (rendered_text, metadata)
        ``rendered_text`` has all ``{{VARIABLES}}`` substituted.
        ``metadata`` is the resolved front-matter dict.
    """
    candidates = _discover_versions(prompt_id)
    if not candidates:
        raise KeyError(
            f"Unknown prompt_id {prompt_id!r}. "
            f"Available: {sorted(list_all_prompts())}"
        )

    # Latest = highest version number
    version = version or max(v for v, _ in candidates)
    file_path = _file_for_version(candidates, version)

    if file_path is None:
        raise KeyError(
            f"Prompt {prompt_id!r} has no version {version}. "
            f"Available versions: {sorted(v for v, _ in candidates)}"
        )

    raw_text = file_path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw_text)

    # Resolve variables
    resolved_vars: Dict[str, str] = {}
    for var_entry in meta.get("variables", []):
        name = var_entry["name"]
        resolved_vars[name] = (
            variables.get(name, var_entry.get("default", ""))
            if variables
            else var_entry.get("default", "")
        )

    if variables:
        for k, v in variables.items():
            if k not in resolved_vars:
                resolved_vars[k] = v

    rendered = _substitute(body, resolved_vars)

    # Surface the resolved MODULE_NAME in metadata
    meta["module_name"] = resolved_vars.get("MODULE_NAME", meta.get("module_name", ""))
    meta["_file_path"] = str(file_path)

    return rendered, meta


def list_all_prompts() -> Dict[str, list]:
    """Return all discovered prompts and their versions.

    Returns
    -------
    {prompt_id: [version, ...], ...}
    """
    result: Dict[str, list] = {}
    for category_dir in sorted(PROMPTS_DIR.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("__"):
            continue
        category = category_dir.name
        for f in sorted(category_dir.iterdir()):
            m = _VERSION_FILE_RE.match(f.name)
            if m:
                slug = m.group(2)
                pid = f"{category}/{slug}"
                result.setdefault(pid, []).append(int(m.group(1)))
    return result


# ── Discovery ──────────────────────────────────────────────────────────────────


def _discover_versions(prompt_id: str) -> List[Tuple[int, Path]]:
    """Scan directory for all version files matching ``prompt_id``.

    Returns ``[(version, Path), ...]`` sorted ascending.
    """
    parts = prompt_id.split("/", 1)
    if len(parts) != 2:
        raise ValueError(
            f"prompt_id must be 'category/slug', got {prompt_id!r}"
        )
    category, slug = parts
    category_dir = PROMPTS_DIR / category
    if not category_dir.is_dir():
        return []

    candidates: List[Tuple[int, Path]] = []
    for f in category_dir.iterdir():
        m = _VERSION_FILE_RE.match(f.name)
        if m and m.group(2) == slug:
            candidates.append((int(m.group(1)), f))
    candidates.sort(key=lambda x: x[0])
    return candidates


def _file_for_version(
    candidates: List[Tuple[int, Path]], version: int
) -> Optional[Path]:
    for v, p in candidates:
        if v == version:
            return p
    return None


# ── Front Matter Parsing ──────────────────────────────────────────────────────


def _parse_front_matter(text: str) -> Tuple[dict, str]:
    """Extract YAML-like front matter from a markdown file.

    Expects ``---`` delimiters at the very top of the file.
    Returns ``(meta_dict, body_text)``.
    """
    lines = text.split("\n")
    meta: Dict[str, Any] = {}

    if lines and lines[0].strip() == "---":
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is not None:
            raw_meta = "\n".join(lines[1:end_idx])
            meta = _parse_yaml_block(raw_meta)
            body = "\n".join(lines[end_idx + 1 :])
            return meta, body

    return meta, text


def _parse_yaml_block(text: str) -> dict:
    """Parse our limited YAML subset used in front matter.

    Supports:
      - scalar keys: ``key: value``
      - nested list entries: ``  - name: ...``
      - simple list of scalars: ``  - item1``
      - folded scalars: ``description: >``
    """
    result: Dict[str, Any] = {}

    # Simple scalars first (non-indented key: value)
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- ") and ": " in stripped and not stripped.startswith(" "):
            key, _, value = stripped.partition(": ")
            result[key.strip()] = _coerce(value.strip())

    # Structured blocks: variables, changelog, description (folded)
    _parse_variables_block(text, result)
    _parse_changelog_block(text, result)
    _parse_folded_scalar(text, "description", result)

    return result


def _parse_variables_block(text: str, result: dict) -> None:
    """Parse the ``variables:`` YAML block into ``[{name, description, default}, ...]``."""
    lines = text.split("\n")
    in_vars = False
    entries: List[dict] = []
    current: Optional[dict] = None

    for line in lines:
        stripped = line.strip()
        if stripped == "variables:":
            in_vars = True
            continue
        if in_vars:
            if not stripped.startswith("- ") and ": " in stripped and not line.startswith(" "):
                break  # next top-level key
            if stripped.startswith("- name:"):
                current = {"name": stripped.split(":", 1)[1].strip()}
                entries.append(current)
            elif current and stripped.startswith("description:"):
                current["description"] = stripped.split(":", 1)[1].strip()
            elif current and stripped.startswith("default:"):
                current["default"] = stripped.split(":", 1)[1].strip()

    if entries:
        result["variables"] = entries


def _parse_changelog_block(text: str, result: dict) -> None:
    """Parse the ``changelog:`` YAML block."""
    lines = text.split("\n")
    in_cl = False
    entries: List[dict] = []
    current: Optional[dict] = None
    in_changes = False

    for line in lines:
        stripped = line.strip()
        if stripped == "changelog:":
            in_cl = True
            continue
        if in_cl:
            if not stripped.startswith("- ") and ": " in stripped and not line.startswith(" "):
                break
            if stripped.startswith("- version:"):
                current = {"version": int(stripped.split(":", 1)[1].strip())}
                entries.append(current)
                in_changes = False
            elif current and stripped.startswith("date:"):
                current["date"] = stripped.split(":", 1)[1].strip()
            elif current and stripped.startswith("changes:"):
                in_changes = True
                current["changes"] = []
            elif in_changes and stripped.startswith("- "):
                current["changes"].append(stripped[2:])

    if entries:
        result["changelog"] = entries


def _parse_folded_scalar(text: str, key: str, result: dict) -> None:
    """Handle ``key: >`` folded multiline scalar."""
    if key in result:
        return  # already set inline
    pattern = rf"^{key}:\s*>\s*$"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            parts: List[str] = []
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and (lines[j].startswith(" ") or lines[j].startswith("\t")):
                    parts.append(lines[j].strip())
                else:
                    break
            if parts:
                result[key] = " ".join(parts)
            return


# ── Substitution ──────────────────────────────────────────────────────────────


def _substitute(text: str, variables: Dict[str, str]) -> str:
    """Replace ``{{VARIABLE}}`` placeholders with values."""

    def _replacer(m: re.Match) -> str:
        name = m.group(1)
        if name not in variables:
            raise KeyError(
                f"Prompt template contains {{{{{name}}}}} but no value was "
                f"provided. Available variables: {sorted(variables)}"
            )
        return variables[name]

    return _VAR_RE.sub(_replacer, text)


def _coerce(value: str) -> Any:
    """Coerce a string to int/float/bool if possible."""
    v = value.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ── CLI ────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m prompts.loader <prompt_id> [--var NAME=VALUE ...]")
        print("\nAvailable prompts:")
        all_prompts = list_all_prompts()
        if not all_prompts:
            print("  (none found)")
        for pid, versions in sorted(all_prompts.items()):
            print(f"  {pid:50s} v{max(versions)}")
        sys.exit(0)

    prompt_id = sys.argv[1]
    variables = {}

    # Parse --var NAME=VALUE (support both "--var NAME=V" and "--var" "NAME=V")
    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg.startswith("--var="):
            kv = arg[6:]
            if "=" in kv:
                k, v = kv.split("=", 1)
                variables[k] = v
            i += 1
        elif arg == "--var" and i + 1 < len(sys.argv):
            i += 1
            kv = sys.argv[i]
            if "=" in kv:
                k, v = kv.split("=", 1)
                variables[k] = v
            i += 1
        else:
            i += 1

    try:
        rendered, meta = load_prompt(prompt_id, variables=variables)
        print(f"# Loaded: {prompt_id}  v{meta['version']}")
        print(f"# Title: {meta.get('title', '(no title)')}")
        print(f"# Module: {meta.get('module_name', '(not set)')}")
        print("# " + "-" * 60)
        print(rendered)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
