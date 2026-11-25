"""One flag source for the factory driver and the Hermes gateway.

The factory driver is a systemd user service and the gateway loads this plugin
into another process. These tests pin the shared file, the one resolution
order, and the single startup line both entry points write.
"""

from __future__ import annotations

import io

import pytest

from bundlebees_plugin import board_stock, factory_driver, flags, memory_recall
from tests.fakes import FakeCtx, load_plugin


def _shared_file(tmp_path, text: str):
    path = tmp_path / "flags.env"
    path.write_text(text, encoding="utf-8")
    return path


def _isolate(monkeypatch, path):
    for name in flags.FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(flags.FLAGS_FILE_ENV, str(path))


def test_missing_file_keeps_every_flag_off(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path / "absent.env")
    assert flags.effective_flags() == {
        flags.FLAG_MEMORY: False,
        flags.FLAG_BOARD_STOCK: False,
        flags.FLAG_ATTACH_FINDINGS: False,
    }


def test_one_shared_file_turns_both_readers_on(tmp_path, monkeypatch):
    path = _shared_file(
        tmp_path,
        "# flags only, never a credential\nMEMORY_ENABLED=1\nBUNDLEBEES_BOARD_STOCK=1\n",
    )
    _isolate(monkeypatch, path)
    assert flags.effective_flags() == {
        flags.FLAG_MEMORY: True,
        flags.FLAG_BOARD_STOCK: True,
        flags.FLAG_ATTACH_FINDINGS: False,
    }
    assert memory_recall.enabled() is True
    assert board_stock.enabled() is True


@pytest.mark.parametrize("value", ["0", "true", "yes", "on", "", "2", "1 1"])
def test_only_exactly_one_is_on(tmp_path, monkeypatch, value):
    _isolate(monkeypatch, _shared_file(tmp_path, f"MEMORY_ENABLED={value}\n"))
    assert flags.enabled(flags.FLAG_MEMORY) is False
    assert memory_recall.enabled() is False


def test_surrounding_whitespace_and_quotes_still_read_one(tmp_path, monkeypatch):
    _isolate(monkeypatch, _shared_file(tmp_path, 'export MEMORY_ENABLED="1"\n'))
    assert flags.enabled(flags.FLAG_MEMORY) is True


def test_process_environment_overrides_the_file(tmp_path, monkeypatch):
    _isolate(monkeypatch, _shared_file(tmp_path, "MEMORY_ENABLED=1\nBUNDLEBEES_BOARD_STOCK=1\n"))
    monkeypatch.setenv(flags.FLAG_MEMORY, "0")
    assert flags.enabled(flags.FLAG_MEMORY) is False
    assert flags.enabled(flags.FLAG_BOARD_STOCK) is True
    assert memory_recall.enabled() is False


def test_an_explicit_mapping_never_reads_the_file(tmp_path):
    path = _shared_file(tmp_path, "MEMORY_ENABLED=1\n")
    assert flags.enabled(flags.FLAG_MEMORY, environ={flags.FLAGS_FILE_ENV: str(path)}) is False
    assert memory_recall.enabled({flags.FLAGS_FILE_ENV: str(path)}) is False
    assert board_stock.enabled({flags.FLAGS_FILE_ENV: str(path)}) is False


def test_unknown_flag_is_refused():
    with pytest.raises(ValueError):
        flags.enabled("MEMORY_ENABLE")


def test_reader_ignores_noise_and_keeps_the_last_value(tmp_path):
    path = _shared_file(
        tmp_path,
        "\n# comment\nnot a setting\n=empty-name\nMEMORY_ENABLED=0\nMEMORY_ENABLED=1\n",
    )
    assert flags.read_flags_file(path) == {"MEMORY_ENABLED": "1"}


def test_startup_line_names_every_effective_flag(tmp_path, monkeypatch):
    path = _shared_file(tmp_path, "BUNDLEBEES_BOARD_STOCK=1\n")
    _isolate(monkeypatch, path)
    assert flags.startup_line() == (
        f"bundlebees flags: MEMORY_ENABLED=0 BUNDLEBEES_BOARD_STOCK=1 ATTACH_FINDINGS=0 "
        f"flags_file={path} (present)"
    )


def test_startup_line_reports_a_missing_file(tmp_path, monkeypatch):
    path = tmp_path / "absent.env"
    _isolate(monkeypatch, path)
    line = flags.startup_line()
    assert f"flags_file={path} (missing)" in line
    assert "MEMORY_ENABLED=0" in line


def test_log_startup_writes_exactly_one_line(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path / "absent.env")
    stream = io.StringIO()
    line = flags.log_startup(stream=stream)
    assert stream.getvalue() == line + "\n"
    assert line.startswith("bundlebees flags: ")


def test_gateway_registration_logs_the_effective_flags(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, _shared_file(tmp_path, "MEMORY_ENABLED=1\n"))
    load_plugin().register(FakeCtx())
    assert "bundlebees flags: MEMORY_ENABLED=1" in capsys.readouterr().err


def test_factory_driver_logs_flags_before_a_startup_refusal(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, _shared_file(tmp_path, "BUNDLEBEES_BOARD_STOCK=1\n"))
    for name in ("BUNDLEBEES_LAUNCHER_URL", "BUNDLEBEES_REPO", "BUNDLEBEES_GITLEAKS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as stop:
        factory_driver.main(["--state", str(tmp_path), "--once", "--require-dispatch-config"])
    err = capsys.readouterr().err
    assert "bundlebees flags: MEMORY_ENABLED=0 BUNDLEBEES_BOARD_STOCK=1 ATTACH_FINDINGS=0" in err
    assert "refusing to start" in str(stop.value)
