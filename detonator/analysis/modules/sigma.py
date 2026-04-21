"""Sigma-rule-based analysis module.

Loads ``*.yml`` / ``*.yaml`` files from one or more rule directories and
evaluates them against an ``AnalysisContext``.

Design notes
------------
Sigma's YAML grammar is used for rule authoring, but the pysigma library
itself is not a dependency: its primary API is a SIEM-backend compiler
(Splunk, Elastic, etc.) that maps detections onto target query languages,
not an in-process evaluator against a Python dict.  We parse with
``yaml.safe_load`` and implement the subset of modifier + condition
semantics we support directly — this keeps us in control of the grammar
and avoids a heavy dependency that would otherwise be unused at runtime.

Supported modifiers (v1): ``contains``, ``startswith``, ``endswith``, ``re``,
``gte``, ``lte``.  Bare field (no modifier) = exact equality.
List values = OR.  Multiple keys in a selection = AND.
``condition:`` supports ``and``, ``or``, ``not``, parentheses, selection
names.  Rules with unsupported modifiers are skipped at load time.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from detonator.analysis.modules.base import AnalysisContext, AnalysisModule, ResourceContent, TechniqueHit, _tech_id

logger = logging.getLogger(__name__)

_SUPPORTED_MODIFIERS = {"contains", "startswith", "endswith", "re", "gte", "lte"}

# Maps dotted field paths onto AnalysisContext attributes
_FIELD_MAP: dict[str, str] = {
    "seed.url": "seed_url",
    "seed.hostname": "seed_hostname",
    "chain.hostname": "chain_hostnames",
    "chain.url": "chain_urls",
    "chain.initiator_type": "chain_initiator_types",
    "chain.resource_type": "chain_resource_types",
    "chain.redirect_domains": "redirect_domains",
    "chain.cross_origin_redirect_count": "cross_origin_redirect_count",
    "dom.html": "dom_html",
    # Resource fields — evaluated per ResourceContent object, not against the whole context
    "resource.url":       "resources[].url",
    "resource.host":      "resources[].host",
    "resource.mime_type": "resources[].mime_type",
    "resource.body":      "resources[].body",
}

# Attribute names that resolve against individual ResourceContent objects
_RESOURCE_ATTR_MAP: dict[str, str] = {
    "resources[].url":       "url",
    "resources[].host":      "host",
    "resources[].mime_type": "mime_type",
    "resources[].body":      "body",
}


def _references_resource_fields(rule: _ParsedRule) -> bool:
    """Return True if any selection in *rule* references a resource.* field."""
    resource_mapped = set(_RESOURCE_ATTR_MAP)
    for predicates in rule.selections.values():
        for field, _mod, _val in predicates:
            if _FIELD_MAP.get(field) in resource_mapped:
                return True
    return False


# ── Rule representation ──────────────────────────────────────────────


class _ParsedRule:
    """Internal representation of a loaded Sigma rule."""

    __slots__ = (
        "title",
        "rule_id",
        "description",
        "signature_type",
        "confidence",
        "selections",
        "condition",
        "source_path",
    )

    def __init__(
        self,
        title: str,
        rule_id: str,
        description: str,
        signature_type: str,
        confidence: float,
        selections: dict[str, list[tuple[str, str | None, Any]]],
        condition: str,
        source_path: str,
    ) -> None:
        self.title = title
        self.rule_id = rule_id
        self.description = description
        self.signature_type = signature_type
        self.confidence = confidence
        self.selections = selections   # name → [(field, modifier, value), ...]
        self.condition = condition
        self.source_path = source_path


# ── Loader ────────────────────────────────────────────────────────────


def _parse_modifier(field_with_modifier: str) -> tuple[str, str | None]:
    """Split ``chain.hostname|contains`` into (``chain.hostname``, ``contains``)."""
    if "|" in field_with_modifier:
        field, modifier = field_with_modifier.split("|", 1)
        return field.strip(), modifier.strip().lower()
    return field_with_modifier.strip(), None


def _load_rule(path: Path) -> _ParsedRule | None:
    """Parse a single YAML rule file.  Returns None and logs on any issue."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("sigma: skipping %s — YAML parse error: %s", path.name, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning("sigma: skipping %s — not a mapping", path.name)
        return None

    title = raw.get("title", "").strip()
    if not title:
        logger.warning("sigma: skipping %s — missing title", path.name)
        return None

    # Rule ID: prefer explicit UUID if valid, else derive from title
    raw_id = raw.get("id", "")
    try:
        rule_id = str(uuid.UUID(str(raw_id)))
    except (ValueError, AttributeError):
        rule_id = _tech_id(title)

    description = raw.get("description", "")
    signature_type = raw.get("signature_type", "infrastructure")
    raw_confidence = raw.get("confidence", 0.9)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.9

    detection_block = raw.get("detection", {})
    if not isinstance(detection_block, dict):
        logger.warning("sigma: skipping %s — detection is not a mapping", path.name)
        return None

    condition = str(detection_block.get("condition", "")).strip()
    if not condition:
        logger.warning("sigma: skipping %s — missing condition", path.name)
        return None

    selections: dict[str, list[tuple[str, str | None, Any]]] = {}
    skip = False

    for sel_name, sel_body in detection_block.items():
        if sel_name == "condition":
            continue
        if not isinstance(sel_body, dict):
            logger.warning(
                "sigma: skipping %s — selection %r is not a mapping", path.name, sel_name
            )
            skip = True
            break
        predicates: list[tuple[str, str | None, Any]] = []
        for raw_field, value in sel_body.items():
            field, modifier = _parse_modifier(raw_field)
            if modifier is not None and modifier not in _SUPPORTED_MODIFIERS:
                logger.warning(
                    "sigma: skipping %s — unsupported modifier %r in field %r",
                    path.name,
                    modifier,
                    raw_field,
                )
                skip = True
                break
            if field not in _FIELD_MAP:
                logger.warning(
                    "sigma: skipping %s — unknown field %r", path.name, field
                )
                skip = True
                break
            predicates.append((field, modifier, value))
        if skip:
            break
        selections[sel_name] = predicates

    if skip:
        return None

    return _ParsedRule(
        title=title,
        rule_id=rule_id,
        description=description,
        signature_type=signature_type,
        confidence=confidence,
        selections=selections,
        condition=condition,
        source_path=str(path),
    )


