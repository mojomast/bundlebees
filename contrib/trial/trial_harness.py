"""Disposable restart trial harness for one campaign job.

What this module is
-------------------
It coordinates the *host-side* half of a restart trial: start the real
coordinator (``python -m bundlebees_plugin.factory_driver``), wait until a
worker attempt is genuinely running, stop **only that coordinator process**,
wait for the worker to complete while the coordinator is down, start the
coordinator again, and then verify from durable execution records that the
restart dispatched, verified and integrated exactly once and that the campaign
reached ``job_complete`` with closeout and owner acceptance still pending.

What it never does
------------------
* It never starts, stops or restarts the launcher or a worker. The launcher's
  per-attempt worker timeout is the launcher's; this harness only reads attempt
  records. ``TIMEOUT_SEMANTICS`` states that in every report.
* It never dispatches. A live trial assumes the job is already authorized and
  queued; the coordinator run 1 does the dispatching.
* It never uses ``shell=True``, a pipeline, ``tee``, ``pkill``, ``killpg`` or a
  process group. The driver is a direct :class:`subprocess.Popen` of an explicit
  argv; the recorded PID is the child itself and is checked against
  ``/proc/<pid>/cmdline`` before it is ever signalled.
* It never writes to the launcher. The injected launcher view is a read-only
  proxy; ``create``/``cancel``/``review`` are refused locally.

Two clocks, two timeouts
------------------------
``Deadline`` is **one** monotonic budget for the whole trial: run 1, the
coordinator downtime, the worker's completion while the coordinator is down,
and run 2. It is started once, before run 1, and is never reset; every wait and
every start decision uses the remaining time. When it expires the harness stops
waiting and refuses to start another coordinator run. It does **not** cancel a
worker attempt: a worker outliving the trial deadline is a fact the report
records, not something this harness can stop.

Three Python contexts
---------------------
They are deliberately separate and recorded separately in every report:

``coordinator``
    The project venv interpreter that runs ``bundlebees_plugin.factory_driver``.
    This host's coordinator venv is the only interpreter that can import the
    project's dependencies.
``sandbox_suite``
    The system interpreter (``/usr/bin/python3`` here) that runs the smoke
    suite *inside the verifier's sandbox*. It has **no pytest**, and the venv
    cannot be executed inside this host's firejail, so the suite is an explicit
    direct-test-runner command (``direct_test_runner_command``), never
    ``python -m pytest`` and never the coordinator interpreter.
``worker``
    ``fake`` for the local rehearsal (a simulated worker script under a local
    interpreter) or ``real`` for a launcher attempt (no local interpreter; the
    harness never starts or stops it).

Evidence
--------
Every count comes from a durable execution record, never from a file count or
prose:

* dispatch      -> ``jobs/<job_id>/intents/<task>-<n>.json`` records plus the
                   job document's ``tasks[*].attempts``;
* verification  -> the job's ``->verified`` transition records plus the
                   ``verify-results/<attempt>.json`` record;
* integration   -> the job's ``integration.accepted_waves`` records and the
                   create-only ``waves/<wave_id>.json`` record;
* worker        -> launcher attempt observations stamped with the harness's
                   own monotonic clock (the launcher's wall-clock timestamps are
                   kept for audit only).

Proof that the worker was running when the coordinator stopped, and that it
completed during the downtime, is built from those observations. When the proof
is unavailable the harness fails closed with a named limitation instead of
inferring it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from bundlebees_plugin import campaign as campaign_mod
from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as reconcile_mod
from bundlebees_plugin import return_report
from bundlebees_plugin import verify_local

SCHEMA = "bundlebees.trial-report/1"
INTENT_SCHEMA = "bundlebees.dispatch-intent/1"
REHEARSAL_MARKER = "trial-rehearsal.marker"

#: What each timeout in a trial actually stops. Copied verbatim into every
#: report so a reader cannot mistake the harness deadline for a worker cancel.
TIMEOUT_SEMANTICS: dict[str, dict[str, Any]] = {
    "overall_deadline": {
        "scope": "one trial: coordinator run 1, coordinator downtime, worker completion, coordinator run 2",
        "starts": "once, before driver run 1",
        "resets": False,
        "stops": ["the coordinator driver process this harness started", "the harness's own waits"],
        "does_not_stop": ["the launcher", "the worker attempt", "any worker container"],
        "on_expiry": "no further coordinator run is started; the driver is signalled and the trial fails closed",
    },
    "launcher_worker_timeout": {
        "scope": "one worker attempt",
        "starts": "by the launcher when it admits the attempt",
        "resets": "per attempt, by the launcher",
        "controlled_by": "launcher",
        "harness_cancellation": False,
        "stops": ["the worker attempt, and only the launcher can do that"],
        "note": (
            "the trial harness never cancels a worker attempt; it reads attempt records and "
            "reports a still-running worker as a named limitation"
        ),
    },
}

#: Launcher attempt states that mean the attempt is over. ``cancelled`` is not
#: completion; the harness reports it as a failed completion, never as success.
TERMINAL_WORKER_STATES = frozenset(
    {"succeeded", "success", "completed", "complete", "done", "failed", "error", "cancelled", "canceled",
     "timed_out", "timeout", "lost", "killed"}
)
SUCCESS_WORKER_STATES = frozenset({"succeeded", "success", "completed", "complete", "done"})
#: States that actually mean the worker is executing. A queued or starting
#: attempt is non-terminal but is not proof that the worker is running.
RUNNING_WORKER_STATES = frozenset({"running", "started", "in_progress", "executing"})

#: Readiness modes for the live CLI. ``file`` is kept for tests and for a host
#: whose driver writes its own ready file; the other two read durable records.
READY_ON_CHOICES = ("file", "dispatch", "worker-running")
RESTART_ON_CHOICES = ("file", "job-complete")
#: Job task states that prove a dispatch was recorded (never ``dispatching``:
#: the POST may still be in flight at that point).
DISPATCHED_TASK_STATES = frozenset({"dispatched", "running", "verifying", "verified", "accepted", "integrating"})
RUNNING_TASK_STATES = frozenset({"running", "verifying"})

#: Environment names passed to the coordinator child. Anything else in the
#: ambient environment is not inherited: an ambient ``PYTHONPATH`` entry can
#: shadow the pinned checkout, which is exactly what this harness refuses.
CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TERM",
    "TZ",
    "USER",
    "LOGNAME",
)


class TrialError(Exception):
    """The trial could not proceed; ``code`` is the durable name of the refusal."""

    def __init__(self, code: str, message: str, fix: str = "", *, report: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)
        self.report = report

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


class TrialRefused(TrialError):
    """A precondition or an exactly-once assertion refused the trial."""


class TrialIncomplete(TrialError):
    """The trial ran but the required proof is unavailable (fail closed)."""


# ---------------------------------------------------------------------------
# Clocks and the one deadline
# ---------------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real monotonic clock. ``time.monotonic`` never jumps with wall time."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock:
    """A deterministic clock for rehearsal tests.

    ``sleep`` advances logical time by at least the requested amount, so a
    bounded wait loop always makes progress. ``step`` lets a test consume a
    large logical interval per poll while the real wait stays tiny.
    """

    def __init__(self, start: float = 1000.0, *, step: float = 0.0) -> None:
        self._now = float(start)
        self.step = float(step)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        requested = max(0.0, float(seconds))
        self.sleeps.append(requested)
        self._now += max(requested, self.step)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


@dataclass(frozen=True)
class Deadline:
    """One monotonic budget, started once and never reset."""

    total_s: float
    started_at: float
    clock: Clock
    starts: int = 1

    @classmethod
    def start(cls, seconds: float, *, clock: Clock | None = None) -> "Deadline":
        value = float(seconds)
        if not value > 0:
            raise TrialRefused(
                "deadline_invalid",
                f"a trial deadline must be a positive number of seconds, got {seconds!r}",
                "Pass --deadline-seconds with a positive value.",
            )
        active = clock if clock is not None else SystemClock()
        return cls(total_s=value, started_at=active.monotonic(), clock=active)

    def elapsed(self) -> float:
        return max(0.0, self.clock.monotonic() - self.started_at)

    def remaining(self) -> float:
        return max(0.0, self.total_s - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def bounded(self, wanted: float) -> float:
        """A wait budget that can never exceed the remaining deadline."""

        return max(0.0, min(float(wanted), self.remaining()))

    def require(self, code: str, *, minimum: float = 0.0) -> float:
        left = self.remaining()
        if left <= minimum:
            raise TrialIncomplete(
                code,
                f"the trial deadline is exhausted ({left:.3f}s of {self.total_s:.3f}s left)",
                "A restart trial gets one deadline; start a new trial with a new authorization if more time is needed.",
            )
        return left

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_s": self.total_s,
            "started_at_mono": self.started_at,
            "elapsed_s": round(self.elapsed(), 6),
            "remaining_s": round(self.remaining(), 6),
            "expired": self.expired(),
            "starts": self.starts,
        }


# ---------------------------------------------------------------------------
# Pinned checkout and the three Python contexts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonContext:
    """One interpreter role. They are deliberately not interchangeable."""

    role: str
    interpreter: str

    def path(self) -> Path:
        return Path(self.interpreter)

    def validate(self) -> None:
        path = self.path()
        if not path.is_absolute():
            raise TrialRefused(
                "interpreter_not_absolute",
                f"the {self.role} interpreter must be an absolute path, got {self.interpreter!r}",
                "Pass the full path to the intended interpreter.",
            )
        if not path.exists():
            raise TrialRefused(
                "interpreter_missing",
                f"the {self.role} interpreter does not exist: {self.interpreter}",
                "Pass an existing interpreter for this role.",
            )
        if not os.access(path, os.X_OK):
            raise TrialRefused(
                "interpreter_not_executable",
                f"the {self.role} interpreter is not executable: {self.interpreter}",
                "Check permissions on the interpreter.",
            )


@dataclass(frozen=True)
class WorkerContext:
    """Where the worker runs. The harness never starts or stops it.

    ``fake`` is the local rehearsal context: a simulated worker script runs
    under a local interpreter and no launcher is involved. ``real`` means the
    launcher owns the attempt on another host; there is no local interpreter
    for it and this harness must never invent one.
    """

    kind: str
    interpreter: str | None = None
    host: str = ""

    def validate(self) -> None:
        if self.kind not in {"fake", "real"}:
            raise TrialRefused(
                "worker_context_invalid",
                f"worker context kind must be 'fake' or 'real', got {self.kind!r}",
                "Use the rehearsal context for a local simulation and the real context for a launcher trial.",
            )
        if self.kind == "fake":
            if not self.interpreter:
                raise TrialRefused(
                    "worker_context_invalid",
                    "a fake worker context needs an interpreter for its simulated worker script",
                    "Pass the interpreter that runs the simulated worker.",
                )
            PythonContext("worker", self.interpreter).validate()
        elif self.interpreter:
            raise TrialRefused(
                "worker_context_invalid",
                "a real worker context must not name a local interpreter; the worker runs under the launcher",
                "Leave the interpreter unset for a real worker and name its host instead.",
            )


@dataclass(frozen=True)
class PythonContexts:
    coordinator: PythonContext
    sandbox_suite: PythonContext
    worker: WorkerContext

    def validate(self) -> None:
        self.coordinator.validate()
        self.sandbox_suite.validate()
        self.worker.validate()
        if os.path.abspath(self.coordinator.interpreter) == os.path.abspath(self.sandbox_suite.interpreter):
            raise TrialRefused(
                "interpreter_conflict",
                (
                    "the sandbox suite interpreter is the coordinator interpreter "
                    f"({self.coordinator.interpreter}); the smoke suite is intended to run "
                    "outside the project venv"
                ),
                "Name the sandbox suite interpreter explicitly (for example /usr/bin/python3).",
            )
        if _is_venv_interpreter(self.sandbox_suite.interpreter):
            raise TrialRefused(
                "sandbox_suite_is_a_venv",
                (
                    f"the sandbox suite interpreter {self.sandbox_suite.interpreter} is a virtualenv; "
                    "the verifier's sandbox runs a system interpreter, and this host's venv cannot be "
                    "executed inside firejail"
                ),
                "Use the system interpreter (for example /usr/bin/python3) for the sandbox suite.",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinator": {"role": "coordinator", "interpreter": self.coordinator.interpreter},
            "sandbox_suite": {"role": "sandbox-suite", "interpreter": self.sandbox_suite.interpreter},
            "worker": {
                "role": "worker",
                "kind": self.worker.kind,
                "interpreter": self.worker.interpreter,
                "host": self.worker.host,
            },
        }


PROBE_SOURCE = (
    "import json, sys, bundlebees_plugin\n"
    "print(json.dumps({\n"
    "    'file': getattr(bundlebees_plugin, '__file__', None),\n"
    "    'executable': sys.executable,\n"
    "    'prefix': sys.prefix,\n"
    "}))\n"
)


@dataclass(frozen=True)
class Checkout:
    """The one bundlebees checkout every command and assertion must import."""

    root: Path
    contexts: PythonContexts

    # -- static checks ------------------------------------------------------

    def resolve(self) -> Path:
        try:
            return Path(self.root).resolve(strict=True)
        except OSError as exc:
            raise TrialRefused(
                "checkout_invalid",
                f"the pinned source root is not a usable directory: {self.root} ({exc})",
                "Pass --source-root pointing at the bundlebees checkout.",
            ) from exc

    def package_init(self) -> Path:
        root = self.resolve()
        init = root / "bundlebees_plugin" / "__init__.py"
        if not init.is_file():
            raise TrialRefused(
                "checkout_invalid",
                f"the pinned source root does not contain bundlebees_plugin: {root}",
                "Point --source-root at the checkout that holds bundlebees_plugin/.",
            )
        return init

    # -- environment --------------------------------------------------------

    def child_env(
        self,
        *,
        base: Mapping[str, str] | None = None,
        pythonpath_tail: str = "",
        extra: Mapping[str, str] | None = None,
        secret: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """A child environment whose ``bundlebees_plugin`` is the pinned one.

        The pinned root is always the *first* ``PYTHONPATH`` entry. The ambient
        ``PYTHONPATH`` is not inherited: a stale site-packages entry earlier in
        the path can shadow the checkout, so it is passed only when a caller
        names it explicitly as ``pythonpath_tail``.
        """

        source = dict(base) if base is not None else os.environ
        env = {name: source[name] for name in CHILD_ENV_ALLOWLIST if source.get(name)}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["BUNDLEBEES_SOURCE_ROOT"] = str(self.resolve())
        parts = [str(self.resolve())]
        if pythonpath_tail:
            parts.extend(part for part in str(pythonpath_tail).split(os.pathsep) if part)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        for name, value in (extra or {}).items():
            if not name or "=" in name:
                raise TrialRefused(
                    "child_env_invalid",
                    f"child environment name is invalid: {name!r}",
                    "Pass real environment names.",
                )
            env[str(name)] = str(value)
        for name, value in (secret or {}).items():
            if not name or "=" in name:
                raise TrialRefused(
                    "child_env_invalid",
                    f"child environment name is invalid: {name!r}",
                    "Pass real environment names.",
                )
            env[str(name)] = str(value)
        return env

    def child_env_names(self, *, extra: Mapping[str, str] | None = None,
                        secret: Mapping[str, str] | None = None) -> list[str]:
        """The environment *names* a child receives. Values are never returned."""

        return sorted(self.child_env(extra=extra, secret=secret))

    # -- interpreter / import verification ---------------------------------

    def verify(
        self,
        *,
        python: str | None = None,
        timeout_s: float = 30.0,
        run: Callable[..., subprocess.CompletedProcess] | None = None,
        probe_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prove the coordinator interpreter imports *this* checkout.

        ``probe_result`` is an injection seam for tests only: when supplied the
        probe subprocess is skipped, exactly like a mocked transport. The real
        path always runs a child process.
        """

        self.package_init()
        interpreter = str(python or self.contexts.coordinator.interpreter)
        result: Mapping[str, Any]
        if probe_result is not None:
            result = dict(probe_result)
        else:
            runner = run or subprocess.run
            env = self.child_env()
            try:
                completed = runner(
                    [interpreter, "-c", PROBE_SOURCE],
                    cwd=str(self.resolve()),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, float(timeout_s)),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TrialIncomplete(
                    "checkout_probe_failed",
                    f"the checkout probe could not run under {interpreter}: {exc}",
                    "Check the interpreter and the checkout; the harness will not guess which bundlebees it is using.",
                ) from exc
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:] or [""]
                raise TrialRefused(
                    "checkout_probe_failed",
                    (
                        f"importing bundlebees_plugin under {interpreter} failed with exit "
                        f"{completed.returncode}: {tail[0][:300]}"
                    ),
                    "Fix the pinned checkout or the interpreter before running a trial.",
                )
            try:
                result = json.loads(completed.stdout or "")
            except ValueError as exc:
                raise TrialRefused(
                    "checkout_probe_invalid",
                    f"the checkout probe printed no usable JSON: {completed.stdout[:300]!r}",
                    "The probe is internal to the harness; inspect the interpreter's stdout.",
                ) from exc
            if not isinstance(result, Mapping):
                raise TrialRefused(
                    "checkout_probe_invalid",
                    "the checkout probe result is not a JSON object",
                    "The probe is internal to the harness.",
                )

        reported = result.get("file")
        if not isinstance(reported, str) or not reported:
            raise TrialRefused(
                "checkout_not_importable",
                (
                    f"bundlebees_plugin has no file location under {interpreter}; "
                    "it is probably a namespace package or not installed there"
                ),
                "Run the trial from the pinned checkout with the coordinator interpreter.",
            )
        root = self.resolve()
        resolved = Path(reported).resolve()
        if not _is_within(resolved, root):
            raise TrialRefused(
                "checkout_mismatch",
                (
                    f"{interpreter} imports bundlebees_plugin from {resolved}, "
                    f"which is outside the pinned checkout {root}"
                ),
                "Prepend the pinned checkout to PYTHONPATH and remove the stale copy from the interpreter's environment.",
            )
        executable = result.get("executable")
        if isinstance(executable, str) and executable:
            expected = os.path.realpath(interpreter)
            actual = os.path.realpath(executable)
            if expected != actual:
                raise TrialRefused(
                    "interpreter_mismatch",
                    f"the probe was expected to run under {interpreter} but reported {executable}",
                    "Check how the interpreter path is resolved on this host.",
                )
        return {
            "source_root": str(root),
            "package_file": str(resolved),
            "interpreter": interpreter,
            "prefix": str(result.get("prefix") or ""),
            "pythonpath": self.child_env()["PYTHONPATH"],
        }

    # -- suite command ------------------------------------------------------

    def assert_suite_command(self, argv: Sequence[str], suite_command: Sequence[str]) -> dict[str, Any]:
        """The coordinator argv must name the *configured* suite command.

        The suite is never defaulted: this environment's system interpreter has
        no pytest, and the project venv cannot be executed inside the sandbox,
        so the smoke command is a direct test runner (see
        :func:`direct_test_runner_command`) supplied explicitly by the caller.
        """

        expected_command = tuple(str(item) for item in suite_command)
        if not expected_command:
            raise TrialRefused(
                "suite_command_not_configured",
                "no suite command was configured for this trial",
                "Pass suite_command explicitly (CLI: --suite-command ... as the last coordinator option).",
            )
        expected_python = os.path.realpath(self.contexts.sandbox_suite.interpreter)
        actual_python = os.path.realpath(expected_command[0])
        if _is_venv_interpreter(expected_command[0]):
            raise TrialRefused(
                "suite_interpreter_is_a_venv",
                (
                    f"the configured suite interpreter {expected_command[0]} is a virtualenv; the smoke "
                    "suite runs under the sandbox system interpreter, which on this host has no pytest"
                ),
                "Configure the direct-test-runner command with /usr/bin/python3.",
            )
        if actual_python != expected_python:
            raise TrialRefused(
                "suite_interpreter_mismatch",
                f"the configured suite runs under {expected_command[0]}, not the sandbox suite interpreter {expected_python}",
                "The smoke suite must use the sandbox system interpreter, never the coordinator venv.",
            )
        items = [str(item) for item in argv]
        if "--suite-command" not in items:
            raise TrialRefused(
                "suite_command_not_pinned",
                "the coordinator argv names no --suite-command; the suite interpreter would be a default guess",
                "Pass --suite-command with the configured explicit suite command.",
            )
        index = items.index("--suite-command")
        tail = tuple(items[index + 1 :])
        if not tail:
            raise TrialRefused(
                "suite_interpreter_missing",
                "--suite-command names no command",
                "Pass the configured suite command after --suite-command.",
            )
        if tail != expected_command:
            raise TrialRefused(
                "suite_command_mismatch",
                f"the coordinator would run {list(tail)!r}, not the configured {list(expected_command)!r}",
                "Use exactly the configured suite command for both coordinator runs.",
            )
        return {"suite_command": list(expected_command), "sandbox_suite_interpreter": expected_python}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_venv_interpreter(interpreter: str) -> bool:
    """Whether the interpreter lives in a virtualenv (``pyvenv.cfg`` above it)."""

    path = Path(interpreter)
    for parent in (path.parent.parent, path.parent):
        try:
            if (parent / "pyvenv.cfg").is_file():
                return True
        except OSError:
            continue
    return False


