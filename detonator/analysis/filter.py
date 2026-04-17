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


# ── Output models ────────────────────────────────────────────────


class FilterEntry(BaseModel):
    url: str
    is_noise: bool
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
    """

    def __init__(
        self,
        noise_domains: list[str] | None = None,
        noise_resource_types: list[str] | None = None,
    ) -> None:
        self._noise_domains: frozenset[str] = frozenset(noise_domains or [])
        self._noise_rtypes: frozenset[str] = frozenset(noise_resource_types or [])

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

        # Build final filtered HAR (clean chain only)
        raw_entries = chain_result.har_chain.get("log", {}).get("entries", [])
        final_raw = [
            e for e in raw_entries
            if e.get("request", {}).get("url", "") in clean_urls
        ]
        log_section = {**chain_result.har_chain.get("log", {}), "entries": final_raw}
        har_chain_final = {**chain_result.har_chain, "log": log_section}

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
