"""Wheel smoke test: set BB_WHEEL and HYVV_WHEEL to the two built artifacts."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]
PROBE = """
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path
import inspect, sys, tempfile

if sys.argv[1] == 'worker-first':
    import rails_emitter
from bundlebees_plugin import rails_cli
import rails_emitter
from rizzstack_rails.__main__ import RailsLedger as CoordinatorLedger

bb = distribution('bundlebees')
hyvv = distribution('hyvv')
assert Path(rails_cli.__file__) == bb.locate_file('bundlebees_plugin/rails_cli.py')
assert Path(rails_emitter.__file__) == hyvv.locate_file('rails_emitter.py')
assert inspect.getsourcefile(rails_cli.main) == str(bb.locate_file('rizzstack_rails/__main__.py'))
for obj, owned in [(CoordinatorLedger, bb.locate_file('rizzstack_rails/ledger.py')),
                   (rails_emitter.RailsLedger, hyvv.locate_file('contrib/rizzstack_rails/ledger.py'))]:
    assert Path(inspect.getsourcefile(obj)).read_bytes() == owned.read_bytes()
with tempfile.TemporaryDirectory() as tmp:
    worker = rails_emitter.RailsEmitter(tmp)
    with closing(worker._writer()) as db:
        assert db.allowed_authorities == {'worker_report'}
        try:
            db.append('decision.recorded', authority='coordinator_record', dedupe_key='forbidden',
                      source={}, scope={}, payload={})
        except rails_emitter.contracts.RailsRefusal as e:
            assert e.code == 'authority'
        else:
            raise AssertionError('worker accepted coordinator authority')
"""


class RailsPackagingTests(unittest.TestCase):
    def test_wheel_layout_refuses_drifted_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / 'bundlebees_plugin'
            plugin.mkdir()
            shutil.copy(ROOT / 'bundlebees_plugin/__init__.py', plugin)
            canonical = root / 'rizzstack_rails'
            shutil.copytree(ROOT / 'contrib/rizzstack_rails', canonical)
            worker = root / 'contrib/rizzstack_rails'
            worker.mkdir(parents=True)
            for name in ('__init__.py', 'contracts.py', 'ledger.py', 'privacy.py'):
                shutil.copy(canonical / name, worker)
            with (worker / 'ledger.py').open('ab') as f:
                f.write(b'\n# different worker pin\n')
            proc = subprocess.run([sys.executable, '-I', '-c',
                                   'import sys; sys.path.insert(0,sys.argv[1]); import bundlebees_plugin',
                                   tmp], cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Incompatible Rails runtimes', proc.stderr)

    def test_checkout_uses_local_coordinator_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, '-I', '-c', """
from pathlib import Path
import sys
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
from bundlebees_plugin import rails_cli
from rizzstack_rails import __main__
assert Path(__main__.__file__) == root/'contrib/rizzstack_rails/__main__.py'
assert rails_cli.main is __main__.main
""", str(ROOT)], cwd=tmp, check=True)

    def test_coinstalled_wheels(self):
        paths = [os.environ.get(key) for key in ('BB_WHEEL', 'HYVV_WHEEL')]
        if not any(paths):
            self.skipTest('requires explicit BB_WHEEL and HYVV_WHEEL artifacts')
        self.assertTrue(all(paths), 'supply both wheels')
        wheels = [Path(p).resolve(strict=True) for p in paths]
        for path in wheels:
            print(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}', flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            env = venv.EnvBuilder(with_pip=True)
            env.create(Path(tmp) / 'env')
            python = env.ensure_directories(Path(tmp) / 'env').env_exe
            subprocess.run([python, '-I', '-m', 'pip', '--isolated', 'install', '--no-index',
                            '--no-deps', *wheels], cwd=tmp, check=True)
            for order in ('worker-first', 'coordinator-first'):
                with self.subTest(order=order):
                    subprocess.run([python, '-I', '-c', PROBE, order], cwd=tmp, check=True)
