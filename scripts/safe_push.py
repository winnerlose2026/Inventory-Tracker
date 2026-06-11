#!/usr/bin/env python3
"""Pre-push validator — catches Dropbox file corruption before it ships.

Background
----------
The project rules in CLAUDE.md describe Dropbox's habit of corrupting
.repo/.git/ during sync. What we hit in May 2026 (~5 broken commits in
one afternoon) is the WORSE failure mode: Dropbox truncating the actual
source files mid-edit, then the truncated copy getting `cp`'d into the
fresh-clone push directory and shipped to origin/main without anyone
noticing the file is shorter than it was supposed to be.

Examples we've seen:
  - app.py lost the bottom 10 lines (broke the imports of new auth code)
  - sync_inventory.py lost the bottom 65 lines (lost the REPRINT logic)
  - templates/index.html lost </script></body></html>, breaking ALL JS
    on the page — blank dashboard, dead tab clicks

These all PARSED as valid Python (because the cut happened at a line
boundary mid-function and Python is lenient), or in the HTML case
LOOKED fine until rendered. Cheap checks would have caught every one.

Usage
-----
From the fresh push clone, after `cp`'ing files in from Dropbox:

    python scripts/safe_push.py
        Check every file changed vs origin/main.

    python scripts/safe_push.py app.py templates/index.html
        Check specific files.

Exits 0 if all checks pass, non-zero with a per-file error report
otherwise. The recommended push flow becomes:

    cp $DROPBOX_FILES .
    python scripts/safe_push.py  &&  git add -A  &&  git commit -m '...'  &&  git push

Checks per file
---------------
*.py           - python -m py_compile
*.html         - script-tag open/close balance,
                 inline JS parses with `node --check`,
                 CSS brace balance inside <style>,
                 presence of </body></html>
all files      - line-count drop vs origin/main flagged when >5%
                 (Dropbox truncations usually take 1-50%+ off the tail)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cmd: list[str]) -> tuple[int, str, str]:
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def py_compile_check(path: Path) -> list[str]:
    code, _, err = _run([sys.executable, "-m", "py_compile", str(path)])
    return [] if code == 0 else [f"py_compile failed: {err.strip()[:400]}"]


def html_check(path: Path) -> list[str]:
    """Sanity-check an HTML template that has inline <script> and <style>."""
    src = path.read_text(encoding="utf-8", errors="replace")
    errors: list[str] = []

    # 1) Script tag balance — the failure mode that black-screened the app.
    opens = len(re.findall(r"<script\b[^>]*>", src, re.IGNORECASE))
    closes = len(re.findall(r"</script[^>]*>", src, re.IGNORECASE))
    if opens != closes:
        errors.append(
            f"<script> tags unbalanced: {opens} open vs {closes} close "
            "(likely truncated tail)"
        )

    # 2) Inline JS must parse. Extract every non-`src=` script body and run
    # `node --check` on it. Jinja expressions get stubbed so they don't
    # confuse the parser.
    pat = re.compile(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script[^>]*>",
        re.DOTALL | re.IGNORECASE,
    )
    for i, m in enumerate(pat.finditer(src)):
        if "src=" in (m.group("attrs") or ""):
            continue
        body = m.group("body")
        if not body.strip():
            continue
        cleaned = re.sub(r"\{\{[^}]*\}\}", '"__JINJA__"', body)
        cleaned = re.sub(r"\{%[^%]*%\}", "", cleaned)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(cleaned)
            tmp_path = tmp.name
        try:
            code, _, err = _run(["node", "--check", tmp_path])
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if code != 0:
            # Surface the first failure line for fast triage.
            first_err = err.strip().splitlines()[:5]
            errors.append(
                f"inline JS block #{i} fails node --check:\n        "
                + "\n        ".join(first_err)
            )

    # 3) CSS brace balance inside <style>.
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", src, re.DOTALL):
        css = m.group(1)
        o, c = css.count("{"), css.count("}")
        if o != c:
            errors.append(f"<style> braces unbalanced: {o} open vs {c} close")

    # 4) Document must close. Truncations love eating the tail.
    if "</body>" not in src.lower() or "</html>" not in src.lower():
        errors.append("missing </body> and/or </html> — file truncated at tail?")

    return errors


def size_drop_check(path: Path, ref: str) -> list[str]:
    """Flag any file that's shrunk by more than 5% vs origin/main.

    Real edits add or remove a few percent of lines at most. A 10%+
    sudden drop is almost always Dropbox eating the tail.
    """
    code, out, _ = _run(["git", "show", f"origin/main:{ref}"])
    if code != 0:
        return []  # new file, no baseline
    prev = out.count("\n")
    cur = path.read_text(encoding="utf-8", errors="replace").count("\n")
    if prev == 0:
        return []
    drop_pct = (prev - cur) / prev * 100.0
    if drop_pct > 5.0:
        return [
            f"line count dropped {prev} -> {cur} ({drop_pct:.1f}% — looks like "
            "Dropbox truncation; restore from origin/main and re-apply edits "
            "via Python script instead of the Edit tool)"
        ]
    return []


def main() -> int:
    p = argparse.ArgumentParser(
        description="Validate files before `git push` — catches Dropbox truncation.",
    )
    p.add_argument(
        "files",
        nargs="*",
        help="Files to check. Default: every file modified vs origin/main.",
    )
    p.add_argument(
        "--ref",
        default="origin/main",
        help="Git ref to compare line counts against (default: origin/main).",
    )
    args = p.parse_args()

    files = args.files
    if not files:
        # Pull the latest origin/main so the comparison is fresh
        _run(["git", "fetch", "--quiet", "origin", "main"])
        code, out, _ = _run(["git", "diff", "--name-only", args.ref])
        files = [f for f in out.splitlines() if f.strip()]

    if not files:
        print("safe_push: nothing to check — no diff vs", args.ref)
        return 0

    print(f"safe_push: validating {len(files)} file(s) vs {args.ref}")
    all_errors: dict[str, list[str]] = {}
    for f in files:
        path = Path(f)
        if not path.exists():
            all_errors[f] = ["does not exist on disk"]
            continue
        errs: list[str] = []
        errs.extend(size_drop_check(path, f))
        if f.endswith(".py"):
            errs.extend(py_compile_check(path))
        elif f.endswith((".html", ".htm")):
            errs.extend(html_check(path))
        if errs:
            all_errors[f] = errs
        else:
            print(f"  OK  {f}")

    if all_errors:
        print("\nFAILED — do not push:\n")
        for f, errs in all_errors.items():
            print(f"  {f}")
            for e in errs:
                # indent multi-line entries cleanly
                first, *rest = e.split("\n", 1)
                print(f"    - {first}")
                for line in rest:
                    print(f"      {line}")
            print()
        print(
            "Recover by either:\n"
            "  1. git checkout origin/main -- <file>  (revert to last known good)\n"
            "     then re-apply edits via a Python script that uses str.replace,\n"
            "     never the Edit tool against a Dropbox path.\n"
            "  2. Read the Dropbox file via the Read tool — if it looks intact\n"
            "     in the tool but truncated on disk, force a re-sync from Dropbox\n"
            "     before re-running this validator."
        )
        return 1

    print("\nsafe_push: all checks pass — safe to commit and push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
