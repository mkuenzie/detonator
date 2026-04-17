"""Parity tests for BuiltinTechniqueModule.

Verifies that the ported detectors produce the same hits that the old
TechniqueDetector produced, using the same fixture topology as
test_chain_filter.py.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from detonator.analysis.chain import HarEntry, extract_chain
from detonator.analysis.filter import NoiseFilter
from detonator.analysis.modules.base import AnalysisContext
from detonator.analysis.modules.builtin import BuiltinTechniqueModule

# ── Fixture HAR (mirrors test_chain_filter.py topology) ──────────────

SEED_URL = "https://phish.evil.example.com/"

_HAR_FIXTURE: dict = {
    "log": {
        "version": "1.2",
        "entries": [
            {
                "request": {"url": SEED_URL, "method": "GET"},
                "response": {"status": 302, "content": {"mimeType": "text/html"}},
                "_initiator": {"type": "other"},
                "_resourceType": "document",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://phish.evil.example.com/landing", "method": "GET"},
                "response": {"status": 200, "content": {"mimeType": "text/html"}},
                "_initiator": {"type": "redirect", "url": SEED_URL},
                "_resourceType": "document",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://phish.evil.example.com/kit.js", "method": "GET"},
                "response": {"status": 200, "content": {"mimeType": "application/javascript"}},
                "_initiator": {"type": "parser", "url": "https://phish.evil.example.com/landing"},
                "_resourceType": "script",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://www.google-analytics.com/collect?v=1", "method": "GET"},
                "response": {"status": 200, "content": {"mimeType": "image/gif"}},
                "_initiator": {
                    "type": "script",
                    "stack": {"callFrames": [{"url": "https://phish.evil.example.com/kit.js"}]},
                },
                "_resourceType": "xhr",
                "serverIPAddress": "2.3.4.5",
            },
            {
                "request": {"url": "https://phish.evil.example.com/image.png", "method": "GET"},
                "response": {"status": 200, "content": {"mimeType": "image/png"}},
                "_initiator": {"type": "parser", "url": "https://phish.evil.example.com/landing"},
                "_resourceType": "image",
                "serverIPAddress": "1.2.3.4",
            },
            {
                "request": {"url": "https://tracker.example.net/ping", "method": "POST"},
                "response": {"status": 204, "content": {}},
                "_initiator": {"type": "other"},
                "_resourceType": "ping",
                "serverIPAddress": "3.4.5.6",
            },
            {
                "request": {"url": "https://cdn.unrelated.com/lib.js", "method": "GET"},
                "response": {"status": 200, "content": {"mimeType": "application/javascript"}},
                "_initiator": {
                    "type": "script",
                    "stack": {"callFrames": [{"url": "https://other.site.com/page"}]},
                },
                "_resourceType": "script",
                "serverIPAddress": "4.5.6.7",
            },
        ],
    }
}


def _write_har(tmp_path: Path, data: dict | None = None) -> Path:
    p = tmp_path / "har_full.har"
    p.write_text(json.dumps(data or _HAR_FIXTURE), encoding="utf-8")
    return p


def _make_context(tmp_path: Path, har_data: dict | None = None) -> AnalysisContext:
    har_path = _write_har(tmp_path, har_data)
    chain_result = extract_chain(har_path, SEED_URL)
    assert chain_result is not None
    nf = NoiseFilter(noise_domains=["google-analytics.com"])
    fr = nf.run(chain_result, "run-test")
    return AnalysisContext.from_chain(chain_result, fr, str(tmp_path), "run-test", SEED_URL)


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builtin_no_hits_for_clean_chain(tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    # The clean chain is all phish.evil.example.com — no known-bad patterns
    names = {h.name for h in hits}
    assert "Google Cloud Storage phishing host" not in names
    assert "Cloudflare Workers abuse" not in names


@pytest.mark.asyncio
async def test_builtin_detects_gcs(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://storage.googleapis.com/phish/page.html",
        seed_hostname="storage.googleapis.com",
        chain_entries=[
            HarEntry(url="https://storage.googleapis.com/phish/page.html", resource_type="document")
        ],
        chain_hostnames=["storage.googleapis.com"],
        chain_urls=["https://storage.googleapis.com/phish/page.html"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    names = {h.name for h in hits}
    assert "Google Cloud Storage phishing host" in names


@pytest.mark.asyncio
async def test_builtin_detects_workers_dev(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://my-worker.evil.workers.dev/",
        seed_hostname="my-worker.evil.workers.dev",
        chain_entries=[
            HarEntry(url="https://my-worker.evil.workers.dev/", resource_type="document")
        ],
        chain_hostnames=["my-worker.evil.workers.dev"],
        chain_urls=["https://my-worker.evil.workers.dev/"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    names = {h.name for h in hits}
    assert "Cloudflare Workers abuse" in names


@pytest.mark.asyncio
async def test_builtin_detects_github_pages(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://evil.github.io/phish/",
        seed_hostname="evil.github.io",
        chain_entries=[HarEntry(url="https://evil.github.io/phish/", resource_type="document")],
        chain_hostnames=["evil.github.io"],
        chain_urls=["https://evil.github.io/phish/"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "GitHub Pages phishing host" for h in hits)


@pytest.mark.asyncio
async def test_builtin_detects_google_forms(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://docs.google.com/forms/d/abc/viewform",
        seed_hostname="docs.google.com",
        chain_entries=[
            HarEntry(
                url="https://docs.google.com/forms/d/abc/viewform",
                resource_type="document",
            )
        ],
        chain_hostnames=["docs.google.com"],
        chain_urls=["https://docs.google.com/forms/d/abc/viewform"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "Google Forms credential harvester" for h in hits)


@pytest.mark.asyncio
async def test_builtin_detects_data_uri(tmp_path: Path) -> None:
    data_url = "data:text/html;base64,PHNjcmlwdD4="
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://evil.example.com/",
        seed_hostname="evil.example.com",
        chain_entries=[HarEntry(url=data_url, resource_type="document")],
        chain_urls=[data_url],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "Data URI payload" for h in hits)


@pytest.mark.asyncio
async def test_builtin_detects_blob_uri(tmp_path: Path) -> None:
    blob_url = "blob:https://evil.example.com/some-uuid"
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://evil.example.com/",
        seed_hostname="evil.example.com",
        chain_entries=[HarEntry(url=blob_url, resource_type="document")],
        chain_urls=[blob_url],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "Blob URI redirect" for h in hits)


@pytest.mark.asyncio
async def test_builtin_detects_sharepoint(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://tenant.sharepoint.com/phish",
        seed_hostname="tenant.sharepoint.com",
        chain_entries=[HarEntry(url="https://tenant.sharepoint.com/phish", resource_type="document")],
        chain_hostnames=["tenant.sharepoint.com"],
        chain_urls=["https://tenant.sharepoint.com/phish"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "Microsoft SharePoint phishing host" for h in hits)


@pytest.mark.asyncio
async def test_builtin_detects_cross_origin_redirect(tmp_path: Path) -> None:
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://landing.evil.com/",
        seed_hostname="landing.evil.com",
        chain_entries=[
            HarEntry(
                url="https://track.redirect1.com/hop",
                resource_type="document",
                initiator_type="redirect",
                initiator_url="https://landing.evil.com/",
            ),
            HarEntry(
                url="https://final.evil.net/page",
                resource_type="document",
                initiator_type="redirect",
                initiator_url="https://track.redirect1.com/hop",
            ),
        ],
        chain_hostnames=["track.redirect1.com", "final.evil.net"],
        chain_urls=[
            "https://track.redirect1.com/hop",
            "https://final.evil.net/page",
        ],
        redirect_domains=["final.evil.net", "track.redirect1.com"],
        cross_origin_redirect_count=2,
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert any(h.name == "Cross-origin redirect chain" for h in hits)


@pytest.mark.asyncio
async def test_builtin_single_redirect_domain_no_hit(tmp_path: Path) -> None:
    """Only one redirect domain → should NOT fire the cross-origin rule."""
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://evil.com/",
        seed_hostname="evil.com",
        chain_entries=[
            HarEntry(
                url="https://evil.com/page2",
                resource_type="document",
                initiator_type="redirect",
                initiator_url="https://evil.com/",
            ),
        ],
        chain_hostnames=["evil.com"],
        chain_urls=["https://evil.com/page2"],
        redirect_domains=["evil.com"],
        cross_origin_redirect_count=1,
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert not any(h.name == "Cross-origin redirect chain" for h in hits)


@pytest.mark.asyncio
async def test_technique_ids_are_deterministic() -> None:
    """Same technique name → same UUID regardless of run_id."""
    _TECH_NS = uuid.UUID("b4c0ffee-dead-beef-cafe-000000000001")
    expected = str(uuid.uuid5(_TECH_NS, "Google Cloud Storage phishing host"))

    ctx = AnalysisContext(
        run_id="run-a",
        seed_url="https://storage.googleapis.com/x",
        seed_hostname="storage.googleapis.com",
        chain_entries=[HarEntry(url="https://storage.googleapis.com/x", resource_type="document")],
        chain_hostnames=["storage.googleapis.com"],
        chain_urls=["https://storage.googleapis.com/x"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    gcs_hits = [h for h in hits if h.name == "Google Cloud Storage phishing host"]
    assert len(gcs_hits) == 1
    assert gcs_hits[0].technique_id == expected


@pytest.mark.asyncio
async def test_builtin_detection_module_field() -> None:
    """Every hit from BuiltinTechniqueModule must have detection_module='builtin'."""
    ctx = AnalysisContext(
        run_id="run-1",
        seed_url="https://storage.googleapis.com/phish/page.html",
        seed_hostname="storage.googleapis.com",
        chain_entries=[
            HarEntry(url="https://storage.googleapis.com/phish/page.html", resource_type="document")
        ],
        chain_hostnames=["storage.googleapis.com"],
        chain_urls=["https://storage.googleapis.com/phish/page.html"],
    )
    module = BuiltinTechniqueModule()
    hits = await module.analyze(ctx)
    assert hits
    assert all(h.detection_module == "builtin" for h in hits)
