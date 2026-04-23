"""Core enrichers — artifact parsers that always run.

These turn raw captured artifacts (browser navigation log, DOM dump) into
observables. They are part of the core product, not extension points —
contributors rarely add here. If you're adding a new external lookup,
see detonator/enrichment/plugins/ instead.

CORE_ENRICHERS maps each enricher's stable short name to its class.
The pipeline instantiates all of them unconditionally; they do not
appear in config.toml and cannot be disabled by the operator.
"""

from __future__ import annotations

from typing import Callable

from detonator.enrichment.base import Enricher
from detonator.enrichment.core.dom import DomExtractor
from detonator.enrichment.core.navigations import NavigationEnricher

CORE_ENRICHERS: dict[str, Callable[[], Enricher]] = {
    "navigations": NavigationEnricher,
    "dom": DomExtractor,
}
