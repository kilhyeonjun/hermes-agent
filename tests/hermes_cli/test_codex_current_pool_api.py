from types import SimpleNamespace


def test_codex_usage_accepts_current_available_entries_tuple(monkeypatch, tmp_path):
    import hermes_cli.codex_usage as usage

    entry = SimpleNamespace(
        id="id-a", label="personal-backup", priority=0, source="manual",
        last_status=None, last_error_reset_at=None, extra={}, runtime_api_key="token",
    )

    class Pool:
        _strategy = "fill_first"
        def _available_entries(self, **_kwargs):
            return [entry], []
        def entries(self):
            return [entry]
        def current(self):
            return entry

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: Pool())
    monkeypatch.setattr(usage, "ROUTE_POLICY_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(
        usage, "fetch_usage",
        lambda *_args, **_kwargs: {"rate_limit": {"primary_window": {}, "secondary_window": {}}},
    )
    payload = usage.collect(mutate=False)
    assert payload["routing"]["current_label"] == "personal-backup"


def test_credential_label_accepts_current_available_entries_tuple():
    from agent.credential_usage import resolve_credential_label

    entry = SimpleNamespace(runtime_api_key="token", label="company")

    class Pool:
        def _available_entries(self, **_kwargs):
            return [entry], []
        def current(self):
            return None

    agent = SimpleNamespace(provider="openai-codex", api_key="token", _credential_pool=Pool())
    assert resolve_credential_label(agent) == "company"