def direct_test_runner_command(
    sandbox_python: str,
    module: str,
    function: str,
) -> tuple[str, ...]:
    """An explicit sandbox suite command for a host whose system python has no pytest.

    ``/usr/bin/python3`` in this environment has no ``pytest``, and the project
    venv cannot be executed inside the sandbox, so the smoke suite is a direct
    import of one pytest-style test function. ``verify_local`` appends the named
    test target as ``argv[1]``; the ``-c`` program ignores it and selects the
    function itself.

    The command is a configuration value, never a default: the caller records
    it and the harness refuses a coordinator argv that names anything else.
    """

    program = (
        "import sys; "
        "sys.path.insert(0, '.'); "
        f"import {module} as t; "
        f"t.{function}()"
    )
    return (str(sandbox_python), "-c", program)


def default_contexts(*, rehearsal: bool, coordinator_python: str | None = None,
                     suite_python: str = "/usr/bin/python3") -> PythonContexts:
    """The three contexts: project venv, system sandbox suite, launcher worker.

    ``suite_python`` is a *role*: the interpreter the verifier's sandbox runs.
    On this host it has no pytest, so the suite command must be the explicit
    direct-test runner built by :func:`direct_test_runner_command`.
    """

    coordinator = str(coordinator_python or sys.executable)
    if rehearsal:
        worker = WorkerContext(kind="fake", interpreter=coordinator, host="localhost")
    else:
        worker = WorkerContext(kind="real", interpreter=None, host="launcher")
    return PythonContexts(
        coordinator=PythonContext("coordinator", coordinator),
        sandbox_suite=PythonContext("sandbox-suite", suite_python),
        worker=worker,
    )


# ---------------------------------------------------------------------------
# The coordinator process: start, record, stop exactly that PID
# ---------------------------------------------------------------------------


FACTORY_DRIVER_MODULE = "bundlebees_plugin.factory_driver"
SIMULATED_DRIVER_MODULE = "contrib.trial.trial_simulated_driver"
SIMULATED_DRIVER_SCRIPT = "trial_simulated_driver.py"
SHELL_NAMES = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish", "cmd.exe", "powershell"})
PIPELINE_TOKENS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "tee", "xargs"})


def validate_driver_argv(
    argv: Sequence[str],
    *,
    allowed_modules: Sequence[str] = (FACTORY_DRIVER_MODULE,),
    rehearsal: bool = False,
) -> tuple[str, ...]:
    """Refuse a wrapper, a shell or a pipeline as the coordinator command.

    ``allowed_modules`` is the set of ``-m`` modules that may be the driver. The
    default is the real coordinator. With ``rehearsal=True`` the allowlist is
    not widened: the argv must name exactly the simulated driver
    (``contrib.trial.trial_simulated_driver``) via ``-m`` — any other module
    is refused, and any ``.py`` script must be exactly
    ``trial_simulated_driver.py``. A rehearsal never starts the real factory
    driver.
    """

    items = [str(item) for item in argv]
    if not items:
        raise TrialRefused("driver_argv_invalid", "the coordinator argv is empty", "Pass an explicit argv.")
    if any(not item for item in items):
        raise TrialRefused("driver_argv_invalid", "the coordinator argv contains an empty argument", "Pass an explicit argv.")
    program = os.path.basename(items[0]).lower()
    if program in SHELL_NAMES:
        raise TrialRefused(
            "driver_argv_is_a_shell",
            f"the coordinator argv starts a shell ({items[0]}); a shell is a wrapper, not the driver",
            "Pass the driver interpreter as argv[0] and the module arguments after it.",
        )
    for index, item in enumerate(items):
        if item in PIPELINE_TOKENS:
            raise TrialRefused(
                "driver_argv_is_a_pipeline",
                f"the coordinator argv contains a pipeline/redirection token {item!r} at position {index}",
                "Run the driver directly; the harness owns stdout and does not tee.",
            )
    modules = [items[i + 1] for i, item in enumerate(items[:-1]) if item == "-m"]
    scripts = [item for item in items if item.endswith(".py")]
    if rehearsal:
        if not modules or set(modules) != {SIMULATED_DRIVER_MODULE}:
            raise TrialRefused(
                "rehearsal_requires_simulated_driver",
                (
                    "a rehearsal trial may only start the simulated driver "
                    f"{SIMULATED_DRIVER_MODULE}; its driver argv names {sorted(set(modules))!r}"
                ),
                (
                    "Use rehearsal_driver_argv() or the rehearse CLI; a rehearsal never "
                    "starts the real factory driver."
                ),
            )
        if scripts and {Path(item).name for item in scripts} != {SIMULATED_DRIVER_SCRIPT}:
            raise TrialRefused(
                "rehearsal_requires_simulated_driver",
                (
                    "a rehearsal trial may only run the simulated driver script "
                    f"{SIMULATED_DRIVER_SCRIPT!r}; its driver argv names "
                    f"{sorted({Path(item).name for item in scripts})!r}"
                ),
                (
                    "Use rehearsal_driver_argv() or the rehearse CLI; a rehearsal never "
                    "starts the real factory driver."
                ),
            )
        return tuple(items)
    if scripts:
        names = {Path(item).name for item in scripts}
        if names != {"factory_driver.py"}:
            raise TrialRefused(
                "driver_argv_not_the_driver",
                f"the coordinator script is {sorted(names)!r}, not the Bundlebees factory driver",
                "Run bundlebees_plugin.factory_driver directly; any other script is a wrapper.",
            )
    if not any(module in allowed_modules for module in modules) and not scripts:
        raise TrialRefused(
            "driver_argv_not_the_driver",
            f"the coordinator argv does not run one of {sorted(allowed_modules)}",
            "Use: <coordinator python> -m bundlebees_plugin.factory_driver ...",
        )
    return tuple(items)


def _proc_cmdline(pid: int) -> tuple[str, ...] | None:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    return tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


