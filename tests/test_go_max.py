"""go-max: admission-controlled slot filling on the owner's request (docs/contracts/go-max.md)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bundlebees_plugin import factory_driver, go_max, grants
from bundlebees_plugin import queue as queue_mod
from bundlebees_plugin import work_scheduler

NOW = 1_800_000_000.0
FILES = ["src/a.py", "tests/test_a.py"]


@pytest.fixture(autouse=True)
def _offline_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)


def cap(**over):
    body = {
        "schema": "hyvv.capacity/1",
        "observed_at": NOW,
        "max_concurrent": 12,
        "active": 2,
        "queued": 0,
        "split_brain": False,
        "active_by_lane": {"kimi": 0, "nous": 1, "go": 1, "other": 0},
        "queued_by_lane": {"kimi": 0, "nous": 0, "go": 0, "other": 0},
        "host": {"mem_total_mb": 65536, "mem_available_mb": 40960, "cpu_count": 24, "cpu_busy_pct": 30.0},
        "docker": {"ok": True, "age_s": 5.0, "ncpu": 20, "mem_total_mb": 49152, "containers_running": 2, "detail": "ok"},
        "rate_limits": {"window_s": 600, "by_lane": {k: {"count": 0, "last_at": None} for k in go_max.LANES}},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return body


def policy(**over):
    return go_max.Policy(**over)


# ---------------------------------------------------------------- mode


def test_mode_is_off_by_default(tmp_path: Path):
    mode = go_max.read_mode(tmp_path, now=NOW)
    assert mode["enabled"] is False
    assert mode["reason"] == "off"


def test_enable_then_disable(tmp_path: Path):
    rec = go_max.enable(tmp_path, by="owner", ttl_s=3600, now=NOW)
    assert rec["schema"] == go_max.MODE_SCHEMA
    assert go_max.read_mode(tmp_path, now=NOW + 10)["enabled"] is True
    go_max.disable(tmp_path, by="owner", now=NOW + 20)
    mode = go_max.read_mode(tmp_path, now=NOW + 30)
    assert mode["enabled"] is False and mode["reason"] == "off"


def test_mode_expires(tmp_path: Path):
    go_max.enable(tmp_path, by="owner", ttl_s=60, now=NOW)
    mode = go_max.read_mode(tmp_path, now=NOW + 61)
    assert mode["enabled"] is False and mode["reason"] == "expired"


def test_ttl_is_capped(tmp_path: Path):
    rec = go_max.enable(tmp_path, by="owner", ttl_s=10 * 24 * 3600, now=NOW)
    assert rec["expires_at"] - NOW == go_max.MAX_TTL_S


def test_unreadable_mode_reads_as_off(tmp_path: Path):
    path = go_max.mode_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    mode = go_max.read_mode(tmp_path, now=NOW)
    assert mode["enabled"] is False and mode["reason"] == "mode_unreadable"


# ---------------------------------------------------------------- policy


def test_policy_defaults():
    p = go_max.Policy()
    assert p.cap == grants.GLOBAL_MAX_BEES == 12
    assert p.per_bee_ram_mb == 4096 and p.per_bee_cpus == 2
    assert p.lane_max["other"] == 0


def test_policy_from_settings_never_lowers_per_bee_resources_or_raises_the_cap():
    p = go_max.Policy.from_settings({"go_max_per_bee_ram_mb": 1024, "go_max_per_bee_cpus": 1, "go_max_cap": 40})
    assert p.per_bee_ram_mb == 4096 and p.per_bee_cpus == 2 and p.cap == 12
    q = go_max.Policy.from_settings({"go_max_per_bee_ram_mb": 6144, "go_max_cap": 6, "go_max_step": 2})
    assert q.per_bee_ram_mb == 6144 and q.cap == 6 and q.step_max == 2


# ---------------------------------------------------------------- lanes and routing


@pytest.mark.parametrize(
    "model,lane",
    [
        ("k3.1-pro", "kimi"),
        ("kimi-for-coding", "kimi"),
        ("nous:hy4-flash", "nous"),
        ("nous:deepseek-v4.1-flash-1017", "nous"),
        ("go:deepseek-v4.1-flash", "go"),
        ("deepseek-v4.1-flash", "go"),
        ("deepseek:deepseek-flash", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_lane_of(model, lane):
    assert go_max.lane_of(model) == lane


def test_route_by_lane():
    complex_brief = {"assigned_files": [f"src/m{i}.py" for i in range(6)], "risk": {"tier": "medium"}}
    assert go_max.route(complex_brief)["lane"] == "kimi"
    low = {"assigned_files": ["docs/a.md"], "risk": {"tier": "low"}}
    assert go_max.route(low)["lane"] == "nous"
    plain = {"assigned_files": ["src/a.py"], "risk": {"tier": "medium"}}
    assert go_max.route(plain)["lane"] == "go"
    pinned = {"assigned_files": ["src/a.py"], "model": "nous:hy4-flash"}
    assert go_max.route(pinned)["model"] == "nous:hy4-flash"


def test_route_refuses_temperature_zero():
    with pytest.raises(go_max.GoMaxError) as exc:
        go_max.route({"assigned_files": ["src/a.py"], "temperature": 0})
    assert exc.value.code == "temperature_zero"


# ---------------------------------------------------------------- admission


def test_admission_healthy_is_bounded_by_the_step():
    adm = go_max.admit(cap(), policy(), now=NOW, dispatched_today=0)
    assert adm["free_slots"] == 4
    assert "ramp_step" in adm["reasons"]
    assert adm["limits"]["shared"] == 10


def test_admission_counts_every_coordinators_bees():
    adm = go_max.admit(cap(active=11), policy(step_max=12), now=NOW, dispatched_today=0)
    assert adm["free_slots"] == 1
    adm = go_max.admit(cap(active=12), policy(step_max=12), now=NOW, dispatched_today=0)
    assert adm["free_slots"] == 0 and "shared_cap_reached" in adm["reasons"]


def test_admission_ram_and_cpu_limits():
    adm = go_max.admit(cap(host={"mem_available_mb": 4096 + 3 * 4096 + 100}), policy(step_max=12), now=NOW, dispatched_today=0)
    assert adm["limits"]["ram"] == 3 and adm["free_slots"] == 3
    adm = go_max.admit(cap(host={"cpu_count": 8}), policy(step_max=12), now=NOW, dispatched_today=0)
    assert adm["limits"]["cpu"] == 2 and adm["free_slots"] == 2
    adm = go_max.admit(cap(docker={"mem_total_mb": 16384}), policy(step_max=12), now=NOW, dispatched_today=0)
    assert adm["limits"]["vm"] == 2 and adm["free_slots"] == 2


@pytest.mark.parametrize(
    "capacity,reason",
    [
        (None, "capacity_unreadable"),
        ({"schema": "something/1"}, "capacity_unreadable"),
        (cap(observed_at=NOW - 3600), "capacity_stale"),
        (cap(docker={"ok": False, "detail": "timeout"}), "docker_unhealthy"),
        (cap(docker={"ok": None, "detail": "not_probed"}), "docker_unknown"),
        (cap(split_brain=True), "launcher_split_brain"),
        (cap(queued=1), "launcher_queue_backlog"),
        (cap(max_concurrent=2), "launcher_full"),
        (cap(host={"mem_available_mb": None}), "host_memory_unknown"),
        (cap(host={"mem_available_mb": 6000}), "ram_pressure"),
        (cap(docker={"mem_total_mb": 8192}), "docker_memory_full"),
        (cap(host={"cpu_busy_pct": None}), "host_cpu_unknown"),
        (cap(host={"cpu_busy_pct": 95.0}), "cpu_pressure"),
        (cap(host={"cpu_count": 4}), "cpu_full"),
    ],
)
def test_admission_fails_closed(capacity, reason):
    adm = go_max.admit(capacity, policy(), now=NOW, dispatched_today=0)
    assert adm["free_slots"] == 0
    assert reason in adm["reasons"]


def test_admission_budget():
    adm = go_max.admit(cap(), policy(daily_attempt_budget=5), now=NOW, dispatched_today=5)
    assert adm["free_slots"] == 0 and "budget_exhausted" in adm["reasons"]
    adm = go_max.admit(cap(), policy(daily_attempt_budget=5), now=NOW, dispatched_today=None)
    assert adm["free_slots"] == 0 and "budget_unreadable" in adm["reasons"]


def test_lane_rooms_and_rate_limits():
    capacity = cap(
        active=9,
        active_by_lane={"kimi": 0, "nous": 8, "go": 1, "other": 0},
        rate_limits={"window_s": 600, "by_lane": {"kimi": {"count": 2, "last_at": NOW - 5},
                                                  "nous": {"count": 0, "last_at": None},
                                                  "go": {"count": 0, "last_at": None},
                                                  "other": {"count": 0, "last_at": None}}},
    )
    adm = go_max.admit(capacity, policy(step_max=12), now=NOW, dispatched_today=0)
    lanes = adm["lanes"]
    assert lanes["nous"]["room"] == 0 and lanes["nous"]["reason"] == "lane_full"
    assert lanes["kimi"]["paused"] is True and lanes["kimi"]["reason"] == "lane_rate_limited"
    assert lanes["go"]["room"] == 11
    assert lanes["other"]["room"] == 0


# ---------------------------------------------------------------- plan


def cand(work_id, lane="go", **over):
    body = {"work_id": work_id, "lane": lane, "model": {"go": "go:deepseek-v4.1-flash", "nous": "nous:deepseek-v4.1-flash-1017", "kimi": "k3.1-pro", "other": "x"}[lane],
            "approved": True, "needs_owner": False}
    body.update(over)
    return body


def test_plan_respects_slots_lanes_and_approval():
    adm = go_max.admit(cap(active_by_lane={"kimi": 3, "nous": 1, "go": 1, "other": 0}, active=5), policy(step_max=4), now=NOW, dispatched_today=0)
    out = go_max.plan(adm, [
        cand("w1", approved=False),
        cand("w2", needs_owner=True),
        cand("w3", "kimi"),
        cand("w4", "kimi"),
        cand("w5", "other"),
        cand("w6", "nous"),
        cand("w7"),
        cand("w8"),
        cand("w9"),
    ])
    assert [a["work_id"] for a in out["admit"]] == ["w3", "w6", "w7", "w8"]
    held = {h["work_id"]: h["reason"] for h in out["held"]}
    assert held["w1"] == "not_approved"
    assert held["w2"] == "needs_owner_yes"
    assert held["w4"] == "lane_full"
    assert held["w5"] == "lane_full"
    assert held["w9"] == "no_free_slots"


# ---------------------------------------------------------------- ledger + status


def test_ledger_counts_the_utc_day(tmp_path: Path):
    go_max.record_dispatch(tmp_path, "w1", lane="go", model="go:deepseek-v4.1-flash", now=NOW)
    go_max.record_dispatch(tmp_path, "w2", lane="nous", model="nous:hy4-flash", now=NOW + 60)
    go_max.record_dispatch(tmp_path, "w3", lane="go", model="go:deepseek-v4.1-flash", now=NOW - 2 * 86400)
    assert go_max.dispatched_today(tmp_path, now=NOW + 120) == 2


def test_status_explains_empty_slots(tmp_path: Path):
    go_max.enable(tmp_path, by="owner", now=NOW)
    record = go_max.build_status(tmp_path, cap(), [], policy=policy(), now=NOW)
    assert record["schema"] == go_max.STATUS_SCHEMA
    assert record["mode"]["enabled"] is True
    assert "no_ready_approved_work" in record["empty_reasons"]
    assert set(record["lanes"]) == set(go_max.LANES)
    assert record["lanes"]["nous"]["active"] == 1
    assert "free" in record["summary"]


def test_status_without_capacity_names_the_reason(tmp_path: Path):
    record = go_max.build_status(tmp_path, None, [cand("w1")], policy=policy(), now=NOW)
    assert record["admission"]["free_slots"] == 0
    assert "capacity_unreadable" in record["empty_reasons"]
    assert "mode_off" in record["empty_reasons"]


def test_status_has_no_secrets_or_hosts(tmp_path: Path):
    record = go_max.build_status(tmp_path, cap(), [cand("w1")], policy=policy(), now=NOW)
    text = json.dumps(record)
    for needle in ("token", "http://", "https://", str(tmp_path)):
        assert needle not in text


# ---------------------------------------------------------------- factory integration


class RecordingWorker:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, job_id: str) -> dict:
        self.calls.append(job_id)
        return {"status": "running", "transitions": []}


def _driver(tmp_path: Path, worker):
    state = tmp_path / "state"
    state.mkdir()
    qf = tmp_path / "queue.json"
    qf.write_text("{}", encoding="utf-8")
    driver = factory_driver.FactoryDriver(state, queue_mod.JsonQueue(qf), tick=worker,
                                          subscribers=["coordinator"], startup=False)
    return state, driver


def _add(state: Path, work_id: str, lease: str):
    work_scheduler.add_item(state, work_id=work_id, title=work_id, kind="feature", priority=0, lease_files=[lease])
    mapping = state / "factory" / "work-jobs"
    mapping.mkdir(parents=True, exist_ok=True)
    (mapping / f"{work_id}.json").write_text(json.dumps({"work_id": work_id, "job_id": f"job-{work_id}"}), encoding="utf-8")
    factory_driver.record_dispatch_approval(state, work_id, job_id=f"job-{work_id}", scope=["src/*"], approved=True)


def _gate(state, capacity, *, lanes=None, step=2):
    lanes = lanes or {}
    return go_max.FactoryGate(
        state,
        capacity_reader=lambda: capacity,
        allowed_reader=lambda: ["go:deepseek-v4.1-flash", "nous:deepseek-v4.1-flash-1017", "nous:hy4-flash", "k3.1-pro"],
        policy=policy(step_max=step),
        lane_reader=lambda item, job_id: {"lane": lanes.get(item["work_id"], "go"), "model": "m", "needs_owner": False},
        now=lambda: NOW,
    )


def test_gate_off_keeps_the_factory_unchanged(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    for i in range(3):
        _add(state, f"w{i}", f"src/f{i}.py")
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], go_max_gate=_gate(state, None))
    out = service.dispatch_eligible()
    service.close()
    assert out["dispatched"] == 3
    assert "go_max" not in out


def test_gate_on_fills_up_to_admission(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    for i in range(3):
        _add(state, f"w{i}", f"src/f{i}.py")
    go_max.enable(state, by="owner", now=NOW)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], go_max_gate=_gate(state, cap(), step=2))
    out = service.dispatch_eligible()
    service.close()
    assert out["dispatched"] == 2
    assert out["go_max"]["admission"]["free_slots"] == 2
    assert any(b.get("reason") == "go_max_no_free_slots" for b in out["blocked"])
    assert out["wake"] is None
    assert go_max.dispatched_today(state, now=NOW) == 2
    assert go_max.read_status(state)["lanes"]["go"]["admitted"] == 2


def test_gate_on_with_unreadable_capacity_dispatches_nothing_and_does_not_wake(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    _add(state, "w0", "src/f0.py")
    go_max.enable(state, by="owner", now=NOW)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], go_max_gate=_gate(state, None))
    out = service.dispatch_eligible()
    service.close()
    assert out["dispatched"] == 0
    assert worker.calls == []
    assert out["wake"] is None
    assert "capacity_unreadable" in out["go_max"]["admission"]["reasons"]


def test_gate_skips_a_held_lane_and_fills_the_next_item(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    _add(state, "w0", "src/f0.py")
    _add(state, "w1", "src/f1.py")
    go_max.enable(state, by="owner", now=NOW)
    gate = _gate(state, cap(), lanes={"w0": "other", "w1": "go"}, step=4)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], go_max_gate=gate)
    out = service.dispatch_eligible()
    service.close()
    assert [d["work_id"] for d in out["dispatched_items"]] == ["w1"]
    assert any(b.get("work_id") == "w0" and b.get("reason") == "go_max_lane_full" for b in out["blocked"])


def test_pressure_starts_a_cooldown(tmp_path: Path):
    state = tmp_path
    go_max.enable(state, by="owner", now=NOW)
    gate = go_max.FactoryGate(state, capacity_reader=lambda: cap(host={"cpu_busy_pct": 99.0}),
                              allowed_reader=lambda: [], policy=policy(), now=lambda: NOW)
    assert gate.begin_pass()["free_slots"] == 0
    gate2 = go_max.FactoryGate(state, capacity_reader=lambda: cap(), allowed_reader=lambda: [],
                               policy=policy(), now=lambda: NOW + 30)
    adm = gate2.begin_pass()
    assert adm["free_slots"] == 0 and "pressure_cooldown" in adm["reasons"]
    later = NOW + 30 + go_max.PRESSURE_COOLDOWN_S
    gate3 = go_max.FactoryGate(state, capacity_reader=lambda: cap(observed_at=later), allowed_reader=lambda: [],
                               policy=policy(), now=lambda: later)
    assert gate3.begin_pass()["free_slots"] > 0


# ---------------------------------------------------------------- dispatch brief routing


def test_route_brief_is_a_no_op_when_off(tmp_path: Path):
    brief = {"assigned_files": ["docs/a.md"], "objective": "x"}
    assert go_max.route_brief(tmp_path, dict(brief), {}) == brief


def test_route_brief_sets_an_allow_listed_model_when_on(tmp_path: Path):
    go_max.enable(tmp_path, by="owner", now=time.time())
    go_max.write_status(tmp_path, {"schema": go_max.STATUS_SCHEMA, "allowed_models": ["go:deepseek-v4.1-flash", "nous:deepseek-v4.1-flash-1017", "nous:hy4-flash"]})
    routed = go_max.route_brief(tmp_path, {"assigned_files": ["docs/a.md"], "objective": "x"}, {})
    assert routed["model"] == "nous:deepseek-v4.1-flash-1017"
    pinned = go_max.route_brief(tmp_path, {"assigned_files": ["docs/a.md"], "model": "go:deepseek-v4.1-flash"}, {})
    assert pinned["model"] == "go:deepseek-v4.1-flash"


def test_route_brief_without_allow_list_leaves_the_default(tmp_path: Path):
    go_max.enable(tmp_path, by="owner", now=time.time())
    routed = go_max.route_brief(tmp_path, {"assigned_files": ["docs/a.md"]}, {})
    assert "model" not in routed


def test_reconcile_dispatch_uses_route_brief():
    src = (Path(__file__).resolve().parent.parent / "bundlebees_plugin" / "reconcile.py").read_text(encoding="utf-8")
    body = src.split("def _dispatch_one(", 1)[1].split("\ndef ", 1)[0]
    assert "route_brief" in body


# ---------------------------------------------------------------- client, tool, CLI


def test_launcher_client_reads_capacity(monkeypatch):
    from bundlebees_plugin.client import LauncherClient

    seen = {}

    def fake(self, method, path, *a, **k):
        seen["call"] = (method, path)
        return {**cap(), "extra": "dropped", "host": {**cap()["host"], "hostname": "dropped"}}

    monkeypatch.setattr(LauncherClient, "request_json", fake)
    client = LauncherClient("http://127.0.0.1:1", token="t" * 40)
    out = client.host_capacity()
    assert seen["call"] == ("GET", "/v1/capacity")
    assert out["schema"] == "hyvv.capacity/1"
    assert "extra" not in out and "hostname" not in out["host"]


def test_tool_status_on_off():
    from tests.fakes import FakeCtx, load_plugin

    ctx = FakeCtx()
    load_plugin().register(ctx)
    assert "bundlebees_go_max" in ctx.tools

    class Client:
        def host_capacity(self):
            return cap()

        def health(self):
            return {"ok": True, "models": ["go:deepseek-v4.1-flash"]}

    ctx.bundlebees_client = Client()
    handler = ctx.tools["bundlebees_go_max"]["handler"]
    on = json.loads(handler({"action": "on", "ttl_minutes": 60}))
    assert on["ok"] is True and on["mode"]["enabled"] is True
    st = json.loads(handler({"action": "status"}))
    assert st["ok"] is True and set(st["status"]["lanes"]) == set(go_max.LANES)
    off = json.loads(handler({"action": "off"}))
    assert off["mode"]["enabled"] is False
    bad = json.loads(handler({"action": "explode"}))
    assert bad["ok"] is False


def test_cli_go_max(tmp_path: Path, capsys):
    from bundlebees_plugin import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    assert cli.go_max_main(["on", "--state-dir", str(tmp_path), "--ttl-minutes", "30"]) == 0
    assert go_max.read_mode(tmp_path)["enabled"] is True
    assert cli.go_max_main(["status", "--state-dir", str(tmp_path), "--json", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "rizzstack.go-max-status/1" in out
    assert cli.go_max_main(["off", "--state-dir", str(tmp_path)]) == 0
    assert go_max.read_mode(tmp_path)["enabled"] is False
