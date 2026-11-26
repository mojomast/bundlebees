"""RED for wave C slice C3: the coordinator issues the Ed25519 dispatch ticket.

hyvv ADR 0002 makes ``preflight`` return, on success, a compact JWS (``alg:
EdDSA``, Ed25519) over the twelve ``rizzstack.ticket/1`` claims. C1 froze the
claim list; C3 is the bundlebees half that *mints* the ticket. It is the only
component that ever touches the coordinator's private key, and the key is
injected from the coordinator's secret store - this module never discovers,
reads or derives a key from the host.

The frozen surface these tests pin down:

* ``issue_ticket(brief, *, private_key, kid, audience, now=None, jti=None,
  ledger=None, ttl_seconds=600)`` returns the compact JWS
  ``base64url(header).base64url(payload).base64url(signature)``.
* The header is exactly ``{"alg": "EdDSA", "kid": <kid>}`` and the payload is
  the RFC 8785 (JCS) canonical JSON of the twelve claims, so the signature is
  deterministic for a fixed key, ``jti`` and ``iat``.
* ``brief_sha256`` is SHA-256 over the JCS form of the brief **without** its
  ``ticket`` key, ``assigned_files_sha256`` is SHA-256 over the JCS form of
  ``assigned_files`` and ``contract_sha256`` hashes the embedded ``contract``
  object (``null`` when the brief carries none).
* ``exp`` is exactly ``iat + 600``; a caller cannot mint a longer-lived
  ticket. ``jti`` is 32 lower-case hex characters, random and single-use: a
  ledger that has already seen it refuses a second issue.
* ``cryptography`` is an optional extra. Importing the module without it
  succeeds; issuing fails closed with the named ``TicketCryptoUnavailable``,
  never a bare ``ImportError``.
* No production key path is read: the private key is a required caller
  argument and the module never consults the environment, the home directory
  or a key file.

RED now: ``bundlebees_plugin/ticket.py`` does not exist. The Ed25519 and JCS
helpers below are test-local so the assertion does not trust the implementation
to check itself. Keys are generated at test time and never written to disk.
"""

from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MODULE_PATH = HERE / "bundlebees_plugin" / "ticket.py"

# --------------------------------------------------------------------------- Ed25519
# RFC 8032 reference arithmetic, test-local: the test must verify the ticket
# with an implementation that is independent of whatever the issuer uses.

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * _inv(5)) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P)


def _ed_add(p, q):
    x1, y1 = p
    x2, y2 = q
    k = (_D * x1 * x2 * y1 * y2) % _P
    x3 = ((x1 * y2 + x2 * y1) * _inv(1 + k)) % _P
    y3 = ((y1 * y2 + x1 * x2) * _inv(1 - k)) % _P
    return (x3 % _P, y3 % _P)


def _ed_mul(p, e):
    q = (0, 1)
    while e > 0:
        if e & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        e >>= 1
    return q


def _encode_point(p):
    x, y = p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (s[31] >> 7):
        x = _P - x
    return (x, y)


def _public_key(seed):
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    return _encode_point(_ed_mul(_B, a))


def _ed25519_verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    r = _decode_point(signature[:32])
    a = _decode_point(public)
    s = int.from_bytes(signature[32:], "little")
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _ed_mul(_B, s) == _ed_add(r, _ed_mul(a, h))


# --------------------------------------------------------------------------- JCS
# RFC 8785 for the value shapes a ticket brief uses: objects, arrays, strings,
# integers, booleans and null. Floats are refused: the claims set has none.

