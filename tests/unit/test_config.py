"""Tests for typed configuration loading: defaults, overrides, and failure."""

from __future__ import annotations

import pytest

from project_context.config import (
    DEFAULT_OPENAI_MODEL,
    ConfigurationError,
    Environment,
    load_config,
)


def test_defaults_are_valid_and_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = load_config(_env_file=None)

    assert config.environment is Environment.LOCAL
    assert config.data_dir == tmp_path / "data"
    assert config.sqlite_path == tmp_path / "data" / "project_context.db"
    assert config.evidence_dir == tmp_path / "data" / "evidence"
    assert config.log_level == "INFO"
    assert config.openai_model == DEFAULT_OPENAI_MODEL
    assert config.feature_drive_enabled is False
    assert config.feature_gmail_enabled is False
    assert config.feature_calendar_enabled is False
    assert config.feature_fathom_enabled is False
    # Loading configuration must not create anything on disk.
    assert not config.data_dir.exists()
    assert not config.evidence_dir.exists()


def test_environment_variable_overrides_are_applied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECT_CONTEXT_ENVIRONMENT", "development")
    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(tmp_path / "custom-data"))
    monkeypatch.setenv("PROJECT_CONTEXT_LOG_LEVEL", "debug")
    monkeypatch.setenv("PROJECT_CONTEXT_OPENAI_MODEL", "gpt-5.6-terra-mini")
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")

    config = load_config(_env_file=None)

    assert config.environment is Environment.DEVELOPMENT
    assert config.data_dir == (tmp_path / "custom-data").resolve()
    assert config.log_level == "DEBUG"  # normalized to uppercase
    assert config.openai_model == "gpt-5.6-terra-mini"
    assert config.feature_drive_enabled is True
    # Unset flags keep their safe default.
    assert config.feature_gmail_enabled is False
    assert config.feature_calendar_enabled is False
    assert config.feature_fathom_enabled is False


def test_explicit_sqlite_and_evidence_paths_are_respected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    custom_db = tmp_path / "somewhere" / "custom.db"
    custom_evidence = tmp_path / "elsewhere" / "evidence-store"
    monkeypatch.setenv("PROJECT_CONTEXT_SQLITE_PATH", str(custom_db))
    monkeypatch.setenv("PROJECT_CONTEXT_EVIDENCE_DIR", str(custom_evidence))

    config = load_config(_env_file=None)

    assert config.sqlite_path == custom_db.resolve()
    assert config.evidence_dir == custom_evidence.resolve()


@pytest.mark.parametrize(
    "env_overrides",
    [
        {"PROJECT_CONTEXT_LOG_LEVEL": "VERBOSE"},
        {"PROJECT_CONTEXT_ENVIRONMENT": "prod-typo"},
        {"PROJECT_CONTEXT_DATA_DIR": "   "},
        {"PROJECT_CONTEXT_OPENAI_MODEL": ""},
    ],
    ids=["bad-log-level", "bad-environment", "blank-data-dir", "blank-model"],
)
def test_invalid_configuration_fails_clearly(tmp_path, monkeypatch, env_overrides):
    monkeypatch.chdir(tmp_path)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigurationError):
        load_config(_env_file=None)


def test_data_dir_pointing_at_a_file_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    file_in_the_way = tmp_path / "not-a-directory"
    file_in_the_way.write_text("not a directory")
    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(file_in_the_way))

    with pytest.raises(ConfigurationError):
        load_config(_env_file=None)


def test_sqlite_path_pointing_at_a_directory_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory_in_the_way = tmp_path / "looks-like-a-db"
    directory_in_the_way.mkdir()
    monkeypatch.setenv("PROJECT_CONTEXT_SQLITE_PATH", str(directory_in_the_way))

    with pytest.raises(ConfigurationError):
        load_config(_env_file=None)


def test_ensure_local_directories_is_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(_env_file=None)
    assert not config.data_dir.exists()

    config.ensure_local_directories()

    assert config.data_dir.is_dir()
    assert config.evidence_dir.is_dir()
    assert config.sqlite_path.parent.is_dir()


def test_ensure_local_directories_restricts_data_and_evidence_dirs_to_owner(tmp_path, monkeypatch):
    """Section 16: "restrict the data directory to the user" — created
    directories are chmod 0700, not left at the default umask-derived
    mode (typically group/other-readable)."""
    import stat

    monkeypatch.chdir(tmp_path)
    config = load_config(_env_file=None)

    config.ensure_local_directories()

    for directory in (config.data_dir, config.evidence_dir):
        mode = stat.S_IMODE(directory.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


def test_ensure_local_directories_self_heals_permissions_on_a_preexisting_directory(
    tmp_path, monkeypatch
):
    """A directory created before this hardening existed (or by some
    other process) is tightened on the next startup, not left as-is."""
    import stat

    monkeypatch.chdir(tmp_path)
    config = load_config(_env_file=None)
    config.data_dir.mkdir(parents=True)
    config.data_dir.chmod(0o755)

    config.ensure_local_directories()

    mode = stat.S_IMODE(config.data_dir.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


def test_credentials_dir_defaults_under_data_dir_and_is_not_precreated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(_env_file=None)
    assert config.credentials_dir == tmp_path / "data" / "credentials"
    assert not config.credentials_dir.exists()

    config.ensure_local_directories()
    assert not config.credentials_dir.exists()  # deliberately not pre-created


def test_google_oauth_is_configured_requires_both_id_and_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(_env_file=None).google_oauth_is_configured is False

    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    assert load_config(_env_file=None).google_oauth_is_configured is False

    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    assert load_config(_env_file=None).google_oauth_is_configured is True


def test_feature_drive_enabled_defaults_false_so_drive_never_starts_unconfigured(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config = load_config(_env_file=None)
    assert config.feature_drive_enabled is False
    # Importing/loading configuration never fails just because Google
    # OAuth client credentials are absent (Prompt 10: "Feature flag must
    # allow the entire Drive integration to remain disabled without
    # import/startup failure").
    assert config.google_oauth_client_id is None
    assert config.google_oauth_client_secret is None
