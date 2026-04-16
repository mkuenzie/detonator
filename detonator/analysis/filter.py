"""Noise classification and technique detection for HAR chain results.

After :func:`~detonator.analysis.chain.extract_chain` separates chain
entries from unrelated requests, this module applies two additional passes:

1. **Noise filter** — marks entries as noise even if they are in the
   initiator chain (known tracker domains, beacon/ping resource types).
2. **Technique detector** — pattern-matches the *clean* chain against a
   catalogue of infrastructure and delivery signatures and returns
   :class:`TechniqueHit` records that the runner persists to the DB.

Usage::

    result = extract_chain(har_path, seed_url)
    filter  = NoiseFilter(noise_domains=config.filter_noise_domains)
    fr      = filter.run(result, run_id)
    # fr.har_chain  → filtered HAR dict to write as har_chain.json
    # fr.technique_hits → to persist via database.upsert_technique + insert_technique_match
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from pydantic import BaseModel

from detonator.analysis.chain import ChainResult, HarEntry
from detonator.models.observables import SignatureType

logger = logging.getLogger(__name__)


# ── Default noise catalogue ──────────────────────────────────────────

# Domains whose requests are always considered analytic/tracking noise
# regardless of their position in the initiator graph.
_DEFAULT_NOISE_DOMAINS: frozenset[str] = frozenset(
    {
        "google-analytics.com",
        "www.google-analytics.com",
        "ssl.google-analytics.com",
        "googletagmanager.com",
        "www.googletagmanager.com",
        "doubleclick.net",
        "ad.doubleclick.net",
        "googlesyndication.com",
        "connect.facebook.net",
        "www.facebook.com",
        "facebook.com",
        "platform.twitter.com",
        "t.co",
        "static.hotjar.com",
        "script.hotjar.com",
        "cdn.segment.com",
        "api.segment.io",
        "intercom.io",
        "widget.intercom.io",
        "bat.bing.com",
        "mc.yandex.ru",
        "analytics.tiktok.com",
        "snap.licdn.com",
    }
)

# Chromium _resourceType values that are unconditionally noise
_NOISE_RESOURCE_TYPES: frozenset[str] = frozenset(
    {"ping", "preflight", "csp-violation-report", "beacon"}
)

# Noise classification reason strings
REASON_NO_CHAIN = "not_in_initiator_chain"
REASON_TRACKER = "known_tracking_domain"
REASON_RESOURCE_TYPE = "noise_resource_type"


# ── Output models ────────────────────────────────────────────────────


class FilterEntry(BaseModel):
    url: str
    is_noise: bool
    reasons: list[str] = []


class TechniqueHit(BaseModel):
    """A matched technique to be persisted to the DB."""

    technique_id: str       # deterministic UUID string
    name: str
    description: str
    signature_type: str     # SignatureType value
    confidence: float
    evidence: dict


class FilterResult(BaseModel):
    run_id: str
    seed_url: str
    total_requests: int
    chain_requests: int
    noise_requests: int
    entries: list[FilterEntry]
    technique_hits: list[TechniqueHit]
    har_chain: dict         # final filtered HAR (chain minus noise)


# ── Technique detection ──────────────────────────────────────────────

_TECH_NS = uuid.UUID("b4c0ffee-dead-beef-cafe-000000000001")


def _tech_id(name: str) -> str:
    """Deterministic UUID for a technique name — same ID across runs."""
    return str(uuid.uuid5(_TECH_NS, name))


def _netloc(url: str) -> str:
    return urlparse(url).netloc or ""


# Per-entry detectors: (name, description, signature_type, predicate)
_ENTRY_DETECTORS: list[tuple[str, str, SignatureType, object]] = [
    (
        "Google Cloud Storage phishing host",
        "Page hosted on storage.googleapis.com — a common free-tier vector for phishing kits",
        SignatureType.INFRASTRUCTURE,
        lambda e: "storage.googleapis.com" in _netloc(e.url),
    ),
    (
        "Cloudflare Workers abuse",
        "Page hosted on workers.dev — used to proxy phishing pages behind Cloudflare infrastructure",
        SignatureType.INFRASTRUCTURE,
        lambda e: _netloc(e.url).endswith(".workers.dev"),
    ),
    (
        "GitHub Pages phishing host",
        "Page hosted on github.io — used for free static phishing page hosting",
        SignatureType.INFRASTRUCTURE,
        lambda e: _netloc(e.url).endswith(".github.io"),
    ),
    (
        "Google Forms credential harvester",
        "Page uses Google Forms (docs.google.com/forms) as a credential collection endpoint",
        SignatureType.DELIVERY,
        lambda e: "docs.google.com" in _netloc(e.url) and "/forms/" in e.url,
    ),
    (
        "Data URI payload",
        "Request uses a data: URI — potential obfuscated redirect or inline content injection",
        SignatureType.EVASION,
        lambda e: e.url.startswith("data:"),
    ),
    (
        "Blob URI redirect",
        "Request uses a blob: URI — common in drive-by download chains",
        SignatureType.EVASION,
        lambda e: e.url.startswith("blob:"),
    ),
    (
        "Microsoft SharePoint phishing host",
        "Page hosted on sharepoint.com — used to lend legitimacy to phishing pages",
        SignatureType.INFRASTRUCTURE,
        lambda e: _netloc(e.url).endswith(".sharepoint.com"),
    ),
]


class TechniqueDetector:
    """Match the chain against the built-in technique catalogue."""

    def detect(self, chain_entries: list[HarEntry], run_id: str) -> list[TechniqueHit]:
        hits: list[TechniqueHit] = []

        # Per-entry checks
        for name, description, sig_type, predicate in _ENTRY_DETECTORS:
            matching = [e for e in chain_entries if predicate(e)]  # type: ignore[operator]
            if matching:
                hits.append(
                    TechniqueHit(
                        technique_id=_tech_id(name),
                        name=name,
                        description=description,
                        signature_type=sig_type.value,
                        confidence=0.9,
                        evidence={"matching_urls": [e.url for e in matching[:10]]},
                    )
                )

        # Chain-level: cross-origin redirect chain
        redirect_entries = [
            e for e in chain_entries if e.initiator_type == "redirect"
        ]
        if redirect_entries:
            redirect_domains = sorted(
                {_netloc(e.url) for e in redirect_entries if _netloc(e.url)}
            )
            # Only flag when the redirect hops across ≥2 distinct netlocs
            if len(redirect_domains) >= 2:
                name = "Cross-origin redirect chain"
                hits.append(
                    TechniqueHit(
                        technique_id=_tech_id(name),
                        name=name,
                        description=(
                            "The initiator chain crosses multiple distinct domains via "
                            "HTTP redirects — common in tracking pixels, cloaking, and "
                            "multi-hop phishing delivery"
                        ),
                        signature_type=SignatureType.DELIVERY.value,
                        confidence=0.75,
                        evidence={"redirect_domains": redirect_domains[:20]},
                    )
                )

        return hits


# ── Noise filter ─────────────────────────────────────────────────────


class NoiseFilter:
    """Classify HAR entries as noise and detect techniques.

    *noise_domains* supplements (does not replace) the built-in default
    list.  Pass an empty list to use defaults only.
    """

    def __init__(
        self,
        noise_domains: list[str] | None = None,
        noise_resource_types: list[str] | None = None,
    ) -> None:
        extra = frozenset(noise_domains or [])
        self._noise_domains: frozenset[str] = _DEFAULT_NOISE_DOMAINS | extra
        self._noise_rtypes: frozenset[str] = (
            _NOISE_RESOURCE_TYPES | frozenset(noise_resource_types or [])
        )
        self._detector = TechniqueDetector()

    def _classify(self, entry: HarEntry, chain_url_set: set[str]) -> list[str]:
        reasons: list[str] = []

        if entry.url not in chain_url_set:
            reasons.append(REASON_NO_CHAIN)

        host = _netloc(entry.url).removeprefix("www.")
        if host in self._noise_domains or any(
            host.endswith(f".{d}") for d in self._noise_domains
        ):
            reasons.append(REASON_TRACKER)

        if entry.resource_type in self._noise_rtypes:
            reasons.append(REASON_RESOURCE_TYPE)

        return reasons

    def run(self, chain_result: ChainResult, run_id: str) -> FilterResult:
        """Classify all entries and produce a :class:`FilterResult`.

        ``har_chain`` in the result is the final filtered HAR dict that
        should be written as ``har_chain.json``.  It contains only the
        requests that are in the initiator chain *and* not classified as
        noise.
        """
        chain_url_set = set(chain_result.chain_urls)

        filter_entries: list[FilterEntry] = []
        clean_urls: set[str] = set()

        for e in chain_result.all_entries:
            reasons = self._classify(e, chain_url_set)
            is_noise = bool(reasons)
            filter_entries.append(FilterEntry(url=e.url, is_noise=is_noise, reasons=reasons))
            if not is_noise:
                clean_urls.add(e.url)

        chain_count = len(clean_urls)
        noise_count = len(filter_entries) - chain_count

        # Technique detection runs against the clean chain
        clean_entries = [e for e in chain_result.all_entries if e.url in clean_urls]
        technique_hits = self._detector.detect(clean_entries, run_id)

        # Build final filtered HAR (clean chain only)
        raw_entries = chain_result.har_chain.get("log", {}).get("entries", [])
        final_raw = [
            e for e in raw_entries
            if e.get("request", {}).get("url", "") in clean_urls
        ]
        log_section = {**chain_result.har_chain.get("log", {}), "entries": final_raw}
        har_chain_final = {**chain_result.har_chain, "log": log_section}

        logger.info(
            "run=%s chain filter: total=%d chain=%d noise=%d techniques=%d",
            run_id,
            len(filter_entries),
            chain_count,
            noise_count,
            len(technique_hits),
        )

        return FilterResult(
            run_id=run_id,
            seed_url=chain_result.seed_url,
            total_requests=len(filter_entries),
            chain_requests=chain_count,
            noise_requests=noise_count,
            entries=filter_entries,
            technique_hits=technique_hits,
            har_chain=har_chain_final,
        )
