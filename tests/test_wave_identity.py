"""Wave records must be per job, not shared through a bare "w1"."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bundlebees_plugin import integrate_job


def job(job_id: str, wave="w1"):
    return {"job_id": job_id, "integration": {"wave": wave}}


class WaveIdentity(unittest.TestCase):
    def test_two_jobs_get_distinct_wave_records(self):
        a = integrate_job.job_wave_id(job("job_a"))
        b = integrate_job.job_wave_id(job("job_b"))
        self.assertEqual(a, "job_a.w1")
        self.assertEqual(b, "job_b.w1")
        self.assertNotEqual(a, b)

    def test_later_waves_keep_the_job_prefix(self):
        self.assertEqual(integrate_job.job_wave_id(job("job_a", wave=3)), "job_a.w3")
        self.assertEqual(integrate_job.job_wave_id(job("job_a", wave="w2")), "job_a.w2")

    def test_a_legacy_record_is_adopted_only_for_its_own_attempts(self):
        with TemporaryDirectory() as tmp:
            waves = Path(tmp) / "waves"
            waves.mkdir()
            (waves / "w1.json").write_text(json.dumps({"attempts": ["att_1"], "head_sha": "a" * 40}))

            adopted = integrate_job.adopt_legacy_wave(Path(tmp), wave="job_a.w1", legacy="w1", attempts=["att_1"])
            self.assertTrue(adopted)
            self.assertTrue((waves / "job_a.w1.json").exists())
            self.assertTrue((waves / "w1.json").exists(), "legacy evidence is preserved")

            # Another job with different attempts must not inherit it.
            other = integrate_job.adopt_legacy_wave(Path(tmp), wave="job_b.w1", legacy="w1", attempts=["att_2"])
            self.assertIsNone(other)
            self.assertFalse((waves / "job_b.w1.json").exists())

    def test_an_existing_per_job_record_is_never_overwritten(self):
        with TemporaryDirectory() as tmp:
            waves = Path(tmp) / "waves"
            waves.mkdir()
            (waves / "w1.json").write_text(json.dumps({"attempts": ["att_1"], "head_sha": "a" * 40}))
            (waves / "job_a.w1.json").write_text(json.dumps({"attempts": ["att_1"], "head_sha": "b" * 40}))

            adopted = integrate_job.adopt_legacy_wave(Path(tmp), wave="job_a.w1", legacy="w1", attempts=["att_1"])
            self.assertIsNone(adopted)
            record = json.loads((waves / "job_a.w1.json").read_text())
            self.assertEqual(record["head_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
