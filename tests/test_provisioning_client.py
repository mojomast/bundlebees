"""Provisioning client tests against a realistic HTTP fixture (no mocks of the contract)."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bundlebees_plugin import provisioning as P

REPO = "mojomast/rizzstack-smoke"
SHA = "dde2aa42d09c05e43895b3120cb3312f765eb876"


class Fixture:
    def __init__(self, *, capabilities=True, schema=P.CAPABILITY_SCHEMA, receipt_sha=SHA,
                 provision_status=200, profiles=("smoke",), enabled=True, lookup=True,
                 lookup_status=200, expired=False):
        self.requests: list[tuple[str, str, dict]] = []
        self.enabled, self.lookup, self.lookup_status, self.expired = enabled, lookup, lookup_status, expired
        self.stored: dict[str, dict] = {}
        self.capabilities, self.schema, self.receipt_sha = capabilities, schema, receipt_sha
        self.provision_status, self.profiles = provision_status, list(profiles)
        self.handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def transport(self, method, path, body):
        import urllib.error
        import urllib.request

        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _make_handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                fixture.requests.append(("GET", self.path, {}))
                if self.path.startswith("/v1/provision/receipts/"):
                    key = self.path.rsplit("/", 1)[-1]
                    if fixture.lookup_status != 200:
                        return self._send(fixture.lookup_status, {"error": "receipt_stale", "message": "the workspace is gone"})
                    if key not in fixture.stored:
                        return self._send(404, {"error": "receipt_unknown"})
                    return self._send(200, fixture.stored[key])
                if self.path != P.CAPABILITY_PATH or not fixture.capabilities:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"schema": fixture.schema, "backend": "hyvv", "profiles": fixture.profiles,
                                 "states": ["ready"], "supports_cleanup": True, "enabled": fixture.enabled,
                                 "supports_receipt_lookup": fixture.lookup})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fixture.requests.append(("POST", self.path, body))
                if self.path == P.CLEANUP_PATH:
                    return self._send(200, {"state": "cleaned", "project": body.get("project")})
                if fixture.provision_status != 200:
                    return self._send(fixture.provision_status, {"error": "host failure"})
                receipt = {
                    "schema": P.CAPABILITY_SCHEMA, "status": "ready", "project": body.get("project", "rizzstack-smoke"),
                    "expires_at": "2000-01-01T00:00:00Z" if fixture.expired else "2099-01-01T00:00:00Z",
                    "repo": body.get("repo"), "resolved_revision": fixture.receipt_sha,
                    "workspace": "C:\\Users\\kyle\\orbit-worker-fixtures\\rizzstack-smoke",
                    "profile": body.get("profile"), "host": "worker-1", "task_scopes": ["smoke/answer.py"],
                    "adopted": False,
                }
                fixture.stored[str(body.get("idempotency_key"))] = receipt
                self._send(200, receipt)

        return Handler


class ClientTests(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = Fixture(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def request(self, **overrides):
        body = {"repo": REPO, "source_rev": SHA, "profile": "smoke", "job_id": "job_1",
                "idempotency_key": "prov_1", "project": "rizzstack-smoke"}
        body.update(overrides)
        return body

    def test_a_host_without_capabilities_is_incompatible_not_guessed(self):
        fixture = self.fixture(capabilities=False)
        client = P.HyvvProvisioner(fixture.transport)

        with self.assertRaises(P.ProvisionIncompatible) as caught:
            client.provision(self.request())
        self.assertEqual(caught.exception.code, "capability_missing")
        self.assertFalse([r for r in fixture.requests if r[0] == "POST"])

    def test_a_different_capability_schema_is_incompatible(self):
        fixture = self.fixture(schema="rizzstack.provisioning/0")
        with self.assertRaises(P.ProvisionIncompatible) as caught:
            P.HyvvProvisioner(fixture.transport).capabilities()
        self.assertEqual(caught.exception.code, "capability_schema")

    def test_a_profile_the_host_never_enrolled_is_denied_before_any_write(self):
        fixture = self.fixture(profiles=("other",))
        with self.assertRaises(P.ProvisionDenied) as caught:
            P.HyvvProvisioner(fixture.transport).provision(self.request())
        self.assertEqual(caught.exception.code, "profile_not_enrolled")
        self.assertFalse([r for r in fixture.requests if r[0] == "POST"])

    def test_a_ready_receipt_is_parsed_and_digestible(self):
        fixture = self.fixture()
        receipt = P.HyvvProvisioner(fixture.transport, expect_host="worker-1").provision(self.request())

        self.assertEqual(receipt.project, "rizzstack-smoke")
        self.assertEqual(receipt.resolved_revision, SHA)
        self.assertEqual(len(receipt.digest), 64)
        self.assertEqual(receipt.task_scopes, ("smoke/answer.py",))

    def test_a_receipt_at_another_revision_is_refused(self):
        fixture = self.fixture(receipt_sha="1" * 40)
        with self.assertRaises(P.ProvisionDenied) as caught:
            P.HyvvProvisioner(fixture.transport).provision(self.request())
        self.assertEqual(caught.exception.code, "receipt_revision")

    def test_a_receipt_from_an_unexpected_host_is_refused(self):
        fixture = self.fixture()
        with self.assertRaises(P.ProvisionDenied) as caught:
            P.HyvvProvisioner(fixture.transport, expect_host="worker-2").provision(self.request())
        self.assertEqual(caught.exception.code, "receipt_host")

    def test_a_503_is_uncertain_and_must_not_be_retried(self):
        fixture = self.fixture(provision_status=503)
        with self.assertRaises(P.ProvisionUncertain) as caught:
            P.HyvvProvisioner(fixture.transport).provision(self.request())
        self.assertEqual(caught.exception.code, "provision_uncertain")

    def test_a_binding_rejects_a_receipt_for_another_project(self):
        fixture = self.fixture()
        receipt = P.HyvvProvisioner(fixture.transport).provision(self.request())
        binding = P.Binding(repo=REPO, source_rev=SHA, host="worker-1", profile="smoke",
                            project="another-project", receipt_digest=receipt.digest,
                            contract_sha256="c" * 64)

        with self.assertRaises(P.ProvisionDenied) as caught:
            binding.validate(receipt, contract_sha256="c" * 64)
        self.assertEqual(caught.exception.code, "binding_project")

    def test_a_binding_rejects_a_changed_contract(self):
        fixture = self.fixture()
        receipt = P.HyvvProvisioner(fixture.transport).provision(self.request())
        binding = P.Binding(repo=REPO, source_rev=SHA, host="worker-1", profile="smoke",
                            project="rizzstack-smoke", receipt_digest=receipt.digest,
                            contract_sha256="c" * 64)

        with self.assertRaises(P.ProvisionDenied) as caught:
            binding.validate(receipt, contract_sha256="d" * 64)
        self.assertEqual(caught.exception.code, "binding_contract")

    def test_cleanup_returns_the_host_result(self):
        fixture = self.fixture()
        result = P.HyvvProvisioner(fixture.transport).cleanup("rizzstack-smoke")
        self.assertEqual(result["state"], "cleaned")

    def test_a_disabled_host_is_unavailable_before_any_write(self):
        fixture = self.fixture(enabled=False)
        with self.assertRaises(P.ProvisionUnavailable) as caught:
            P.HyvvProvisioner(fixture.transport).provision(self.request())
        self.assertEqual(caught.exception.code, "provisioning_disabled")
        self.assertFalse([r for r in fixture.requests if r[0] == "POST"])

    def test_a_lost_acknowledgement_is_recovered_by_receipt_lookup(self):
        fixture = self.fixture(provision_status=503)
        client = P.HyvvProvisioner(fixture.transport, expect_host="worker-1")
        with self.assertRaises(P.ProvisionUncertain):
            client.provision(self.request())

        # The host applied it; the coordinator recovers without re-POSTing.
        fixture.stored["prov_1"] = {
            "schema": P.CAPABILITY_SCHEMA, "status": "ready", "project": "rizzstack-smoke",
            "repo": REPO, "resolved_revision": SHA, "workspace": "C:\\w\\rizzstack-smoke",
            "profile": "smoke", "host": "worker-1", "task_scopes": [],
            "expires_at": "2099-01-01T00:00:00Z",
        }
        receipt = client.authoritative_receipt("prov_1", expect_repo=REPO, expect_rev=SHA,
                                               expect_profile="smoke", expect_project="rizzstack-smoke",
                                               expect_host="worker-1")
        self.assertEqual(receipt.resolved_revision, SHA)
        self.assertEqual(len([r for r in fixture.requests if r[1] == P.PROVISION_PATH]), 1)

    def test_a_stale_host_receipt_is_uncertain_not_usable(self):
        fixture = self.fixture(lookup_status=409)
        client = P.HyvvProvisioner(fixture.transport)
        with self.assertRaises(P.ProvisionUncertain) as caught:
            client.authoritative_receipt("prov_1", expect_repo=REPO, expect_rev=SHA,
                                         expect_profile="smoke", expect_project="rizzstack-smoke")
        self.assertEqual(caught.exception.code, "receipt_stale")

    def test_an_expired_receipt_cannot_be_bound(self):
        fixture = self.fixture(expired=True)
        with self.assertRaises(P.ProvisionDenied) as caught:
            P.HyvvProvisioner(fixture.transport).provision(self.request())
        self.assertEqual(caught.exception.code, "receipt_expired")

    def test_receipt_lookup_for_another_project_is_denied(self):
        fixture = self.fixture()
        P.HyvvProvisioner(fixture.transport).provision(self.request())
        with self.assertRaises(P.ProvisionDenied) as caught:
            P.HyvvProvisioner(fixture.transport).authoritative_receipt(
                "prov_1", expect_repo=REPO, expect_rev=SHA, expect_profile="smoke",
                expect_project="other-project")
        self.assertEqual(caught.exception.code, "receipt_project")


if __name__ == "__main__":
    unittest.main()
