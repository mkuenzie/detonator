"""Tests for data models."""

from detonator.models import (
    EgressType,
    Observable,
    ObservableType,
    RunConfig,
    RunRecord,
    RunState,
    StateTransition,
    VMInfo,
    VMState,
)


def test_vm_info_defaults():
    vm = VMInfo(vm_id="100", name="sandbox", state=VMState.STOPPED)
    assert vm.vm_id == "100"
    assert vm.snapshots == []
    assert vm.network is None


def test_run_config_defaults():
    cfg = RunConfig(url="https://example.com")
    assert cfg.egress == EgressType.DIRECT
    assert cfg.timeout_sec == 60
    assert cfg.interactive is False


def test_run_record_generates_id():
    r1 = RunRecord(config=RunConfig(url="https://a.com"))
    r2 = RunRecord(config=RunConfig(url="https://b.com"))
    assert r1.id != r2.id
    assert r1.state == RunState.PENDING


def test_state_transition():
    t = StateTransition(from_state=RunState.PENDING, to_state=RunState.PROVISIONING)
    assert t.from_state == RunState.PENDING
    assert t.detail is None


def test_observable_defaults():
    obs = Observable(type=ObservableType.DOMAIN, value="example.com")
    assert obs.metadata == {}
    assert obs.first_seen is not None
