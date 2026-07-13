"""Tests for credential exclusion during profile export.

Profile exports exclude canonical profile-root auth.json, auth.lock, .env,
and .op.env.
Nested project files with the same basenames remain ordinary project data.
"""

import os
import stat
import tarfile

import pytest

from hermes_cli import profiles
from hermes_cli.profiles import export_profile, _DEFAULT_EXPORT_EXCLUDE_ROOT


class TestCredentialExclusion:

    def test_auth_json_in_default_exclude_set(self):
        """auth.json must be in the default export exclusion set."""
        assert "auth.json" in _DEFAULT_EXPORT_EXCLUDE_ROOT

    def test_dotenv_in_default_exclude_set(self):
        """.env must be in the default export exclusion set."""
        assert ".env" in _DEFAULT_EXPORT_EXCLUDE_ROOT

    def test_onepassword_bootstrap_env_in_default_exclude_set(self):
        """The 1Password service-account bootstrap token is never portable."""
        assert ".op.env" in _DEFAULT_EXPORT_EXCLUDE_ROOT

    def test_auth_lock_in_default_exclude_set(self):
        """auth.lock must not be archived or restored with a stale inode."""
        assert "auth.lock" in _DEFAULT_EXPORT_EXCLUDE_ROOT

    def test_named_profile_export_excludes_auth(self, tmp_path, monkeypatch):
        """Named export omits every profile-root credential authority file."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        # Create a profile with credentials
        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        (profile_dir / "auth.json").write_text('{"tokens": {"access": "sk-secret"}}')
        (profile_dir / "auth.lock").write_text("stale-lock")
        (profile_dir / ".env").write_text("OPENROUTER_API_KEY=sk-secret-key\n")
        (profile_dir / ".op.env").write_text(
            "OP_SERVICE_ACCOUNT_TOKEN=op-secret\n"
        )
        (profile_dir / "SOUL.md").write_text("I am helpful.\n")
        (profile_dir / "memories").mkdir()
        (profile_dir / "memories" / "MEMORY.md").write_text("# Memories\n")
        nested = profile_dir / "workspace" / "project"
        nested.mkdir(parents=True)
        (nested / "auth.json").write_text('{"project": true}')

        monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: profiles_root)
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: profile_dir)
        monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)

        output = tmp_path / "export.tar.gz"
        result = export_profile("testprofile", str(output))

        # Check archive contents
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()

        assert any("config.yaml" in n for n in names), "config.yaml should be in export"
        assert any("SOUL.md" in n for n in names), "SOUL.md should be in export"
        assert "testprofile/auth.json" not in names
        assert "testprofile/auth.lock" not in names
        assert "testprofile/.env" not in names
        assert "testprofile/.op.env" not in names
        assert "testprofile/workspace/project/auth.json" in names

    def test_named_export_excludes_hardlink_alias_of_root_auth(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "hardlink-profile"
        profile_dir.mkdir(parents=True)
        auth_path = profile_dir / ".op.env"
        auth_path.write_text("OP_SERVICE_ACCOUNT_TOKEN=secret")
        (profile_dir / "innocent.bin").hardlink_to(auth_path)
        (profile_dir / "config.yaml").write_text("model: safe\n")

        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda _name: profile_dir
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.validate_profile_name", lambda _name: None
        )

        result = export_profile(
            "hardlink-profile", str(tmp_path / "hardlink-export.tar.gz")
        )

        with tarfile.open(result, "r:gz") as tf:
            names = set(tf.getnames())
        assert "hardlink-profile/.op.env" not in names
        assert "hardlink-profile/innocent.bin" not in names

    def test_export_refuses_symlink_output_target(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "safe"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: safe\n")
        outside = tmp_path / "outside.txt"
        outside.write_text("preserve-me")
        output = tmp_path / "export.tar.gz"
        output.symlink_to(outside)

        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda _name: profile_dir
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.validate_profile_name", lambda _name: None
        )

        with pytest.raises(ValueError, match="output"):
            export_profile("safe", str(output))
        assert outside.read_text() == "preserve-me"

    def test_named_export_ignores_symlink_inside_excluded_home(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "portable"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: safe\n")
        outside = tmp_path / "outside-home"
        outside.mkdir()
        (outside / "credential.txt").write_text("not-portable")
        (profile_dir / "home").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda _name: profile_dir
        )

        result = export_profile("portable", str(tmp_path / "portable.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            names = set(tf.getnames())
        assert not any(name.startswith("portable/home") for name in names)

    def test_named_export_rejects_hardlink_alias_of_excluded_home(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "portable"
        hidden = profile_dir / "home" / "credential.txt"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("not-portable")
        workspace = profile_dir / "workspace"
        workspace.mkdir()
        (workspace / "alias.txt").hardlink_to(hidden)
        (profile_dir / "config.yaml").write_text("model: safe\n")
        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda _name: profile_dir
        )

        with pytest.raises(ValueError, match="hard-linked"):
            export_profile("portable", str(tmp_path / "portable.tar.gz"))

    def test_export_output_race_replaces_symlink_without_following_it(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "safe"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: safe\n")
        outside = tmp_path / "outside.txt"
        outside.write_text("preserve-me")
        output = tmp_path / "export.tar.gz"
        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda _name: profile_dir
        )
        real_copytree = profiles.shutil.copytree

        def _copy_then_race(*args, **kwargs):
            result = real_copytree(*args, **kwargs)
            if not os.path.lexists(output):
                output.symlink_to(outside)
            return result

        monkeypatch.setattr(profiles.shutil, "copytree", _copy_then_race)

        result = export_profile("safe", str(output))

        assert outside.read_text() == "preserve-me"
        assert result == output
        assert output.is_file()
        assert not output.is_symlink()
        assert output.stat().st_nlink == 1
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
