"""Tests for the TLS enricher — full chain capture and link topology."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from detonator.enrichment.base import RunContext
from detonator.enrichment.tls import TlsEnricher
from detonator.models.observables import ObservableType, RelationshipType


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="tls-test",
        artifact_dir=str(tmp_path),
        seed_url="https://example.com",
        domains=["example.com"],
    )


def _make_cert(subject_cn: str, issuer_cn: str, issuer_org: str = "", self_signed: bool = False):
    """Build a minimal mock x509 Certificate for testing."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = MagicMock()
    cert.serial_number = 12345
    cert.not_valid_before_utc = MagicMock()
    cert.not_valid_before_utc.isoformat.return_value = "2024-01-01T00:00:00+00:00"
    cert.not_valid_after_utc = MagicMock()
    cert.not_valid_after_utc.isoformat.return_value = "2025-01-01T00:00:00+00:00"
    cert.fingerprint = MagicMock(return_value=bytes.fromhex("ab" * 32))

    def _make_name(cn_val: str, org_val: str = ""):
        name = MagicMock()
        cn_attrs = [MagicMock(value=cn_val)] if cn_val else []
        org_attrs = [MagicMock(value=org_val)] if org_val else []
        def get_attrs(oid):
            if oid == x509.NameOID.COMMON_NAME:
                return cn_attrs
            if oid == x509.NameOID.ORGANIZATION_NAME:
                return org_attrs
            return []
        name.get_attributes_for_oid.side_effect = get_attrs
        name.rfc4514_string.return_value = f"CN={cn_val}"
        return name

    cert.subject = _make_name(subject_cn)
    cert.issuer = _make_name(issuer_cn, issuer_org)

    # SANs
    san_ext = MagicMock()
    san_ext.value = [MagicMock(__str__=lambda s: f"DNS:{subject_cn}")]
    cert.extensions.get_extension_for_class.return_value = san_ext

    return cert


@pytest.mark.asyncio
async def test_single_leaf_cert_no_chain(tmp_path: Path) -> None:
    """When only a leaf cert is returned, we get 1 CERTIFICATE observable + presents_certificate link."""
    leaf_der = b"\x00" * 32

    with (
        patch("detonator.enrichment.tls._get_der_chain", return_value=[leaf_der]),
        patch("cryptography.x509.load_der_x509_certificate") as mock_load,
    ):
        leaf_mock = _make_cert("example.com", "Let's Encrypt R3", "Let's Encrypt")
        mock_load.return_value = leaf_mock

        enricher = TlsEnricher()
        results = await enricher.enrich(_ctx(tmp_path))

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    obs_types = {o.type for o in result.observables}
    assert ObservableType.CERTIFICATE in obs_types
    assert ObservableType.DOMAIN in obs_types

    rel_types = {lk.relationship for lk in result.observable_links}
    assert RelationshipType.PRESENTS_CERTIFICATE in rel_types
    assert RelationshipType.ISSUED_BY not in rel_types


@pytest.mark.asyncio
async def test_three_cert_chain_topology(tmp_path: Path) -> None:
    """3-cert chain: leaf + 1 intermediate + 1 root produces correct link topology."""
    fake_chain = [b"\x00" * 32, b"\x01" * 32, b"\x02" * 32]

    leaf_mock = _make_cert("example.com", "Let's Encrypt R3", "Let's Encrypt")
    intermediate_mock = _make_cert("Let's Encrypt R3", "ISRG Root X1", "Internet Security Research Group")
    root_mock = _make_cert("ISRG Root X1", "ISRG Root X1", "Internet Security Research Group", self_signed=True)

    cert_map = {
        fake_chain[0]: leaf_mock,
        fake_chain[1]: intermediate_mock,
        fake_chain[2]: root_mock,
    }

    with (
        patch("detonator.enrichment.tls._get_der_chain", return_value=fake_chain),
        patch("cryptography.x509.load_der_x509_certificate", side_effect=lambda der: cert_map[der]),
    ):
        enricher = TlsEnricher()
        results = await enricher.enrich(_ctx(tmp_path))

    assert len(results) == 1
    result = results[0]
    assert result.error is None

    obs_types = [o.type for o in result.observables]
    assert obs_types.count(ObservableType.CERTIFICATE) == 1
    assert obs_types.count(ObservableType.CERTIFICATE_AUTHORITY) == 2
    assert ObservableType.DOMAIN in obs_types

    rels = [lk.relationship for lk in result.observable_links]
    assert rels.count(RelationshipType.PRESENTS_CERTIFICATE) == 1
    assert rels.count(RelationshipType.ISSUED_BY) == 2


@pytest.mark.asyncio
async def test_tls_connect_failure_returns_error(tmp_path: Path) -> None:
    with patch("detonator.enrichment.tls._get_der_chain", side_effect=OSError("refused")):
        enricher = TlsEnricher()
        results = await enricher.enrich(_ctx(tmp_path))

    assert len(results) == 1
    assert results[0].error is not None
    assert "TLS connect failed" in results[0].error


@pytest.mark.asyncio
async def test_cert_metadata_populated(tmp_path: Path) -> None:
    """Leaf cert observable carries expected metadata keys."""
    leaf_der = b"\x00" * 32

    with (
        patch("detonator.enrichment.tls._get_der_chain", return_value=[leaf_der]),
        patch("cryptography.x509.load_der_x509_certificate") as mock_load,
    ):
        mock_load.return_value = _make_cert("example.com", "Let's Encrypt R3", "Let's Encrypt")
        enricher = TlsEnricher()
        results = await enricher.enrich(_ctx(tmp_path))

    cert_obs = next(o for o in results[0].observables if o.type == ObservableType.CERTIFICATE)
    for key in ("fingerprint_sha256", "subject_cn", "issuer_cn", "issuer_org", "not_before", "not_after"):
        assert key in cert_obs.metadata, f"missing metadata key: {key}"