@dataclass
class DriverProcess:
    """The exact child process this harness started and may signal."""

    run: int
    argv: tuple[str, ...]
    proc: subprocess.Popen
    log_path: Path
    started_at: float
    identity_verified: bool = True
    _log: Any = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    def close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            finally:
                self._log = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "pid": self.pid,
            "argv": list(self.argv),
            "started_at_mono": self.started_at,
            "returncode": self.returncode,
            "identity_verified": self.identity_verified,
            "log": str(self.log_path),
        }


def start_driver(
    argv: Sequence[str],
    *,
    run: int,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    clock: Clock,
    allowed_modules: Sequence[str] = (FACTORY_DRIVER_MODULE,),
    validate_argv: bool = True,
    rehearsal: bool = False,
) -> DriverProcess:
    """Start the coordinator directly and prove the recorded PID is the driver.

    ``subprocess.Popen(argv)`` with an explicit argv and ``shell=False`` makes
    the child exactly the program named in ``argv``; there is no wrapping shell
    or tee in between. ``/proc/<pid>/cmdline`` is compared with the argv we
    started, so a swapped or wrapped process is a named refusal before anything
    is signalled. ``rehearsal=True`` refuses every driver module except the
    simulated one before any process is started.
    """

    items = (
        validate_driver_argv(argv, allowed_modules=allowed_modules, rehearsal=rehearsal)
        if validate_argv
        else tuple(str(item) for item in argv)
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            list(items),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
        )
    except OSError as exc:
        handle.close()
        raise TrialRefused(
            "driver_start_failed",
            f"the coordinator could not be started: {type(exc).__name__}: {exc}",
            "Check the coordinator argv, interpreter and working directory.",
        ) from exc
    record = DriverProcess(
        run=run, argv=items, proc=proc, log_path=log_path,
        started_at=clock.monotonic(), _log=handle,
    )
    reported = _proc_cmdline(record.pid)
    if reported != items and proc.poll() is None:
        # Across fork/exec procfs can briefly show an empty cmdline *or our
        # own*. Neither identifies the driver. Retry only those two cases;
        # keep an unrelated wrapper a refusal and require an exact match.
        inherited = _proc_cmdline(os.getpid())
        for _ in range(20):
            if (reported and reported != inherited) or proc.poll() is not None:
                break
            time.sleep(0.002)
            reported = _proc_cmdline(record.pid)
            if reported == items:
                break
    if reported and reported != items:
        record.close_log()
        _signal_pid(record.pid, signal.SIGKILL, reason="identity_mismatch")
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        raise TrialRefused(
            "driver_identity_mismatch",
            (
                f"the recorded pid {record.pid} runs {list(reported)!r}, not the argv the harness "
                "started; refusing to record or signal it as the coordinator"
            ),
            "Start the driver directly, without a wrapper or a pipeline.",
        )
    if not reported:
        # The child may have exited before /proc could be read (a fast --once
        # run 2 is legitimate). The PID is never signalled without a live
        # returncode check, so the audit marks the identity unverified.
        record.identity_verified = False
    return record


def _signal_pid(pid: int, signum: int, *, reason: str) -> dict[str, Any]:
    """Signal exactly one PID. Never a process group, never a pattern."""

    number = int(pid)
    if number <= 0:
        raise TrialRefused("pid_invalid", f"refusing to signal pid {pid!r}", "Record the child's real pid.")
    try:
        os.kill(number, signum)
    except ProcessLookupError:
        return {"pid": number, "signal": signum, "reason": reason, "delivered": False, "detail": "no such process"}
    except PermissionError as exc:
        raise TrialRefused(
            "signal_refused",
            f"signalling pid {number} was refused: {exc}",
            "Run the harness as the user that owns the coordinator process.",
        ) from exc
    return {"pid": number, "signal": signum, "reason": reason, "delivered": True}


def _still_our_process(pid: int, argv: Sequence[str]) -> bool:
    """Whether ``pid`` is still alive *and* still the program we started."""

    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    reported = _proc_cmdline(int(pid))
    if reported is None:
        return False
    return tuple(reported) == tuple(argv)


@dataclass
class StopReport:
    pid: int
    request_signal: str | None
    signals: list[dict[str, Any]]
    waits: list[dict[str, Any]]
    returncode: int | None
    shutdown_confirmed: bool
    code: str
    stop_requested_at: float
    exited_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "request_signal": self.request_signal,
            "signals": self.signals,
            "waits": self.waits,
            "returncode": self.returncode,
            "shutdown_confirmed": self.shutdown_confirmed,
            "code": self.code,
            "stop_requested_at_mono": self.stop_requested_at,
            "exited_at_mono": self.exited_at,
            "duration_s": (
                None if self.exited_at is None else round(self.exited_at - self.stop_requested_at, 6)
            ),
        }


def stop_driver(
    record: DriverProcess,
    *,
    deadline: Deadline,
    clock: Clock,
    term_grace_s: float = 5.0,
    kill_grace_s: float = 2.0,
) -> StopReport:
    """SIGTERM the recorded PID, wait within the deadline, then SIGKILL it.

    Every wait budget is ``min(grace, remaining deadline)``; a child therefore
    cannot hold cleanup past the deadline. ``returncode`` is asserted to be set
    before shutdown is called confirmed.
    """

    if int(record.pid) != int(record.proc.pid):
        raise TrialRefused(
            "pid_mismatch",
            f"recorded pid {record.pid} is not the started pid {record.proc.pid}",
            "Refuse to signal anything but the process this harness started.",
        )
    pid = record.pid
    waits: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    stop_at = clock.monotonic()

    def wait_once(label: str, wanted: float) -> int | None:
        remaining = deadline.remaining()
        budget = max(0.0, min(float(wanted), remaining))
        waits.append({"label": label, "budget_s": round(budget, 6), "remaining_s": round(remaining, 6)})
        if budget <= 0:
            return record.returncode
        try:
            return record.proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            return None

    if record.returncode is not None:
        record.close_log()
        return StopReport(
            pid=pid, request_signal=None, signals=signals, waits=waits,
            returncode=record.returncode, shutdown_confirmed=True, code="already_exited",
            stop_requested_at=stop_at, exited_at=clock.monotonic(),
        )

    if deadline.expired():
        signals.append(_signal_pid(pid, signal.SIGKILL, reason="deadline_expired"))
        _reap_now(record.proc)
        confirmed = record.returncode is not None and not _still_our_process(pid, record.argv)
        record.close_log()
        return StopReport(
            pid=pid, request_signal="SIGKILL", signals=signals, waits=waits,
            returncode=record.returncode, shutdown_confirmed=confirmed,
            code="stopped_after_deadline_sigkill" if confirmed else "deadline_exceeded",
            stop_requested_at=stop_at, exited_at=clock.monotonic() if confirmed else None,
        )

    signals.append(_signal_pid(pid, signal.SIGTERM, reason="trial_stop"))
    returncode = wait_once("SIGTERM", term_grace_s)
    if returncode is not None:
        exited_at = clock.monotonic()
        confirmed = record.returncode is not None
        record.close_log()
        return StopReport(
            pid=pid, request_signal="SIGTERM", signals=signals, waits=waits,
            returncode=record.returncode, shutdown_confirmed=confirmed,
            code="stopped" if confirmed else "exit_unconfirmed",
            stop_requested_at=stop_at, exited_at=exited_at,
        )

    if deadline.expired():
        signals.append(_signal_pid(pid, signal.SIGKILL, reason="deadline_expired"))
        _reap_now(record.proc)
        confirmed = record.returncode is not None and not _still_our_process(pid, record.argv)
        record.close_log()
        return StopReport(
            pid=pid, request_signal="SIGTERM", signals=signals, waits=waits,
            returncode=record.returncode, shutdown_confirmed=confirmed,
            code="stopped_after_deadline_sigkill" if confirmed else "deadline_exceeded",
            stop_requested_at=stop_at, exited_at=clock.monotonic() if confirmed else None,
        )

    signals.append(_signal_pid(pid, signal.SIGKILL, reason="sigterm_ignored"))
    returncode = wait_once("SIGKILL", kill_grace_s)
    exited_at = clock.monotonic() if returncode is not None else None
    still_ours = _still_our_process(pid, record.argv)
    confirmed = record.returncode is not None and not still_ours
    if returncode is not None and still_ours:  # pragma: no cover - kernel reaped it; defensive
        confirmed = False
    record.close_log()
    return StopReport(
        pid=pid, request_signal="SIGTERM", signals=signals, waits=waits,
        returncode=record.returncode, shutdown_confirmed=confirmed,
        code="stopped_after_sigkill" if confirmed else "shutdown_unconfirmed",
        stop_requested_at=stop_at, exited_at=exited_at,
    )


def _reap_now(proc: subprocess.Popen, *, attempts: int = 5) -> None:
    """Reap a just-killed child without waiting on its work."""

    for _ in range(max(1, attempts)):
        if proc.poll() is not None:
            return
        time.sleep(0.001)


# ---------------------------------------------------------------------------
# Approval receipt binding (the approval lane owns the system)
# ---------------------------------------------------------------------------


class ApprovalAdapter(Protocol):
    def verify_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def consume_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...


def resolve_approval_adapter(adapter: ApprovalAdapter | None = None) -> ApprovalAdapter | None:
    """Bind to ``bundlebees_plugin.approval`` when that lane has landed.

    The harness does not implement a second approval system: it only calls the
    approval module's ``verify_receipt`` and ``consume_receipt``. When the
    module or either function is absent, the adapter is ``None`` and a live
    trial refuses with ``approval_unavailable``.
    """

    if adapter is not None:
        return adapter
    try:
        approval_mod = importlib.import_module("bundlebees_plugin.approval")
    except ImportError:
        return None
    verify = getattr(approval_mod, "verify_receipt", None)
    consume = getattr(approval_mod, "consume_receipt", None)
    if not callable(verify) or not callable(consume):
        return None
    return _ModuleApproval(verify, consume)


class _ModuleApproval:
    def __init__(self, verify: Callable[..., Any], consume: Callable[..., Any]) -> None:
        self._verify = verify
        self._consume = consume

    def verify_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._verify(receipt)

    def consume_receipt(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        # The approval lane keys consumption to a stable consumer. A trial is
        # exactly one consumer; a second trial with the same receipt is a
        # replay and must be refused by the approval module.
        return self._consume(receipt, consumer="bundlebees-restart-trial")


def _verified_receipt(result: Any) -> dict[str, Any]:
    """Accept only an explicit positive receipt verdict."""

    if not isinstance(result, Mapping):
        raise TrialRefused(
            "approval_receipt_unusable",
            f"verify_receipt returned {type(result).__name__}, not a mapping",
            "Use the approval lane's receipt verifier; the harness will not guess a verdict.",
        )
    decision_value = str(result.get("decision") or "").strip().lower()
    if decision_value == "declined":
        raise TrialRefused(
            "approval_receipt_declined",
            "the owner declined this trial",
            "Do not dispatch; ask for a fresh request if the work changes.",
        )
    positive = decision_value == "approved" or any(
        result.get(key) is True for key in ("ok", "valid", "verified", "approved")
    )
    identity = str(
        result.get("receipt_id") or result.get("request_id") or result.get("id") or ""
    ).strip()
    if not positive or not identity:
        raise TrialRefused(
            "approval_receipt_rejected",
            f"the approval receipt was not accepted: {dict(result)!r}"[:500],
            "Obtain a valid approval receipt for this trial before running it.",
        )
    return dict(result)


# ---------------------------------------------------------------------------
# Read-only launcher view and worker observations
# ---------------------------------------------------------------------------


READ_ONLY_LAUNCHER_METHODS = frozenset(
    {
        "health",
        "capacity",
        "attempts",
        "attempts_raw",
        "get",
        "get_raw",
        "events",
        "result",
        "result_envelope",
        "projects",
        "containers",
        "landing_events",
        "landing_export",
    }
)


class ReadOnlyLauncher:
    """A launcher proxy that can only read. Writes are refused locally."""

    def __init__(self, inner: Any) -> None:
        if inner is None:
            raise TrialRefused("launcher_missing", "no launcher reader was supplied", "Inject a launcher client or omit preflight.")
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name not in READ_ONLY_LAUNCHER_METHODS:
            raise TrialRefused(
                "launcher_write_refused",
                f"the trial harness may only read launcher state; {name!r} is not a read method",
                "Use a read-only launcher client for a trial.",
            )
        return getattr(self._inner, name)

    def health(self) -> dict[str, Any]:
        return dict(self._inner.health() or {})

    def attempts(self) -> list[dict[str, Any]]:
        return [dict(item) for item in (self._inner.attempts() or [])]


@dataclass(frozen=True)
class WorkerObservation:
    """One read of the launcher's attempt records, stamped monotonically."""

    attempt_id: str
    state: str
    observed_at: float
    started_at: str = ""
    finished_at: str = ""
    source: str = "launcher"
    cursor: int | None = None

    @property
    def terminal(self) -> bool:
        return self.state.strip().lower() in TERMINAL_WORKER_STATES

    @property
    def succeeded(self) -> bool:
        return self.state.strip().lower() in SUCCESS_WORKER_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "observed_at_mono": self.observed_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "source": self.source,
            "cursor": self.cursor,
            "terminal": self.terminal,
        }


class WorkerProbe(Protocol):
    def observe(self) -> Sequence[WorkerObservation]: ...


