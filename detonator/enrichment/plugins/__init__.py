"""Plug-in enrichers — opt-in external lookups.

Each plug-in implements detonator.enrichment.base.Enricher and performs an
external query (WHOIS, DNS, TLS handshake, favicon fetch, hosting lookup, ...)
against the domains/IPs/URLs in RunContext.

To add a plug-in:
  1. Drop a new module here that subclasses Enricher.
  2. If it supports per-host exclusions (most external lookups do),
     set ``supports_exclusions = True`` on the class.
  3. Register it below in PLUGIN_ENRICHERS under a stable short name.

The short name is what operators put in config.toml under
[enrichment].modules, and what exclusions rows reference.

PLUGIN_ENRICHERS maps each enricher's stable short name to its class.
The pipeline instantiates only those whose names appear in
config.enrichment.modules.
"""

from __future__ import annotations

from typing import Callable

from detonator.enrichment.base import Enricher
from detonator.enrichment.plugins.dns import DnsEnricher
from detonator.enrichment.plugins.favicon import FaviconEnricher
from detonator.enrichment.plugins.hosting import HostingEnricher
from detonator.enrichment.plugins.tld import TldEnricher
from detonator.enrichment.plugins.tls import TlsEnricher
from detonator.enrichment.plugins.whois import WhoisEnricher

PLUGIN_ENRICHERS: dict[str, Callable[[], Enricher]] = {
    "whois": WhoisEnricher,
    "dns": DnsEnricher,
    "tls": TlsEnricher,
    "favicon": FaviconEnricher,
    "tld": TldEnricher,
    "hosting": HostingEnricher,
}
