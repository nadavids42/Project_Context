"""Freezes/reads the three benchmark corpora as version-controlled files
under ``tests/golden_projects/benchmark_corpus/`` (Section 13.7 step 1:
"Freeze corpus, ground truth..."; Section 15: "The three datasets in
Section 13 are versioned test assets").

Each project gets its own directory: one raw file per artifact (real
bytes a real upload would carry — ``.vtt``/``.txt``/``.md``), a
``manifest.json`` (artifact metadata, chronology, checkpoints, fact
plan, baseline fixtures, and the project's own ``chunk_target_chars`),
and a ``ground_truth.json`` (items/transitions) — kept as separate files
per Section 13.3: "Store ground truth as versioned JSON/CSV separate
from application outputs" (extended here to also separate it from the
source fixtures themselves).

``build_and_write_all`` regenerates every file from
``project_context.evaluation.corpus_data`` — the authoring source of
truth — and is run explicitly (``scripts/build_benchmark_corpus.py``),
not on every import: the materialized files are what both runners and
tests actually load (``load_corpus_project``), so a corpus is "frozen"
in the literal sense of being real, diffable, git-tracked files, not
regenerated implicitly at run time.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_context.evaluation import corpus_text
from project_context.evaluation.schema import (
    GROUND_TRUTH_SCHEMA_VERSION,
    CorpusProject,
    require_benchmark_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_ROOT = REPO_ROOT / "tests" / "golden_projects" / "benchmark_corpus"

_MANIFEST_FILENAME = "manifest.json"
_GROUND_TRUTH_FILENAME = "ground_truth.json"


class CorpusValidationError(ValueError):
    """Raised by `validate_corpus_project` for any inconsistency that
    would silently break the ledger runner's scripted extraction or
    scoring's evidence-span checks."""


def validate_corpus_project(project: CorpusProject, *, chunk_target_chars: int) -> None:
    """Beyond `CorpusProject`'s own Pydantic validators (which cannot see
    artifact text), verify:

    - every `GroundTruthTransition` mention's `statement` is an exact
      substring of its cited artifact's real parsed text (Section 13.3:
      "exact supporting... evidence spans");
    - `fact_plan[artifact_id]` has exactly one entry per real parsed
      block, in the same order chunking will produce (the one-block-one-
      chunk invariant every scripted-extraction call relies on).

    Raises `CorpusValidationError` on the first problem found, naming the
    artifact/transition so a corpus author can fix it directly. Also
    enforces Section 13.2's benchmark-scale requirements
    (`project_context.evaluation.schema.require_benchmark_corpus`) —
    this function is for the three real frozen corpora, not for small
    test fixtures exercising one formula in isolation.
    """
    from project_context.chunking import chunk_blocks

    try:
        require_benchmark_corpus(project)
    except ValueError as exc:
        raise CorpusValidationError(str(exc)) from exc

    for artifact in project.artifacts:
        parsed = corpus_text.parse_artifact(artifact)
        chunks = chunk_blocks(parsed.blocks, target_chars=chunk_target_chars, overlap_ratio=0.0)
        plan = project.fact_plan.get(artifact.artifact_id, ())
        if not (len(parsed.blocks) == len(chunks) == len(plan)):
            raise CorpusValidationError(
                f"artifact {artifact.artifact_id!r}: {len(parsed.blocks)} parsed blocks, "
                f"{len(chunks)} chunks, {len(plan)} fact_plan entries — must all agree "
                "(check for adjacent same-speaker VTT turns merging, or a missing/extra "
                "fact_plan entry)"
            )

    for item in project.items:
        for transition in item.transitions:
            for mention in transition.mentions:
                artifact = project.artifact_by_id(mention.artifact_id)
                text = corpus_text.parse_artifact(artifact).text
                if mention.statement not in text:
                    raise CorpusValidationError(
                        f"item {item.item_id!r} transition {transition.transition_id!r}: "
                        f"statement not found verbatim in artifact {mention.artifact_id!r}"
                    )


def _project_dir(project_key: str, *, root: Path) -> Path:
    return root / project_key


