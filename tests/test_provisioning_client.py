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
                 provision_status=200, profiles=("smoke",)):
        self.requests: list[tuple[str, str, dict]] = []
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
                if self.path != P.CAPABILITY_PATH or not fixture.capabilities:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"schema": fixture.schema, "backend": "hyvv", "profiles": fixture.profiles,
                                 "states": ["ready"], "supports_cleanup": True})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fixture.requests.append(("POST", self.path, body))
                if self.path == P.CLEANUP_PATH:
                    return self._send(200, {"state": "cleaned", "project": body.get("project")})
                if fixture.provision_status != 200:
                    return self._send(fixture.provision_status, {"error": "host failure"})
                self._send(200, {
                    "schema": P.CAPABILITY_SCHEMA, "status": "ready", "project": body.get("project", "rizzstack-smoke"),
                    "repo": body.get("repo"), "resolved_revision": fixture.receipt_sha,
                    "workspace": "C:\\Users\\kyle\\orbit-worker-fixtures\\rizzstack-smoke",
                    "profile": body.get("profile"), "host": "worker-1", "task_scopes": ["smoke/answer.py"],
                    "adopted": False,
                })

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


if __name__ == "__main__":
    unittest.main()