class LauncherAttemptProbe:
    """The real probe: reads launcher attempt records and stamps them locally.

    The launcher's own ``started_at``/``finished_at`` are wall-clock strings and
    are kept for audit only. The proof of ordering uses ``observed_at`` from the
    harness's monotonic clock, taken at the moment the record was read.
    """

    def __init__(self, reader: Any, *, clock: Clock, attempt_id: str = "") -> None:
        self.reader = reader if isinstance(reader, ReadOnlyLauncher) else ReadOnlyLauncher(reader)
        self.clock = clock
        self.attempt_id = str(attempt_id or "")

    def observe(self) -> list[WorkerObservation]:
        try:
            entries = self.reader.attempts()
        except TrialError:
            raise
        except Exception as exc:  # noqa: BLE001 - a launcher read failure is a named limitation
            raise TrialIncomplete(
                "worker_observation_failed",
                f"launcher attempt records could not be read: {type(exc).__name__}: {exc}",
                "Check the launcher health; the trial cannot prove worker state without its records.",
            ) from exc
        now = self.clock.monotonic()
        found: list[WorkerObservation] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            attempt_id = str(entry.get("attempt_id") or entry.get("id") or "")
            if not attempt_id or (self.attempt_id and attempt_id != self.attempt_id):
                continue
            state = str(entry.get("state") or entry.get("status") or "")
            if not state:
                continue
            cursor = entry.get("event_cursor", entry.get("cursor"))
            found.append(
                WorkerObservation(
                    attempt_id=attempt_id,
                    state=state,
                    observed_at=now,
                    started_at=str(entry.get("started_at") or entry.get("created_at") or ""),
                    finished_at=str(entry.get("finished_at") or ""),
                    source="launcher",
                    cursor=int(cursor) if isinstance(cursor, (int, float)) and not isinstance(cursor, bool) else None,
                )
            )
        return found


# ---------------------------------------------------------------------------
# Durable execution records -> counts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchRecord:
    task_id: str
    attempt_no: int
    status: str
    attempt_id: str
    idempotency_key: str
    path: str
    written_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "path": self.path,
            "written_at": self.written_at,
        }


@dataclass(frozen=True)
class VerificationRecord:
    task_id: str
    attempt_id: str
    path: str
    ok: bool
    sha: str
    base_rev: str
    gitleaks_exit: Any
    suite_exit: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "path": self.path,
            "ok": self.ok,
            "sha": self.sha,
            "base_rev": self.base_rev,
            "gitleaks_exit": self.gitleaks_exit,
            "suite_exit": self.suite_exit,
        }


@dataclass(frozen=True)
class WaveRecord:
    wave_id: str
    accepted: bool
    head_sha: str
    attempts: tuple[str, ...]
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave_id": self.wave_id,
            "accepted": self.accepted,
            "head_sha": self.head_sha,
            "attempts": list(self.attempts),
            "path": self.path,
        }


@dataclass
class TrialEvidence:
    job_id: str
    task_states: dict[str, str]
    job_attempts: dict[str, list[str]]
    dispatches: list[DispatchRecord]
    verifications: list[VerificationRecord]
    verify_transitions: int
    accepted_waves: list[dict[str, Any]]
    wave_records: list[WaveRecord]

    # -- derived counts ----------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {
            "dispatch_records": len(self.dispatches),
            "dispatched_attempts": len({item.attempt_id for item in self.dispatches if item.attempt_id}),
            "job_attempts": sum(len(items) for items in self.job_attempts.values()),
            "verification_records": len(self.verifications),
            "verify_transitions": self.verify_transitions,
            "accepted_waves": len(self.accepted_waves),
            "accepted_wave_records": len([item for item in self.wave_records if item.accepted]),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_states": dict(sorted(self.task_states.items())),
            "job_attempts": {key: list(value) for key, value in sorted(self.job_attempts.items())},
            "dispatch_records": [item.to_dict() for item in self.dispatches],
            "verification_records": [item.to_dict() for item in self.verifications],
            "verify_transitions": self.verify_transitions,
            "accepted_waves": list(self.accepted_waves),
            "accepted_wave_records": [item.to_dict() for item in self.wave_records],
            "counts": self.counts(),
        }

    # -- exactly once ------------------------------------------------------

    def assert_exactly_once(self) -> dict[str, Any]:
        """One dispatch, one verification, one accepted wave -- or a refusal.

        Every branch names the record that proves the duplicate, so a reader can
        inspect it instead of trusting the count.
        """

        dispatched = [item for item in self.dispatches if item.attempt_id]
        unproven = [item for item in self.dispatches if not item.attempt_id]
        if unproven:
            raise TrialIncomplete(
                "dispatch_unproven",
                (
                    "a dispatch intent exists without an attempt id: "
                    + ", ".join(f"{item.task_id}#{item.attempt_no}" for item in unproven)
                ),
                "Inspect the intent records; an unconfirmed dispatch is not evidence of a completed one.",
            )
        if not dispatched:
            raise TrialRefused(
                "missing_dispatch",
                "no dispatch intent record names an attempt; the coordinator never dispatched",
                "Check that run 1 reached the dispatch step and recorded an attempt.",
            )
        if len(dispatched) > 1 or len({item.attempt_id for item in dispatched}) > 1:
            raise TrialRefused(
                "duplicate_dispatch",
                (
                    "more than one dispatch is recorded: "
                    + ", ".join(f"{item.task_id}#{item.attempt_no}->{item.attempt_id}" for item in dispatched)
                ),
                "A restart must adopt the persisted dispatch; a second dispatch is a job/queue seam failure.",
            )
        attempt_id = dispatched[0].attempt_id
        all_attempts = [item for items in self.job_attempts.values() for item in items]
        if all_attempts != [attempt_id]:
            raise TrialRefused(
                "duplicate_attempt",
                f"the job records attempts {all_attempts!r}, expected exactly [{attempt_id!r}]",
                "A restart must not append a second attempt for the same task.",
            )

        if self.verify_transitions == 0:
            raise TrialRefused(
                "missing_verification",
                "the job records no ->verified transition; verification never ran",
                "Check that run 2 verified the completed attempt.",
            )
        if self.verify_transitions > 1:
            raise TrialRefused(
                "duplicate_verification",
                f"the job records {self.verify_transitions} ->verified transitions, expected exactly 1",
                "Verification must be recorded once per attempt; a second one is a duplicate.",
            )
        if len(self.verifications) != 1:
            raise TrialRefused(
                "duplicate_verification" if len(self.verifications) > 1 else "missing_verification",
                f"{len(self.verifications)} verification record(s) exist for attempt {attempt_id}, expected exactly 1",
                "Inspect verify-results/; verification evidence is per attempt and must be recorded once.",
            )
        verification = self.verifications[0]
        if not verification.ok or not verification.sha:
            raise TrialRefused(
                "verification_not_ok",
                f"the verification record for {attempt_id} is not a pass with a commit sha",
                "Re-run verification; a failed record is not acceptance evidence.",
            )
        if verification.gitleaks_exit != 0 or verification.suite_exit != 0:
            raise TrialRefused(
                "verification_gates_failed",
                (
                    f"verification for {attempt_id} records gitleaks={verification.gitleaks_exit!r} "
                    f"and suite={verification.suite_exit!r}"
                ),
                "Both gates must pass; a failed gate is not acceptance evidence.",
            )
        if verification.attempt_id != attempt_id:
            raise TrialRefused(
                "verification_attempt_mismatch",
                f"the verification record names {verification.attempt_id}, not the dispatched {attempt_id}",
                "Inspect the job's attempt bindings.",
            )

        if not self.accepted_waves:
            raise TrialRefused(
                "missing_accepted_wave",
                "the job document records no accepted wave",
                "Check that run 2 integrated the verified attempt.",
            )
        if len(self.accepted_waves) > 1 or len([w for w in self.wave_records if w.accepted]) > 1:
            raise TrialRefused(
                "duplicate_accepted_wave",
                (
                    f"the job records {len(self.accepted_waves)} accepted wave(s) and "
                    f"{len([w for w in self.wave_records if w.accepted])} accepted wave record(s), expected exactly 1"
                ),
                "A restart must adopt an accepted wave; a second accepted wave is a duplicate integration.",
            )
        waves = [w for w in self.wave_records if w.accepted]
        if not waves:
            raise TrialIncomplete(
                "accepted_wave_record_missing",
                "the job records an accepted wave but no create-only wave record exists",
                "Inspect waves/; the job's claim alone is not the integration record.",
            )
        wave = waves[0]
        last_commit = str(self.accepted_waves[0].get("commit") or "")
        if not last_commit or wave.head_sha != last_commit:
            raise TrialRefused(
                "accepted_wave_mismatch",
                f"the wave record head {wave.head_sha!r} does not match the job's accepted commit {last_commit!r}",
                "Inspect the wave record and the job's accepted_waves entry.",
            )
        if attempt_id not in wave.attempts:
            raise TrialRefused(
                "accepted_wave_attempt_mismatch",
                f"the wave record names attempts {list(wave.attempts)!r}, not the dispatched {attempt_id}",
                "Inspect the wave record.",
            )
        return self.counts()


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrialIncomplete(
            f"{what}_unreadable",
            f"{path} cannot be read: {exc}",
            "Inspect the state directory; unreadable evidence is not absent evidence.",
        ) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise TrialIncomplete(
            f"{what}_corrupt",
            f"{path} is not valid JSON: {exc}",
            "Inspect the record by hand; a corrupt record is never permission to proceed.",
        ) from exc
    if not isinstance(value, dict):
        raise TrialIncomplete(
            f"{what}_corrupt",
            f"{path} is not a JSON object",
            "Inspect the record by hand.",
        )
    return value


def collect_evidence(state_dir: Path | str, job_id: str) -> TrialEvidence:
    """Read the job's execution records. Read-only; no state is written."""

    state = Path(state_dir)
    job = jobs.load_job(state, str(job_id), recover=False)
    tasks = [task for task in job.get("tasks") or [] if isinstance(task, Mapping)]
    task_states = {str(task.get("task_id")): str(task.get("state") or "pending") for task in tasks}
    job_attempts = {
        str(task.get("task_id")): [str(item) for item in (task.get("attempts") or [])]
        for task in tasks
    }

    dispatches: list[DispatchRecord] = []
    intents_dir = state / jobs.JOBS_DIR / str(job_id) / "intents"
    if intents_dir.is_dir():
        for path in sorted(intents_dir.glob("*.json")):
            record = _read_json_object(path, what="dispatch_intent")
            status = str(record.get("status") or "")
            if status == reconcile_mod.INTENT_NOT_SENT:
                continue
            dispatches.append(
                DispatchRecord(
                    task_id=str(record.get("task_id") or ""),
                    attempt_no=int(record.get("attempt_no") or 0),
                    status=status,
                    attempt_id=str(record.get("attempt_id") or ""),
                    idempotency_key=str(record.get("idempotency_key") or ""),
                    path=str(path),
                    written_at=float(record["written_at"]) if isinstance(record.get("written_at"), (int, float)) else None,
                )
            )

    events = [event for event in job.get("events") or [] if isinstance(event, Mapping)]
    verify_transitions = len(
        [event for event in events if str(event.get("transition") or "").endswith("->verified")]
    )

    verifications: list[VerificationRecord] = []
    for task_id, attempts in job_attempts.items():
        for attempt_id in attempts:
            record = verify_local.read_record(state, attempt_id)
            if not isinstance(record, Mapping):
                continue
            verifications.append(
                VerificationRecord(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    path=str(verify_local.record_path(state, attempt_id)),
                    ok=record.get("ok") is True,
                    sha=str(record.get("sha") or ""),
                    base_rev=str(record.get("base_rev") or ""),
                    gitleaks_exit=(record.get("gitleaks") or {}).get("exit_code")
                    if isinstance(record.get("gitleaks"), Mapping) else None,
                    suite_exit=(record.get("suite") or {}).get("exit_code")
                    if isinstance(record.get("suite"), Mapping) else None,
                )
            )

    integration = job.get("integration") if isinstance(job.get("integration"), Mapping) else {}
    accepted_waves = [dict(item) for item in (integration.get("accepted_waves") or []) if isinstance(item, Mapping)]

    wave_records: list[WaveRecord] = []
    waves_dir = state / "waves"
    if waves_dir.is_dir():
        for path in sorted(waves_dir.glob("*.json")):
            record = _read_json_object(path, what="wave_record")
            wave_records.append(
                WaveRecord(
                    wave_id=str(record.get("wave_id") or path.stem),
                    accepted=record.get("accepted") is True,
                    head_sha=str(record.get("head_sha") or ""),
                    attempts=tuple(str(item) for item in (record.get("attempts") or [])),
                    path=str(path),
                )
            )

    return TrialEvidence(
        job_id=str(job_id),
        task_states=task_states,
        job_attempts=job_attempts,
        dispatches=dispatches,
        verifications=verifications,
        verify_transitions=verify_transitions,
        accepted_waves=accepted_waves,
        wave_records=wave_records,
    )


# ---------------------------------------------------------------------------
# Worker proofs
# ---------------------------------------------------------------------------


def running_attempt(observations: Iterable[WorkerObservation]) -> WorkerObservation:
    """The one attempt the launcher reports as running, or a named refusal."""

    live = [
        item for item in observations
        if item.state.strip().lower() in RUNNING_WORKER_STATES
    ]
    unique = {item.attempt_id: item for item in live}
    if not unique:
        seen = sorted({f"{item.attempt_id}:{item.state}" for item in observations})
        raise TrialIncomplete(
            "worker_not_running",
            "no launcher attempt record shows a running worker at the stop boundary"
            + (f" (observed {seen})" if seen else ""),
            "Wait until the attempt is running before stopping the coordinator.",
        )
    if len(unique) > 1:
        raise TrialIncomplete(
            "worker_ambiguous",
            f"more than one attempt is running ({sorted(unique)}); the trial cannot choose one",
            "Run the trial with exactly one in-flight worker.",
        )
    return next(iter(unique.values()))


