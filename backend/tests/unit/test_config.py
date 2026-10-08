from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ClassifierProvider, LLMProvider, Settings


def test_bad_llm_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "olama")
    with pytest.raises(ValidationError):
        Settings()


def test_provider_defaults_are_self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLASSIFIER_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = Settings()
    assert settings.classifier_provider is ClassifierProvider.LAYA
    assert settings.llm_provider is LLMProvider.OLLAMA


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLASSIFIER_PROVIDER", "jev")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    settings = Settings()
    assert settings.classifier_provider is ClassifierProvider.JEV
    assert settings.llm_provider is LLMProvider.ANTHROPIC


def test_poll_interval_lower_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "5")
    with pytest.raises(ValidationError):
        Settings()


def test_missing_db_password_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD")
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_escapes_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "p@ss:w/rd")
    url = Settings().database_url
    assert url.password == "p@ss:w/rd"  # the real value is intact
    assert "p%40ss%3Aw%2Frd" in url.render_as_string(hide_password=False)  # encoded in the URL


def test_password_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    settings = Settings()
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.database_url)


def test_password_from_secrets_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DB_PASSWORD")
    (tmp_path / "db_password").write_text("from-file")
    settings = Settings(_secrets_dir=tmp_path)  # same mechanism as /run/secrets in Docker
    assert settings.db_password.get_secret_value() == "from-file"
