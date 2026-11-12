# HR1: verify human reviews with a signed approval token

**Contract id:** `HR1-human-review-token` (acdd/1). The approved tests come first: they were committed in their own commit, before any implementation. Owner: Kyle (approved 12 Nov 2026, 5:37 PM ET: "Build a human-review verifier using a signed approval token I issue.") Stacked on bundlebees #12 (`fix/review-c1-c6`).

## Outcome

A REVIEW counts as a *human* review only when Kyle has signed it himself. He signs locally with an Ed25519 private key that only he holds. bundlebees verifies the signature with a configured **public** key. Any other review is coordinator-recorded, or is reported as an unverified human review. bundlebees never generates, stores or asks for the private key.

## Interfaces

- `bundlebees_plugin/review_token.py`:
  - `mint`, `verify`, `consume`, `decode_unverified`, `load_private_key`, `load_public_key` and `key_id`;
  - the error `ReviewTokenError(code)`;
  - `MAX_TTL_S`;
  - a standalone CLI, `python -m bundlebees_plugin.review_token mint|pubkey`.
- `hermes bundlebees review-token mint|pubkey` (the same commands, through the plugin).
- `hermes bundlebees tdd review --role human --token-file F`.
- A new setting, `review_public_key`: a PEM file path, or `ed25519:<base64 raw key>`. It isn't a secret.
- The `tdd report --json` output gains:
  - `chain_head`;
  - `human_authorization.status`, one of `verified`, `unverified` or `not_recorded`, with `reviewer` and `reason`;
  - the review identity `human_verified`.

## Acceptance criteria

| AC | Criterion | Approved tests |
|---|---|---|
| AC-001 | A minted token binds the reviewer label, contract id and sha256, the approved test revision (`source_rev`), the evidence chain head, `iat`/`exp`, a fresh 128-bit nonce, the key id and the verdict `approve`. Mint refuses a missing or malformed binding and a TTL over `MAX_TTL_S`. | `tests/test_review_token.py::test_mint_binds_reviewer_contract_test_rev_chain_head_expiry_and_nonce`, `::test_mint_refuses_bad_or_missing_binding` |
| AC-002 | A token verifies only against the configured public key, given as PEM or inline, and only for the exact binding. | `::test_valid_token_verifies_with_pem_or_inline_public_key` |
| AC-003 | A wrong contract, hash, test rev or chain head; an expired or not-yet-valid token; a wrong key; tampered claims; a malformed token; or an over-long signed lifetime all fail with a named code. | `::test_wrong_binding_or_time_is_refused`, `::test_wrong_key_tampered_or_malformed_token_is_refused`, `::test_signed_token_with_overlong_lifetime_is_refused` |
| AC-004 | A token is single-use. Its nonce is consumed in an owner-only store, and a second use is `token_replayed`. | `::test_consumed_token_cannot_be_replayed` |
| AC-005 | A missing or unusable public key fails closed. A private key file readable by anyone other than its owner is refused. | `::test_missing_or_bad_public_key_fails_closed`, `::test_private_key_file_must_be_owner_only` |
| AC-006 | The repository tracks no private key, and mint prints only the token (and `pubkey` only the public key). | `::test_repository_tracks_no_private_key`, `::test_standalone_mint_prints_only_the_token` |
| AC-007 | A valid token records a review with role `human`, attestation `human_authorization`, `identity_verified: true`, the token, the key id, and a chain head equal to the record's `prev_sha256`. The report says `human_verified` / `verified`. | `tests/test_cli_review_token.py::test_valid_token_records_a_verified_human_review` |
| AC-008 | A wrong head, key or test rev, a reviewer mismatch, no configured key, a garbage token, or a reused token records nothing and fails with `human_review_unverified`. | `::test_unverifiable_token_fails_closed_and_records_nothing`, `::test_same_token_cannot_be_used_twice` |
| AC-009 | The report re-verifies the stored token. With a rotated or missing key, the review phase is `human_review_unverified`, the status is `unverified` with a reason, and the result is not accepted. | `::test_report_reverifies_and_fails_closed` |
| AC-010 | Without a token, `--role human` stays a coordinator-recorded review (C6), and `review-token mint` is available in the plugin CLI. | `::test_role_human_without_token_stays_coordinator_recorded`, `::test_plugin_cli_has_review_token_mint` |

## Scope

- **Assigned files:**
  - `bundlebees_plugin/review_token.py` (new);
  - `bundlebees_plugin/acdd_cli.py`, `bundlebees_plugin/tdd.py`, `bundlebees_plugin/cli.py` and `bundlebees_plugin/settings.py`;
  - `bundlebees_plugin/schemas/evidence.schema.json` and `plugin.yaml`;
  - `docs/ACDD.md`, `README.md` and `CHANGELOG.md`.
- **Test files:** `tests/test_review_token.py` and `tests/test_cli_review_token.py`.
- **Prohibited:** generating or committing any real key, and weakening the C6 default.

## Dependencies and human gates

The tests need `cryptography` (Ed25519), which is a `dev` extra and a new optional `review` extra. Without it, verification fails closed (`review_key_invalid`), and CI requires it rather than skipping. The human gates stay with Kyle: merge, tag, release, deploy and publish.
