"""The thin connector protocol every connector implements (Section 11:
"All connectors implement a small protocol"). Only Drive implements it
in Prompt 10; the shape is deliberately generic enough for
Gmail/Calendar/Fathom to reuse later without change.

Connectors normalize metadata but never extract project facts (Section
11: "They normalize metadata but do not extract project facts") — that
remains `project_context.services.extraction`'s job, unchanged, over
whatever `project_context.services.sync` stores from a `RawArtifact`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from project_context.domain.evidence import ArtifactType, EvidenceSourceType


class ConnectorHealthStatus(StrEnum):
    """The outcome of `Connector.validate_config()` — deliberately a
    smaller vocabulary than `sources.health_status`; orchestration maps
    this onto the full `SourceHealthStatus` enum."""

    OK = "ok"
    AUTH_ERROR = "auth_error"
    PERMISSION_ERROR = "permission_error"
    CONFIG_ERROR = "config_error"
    ERROR = "error"


class ConnectorHealth(BaseModel):
    status: ConnectorHealthStatus
    detail: str | None = None


class ArtifactMetadata(BaseModel):
    """Normalized external-item metadata — the same shape regardless of
    which connector produced it. `version_marker` is whatever proves
    "changed" for this connector (Drive: `modifiedTime`); it is compared
    as an opaque string, never parsed as a real timestamp by generic
    orchestration code."""

    model_config = ConfigDict(frozen=True)

    external_id: str
    title: str
    artifact_type: ArtifactType
    source_type: EvidenceSourceType | None = None
    mime_type: str | None = None
    version_marker: str
    size_bytes: int | None = None
    author: str | None = None
    occurred_at: str | None = None
    external_url: str | None = None
    #: True for a container (e.g. a Drive folder) discovered only to be
    #: recursed into — never itself fetched as evidence.
    is_container: bool = False
    #: True for a trashed/deleted-at-source item — surfaced so
    #: orchestration can mark existing evidence unavailable without
    #: erasing it (FR-027).
    is_trashed: bool = False
    #: Connector-specific fields with no generic meaning (e.g. Drive's
    #: raw `mimeType` before translation to `ArtifactType`) — kept out
    #: of the typed fields above so this model never needs a connector-
    #: specific subclass.
    extra: dict[str, Any] = Field(default_factory=dict)


class DiscoveryPage(BaseModel):
    """One page of `Connector.discover()`. `next_checkpoint` is `None`
    exactly when there is no further page — orchestration never needs
    to inspect its shape, only pass it back on the next call."""

    model_config = ConfigDict(frozen=True)

    artifacts: tuple[ArtifactMetadata, ...]
    next_checkpoint: dict[str, Any] | None = None


class RawArtifact(BaseModel):
    """The fetched bytes for one `ArtifactMetadata`, ready for the
    existing parser registry — connectors never parse content
    themselves (Section 8: "Parser registry ... Must remain
    deterministic")."""

    model_config = ConfigDict(frozen=True)

    metadata: ArtifactMetadata
    data: bytes
    mime_type: str
    filename: str | None = None


class Connector(Protocol):
    """Section 11's connector protocol, verbatim in shape:

    ```python
    class Connector(Protocol):
        def validate_config(self) -> ConnectorHealth: ...
        def preview(self, boundary: dict, limit: int = 20) -> list[ArtifactMetadata]: ...
        def discover(self, checkpoint: dict | None) -> DiscoveryPage: ...
        def fetch(self, artifact: ArtifactMetadata) -> RawArtifact: ...
    ```

    `preview` takes an explicit `boundary` (rather than the connector's
    own already-configured one) so the UI can dry-run a folder ID the
    user has typed but not yet saved (FR-002: "a dry run displays
    matched metadata"); `discover`/`fetch` always operate on the
    boundary/credentials the connector was constructed with.
    """

    def validate_config(self) -> ConnectorHealth: ...

    def preview(self, boundary: dict[str, Any], limit: int = 20) -> list[ArtifactMetadata]: ...

    def discover(self, checkpoint: dict[str, Any] | None) -> DiscoveryPage: ...

    def fetch(self, artifact: ArtifactMetadata) -> RawArtifact: ...
