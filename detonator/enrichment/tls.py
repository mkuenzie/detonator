"""TLS certificate chain enricher.

Opens a TLS connection to each domain (port 443) and captures the full
certificate chain (leaf + intermediates + root).

Leaf (position 0) → CERTIFICATE observable, value "{subject_cn} (fp:{fp[:12]})".
Intermediate / root (position ≥ 1) → CERTIFICATE_AUTHORITY observable, value
"{subject_cn} ({subject_org})" when both present, otherwise "{subject_cn}".
CA observables are keyed by subject DN so the same CA node is reused across
all runs that chain through it.

Links emitted:
  domain → (presents_certificate) → leaf_cert
  cert[0] → (issued_by) → ca[1]
  ca[i]   → (issued_by) → ca[i+1]  (for i ≥ 1, skips the self-signed root)

Python ≥ 3.13 returns the full chain via SSLSocket.get_verified_chain().
Earlier versions fall back to the leaf cert only (graceful degradation).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import UTC, datetime

from detonator.enrichment.base import (
    Enricher,
    EnrichmentResult,
    RunContext,
    observable_id,
)
from detonator.models.observables import (
    Observable,
    ObservableLink,
    ObservableType,
    RelationshipType,
)

logger = logging.getLogger(__name__)

_TLS_PORT = 443
_CONNECT_TIMEOUT = 10.0


class TlsEnricher(Enricher):
    """TLS certificate chain enricher."""

    @property
    def name(self) -> str:
        return "tls"

    def accepts(self, artifact_type: str) -> bool:
        return artifact_type == "domain"

    async def enrich(self, context: RunContext) -> list[EnrichmentResult]:
        domains = [d for d in context.domains if not self._is_host_excluded(d)]
        skipped = len(context.domains) - len(domains)
        if skipped:
            logger.debug("tls: skipped %d excluded domains", skipped)
        if not domains:
            return []

        tasks = [self._probe(domain) for domain in domains]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[EnrichmentResult] = []
        for domain, outcome in zip(domains, raw):
            if isinstance(outcome, Exception):
                results.append(
                    EnrichmentResult(
                        enricher=self.name,
                        input_value=domain,
                        error=str(outcome),
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _probe(self, domain: str) -> EnrichmentResult:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
        except ImportError:
            return EnrichmentResult(
                enricher=self.name,
                input_value=domain,
                error="cryptography package not installed — install detonator[enrichment]",
            )

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            der_chain = await asyncio.wait_for(
                _get_der_chain(domain, ctx), timeout=_CONNECT_TIMEOUT
            )
        except Exception as exc:
            return EnrichmentResult(
                enricher=self.name,
                input_value=domain,
                error=f"TLS connect failed: {exc}",
            )

        now = datetime.now(UTC)
        domain_obs_id = observable_id(ObservableType.DOMAIN, domain)
        domain_obs = Observable(
            id=domain_obs_id,
            type=ObservableType.DOMAIN,
            value=domain,
            first_seen=now,
            last_seen=now,
        )

        observables: list[Observable] = [domain_obs]
        links: list[ObservableLink] = []
        cert_obs_ids: list[object] = []

        for pos, der in enumerate(der_chain):
            cert = x509.load_der_x509_certificate(der)
            fp_hex = cert.fingerprint(hashes.SHA256()).hex()

            subject_cn = _get_attr(cert.subject, x509.NameOID.COMMON_NAME)
            subject_org = _get_attr(cert.subject, x509.NameOID.ORGANIZATION_NAME)
            issuer_cn = _get_attr(cert.issuer, x509.NameOID.COMMON_NAME)
            issuer_org = _get_attr(cert.issuer, x509.NameOID.ORGANIZATION_NAME)

            is_self_signed = (
                cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME) ==
                cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            )

            if pos == 0:
                # Leaf certificate
                obs_type = ObservableType.CERTIFICATE
                obs_value = f"{subject_cn} (fp:{fp_hex[:12]})"
                obs_id = observable_id(obs_type, obs_value)

                sans: list[str] = []
                try:
                    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                    sans = [str(n) for n in ext.value]
                except x509.ExtensionNotFound:
                    pass

                meta: dict[str, str] = {
                    "fingerprint_sha256": fp_hex,
                    "subject_cn": subject_cn,
                    "subject_org": subject_org,
                    "issuer_cn": issuer_cn,
                    "issuer_org": issuer_org,
                    "serial": str(cert.serial_number),
                    "not_before": cert.not_valid_before_utc.isoformat(),
                    "not_after": cert.not_valid_after_utc.isoformat(),
                    "sans": ", ".join(sans),
                }
            else:
                # Intermediate or root CA
                obs_type = ObservableType.CERTIFICATE_AUTHORITY
                if subject_cn and subject_org:
                    obs_value = f"{subject_cn} ({subject_org})"
                else:
                    obs_value = subject_cn or subject_org or fp_hex[:16]
                obs_id = observable_id(obs_type, obs_value)

                subject_dn = cert.subject.rfc4514_string()
                meta = {
                    "fingerprint_sha256": fp_hex,
                    "subject_dn": subject_dn,
                    "subject_cn": subject_cn,
                    "subject_org": subject_org,
                    "not_before": cert.not_valid_before_utc.isoformat(),
                    "not_after": cert.not_valid_after_utc.isoformat(),
                    "is_self_signed": str(is_self_signed).lower(),
                }

            obs = Observable(
                id=obs_id,
                type=obs_type,
                value=obs_value,
                first_seen=now,
                last_seen=now,
                metadata=meta,
            )
            observables.append(obs)
            cert_obs_ids.append(obs_id)

        # domain → (presents_certificate) → leaf_cert
        if cert_obs_ids:
            links.append(
                ObservableLink(
                    source_id=domain_obs_id,
                    target_id=cert_obs_ids[0],
                    relationship=RelationshipType.PRESENTS_CERTIFICATE,
                    first_seen=now,
                    last_seen=now,
                )
            )

        # cert[i] → (issued_by) → ca[i+1]
        for i in range(len(cert_obs_ids) - 1):
            links.append(
                ObservableLink(
                    source_id=cert_obs_ids[i],
                    target_id=cert_obs_ids[i + 1],
                    relationship=RelationshipType.ISSUED_BY,
                    first_seen=now,
                    last_seen=now,
                )
            )

        # Summarise leaf cert data for EnrichmentResult.data
        leaf_cert = x509.load_der_x509_certificate(der_chain[0]) if der_chain else None
        data: dict = {}
        if leaf_cert is not None:
            subject_cn = _get_attr(leaf_cert.subject, x509.NameOID.COMMON_NAME)
            issuer_cn = _get_attr(leaf_cert.issuer, x509.NameOID.COMMON_NAME)
            issuer_org = _get_attr(leaf_cert.issuer, x509.NameOID.ORGANIZATION_NAME)
            sans_list: list[str] = []
            try:
                ext = leaf_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                sans_list = [str(n) for n in ext.value]
            except x509.ExtensionNotFound:
                pass
            fp_hex = leaf_cert.fingerprint(hashes.SHA256()).hex()
            data = {
                "subject_cn": subject_cn,
                "issuer_cn": issuer_cn,
                "issuer_org": issuer_org,
                "sans": sans_list,
                "not_before": leaf_cert.not_valid_before_utc.isoformat(),
                "not_after": leaf_cert.not_valid_after_utc.isoformat(),
                "serial": str(leaf_cert.serial_number),
                "fingerprint_sha256": fp_hex,
                "chain_length": len(der_chain),
            }

        return EnrichmentResult(
            enricher=self.name,
            input_value=domain,
            data=data,
            observables=observables,
            observable_links=links,
        )


def _get_attr(name: object, oid: object) -> str:
    """Extract a single string attribute from an x509 Name, or '' if absent."""
    try:
        from cryptography import x509 as _x509

        attrs = name.get_attributes_for_oid(oid)  # type: ignore[union-attr]
        return attrs[0].value if attrs else ""
    except Exception:
        return ""


async def _get_der_chain(domain: str, ctx: ssl.SSLContext) -> list[bytes]:
    """Return DER-encoded certs from the TLS handshake (full chain if possible)."""
    loop = asyncio.get_running_loop()

    def _blocking() -> list[bytes]:
        import socket

        with socket.create_connection((domain, _TLS_PORT), timeout=_CONNECT_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                # Python 3.13+ exposes the full verified chain.
                get_chain = getattr(ssock, "get_verified_chain", None)
                if get_chain is not None:
                    chain = get_chain()
                    return [c.public_bytes(ssl.ENCODING_DER) if hasattr(c, "public_bytes") else bytes(c) for c in chain]

                # Fallback: leaf only.
                der = ssock.getpeercert(binary_form=True)
                if der is None:
                    raise RuntimeError("No certificate returned by server")
                return [der]

    return await loop.run_in_executor(None, _blocking)
