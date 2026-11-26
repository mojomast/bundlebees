"""Provenance for verify records: which repository, slice, PR and contract a record proves.

A verify record is the merge lane's only proof that a change was tested, so it
has to say *what* it proves.  The lane binds a record to the frozen base lease
that classified the pull request: the record's ``repo``, ``slice`` and
``contract_sha256`` must equal that lease's, or the record is refused even at a
head whose bytes match (``verify_provenance_mismatch``).  A record written
before this module existed carries no provenance and is therefore not evidence
for any pull request.

The writer here is deliberately fail-closed: every field is validated, and a
record that cannot name its repository, slice and contract is not written at
all.  A half-attributed record is worse than a missing one, because it reads as
evidence while belonging to nothing.  The pull request is stamped whenever one
is known -- an exact-head record without it is still valid evidence, because
the lane's own predicate binds on the other three and treats ``pr`` as an
additional claim when present -- so paths that verify a branch before a PR
exists (``tdd``/``refactor``) keep working.

Sources (all dispatch-time truth, never worker claims):

* ``repo`` -- the ``owner/name`` slug from the lease brief's ``repo``, else the
  worktree's own ``origin`` remote when that parses as a GitHub slug.
* ``slice`` -- the embedded frozen contract's ``id`` (the same name the base
  lease carries as ``slice``).
* ``contract_sha256`` -- the contract's canonical hash, recomputed and required
  to equal the ``review.contract_sha256`` the freeze recorded.
* ``pr`` -- the pull request the verified head belongs to: an explicit
  ``--pull-request``, the attempt's delivery record, the lease brief, or the
  PR this verify run opened.  Stamped only when one is known.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: The fields the merge lane binds a record to the frozen base lease with.
#: ``pr`` is the fourth provenance field, stamped when a pull request is known.
BINDING_FIELDS = ("repo", "slice", "contract_sha256")
FIELDS = ("repo", "slice", "pr", "contract_sha256")

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_SLICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PULL_URL_RE = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")

_FIX_PRODUCER = (
    "Dispatch the brief with a frozen acdd/1 contract and a repo slug, and verify "
    "the branch that has a pull request."
)


class ProvenanceError(Exception):
    """A verify record cannot be attributed, with a stable machine code.

    ``code`` is one of ``verify_provenance_missing`` (a required field is
    absent) or ``verify_provenance_invalid`` (the lease names something
    unusable).
    """

    def __init__(self, code: str, error: str, fix: str = _FIX_PRODUCER) -> None:
        self.code = str(code)
        self.error = str(error)
        self.fix = str(fix)
        super().__init__(self.error)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.error, "fix": self.fix}


def _missing(field: str, detail: str = "") -> ProvenanceError:
    suffix = (": " + detail) if detail else ""
    return ProvenanceError(
        "verify_provenance_missing",
        f"verify provenance is missing {field}{suffix}",
        _FIX_PRODUCER,
    )


def repo_slug(value: Any) -> str:
    """An ``owner/name`` repository slug, lower-cased, or a refusal."""
    text = value.strip() if isinstance(value, str) else ""
    if text.endswith(".git"):
        text = text[:-4]
    if not text or not _REPO_RE.fullmatch(text) or ".." in text:
        raise _missing("repo", f"{value!r} is not an owner/name slug")
    return text.lower()


def slice_name(value: Any) -> str:
    """The slice id, an opaque identity token, or a refusal."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or not _SLICE_RE.fullmatch(text) or ".." in text or "/" in text:
        raise _missing("slice", f"{value!r} is not a slice id")
    return text


