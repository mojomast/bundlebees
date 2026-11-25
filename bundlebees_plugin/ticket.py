import base64
import hashlib
import re
import threading
import time
import uuid


TICKET_TTL_SECONDS = 600

_JTI_RE = re.compile(r"[0-9a-f]{32}")
_CRYPTO_UNAVAILABLE = "cryptography is not installed; install the tickets extra"


class TicketError(Exception):
    pass


class TicketReuseError(TicketError):
    pass


class TicketCryptoUnavailable(TicketError):
    pass


def _jcs_string(value):
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\f":
            out.append("\\f")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out).encode("utf-8")


def jcs(value):
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return b"[" + b",".join(jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JCS object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return b"{" + b",".join(
            _jcs_string(key) + b":" + jcs(value[key]) for key in keys
        ) + b"}"
    raise TypeError(f"JCS cannot encode {type(value).__name__}")


def _sha256_jcs(value):
    return hashlib.sha256(jcs(value)).hexdigest()


def brief_digest(brief):
    unsigned_brief = {key: value for key, value in brief.items() if key != "ticket"}
    return _sha256_jcs(unsigned_brief)


class JtiLedger:
    def __init__(self):
        self._claims = {}
        self._lock = threading.Lock()

    def claim(self, jti, exp):
        if not isinstance(jti, str) or _JTI_RE.fullmatch(jti) is None:
            raise TicketError("jti must be 32 lower-case hexadecimal characters")
        with self._lock:
            if jti in self._claims:
                raise TicketReuseError("jti has already been claimed")
            self._claims[jti] = exp


def _base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue_ticket(
    brief,
    *,
    private_key,
    kid,
    audience,
    now=None,
    jti=None,
    ledger=None,
    ttl_seconds=TICKET_TTL_SECONDS,
):
    if not isinstance(kid, str) or not kid:
        raise TicketError("kid must be a non-empty string")
    if not isinstance(audience, str) or not audience:
        raise TicketError("audience must be a non-empty string")
    if not isinstance(brief, dict):
        raise TicketError("brief must be an object")
    if "source_rev" not in brief or "assigned_files" not in brief:
        raise TicketError("brief must contain source_rev and assigned_files")
    try:
        risk_tier = brief["risk"]["tier"]
    except (KeyError, TypeError):
        raise TicketError("brief risk tier must be low, medium, or high") from None
    if not isinstance(risk_tier, str) or risk_tier not in {"low", "medium", "high"}:
        raise TicketError("brief risk tier must be low, medium, or high")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= TICKET_TTL_SECONDS
    ):
        raise TicketError("ttl_seconds must be between 1 and 600")
    iat = int(time.time()) if now is None else now
    if isinstance(iat, bool) or not isinstance(iat, int):
        raise TicketError("now must be an integer Unix timestamp")
    if jti is None:
        jti = uuid.uuid4().hex
    if not isinstance(jti, str) or _JTI_RE.fullmatch(jti) is None:
        raise TicketError("jti must be 32 lower-case hexadecimal characters")
    try:
        key_bytes = memoryview(private_key).tobytes()
    except TypeError:
        raise TicketError("private_key must be a 32-byte Ed25519 seed") from None
    if len(key_bytes) != 32:
        raise TicketError("private_key must be a 32-byte Ed25519 seed")

    contract = brief.get("contract")
    claims = {
        "brief_sha256": brief_digest(brief),
        "project": brief["project"],
        "source_rev": brief["source_rev"],
        "assigned_files_sha256": _sha256_jcs(brief["assigned_files"]),
        "risk_tier": risk_tier,
        "contract_sha256": _sha256_jcs(contract) if isinstance(contract, dict) else None,
        "assignment": None,
        "aud": audience,
        "iat": iat,
        "exp": iat + ttl_seconds,
        "jti": jti,
        "kid": kid,
    }
    header_segment = _base64url(jcs({"alg": "EdDSA", "kid": kid}))
    payload_segment = _base64url(jcs(claims))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise TicketCryptoUnavailable(_CRYPTO_UNAVAILABLE) from exc

    if ledger is not None:
        ledger.claim(jti, claims["exp"])
    signature = Ed25519PrivateKey.from_private_bytes(key_bytes).sign(signing_input)
    return f"{header_segment}.{payload_segment}.{_base64url(signature)}"
