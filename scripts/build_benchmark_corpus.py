#!/usr/bin/env python3
"""Regenerate the three frozen benchmark corpora under
tests/golden_projects/benchmark_corpus/ from
project_context.evaluation.corpus_data (Section 13.2/13.7).

Run this only when deliberately changing the corpus itself — the
materialized files it writes are the actual version-controlled fixtures
every runner and test loads; this script is provenance/reproducibility,
not something the evaluation run itself re-invokes.

Usage:
    python scripts/build_benchmark_corpus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_context.evaluation.materialize import build_and_write_all  # noqa: E402


def main() -> int:
    paths = build_and_write_all()
    for path in paths:
        print(f"wrote {path}")
    print(f"\n{len(paths)} project(s) written and validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
