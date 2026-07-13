import importlib
import os
import sys

import pytest

from hermes_cli.env_loader import (
    load_hermes_dotenv,
    reset_secret_source_cache,
)


def test_user_env_overrides_stale_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"


def test_project_env_overrides_stale_shell_values_when_user_env_missing(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://project.example/v1"


def test_project_env_is_sanitized_before_loading(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=0123456789:test"
        "ANTHROPIC_API_KEY=sk-ant-test123\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "0123456789:test"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-test123"


def test_user_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [user_env, project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"
    assert os.getenv("OPENAI_API_KEY") == "project-key"


def test_named_profile_does_not_inherit_shell_or_project_credentials(
    tmp_path, monkeypatch
):
    reset_secret_source_cache()
    home = tmp_path / ".hermes" / "profiles" / "child"
    home.mkdir(parents=True)
    user_env = home / ".env"
    user_env.write_text("# intentionally credential-free\n", encoding="utf-8")
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_API_KEY=project-secret\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ambient-bot")

    loaded = load_hermes_dotenv(
        hermes_home=home,
        project_env=project_env,
    )

    assert loaded == [user_env]
    assert "OPENAI_API_KEY" not in os.environ
    assert "TELEGRAM_BOT_TOKEN" not in os.environ


def test_named_profile_loads_only_its_explicit_credentials(tmp_path, monkeypatch):
    reset_secret_source_cache()
    home = tmp_path / ".hermes" / "profiles" / "child"
    home.mkdir(parents=True)
    user_env = home / ".env"
    user_env.write_text("OPENAI_API_KEY=profile-secret\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-anthropic")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [user_env]
    assert os.environ["OPENAI_API_KEY"] == "profile-secret"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_process_cannot_switch_between_named_profile_secret_authorities(
    tmp_path, monkeypatch
):
    reset_secret_source_cache()
    first = tmp_path / ".hermes" / "profiles" / "first"
    second = tmp_path / ".hermes" / "profiles" / "second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / ".env").write_text("OPENAI_API_KEY=first\n", encoding="utf-8")
    (second / ".env").write_text("OPENAI_API_KEY=second\n", encoding="utf-8")

    load_hermes_dotenv(hermes_home=first)

    with pytest.raises(RuntimeError, match="secret authority"):
        load_hermes_dotenv(hermes_home=second)


def test_process_cannot_switch_from_named_to_default_secret_authority(
    tmp_path, monkeypatch
):
    reset_secret_source_cache()
    named = tmp_path / ".hermes" / "profiles" / "child"
    default = tmp_path / ".hermes"
    named.mkdir(parents=True)
    (named / ".env").write_text("OPENAI_API_KEY=named\n", encoding="utf-8")
    (default / ".env").write_text("OPENAI_API_KEY=default\n", encoding="utf-8")

    load_hermes_dotenv(hermes_home=named)

    with pytest.raises(RuntimeError, match="secret authority"):
        load_hermes_dotenv(hermes_home=default)
    assert os.environ["OPENAI_API_KEY"] == "named"


def test_named_profile_rejects_symlinked_home_directory(tmp_path, monkeypatch):
    reset_secret_source_cache()
    profiles = tmp_path / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    (profiles / "child").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")

    with pytest.raises(RuntimeError, match="regular directory"):
        load_hermes_dotenv(hermes_home=profiles / "child")

    assert os.environ["OPENAI_API_KEY"] == "ambient"


def test_named_profile_does_not_sanitize_unused_project_env(
    tmp_path, monkeypatch
):
    reset_secret_source_cache()
    home = tmp_path / ".hermes" / "profiles" / "child"
    home.mkdir(parents=True)
    (home / ".env").write_text("# private\n", encoding="utf-8")
    project_env = tmp_path / "project.env"
    original = b"OPENAI_API_KEY=project\x00secret\n"
    project_env.write_bytes(original)

    load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert project_env.read_bytes() == original


def test_named_profile_rejects_symlinked_env_file(tmp_path, monkeypatch):
    reset_secret_source_cache()
    home = tmp_path / ".hermes" / "profiles" / "child"
    home.mkdir(parents=True)
    outside = tmp_path / "outside.env"
    outside.write_text("OPENAI_API_KEY=outside\n", encoding="utf-8")
    (home / ".env").symlink_to(outside)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")

    with pytest.raises(RuntimeError, match="regular"):
        load_hermes_dotenv(hermes_home=home)

    assert os.environ["OPENAI_API_KEY"] == "ambient"


def test_null_bytes_in_user_env_are_stripped(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Null bytes can be introduced when copy-pasting API keys.
    env_file.write_text("GLM_API_KEY=abc\x00\x00\nOPENAI_API_KEY=sk-123\n", encoding="utf-8")

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GLM_API_KEY") == "abc"
    assert os.getenv("OPENAI_API_KEY") == "sk-123"


def test_main_import_applies_user_env_over_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\nHERMES_INFERENCE_PROVIDER=custom\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")

    sys.modules.pop("hermes_cli.main", None)
    importlib.import_module("hermes_cli.main")

    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "custom"