def prove_running_at_stop(
    *,
    pre_stop: WorkerObservation,
    stop_at: float,
    observations: Sequence[WorkerObservation],
    staleness_s: float,
) -> dict[str, Any]:
    """Prove the attempt was live when the coordinator was told to stop."""

    if pre_stop.terminal:
        raise TrialIncomplete(
            "worker_running_unproven",
            f"attempt {pre_stop.attempt_id} was already {pre_stop.state!r} before the stop request",
            "The worker must still be running when the coordinator is stopped.",
        )
    if pre_stop.observed_at > stop_at:
        raise TrialIncomplete(
            "worker_running_unproven",
            "the running observation is later than the stop request; ordering cannot be proven",
            "Observe the worker before signalling the coordinator.",
        )
    age = stop_at - pre_stop.observed_at
    if age > staleness_s:
        raise TrialIncomplete(
            "worker_running_stale",
            (
                f"the running observation for {pre_stop.attempt_id} is {age:.3f}s old "
                f"(limit {staleness_s:.3f}s) at the stop request"
            ),
            "Re-observe the worker immediately before stopping the coordinator.",
        )
    early_terminal = [
        item for item in observations
        if item.attempt_id == pre_stop.attempt_id and item.terminal and item.observed_at <= stop_at
    ]
    if early_terminal:
        raise TrialIncomplete(
            "worker_running_unproven",
            (
                f"attempt {pre_stop.attempt_id} was already terminal ({early_terminal[0].state!r}) "
                "before the stop request"
            ),
            "Do not infer a running worker from an older observation.",
        )
    return {
        "attempt_id": pre_stop.attempt_id,
        "observed_state": pre_stop.state,
        "observed_at_mono": pre_stop.observed_at,
        "stop_requested_at_mono": stop_at,
        "age_s": round(age, 6),
        "staleness_limit_s": staleness_s,
    }


def prove_downtime_completion(
    *,
    attempt_id: str,
    driver_exit_at: float,
    observations: Sequence[WorkerObservation],
    restart_started_at: float | None,
) -> dict[str, Any]:
    """Prove the attempt completed after the coordinator exited and before run 2."""

    terminal = [
        item for item in observations
        if item.attempt_id == attempt_id and item.terminal and item.observed_at > driver_exit_at
    ]
    if not terminal:
        raise TrialIncomplete(
            "worker_completion_unproven",
            (
                f"no terminal launcher observation for {attempt_id} after the coordinator exited "
                f"at monotonic {driver_exit_at:.3f}"
            ),
            "The trial cannot claim downtime completion without a terminal attempt record observed after the exit.",
        )
    first = min(terminal, key=lambda item: item.observed_at)
    if not first.succeeded:
        raise TrialIncomplete(
            "worker_completion_failed",
            f"attempt {attempt_id} ended as {first.state!r}, not a successful completion",
            "A failed or cancelled attempt is not downtime completion.",
        )
    if restart_started_at is not None and first.observed_at >= restart_started_at:
        raise TrialIncomplete(
            "worker_completion_after_restart",
            (
                f"attempt {attempt_id} was first observed {first.state!r} at "
                f"{first.observed_at:.3f}, after run 2 started at {restart_started_at:.3f}"
            ),
            "Poll the launcher during the downtime; completion after the restart does not prove downtime completion.",
        )
    return {
        "attempt_id": attempt_id,
        "state": first.state,
        "observed_at_mono": first.observed_at,
        "driver_exit_at_mono": driver_exit_at,
        "downtime_s": round(first.observed_at - driver_exit_at, 6),
        "observations": [item.to_dict() for item in observations],
    }


# ---------------------------------------------------------------------------
# Durable readiness signals (no foreign ready-file writer needed)
# ---------------------------------------------------------------------------


def job_readiness(state_dir: Path | str, job_id: str, mode: str) -> bool:
    """Whether the durable job records the state the trial is waiting for.

    ``dispatch`` means a task has a recorded attempt (or is already past
    ``dispatching``), i.e. the coordinator's POST completed. ``worker-running``
    means a task row is ``running``/``verifying``. Neither reads a file count or
    the coordinator's stdout.
    """

    if mode not in {"dispatch", "worker-running"}:
        raise TrialRefused(
            "readiness_mode_invalid",
            f"unknown readiness mode {mode!r}",
            f"Use one of {list(READY_ON_CHOICES)}.",
        )
    try:
        job = jobs.load_job(Path(state_dir), str(job_id), recover=False)
    except jobs.JobError as exc:
        raise TrialIncomplete(
            "job_unreadable",
            f"the job {job_id!r} cannot be read for readiness: {exc.code}: {exc}",
            "Inspect the job document; readiness cannot be inferred from a damaged record.",
        ) from exc
    tasks = [task for task in job.get("tasks") or [] if isinstance(task, Mapping)]
    if mode == "dispatch":
        return any(
            bool(task.get("attempts")) or str(task.get("state") or "") in DISPATCHED_TASK_STATES
            for task in tasks
        )
    return any(str(task.get("state") or "") in RUNNING_TASK_STATES for task in tasks)


def campaign_job_complete(state_dir: Path | str, campaign_id: str) -> bool:
    """Whether the campaign checkpoint says ``job_complete`` (read-only plan)."""

    if not campaign_id:
        raise TrialRefused(
            "campaign_required",
            "--restart-on job-complete needs a campaign checkpoint",
            "Pass --campaign with the campaign id this job belongs to.",
        )
    try:
        plan = campaign_mod.resume_plan(Path(state_dir), str(campaign_id))
    except campaign_mod.CampaignError as exc:
        raise TrialIncomplete(
            "campaign_unreadable",
            f"the campaign {campaign_id!r} cannot be read for restart readiness: {exc.code}: {exc}",
            "Inspect the campaign checkpoint; restart readiness is never inferred.",
        ) from exc
    return str(plan.get("phase") or "") == "job_complete"


def readiness_callable(
    state_dir: Path | str,
    job_id: str,
    *,
    mode: str,
    path: Path | None = None,
) -> Callable[[], bool]:
    """Build the run-1 readiness predicate for a CLI mode."""

    if mode == "file":
        if path is None:
            raise TrialRefused(
                "ready_file_missing",
                "--ready-on file needs --ready-file",
                "Pass the file the coordinator run writes when ready, or use --ready-on dispatch.",
            )
        target = Path(path)
        return lambda: target.exists()
    if mode == "dispatch":
        return lambda: job_readiness(state_dir, job_id, "dispatch")
    if mode == "worker-running":
        return lambda: job_readiness(state_dir, job_id, "worker-running")
    raise TrialRefused(
        "readiness_mode_invalid",
        f"unknown readiness mode {mode!r}",
        f"Use one of {list(READY_ON_CHOICES)}.",
    )


def restart_callable(
    state_dir: Path | str,
    job_id: str,
    campaign_id: str,
    *,
    mode: str,
    path: Path | None = None,
) -> Callable[[], bool]:
    """Build the run-2 completion predicate for a CLI mode."""

    if mode == "file":
        if path is None:
            raise TrialRefused(
                "ready_file_missing",
                "--restart-on file needs --restart-ready-file",
                "Pass the file run 2 writes when it has finished, or use --restart-on job-complete.",
            )
        target = Path(path)
        return lambda: target.exists()
    if mode == "job-complete":
        return lambda: campaign_job_complete(state_dir, campaign_id)
    raise TrialRefused(
        "readiness_mode_invalid",
        f"unknown restart mode {mode!r}",
        f"Use one of {list(RESTART_ON_CHOICES)}.",
    )


def code_rev_of(root: Path | str) -> str:
    """The checkout's git HEAD, read-only; ``""`` when it is not a git checkout."""

    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": os.environ.get("HOME", "/tmp"),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    value = (done.stdout or "").strip()
    return value if _is_sha(value) else ""


def _is_sha(value: str) -> bool:
    return len(value) in (40, 64) and all(ch in "0123456789abcdefABCDEF" for ch in value)


# ---------------------------------------------------------------------------
# Rehearsal: scripted worker observations
# ---------------------------------------------------------------------------


