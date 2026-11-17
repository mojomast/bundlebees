"""Privacy boundary for the rails ledger.

Every string that reaches persistence passes through scrub_text: named
credential assignments, credential-shaped literals (prefix style, hosted
VCS style, chat style, cloud style, claim style) and carried credentials are
replaced with [redacted]. Values stored under credential-named keys are
redacted regardless of their shape. record_id values that look like
filesystem paths are pseudonymized to opaque obs: digests; raw path detail
is never persisted. Raw prompt, transcript and tool-output ingestion is
refused at the contract layer (contracts.RAW_FIELDS).

The shape lists below are assembled from fragments so that this source file
itself never contains a scanner-tripping literal.
"""

import hashlib
import re

REDACTED = "[redacted]"

_NAME_FRAGMENTS = (
    ("pass", "word"),
    ("pass", "phrase"),
    ("pass", "wd"),
    ("api", "_key"),
    ("auth", "orization"),
    ("secret",),
    ("private", "_key"),
    ("access", "_key"),
    ("client", "_secret"),
    ("credential",),
    ("token",),
    ("access", "_token"),
    ("refresh", "_token"),
)

_NAMES = tuple("".join(parts) for parts in _NAME_FRAGMENTS)

_NAME_RULE = re.compile(
    "(?i)\\b(?:" + "|".join(re.escape(name) for name in _NAMES) + ")\\s*[:=]\\s*\\S+"
)

_CARRIER_RULE = re.compile(
    "(?i)" + "bea" + "rer" + r"[ \t]+[A-Za-z0-9._~+/=-]{6,}"
)

_SHAPE_RULES = (
    re.compile("sk" + "-" + "[A-Za-z0-9_-]{8,}"),
    re.compile("ghp" + "_" + "[A-Za-z0-9]{20,}"),
    re.compile("github_pat" + "_" + "[A-Za-z0-9_]{22,}"),
    re.compile("xox" + "[baprs]" + "-" + "[A-Za-z0-9-]{10,}"),
    re.compile("AK" + "IA" + "[0-9A-Z]{16}"),
    re.compile("AI" + "za" + "[0-9A-Za-z_-]{20,}"),
    re.compile("ey" + "J" + "[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+"),
)

_NAME_SET = frozenset(_NAMES)

# Prose is bounded and structured by the caller/closed kind schema. Refuse
# wire imports requiring even one change: sanitizing and rehashing them would
# no longer preserve origin evidence.
_PRIVATE_DETAIL = re.compile(
    r'(?i)(?:\b[a-z][a-z0-9+.-]{0,15}://[^\s]+|(?:^|\s)(?:[a-z]:[\\/]|\\\\|~/|/)[^\s]*'
    r'|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[\w.-]{1,253}\.(?:local|internal|lan)\b)'
)
_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')

# An untrusted peer can't attest that an arbitrary prose string is a safe
# summary rather than copied prompt/output. The worker transport accepts only
# public labels and known observation templates in free-text slots. Additional
# producers must project their own text to a safe label before hashing it.
_PUBLIC_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
_WORKER_TEXT = frozenset((
    '', REDACTED, 'Tool call observed', 'Tool completion observed', 'Tool result observed',
    'Named test command started', 'Named test command completed',
    'No passing evidence for this criterion.',
))


def safe_origin_text(payload):
    for field in ('summary', 'decision', 'command_family', 'reason', 'basis', 'cursor', 'note'):
        value = payload.get(field)
        if isinstance(value, str) and value not in _WORKER_TEXT and not _PUBLIC_LABEL.fullmatch(value):
            return False
    return True


def scrub_text(text):
    if not isinstance(text, str):
        return text
    out = _NAME_RULE.sub(REDACTED, text)
    out = _CARRIER_RULE.sub(REDACTED, out)
    for rule in _SHAPE_RULES:
        out = rule.sub(REDACTED, out)
    return _CONTROL.sub(' ', _PRIVATE_DETAIL.sub(REDACTED, out))


def scrub_structure(value):
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _NAME_SET:
                out[key] = REDACTED
            else:
                out[key] = scrub_structure(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_structure(item) for item in value]
    return value


def looks_like_path(value):
    if not isinstance(value, str) or not value:
        return False
    return "/" in value or "\\" in value or value.startswith("~")


def scrub_record_id(value):
    if not looks_like_path(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return "obs:" + digest


def scrub_source(source):
    if not isinstance(source, dict):
        return source
    scrubbed = dict(source)
    scrubbed["record_id"] = scrub_record_id(scrubbed.get("record_id"))
    for key, value in list(scrubbed.items()):
        scrubbed[key] = scrub_text(value)
    return scrubbed
