"""Data models for the observable/technique/campaign entity system."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ObservableType(StrEnum):
    DOMAIN = "domain"
    IP = "ip"
    URL = "url"
    FAVICON_HASH = "favicon_hash"
    EMAIL = "email"
    PHONE = "phone"
    TLS_FINGERPRINT = "tls_fingerprint"
    CRYPTO_WALLET = "crypto_wallet"
    REGISTRANT = "registrant"
    ASN = "asn"


class ObservableSource(StrEnum):
    HAR = "har"
    ENRICHMENT = "enrichment"
    DOM_EXTRACTION = "dom_extraction"


class RelationshipType(StrEnum):
    RESOLVES_TO = "resolves_to"
    REDIRECTS_TO = "redirects_to"
    SERVES_FAVICON = "serves_favicon"
    REGISTERED_BY = "registered_by"
    CO_OCCURS_WITH = "co_occurs_with"
    HOSTS = "hosts"
    ISSUED_BY = "issued_by"


class SignatureType(StrEnum):
    INFRASTRUCTURE = "infrastructure"
    DELIVERY = "delivery"
    EVASION = "evasion"
    CONTENT = "content"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    RESOLVED = "resolved"


class Observable(BaseModel):
    """A single observed indicator."""

    id: UUID = Field(default_factory=uuid4)
    type: ObservableType
    value: str
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str] = {}


class ObservableLink(BaseModel):
    """A typed relationship between two observables."""

    source_id: UUID
    target_id: UUID
    relationship: RelationshipType
    confidence: float = 1.0
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence: dict | None = None


class Technique(BaseModel):
    """A behavioral pattern / detection signature."""

    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str
    signature_type: SignatureType
    detection_module: str | None = None


class TechniqueMatch(BaseModel):
    """A specific match of a technique in a run."""

    technique_id: UUID
    run_id: UUID
    confidence: float = 1.0
    evidence: dict | None = None


class Campaign(BaseModel):
    """A grouping of related sites/runs representing a threat operation."""

    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str = ""
    status: CampaignStatus = CampaignStatus.ACTIVE
    confidence: float = 0.0
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