def write_corpus_project(
    project: CorpusProject, *, chunk_target_chars: int, root: Path = DEFAULT_CORPUS_ROOT
) -> Path:
    """Write one project's frozen files, overwriting any existing ones.
    Validates first — a corpus that fails `validate_corpus_project` is
    never written, so the on-disk corpus is never silently inconsistent.
    """
    validate_corpus_project(project, chunk_target_chars=chunk_target_chars)

    project_dir = _project_dir(project.key, root=root)
    artifacts_dir = project_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    for artifact in project.artifacts:
        (artifacts_dir / artifact.filename).write_bytes(artifact.raw_bytes)

    manifest = {
        "schema_version": GROUND_TRUTH_SCHEMA_VERSION,
        "key": project.key,
        "name": project.name,
        "objective": project.objective,
        "stage": project.stage,
        "chunk_target_chars": chunk_target_chars,
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "kind": a.kind.value,
                "title": a.title,
                "occurred_at": a.occurred_at,
                "author": a.author,
                "filename": a.filename,
                "project_key": a.project_key,
                "ambiguous_assignment": a.ambiguous_assignment,
                "material": a.material,
            }
            for a in project.artifacts_sorted()
        ],
        "checkpoints": [c.model_dump(mode="json") for c in project.checkpoints],
        "fact_plan": {
            artifact_id: [ref.model_dump(mode="json") if ref is not None else None for ref in refs]
            for artifact_id, refs in project.fact_plan.items()
        },
        "baseline_fake_claims": {
            checkpoint_id: [c.model_dump(mode="json") for c in claims]
            for checkpoint_id, claims in project.baseline_fake_claims.items()
        },
        "ambiguous_aliases": [a.model_dump(mode="json") for a in project.ambiguous_aliases],
    }
    (project_dir / _MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    ground_truth = {
        "schema_version": GROUND_TRUTH_SCHEMA_VERSION,
        "project_key": project.key,
        "items": [item.model_dump(mode="json") for item in project.items],
    }
    (project_dir / _GROUND_TRUTH_FILENAME).write_text(
        json.dumps(ground_truth, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return project_dir


def load_corpus_project(
    project_key: str, *, root: Path = DEFAULT_CORPUS_ROOT
) -> tuple[CorpusProject, int]:
    """Read one project back from its frozen files. Returns
    `(project, chunk_target_chars)`, mirroring
    `project_context.evaluation.corpus_data.builder.assemble`'s return
    shape so callers never need to know whether a project came from disk
    or from a fresh build."""
    project_dir = _project_dir(project_key, root=root)
    manifest = json.loads((project_dir / _MANIFEST_FILENAME).read_text(encoding="utf-8"))
    ground_truth = json.loads((project_dir / _GROUND_TRUTH_FILENAME).read_text(encoding="utf-8"))

    artifacts = []
    for entry in manifest["artifacts"]:
        raw_bytes = (project_dir / "artifacts" / entry["filename"]).read_bytes()
        artifacts.append(
            {
                "artifact_id": entry["artifact_id"],
                "kind": entry["kind"],
                "title": entry["title"],
                "occurred_at": entry["occurred_at"],
                "author": entry["author"],
                "filename": entry["filename"],
                "raw_bytes": raw_bytes,
                "project_key": entry["project_key"],
                "ambiguous_assignment": entry["ambiguous_assignment"],
                "material": entry["material"],
            }
        )

    project = CorpusProject.model_validate(
        {
            "key": manifest["key"],
            "name": manifest["name"],
            "objective": manifest["objective"],
            "stage": manifest["stage"],
            "artifacts": artifacts,
            "items": ground_truth["items"],
            "checkpoints": manifest["checkpoints"],
            "fact_plan": manifest["fact_plan"],
            "baseline_fake_claims": manifest["baseline_fake_claims"],
            "ambiguous_aliases": manifest.get("ambiguous_aliases", []),
        }
    )
    return project, manifest["chunk_target_chars"]


def build_and_write_all(*, root: Path = DEFAULT_CORPUS_ROOT) -> list[Path]:
    """Regenerate every project's frozen files from
    `project_context.evaluation.corpus_data`. Run explicitly via
    `scripts/build_benchmark_corpus.py` — never implicitly."""
    from project_context.evaluation.corpus_data import ALL_PROJECT_BUILDERS

    written = []
    for builder in ALL_PROJECT_BUILDERS:
        project, chunk_target_chars = builder()
        written.append(
            write_corpus_project(project, chunk_target_chars=chunk_target_chars, root=root)
        )
    return written