def pull_number(value: Any) -> int:
    """A positive pull-request number; booleans and non-digits are refused."""
    if isinstance(value, bool) or value is None:
        raise _missing("pr", f"{value!r} is not a pull-request number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise _missing("pr", f"{value!r} is not a pull-request number")
    if number <= 0:
        raise _missing("pr", f"{value!r} is not a positive pull-request number")
    return number


def contract_digest(value: Any) -> str:
    """A lower-case 64-hex contract digest, or a refusal."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text or not _SHA256_RE.fullmatch(text):
        raise _missing("contract_sha256", f"{value!r} is not a 64-hex digest")
    return text


def pr_from_url(url: Any) -> int | None:
    """The pull-request number in a ``.../pull/<n>`` URL, or None."""
    text = url.strip() if isinstance(url, str) else ""
    match = _PULL_URL_RE.match(text)
    if not match:
        return None
    try:
        return int(match.group(3))
    except ValueError:
        return None


def pr_from_delivery(document: Any) -> int | None:
    """The PR number an attempt's delivery record names, or None."""
    if not isinstance(document, Mapping):
        return None
    return pr_from_url(document.get("pr_url"))


def _brief_of(lease: Mapping[str, Any]) -> Mapping[str, Any]:
    brief = lease.get("brief")
    return brief if isinstance(brief, Mapping) else {}


def _contract_of(lease: Mapping[str, Any]) -> Mapping[str, Any]:
    brief = _brief_of(lease)
    contract = brief.get("contract")
    if not isinstance(contract, Mapping):
        contract = lease.get("contract")
    if not isinstance(contract, Mapping):
        raise _missing("slice", "the lease carries no frozen acdd/1 contract")
    return contract


def from_lease(lease: Any, *, repo: Any, pr: Any = None) -> dict[str, Any]:
    """Provenance from the dispatch-time lease and its embedded frozen contract.

    ``repo`` overrides the brief's own slug; passing ``None`` falls back to it.
    ``pr`` is attached only when given: a first verify run has no PR yet, and
    the caller completes the record with :func:`with_pr` once it does.
    """
    if not isinstance(lease, Mapping):
        raise _missing("repo", "the lease is not a record")
    brief = _brief_of(lease)
    contract = _contract_of(lease)

    from . import contract as acdd  # noqa: PLC0415 - keep the module import surface small

    try:
        digest = acdd.check_frozen(contract)
    except acdd.ContractError as exc:
        raise ProvenanceError(
            "verify_provenance_invalid",
            f"the lease contract is not a frozen, unchanged acdd/1 contract: {exc.error}",
            "Re-dispatch the brief from the frozen contract; never edit a frozen contract.",
        ) from exc

    slice_id = slice_name(contract.get("id"))
    for key in ("slice", "slice_name", "contract_id"):
        named = brief.get(key)
        if isinstance(named, str) and named.strip() and named.strip() != slice_id:
            raise ProvenanceError(
                "verify_provenance_invalid",
                f"the brief names slice {named!r} but its contract id is {slice_id!r}",
                "Fix the brief so its slice and contract id agree, then re-dispatch.",
            )
    provenance: dict[str, Any] = {
        "repo": repo_slug(repo if repo else (brief.get("repo") or lease.get("repo"))),
        "slice": slice_id,
        "contract_sha256": contract_digest(digest),
    }
    if pr is not None:
        provenance["pr"] = pull_number(pr)
    return provenance


def with_pr(provenance: Any, pr: Any) -> dict[str, Any]:
    """Return *provenance* with its pull-request number, when one is known.

    ``pr`` is additional evidence, not a binding field: a first verify run
    opens the PR after the commit exists, and a ``tdd``/``refactor`` verify may
    never have one.  An absent number leaves the binding unchanged rather than
    refusing the record; a present but invalid one is refused.
    """
    if not isinstance(provenance, Mapping):
        raise _missing("repo", "no lease provenance was resolved")
    completed = dict(provenance)
    if pr is None:
        completed.pop("pr", None)
        return {field: completed[field] for field in BINDING_FIELDS}
    completed["pr"] = pull_number(pr)
    return {field: completed[field] for field in FIELDS}


def stamp(payload: Mapping[str, Any], provenance: Any) -> dict[str, Any]:
    """Return *payload* with its validated provenance, or a named refusal.

    ``repo``, ``slice`` and ``contract_sha256`` are required: without them the
    record is not evidence for any pull request.  ``pr`` is stamped when the
    provenance carries one, and a non-null value that is not a positive
    integer is refused rather than dropped.
    """
    if not isinstance(payload, Mapping):
        raise ProvenanceError("verify_provenance_invalid", "the verify payload is not an object")
    if not isinstance(provenance, Mapping):
        raise _missing("repo", "no provenance was supplied for this verify record")
    stamped = dict(payload)
    stamped["repo"] = repo_slug(provenance.get("repo"))
    stamped["slice"] = slice_name(provenance.get("slice"))
    stamped["contract_sha256"] = contract_digest(provenance.get("contract_sha256"))
    if provenance.get("pr") is not None:
        stamped["pr"] = pull_number(provenance.get("pr"))
    else:
        stamped.pop("pr", None)
    return stamped
