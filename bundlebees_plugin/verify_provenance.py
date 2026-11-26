"""Provenance for verify records: which repository, slice, PR and contract a record proves.

A verify record is the merge lane's only proof that a change was tested, so it
has to say *what* it proves.  The lane binds a record to the frozen base lease
that classified the pull request: the record's ``repo``, ``slice`` and
``contract_sha256`` must equal that lease's, or the record is refused even at a
head whose bytes match (``verify_provenance_mismatch``).  A record written
before this module existed carries no provenance and is therefore not evidence
for any pull request.

The writer here is deliberately fail-closed: every field is validated, and a
record that cannot name its repository, slice, pull request and contract is not
written at all.  A half-attributed record is worse than a missing one, because
it reads as evidence while belonging to nothing.

Sources (all dispatch-time truth, never worker claims):

* ``repo`` -- the ``owner/name`` slug from the lease brief's ``repo``, else the
  worktree's own ``origin`` remote when that parses as a GitHub slug.
* ``slice`` -- the embedded frozen contract's ``id`` (the same name the base
  lease carries as ``slice``).
* ``contract_sha256`` -- the contract's canonical hash, recomputed and required
  to equal the ``review.contract_sha256`` the freeze recorded.
* ``pr`` -- the pull request the verified head belongs to: an explicit
  ``--pull-request``, the attempt's delivery record, the lease brief, or the
  PR this verify run opened.
"""

from __future__ import annotations

from typing import Any, Mapping

FIELDS = ("repo", "slice", "pr", "contract_sha256")
BINDING_FIELDS = ("repo", "slice", "contract_sha256")


class ProvenanceError(Exception):
    """A verify record cannot be attributed, with a stable machine code."""

    def __init__(self, code: str, error: str, fix: str = "") -> None:
        self.code = str(code)
        self.error = str(error)
        self.fix = str(fix)
        super().__init__(self.error)


def repo_slug(value: Any) -> str:
    raise NotImplementedError("verify-writer RED: repo_slug")


def slice_name(value: Any) -> str:
    raise NotImplementedError("verify-writer RED: slice_name")


def pull_number(value: Any) -> int:
    raise NotImplementedError("verify-writer RED: pull_number")


def contract_digest(value: Any) -> str:
    raise NotImplementedError("verify-writer RED: contract_digest")


def pr_from_url(url: Any) -> int | None:
    raise NotImplementedError("verify-writer RED: pr_from_url")


def pr_from_delivery(document: Any) -> int | None:
    raise NotImplementedError("verify-writer RED: pr_from_delivery")


def from_lease(lease: Mapping[str, Any], *, repo: Any, pr: Any = None) -> dict[str, Any]:
    raise NotImplementedError("verify-writer RED: from_lease")


def with_pr(provenance: Mapping[str, Any], pr: Any) -> dict[str, Any]:
    raise NotImplementedError("verify-writer RED: with_pr")


def stamp(payload: Mapping[str, Any], provenance: Mapping[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("verify-writer RED: stamp")
