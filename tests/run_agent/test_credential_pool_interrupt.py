"""Regression test for #26145: credential pool rotation after interrupt-resume.

When has_retried_429 is lost (user cancels between 429s), the pool should
still rotate if the current credential is already marked exhausted.
"""
from unittest.mock import MagicMock, patch

from agent.credential_pool import PooledCredential, STATUS_EXHAUSTED
from agent.error_classifier import FailoverReason


def _make_entry(idx, **overrides):
    defaults = dict(
        provider="test-provider",
        id=f"cred-{idx}",
        label=f"Credential {idx}",
        auth_type="api_key",
        priority=idx,
        source="manual",
        access_token=f"key-{idx}",
    )
    defaults.update(overrides)
    return PooledCredential(**defaults)


def _make_pool(entries):
    pool = MagicMock()
    pool.entries = MagicMock(return_value=entries)
    pool.current.return_value = entries[0]
    # Must be set explicitly — MagicMock.provider returns a truthy
    # child mock, which would trigger the provider-mismatch guard.
    pool.provider = ""
    return pool


def test_rotate_immediately_when_credential_already_exhausted():
    """If current credential has last_status='exhausted', rotate on first 429
    instead of retrying (Option A fix for #26145)."""
    entries = [_make_entry(0, last_status=STATUS_EXHAUSTED, last_error_code=429), _make_entry(1)]
    pool = _make_pool(entries)
    pool.mark_exhausted_and_rotate.return_value = entries[1]

    from run_agent import AIAgent
    with patch("model_tools.get_tool_definitions", return_value=[]),          patch("model_tools.check_toolset_requirements", return_value={}),          patch("agent.process_bootstrap.OpenAI"):
        agent = MagicMock(spec=AIAgent)
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        recovered, retried = AIAgent._recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=False,  # Key: False on first 429 after interrupt
            classified_reason=FailoverReason.rate_limit,
        )

    assert recovered is True
    assert retried is False
    pool.mark_exhausted_and_rotate.assert_called_once()


def test_codex_entitlement_rotates_model_scoped_entry():
    entries = [_make_entry(0), _make_entry(1)]
    pool = _make_pool(entries)
    pool.provider = "openai-codex"
    pool.mark_entitlement_unavailable_and_rotate.return_value = entries[1]
    from run_agent import AIAgent
    agent = MagicMock(spec=AIAgent)
    agent._credential_pool = pool
    agent.provider = "openai-codex"
    agent.model = "gpt-5.6-sol"
    agent.api_key = "key-0"
    agent._credential_pool_entry_id = "cred-0"
    agent._swap_credential = MagicMock()

    recovered, retried = AIAgent._recover_with_credential_pool(
        agent, status_code=400, has_retried_429=False,
        classified_reason=FailoverReason.entitlement)

    assert (recovered, retried) == (True, False)
    pool.mark_entitlement_unavailable_and_rotate.assert_called_once_with(
        model="gpt-5.6-sol", api_key_hint="key-0", credential_id="cred-0")
    agent._swap_credential.assert_called_once_with(entries[1])


def test_persisted_codex_entitlement_is_skipped_and_resettable():
    entries = [
        _make_entry(0, extra={"unavailable_models": ["gpt-5.6-sol"]}),
        _make_entry(1),
    ]
    from agent.credential_pool import CredentialPool
    pool = CredentialPool("openai-codex", entries)

    assert pool.select(model="gpt-5.6-sol").id == "cred-1"
    with patch.object(pool, "_persist"):
        assert pool.reset_statuses() == 1
    assert pool.select(model="gpt-5.6-sol").id == "cred-0"




def test_rotate_on_second_429_when_not_exhausted():
    """When credential is active and this is the second 429, rotate (existing behavior)."""
    entries = [_make_entry(0, last_status=None), _make_entry(1)]
    pool = _make_pool(entries)
    pool.mark_exhausted_and_rotate.return_value = entries[1]

    from run_agent import AIAgent
    with patch("model_tools.get_tool_definitions", return_value=[]),          patch("model_tools.check_toolset_requirements", return_value={}),          patch("agent.process_bootstrap.OpenAI"):
        agent = MagicMock(spec=AIAgent)
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        recovered, retried = AIAgent._recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=True,  # Second 429
            classified_reason=FailoverReason.rate_limit,
        )

    assert recovered is True
    assert retried is False
    pool.mark_exhausted_and_rotate.assert_called_once()
