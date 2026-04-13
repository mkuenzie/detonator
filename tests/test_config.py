"""Tests for TOML config loading."""

import tempfile
from pathlib import Path

from detonator.config import DetonatorConfig, load_config


def test_load_example_config():
    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.vm_provider.type == "proxmox"
    assert cfg.default_vm_id == "100"
    assert cfg.default_snapshot == "clean"
    assert "direct" in cfg.egress
    assert cfg.egress["direct"].type == "direct"
    assert cfg.storage.data_dir == "data"
    assert cfg.agent.port == 8000


def test_defaults():
    cfg = DetonatorConfig()
    assert cfg.vm_provider.type == "proxmox"
    assert cfg.log_level == "INFO"
    assert cfg.timeouts.detonate_sec == 120
    assert len(cfg.enrichment_modules) == 4


def test_minimal_toml():
    content = b"""
default_vm_id = "200"

[vm_provider]
type = "proxmox"
"""
    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
        f.write(content)
        f.flush()
        cfg = load_config(f.name)

    assert cfg.default_vm_id == "200"
    assert cfg.storage.db_path == "data/detonator.db"
