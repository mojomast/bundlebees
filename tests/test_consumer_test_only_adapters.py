"""RED for wave A slice A10: test-only consumer controls need an explicit test flag.

The ``authorized`` recovery adapter derives a superseding contract, and
``BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL`` kills the consumer on purpose. Both
exist for the crash and wake-up tests. Neither may be reachable in production
through a work item's adapter hint, a CLI flag or a stray environment
variable unless ``BUNDLEBEES_TEST_ADAPTERS=1`` is set.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import agent_consumer


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BUNDLEBEES_TEST_ADAPTERS", raising=False)
    monkeypatch.delenv(agent_consumer.CRASH_ENV, raising=False)


def test_flag_name_is_a_constant():
    assert agent_consumer.TEST_ADAPTERS_ENV == "BUNDLEBEES_TEST_ADAPTERS"


def test_production_adapters_exclude_authorized():
    assert "authorized" not in agent_consumer.allowed_adapters()
    assert {"hermes", "deterministic"} <= set(agent_consumer.allowed_adapters())


def test_flag_enables_authorized(monkeypatch):
    monkeypatch.setenv("BUNDLEBEES_TEST_ADAPTERS", "1")
    assert "authorized" in agent_consumer.allowed_adapters()


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1"])
def test_only_exact_one_enables(monkeypatch, value):
    monkeypatch.setenv("BUNDLEBEES_TEST_ADAPTERS", value)
    assert "authorized" not in agent_consumer.allowed_adapters()


def test_choosing_authorized_without_flag_is_refused():
    with pytest.raises(agent_consumer.ConsumerError) as caught:
        agent_consumer.resolve_adapter("authorized", {"adapter": "hermes"})
    assert caught.value.code == "adapter_test_only"


def test_work_item_hint_cannot_pick_authorized_without_flag():
    with pytest.raises(agent_consumer.ConsumerError) as caught:
        agent_consumer.resolve_adapter(None, {"adapter": "authorized"})
    assert caught.value.code == "adapter_test_only"


def test_resolve_adapter_defaults_to_hermes():
    assert agent_consumer.resolve_adapter(None, {}) == "hermes"


def test_unknown_adapter_keeps_its_code():
    with pytest.raises(agent_consumer.ConsumerError) as caught:
        agent_consumer.resolve_adapter("nope", {})
    assert caught.value.code == "adapter_unknown"


def test_crash_switch_ignored_without_flag(monkeypatch):
    monkeypatch.setenv(agent_consumer.CRASH_ENV, "1")
    assert agent_consumer.crash_armed() is False


def test_crash_switch_armed_with_flag(monkeypatch):
    monkeypatch.setenv(agent_consumer.CRASH_ENV, "1")
    monkeypatch.setenv("BUNDLEBEES_TEST_ADAPTERS", "1")
    assert agent_consumer.crash_armed() is True
