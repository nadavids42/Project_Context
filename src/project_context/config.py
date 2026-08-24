"""Typed application configuration.

Configuration is read from environment variables (prefixed
``PROJECT_CONTEXT_``) or a local ``.env`` file, with safe defaults so the
application runs out of the box. Values are validated eagerly and fail with
a clear, actionable message rather than starting the application in an
unknown state.

This module only *validates* configuration — it does not create the data
directory, evidence directory, or SQLite file. Directory/file creation is a
side effect that belongs to the code paths that actually need those paths
to exist (database initialization, evidence storage), not to importing or
loading configuration.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Default OpenAI model per the product plan (Section 12.2), pinned rather
#: than left to routing/auto-selection.
DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

#: Log levels the application accepts, independent of any aliases
#: (e.g. WARN) the stdlib also recognizes.
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

DEFAULT_DATA_DIR = Path("data")
DEFAULT_SQLITE_FILENAME = "project_context.db"
DEFAULT_EVIDENCE_DIRNAME = "evidence"
DEFAULT_CREDENTIALS_DIRNAME = "credentials"

#: FR-006 / Section 11.1 ("App-configured max 25 MB/file"). A hard
#: per-file ceiling enforced by the ingestion service, independent of any
#: Streamlit server-level upload limit.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Chunk target size in *characters*, not tokens — Section 8 asks for a
#: "configurable character/token target without adding a model tokenizer
#: dependency unless already justified"; no extraction step exists yet to
#: justify one, so chunking is sized in characters with a rough
#: characters-per-token estimate (see chunking.py) for the stored
#: `token_estimate` column.
DEFAULT_CHUNK_TARGET_CHARS = 4000
DEFAULT_CHUNK_OVERLAP_RATIO = 0.10

#: Owner-only directory permissions (0700) — Section 16, "restrict the
#: data directory to the user." Applied to `data_dir`/`evidence_dir`
#: exactly like `credentials/store.py` already applies it to
#: `credentials_dir`. `os.chmod` is a no-op on filesystems that don't
#: support POSIX permission bits (e.g. FAT); it never raises there, so
#: this degrades silently rather than failing configuration on an
#: unsupported OS ("where the OS supports them").
_OWNER_ONLY_DIR = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


class Environment(StrEnum):
    """Named deployment environments. The prototype targets LOCAL only."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ConfigurationError(ValueError):
    """Raised when configuration is missing, malformed, or unsafe.

    Wraps the underlying validation error(s) with a single, readable
    message so callers (including the Streamlit health panel) can display
    it directly without leaking a raw framework traceback.
    """


def _restrict_to_owner(directory: Path) -> None:
    """Best-effort `chmod 0700`. Never raises: a filesystem that doesn't
    support POSIX permission bits (or a directory this process doesn't
    own) must not turn a permission tightening step into a startup
    failure — the directory is still created and usable, just not
    verified-restricted on that filesystem."""
    with contextlib.suppress(OSError):
        os.chmod(directory, _OWNER_ONLY_DIR)


