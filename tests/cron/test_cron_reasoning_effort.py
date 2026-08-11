"""Per-job reasoning-effort overrides for cron jobs."""

import json

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    cron_dir = hermes_home / "cron"
    output_dir = cron_dir / "output"
    scripts_dir = hermes_home / "scripts"
    output_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", output_dir)
    return hermes_home


def test_job_reasoning_effort_is_normalized_persisted_and_can_be_cleared(cron_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="Summarize", schedule="every 1h", reasoning_effort=" MeDiuM ")
    assert job["reasoning_effort"] == "medium"
    assert update_job(job["id"], {"reasoning_effort": "high"})["reasoning_effort"] == "high"
    assert update_job(job["id"], {"reasoning_effort": ""})["reasoning_effort"] is None


def test_job_reasoning_effort_rejects_invalid_value(cron_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="reasoning effort"):
        create_job(prompt="Summarize", schedule="every 1h", reasoning_effort="turbo")


def test_cron_tool_exposes_job_reasoning_effort(cron_env, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(action="create", prompt="Summarize", schedule="every 1h", reasoning_effort="low"))
    assert result["job"]["reasoning_effort"] == "low"


def test_scheduler_prefers_job_reasoning_effort_over_profile():
    import cron.scheduler as scheduler

    assert scheduler._resolve_cron_reasoning_config(
        {"reasoning_effort": "low"}, {"agent": {"reasoning_effort": "high"}}
    ) == {"enabled": True, "effort": "low"}


def test_cronjob_schema_advertises_every_supported_effort():
    from hermes_constants import VALID_REASONING_EFFORTS
    from tools.cronjob_tools import CRONJOB_SCHEMA

    assert CRONJOB_SCHEMA["parameters"]["properties"]["reasoning_effort"]["enum"] == [
        "", "none", *VALID_REASONING_EFFORTS
    ]
