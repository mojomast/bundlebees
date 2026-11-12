"""Approved tests for acdd_lint.contracts and acdd_lint.evidence (slice L2, written first, RED).

Protocol checks beyond the JSON shape (docs/16 §2 and §5): criterion ids,
named tests in scope, frozen hashes, and evidence history (RED before GREEN,
same fingerprints, current contract hash, valid signatures).
Problems are dicts: {"code", "where", "message"}.
"""

from __future__ import annotations

import unittest

from tests.support import KEY, canonical_sha, clone, contract, frozen, green, record, red


def codes(problems):
    return sorted({p["code"] for p in problems})


class ContractTests(unittest.TestCase):
    def setUp(self):
        from acdd_lint import contracts
        self.m = contracts

    def test_canonical_hash_matches_the_protocol(self):
        c = frozen()
        self.assertEqual(self.m.contract_sha256(c), canonical_sha(c))
        self.assertEqual(self.m.contract_sha256(c), c["review"]["contract_sha256"])

    def test_clean_frozen_contract_has_no_problems(self):
        self.assertEqual(self.m.check_contract(frozen()), [])

    def test_problem_shape(self):
        p = self.m.check_contract(contract())[0]
        self.assertEqual(set(p), {"code", "where", "message"})
        self.assertTrue(p["message"])

    def test_bad_and_duplicate_ac_ids(self):
        c = frozen()
        c["acceptance_criteria"][1]["id"] = "AC-001"
        self.assertIn("ac_id_duplicate", codes(self.m.check_contract(c)))
        c = clone(frozen())
        c["acceptance_criteria"][0]["id"] = "AC-1"
        self.assertIn("ac_id_invalid", codes(self.m.check_contract(c)))

    def test_test_criteria_need_named_tests_in_scope(self):
        c = contract()
        c["acceptance_criteria"][0]["tests"] = []
        self.assertIn("ac_untested", codes(self.m.check_contract(c)))
        c = contract()
        c["acceptance_criteria"][0]["tests"] = ["tests/test_other.py::test_adds"]
        self.assertIn("test_not_in_scope", codes(self.m.check_contract(c)))
        c = contract()
        c["acceptance_criteria"][0]["tests"] = ["tests/test_tax.py"]  # a whole approved file is fine
        self.assertNotIn("test_not_in_scope", codes(self.m.check_contract(c)))

    def test_unfrozen_and_changed_contracts(self):
        self.assertIn("contract_not_frozen", codes(self.m.check_contract(contract())))
        c = frozen()
        c["review"]["status"] = "draft"
        self.assertIn("contract_not_frozen", codes(self.m.check_contract(c)))
        c = frozen()
        c["scope"]["assigned_files"].append("shop/other.py")
        self.assertIn("contract_changed", codes(self.m.check_contract(c)))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        from acdd_lint import evidence
        self.m = evidence
        self.c = frozen()

    def hist(self, records, contract=None, key=None):
        c = self.c if contract is None else contract
        return self.m.check_history(c or None, [(f"{i:04d}-{r['phase']}.json", r) for i, r in enumerate(records, 1)], key=key)

    def test_signature_round_trip(self):
        r = green(self.c)
        self.assertTrue(self.m.verify_signature(r, KEY))
        self.assertTrue(self.m.verify_signature({**r, "_path": "x"}, KEY))
        self.assertFalse(self.m.verify_signature({**r, "classification": "behavioral_failure"}, KEY))
        self.assertFalse(self.m.verify_signature(red(self.c, sign=False), KEY))

    def test_clean_history(self):
        self.assertEqual(self.hist([red(self.c), green(self.c)]), [])
        self.assertEqual(self.hist([red(self.c), green(self.c)], key=KEY), [])

    def test_green_needs_an_earlier_valid_red(self):
        self.assertIn("red_missing", codes(self.hist([green(self.c)])))
        late_red = record(self.c, "red", "behavioral_failure", at="2026-11-12T18:03:00Z")
        self.assertIn("red_missing", codes(self.hist([green(self.c), late_red])))
        bad_red = record(self.c, "red", "syntax_error", at="2026-11-12T18:01:00Z")
        self.assertIn("red_invalid", codes(self.hist([bad_red, green(self.c)])))

    def test_tests_after_code_contract_needs_no_red(self):
        c = frozen(tdd={"applicable": True, "test_first": False})
        self.assertNotIn("red_missing", codes(self.hist([green(c)], contract=c)))

    def test_failed_green_and_changed_fingerprints(self):
        self.assertIn("green_failed", codes(self.hist([red(self.c), record(self.c, "green", "behavioral_failure", at="2026-11-12T18:02:00Z")])))
        self.assertIn("tests_tampered", codes(self.hist([red(self.c), green(self.c, fp={"tests/test_tax.py": "e" * 64})])))

    def test_worker_records_are_not_authority(self):
        self.assertIn("red_missing", codes(self.hist([red(self.c, role="worker"), green(self.c)])))

    def test_stale_orphan_and_tampered(self):
        stale = green(self.c, contract_sha256="f" * 64)
        self.assertIn("evidence_stale", codes(self.hist([red(self.c), stale])))
        self.assertIn("evidence_orphan", codes(self.hist([red(self.c)], contract=False)))
        forged = {**green(self.c), "classification": "passed", "criteria": {"AC-001": "passed", "AC-002": "passed"}}
        self.assertIn("evidence_tampered", codes(self.hist([red(self.c), forged], key=KEY)))
        unsigned = green(self.c, sign=False)
        self.assertIn("evidence_tampered", codes(self.hist([red(self.c), unsigned], key=KEY)))
        self.assertNotIn("evidence_tampered", codes(self.hist([red(self.c), unsigned])))

    def test_problems_name_the_file(self):
        problems = self.hist([green(self.c)])
        self.assertTrue(all(p["where"].startswith("0001-green.json") for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