def _jcs_string(value):
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jcs(value):
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _jcs_string(value).encode("utf-8")
    if isinstance(value, (list, tuple)):
        return b"[" + b",".join(_jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JCS object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return b"{" + b",".join(_jcs_string(key).encode("utf-8") + b":" + _jcs(value[key]) for key in keys) + b"}"
    raise TypeError(f"JCS cannot encode {type(value).__name__}")


def _b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# --------------------------------------------------------------------------- fixtures

KID = "coordinator-test-2026-11"
AUDIENCE = "hyvv-test-host"
NOW = 1764000000

BRIEF = {
    "objective": "issue one dispatch ticket",
    "project": "demo",
    "branch": "feat/wave-c-c3",
    "source_rev": "a" * 40,
    "assigned_files": ["bundlebees_plugin/ticket.py", "tests/test_ticket_issue.py"],
    "acceptance": ["the ticket verifies with the test key"],
    "risk": {"tier": "medium"},
    "contract": {
        "contract_version": "acdd/1",
        "id": "wave-c-c3",
        "title": "issue the signed dispatch ticket",
    },
}

ADR_CLAIMS = {
    "brief_sha256",
    "project",
    "source_rev",
    "assigned_files_sha256",
    "risk_tier",
    "contract_sha256",
    "assignment",
    "aud",
    "iat",
    "exp",
    "jti",
    "kid",
}

_JTI_RE = re.compile(r"^[0-9a-f]{32}$")


def _load_ticket_module(name="bundlebees_ticket_c3"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reexec(module):
    """Re-execute the module in place; importlib.reload needs a sys.path spec."""
    spec = importlib.util.spec_from_file_location(module.__name__, MODULE_PATH)
    module.__spec__ = spec
    spec.loader.exec_module(module)


IMPORT_ERROR = None
try:
    T = _load_ticket_module() if MODULE_PATH.is_file() else None
except Exception as exc:  # pragma: no cover - the RED message carries the reason
    T = None
    IMPORT_ERROR = exc


def _digest(value):
    return hashlib.sha256(_jcs(value)).hexdigest()


class _ImportBlocker:
    """Refuse a named top-level package from the import system."""

    def __init__(self, blocked):
        self.blocked = blocked

    def find_spec(self, name, path=None, target=None):
        if name == self.blocked or name.startswith(self.blocked + "."):
            raise ImportError(f"{name} is blocked for this test")
        return None


class _TicketCase(unittest.TestCase):
    def setUp(self):
        if T is None:
            self.fail(
                "bundlebees_plugin/ticket.py is missing or does not import"
                + (f": {IMPORT_ERROR!r}" if IMPORT_ERROR else "")
            )
        self.seed = os.urandom(32)
        self.public = _public_key(self.seed)

    def issue(self, brief=None, **overrides):
        kwargs = {
            "private_key": self.seed,
            "kid": KID,
            "audience": AUDIENCE,
            "now": NOW,
        }
        kwargs.update(overrides)
        return T.issue_ticket(BRIEF if brief is None else brief, **kwargs)

    def parts(self, jws):
        self.assertIsInstance(jws, str)
        pieces = jws.split(".")
        self.assertEqual(len(pieces), 3, "a compact JWS has three dot-separated parts")
        return pieces

    def claims(self, jws):
        pieces = self.parts(jws)
        return json.loads(_b64url_decode(pieces[1]))


class IssuedTicketClaims(_TicketCase):
    def test_header_is_eddsa_and_payload_is_the_twelve_claims(self):
        jws = self.issue()
        header_segment, payload_segment, signature_segment = self.parts(jws)

        header = json.loads(_b64url_decode(header_segment))
        self.assertEqual(header, {"alg": "EdDSA", "kid": KID})

        claims = json.loads(_b64url_decode(payload_segment))
        self.assertEqual(set(claims), ADR_CLAIMS)
        self.assertEqual(len(claims), 12)

        signature = _b64url_decode(signature_segment)
        self.assertEqual(len(signature), 64)
        signed = f"{header_segment}.{payload_segment}".encode("ascii")
        self.assertTrue(
            _ed25519_verify(self.public, signed, signature),
            "the JWS signature must verify with the test public key over header.payload",
        )

    def test_payload_is_the_rfc8785_canonical_json_of_the_claims(self):
        jws = self.issue()
        payload_segment = self.parts(jws)[1]
        payload = _b64url_decode(payload_segment)
        claims = json.loads(payload)
        self.assertEqual(payload, _jcs(claims), "the signed payload must be JCS canonical JSON")

    def test_brief_digest_is_rfc8785_over_the_brief_without_its_ticket(self):
        claims = self.claims(self.issue())
        self.assertEqual(claims["brief_sha256"], _digest(BRIEF))

        # Insertion order cannot leak into the digest: reordered keys agree.
        reordered = dict(reversed(list(BRIEF.items())))
        self.assertEqual(self.claims(self.issue(brief=reordered))["brief_sha256"], _digest(BRIEF))

        # A stale ticket on the input is not part of the brief being signed.
        stale = dict(BRIEF, ticket="header.payload.signature")
        self.assertEqual(self.claims(self.issue(brief=stale))["brief_sha256"], _digest(BRIEF))

        # A different brief is a different digest.
        other = dict(BRIEF, objective="issue a different ticket")
        self.assertNotEqual(self.claims(self.issue(brief=other))["brief_sha256"], _digest(BRIEF))

    def test_remaining_claims_come_from_the_brief(self):
        claims = self.claims(self.issue())
        self.assertEqual(claims["project"], BRIEF["project"])
        self.assertEqual(claims["source_rev"], BRIEF["source_rev"])
        self.assertEqual(claims["risk_tier"], BRIEF["risk"]["tier"])
        self.assertEqual(claims["assigned_files_sha256"], _digest(BRIEF["assigned_files"]))
        self.assertEqual(claims["contract_sha256"], _digest(BRIEF["contract"]))
        self.assertIsNone(claims["assignment"], "assignment is null-only in v1")
        self.assertEqual(claims["aud"], AUDIENCE)
        self.assertEqual(claims["kid"], KID)

    def test_a_brief_without_a_contract_gets_a_null_contract_digest(self):
        brief = {key: value for key, value in BRIEF.items() if key != "contract"}
        self.assertIsNone(self.claims(self.issue(brief=brief))["contract_sha256"])

    def test_exp_is_exactly_ten_minutes_and_ttl_cannot_be_extended(self):
        claims = self.claims(self.issue())
        self.assertEqual(T.TICKET_TTL_SECONDS, 600)
        self.assertEqual(claims["iat"], NOW)
        self.assertEqual(claims["exp"], NOW + 600)
        self.assertEqual(claims["exp"] - claims["iat"], 600)

        with self.assertRaises(T.TicketError):
            self.issue(ttl_seconds=601)
        with self.assertRaises(T.TicketError):
            self.issue(ttl_seconds=86400)

    def test_issue_is_deterministic_for_a_fixed_key_jti_and_iat(self):
        fixed_jti = "0" * 32
        first = self.issue(jti=fixed_jti)
        second = self.issue(jti=fixed_jti)
        self.assertEqual(first, second, "Ed25519 is deterministic; only jti/iat may vary")


class SingleUseJti(_TicketCase):
    def test_default_jti_is_random_32_hex_and_unique_per_issue(self):
        issued = {self.claims(self.issue())["jti"] for _ in range(5)}
        self.assertEqual(len(issued), 5, "every issue must mint a fresh jti")
        for jti in issued:
            self.assertRegex(jti, _JTI_RE)

    def test_a_replayed_jti_is_refused_by_the_ledger(self):
        ledger = T.JtiLedger()
        jti = "f" * 32
        self.issue(jti=jti, ledger=ledger)
        with self.assertRaises(T.TicketReuseError):
            self.issue(jti=jti, ledger=ledger)

    def test_ledger_refuses_the_same_jti_twice_even_without_a_ticket(self):
        ledger = T.JtiLedger()
        ledger.claim("1" * 32, NOW + 600)
        with self.assertRaises(T.TicketReuseError):
            ledger.claim("1" * 32, NOW + 600)
        # A different jti is fine.
        ledger.claim("2" * 32, NOW + 600)


class InputValidation(_TicketCase):
    def test_empty_audience_and_kid_are_refused(self):
        with self.assertRaises(T.TicketError):
            self.issue(audience="")
        with self.assertRaises(T.TicketError):
            self.issue(kid="")

    def test_a_brief_without_a_risk_tier_is_refused(self):
        brief = {key: value for key, value in BRIEF.items() if key != "risk"}
        with self.assertRaises(T.TicketError):
            self.issue(brief=brief)


class KeyHandling(_TicketCase):
    def test_private_key_is_a_required_injected_argument(self):
        parameters = inspect.signature(T.issue_ticket).parameters
        self.assertIn("private_key", parameters)
        self.assertEqual(parameters["private_key"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(
            parameters["private_key"].default,
            inspect.Parameter.empty,
            "the issuer must never fall back to a discovered key",
        )

    def test_module_never_consults_environment_or_home_for_a_key(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        forbidden_attributes = {"environ", "getenv", "expanduser", "home"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                self.fail(
                    f"ticket.py reads {node.attr!r} at line {node.lineno}; "
                    "the private key must be injected, never discovered"
                )

    def test_no_production_key_path_is_opened(self):
        secret_dir = Path(tempfile.mkdtemp(prefix="coordinator-secret-"))
        secret_file = secret_dir / "coordinator-ed25519.key"
        secret_file.write_bytes(b"production-key-sentinel-never-read")
        names = (
            "BUNDLEBEES_TICKET_PRIVATE_KEY",
            "BUNDLEBEES_TICKET_KEY_PATH",
            "RIZZSTACK_TICKET_PRIVATE_KEY",
            "COORDINATOR_ED25519_KEY",
        )
        saved = {name: os.environ.get(name) for name in names}
        opened = []
        real_builtin_open = builtins.open
        real_io_open = io.open

        def record(file, *args, **kwargs):
            opened.append(str(file))
            return real_builtin_open(file, *args, **kwargs)

        def record_io(file, *args, **kwargs):
            opened.append(str(file))
            return real_io_open(file, *args, **kwargs)

        try:
            for name in names:
                os.environ[name] = str(secret_file)
            builtins.open = record
            io.open = record_io
            self.issue()
        finally:
            builtins.open = real_builtin_open
            io.open = real_io_open
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        for path in opened:
            self.assertNotIn("coordinator-secret", path, f"ticket.py opened a key path: {path}")
            self.assertNotIn(".ssh", path, f"ticket.py opened an ssh path: {path}")
        self.assertEqual(secret_file.read_bytes(), b"production-key-sentinel-never-read")

    def test_no_key_material_is_committed_with_the_module_or_test(self):
        markers = (
            "BEGIN " + "PRIVATE KEY",
            "BEGIN " + "OPENSSH PRIVATE KEY",
            "BEGIN " + "ED25519 PRIVATE KEY",
        )
        for path in (MODULE_PATH, Path(__file__)):
            text = path.read_text(encoding="utf-8")
            for marker in markers:
                self.assertNotIn(marker, text, f"{path.name} carries key material")


class OptionalCryptography(_TicketCase):
    def test_cryptography_stays_an_optional_extra(self):
        project = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        core = " ".join(project.get("dependencies") or []).lower()
        self.assertNotIn("cryptography", core, "cryptography must not be a core dependency")
        extra = " ".join((project.get("optional-dependencies") or {}).get("tickets") or []).lower()
        self.assertIn("cryptography", extra, "the tickets extra must carry cryptography")

    def test_missing_cryptography_fails_closed_with_a_named_error(self):
        self.assertTrue(hasattr(T, "TicketCryptoUnavailable"))
        self.assertTrue(issubclass(T.TicketCryptoUnavailable, Exception))

        saved = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "cryptography" or name.startswith("cryptography.")
        }
        for name in saved:
            del sys.modules[name]
        blocker = _ImportBlocker("cryptography")
        sys.meta_path.insert(0, blocker)
        try:
            try:
                _reexec(T)
            except ImportError as exc:
                self.fail(
                    "ticket.py must import cryptography lazily so the module still loads "
                    f"without the extra: {exc}"
                )
            with self.assertRaises(T.TicketCryptoUnavailable) as caught:
                self.issue()
            self.assertIn("cryptography", str(caught.exception).lower())
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update(saved)
            _reexec(T)


if __name__ == "__main__":
    unittest.main()