def _load_rules_from_dirs(rules_dirs: list[str]) -> list[_ParsedRule]:
    rules: list[_ParsedRule] = []
    loaded = skipped = 0
    for rules_dir in rules_dirs:
        dir_path = Path(rules_dir)
        if not dir_path.is_dir():
            logger.warning("sigma: rules_dir %r does not exist — skipping", rules_dir)
            continue
        for path in sorted(dir_path.glob("*.yml")) + sorted(dir_path.glob("*.yaml")):
            rule = _load_rule(path)
            if rule is not None:
                rules.append(rule)
                loaded += 1
            else:
                skipped += 1
    logger.info("sigma: loaded %d rule(s), skipped %d", loaded, skipped)
    return rules


# ── Evaluator ─────────────────────────────────────────────────────────


def _resolve_field(
    context: AnalysisContext,
    field: str,
    resource: ResourceContent | None = None,
) -> Any:
    """Return the value for a dotted field path.

    For ``resource.*`` fields, resolves against *resource* when provided;
    returns None otherwise (rule will not match).
    """
    attr = _FIELD_MAP[field]
    if attr in _RESOURCE_ATTR_MAP:
        if resource is None:
            return None
        return getattr(resource, _RESOURCE_ATTR_MAP[attr], None)
    return getattr(context, attr, None)


def _match_value(ctx_value: Any, modifier: str | None, rule_value: Any) -> bool:
    """Test a single (modifier, rule_value) against one resolved context value."""
    if modifier is None:
        return str(ctx_value) == str(rule_value)
    if modifier == "contains":
        return str(rule_value).lower() in str(ctx_value).lower()
    if modifier == "startswith":
        return str(ctx_value).lower().startswith(str(rule_value).lower())
    if modifier == "endswith":
        return str(ctx_value).lower().endswith(str(rule_value).lower())
    if modifier == "re":
        return bool(re.search(str(rule_value), str(ctx_value), re.IGNORECASE))
    if modifier == "gte":
        try:
            return float(ctx_value) >= float(rule_value)
        except (TypeError, ValueError):
            return False
    if modifier == "lte":
        try:
            return float(ctx_value) <= float(rule_value)
        except (TypeError, ValueError):
            return False
    return False


def _eval_predicate(
    context: AnalysisContext,
    field: str,
    modifier: str | None,
    rule_value: Any,
    resource: ResourceContent | None = None,
) -> tuple[bool, list[Any]]:
    """Evaluate one predicate; returns (matched, matched_values)."""
    ctx_value = _resolve_field(context, field, resource)

    # Normalise rule_value to a list for OR semantics
    rule_values = rule_value if isinstance(rule_value, list) else [rule_value]

    matched_values: list[Any] = []

    if isinstance(ctx_value, list):
        # List field: pass if ANY element satisfies ANY rule_value
        for elem in ctx_value:
            for rv in rule_values:
                if _match_value(elem, modifier, rv):
                    matched_values.append(elem)
                    break
        return bool(matched_values), matched_values
    else:
        # Scalar field: pass if ANY rule_value matches
        for rv in rule_values:
            if _match_value(ctx_value, modifier, rv):
                matched_values.append(ctx_value)
                return True, matched_values
        return False, []