def _require_non_blank_path_str(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{field_name} must not be empty or whitespace-only")
        if "\x00" in value:
            raise ValueError(f"{field_name} must not contain a NUL byte")
    return value


class AppConfig(BaseSettings):
    """Application configuration, sourced from environment / `.env`.

    All fields have safe local defaults; nothing here requires network
    access or external accounts. Feature flags for every connector default
    to disabled because no connector is implemented yet.
    """

    model_config = SettingsConfigDict(
        env_prefix="PROJECT_CONTEXT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL

    data_dir: Path = DEFAULT_DATA_DIR
    # sqlite_path and evidence_dir default to locations under data_dir;
    # left as None here so the model validator can derive them.
    sqlite_path: Path | None = None
    evidence_dir: Path | None = None

    log_level: str = "INFO"

    openai_model: str = DEFAULT_OPENAI_MODEL

    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    chunk_target_chars: int = DEFAULT_CHUNK_TARGET_CHARS
    chunk_overlap_ratio: float = DEFAULT_CHUNK_OVERLAP_RATIO

    feature_drive_enabled: bool = False
    feature_gmail_enabled: bool = False
    feature_calendar_enabled: bool = False
    feature_fathom_enabled: bool = False

    #: credentials_dir defaults to a location under data_dir, exactly
    #: like sqlite_path/evidence_dir — left as None here so the model
    #: validator can derive it. Holds only the encrypted-fallback
    #: secrets file and its separate master key (Section 16) — never
    #: created unless the OS keyring is unavailable at least once.
    credentials_dir: Path | None = None

    #: Google OAuth desktop-app client credentials (Section 11.2: "OAuth
    #: 2.0 desktop client with localhost callback"). Both must be set to
    #: enable Drive — see the feature flag above and
    #: `google_oauth_is_configured`. Never given a default; there is no
    #: safe placeholder client ID/secret. Loaded from environment/`.env`
    #: like every other setting here, never committed (Section 8:
    #: "`.env` ... gitignored").
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    #: 0 lets the OS pick a free localhost port for the OAuth callback
    #: (Section 11.2: "localhost callback").
    google_oauth_redirect_port: int = 0

    @field_validator("data_dir", mode="before")
    @classmethod
    def _validate_data_dir_str(cls, value: Any) -> Any:
        return _require_non_blank_path_str(value, "data_dir")

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def _validate_sqlite_path_str(cls, value: Any) -> Any:
        if value in ("", None):
            return None
        return _require_non_blank_path_str(value, "sqlite_path")

    @field_validator("evidence_dir", mode="before")
    @classmethod
    def _validate_evidence_dir_str(cls, value: Any) -> Any:
        if value in ("", None):
            return None
        return _require_non_blank_path_str(value, "evidence_dir")

    @field_validator("credentials_dir", mode="before")
    @classmethod
    def _validate_credentials_dir_str(cls, value: Any) -> Any:
        if value in ("", None):
            return None
        return _require_non_blank_path_str(value, "credentials_dir")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized not in VALID_LOG_LEVELS:
                raise ValueError(
                    f"log_level must be one of {sorted(VALID_LOG_LEVELS)}, got {value!r}"
                )
            return normalized
        return value

    @field_validator("openai_model", mode="before")
    @classmethod
    def _validate_openai_model(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("openai_model must not be empty")
        return value

    @field_validator("max_upload_bytes")
    @classmethod
    def _validate_max_upload_bytes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_upload_bytes must be positive")
        return value

    @field_validator("chunk_target_chars")
    @classmethod
    def _validate_chunk_target_chars(cls, value: int) -> int:
        if value < 200:
            raise ValueError("chunk_target_chars must be at least 200")
        return value

    @field_validator("chunk_overlap_ratio")
    @classmethod
    def _validate_chunk_overlap_ratio(cls, value: float) -> float:
        if not (0.0 <= value < 0.5):
            raise ValueError("chunk_overlap_ratio must be in [0.0, 0.5)")
        return value

    @model_validator(mode="after")
    def _resolve_and_check_paths(self) -> AppConfig:
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.data_dir.exists() and not self.data_dir.is_dir():
            raise ValueError(f"data_dir {self.data_dir} exists and is not a directory")

        if self.sqlite_path is None:
            self.sqlite_path = self.data_dir / DEFAULT_SQLITE_FILENAME
        else:
            self.sqlite_path = self.sqlite_path.expanduser().resolve()
        if self.sqlite_path.exists() and self.sqlite_path.is_dir():
            raise ValueError(f"sqlite_path {self.sqlite_path} is a directory, expected a file")

        if self.evidence_dir is None:
            self.evidence_dir = self.data_dir / DEFAULT_EVIDENCE_DIRNAME
        else:
            self.evidence_dir = self.evidence_dir.expanduser().resolve()
        if self.evidence_dir.exists() and not self.evidence_dir.is_dir():
            raise ValueError(f"evidence_dir {self.evidence_dir} exists and is not a directory")

        if self.sqlite_path == self.evidence_dir:
            raise ValueError("sqlite_path and evidence_dir must not be the same path")

        if self.credentials_dir is None:
            self.credentials_dir = self.data_dir / DEFAULT_CREDENTIALS_DIRNAME
        else:
            self.credentials_dir = self.credentials_dir.expanduser().resolve()
        if self.credentials_dir.exists() and not self.credentials_dir.is_dir():
            raise ValueError(
                f"credentials_dir {self.credentials_dir} exists and is not a directory"
            )

        return self

    @property
    def google_oauth_is_configured(self) -> bool:
        """True only when both the client ID and secret are set — the
        Sources & Settings page uses this to explain *why* Drive connect
        is unavailable rather than just hiding the button (Section 11.2:
        "Request Drive permission only when Drive is enabled")."""
        return bool(self.google_oauth_client_id) and bool(self.google_oauth_client_secret)

    def ensure_local_directories(self) -> None:
        """Create data_dir and evidence_dir if missing.

        Explicit, opt-in side effect — never called merely by loading or
        importing configuration. Intended for callers (app startup, DB
        init) that actually need these directories to exist.
        `credentials_dir` is deliberately not created here — the
        credential store creates it itself, on first actual use, only if
        the OS keyring turns out to be unavailable (Section 16).

        Every directory this creates is restricted to the owner (0700)
        immediately after creation (Section 16: "restrict the data
        directory to the user") — including on a rerun against a
        directory that already existed, so a directory created before
        this hardening was added self-heals the next time the app
        starts."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(self.data_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(self.evidence_dir)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(self.sqlite_path.parent)


def load_config(**overrides: Any) -> AppConfig:
    """Load and validate :class:`AppConfig`, failing clearly on bad input.

    Any keyword argument is forwarded to ``AppConfig`` (e.g. ``_env_file``
    to disable reading a real ``.env`` file in tests). Raises
    :class:`ConfigurationError` with a readable, complete message instead
    of a raw ``pydantic.ValidationError``.
    """
    try:
        return AppConfig(**overrides)
    except ValidationError as exc:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"Invalid configuration: {messages}") from exc


def level_to_int(log_level: str) -> int:
    """Convert a validated level name to its stdlib numeric value."""
    return logging.getLevelNamesMapping()[log_level]
