"""Noise classification for the navigation-scope HAR.

After :func:`~detonator.analysis.navigation.extract_navigation_scope` unions
the BFS results from every navigation root, this module applies one more
pass: drop known tracker domains and noise resource types (``ping``,
``preflight``, beacons, etc.).

Usage::

    scope  = extract_navigation_scope(har_path, nav_path, seed_url)
    filter = NoiseFilter(noise_domains=config.filter_noise_domains)
    fr     = filter.run(scope, run_id)
    # fr.har_navigation  → filtered HAR dict to write as har_navigation.json
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydantic import BaseModel

from detonator.analysis.navigation import HarEntry, NavigationScope

logger = logging.getLogger(__name__)

# Noise classification reason strings
REASON_OUT_OF_SCOPE = "not_in_navigation_scope"
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
    in_scope: bool = False   # True when reachable from any navigation root
    reasons: list[str] = []


class FilterResult(BaseModel):
    run_id: str
    seed_url: str
    total_requests: int
    scope_requests: int
    noise_requests: int
    entries: list[FilterEntry]
    har_navigation: dict     # final filtered HAR (scope minus noise)


# ── Helpers ─────────────────────────────────────────────────────


def _netloc(url: str) -> str:
    return urlparse(url).netloc or ""


# ── Noise filter ─────────────────────────────────────────────────────


class NoiseFilter:
    """Classify HAR entries as noise.

    *noise_domains* supplements (does not replace) the built-in default
    list.  Pass an empty list to use defaults only.

    When *require_navigation_scope* is False (the default) entries outside
    the navigation scope are still kept — they receive ``in_scope=False``
    but are not marked as noise.  Set it to True to restrict the output
    strictly to scope-reachable URLs.  The legacy alias
    ``require_initiator_chain`` is accepted as a kwarg for TOML configs that
    still use the old name.
    """

    def __init__(
        self,
        noise_domains: list[str] | None = None,
        noise_resource_types: list[str] | None = None,
        require_navigation_scope: bool | None = None,
        *,
        require_initiator_chain: bool | None = None,
    ) -> None:
        self._noise_domains: frozenset[str] = _DEFAULT_NOISE_DOMAINS | frozenset(noise_domains or [])
        self._noise_rtypes: frozenset[str] = _DEFAULT_NOISE_RTYPES | frozenset(noise_resource_types or [])
        if require_navigation_scope is None:
            require_navigation_scope = bool(require_initiator_chain) if require_initiator_chain is not None else False
        self._require_scope = require_navigation_scope

    def _classify(self, entry: HarEntry, scope_url_set: set[str]) -> tuple[list[str], bool]:
        """Return (reasons, in_scope)."""
        reasons: list[str] = []
        in_scope = entry.url in scope_url_set

        if not in_scope and self._require_scope:
            reasons.append(REASON_OUT_OF_SCOPE)

        host = _netloc(entry.url).removeprefix("www.")
        if host in self._noise_domains or any(
            host.endswith(f".{d}") for d in self._noise_domains
        ):
            reasons.append(REASON_TRACKER)

        if entry.resource_type in self._noise_rtypes:
            reasons.append(REASON_RESOURCE_TYPE)

        return reasons, in_scope

    def run(self, nav_scope: NavigationScope, run_id: str) -> FilterResult:
        """Classify all entries and produce a :class:`FilterResult`.

        ``har_navigation`` in the result is the filtered HAR dict that should
        be written as ``har_navigation.json``.  When ``require_navigation_scope``
        is False (the default) out-of-scope entries that pass noise checks are
        retained; otherwise only scope-reachable URLs survive.
        """
        scope_url_set = set(nav_scope.scope_urls)

        filter_entries: list[FilterEntry] = []
        clean_urls: set[str] = set()

        for e in nav_scope.all_entries:
            reasons, in_scope = self._classify(e, scope_url_set)
            is_noise = bool(reasons)
            filter_entries.append(
                FilterEntry(url=e.url, is_noise=is_noise, in_scope=in_scope, reasons=reasons)
            )
            if not is_noise:
                clean_urls.add(e.url)

        scope_count = len(clean_urls)
        noise_count = len(filter_entries) - scope_count

        # Build final filtered HAR.  When out-of-scope entries are allowed (the
        # default), start from the full HAR so they appear in the output if not
        # noise; otherwise restrict to scope-reachable URLs only.
        source_har = (
            nav_scope.har_full
            if (nav_scope.har_full and not self._require_scope)
            else nav_scope.har_navigation
        )
        raw_entries = source_har.get("log", {}).get("entries", [])
        final_raw = [
            e for e in raw_entries
            if e.get("request", {}).get("url", "") in clean_urls
        ]
        log_section = {**source_har.get("log", {}), "entries": final_raw}
        har_navigation_final = {**source_har, "log": log_section}

        logger.info(
            "run=%s navigation filter: total=%d scope=%d noise=%d",
            run_id,
            len(filter_entries),
            scope_count,
            noise_count,
        )

        return FilterResult(
            run_id=run_id,
            seed_url=nav_scope.seed_url,
            total_requests=len(filter_entries),
            scope_requests=scope_count,
            noise_requests=noise_count,
            entries=filter_entries,
            har_navigation=har_navigation_final,
        )
