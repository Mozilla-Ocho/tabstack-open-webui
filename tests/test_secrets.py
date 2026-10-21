"""Guard rails: nothing key-shaped in fixtures or docs, and plain ASCII everywhere."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED_FOR_KEYS = [ROOT / "tests" / "fixtures", ROOT / "docs"]
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".toml", ".yml", ".yaml", ".txt", ""}
SKIP_DIRS = {
    ".venv",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "screenshots",
}

# The format of a Tabstack key is not documented, so these patterns are generic:
# an assignment to TABSTACK_API_KEY, a bearer token, or a long unbroken run of
# key-like characters with letters and at least four digits. Long identifiers
# (ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS, _PyUnicodeWriter_WriteLatin1String)
# and URL fragments after '#' are skipped; a random 32+ character key almost
# always has four or more digits.
KEY_PATTERNS = [
    re.compile(r"TABSTACK_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(
        r"(?<![A-Za-z0-9_\-/.#])(?=(?:[A-Za-z_\-]*[0-9]){4})(?=[A-Za-z0-9_\-]*[A-Za-z])"
        r"[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-/.])"
    ),
]


def files_under(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for base in paths:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not SKIP_DIRS.intersection(path.parts):
                found.append(path)
    return found


def repo_text_files() -> list[Path]:
    return [
        p
        for p in files_under([ROOT])
        if p.suffix in TEXT_SUFFIXES
        and p.name != "uv.lock"
        and p.name != ".python-version"
    ]


def test_fixtures_and_docs_contain_nothing_key_shaped() -> None:
    hits: list[str] = []
    for path in files_under(SCANNED_FOR_KEYS):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in KEY_PATTERNS:
            for match in pattern.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}: {match.group(0)[:12]}...")
    assert hits == []


def test_real_key_from_environment_is_not_in_the_repo() -> None:
    key = os.environ.get("TABSTACK_API_KEY", "")
    if len(key) < 8:
        return
    leaks = [
        str(p.relative_to(ROOT))
        for p in files_under([ROOT])
        if p.name != ".env" and key in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert leaks == []  # file names only; the key itself is never printed


def test_env_file_is_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored


def test_repo_text_is_plain_ascii() -> None:
    offenders: list[str] = []
    for path in repo_text_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.isascii():
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == []


def test_key_patterns_catch_planted_samples() -> None:
    planted = [
        "TABSTACK_API_KEY=abcdef123456",
        "Authorization: Bearer abcdefghijklmnop1234",
        "token sample0123456789abcdefghijklmnopqrstuv end",
    ]
    for sample in planted:
        assert any(p.search(sample) for p in KEY_PATTERNS), sample
    for innocent in [
        "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS",
        "https://example.com/a/b",
        "_PyUnicodeWriter_WriteLatin1String",
        "(http://docs.python.org#whatsnew-pep750-template-string-literals)",
    ]:
        assert not any(p.search(innocent) for p in KEY_PATTERNS), innocent


def test_tool_file_survives_open_webui_formatting() -> None:
    # Open WebUI runs black with its default mode on tool code before saving.
    import black

    source = (ROOT / "tabstack_tool.py").read_text(encoding="utf-8")
    assert black.format_str(source, mode=black.Mode()) == source
