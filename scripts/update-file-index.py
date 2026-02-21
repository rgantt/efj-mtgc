#!/usr/bin/env python3
"""Update file index line counts in CLAUDE.md / CLAUDE_NEW.md.

Scans project directories for source files >= 200 lines. Updates line counts,
adds new files with blank summaries, removes files below threshold or deleted.
Preserves hand-written purpose/summary strings.

Usage:
    python3 scripts/update-file-index.py          # manual run
    # Also runs automatically via .git/hooks/pre-commit
"""

import re
import subprocess
import sys
from pathlib import Path

MIN_LINES = 200
REPO_ROOT = Path(__file__).resolve().parent.parent

ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|$")
SKIP = frozenset({"__init__.py", "__main__.py"})


def find_target():
    for name in ("CLAUDE.md", "CLAUDE_NEW.md"):
        p = REPO_ROOT / name
        if p.exists() and "## File Index" in p.read_text():
            return p
    sys.exit("No file with '## File Index' section found")


def count_lines(path):
    with open(path, errors="replace") as f:
        return sum(1 for _ in f)


def scan(base, pattern, exclude=frozenset()):
    d = REPO_ROOT / base
    if not d.is_dir():
        return []
    return sorted(
        f for f in d.glob(pattern) if f.is_file() and f.name not in exclude
    )


def files_for_section(header):
    """Return [(display_name, line_count)] or None if section not recognized."""
    pairs = []

    if "`mtg_collector/cli/`" in header:
        pairs = [(f.name, f) for f in scan("mtg_collector/cli", "*.py", SKIP)]
    elif "`mtg_collector/db/`" in header:
        pairs = [(f.name, f) for f in scan("mtg_collector/db", "*.py", SKIP)]
    elif "`mtg_collector/services/`" in header:
        pairs = [(f.name, f) for f in scan("mtg_collector/services", "*.py", SKIP)]
    elif "`mtg_collector/static/`" in header:
        pairs = [(f.name, f) for f in scan("mtg_collector/static", "*.html")]
    elif "importers" in header.lower() and "exporters" in header.lower():
        pairs = [
            (f"importers/{f.name}", f)
            for f in scan("mtg_collector/importers", "*.py", SKIP)
        ]
        pairs += [
            (f"exporters/{f.name}", f)
            for f in scan("mtg_collector/exporters", "*.py", SKIP)
        ]
    elif "other key files" in header.lower():
        pairs = [(f.name, f) for f in scan(".", "*.py", SKIP)]
        pairs += [(f.name, f) for f in scan("mtg_collector", "*.py", SKIP)]
        pairs += [(f.name, f) for f in scan("scripts", "*.py")]
        for name in ("pyproject.toml", "Containerfile"):
            f = REPO_ROOT / name
            if f.is_file():
                pairs.append((name, f))
    elif "test" in header.lower():
        pairs = [(f.name, f) for f in scan("tests", "test_*.py")]
    else:
        return None

    results = []
    for display, path in pairs:
        n = count_lines(path)
        if n >= MIN_LINES:
            results.append((display, n))
    results.sort(key=lambda x: -x[1])
    return results


def update(content):
    lines = content.split("\n")

    # Find ## File Index boundaries
    idx_start = idx_end = None
    for i, ln in enumerate(lines):
        if ln.strip() == "## File Index":
            idx_start = i
        elif idx_start is not None and ln.startswith("## ") and i > idx_start:
            idx_end = i
            break
    if idx_start is None:
        return content
    if idx_end is None:
        idx_end = len(lines)

    # Collect subsections: (header_line, table_data_start, table_data_end)
    subs = []
    i = idx_start + 1
    while i < idx_end:
        if lines[i].startswith("### "):
            hdr = i
            j = i + 1
            tbl_start = tbl_end = None
            while j < idx_end and not lines[j].startswith("### "):
                stripped = lines[j].strip()
                if (
                    tbl_start is None
                    and stripped.startswith("|")
                    and j + 1 < idx_end
                    and re.match(r"^\|[-\s:]+\|", lines[j + 1].strip())
                ):
                    tbl_start = j + 2  # data rows start after header + separator
                    k = tbl_start
                    while (
                        k < idx_end
                        and lines[k].strip().startswith("|")
                        and not lines[k].startswith("### ")
                    ):
                        k += 1
                    tbl_end = k
                    break
                j += 1
            if tbl_start is not None:
                subs.append((hdr, tbl_start, tbl_end))
            i = tbl_end if tbl_end is not None else j + 1
        else:
            i += 1

    # Process in reverse to keep line indices valid
    for hdr, tbl_start, tbl_end in reversed(subs):
        header = lines[hdr]
        new_files = files_for_section(header)
        if new_files is None:
            continue

        # Parse existing purposes
        purposes = {}
        for row in lines[tbl_start:tbl_end]:
            m = ROW_RE.match(row.strip())
            if m:
                purposes[m.group(1)] = m.group(3).strip()

        # Build new rows
        new_rows = []
        for display, n in new_files:
            purpose = purposes.get(display, "")
            new_rows.append(f"| `{display}` | {n} | {purpose} |")

        lines[tbl_start:tbl_end] = new_rows

    return "\n".join(lines)


def main():
    target = find_target()
    original = target.read_text()
    updated = update(original)

    if original == updated:
        return

    target.write_text(updated)

    # Stage the changed file for the current commit
    subprocess.run(
        ["git", "add", str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
    )


if __name__ == "__main__":
    main()
