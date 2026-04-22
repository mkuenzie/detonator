"""TLS certificate enricher.

Opens a TLS connection to each domain (port 443) and extracts the leaf
certificate's subject, SANs, issuer, validity window, and SHA-256 fingerprint.
The fingerprint is stored as a TLS_FINGERPRINT observable linked to the domain.
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
            from cryptography.hazmat.primitives import hashes, serialization
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
            der_cert = await asyncio.wait_for(
                _get_der_cert(domain, ctx), timeout=_CONNECT_TIMEOUT
            )
        except Exception as exc:
            return EnrichmentResult(
                enricher=self.name,
                input_value=domain,
                error=f"TLS connect failed: {exc}",
            )

        cert = x509.load_der_x509_certificate(der_cert)
        fp_hex = cert.fingerprint(hashes.SHA256()).hex()

        # Subject CN
        subject_cn = _get_attr(cert.subject, x509.NameOID.COMMON_NAME)
        issuer_cn = _get_attr(cert.issuer, x509.NameOID.COMMON_NAME)
        issuer_org = _get_attr(cert.issuer, x509.NameOID.ORGANIZATION_NAME)

        # SANs
        sans: list[str] = []
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans = [str(n) for n in ext.value]
        except x509.ExtensionNotFound:
            pass

        data = {
            "subject_cn": subject_cn,
            "issuer_cn": issuer_cn,
            "issuer_org": issuer_org,
            "sans": sans,
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "serial": str(cert.serial_number),
            "fingerprint_sha256": fp_hex,
        }

        now = datetime.now(UTC)
        domain_obs_id = observable_id(ObservableType.DOMAIN, domain)
        fp_obs_id = observable_id(ObservableType.TLS_FINGERPRINT, fp_hex)

        fp_obs = Observable(
            id=fp_obs_id,
            type=ObservableType.TLS_FINGERPRINT,
            value=fp_hex,
            first_seen=now,
            last_seen=now,
            metadata={
                "subject_cn": subject_cn,
                "issuer_org": issuer_org,
                "not_after": cert.not_valid_after_utc.isoformat(),
            },
        )
        domain_obs = Observable(
            id=domain_obs_id,
            type=ObservableType.DOMAIN,
            value=domain,
            first_seen=now,
            last_seen=now,
        )
        link = ObservableLink(
            source_id=domain_obs_id,
            target_id=fp_obs_id,
            relationship=RelationshipType.ISSUED_BY,
            first_seen=now,
            last_seen=now,
            evidence={"issuer_cn": issuer_cn, "issuer_org": issuer_org},
        )

        return EnrichmentResult(
            enricher=self.name,
            input_value=domain,
            data=data,
            observables=[domain_obs, fp_obs],
            observable_links=[link],
        )


def _get_attr(name: object, oid: object) -> str:
    """Extract a single string attribute from an x509 Name, or '' if absent."""
    try:
        from cryptography import x509 as _x509

        attrs = name.get_attributes_for_oid(oid)  # type: ignore[union-attr]
        return attrs[0].value if attrs else ""
    except Exception:
        return ""


async def _get_der_cert(domain: str, ctx: ssl.SSLContext) -> bytes:
    """Open a TLS connection and return the DER-encoded leaf certificate."""
    loop = asyncio.get_running_loop()

    def _blocking() -> bytes:
        import socket

        with socket.create_connection((domain, _TLS_PORT), timeout=_CONNECT_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                der = ssock.getpeercert(binary_form=True)
                if der is None:
                    raise RuntimeError("No certificate returned by server")
                return der

    return await loop.run_in_executor(None, _blocking)
