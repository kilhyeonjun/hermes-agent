"""Per-job reasoning-effort overrides for cron jobs."""

import argparse
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron storage for create/update tool tests."""
    hermes_home = tmp_path / ".hermes"
    cron_dir = hermes_home / "cron"
    output_dir = cron_dir / "output"
    scripts_dir = hermes_home / "scripts"
    output_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", output_dir)
    return hermes_home


def test_create_and_update_job_reasoning_effort(cron_env):
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt="Summarize",
        schedule="every 1h",
        reasoning_effort=" MeDiuM ",
    )
    assert job["reasoning_effort"] == "medium"

    updated = update_job(job["id"], {"reasoning_effort": "high"})
    assert updated["reasoning_effort"] == "high"

    cleared = update_job(job["id"], {"reasoning_effort": ""})
    assert cleared["reasoning_effort"] is None


def test_rejects_invalid_reasoning_effort_on_create_and_update(cron_env):
    from cron.jobs import create_job, update_job

    with pytest.raises(ValueError, match="reasoning effort"):
        create_job(
            prompt="Summarize",
            schedule="every 1h",
            reasoning_effort="ultra",
        )

    job = create_job(prompt="Summarize", schedule="every 1h")
    with pytest.raises(ValueError, match="reasoning effort"):
        update_job(job["id"], {"reasoning_effort": "turbo"})


def test_cronjob_tool_create_update_list_reasoning_effort(cron_env, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="Summarize",
            schedule="every 1h",
            reasoning_effort="low",
        )
    )
    assert created["success"] is True
    assert created["job"]["reasoning_effort"] == "low"

    updated = json.loads(
        cronjob(
            action="update",
            job_id=created["job_id"],
            reasoning_effort="xhigh",
        )
    )
    assert updated["success"] is True
    assert updated["job"]["reasoning_effort"] == "xhigh"

    listed = json.loads(cronjob(action="list", include_disabled=True))
    assert listed["jobs"][0]["reasoning_effort"] == "xhigh"


def test_cronjob_tool_schema_advertises_reasoning_effort():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    prop = CRONJOB_SCHEMA["parameters"]["properties"]["reasoning_effort"]
    assert prop["enum"] == ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


def test_scheduler_prefers_job_reasoning_effort_then_profile():
    import cron.scheduler as scheduler

    resolve = getattr(scheduler, "_resolve_cron_reasoning_config")
    config = {"agent": {"reasoning_effort": "high"}}

    assert resolve({"reasoning_effort": "low"}, config) == {
        "enabled": True,
        "effort": "low",
    }
    assert resolve({}, config) == {"enabled": True, "effort": "high"}
    assert resolve({"reasoning_effort": "none"}, config) == {"enabled": False}


def test_cron_cli_parser_accepts_reasoning_effort_and_clear_flag():
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda args: 0)

    created = parser.parse_args(
        ["cron", "create", "every 1h", "Summarize", "--reasoning-effort", "low"]
    )
    assert created.reasoning_effort == "low"

    edited = parser.parse_args(
        ["cron", "edit", "job-1", "--clear-reasoning-effort"]
    )
    assert edited.clear_reasoning_effort is True


def test_cron_cli_create_forwards_reasoning_effort(monkeypatch):
    import hermes_cli.cron as cli_cron

    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "job_id": "job-1",
            "name": "test",
            "schedule": "every 1h",
            "next_run_at": "later",
            "job": {},
        }

    monkeypatch.setattr(cli_cron, "_cron_api", fake_api)
    monkeypatch.setattr(cli_cron, "_warn_if_gateway_not_running", lambda: None)
    args = SimpleNamespace(
        schedule="every 1h",
        prompt="Summarize",
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
        reasoning_effort="medium",
    )

    assert cli_cron.cron_create(args) == 0
    assert captured["reasoning_effort"] == "medium"


def test_cron_cli_edit_clear_forwards_empty_override(cron_env, monkeypatch):
    import cron.jobs as jobs
    import hermes_cli.cron as cli_cron

    job = jobs.create_job(prompt="Summarize", schedule="every 1h")
    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "job": {
                "job_id": job["id"],
                "name": "test",
                "schedule": "every 1h",
                "skills": [],
            },
        }

    monkeypatch.setattr(cli_cron, "_cron_api", fake_api)
    args = SimpleNamespace(
        job_id=job["id"],
        schedule=None,
        prompt=None,
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        add_skills=None,
        remove_skills=None,
        clear_skills=False,
        script=None,
        workdir=None,
        no_agent=None,
        reasoning_effort=None,
        clear_reasoning_effort=True,
    )

    assert cli_cron.cron_edit(args) == 0
    assert captured["reasoning_effort"] == ""
