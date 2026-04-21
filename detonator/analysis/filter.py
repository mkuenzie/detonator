"""Noise classification for HAR chain results.

After :func:`~detonator.analysis.chain.extract_chain` separates chain
entries from unrelated requests, this module applies one additional pass:

**Noise filter** — marks entries as noise even if they are in the
initiator chain (known tracker domains, beacon/ping resource types).

Technique detection has moved to :mod:`detonator.analysis.modules` and is
driven by the :class:`~detonator.analysis.modules.pipeline.AnalysisPipeline`
in the runner's filtering stage.

Usage::

    result = extract_chain(har_path, seed_url)
    filter  = NoiseFilter(noise_domains=config.filter_noise_domains)
    fr      = filter.run(result, run_id)
    # fr.har_chain  → filtered HAR dict to write as har_chain.json
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydantic import BaseModel

from detonator.analysis.chain import ChainResult, HarEntry

logger = logging.getLogger(__name__)

# Noise classification reason strings
REASON_NO_CHAIN = "not_in_initiator_chain"
REASON_TRACKER = "known_tracking_domain"
REASON_RESOURCE_TYPE = "noise_resource_type"

# Built-in tracker domain list — user list supplements, not replaces these.
_DEFAULT_NOISE_DOMAINS: frozenset[str] = frozenset({
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "doubleclick.net",
    "googlesyndication.com",
    "facebook.net",
    "connect.facebook.net",
    "hotjar.com",
    "segment.com",
    "segment.io",
    "intercom.io",
    "intercomassets.com",
    "bat.bing.com",
    "mc.yandex.ru",
    "yandex.ru",
    "analytics.tiktok.com",
    "ads.linkedin.com",
    "snap.licdn.com",
    "cdn.amplitude.com",
    "api.amplitude.com",
    "mixpanel.com",
    "heapanalytics.com",
    "cdn.heapanalytics.com",
})

# Built-in noise resource types — HAR _resourceType values that are always noise.
_DEFAULT_NOISE_RTYPES: frozenset[str] = frozenset({
    "ping",
    "preflight",
    "csp-violation-report",
    "beacon",
})


# ── Output models ────────────────────────────────────────────────


class FilterEntry(BaseModel):
    url: str
    is_noise: bool
    is_chain: bool = False   # True when reachable from seed via initiator graph
    reasons: list[str] = []


class FilterResult(BaseModel):
    run_id: str
    seed_url: str
    total_requests: int
    chain_requests: int
    noise_requests: int
    entries: list[FilterEntry]
    har_chain: dict         # final filtered HAR (chain minus noise)


# ── Helpers ─────────────────────────────────────────────────────


def _netloc(url: str) -> str:
    return urlparse(url).netloc or ""


# ── Noise filter ─────────────────────────────────────────────────────


class NoiseFilter:
    """Classify HAR entries as noise.

    *noise_domains* supplements (does not replace) the built-in default
    list.  Pass an empty list to use defaults only.

    When *require_initiator_chain* is False (the default) entries that are not
    reachable from the seed URL via the initiator graph are still kept — they
    receive ``is_chain=False`` but are not marked as noise.  Set it to True to
    restore the old behavior where orphan entries are always noise.
    """

    def __init__(
        self,
        noise_domains: list[str] | None = None,
        noise_resource_types: list[str] | None = None,
        require_initiator_chain: bool = False,
    ) -> None:
        self._noise_domains: frozenset[str] = _DEFAULT_NOISE_DOMAINS | frozenset(noise_domains or [])
        self._noise_rtypes: frozenset[str] = _DEFAULT_NOISE_RTYPES | frozenset(noise_resource_types or [])
        self._require_chain = require_initiator_chain

    def _classify(self, entry: HarEntry, chain_url_set: set[str]) -> tuple[list[str], bool]:
        """Return (reasons, in_chain).

        *in_chain* is True when the entry is reachable from the seed URL via
        the initiator graph regardless of whether it is ultimately noise.
        """
        reasons: list[str] = []
        in_chain = entry.url in chain_url_set

        if not in_chain and self._require_chain:
            reasons.append(REASON_NO_CHAIN)

        host = _netloc(entry.url).removeprefix("www.")
        if host in self._noise_domains or any(
            host.endswith(f".{d}") for d in self._noise_domains
        ):
            reasons.append(REASON_TRACKER)

        if entry.resource_type in self._noise_rtypes:
            reasons.append(REASON_RESOURCE_TYPE)

        return reasons, in_chain

    def run(self, chain_result: ChainResult, run_id: str) -> FilterResult:
        """Classify all entries and produce a :class:`FilterResult`.

        ``har_chain`` in the result is the final filtered HAR dict that
        should be written as ``har_chain.json``.  It contains all non-noise
        entries — including initiator-graph orphans when
        ``require_initiator_chain`` is False (the default).
        """
        chain_url_set = set(chain_result.chain_urls)

        filter_entries: list[FilterEntry] = []
        clean_urls: set[str] = set()

        for e in chain_result.all_entries:
            reasons, in_chain = self._classify(e, chain_url_set)
            is_noise = bool(reasons)
            filter_entries.append(
                FilterEntry(url=e.url, is_noise=is_noise, is_chain=in_chain, reasons=reasons)
            )
            if not is_noise:
                clean_urls.add(e.url)

        chain_count = len(clean_urls)
        noise_count = len(filter_entries) - chain_count

        # Build final filtered HAR.  When orphans are allowed (the default),
        # start from the full HAR so orphan entries that are not noise appear in
        # the output; otherwise restrict to the initiator-chain subset.
        source_har = chain_result.har_all if (chain_result.har_all and not self._require_chain) else chain_result.har_chain
        raw_entries = source_har.get("log", {}).get("entries", [])
        final_raw = [
            e for e in raw_entries
            if e.get("request", {}).get("url", "") in clean_urls
        ]
        log_section = {**source_har.get("log", {}), "entries": final_raw}
        har_chain_final = {**source_har, "log": log_section}

        logger.info(
            "run=%s chain filter: total=%d chain=%d noise=%d",
            run_id,
            len(filter_entries),
            chain_count,
            noise_count,
        )

        return FilterResult(
            run_id=run_id,
            seed_url=chain_result.seed_url,
            total_requests=len(filter_entries),
            chain_requests=chain_count,
            noise_requests=noise_count,
            entries=filter_entries,
            har_chain=har_chain_final,
        )
