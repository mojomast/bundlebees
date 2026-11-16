"""The installed CLI must be able to enable provisioning through settings.

`cmd_job` builds its provisioner via ``provisioning.from_client(settings, client)``
and ``settings_from_ctx`` only returns *declared* keys. If ``provisioning`` is
absent from the settings schema, the flat config can never enable provisioning
and every provision-bearing job blocks forever with
"this job requires provisioning but no provisioner was supplied".
"""

import unittest

from bundlebees_plugin import provisioning as P
from bundlebees_plugin.settings import DEFAULTS, settings_from_ctx


class _Ctx:
    def __init__(self, values):
        self._values = dict(values)

    def get_config(self, key, default=None):
        return self._values.get(key, default)


class FlatSettingsEnableProvisioning(unittest.TestCase):
    def test_settings_schema_declares_the_provisioning_keys(self):
        self.assertIn("provisioning_enabled", DEFAULTS)
        self.assertIn("provisioning_host", DEFAULTS)

    def test_settings_from_ctx_carries_declared_provisioning_keys(self):
        settings = settings_from_ctx(_Ctx({"provisioning_enabled": True, "provisioning_host": "brrrrrrrr"}))
        self.assertTrue(settings["provisioning_enabled"])
        self.assertEqual(settings["provisioning_host"], "brrrrrrrr")

    def test_from_client_builds_from_flat_settings(self):
        class FakeClient:
            def request_json(self, method, path, body):
                return {"ok": True}

        settings = settings_from_ctx(_Ctx({"provisioning_enabled": True, "provisioning_host": "worker-1"}))
        prov = P.from_client(settings, FakeClient())
        self.assertIsNotNone(prov)
        self.assertEqual(prov.expect_host, "worker-1")

    def test_from_client_defaults_to_none(self):
        class FakeClient:
            pass

        self.assertIsNone(P.from_client(settings_from_ctx(_Ctx({})), FakeClient()))


if __name__ == "__main__":
    unittest.main()
