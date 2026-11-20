"""Doctor: gateway load path and in-process Hermes version (live 'unknown' readings).

On a git-installed Hermes the doctor reported ``gateway loaded: unknown`` (Hermes keeps only
``settings`` under ``plugins.entries.bundlebees`` and loads enabled plugins from
``<HERMES_HOME>/plugins/<name>``) and ``hermes_api_floor: unknown`` (no ``hermes``
distribution, and ``hermes --version`` took ~38 s against the 15 s subprocess timeout).
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

from bundlebees_plugin import doctor
from tests.test_doctor import _ctx, _merge_defaults

MAIN_SHA = "1" * 40


def _home(tmp_path, version='0.2.0', git=True):
    home = tmp_path / "hermes-home"
    plugin = home / "plugins" / "bundlebees"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(f'name: bundlebees\nversion: "{version}"\n', encoding="utf-8")
    if git:
        (plugin / ".git").mkdir()
        (plugin / ".git" / "HEAD").write_text("1" * 40 + "\n", encoding="utf-8")
    return home, plugin


CONFIG = {"plugins": {"enabled": ["bundlebees", "other"], "disabled": [], "entries": {"bundlebees": {"settings": {"x": 1}}}}}


# --- load path -------------------------------------------------------------------------------

def test_entry_without_a_path_resolves_to_the_hermes_plugin_directory(tmp_path):
    home, plugin = _home(tmp_path)
    status, entry = doctor._gateway_entry(CONFIG, home, proc=tmp_path / "no-proc")
    assert status == "ok"
    assert entry["path"] == str(plugin)
    assert entry["path_source"] == "hermes plugin directory"
    assert entry["settings"] == {"x": 1}
    assert "gateway_started_at" not in entry


def test_explicit_path_still_wins(tmp_path):
    home, _ = _home(tmp_path)
    cfg = json.loads(json.dumps(CONFIG)); cfg["plugins"]["entries"]["bundlebees"]["path"] = "/elsewhere"
    _, entry = doctor._gateway_entry(cfg, home, proc=tmp_path / "no-proc")
    assert entry["path"] == "/elsewhere" and "path_source" not in entry


def test_no_path_is_invented_for_a_disabled_absent_or_manifestless_plugin(tmp_path):
    home, plugin = _home(tmp_path)
    for plugins in ({"enabled": [], "entries": {"bundlebees": {}}},
                    {"enabled": ["bundlebees"], "disabled": ["bundlebees"], "entries": {"bundlebees": {}}},
                    {"entries": {"bundlebees": {}}}):
        _, entry = doctor._gateway_entry({"plugins": plugins}, home, proc=tmp_path / "no-proc")
        assert "path" not in entry, plugins
    (plugin / "plugin.yaml").unlink()
    _, entry = doctor._gateway_entry(CONFIG, home, proc=tmp_path / "no-proc")
    assert "path" not in entry
    assert doctor._gateway_entry({"plugins": {"entries": {}}}, home) == ("ok", None)


# --- gateway start time ----------------------------------------------------------------------

def _fake_proc(tmp_path, pid=4242, ticks=5000, btime=1_700_000_000):
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    rest = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 10
    (proc / str(pid) / "stat").write_text(f"{pid} (hermes gateway) " + " ".join(rest) + "\n", encoding="utf-8")
    (proc / "stat").write_text(f"cpu 1 2 3\nbtime {btime}\n", encoding="utf-8")
    return proc


def _state(home, pid=4242, ticks=5000, state="running"):
    (home / "gateway_state.json").write_text(json.dumps({"pid": pid, "start_time": ticks, "gateway_state": state}), encoding="utf-8")


def test_gateway_start_is_trusted_only_when_the_live_process_matches(tmp_path):
    home, _ = _home(tmp_path)
    proc = _fake_proc(tmp_path)
    _state(home)
    started = doctor._gateway_started_at(home, proc)
    assert started == 1_700_000_000 + 5000 / os.sysconf("SC_CLK_TCK")
    _state(home, ticks=5001)                     # pid reused by another process
    assert doctor._gateway_started_at(home, proc) is None
    _state(home, state="stopped")
    assert doctor._gateway_started_at(home, proc) is None
    _state(home, pid=999)                        # no such process
    assert doctor._gateway_started_at(home, proc) is None
    (home / "gateway_state.json").write_text("{not json", encoding="utf-8")
    assert doctor._gateway_started_at(home, proc) is None


# --- the check end to end --------------------------------------------------------------------

def _fake_git(repo, argv):
    if argv[:2] == ["rev-parse", "HEAD"]:
        return (0, MAIN_SHA + "\n", "")
    if argv[:1] == ["rev-parse"]:
        return (0, MAIN_SHA + "\n", "")
    if argv[:1] == ["show"] and argv[1].endswith(":plugin.yaml"):
        return (0, 'version: "0.2.0"\n', "")
    if argv[:1] == ["show"] and argv[1].endswith(":pyproject.toml"):
        return (0, '[project]\nversion = "0.2.0"\n', "")
    return (1, "", "unexpected probe")


def _gateway_row(tmp_path, config_result):
    base = doctor.default_checkers(repo_roots={"bundlebees": tmp_path}, git_runner=_fake_git,
                                   gateway_config=lambda: config_result)
    checkers = _merge_defaults(base, keep={"bundlebees_gateway_loaded"})
    report = doctor.collect(_ctx(), None, checkers)
    repo = next(r for r in report["repos"] if r["repo"] == "bundlebees")
    return repo["aspects"]["gateway_loaded"]


def test_conventional_path_makes_the_gateway_row_comparable(tmp_path):
    home, _ = _home(tmp_path)
    row = _gateway_row(tmp_path, doctor._gateway_entry(CONFIG, home, proc=tmp_path / "no-proc"))
    assert row["state"] == "healthy", row
    assert row["rev"] == "0.2.0"


def test_a_checkout_moved_after_the_gateway_started_is_not_vouched_for(tmp_path):
    home, plugin = _home(tmp_path)
    proc = _fake_proc(tmp_path)
    _state(home)
    started = doctor._gateway_started_at(home, proc)
    os.utime(plugin / ".git" / "HEAD", (started + 60, started + 60))
    row = _gateway_row(tmp_path, doctor._gateway_entry(CONFIG, home, proc=proc))
    assert row["state"] == "unknown", row
    assert "changed after the gateway started" in row["detail"]
    assert row.get("fix")
    os.utime(plugin / ".git" / "HEAD", (started - 60, started - 60))
    row = _gateway_row(tmp_path, doctor._gateway_entry(CONFIG, home, proc=proc))
    assert row["state"] == "healthy", row


# --- Hermes version --------------------------------------------------------------------------

def test_in_process_version_info_is_used_and_placeholders_are_refused():
    info = lambda value: (lambda: types.SimpleNamespace(base_version=value))
    assert doctor._version_info_hermes_version(info("0.23.0")) == "0.23.0"
    for bad in ("unknown", "0.0.0", "", "v0.23", "0.23.0+4.gabc"):
        assert doctor._version_info_hermes_version(info(bad)) is None, bad

    def boom():
        raise RuntimeError("no stamp")
    assert doctor._version_info_hermes_version(boom) is None


def test_default_version_prefers_in_process_identity_over_the_slow_cli(monkeypatch):
    monkeypatch.setattr(doctor.importlib.metadata, "version", lambda name: (_ for _ in ()).throw(doctor.importlib.metadata.PackageNotFoundError(name)))
    monkeypatch.setattr(doctor, "_version_info_hermes_version", lambda get_info=None: "0.23.0")
    def no_cli(*a, **k):
        raise AssertionError("hermes --version must not run when the in-process identity is available")
    monkeypatch.setattr(doctor.subprocess, "run", no_cli)
    assert doctor._default_hermes_version() == "0.23.0"
