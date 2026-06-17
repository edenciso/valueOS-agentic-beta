"""
ValueOS v0.1 — Shared Data Models
Maps to PRD Section 4 (Core Data Schema) from the Implementation Mapping document.
These models enforce data contracts across all Lambda functions.
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


def ulid_sortable() -> str:
    """Generate a time-sortable unique ID (ULID-like).
    First 10 chars encode millisecond timestamp, remaining 16 are random.
    This gives us time-sorted DynamoDB range keys without a GSI."""
    ts = int(time.time() * 1000)
    ts_part = format(ts, '012x')
    rand_part = uuid.uuid4().hex[:16]
    return f"{ts_part}-{rand_part}"


# ═══════════════════════════════════════════════════
# VI-001: LLM Usage Event
# Maps to: llm_usage_events (TimescaleDB hypertable) in full arch
# Beta implementation: DynamoDB + Timestream dual-write
# ═══════════════════════════════════════════════════
class UsageEvent:
    """A single LLM API call record. This is the atomic unit of data
    in the ingestion pipeline — every token spent flows through here."""

    VALID_PROVIDERS = {"openai", "anthropic", "google", "bedrock", "azure_openai"}

    def __init__(self, tenant_id: str, provider: str, model_id: str,
                 input_tokens: int, output_tokens: int,
                 latency_ms: Optional[int] = None,
                 agent_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 metadata: Optional[dict] = None):
        self.event_id = ulid_sortable()
        self.tenant_id = tenant_id
        self.provider = provider.lower()
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.agent_id = agent_id
        self.user_id = user_id
        self.metadata = metadata or {}
        self.recorded_at = datetime.now(timezone.utc).isoformat()
        # TTL: auto-expire raw events after 90 days (cost optimization)
        self.ttl = int(time.time()) + (90 * 24 * 3600)

    def validate(self) -> list[str]:
        """Validate the event before writing. Returns list of errors (empty = valid)."""
        errors = []
        if not self.tenant_id:
            errors.append("tenant_id is required")
        if self.provider not in self.VALID_PROVIDERS:
            errors.append(f"provider must be one of: {self.VALID_PROVIDERS}")
        if not self.model_id:
            errors.append("model_id is required")
        if self.input_tokens < 0:
            errors.append("input_tokens must be >= 0")
        if self.output_tokens < 0:
            errors.append("output_tokens must be >= 0")
        return errors

    def to_dynamo(self) -> dict:
        """Serialize to DynamoDB item format."""
        item = {
            "tenant_id": self.tenant_id,
            "event_id": self.event_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "recorded_at": self.recorded_at,
            "ttl": self.ttl,
        }
        if self.latency_ms is not None:
            item["latency_ms"] = self.latency_ms
        if self.agent_id:
            item["agent_id"] = self.agent_id
        if self.user_id:
            item["user_id"] = self.user_id
        if self.metadata:
            item["metadata"] = self.metadata
        return item

    def to_timestream_record(self) -> dict:
        """Serialize to Timestream record format for time-series analytics."""
        return {
            "Dimensions": [
                {"Name": "tenant_id", "Value": self.tenant_id},
                {"Name": "provider", "Value": self.provider},
                {"Name": "model_id", "Value": self.model_id},
            ],
            "MeasureName": "usage",
            "MeasureValueType": "MULTI",
            "MeasureValues": [
                {"Name": "input_tokens", "Value": str(self.input_tokens), "Type": "BIGINT"},
                {"Name": "output_tokens", "Value": str(self.output_tokens), "Type": "BIGINT"},
                {"Name": "latency_ms", "Value": str(self.latency_ms or 0), "Type": "BIGINT"},
            ],
            "Time": str(int(time.time() * 1000)),
            "TimeUnit": "MILLISECONDS",
        }


# ═══════════════════════════════════════════════════
# VI-002: Cost Record
# Maps to: CostRecordsTable — enriched usage with dollar values
# ═══════════════════════════════════════════════════
class CostRecord:
    """An enriched usage event with calculated cost in USD.
    Created by the Cost Engine Lambda when it processes DynamoDB Stream events."""

    def __init__(self, tenant_id: str, event_id: str, provider: str,
                 model_id: str, input_tokens: int, output_tokens: int,
                 input_cost_usd: Decimal, output_cost_usd: Decimal,
                 total_cost_usd: Decimal, recorded_at: str,
                 pricing_version: str = "2026-02",
                 agent_id: Optional[str] = None,
                 user_id: Optional[str] = None):
        self.cost_record_id = ulid_sortable()
        self.tenant_id = tenant_id
        self.source_event_id = event_id
        self.provider = provider
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.input_cost_usd = input_cost_usd
        self.output_cost_usd = output_cost_usd
        self.total_cost_usd = total_cost_usd
        self.recorded_at = recorded_at
        self.pricing_version = pricing_version
        self.agent_id = agent_id
        self.user_id = user_id
        # Composite sort key for GSI: enables "cost by date + provider" queries
        date_str = recorded_at[:10]  # YYYY-MM-DD
        self.date_provider = f"{date_str}#{provider}#{model_id}"

    def to_dynamo(self) -> dict:
        """Serialize to DynamoDB item. Note: Decimal is native to DynamoDB."""
        item = {
            "tenant_id": self.tenant_id,
            "cost_record_id": self.cost_record_id,
            "source_event_id": self.source_event_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "recorded_at": self.recorded_at,
            "date_provider": self.date_provider,
            "pricing_version": self.pricing_version,
        }
        if self.agent_id:
            item["agent_id"] = self.agent_id
        if self.user_id:
            item["user_id"] = self.user_id
        return item


# ═══════════════════════════════════════════════════
# GF-007: Audit Log Entry (Immutable, Hash-Chained)
# Maps to: audit_log (Append-only PostgreSQL) in full arch
# Beta: DynamoDB with hash_chain column for tamper evidence
# ═══════════════════════════════════════════════════
class AuditEntry:
    """An immutable audit log entry with hash-chain integrity.
    
    The hash_chain field implements the blockchain-like tamper-evident structure
    from the Implementation Mapping (Section 4.2): each entry's hash incorporates
    the previous entry's hash, so any modification breaks the chain.
    
    Verification: GET /api/v1/audit/logs/{id}/chain-verify walks the chain
    backward and validates each hash link.
    """

    VALID_ACTOR_TYPES = {"user", "agent", "system", "integration"}
    VALID_ACTIONS = {"created", "updated", "deleted", "accessed", "evaluated",
                     "ingested", "calculated", "detected", "generated", "forecasted"}

    def __init__(self, tenant_id: str, event_type: str,
                 actor_type: str, actor_id: str, action: str,
                 resource_type: Optional[str] = None,
                 resource_id: Optional[str] = None,
                 details: Optional[dict] = None,
                 ip_address: Optional[str] = None,
                 previous_hash: Optional[str] = None):
        self.log_id = ulid_sortable()
        self.tenant_id = tenant_id
        self.event_type = event_type
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.action = action
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.details = details or {}
        self.ip_address = ip_address
        self.created_at = datetime.now(timezone.utc).isoformat()
        # Hash chain: SHA-256(previous_hash + this_entry_json)
        self.previous_hash = previous_hash or "GENESIS"
        self.hash_chain = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute the tamper-evident hash for this entry.
        The hash includes the previous hash, creating an unbreakable chain."""
        payload = json.dumps({
            "log_id": self.log_id,
            "tenant_id": self.tenant_id,
            "event_type": self.event_type,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "details": self.details,
            "created_at": self.created_at,
            "previous_hash": self.previous_hash,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dynamo(self) -> dict:
        item = {
            "tenant_id": self.tenant_id,
            "log_id": self.log_id,
            "event_type": self.event_type,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "created_at": self.created_at,
            "previous_hash": self.previous_hash,
            "hash_chain": self.hash_chain,
        }
        if self.resource_type:
            item["resource_type"] = self.resource_type
        if self.resource_id:
            item["resource_id"] = self.resource_id
        if self.details:
            item["details"] = self.details
        if self.ip_address:
            item["ip_address"] = self.ip_address
        return item


# ═══════════════════════════════════════════════════
# AI Agent Insight (generated by Claude agents)
# ═══════════════════════════════════════════════════
class Insight:
    """An AI-generated insight, anomaly alert, or forecast.
    These are produced by the three AI agents and consumed by the dashboard."""

    VALID_TYPES = {"anomaly", "daily_summary", "forecast", "recommendation", "alert"}
    VALID_SEVERITIES = {"info", "warning", "critical"}

    def __init__(self, tenant_id: str, insight_type: str,
                 title: str, summary: str, detail: str,
                 severity: str = "info",
                 data_points: Optional[dict] = None,
                 recommendations: Optional[list] = None,
                 agent_model: str = "claude-sonnet-4-20250514"):
        self.insight_id = ulid_sortable()
        self.tenant_id = tenant_id
        self.insight_type = insight_type
        self.title = title
        self.summary = summary
        self.detail = detail
        self.severity = severity
        self.data_points = data_points or {}
        self.recommendations = recommendations or []
        self.agent_model = agent_model
        self.generated_at = datetime.now(timezone.utc).isoformat()

    def to_dynamo(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "insight_id": self.insight_id,
            "insight_type": self.insight_type,
            "title": self.title,
            "summary": self.summary,
            "detail": self.detail,
            "severity": self.severity,
            "data_points": self.data_points,
            "recommendations": self.recommendations,
            "agent_model": self.agent_model,
            "generated_at": self.generated_at,
        }
