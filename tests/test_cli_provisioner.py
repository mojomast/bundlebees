"""Gate 1: the production CLI must construct and pass the provisioner."""

from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

from bundlebees_plugin import provisioning as P
from bundlebees_plugin import job_cli


class FakeClient:
    def __init__(self):
        self.calls = []

    def request_json(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == P.CAPABILITY_PATH:
            return {"schema": P.CAPABILITY_SCHEMA, "backend": "hyvv", "profiles": ["smoke"],
                    "enabled": True, "supports_receipt_lookup": True, "states": ["ready"]}
        return {"status": "ready"}


class FactoryTests(unittest.TestCase):
    def test_a_disabled_config_yields_no_provisioner(self):
        self.assertIsNone(P.from_client({"provisioning": {"enabled": False}}, FakeClient()))
        self.assertIsNone(P.from_client({}, FakeClient()))

    def test_an_enabled_config_builds_the_http_provisioner(self):
        prov = P.from_client({"provisioning": {"enabled": True, "host": "worker-1"}}, FakeClient())
        self.assertIsInstance(prov, P.HyvvProvisioner)
        caps = prov.capabilities()
        self.assertEqual(caps.profiles, ("smoke",))
        self.assertTrue(caps.supports_receipt_lookup)

    def test_client_errors_map_into_the_provisioning_status_space(self):
        class Boom(FakeClient):
            def request_json(self, method, path, payload=None):
                if path == P.CAPABILITY_PATH:
                    return {"schema": P.CAPABILITY_SCHEMA, "backend": "hyvv", "profiles": ["smoke"],
                            "enabled": True, "supports_receipt_lookup": True}
                err = RuntimeError("gateway timeout")
                err.status = 503
                err.code = "upstream_timeout"
                raise err

        prov = P.from_client({"provisioning": {"enabled": True}}, Boom())
        with self.assertRaises(P.ProvisionUncertain):
            prov.provision({"repo": "mojomast/smoke-real", "source_rev": "a" * 40, "profile": "smoke",
                            "job_id": "j", "idempotency_key": "k", "project": "smoke-real"})


class CmdJobWiringTests(unittest.TestCase):
    def test_tick_receives_the_configured_provisioner(self):
        captured = {}

        def fake_tick(state_dir, job_id, **kwargs):
            captured.update(kwargs)
            return {"status": "ok"}

        original = job_cli.tick
        job_cli.tick = fake_tick
        self.addCleanup(lambda: setattr(job_cli, "tick", original))

        with TemporaryDirectory() as tmp:
            args = Namespace(command="job", job_handler="tick", job_id="j", repo=str(Path(tmp)), gitleaks=None,
                             state_dir=tmp, note=None)
            sentinel = object()
            code = job_cli.cmd_job(None, args, state_dir=Path(tmp), client_factory=lambda: FakeClient(),
                                   provisioner_factory=lambda: sentinel)
        self.assertEqual(code, 0)
        self.assertIs(captured.get("provisioner"), sentinel)

    def test_tick_without_a_factory_keeps_the_legacy_path(self):
        captured = {}

        def fake_tick(state_dir, job_id, **kwargs):
            captured.update(kwargs)
            return {"status": "ok"}

        original = job_cli.tick
        job_cli.tick = fake_tick
        self.addCleanup(lambda: setattr(job_cli, "tick", original))

        with TemporaryDirectory() as tmp:
            args = Namespace(command="job", job_handler="tick", job_id="j", repo=str(Path(tmp)), gitleaks=None,
                             state_dir=tmp, note=None)
            job_cli.cmd_job(None, args, state_dir=Path(tmp), client_factory=lambda: FakeClient())
        self.assertIsNone(captured.get("provisioner"))


if __name__ == "__main__":
    unittest.main()