class ScriptedWorkerProbe:
    """Rehearsal worker observations. It never contacts a launcher.

    The scripted worker is running before the stop and reports success only
    after ``note_driver_exited`` is called, so the harness exercises the same
    proof path as a real launcher observation.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        attempt_id: str,
        completion_polls: int = 1,
        on_complete: Callable[[], None] | None = None,
        completion_state: str = "succeeded",
    ) -> None:
        self.clock = clock
        self.attempt_id = attempt_id
        self.completion_polls = max(0, int(completion_polls))
        self.on_complete = on_complete
        self.completion_state = completion_state
        self.downtime = False
        self.polls = 0
        self.downtime_polls = 0
        self.completed = False

    def note_driver_exited(self) -> None:
        self.downtime = True

    def observe(self) -> list[WorkerObservation]:
        now = self.clock.monotonic()
        if not self.downtime:
            self.polls += 1
            return [WorkerObservation(self.attempt_id, "running", now, source="rehearsal")]
        self.downtime_polls += 1
        if self.completed:
            return [WorkerObservation(self.attempt_id, self.completion_state, now, source="rehearsal")]
        if self.downtime_polls >= self.completion_polls:
            self.completed = True
            if self.on_complete is not None:
                self.on_complete()
            return [WorkerObservation(self.attempt_id, self.completion_state, now, source="rehearsal")]
        return [WorkerObservation(self.attempt_id, "running", now, source="rehearsal")]


# ---------------------------------------------------------------------------
# The trial
# ---------------------------------------------------------------------------


@dataclass
class TrialPlan:
    state_dir: Path
    job_id: str
    checkout: Checkout
    driver_argv: tuple[str, ...]
    deadline_s: float = 900.0
    restart_argv: tuple[str, ...] = ()
    campaign_id: str = ""
    rehearsal: bool = True
    live_confirmed: bool = False
    approval_receipt: Mapping[str, Any] | None = None
    trial_id: str = ""
    driver_env: Mapping[str, str] = field(default_factory=dict)
    #: Environment values that must never appear in a report: only their names
    #: are recorded. A token belongs here, never in ``driver_env`` or argv.
    driver_secret_env: Mapping[str, str] = field(default_factory=dict)
    ready_path: Path | None = None
    restart_ready_path: Path | None = None
    term_grace_s: float = 5.0
    kill_grace_s: float = 2.0
    poll_interval_s: float = 0.25
    observation_staleness_s: float = 60.0
    restart_min_remaining_s: float = 1.0
    report_path: Path | None = None
    launcher: Any = None
    #: An optional attempt identity that the worker observation must match.
    #: When the job already records one attempt, the preflight derives it.
    expected_attempt_id: str = ""
    #: The exact code revision the trial exercises. Empty means "read the
    #: checkout's git HEAD"; a non-empty value must match that HEAD.
    code_rev: str = ""
    #: The explicit smoke/integration suite command. It is never defaulted: this
    #: host's system python has no pytest and its venv cannot run in the sandbox,
    #: so the caller records the exact direct-test-runner argv (see
    #: ``direct_test_runner_command``).
    suite_command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.driver_argv = tuple(str(item) for item in self.driver_argv)
        self.restart_argv = tuple(str(item) for item in self.restart_argv) or self.driver_argv
        self.suite_command = tuple(str(item) for item in self.suite_command)
        self.code_rev = str(self.code_rev or "")
        if self.code_rev and not _is_sha(self.code_rev):
            raise TrialRefused(
                "code_rev_invalid",
                f"code_rev must be a hex commit sha, got {self.code_rev!r}",
                "Pass --code-rev with the full commit sha the trial exercises.",
            )
        if not self.trial_id:
            self.trial_id = "trial-" + uuid.uuid4().hex[:12]
        if self.ready_path is None:
            self.ready_path = self.state_dir / "trials" / self.trial_id / "driver-1.ready"
        else:
            self.ready_path = Path(self.ready_path)
        if self.restart_ready_path is None:
            # Run 2 must never inherit run 1's ready file: it would look
            # "finished" the instant it started.
            self.restart_ready_path = Path(self.ready_path).with_name("driver-2.ready")
        else:
            self.restart_ready_path = Path(self.restart_ready_path)
        if self.report_path is None:
            self.report_path = self.state_dir / "trials" / self.trial_id / "report.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RestartTrial:
    """One disposable restart trial. Nothing here starts a launcher or worker."""

    def __init__(
        self,
        plan: TrialPlan,
        *,
        clock: Clock | None = None,
        worker_probe: WorkerProbe | None = None,
        approval: ApprovalAdapter | None = None,
        readiness: Callable[[], bool] | None = None,
        restart_ready: Callable[[], bool] | None = None,
        run: Callable[..., subprocess.CompletedProcess] | None = None,
        probe_result: Mapping[str, Any] | None = None,
    ) -> None:
        self.plan = plan
        self.clock = clock if clock is not None else SystemClock()
        self.worker_probe = worker_probe
        self.approval = resolve_approval_adapter(approval)
        self.readiness = readiness or self._file_ready
        self.restart_ready = restart_ready or self._restart_file_ready
        self._probe_runner = run
        self._probe_result = probe_result
        self.deadline: Deadline | None = None
        self.code_rev: str = ""
        self.report: dict[str, Any] = {}
        self._timeline: list[dict[str, Any]] = []
        self._drivers: list[DriverProcess] = []
        self._driver_reports: list[dict[str, Any]] = []
        self._stops: list[StopReport] = []
        self._observations: list[WorkerObservation] = []
        self._limitations: list[str] = []
        self.expected_attempt_id: str = str(plan.expected_attempt_id or "").strip()

    # -- report scaffolding -------------------------------------------------

    def _step(self, name: str, **detail: Any) -> None:
        offset = 0.0 if self.deadline is None else round(self.deadline.elapsed(), 6)
        self._timeline.append({"step": name, "at_s": offset, "detail": detail})

    def _base_report(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "trial_id": self.plan.trial_id,
            "rehearsal": bool(self.plan.rehearsal),
            "job_id": self.plan.job_id,
            "campaign_id": self.plan.campaign_id,
            "state_dir": str(self.plan.state_dir),
            "source_root": str(self.plan.checkout.root),
            "started_at": _now_iso(),
            "finished_at": "",
            "verdict": "running",
            "code": "",
            "message": "",
            "timeouts": json.loads(json.dumps(TIMEOUT_SEMANTICS)),
            "contexts": self.plan.checkout.contexts.to_dict(),
            "deadline": {},
            "timeline": self._timeline,
            "drivers": self._driver_reports,
            "worker": {},
            "evidence": {},
            "campaign": None,
            "owner_acceptance": {},
            "limitations": self._limitations,
        }

    def _write_report(self) -> dict[str, Any]:
        report = self.report
        report["finished_at"] = _now_iso()
        report["timeline"] = self._timeline
        report["drivers"] = self._driver_reports
        report["limitations"] = list(dict.fromkeys(self._limitations))
        report["deadline"] = self.deadline.to_dict() if self.deadline is not None else {}
        if self.plan.report_path is not None:
            path = Path(self.plan.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        return report

    def _fail(self, exc: TrialError) -> None:
        self.report["verdict"] = "incomplete" if isinstance(exc, TrialIncomplete) else "refused"
        self.report["code"] = exc.code
        self.report["message"] = exc.message
        if exc.fix:
            self.report["fix"] = exc.fix
        if isinstance(exc, TrialIncomplete):
            self._limitations.append(f"{exc.code}: {exc.message}")

    # -- preflight ----------------------------------------------------------

    def _file_ready(self) -> bool:
        return bool(self.plan.ready_path and Path(self.plan.ready_path).exists())

    def _restart_file_ready(self) -> bool:
        return bool(
            self.plan.restart_ready_path and Path(self.plan.restart_ready_path).exists()
        )

    def _preflight(self) -> dict[str, Any]:
        state = Path(self.plan.state_dir)
        if not state.is_dir():
            raise TrialRefused(
                "state_missing",
                f"the trial state directory does not exist: {state}",
                "Create or pass the coordinator state directory.",
            )
        # A trial is disposable. Reusing its ready/report artefacts can make a
        # later run look successful before either driver has done anything.
        for label, path in (
            ("report", self.plan.report_path),
            ("ready", self.plan.ready_path),
            ("restart_ready", self.plan.restart_ready_path),
        ):
            if path is not None and Path(path).exists():
                raise TrialRefused(
                    "trial_artifacts_already_exist",
                    f"the {label} path already exists for this trial: {path}",
                    "Use a fresh trial_id/report path; never reuse a prior trial's evidence.",
                )
        try:
            job = jobs.load_job(state, self.plan.job_id, recover=False)
        except jobs.JobError as exc:
            raise TrialRefused(
                "job_unreadable",
                f"the trial job {self.plan.job_id!r} cannot be loaded: {exc}",
                "Inspect the job document and its authorization before running a trial.",
            ) from exc
        tasks = [task for task in job.get("tasks") or [] if isinstance(task, Mapping)]
        if len(tasks) != 1:
            raise TrialRefused(
                "trial_requires_single_task",
                (
                    f"the trial job has {len(tasks)} tasks; the exactly-once check is defined for "
                    "one task, one attempt and one accepted wave"
                ),
                "Run the restart trial against a single-task job, or extend the evidence model first.",
            )
        recorded_attempts = sorted(
            {
                str(attempt)
                for task in tasks
                for attempt in (task.get("attempts") or [])
                if str(attempt or "").strip()
            }
        )
        if len(recorded_attempts) > 1:
            raise TrialRefused(
                "trial_requires_single_attempt",
                f"the trial job already records attempts {recorded_attempts!r}",
                "Use a job with at most one recorded attempt; this harness proves one dispatch.",
            )
        if recorded_attempts:
            if self.expected_attempt_id and self.expected_attempt_id != recorded_attempts[0]:
                raise TrialRefused(
                    "expected_attempt_mismatch",
                    (
                        f"the plan expects attempt {self.expected_attempt_id!r}, but the job records "
                        f"{recorded_attempts[0]!r}"
                    ),
                    "Correct the plan; the harness will not accept a different worker's evidence.",
                )
            self.expected_attempt_id = recorded_attempts[0]
        if self.plan.campaign_id:
            try:
                campaign_mod.load_campaign(state, self.plan.campaign_id)
            except campaign_mod.CampaignError as exc:
                raise TrialRefused(
                    "campaign_unreadable",
                    f"the campaign {self.plan.campaign_id!r} cannot be loaded: {exc}",
                    "Inspect the campaign checkpoint before running a trial.",
                ) from exc
        if not self.plan.rehearsal and not self.plan.live_confirmed:
            raise TrialRefused(
                "live_trial_not_confirmed",
                "a live restart trial needs an explicit confirmation",
                "Pass live_confirmed=True (CLI: --live-confirm) only after the owner approved this trial.",
            )
        self.plan.checkout.contexts.validate()
        verify = self.plan.checkout.verify(
            run=self._probe_runner, probe_result=self._probe_result
        )
        self._step("checkout_verified", package_file=verify["package_file"], interpreter=verify["interpreter"])
        detected_rev = code_rev_of(verify["source_root"])
        if self.plan.code_rev and detected_rev and self.plan.code_rev != detected_rev:
            raise TrialRefused(
                "code_rev_mismatch",
                f"the trial was approved for code_rev {self.plan.code_rev}, but the checkout is at {detected_rev}",
                "Check out the approved revision, or obtain an approval for this one.",
            )
        self.code_rev = self.plan.code_rev or detected_rev
        self.report["code_rev"] = {
            "value": self.code_rev,
            "source": "configured" if self.plan.code_rev else ("git-head" if detected_rev else "unavailable"),
        }
        if not self.code_rev:
            if self.plan.rehearsal:
                self._limitations.append(
                    "code_rev_unavailable: the checkout has no readable git HEAD; the trial is not bound to a code revision"
                )
            else:
                raise TrialRefused(
                    "code_rev_unverifiable",
                    "the live checkout cannot name its git HEAD, so the trial cannot bind to a code revision",
                    "Run from the pinned Bundlebees checkout or pass an exact --code-rev.",
                )
        self._step("code_rev_recorded", code_rev=self.code_rev)
        argv = validate_driver_argv(
            self.plan.driver_argv,
            allowed_modules=self._driver_modules(),
            rehearsal=self.plan.rehearsal,
        )
        restart_argv = validate_driver_argv(
            self.plan.restart_argv,
            allowed_modules=self._driver_modules(),
            rehearsal=self.plan.rehearsal,
        )
        suite: dict[str, Any] | None = None
        if not self.plan.rehearsal:
            suite = self.plan.checkout.assert_suite_command(argv, self.plan.suite_command)
            restart_suite = self.plan.checkout.assert_suite_command(restart_argv, self.plan.suite_command)
            if restart_suite["suite_command"] != suite["suite_command"]:
                raise TrialRefused(
                    "suite_command_mismatch",
                    "the two coordinator runs would use different suite commands",
                    "Use the same --suite-command for both runs.",
                )
        read_only = None
        if self.plan.launcher is not None:
            read_only = ReadOnlyLauncher(self.plan.launcher)
            health = read_only.health()
            self._step("launcher_read_only_preflight", health_keys=sorted(health))
        approval: dict[str, Any] | None = None
        if self.approval is not None:
            if self.plan.approval_receipt is None and not self.plan.rehearsal:
                # A live trial must bind a receipt. A rehearsal dispatches
                # nothing real (the rehearsal gate pins the driver to the
                # simulated one), so a bound lane without a receipt is
                # recorded as not-required in the report instead of refused;
                # when a rehearsal DOES carry a receipt it is still verified
                # and consumed exactly once below.
                raise TrialRefused(
                    "approval_receipt_missing",
                    "an approval adapter is bound but no receipt was supplied",
                    "Pass the approval lane's receipt for this trial.",
                )
            receipt = (
                dict(self.plan.approval_receipt)
                if self.plan.approval_receipt is not None
                else None
            )
            if receipt is not None:
                receipt_rev = str(receipt.get("code_rev") or "")
                if receipt_rev and self.code_rev and receipt_rev != self.code_rev:
                    raise TrialRefused(
                        "approval_receipt_code_rev_mismatch",
                        f"the receipt is bound to code_rev {receipt_rev}, but the trial runs {self.code_rev}",
                        "Obtain a receipt for the revision being exercised; the harness never widens a receipt.",
                    )
                verified = _verified_receipt(self.approval.verify_receipt(receipt))
                verified_rev = str(verified.get("code_rev") or "")
                if verified_rev and self.code_rev and verified_rev != self.code_rev:
                    raise TrialRefused(
                        "approval_receipt_code_rev_mismatch",
                        f"the approval lane verified code_rev {verified_rev}, but the trial runs {self.code_rev}",
                        "Do not weaken the receipt binding; obtain a receipt for this revision.",
                    )
                consumed = self.approval.consume_receipt(dict(self.plan.approval_receipt))
                if not isinstance(consumed, Mapping) or consumed.get("consumed") is not True:
                    raise TrialRefused(
                        "approval_receipt_not_consumed",
                        (
                            "the approval lane did not return an explicit consumed=true receipt; "
                            f"got {type(consumed).__name__}"
                        ),
                        "A receipt must be consumed exactly once before any driver starts.",
                    )
                approval = {
                    "verified": True,
                    "consumed": True,
                    "receipt_id": str(
                        verified.get("receipt_id") or verified.get("request_id") or verified.get("id") or ""
                    ),
                    "code_rev": receipt_rev or verified_rev or "",
                    "basis": sorted(str(key) for key in verified),
                }
                self._step("approval_receipt_consumed", receipt_id=approval["receipt_id"])
        elif not self.plan.rehearsal:
            raise TrialRefused(
                "approval_unavailable",
                (
                    "bundlebees_plugin.approval does not expose verify_receipt/consume_receipt, "
                    "so a live trial has no receipt to bind to"
                ),
                "Land or inject the approval lane's receipt interface; this harness does not implement approval.",
            )
        self.report["approval"] = approval or {
            "verified": False,
            "consumed": False,
            "required": False,
            "basis": "local rehearsal dispatches nothing and needs no approval receipt",
        }
        self.report["driver_argv"] = list(argv)
        self.report["restart_argv"] = list(restart_argv)
        self.report["suite_command"] = suite
        self.report["child_env_names"] = sorted(self.plan.checkout.child_env(extra=self.plan.driver_env))
        return verify

    # -- process lifecycle --------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        return self.plan.checkout.child_env(extra=self.plan.driver_env)

    def _require_deadline(self) -> Deadline:
        if self.deadline is None:  # pragma: no cover - run() starts it before any wait
            raise TrialRefused(
                "deadline_missing",
                "no deadline was started for this trial",
                "Start the trial through RestartTrial.run().",
            )
        return self.deadline

    def _driver_modules(self) -> tuple[str, ...]:
        if self.plan.rehearsal:
            return (SIMULATED_DRIVER_MODULE,)
        return (FACTORY_DRIVER_MODULE,)

    def _start_run(self, run: int) -> DriverProcess:
        argv = self.plan.driver_argv if run == 1 else self.plan.restart_argv
        trial_dir = Path(self.plan.report_path).parent if self.plan.report_path else Path(self.plan.state_dir)
        log_path = trial_dir / f"driver-{run}.log"
        record = start_driver(
            argv,
            run=run,
            cwd=self.plan.checkout.resolve(),
            env=self._child_env(),
            log_path=log_path,
            clock=self.clock,
            allowed_modules=self._driver_modules(),
            rehearsal=self.plan.rehearsal,
        )
        self._drivers.append(record)
        self._driver_reports.append(record.to_dict())
        self._step(
            f"driver_{run}_started",
            pid=record.pid,
            argv=list(record.argv),
            started_at_mono=record.started_at,
        )
        return record

    def _stop_run(self, record: DriverProcess) -> StopReport:
        stop = stop_driver(
            record,
            deadline=self._require_deadline(),
            clock=self.clock,
            term_grace_s=self.plan.term_grace_s,
            kill_grace_s=self.plan.kill_grace_s,
        )
        self._stops.append(stop)
        self._step(f"driver_{record.run}_stopped", **stop.to_dict())
        if not stop.shutdown_confirmed or stop.returncode is None:
            raise TrialIncomplete(
                "coordinator_shutdown_unconfirmed",
                (
                    f"driver run {record.run} (pid {record.pid}) did not confirm shutdown: "
                    f"{stop.code}, returncode={stop.returncode!r}"
                ),
                "Inspect the driver log; the harness will not continue with a coordinator it cannot account for.",
            )
        return stop

    def _cleanup_leftover_drivers(self) -> None:
        """Stop any coordinator child that a failed wait left alive.

        The normal path already stops run 1 and run 2. This is the failure
        path: a deadline or readiness refusal must not leave a driver running
        after the harness itself exits. Only PIDs this harness recorded are
        signalled; workers are never touched.
        """
        if not self._drivers:
            return
        deadline = self.deadline
        if deadline is None:
            deadline = Deadline.start(
                max(1.0, float(self.plan.term_grace_s) + float(self.plan.kill_grace_s) + 1.0),
                clock=self.clock,
            )
        for record in self._drivers:
            if record.returncode is not None:
                continue
            try:
                stop = stop_driver(
                    record,
                    deadline=deadline,
                    clock=self.clock,
                    term_grace_s=self.plan.term_grace_s,
                    kill_grace_s=self.plan.kill_grace_s,
                )
                self._stops.append(stop)
                self._step(f"driver_{record.run}_cleanup", **stop.to_dict())
                if not stop.shutdown_confirmed or stop.returncode is None:
                    self._limitations.append(
                        f"driver_{record.run}_cleanup_unconfirmed: pid {record.pid} code={stop.code}"
                    )
            except Exception as exc:  # pragma: no cover - best-effort failure path
                self._limitations.append(
                    f"driver_{record.run}_cleanup_failed: {type(exc).__name__}: {exc}"
                )
            finally:
                record.close_log()

    # -- waits --------------------------------------------------------------

    def _wait_for(self, predicate: Callable[[], Any], *, code: str, what: str) -> Any:
        interval = max(0.0, float(self.plan.poll_interval_s))
        while True:
            value = predicate()
            if value:
                return value
            deadline = self._require_deadline()
            remaining = deadline.remaining()
            if remaining <= 0:
                raise TrialIncomplete(
                    code,
                    (
                        f"{what} did not happen before the one trial deadline "
                        f"({deadline.total_s:.3f}s; {deadline.remaining():.3f}s left)"
                    ),
                    "A restart trial gets one deadline; start a new trial with a new authorization if more time is needed.",
                )
            self.clock.sleep(min(interval, remaining))

    def _observe_worker(self) -> list[WorkerObservation]:
        if self.worker_probe is None:
            raise TrialRefused(
                "worker_probe_missing",
                "no worker probe was supplied; the trial cannot observe the worker",
                "Inject a launcher attempt probe (real) or the scripted rehearsal probe.",
            )
        observations = [item for item in self.worker_probe.observe()]
        if self.expected_attempt_id:
            wrong = [item for item in observations if item.attempt_id != self.expected_attempt_id]
            if wrong:
                raise TrialIncomplete(
                    "worker_attempt_mismatch",
                    (
                        f"the trial expects attempt {self.expected_attempt_id!r}, but the probe observed "
                        f"{sorted({item.attempt_id for item in wrong})!r}"
                    ),
                    "Use the attempt this job actually dispatched; never accept another job's evidence.",
                )
        self._observations.extend(observations)
        self._step(
            "worker_observed",
            attempt_ids=sorted({item.attempt_id for item in observations}),
            states=sorted({item.state for item in observations}),
        )
        return observations

    # -- campaign / acceptance ---------------------------------------------

    def _campaign_state(self) -> dict[str, Any]:
        if not self.plan.campaign_id:
            self._limitations.append("campaign_not_configured: no campaign checkpoint was named")
            return {}
        # resume_plan reconciles against the durable job; one read of its result
        # is enough and avoids a second checkpoint write for the same view.
        plan = campaign_mod.resume_plan(Path(self.plan.state_dir), self.plan.campaign_id)
        reconciled = campaign_mod.load_campaign(Path(self.plan.state_dir), self.plan.campaign_id)
        self._step("campaign_reconciled", phase=reconciled.get("phase"), revision=reconciled.get("revision"))
        if plan.get("phase") != "job_complete":
            raise TrialIncomplete(
                "campaign_not_complete",
                f"the campaign is {plan.get('phase')!r}, not 'job_complete' after run 2",
                "Inspect the campaign checkpoint, the job evidence and the driver log.",
            )
        if plan.get("should_continue") is not False or plan.get("requires_human") is not False:
            raise TrialRefused(
                "campaign_still_actionable",
                f"resume_plan still asks for work: {plan!r}"[:500],
                "A restart trial ends at job_complete with no automatic continuation.",
            )
        if plan.get("requires_campaign_closeout") is not True:
            raise TrialRefused(
                "campaign_closeout_not_pending",
                "job_complete did not report a pending campaign closeout",
                "Inspect the campaign layer; job acceptance must not claim campaign acceptance.",
            )
        return {"reconcile": reconciled, "resume_plan": plan}

    def _owner_acceptance(self) -> dict[str, Any]:
        """Owner acceptance is the receiver's decision. The harness only reads it."""

        manifest = None
        try:
            manifest = return_report.read_manifest(Path(self.plan.state_dir), self.plan.job_id)
        except Exception as exc:  # noqa: BLE001 - a damaged report is not an acceptance
            raise TrialIncomplete(
                "owner_acceptance_unreadable",
                f"the RETURN manifest could not be read: {type(exc).__name__}: {exc}",
                "Inspect the job's return.json; the harness will not infer a decision.",
            ) from exc
        if manifest is None:
            record = {
                "status": "pending",
                "basis": "no RETURN manifest exists; bundlebees records producer evidence, never the receiver's decision",
            }
        else:
            decision = manifest.get("receiver_decision") if isinstance(manifest.get("receiver_decision"), Mapping) else {}
            status = str(decision.get("status") or "pending")
            record = {
                "status": status,
                "basis": "RETURN manifest receiver_decision",
                "decided_by": decision.get("decided_by"),
            }
        if str(record["status"]) != "pending":
            raise TrialIncomplete(
                "owner_acceptance_not_pending",
                f"the receiver decision is {record['status']!r}, not 'pending'",
                "A trial ends before owner acceptance; do not claim it as pending when a decision exists.",
            )
        self._step("owner_acceptance_pending", basis=record["basis"])
        return record

    # -- main ---------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.report = self._base_report()
        try:
            self._preflight()
            self.deadline = Deadline.start(self.plan.deadline_s, clock=self.clock)
            self._step("deadline_started", total_s=self.deadline.total_s)
            self.report["deadline"] = self.deadline.to_dict()

            first = self._start_run(1)
            self._wait_for(self.readiness, code="deadline_exhausted_before_ready", what="run 1 readiness")
            self._step("driver_1_ready", pid=first.pid)

            before = self._observe_worker()
            running = running_attempt(before)
            stop = self._stop_run(first)
            running_proof = prove_running_at_stop(
                pre_stop=running,
                stop_at=stop.stop_requested_at,
                observations=self._observations,
                staleness_s=float(self.plan.observation_staleness_s),
            )
            self._step("worker_running_at_stop", **running_proof)

            if hasattr(self.worker_probe, "note_driver_exited"):
                self.worker_probe.note_driver_exited()  # type: ignore[attr-defined]

            self._wait_for(
                lambda: self._terminal_observation(running.attempt_id),
                code="deadline_exhausted_during_downtime",
                what="worker completion during coordinator downtime",
            )
            downtime_proof = prove_downtime_completion(
                attempt_id=running.attempt_id,
                driver_exit_at=stop.exited_at if stop.exited_at is not None else stop.stop_requested_at,
                observations=self._observations,
                restart_started_at=None,
            )
            self._step("worker_completed_during_downtime", **downtime_proof)

            left = self._require_deadline().require(
                "deadline_exhausted_before_restart",
                minimum=max(0.0, float(self.plan.restart_min_remaining_s)),
            )
            self._step("restart_allowed", remaining_s=round(left, 6))
            second = self._start_run(2)

            def restart_done() -> str | None:
                if second.returncode is not None:
                    return "exited"
                if self.restart_ready():
                    return "ready"
                return None

            self._wait_for(restart_done, code="deadline_exhausted_before_restart_ready",
                           what="run 2 completion")
            stopped_here = False
            if second.returncode is None:
                # A resident run 2 declares completion through the readiness
                # signal; stopping it is the harness ending its own coordinator,
                # never a worker cancellation.
                self._stop_run(second)
                stopped_here = True
            else:
                self._step("driver_2_exited", pid=second.pid, returncode=second.returncode)
            expected_stop = stopped_here and second.returncode == -int(signal.SIGTERM)
            if second.returncode not in (None, 0) and not expected_stop:
                raise TrialIncomplete(
                    "driver_2_failed",
                    f"coordinator run 2 exited with {second.returncode}",
                    "Inspect the driver log and the durable job state.",
                )
            downtime_proof["restart_started_at_mono"] = second.started_at
            if downtime_proof["observed_at_mono"] >= second.started_at:
                raise TrialIncomplete(
                    "worker_completion_after_restart",
                    (
                        f"the worker was first seen {downtime_proof['state']!r} at "
                        f"{downtime_proof['observed_at_mono']:.3f}, after run 2 started at "
                        f"{second.started_at:.3f}"
                    ),
                    "Completion must be observed during the coordinator downtime, not after the restart.",
                )

            evidence = collect_evidence(self.plan.state_dir, self.plan.job_id)
            counts = evidence.assert_exactly_once()
            self._step("evidence_exactly_once", **counts)
            self.report["evidence"] = evidence.to_dict()

            campaign_state = self._campaign_state()
            self.report["campaign"] = {
                "phase": (campaign_state.get("resume_plan") or {}).get("phase"),
                "requires_campaign_closeout": (campaign_state.get("resume_plan") or {}).get(
                    "requires_campaign_closeout"
                ),
                "should_continue": (campaign_state.get("resume_plan") or {}).get("should_continue"),
                "next_action": (campaign_state.get("reconcile") or {}).get("next_action"),
                "revision": (campaign_state.get("reconcile") or {}).get("revision"),
            }
            self.report["owner_acceptance"] = self._owner_acceptance()
            self.report["worker"] = {
                "running_at_stop": running_proof,
                "completed_during_downtime": downtime_proof,
                "attempt_id": running.attempt_id,
            }
            self.report["verdict"] = "pass"
            self.report["code"] = "ok"
            self.report["message"] = (
                "restart adopted the completed attempt; one dispatch, one verification, one accepted wave; "
                "campaign closeout and owner acceptance are still pending"
            )
            self._step("trial_passed")
        except TrialError as exc:
            self._fail(exc)
            self._cleanup_leftover_drivers()
            report = self._write_report()
            exc.report = report
            raise
        finally:
            self._cleanup_leftover_drivers()
            for record in self._drivers:
                record.close_log()
        return self._write_report()

    def _terminal_observation(self, attempt_id: str) -> WorkerObservation | None:
        fresh = [item for item in self.worker_probe.observe()] if self.worker_probe else []
        self._observations.extend(fresh)
        terminal = [
            item for item in self._observations
            if item.attempt_id == attempt_id and item.terminal
        ]
        if not terminal:
            if fresh:
                self._step(
                    "worker_still_running",
                    states=sorted({item.state for item in fresh}),
                )
            return None
        return min(terminal, key=lambda item: item.observed_at)


