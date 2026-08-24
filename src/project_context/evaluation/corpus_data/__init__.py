"""The three authored corpus builders (Section 13.2). Each module exposes
one ``build() -> tuple[CorpusProject, int]`` function (project, its
self-verifying ``chunk_target_chars`` — see ``builder.assemble``).
``ALL_PROJECT_BUILDERS`` is the one list ``project_context.evaluation.
materialize.build_and_write_all`` and the CLI walk to regenerate/run
every project without hard-coding three import statements elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable

from project_context.evaluation.corpus_data import advisory, implementation, launch
from project_context.evaluation.schema import CorpusProject

ALL_PROJECT_BUILDERS: tuple[Callable[[], tuple[CorpusProject, int]], ...] = (
    implementation.build,
    advisory.build,
    launch.build,
)

ALL_PROJECT_KEYS: tuple[str, ...] = ("implementation", "advisory", "launch")