def _eval_selection(
    context: AnalysisContext,
    selection: list[tuple[str, str | None, Any]],
    resource: ResourceContent | None = None,
) -> tuple[bool, dict[str, list[Any]]]:
    """Evaluate a selection (AND of all predicates); returns (matched, evidence_dict)."""
    evidence: dict[str, list[Any]] = {}
    for field, modifier, rule_value in selection:
        matched, matched_values = _eval_predicate(context, field, modifier, rule_value, resource)
        if not matched:
            return False, {}
        evidence[field] = matched_values
    return True, evidence


# Tokeniser for condition expressions
_COND_TOKEN = re.compile(r"\(|\)|and\b|or\b|not\b|\w+", re.IGNORECASE)


def _eval_condition(
    condition: str,
    selections: dict[str, list[tuple[str, str | None, Any]]],
    context: AnalysisContext,
    resource: ResourceContent | None = None,
) -> tuple[bool, dict[str, list[Any]]]:
    """Evaluate the condition expression; returns (matched, accumulated_evidence)."""
    tokens = _COND_TOKEN.findall(condition)
    pos = 0
    accumulated_evidence: dict[str, list[Any]] = {}

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def consume() -> str:
        nonlocal pos
        t = tokens[pos]
        pos += 1
        return t

    def parse_primary() -> bool:
        nonlocal pos
        t = peek()
        if t is None:
            return False
        if t == "(":
            consume()
            val = parse_or()
            if peek() == ")":
                consume()
            return val
        if t.lower() == "not":
            consume()
            return not parse_primary()
        # It's a selection name
        consume()
        sel = selections.get(t)
        if sel is None:
            return False
        matched, evidence = _eval_selection(context, sel, resource)
        if matched:
            accumulated_evidence.update(evidence)
        return matched

    def parse_and() -> bool:
        left = parse_primary()
        while peek() and peek().lower() == "and":
            consume()
            right = parse_primary()
            left = left and right
        return left

    def parse_or() -> bool:
        left = parse_and()
        while peek() and peek().lower() == "or":
            consume()
            right = parse_and()
            left = left or right
        return left

    result = parse_or()
    return result, accumulated_evidence


# ── Module ────────────────────────────────────────────────────────────


class SigmaModule(AnalysisModule):
    """Evaluates Sigma-style YAML rules against an AnalysisContext."""

    def __init__(self, rules_dirs: list[str]) -> None:
        self._rules = _load_rules_from_dirs(rules_dirs)

    @property
    def name(self) -> str:
        return "sigma"

    async def analyze(self, context: AnalysisContext) -> list[TechniqueHit]:
        hits: list[TechniqueHit] = []

        for rule in self._rules:
            try:
                if _references_resource_fields(rule):
                    # Evaluate once per resource; emit one hit per match
                    for resource in context.resources:
                        matched, evidence = _eval_condition(
                            rule.condition, rule.selections, context, resource
                        )
                        if matched:
                            hits.append(
                                TechniqueHit(
                                    technique_id=rule.rule_id,
                                    name=rule.title,
                                    description=rule.description,
                                    signature_type=rule.signature_type,
                                    confidence=rule.confidence,
                                    evidence={
                                        **evidence,
                                        "resource_url": resource.url,
                                        "mime_type": resource.mime_type,
                                    },
                                    detection_module="sigma",
                                )
                            )
                else:
                    matched, evidence = _eval_condition(
                        rule.condition, rule.selections, context
                    )
                    if matched:
                        hits.append(
                            TechniqueHit(
                                technique_id=rule.rule_id,
                                name=rule.title,
                                description=rule.description,
                                signature_type=rule.signature_type,
                                confidence=rule.confidence,
                                evidence=evidence,
                                detection_module="sigma",
                            )
                        )
            except Exception as exc:
                logger.warning(
                    "run=%s sigma: error evaluating rule %r: %s",
                    context.run_id,
                    rule.title,
                    exc,
                )
                continue

        logger.debug(
            "run=%s sigma: %d hit(s) from %d rule(s)",
            context.run_id,
            len(hits),
            len(self._rules),
        )
        return hits
