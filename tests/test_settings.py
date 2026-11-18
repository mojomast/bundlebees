

def test_settings_from_ctx_exposes_the_attribution_keys():
    """E9's gate reads the canary list and the worker facts through settings_from_ctx,
    which only returns keys present in DEFAULTS. Missing keys made the gate silently inert
    (a real canary dispatch went out unattributed before this test existed)."""
    from bundlebees_plugin import settings

    class Ctx:
        def get_config(self, key, default=None):
            return {
                "attribution_canary_projects": ["rizzstack-m2-canary"],
                "attribution_worker_id": "worker-1",
                "attribution_ledger_chain": "chain-1",
            }.get(key, default)

    out = settings.settings_from_ctx(Ctx())
    assert out["attribution_canary_projects"] == ["rizzstack-m2-canary"]
    assert out["attribution_worker_id"] == "worker-1"
    assert out["attribution_ledger_chain"] == "chain-1"