# ---------------------------------------------------------------------------
# Rehearsal state fixture (offline; no dispatch, no network)
# ---------------------------------------------------------------------------


REHEARSAL_SHA = "b" * 40
REHEARSAL_ATTEMPT = "att-rehearsal-1"


def build_rehearsal_state(
    state_dir: Path,
    *,
    job_id: str = "trial-job",
    campaign_id: str = "trial-campaign",
    base_rev: str = REHEARSAL_SHA,
) -> dict[str, Any]:
    """Create an authorized job and campaign for the rehearsal driver.

    This is a fixture, not an authorization: it calls the real
    ``jobs.record_authorization`` with ``interactive=False`` and a clearly
    named test approver. It dispatches nothing.
    """

    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    jobs.record_authorization(
        state,
        job_id=job_id,
        statement="Local restart rehearsal for the trial harness; no repository is touched.",
        repo="mojomast/trial-rehearsal",
        base_rev=base_rev,
        scope=["src/rehearsal.py"],
        bees=1,
        wall_clock_s=600,
        require_tty=False,
        interactive=False,
        by="trial-rehearsal-fixture",
    )
    jobs.create_job(
        state,
        job_id=job_id,
        summary="restart rehearsal",
        tasks=[
            {
                "task_id": "T1",
                "objective": "produce the rehearsal slice",
                "lease": ["src/rehearsal.py"],
                "acceptance": {
                    "criteria": ["the rehearsal slice exists"],
                    "tests": ["tests/test_rehearsal.py"],
                    "commands": ["pytest tests/test_rehearsal.py"],
                },
            }
        ],
    )
    campaign_mod.create_campaign(
        state,
        campaign_id=campaign_id,
        job_id=job_id,
        objective="Rehearse a coordinator restart without a worker or a launcher",
        branch="feat/trial-rehearsal",
    )
    marker = state / REHEARSAL_MARKER
    marker.write_text("rehearsal-only\n", encoding="utf-8")
    return {"state": state, "job_id": job_id, "campaign_id": campaign_id}


def rehearsal_driver_argv(
    state_dir: Path,
    *,
    trial_dir: Path,
    pass_number: int = 1,
    job_id: str = "trial-job",
    campaign_id: str = "trial-campaign",
    python: str | None = None,
) -> tuple[str, ...]:
    return (
        str(python or sys.executable),
        "-m",
        "contrib.trial.trial_simulated_driver",
        "--state",
        str(Path(state_dir)),
        "--trial-dir",
        str(Path(trial_dir)),
        "--job",
        job_id,
        "--campaign",
        campaign_id,
        "--attempt-id",
        REHEARSAL_ATTEMPT,
        "--pass",
        str(int(pass_number)),
    )


