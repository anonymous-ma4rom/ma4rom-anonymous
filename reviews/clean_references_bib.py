#!/usr/bin/env python3
"""Remove redundant bibliography fields without changing citation keys."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


REDUNDANT_NOTES = {
    "main track",
    "first published online in 2022",
    "preprint/online version",
}


def clean_entry(entry: str) -> str:
    """Drop DOI URLs and nonessential notes from one BibTeX entry."""
    has_doi = re.search(r"(?mi)^\s*doi\s*=", entry) is not None

    if has_doi:
        entry = re.sub(
            r"(?mi)^\s*url\s*=\s*\{https?://doi\.org/[^}]+\},?\s*\n",
            "",
            entry,
        )

    def remove_redundant_note(match: re.Match[str]) -> str:
        value = match.group(1).strip().lower()
        return "" if value in REDUNDANT_NOTES else match.group(0)

    entry = re.sub(
        r"(?mi)^\s*note\s*=\s*\{([^}]*)\},?\s*\n",
        remove_redundant_note,
        entry,
    )

    # The DOI value is data, so the underscore should not be LaTeX-escaped.
    entry = re.sub(
        r"(?mi)^(\s*doi\s*=\s*\{[^}]*)\\_([^}]*\})",
        r"\1_\2",
        entry,
    )

    # Removing a final field can leave a comma before the closing brace.
    entry = re.sub(r",\s*\n\}", "\n}", entry)
    return entry


def clean_bibliography(text: str) -> str:
    """Clean every BibTeX entry while preserving comments and ordering."""
    parts = re.split(r"(?=^@)", text, flags=re.MULTILINE)
    return "".join(clean_entry(part) if part.startswith("@") else part for part in parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = args.input.read_text(encoding="utf-8")
    args.output.write_text(clean_bibliography(source), encoding="utf-8")


if __name__ == "__main__":
    main()
