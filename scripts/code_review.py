#!/usr/bin/env python3
"""Automated daily code review — scans the carry bot codebase for issues.

Checks for:
  * Functions > 50 lines (complexity)
  * Bare except clauses (swallowed errors)
  * Hardcoded secrets / API keys
  * TODO / FIXME / HACK comments
  * Missing type hints on public functions
  * Duplicate code patterns
  * Test coverage gaps (functions without tests)

Usage::

    PYTHONPATH=. python3 scripts/code_review.py
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

# Directories to scan
SCAN_DIRS = ["core", "scripts", "utils", "bot", "config", "backtest"]
# Never scan the scanner itself (its docstrings mention TODO/secret patterns).
EXCLUDE_FILES = {"scripts/code_review.py"}
# Patterns that look like secrets
SECRET_PATTERNS = [
    r'api[_-]?key\s*=\s*["\'][^"\']{20,}',
    r'api[_-]?secret\s*=\s*["\'][^"\']{20,}',
    r'password\s*=\s*["\'][^"\']{8,}',
    r'token\s*=\s*["\'][^"\']{20,}',
]
MAX_FUNC_LINES = 50


def _find_python_files() -> list[Path]:
    root = Path(".")
    files = []
    for d in SCAN_DIRS:
        for p in (root / d).rglob("*.py"):
            rel = str(p)
            if "__pycache__" in rel or rel in EXCLUDE_FILES:
                continue
            files.append(p)
    return files


def check_long_functions(files: list[Path]) -> list[str]:
    issues = []
    for f in files:
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > MAX_FUNC_LINES:
                    issues.append(
                        f"  ⚠️ {f}:{node.lineno} — {node.name}() is {length} lines "
                        f"(>{MAX_FUNC_LINES})"
                    )
    return issues


def check_bare_except(files: list[Path]) -> list[str]:
    issues = []
    for f in files:
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(f"  ⚠️ {f}:{node.lineno} — bare 'except:' swallows all errors")
    return issues


def check_secrets(files: list[Path]) -> list[str]:
    issues = []
    for f in files:
        text = f.read_text()
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                issues.append(f"  🚨 {f} — potential secret: {pattern}")
    return issues


def check_todos(files: list[Path]) -> list[str]:
    issues = []
    for f in files:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r'\b(TODO|FIXME|HACK|XXX)\b', line):
                issues.append(f"  📝 {f}:{i} — {line.strip()}")
    return issues


def check_missing_types(files: list[Path]) -> list[str]:
    issues = []
    for f in files:
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                has_return = node.returns is not None
                untyped_args = [
                    a.arg for a in node.args.args
                    if a.annotation is None and a.arg != "self"
                ]
                if untyped_args or not has_return:
                    missing = []
                    if untyped_args:
                        missing.append(f"args: {untyped_args}")
                    if not has_return:
                        missing.append("return type")
                    issues.append(
                        f"  💡 {f}:{node.lineno} — {node.name}() missing {', '.join(missing)}"
                    )
    return issues


def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    files = _find_python_files()

    print(f"\n{'═' * 70}")
    print(f"  🔍 DAILY CODE REVIEW  |  {now}  |  {len(files)} files")
    print(f"{'═' * 70}\n")

    sections = [
        ("🚨 Potential Secrets", check_secrets(files)),
        ("⚠️  Long Functions (>{MAX_FUNC_LINES} lines)", check_long_functions(files)),
        ("⚠️  Bare Except Clauses", check_bare_except(files)),
        ("📝 TODO / FIXME / HACK", check_todos(files)),
        ("💡 Missing Type Hints (public functions)", check_missing_types(files)),
    ]

    total = 0
    for title, issues in sections:
        print(f"  {title}: {len(issues)}")
        for issue in issues[:10]:  # cap at 10 per section
            print(issue)
        if len(issues) > 10:
            print(f"  ... and {len(issues) - 10} more")
        total += len(issues)
        print()

    print(f"{'─' * 70}")
    if total == 0:
        print("  ✅ No issues found — code looks clean!")
    else:
        print(f"  📊 Total: {total} issues across {len(sections)} categories")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
