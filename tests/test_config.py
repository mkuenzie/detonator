"""Tests for TOML config loading."""

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from detonator.config import DetonatorConfig, EnrichmentConfig, EnricherConfig, load_config


def test_load_example_config():
    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.vm_provider.type == "proxmox"
    assert len(cfg.agents) == 1
    assert cfg.agents[0].name == "win11-sandbox"
    assert cfg.agents[0].vm_id == "100"
    assert cfg.agents[0].snapshot == "clean"
    assert cfg.agents[0].port == 8000
    assert "direct" in cfg.egress
    assert cfg.egress["direct"].type == "direct"
    assert cfg.storage.data_dir == "data"
    # New enrichment config should parse correctly
    assert cfg.enrichment.modules == ["whois", "dns", "tls", "favicon", "navigations"]
    assert "jsdelivr.net" in cfg.enrichment.whois.exclude_hosts


def test_defaults():
    cfg = DetonatorConfig()
    assert cfg.vm_provider.type == "proxmox"
    assert cfg.log_level == "INFO"
    assert cfg.timeouts.detonate_sec == 120
    assert cfg.enrichment.modules == ["whois", "dns", "tls", "favicon", "navigations"]
    assert cfg.agents == []


def test_minimal_toml():
    content = b"""
[[agents]]
name = "sandbox"
vm_id = "200"
snapshot = "clean"

[vm_provider]
type = "proxmox"
"""
    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
        f.write(content)
        f.flush()
        cfg = load_config(f.name)

    assert cfg.agents[0].vm_id == "200"
    assert cfg.storage.db_path == "data/detonator.db"


def test_get_agent_by_name():
    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    agent = cfg.get_agent("win11-sandbox")
    assert agent.vm_id == "100"

    with pytest.raises(KeyError):
        cfg.get_agent("nope")


def test_default_agent_raises_when_empty():
    cfg = DetonatorConfig()
    with pytest.raises(RuntimeError):
        cfg.default_agent()


def test_enrichment_config_parses_from_toml():
    content = b"""
[enrichment]
modules = ["whois", "dns"]

[enrichment.whois]
exclude_hosts = ["example.com", "badcdn.net"]

[enrichment.dns]
exclude_hosts = []
"""
    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
        f.write(content)
        f.flush()
        cfg = load_config(f.name)

    assert cfg.enrichment.modules == ["whois", "dns"]
    assert "example.com" in cfg.enrichment.whois.exclude_hosts
    assert "badcdn.net" in cfg.enrichment.whois.exclude_hosts
    assert cfg.enrichment.dns.exclude_hosts == []


def test_old_enrichment_modules_key_rejected():
    """Flat enrichment_modules key must be rejected — no silent fallback."""
    with pytest.raises(ValidationError):
        DetonatorConfig(enrichment_modules=["whois", "dns"])
