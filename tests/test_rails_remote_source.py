"""Rails R09 remote peer-source acceptance tests (RED-first).

The fixtures speak the frozen ``rizzstack.rails-export/1`` wire contract:
exclusive numeric ``after``, full ``rizzstack.rails/1`` envelopes, ``head`` and
``has_more``. The transport is injected so no socket is opened. Every digest is
derived at run time; no literal digest or credential is stored here.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
for path in (REPO_ROOT, CONTRIB_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from rizzstack_rails import contracts as rails_contracts
from rizzstack_rails import ledger as rails_ledger
from bundlebees_plugin import rails_remote_source as remote

CHAIN_ID = "c1"
PEER_ID = "peer-a"
BASE_URL = "https://peer.example:8443"
EXPORT_SCHEMA = "rizzstack.rails-export/1"
BASE_STAMP = "2026-01-01T00:00:00.000000Z"
BASE_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _pages(count, chain_id=CHAIN_ID):
    prev = rails_contracts.GENESIS
    pages = []
    for seq in range(1, count + 1):
        envelope = {
            "schema": rails_contracts.SCHEMA,
            "chain_id": chain_id,
            "seq": seq,
            "event_id": chain_id + ":" + str(seq),
            "dedupe_key": "dedupe-" + str(seq),
            "prev_hash": prev,
            "kind": "decision.recorded",
            "source": {
                "adapter": "origin",
                "instance": "origin-a",
                "record_id": "rec-" + str(seq),
                "revision": "rev-1",
            },
            "scope": {key: "task-1" if key == "task_id" else None for key in
                      ("repo", "campaign_id", "job_id", "task_id", "attempt_id", "session_id", "action_id", "contract_sha256", "code_rev")},
            "lane_id": "lane-origin",
            "parent_lane_id": None,
            "parent_event_id": None,
            "occurred_at": None,
            "recorded_at": BASE_STAMP,
            "duration_ms": None,
            "timing_quality": "unknown",
            "authority": "imported_observation",
            "payload": {"summary": "d" + str(seq)},
            "redaction_policy": "rails-safe/1",
        }
        envelope["hash"] = rails_contracts.rails_hash(prev, envelope)
        prev = envelope["hash"]
        pages.append(rails_contracts.canonicalize(envelope).decode("ascii"))
    return pages


def _mutate(pages, seq, payload):
    parsed = [json.loads(text) for text in pages]
    prev = parsed[seq - 2]["hash"] if seq > 1 else rails_contracts.GENESIS
    for index in range(seq - 1, len(parsed)):
        envelope = {key: value for key, value in parsed[index].items() if key != "hash"}
        if index == seq - 1:
            envelope["payload"] = payload
        envelope["prev_hash"] = prev
        envelope["hash"] = rails_contracts.rails_hash(prev, envelope)
        parsed[index] = envelope
        prev = envelope["hash"]
    return [rails_contracts.canonicalize(item).decode("ascii") for item in parsed]


def _event(text):
    return json.loads(text)


def _body(
    pages,
    *,
    after=0,
    next_cursor=None,
    head=None,
    has_more=False,
    chain_id=CHAIN_ID,
    **extra
):
    events = [_event(text) for text in pages]
    if next_cursor is None:
        next_cursor = events[-1]["seq"] if events else after
    if head is None and events:
        head = {"seq": events[-1]["seq"], "hash": events[-1]["hash"]}
    body = {
        "schema": EXPORT_SCHEMA,
        "chain_id": chain_id,
        "after": after,
        "events": events,
        "next_cursor": next_cursor,
        "head": head,
        "has_more": has_more,
    }
    body.update(extra)
    return body


def _head(texts):
    if not texts:
        return None
    envelope = _event(texts[-1])
    return {"seq": envelope["seq"], "hash": envelope["hash"]}


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, url=None):
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.url = url


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, url, **kwargs):
        self.requests.append(url)
        if not self.responses:
            raise AssertionError("unexpected transport request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(body, status=200, headers=None, url=None):
    return FakeResponse(
        status=status,
        body=json.dumps(body).encode("utf-8"),
        headers=headers,
        url=url,
    )


class RemoteSourceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rails-r09-")
        self.addCleanup(self._tmp.cleanup)
        self.now = lambda: BASE_DT.timestamp()

    def source(self, transport, **overrides):
        options = {
            "base_url": BASE_URL,
            "peer_id": PEER_ID,
            "transport": transport,
            "now": self.now,
            "stale_after_seconds": 60,
        }
        options.update(overrides)
        return remote.RemoteRailSource(**options)


class EnrollmentTests(RemoteSourceCase):
    def test_real_pinned_read_sends_scoped_credential_without_exposing_it(self):
        import secrets
        secret = secrets.token_urlsafe(24)
        class Peer(BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.headers.get("Authorization") == "Bearer " + secret
                self.send_response(200 if ok else 401)
                self.end_headers()
                self.wfile.write(json.dumps(_body(_pages(1)) if ok else {}).encode())
            def log_message(self, *_):
                pass
        with HTTPServer(("127.0.0.1", 0), Peer) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                peer = remote.RemoteRailSource(base_url="http://127.0.0.1:%d" % server.server_port,
                                               token_provider=lambda: secret)
                report = peer.fetch(CHAIN_ID)
                self.assertEqual(report['count'], 1)
                for private in (secret, '127.0.0.1'):
                    self.assertNotIn(private, json.dumps([report, peer.status()]))
            finally:
                server.shutdown()
                thread.join()

    def test_missing_secret_refuses_before_network_io(self):
        peer = remote.RemoteRailSource(base_url=BASE_URL, token_provider=lambda: None)
        with patch.object(remote.launcher_client, '_pinned_request') as send, self.assertRaises(remote.EnrollmentRefused):
            peer.fetch(CHAIN_ID)
        send.assert_not_called()

    def test_peer_override_cannot_inherit_enrolled_launcher_credential(self):
        with patch.object(remote.launcher_client, 'get_launcher_token') as secret:
            peer = remote.RemoteRailSource(base_url=BASE_URL, config={'launcher_url': 'https://launcher.example'})
            with self.assertRaises(remote.EnrollmentRefused):
                peer.fetch(CHAIN_ID)
            secret.assert_not_called()

    def test_enrolled_launcher_requests_dedicated_read_secret(self):
        with patch.object(remote.launcher_client, 'get_launcher_token', return_value=None) as secret:
            peer = remote.RemoteRailSource(config={'launcher_url': BASE_URL}, env={})
            with self.assertRaises(remote.EnrollmentRefused):
                peer.fetch(CHAIN_ID)
            secret.assert_called_once_with('BUNDLEBEES_RAILS_READ_TOKEN')

    def test_enrolled_base_url_from_explicit_config(self):
        source = remote.RemoteRailSource(
            config={"peer_base_url": BASE_URL, "peer_id": PEER_ID},
            transport=FakeTransport(),
            now=self.now,
        )
        self.assertEqual(source.base_url, BASE_URL)
        self.assertEqual(source.peer_id, PEER_ID)
        self.assertEqual(
            source.export_url(CHAIN_ID, cursor=0, limit=50),
            BASE_URL + "/v1/rails/events?after=0&limit=50",
        )

    def test_enrolled_base_url_from_environment(self):
        source = remote.RemoteRailSource(
            env={
                "BUNDLEBEES_RAILS_PEER_BASE_URL": BASE_URL,
                "BUNDLEBEES_RAILS_PEER_ID": PEER_ID,
            },
            transport=FakeTransport(),
            now=self.now,
        )
        self.assertEqual(source.base_url, BASE_URL)

    def test_missing_or_unsafe_enrollment_is_refused(self):
        with self.assertRaises(remote.EnrollmentRefused):
            remote.RemoteRailSource(env={}, transport=FakeTransport(), now=self.now)
        for base in (
            "ftp://peer.example/rails",
            "https://u:p@peer.example/rails",
            "https://peer.example/rails?mode=read",
            "https://peer.example/rails#frag",
            "https://peer.example/../rails",
            "not-a-url",
        ):
            with self.assertRaises(remote.EnrollmentRefused, msg=base):
                remote.RemoteRailSource(
                    base_url=base, peer_id=PEER_ID, transport=FakeTransport()
                )

    def test_describe_names_the_frozen_export_format(self):
        source = self.source(FakeTransport())
        described = source.describe()
        self.assertEqual(described["adapter_id"], "rails_remote")
        self.assertEqual(described["version"], "rizzstack.rails-source/1")
        self.assertIn(EXPORT_SCHEMA, described["supported_formats"])


class PolicyTests(RemoteSourceCase):
    def test_off_policy_urls_are_refused_before_transport(self):
        transport = FakeTransport()
        source = self.source(transport)
        for url in (
            "https://other.example/v1/rails/events?after=0&limit=10",
            "http://peer.example:8443/v1/rails/events?after=0&limit=10",
            "https://peer.example/v1/rails/events?after=0&limit=10",
            "https://peer.example:8443/rails/events?after=0&limit=10",
            "https://peer.example:8443/v1/rails/head",
            "https://peer.example:8443/v1/rails/events?after=0&limit=10&mode=read",
            "https://peer.example:8443/v1/rails/events?after=0&after=1&limit=10",
            "https://u:p@peer.example:8443/v1/rails/events?after=0&limit=10",
            "ftp://peer.example:8443/v1/rails/events?after=0&limit=10",
        ):
            with self.assertRaises(remote.OffPolicyUrlRefused, msg=url):
                source.fetch_url(url)
        self.assertEqual(transport.requests, [])

    def test_redirect_is_refused_and_never_followed(self):
        transport = FakeTransport(
            _response({}, status=302, headers={"Location": "https://evil.example/x"})
        )
        with self.assertRaises(remote.RedirectRefused) as caught:
            self.source(transport).fetch(CHAIN_ID)
        self.assertEqual(caught.exception.code, "redirect_refused")
        self.assertEqual(len(transport.requests), 1)

    def test_same_origin_redirect_is_still_refused(self):
        transport = FakeTransport(
            _response(
                {},
                status=307,
                headers={"Location": BASE_URL + "/v1/rails/events?after=0&limit=500"},
            )
        )
        with self.assertRaises(remote.RedirectRefused):
            self.source(transport).fetch(CHAIN_ID)

    def test_transport_that_lands_off_policy_is_refused(self):
        transport = FakeTransport(
            _response(
                _body([]),
                url="https://evil.example/v1/rails/events?after=0&limit=500",
            )
        )
        with self.assertRaises(remote.OffPolicyUrlRefused):
            self.source(transport).fetch(CHAIN_ID)

    def test_after_alias_and_get_only_transport(self):
        class GetOnly:
            def __init__(self, response):
                self.response = response

            def get(self, url, **kwargs):
                return self.response

        body = _body(_pages(1), next_cursor=1)
        source = self.source(GetOnly(_response(body)))
        report = source.fetch(CHAIN_ID, after=0)
        self.assertEqual(report["origin_bytes"], _pages(1))

    def test_export_url_has_only_after_and_limit(self):
        source = self.source(FakeTransport())
        url = source.export_url(CHAIN_ID, cursor=3, limit=25)
        self.assertEqual(
            url, BASE_URL + "/v1/rails/events?after=3&limit=25"
        )
        self.assertNotIn("chain_id", url)
        for bad in (-1, True):
            with self.assertRaises(remote.PeerProtocolRefused):
                source.export_url(CHAIN_ID, cursor=bad)
        with self.assertRaises(remote.PeerProtocolRefused):
            source.export_url(CHAIN_ID, limit=501)


class OriginBytesTests(RemoteSourceCase):
    def test_origin_bytes_and_hashes_are_preserved_verbatim(self):
        pages = _pages(3)
        transport = FakeTransport(_response(_body(pages, next_cursor=3)))
        report = self.source(transport).fetch(CHAIN_ID)
        self.assertEqual(report["origin_bytes"], pages)
        self.assertEqual(report["events"], pages)
        expected = [_event(text)["hash"] for text in pages]
        self.assertEqual(report["origin_hashes"], expected)
        self.assertEqual(
            report["bytes_sha256"],
            [hashlib.sha256(text.encode("ascii")).hexdigest() for text in pages],
        )
        self.assertTrue(report["preserved"])
        self.assertEqual(report["range"], [1, 3])
        self.assertEqual(report["next_cursor"], 3)
        self.assertEqual(report["next_seq"], 4)
        self.assertEqual(report["count"], 3)
        self.assertIsNone(report["gap"])
        self.assertIsNone(report["loss"])

    def test_tampered_payload_without_rehash_is_refused(self):
        pages = _pages(2)
        parsed = _event(pages[1])
        parsed["payload"] = {"decision": "tampered"}
        body = _body([pages[0]], next_cursor=1)
        body["events"].append(parsed)
        body["next_cursor"] = 2
        with self.assertRaises(remote.OriginBytesRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)

    def test_noncanonical_origin_text_is_refused_not_reserialized(self):
        pages = _pages(1)
        spaced = pages[0].replace("{", "{ ", 1)
        body = _body([], next_cursor=1)
        body["events"] = [spaced]
        with self.assertRaises(remote.OriginBytesRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)

    def test_wrong_export_schema_is_refused(self):
        body = _body(_pages(1), next_cursor=1)
        body["schema"] = "rizzstack.rails-export/2"
        with self.assertRaises(remote.PeerProtocolRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)

    def test_after_mismatch_is_refused(self):
        body = _body(_pages(1), after=5, next_cursor=1)
        with self.assertRaises(remote.PeerProtocolRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)

    def test_chain_id_mismatch_is_refused(self):
        body = _body(_pages(2), chain_id="other", next_cursor=2)
        with self.assertRaises(remote.PeerProtocolRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)
    def test_head_hash_mismatch_is_refused(self):
        pages = _pages(2)
        body = _body(pages, next_cursor=2)
        body["head"] = {"seq": 2, "hash": _event(pages[0])["hash"]}
        with self.assertRaises(remote.OriginBytesRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)

    def test_head_behind_events_is_refused(self):
        pages = _pages(2)
        body = _body(pages, next_cursor=2, head=_head(pages[:1]))
        with self.assertRaises(remote.PeerProtocolRefused):
            self.source(FakeTransport(_response(body))).fetch(CHAIN_ID)


class GapTests(RemoteSourceCase):
    def test_pagination_metadata_cannot_skip_unverified_rows(self):
        for events, more, cursor in (([], True, 0), (_pages(1), True, 9), (_pages(1), False, 4)):
            with self.subTest(cursor=cursor):
                bad = _body(events, has_more=more, next_cursor=cursor)
                peer = self.source(FakeTransport(_response(bad)))
                with self.assertRaises(remote.PeerProtocolRefused):
                    peer.fetch(CHAIN_ID)
                self.assertEqual(peer.status(CHAIN_ID)['cursor'], 0)

    def test_leading_gap_is_reported_and_not_filled(self):
        pages = _pages(5)
        transport = FakeTransport(_response(_body(pages[2:], next_cursor=5)))
        report = self.source(transport).fetch(CHAIN_ID)
        self.assertEqual(report["gap"], {"start": 1, "end": 2})
        self.assertEqual(report["gaps"], [{"start": 1, "end": 2}])
        self.assertFalse(report["contiguous"])
        self.assertFalse(report["filled"])
        self.assertEqual(report["importable_events"], [])
        self.assertEqual(
            [_event(text)["seq"] for text in report["events"]], [3, 4, 5]
        )
        self.assertEqual(report["status"], "gap")

    def test_internal_and_trailing_gaps_are_reported(self):
        pages = _pages(5)
        holes = [pages[0], pages[1], pages[3], pages[4]]
        report = self.source(FakeTransport(_response(_body(holes)))).fetch(CHAIN_ID)
        self.assertEqual(report["gaps"], [{"start": 3, "end": 3}])
        self.assertEqual(report["importable_events"], pages[:2])
        trailing = self.source(
            FakeTransport(
                _response(
                    _body(pages[:2], next_cursor=2, head=_head(pages))
                )
            )
        ).fetch(CHAIN_ID)
        self.assertEqual(trailing["gaps"], [{"start": 3, "end": 5}])
        self.assertFalse(trailing["complete"])

    def test_has_more_does_not_create_a_trailing_gap(self):
        pages = _pages(5)
        report = self.source(
            FakeTransport(
                _response(
                    _body(pages[:2], next_cursor=2, head=_head(pages), has_more=True)
                )
            )
        ).fetch(CHAIN_ID)
        self.assertEqual(report["gaps"], [])
        self.assertTrue(report["has_more"])
        self.assertFalse(report["complete"])

    def test_strict_gap_raises_typed_error(self):
        pages = _pages(3)
        transport = FakeTransport(_response(_body(pages[1:], next_cursor=3)))
        with self.assertRaises(remote.ExportGapDetected) as caught:
            self.source(transport).fetch(CHAIN_ID, strict=True)
        self.assertEqual(caught.exception.code, "export_gap")
        self.assertEqual(caught.exception.gaps, [{"start": 1, "end": 1}])

    def test_gap_cannot_be_committed(self):
        pages = _pages(3)
        source = self.source(FakeTransport(_response(_body(pages[1:], next_cursor=3))))
        report = source.fetch(CHAIN_ID)
        with self.assertRaises(remote.ExportGapDetected):
            source.commit(report)


class StaleHostTests(RemoteSourceCase):
    def test_successful_contact_without_peer_clock_is_fresh(self):
        report = self.source(FakeTransport(_response(_body(_pages(1), next_cursor=1)))).fetch(
            CHAIN_ID
        )
        self.assertTrue(report["fresh"])
        self.assertFalse(report["stale"])
        self.assertEqual(report["status"], "ok")

    def test_old_peer_clock_is_stale_and_not_fresh(self):
        old = _stamp(BASE_DT - timedelta(minutes=10))
        report = self.source(
            FakeTransport(
                _response(
                    _body(_pages(1), next_cursor=1),
                    headers={"X-Rails-Last-Seen": old},
                )
            )
        ).fetch(CHAIN_ID)
        self.assertTrue(report["stale"])
        self.assertFalse(report["fresh"])
        self.assertEqual(report["stale_reason"], "age_exceeded")
        self.assertGreaterEqual(report["last_seen_age_seconds"], 600)
        self.assertEqual(report["status"], "stale")

    def test_invalid_peer_clock_is_never_fresh(self):
        report = self.source(
            FakeTransport(
                _response(
                    _body(_pages(1), next_cursor=1),
                    headers={"X-Rails-Last-Seen": "not-a-time"},
                )
            )
        ).fetch(CHAIN_ID)
        self.assertTrue(report["stale"])
        self.assertFalse(report["fresh"])
        self.assertEqual(report["stale_reason"], "last_seen_invalid")

    def test_peer_health_retains_observed_staleness_after_restart(self):
        state_path = os.path.join(self._tmp.name, 'peer.json')
        for stamp in (_stamp(BASE_DT - timedelta(minutes=10)), _stamp(BASE_DT + timedelta(minutes=10)), 'invalid'):
            with self.subTest(stamp=stamp):
                reply = _response(_body(_pages(1)), headers={'X-Rails-Last-Seen': stamp})
                reader = self.source(FakeTransport(reply), state_path=state_path)
                self.assertTrue(reader.fetch(CHAIN_ID)['stale'])
                self.assertFalse(self.source(FakeTransport(), state_path=state_path).health()['fresh'])

    def test_require_fresh_raises_typed_error(self):
        old = _stamp(BASE_DT - timedelta(minutes=10))
        transport = FakeTransport(
            _response(
                _body(_pages(1), next_cursor=1),
                headers={"X-Rails-Last-Seen": old},
            )
        )
        with self.assertRaises(remote.StaleHostDetected) as caught:
            self.source(transport).fetch(CHAIN_ID, require_fresh=True)
        self.assertEqual(caught.exception.code, "stale_host")

    def test_health_uses_local_last_seen_and_never_calls_old_fresh(self):
        state_path = os.path.join(self._tmp.name, "state.json")
        source = self.source(
            FakeTransport(_response(_body(_pages(1), next_cursor=1))),
            state_path=state_path,
        )
        source.fetch(CHAIN_ID)
        self.assertEqual(source.health()["state"], "live")
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        old = BASE_DT.timestamp() - 600
        state["peer"]["last_seen"] = old
        state["peer"]["last_seen_stamp"] = _stamp(BASE_DT - timedelta(minutes=10))
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        reopened = self.source(FakeTransport(), state_path=state_path)
        health = reopened.health()
        self.assertEqual(health["state"], "stale")
        self.assertTrue(health["stale"])
        self.assertFalse(health["fresh"])

    def test_health_without_contact_is_unavailable(self):
        self.assertEqual(self.source(FakeTransport()).health()["state"], "unavailable")


class LossAfterCommitTests(RemoteSourceCase):
    def test_partial_export_anchors_covered_rows_and_partial_replay_is_not_loss(self):
        pages = _pages(3)
        page_one = _response(_body(pages[:1], head=_head(pages), has_more=True))
        peer = self.source(FakeTransport(page_one, _response(_body(pages[1:], after=1)), page_one))
        self.assertEqual(peer.commit(peer.fetch(CHAIN_ID))["head_hash"], _head(pages[:1])["hash"])
        peer.commit(peer.fetch(CHAIN_ID, after=1))
        prefix = peer.fetch(CHAIN_ID)
        self.assertIsNone(prefix["loss"])
        self.assertEqual(peer.commit(prefix)["cursor"], 3)
        self.assertEqual(peer.status(CHAIN_ID)["head_hash"], _head(pages)["hash"])

    def _committed(self, pages, **source_options):
        transport = FakeTransport(_response(_body(pages, next_cursor=_event(pages[-1])["seq"])))
        source = self.source(transport, **source_options)
        report = source.fetch(CHAIN_ID)
        source.commit(report)
        return source

    def test_peer_cursor_behind_commit_is_loss(self):
        pages = _pages(3)
        source = self._committed(pages)
        source.transport = FakeTransport(
            _response(_body(pages[:2], next_cursor=2))
        )
        with self.assertRaises(remote.LossAfterCommit) as caught:
            source.replay_after_commit(CHAIN_ID)
        error = caught.exception
        self.assertEqual(error.code, "loss_after_commit")
        self.assertEqual(error.committed_cursor, 3)
        self.assertEqual(error.committed_next_seq, 4)
        self.assertEqual(error.peer_cursor, 2)
        self.assertEqual(error.peer_next_seq, 3)
        self.assertIn(3, error.missing)

    def test_missing_committed_event_is_loss_not_silent(self):
        pages = _pages(4)
        source = self._committed(pages)
        source.transport = FakeTransport(
            _response(
                _body([pages[0], pages[1], pages[3]], next_cursor=4, head=_head(pages))
            )
        )
        with self.assertRaises(remote.LossAfterCommit) as caught:
            source.replay_after_commit(CHAIN_ID)
        self.assertIn(3, caught.exception.missing)

    def test_forked_committed_event_is_loss(self):
        pages = _pages(3)
        source = self._committed(pages)
        forked = _mutate(pages, 2, {"decision": "forked"})
        source.transport = FakeTransport(
            _response(_body(forked, next_cursor=3))
        )
        with self.assertRaises(remote.LossAfterCommit) as caught:
            source.replay_after_commit(CHAIN_ID)
        self.assertEqual(caught.exception.reason, "hash_mismatch")

    def test_head_behind_commit_is_loss(self):
        pages = _pages(3)
        source = self._committed(pages)
        source.transport = FakeTransport(
            _response(_body([], next_cursor=3, head=_head(pages[:2])))
        )
        with self.assertRaises(remote.LossAfterCommit) as caught:
            source.replay_after_commit(CHAIN_ID)
        self.assertEqual(caught.exception.reason, "head_behind")

    def test_full_replay_after_commit_is_lossless(self):
        pages = _pages(3)
        source = self._committed(pages)
        source.transport = FakeTransport(
            _response(_body(pages, next_cursor=3))
        )
        report = source.replay_after_commit(CHAIN_ID)
        self.assertEqual(report["origin_bytes"], pages)
        self.assertIsNone(report["loss"])
        self.assertEqual(report["committed_cursor"], 3)
        self.assertEqual(report["committed_next_seq"], 4)

    def test_loss_can_be_surfaced_as_report(self):
        pages = _pages(3)
        source = self._committed(pages)
        source.transport = FakeTransport(
            _response(_body(pages[:2], next_cursor=2))
        )
        report = source.fetch(CHAIN_ID, cursor=0, allow_loss=True)
        self.assertEqual(report["status"], "loss")
        self.assertEqual(report["loss"]["committed_cursor"], 3)
        self.assertEqual(report["loss"]["peer_cursor"], 2)
        self.assertFalse(report["complete"])

    def test_commit_state_persists_across_instances(self):
        pages = _pages(2)
        state_path = os.path.join(self._tmp.name, "remote-state.json")
        self._committed(pages, state_path=state_path)
        self.assertTrue(os.path.exists(state_path))
        reopened = self.source(
            FakeTransport(_response(_body(pages[:1], next_cursor=1))),
            state_path=state_path,
        )
        with self.assertRaises(remote.LossAfterCommit):
            reopened.replay_after_commit(CHAIN_ID)

    def test_fresh_incremental_fetch_after_commit_is_not_loss(self):
        pages = _pages(4)
        source = self._committed(pages[:3])
        source.transport = FakeTransport(
            _response(_body([pages[3]], after=3, next_cursor=4))
        )
        report = source.fetch(CHAIN_ID, cursor=3)
        self.assertEqual(report["count"], 1)
        self.assertIsNone(report["loss"])
        self.assertEqual(report["next_cursor"], 4)
        self.assertEqual(report["next_seq"], 5)


class ExportErrorTests(RemoteSourceCase):
    def test_reset_required_is_typed(self):
        body = {"ok": False, "error": {"code": "reset_required", "retryable": False}}
        with self.assertRaises(remote.ResetRequired):
            self.source(FakeTransport(_response(body, status=409))).fetch(CHAIN_ID)

    def test_source_gap_error_is_typed_and_not_filled(self):
        body = {"ok": False, "error": {"code": "source_gap", "retryable": True}}
        with self.assertRaises(remote.SourceGapRefused) as caught:
            self.source(FakeTransport(_response(body, status=409))).fetch(CHAIN_ID)
        self.assertEqual(caught.exception.code, "source_gap")
    def test_reset_required_after_commit_is_loss(self):
        pages = _pages(2)
        source = self.source(
            FakeTransport(_response(_body(pages, next_cursor=2)))
        )
        source.commit(source.fetch(CHAIN_ID))
        reset = {"ok": False, "error": {"code": "reset_required", "retryable": False}}
        source.transport = FakeTransport(_response(reset, status=409))
        with self.assertRaises(remote.LossAfterCommit) as caught:
            source.replay_after_commit(CHAIN_ID)
        self.assertEqual(caught.exception.reason, "reset_required")


class EventDraftTests(RemoteSourceCase):
    def _validate(self, draft):
        self.assertEqual(frozenset(draft), frozenset(remote._EVENT_DRAFT_KEYS))
        rails_contracts.validate_event(
            draft["kind"],
            draft["source"],
            draft["scope"],
            draft["payload"],
            draft["authority"],
            draft["lane"],
            draft["occurred_at"],
            draft["duration_ms"],
            draft["timing_quality"],
            draft["redaction_policy"],
        )

    def test_checkpoint_draft_matches_rails_event_schema(self):
        pages = _pages(3)
        report = self.source(
            FakeTransport(_response(_body(pages, next_cursor=3)))
        ).fetch(CHAIN_ID)
        drafts = self.source(FakeTransport()).emit_events(report)
        self.assertEqual([draft["kind"] for draft in drafts], ["source.checkpoint"])
        draft = drafts[0]
        self._validate(draft)
        self.assertEqual(draft["source"]["adapter"], "rails.remote")
        self.assertTrue(draft["dedupe_key"])
        self.assertEqual(draft["source"]["instance"], PEER_ID)
        self.assertEqual(draft["authority"], "imported_observation")
        self.assertEqual(draft["payload"]["range"], [1, 3])
        self.assertEqual(draft["payload"]["cursor"], 3)
        self.assertEqual(draft["payload"]["count"], 3)
        self.assertEqual(
            draft["payload"]["digest"],
            hashlib.sha256("".join(pages).encode("ascii")).hexdigest(),
        )

    def test_gap_stale_and_loss_drafts_are_typed_source_events(self):
        pages = _pages(3)
        gap_report = self.source(
            FakeTransport(_response(_body(pages[1:], next_cursor=3)))
        ).fetch(CHAIN_ID)
        gap_drafts = self.source(FakeTransport()).emit_events(gap_report)
        self.assertEqual([draft["kind"] for draft in gap_drafts], ["source.gap"])
        self.assertEqual(gap_drafts[0]["payload"]["code"], "export_gap")
        self._validate(gap_drafts[0])

        old = _stamp(BASE_DT - timedelta(minutes=10))
        stale_report = self.source(
            FakeTransport(
                _response(
                    _body(pages, next_cursor=3),
                    headers={"X-Rails-Last-Seen": old},
                )
            )
        ).fetch(CHAIN_ID)
        stale_drafts = self.source(FakeTransport()).emit_events(stale_report)
        self.assertIn("source.error", [draft["kind"] for draft in stale_drafts])
        self.assertIn(
            "stale_host", [draft["payload"]["code"] for draft in stale_drafts]
        )
        for draft in stale_drafts:
            self._validate(draft)

        source = self.source(
            FakeTransport(_response(_body(pages, next_cursor=3)))
        )
        source.commit(source.fetch(CHAIN_ID))
        source.transport = FakeTransport(
            _response(_body(pages[:2], next_cursor=2))
        )
        loss_report = source.fetch(CHAIN_ID, cursor=0, allow_loss=True)
        loss_drafts = self.source(FakeTransport()).emit_events(loss_report)
        self.assertIn("source.error", [draft["kind"] for draft in loss_drafts])
        self.assertIn(
            "loss_after_commit", [draft["payload"]["code"] for draft in loss_drafts]
        )
        for draft in loss_drafts:
            self._validate(draft)


class LedgerIntegrationTests(RemoteSourceCase):
    def test_reopened_reader_resumes_from_sqlite_and_rejects_lost_head(self):
        pages = _pages(3)
        db_path = os.path.join(self._tmp.name, 'rails.sqlite3')
        led = rails_ledger.RailsLedger(db_path)
        try:
            peer = self.source(FakeTransport(_response(_body(pages[:2], head=_head(pages), has_more=True))))
            peer.import_into(led, CHAIN_ID, limit=2)
        finally:
            led.close()
        led = rails_ledger.RailsLedger(db_path)
        self.addCleanup(led.close)
        peer = self.source(FakeTransport(_response(_body(pages[2:], after=2))))
        self.assertEqual(peer.import_into(led, CHAIN_ID)['ledger']['imported'], 1)
        peer = self.source(FakeTransport(_response(_body([], after=3, head=_head(pages[:1])))))
        with self.assertRaises(remote.LossAfterCommit):
            peer.import_into(led, CHAIN_ID)
        self.assertEqual(led.import_cursor(CHAIN_ID)['next_seq'], 4)

    def test_worker_identity_does_not_grant_owner_authority(self):
        record = _event(_pages(1)[0]); del record['hash']
        record['authority'] = 'owner_record'
        record['hash'] = rails_contracts.rails_hash(record['prev_hash'], record)
        peer = self.source(FakeTransport(_response(_body([rails_contracts.canonicalize(record).decode()]))))
        led = rails_ledger.RailsLedger(os.path.join(self._tmp.name, 'rails.sqlite3'))
        self.addCleanup(led.close)
        with self.assertRaises(remote.EnrollmentRefused):
            peer.import_into(led, CHAIN_ID)
        self.assertEqual(led.import_cursor(CHAIN_ID)['next_seq'], 1)

    def test_import_preserves_bytes_in_actual_ledger(self):
        pages = _pages(3)
        db_path = os.path.join(self._tmp.name, "rails.sqlite3")
        ledger = rails_ledger.RailsLedger(db_path)
        self.addCleanup(ledger.close)
        source = self.source(
            FakeTransport(_response(_body(pages, next_cursor=3)))
        )
        report = source.import_into(ledger, CHAIN_ID)
        self.assertEqual(report["ledger"]["imported"], 3)
        rows = ledger.query(
            "SELECT seq, origin_hash, origin_bytes FROM imported_origin_rows"
            " WHERE chain_id = ? ORDER BY seq",
            (CHAIN_ID,),
        )
        self.assertEqual([row["origin_bytes"] for row in rows], pages)
        self.assertEqual(
            [row["origin_hash"] for row in rows],
            [_event(text)["hash"] for text in pages],
        )
        self.assertEqual(source.status(CHAIN_ID)["cursor"], 3)
        self.assertEqual(source.status(CHAIN_ID)["next_seq"], 4)
        self.assertTrue(ledger.verify_chain()["ok"])

    def test_read_only_or_foreign_ledger_interface_is_not_ducked(self):
        source = self.source(
            FakeTransport(_response(_body(_pages(1), next_cursor=1)))
        )
        with self.assertRaises(remote.RemoteSourceRefusal):
            source.import_into(object(), CHAIN_ID)


if __name__ == "__main__":
    unittest.main()