def run_rehearsal(
    *,
    state_dir: Path | str | None = None,
    source_root: Path | str | None = None,
    suite_python: str = "/usr/bin/python3",
    deadline_s: float = 60.0,
    clock: Clock | None = None,
    keep: bool = False,
    approval: ApprovalAdapter | None = None,
    receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the offline rehearsal end to end. Returns the trial report.

    ``suite_python`` is the sandbox suite context, explicitly the system
    interpreter, never the coordinator venv.
    """

    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[2]
    created = state_dir is None
    state = Path(tempfile.mkdtemp(prefix="bundlebees-trial-rehearsal-")) if created else Path(state_dir)
    if (state / jobs.JOBS_DIR / "trial-job.json").exists() or (state / REHEARSAL_MARKER).exists():
        fixture = {"state": state, "job_id": "trial-job", "campaign_id": "trial-campaign"}
    else:
        fixture = build_rehearsal_state(state)
    trial_dir = state / "trials" / "rehearsal"
    trial_dir.mkdir(parents=True, exist_ok=True)
    worker_result = trial_dir / "worker-result.json"

    def complete_worker() -> None:
        # The simulated worker completed while the coordinator was down. Run 2
        # reads this file; its absence is a named refusal, never an inference.
        worker_result.write_text(
            json.dumps(
                {
                    "attempt_id": REHEARSAL_ATTEMPT,
                    "state": "succeeded",
                    "note": "rehearsal-only simulated worker result; no repository was touched",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    contexts = default_contexts(
        rehearsal=True,
        coordinator_python=sys.executable,
        suite_python=suite_python,
    )
    checkout = Checkout(root=root, contexts=contexts)
    plan = TrialPlan(
        state_dir=state,
        job_id=fixture["job_id"],
        campaign_id=fixture["campaign_id"],
        checkout=checkout,
        driver_argv=rehearsal_driver_argv(
            state, trial_dir=trial_dir, pass_number=1,
            job_id=fixture["job_id"], campaign_id=fixture["campaign_id"],
        ),
        restart_argv=rehearsal_driver_argv(
            state, trial_dir=trial_dir, pass_number=2,
            job_id=fixture["job_id"], campaign_id=fixture["campaign_id"],
        ),
        deadline_s=deadline_s,
        rehearsal=True,
        live_confirmed=False,
        trial_id="rehearsal",
        ready_path=trial_dir / "driver-1.ready",
        restart_ready_path=trial_dir / "driver-2.ready",
        report_path=trial_dir / "report.json",
        driver_env={"BUNDLEBEES_TRIAL_REHEARSAL": "1"},
        poll_interval_s=0.01,
        term_grace_s=2.0,
        kill_grace_s=1.0,
        observation_staleness_s=30.0,
        approval_receipt=receipt,
    )
    active_clock = clock if clock is not None else SystemClock()
    probe = ScriptedWorkerProbe(
        clock=active_clock,
        attempt_id=REHEARSAL_ATTEMPT,
        completion_polls=1,
        on_complete=complete_worker,
    )
    trial = RestartTrial(
        plan,
        clock=active_clock,
        worker_probe=probe,
        approval=approval,
    )
    report = trial.run()
    report["rehearsal_dir"] = str(state)
    report["rehearsal_dir_removed"] = False
    if created and not keep:
        shutil.rmtree(state, ignore_errors=True)
        report["rehearsal_dir_removed"] = True
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def prepare_request(
    state_dir: Path | str,
    plan: Mapping[str, Any],
    *,
    source_root: Path | str | None = None,
    code_rev: str | None = None,
    ttl_s: int = 900,
    now: Any = None,
) -> dict[str, Any]:
    """Create the inert owner-approval request for a trial plan.

    This is the only step before the owner card: it writes no job and grants no
    authority. The trusted application records the signed decision; the
    coordinator consumes that receipt only after the owner clicked.
    """

    try:
        from bundlebees_plugin import approval  # noqa: PLC0415 - approval lane may not be installed
    except Exception as exc:  # pragma: no cover - deployment fault
        raise TrialRefused(
            "approval_unavailable",
            f"bundlebees_plugin.approval is unavailable: {exc}",
            "Install the approval bridge before preparing a live trial.",
        ) from exc
    if not isinstance(plan, Mapping):
        raise TrialRefused("plan_invalid", "the trial plan must be a JSON object", "Pass --plan.")
    schema = str(plan.get("schema") or "")
    if schema != "bundlebees.restart-trial-plan/1":
        raise TrialRefused(
            "plan_invalid",
            f"unexpected trial plan schema {schema!r}",
            "Use a trial/restart-trial-*.json plan.",
        )
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[2]
    revision = str(code_rev or plan.get("code_rev") or approval.code_revision(root)).strip().lower()
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise TrialRefused(
            "code_rev_unverifiable",
            "the approval request needs the exact 40-hex Bundlebees revision",
            "Run from the pinned checkout or pass --code-rev.",
        )
    budgets = plan.get("budgets") if isinstance(plan.get("budgets"), Mapping) else {}
    worker = plan.get("worker") if isinstance(plan.get("worker"), Mapping) else {}
    coordinator = plan.get("coordinator") if isinstance(plan.get("coordinator"), Mapping) else {}
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise TrialRefused("plan_invalid", "the plan needs a non-empty tasks list", "Pass --plan.")
    request = approval.create_request(
        Path(state_dir),
        job_id=str(plan.get("job_id") or ""),
        statement=str(plan.get("statement") or ""),
        repo=str(plan.get("repository") or plan.get("repo") or ""),
        base_rev=str(plan.get("base_rev") or ""),
        scope=list(plan.get("scope") or []),
        tasks=tasks,
        bees=int(budgets.get("bees") or 0),
        wall_clock_s=int(budgets.get("wall_clock_s") or 0),
        worker_project=str(worker.get("project") or ""),
        worker_profile=str(worker.get("profile") or ""),
        code_rev=revision,
        coordinator_stop=coordinator.get("stop"),
        restart=coordinator.get("restart"),
        budgets={
            key: budgets[key]
            for key in ("attempts", "accepted_waves", "verifications")
            if key in budgets
        },
        ttl_s=int(ttl_s),
        now=now,
    )
    return {
        "schema": "bundlebees.restart-trial-approval/1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "card_path": f"/api/plugins/bundlebees/approval/{request['request_id']}",
        "code_rev": revision,
        "state_dir": str(Path(state_dir)),
        "request": request,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bundlebees-trial-harness",
        description=(
            "Coordinate a disposable coordinator restart trial, or run the offline rehearsal. "
            "The harness never starts or stops the launcher or a worker."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rehearse = sub.add_parser("rehearse", help="offline rehearsal with a simulated worker and driver")
    rehearse.add_argument("--state", default=None, help="rehearsal state directory (default: a temp dir)")
    rehearse.add_argument("--source-root", default=None, help="pinned bundlebees checkout")
    rehearse.add_argument("--suite-python", default="/usr/bin/python3", help="sandbox suite interpreter (system python)")
    rehearse.add_argument("--deadline-seconds", type=float, default=60.0)
    rehearse.add_argument("--keep", action="store_true", help="keep the rehearsal state directory")
    rehearse.add_argument("--report", default=None, help="also copy the trial report to this path")

    run = sub.add_parser("run", help="coordinate a real restart trial (never starts the launcher)")
    prepare = sub.add_parser("prepare", help="create the inert one-time owner approval request for a plan")
    run.add_argument("--state", required=True)
    run.add_argument("--job", required=True)
    run.add_argument("--campaign", default="")
    run.add_argument("--source-root", required=True)
    run.add_argument("--coordinator-python", default=sys.executable)
    run.add_argument("--suite-python", default="/usr/bin/python3")
    run.add_argument("--deadline-seconds", type=float, required=True)
    run.add_argument("--ready-file", default=None, help="file the first coordinator run creates when ready (--ready-on file)")
    run.add_argument("--restart-ready-file", default=None, help="file run 2 creates when it has finished (--restart-on file)")
    run.add_argument(
        "--ready-on",
        choices=READY_ON_CHOICES,
        default="file",
        help=(
            "when run 1 counts as ready: 'dispatch' (a task has a recorded attempt), "
            "'worker-running' (a task row is running/verifying), or 'file' (default, needs --ready-file)"
        ),
    )
    run.add_argument(
        "--restart-on",
        choices=RESTART_ON_CHOICES,
        default="file",
        help="when run 2 counts as finished: 'job-complete' (campaign resume_plan) or 'file' (default)",
    )
    run.add_argument("--code-rev", default="", help="approved commit sha; defaults to the checkout HEAD")
    run.add_argument("--receipt", default=None, help="approval receipt JSON file")
    run.add_argument("--live-confirm", action="store_true", help="confirm the owner approved this trial")
    run.add_argument("--launcher-url", default="", help="read-only launcher URL for preflight/attempt polling; the token is read from the Hermes secret scope")
    run.add_argument("--driver-argv", required=True, help="shell-free argv for run 1 (JSON list)")
    run.add_argument("--restart-argv", default="", help="shell-free argv for run 2 (JSON list)")
    run.add_argument(
        "--suite-command",
        nargs=argparse.REMAINDER,
        default=[],
        help=(
            "explicit sandbox suite command, last option (for example: "
            "/usr/bin/python3 -c \"import sys; sys.path.insert(0, '.'); import tests.test_smoke as t; t.test_x()\")"
        ),
    )
    run.add_argument("--report", default=None)

    prepare.add_argument("--state", required=True, help="coordinator state directory")
    prepare.add_argument("--plan", required=True, help="trial/restart-trial-*.json")
    prepare.add_argument("--source-root", default=None, help="pinned Bundlebees checkout")
    prepare.add_argument("--code-rev", default="", help="approved 40-hex revision; defaults to checkout HEAD")
    prepare.add_argument("--ttl-seconds", type=int, default=900)
    prepare.add_argument("--report", default=None)

    return parser


def _load_json_arg(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"{what} must be JSON: {exc}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rehearse":
        try:
            report = run_rehearsal(
                state_dir=args.state,
                source_root=args.source_root,
                suite_python=args.suite_python,
                deadline_s=args.deadline_seconds,
                keep=args.keep,
            )
        except TrialError as exc:
            report = exc.report or {}
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
            return 2
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0 if report.get("verdict") == "pass" else 2

    if args.command == "prepare":
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"plan_invalid: cannot read trial plan: {exc}", file=sys.stderr)
            return 2
        try:
            result = prepare_request(
                args.state,
                plan,
                source_root=args.source_root,
                code_rev=args.code_rev or None,
                ttl_s=args.ttl_seconds,
            )
        except (TrialError, OSError, ValueError) as exc:
            code = getattr(exc, "code", "plan_invalid")
            print(f"{code}: {exc}", file=sys.stderr)
            return 2
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0

    if not os.environ.get("BUNDLEBEES_TRIAL_LIVE"):
        print(
            "refusing a live trial: set BUNDLEBEES_TRIAL_LIVE=1 and pass --live-confirm after the owner approved it",
            file=sys.stderr,
        )
        return 2
    driver_argv = _load_json_arg(args.driver_argv, "--driver-argv")
    restart_argv = _load_json_arg(args.restart_argv, "--restart-argv") if args.restart_argv else []
    if not isinstance(driver_argv, list) or not all(isinstance(item, str) for item in driver_argv):
        raise SystemExit("--driver-argv must be a JSON list of strings")
    if restart_argv and (not isinstance(restart_argv, list) or not all(isinstance(item, str) for item in restart_argv)):
        raise SystemExit("--restart-argv must be a JSON list of strings")
    receipt = None
    if args.receipt:
        receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    contexts = default_contexts(
        rehearsal=False,
        coordinator_python=args.coordinator_python,
        suite_python=args.suite_python,
    )
    checkout = Checkout(root=Path(args.source_root), contexts=contexts)
    launcher = None
    if args.launcher_url:
        from bundlebees_plugin.client import LauncherClient, get_launcher_token  # noqa: PLC0415 - only a live trial needs the transport

        token = get_launcher_token()
        if not token:
            raise SystemExit(
                "BUNDLEBEES_LAUNCHER_TOKEN is not configured in the Hermes secret scope; "
                "the trial will not put a credential on the command line"
            )
        launcher = LauncherClient(args.launcher_url, token=token)
    try:
        readiness = readiness_callable(
            args.state, args.job, mode=args.ready_on,
            path=Path(args.ready_file) if args.ready_file else None,
        )
        restart_ready = restart_callable(
            args.state, args.job, args.campaign, mode=args.restart_on,
            path=Path(args.restart_ready_file) if args.restart_ready_file else None,
        )
    except TrialError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    plan = TrialPlan(
        state_dir=Path(args.state),
        job_id=args.job,
        campaign_id=args.campaign,
        checkout=checkout,
        driver_argv=tuple(driver_argv),
        restart_argv=tuple(restart_argv),
        deadline_s=args.deadline_seconds,
        rehearsal=False,
        live_confirmed=bool(args.live_confirm) and os.environ.get("BUNDLEBEES_TRIAL_LIVE") == "1",
        approval_receipt=receipt,
        ready_path=Path(args.ready_file) if args.ready_file else None,
        report_path=Path(args.report) if args.report else None,
        launcher=launcher,
        code_rev=args.code_rev,
        suite_command=tuple(args.suite_command or ()),
    )
    probe = LauncherAttemptProbe(launcher, clock=SystemClock()) if launcher is not None else None
    trial = RestartTrial(
        plan,
        worker_probe=probe,
        readiness=readiness,
        restart_ready=restart_ready,
    )
    try:
        report = trial.run()
    except TrialError as exc:
        report = exc.report or {}
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("verdict") == "pass" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
