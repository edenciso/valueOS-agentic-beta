"""
ValueOS v0.1 — LLM Usage Ingestion Pipeline
PRD Requirement: VI-001 (Ingest usage data from LLM APIs)
Implementation Mapping: LLM Ingestion Pipeline Service (Section 2.1.1)

This Lambda handles two API Gateway routes:
  POST /api/v1/ingestion/llm-usage   — Single event ingestion
  POST /api/v1/ingestion/batch        — Batch ingestion (up to 100 events)

The write path is:
  API Gateway → This Lambda → DynamoDB (primary) + Timestream (analytics)
                                  ↓
                          DynamoDB Stream → Cost Engine Lambda (VI-002)

This dual-write pattern gives us:
  - DynamoDB: reliable, cost-effective storage with automatic cost engine trigger
  - Timestream: optimized time-series queries for the dashboard (avoids expensive scans)
"""
import json
import os
import sys
import time

import boto3

# Add shared modules to path (Lambda layers would be cleaner for prod)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../"))

from shared.models.schemas import UsageEvent
from shared.middleware.auth import (
    api_handler, json_response, parse_body, write_audit_entry
)

# ─────────────────────────────────────────────────
# AWS Clients (initialized once, reused across invocations)
# ─────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb")
timestream_write = boto3.client("timestream-write")

USAGE_TABLE = os.environ.get("USAGE_TABLE", "valueos-dev-usage-events")
TIMESTREAM_DB = os.environ.get("TIMESTREAM_DB", "valueos-dev")
TIMESTREAM_TABLE = os.environ.get("TIMESTREAM_USAGE_TABLE", "llm_usage_metrics")


def _write_to_dynamodb(events: list[UsageEvent]) -> dict:
    """Batch write usage events to DynamoDB.
    Uses batch_writer for efficient throughput (auto-batches into 25-item requests)."""
    table = dynamodb.Table(USAGE_TABLE)
    success_count = 0
    errors = []

    with table.batch_writer() as batch:
        for event in events:
            try:
                batch.put_item(Item=event.to_dynamo())
                success_count += 1
            except Exception as e:
                errors.append({"event_id": event.event_id, "error": str(e)})

    return {"success": success_count, "errors": errors}


def _write_to_timestream(events: list[UsageEvent]):
    """Write usage metrics to Timestream for time-series analytics.
    This is a best-effort write — Timestream failures should not block ingestion.
    The dashboard can fall back to DynamoDB queries if Timestream is unavailable."""
    records = [event.to_timestream_record() for event in events]
    try:
        # Timestream accepts up to 100 records per write
        for i in range(0, len(records), 100):
            batch = records[i:i + 100]
            timestream_write.write_records(
                DatabaseName=TIMESTREAM_DB,
                TableName=TIMESTREAM_TABLE,
                Records=batch,
                CommonAttributes={}
            )
    except timestream_write.exceptions.RejectedRecordsException as e:
        # Log rejected records but don't fail the whole request
        print(f"[WARN] Timestream rejected some records: {e}")
    except Exception as e:
        print(f"[WARN] Timestream write failed (non-blocking): {e}")


# ═══════════════════════════════════════════════════
# SINGLE EVENT INGESTION
# POST /api/v1/ingestion/llm-usage
# ═══════════════════════════════════════════════════
@api_handler(required_permission="write")
def ingest_single(event, context, auth):
    """Ingest a single LLM usage event.
    
    Expected body:
    {
        "provider": "openai",
        "model_id": "gpt-4o",
        "input_tokens": 1500,
        "output_tokens": 800,
        "latency_ms": 1200,          // optional
        "agent_id": "agent-123",     // optional
        "metadata": { ... }          // optional
    }
    """
    body = parse_body(event)
    tenant_id = auth["tenant_id"]

    # Create and validate the usage event
    usage_event = UsageEvent(
        tenant_id=tenant_id,
        provider=body.get("provider", ""),
        model_id=body.get("model_id", ""),
        input_tokens=int(body.get("input_tokens", 0)),
        output_tokens=int(body.get("output_tokens", 0)),
        latency_ms=body.get("latency_ms"),
        agent_id=body.get("agent_id"),
        user_id=auth["user_id"],
        metadata=body.get("metadata"),
    )

    validation_errors = usage_event.validate()
    if validation_errors:
        return json_response(400, {
            "error": "Validation Error",
            "details": validation_errors,
        })

    # Write to DynamoDB (primary store, triggers cost engine via Stream)
    result = _write_to_dynamodb([usage_event])

    # Best-effort write to Timestream (analytics)
    _write_to_timestream([usage_event])

    # Audit log entry (GF-007)
    write_audit_entry(
        tenant_id=tenant_id,
        event_type="llm.usage.recorded",
        actor_type="user" if auth["role"] != "system" else "integration",
        actor_id=auth["user_id"],
        action="ingested",
        resource_type="usage_event",
        resource_id=usage_event.event_id,
        details={
            "provider": usage_event.provider,
            "model_id": usage_event.model_id,
            "total_tokens": usage_event.input_tokens + usage_event.output_tokens,
        },
    )

    return json_response(201, {
        "status": "accepted",
        "event_id": usage_event.event_id,
        "recorded_at": usage_event.recorded_at,
    })


# ═══════════════════════════════════════════════════
# BATCH INGESTION
# POST /api/v1/ingestion/batch
# ═══════════════════════════════════════════════════
@api_handler(required_permission="write")
def ingest_batch(event, context, auth):
    """Ingest a batch of LLM usage events (up to 100 per request).
    
    Expected body:
    {
        "events": [
            { "provider": "openai", "model_id": "gpt-4o", "input_tokens": 1500, "output_tokens": 800 },
            { "provider": "anthropic", "model_id": "claude-sonnet-4-20250514", "input_tokens": 2000, "output_tokens": 1200 },
            ...
        ]
    }
    """
    body = parse_body(event)
    tenant_id = auth["tenant_id"]
    raw_events = body.get("events", [])

    if not raw_events:
        return json_response(400, {"error": "No events provided"})

    if len(raw_events) > 100:
        return json_response(400, {
            "error": "Batch too large",
            "message": "Maximum 100 events per batch request",
        })

    # Parse and validate all events
    usage_events = []
    validation_errors = []

    for i, raw in enumerate(raw_events):
        evt = UsageEvent(
            tenant_id=tenant_id,
            provider=raw.get("provider", ""),
            model_id=raw.get("model_id", ""),
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            latency_ms=raw.get("latency_ms"),
            agent_id=raw.get("agent_id"),
            user_id=auth["user_id"],
            metadata=raw.get("metadata"),
        )
        errors = evt.validate()
        if errors:
            validation_errors.append({"index": i, "errors": errors})
        else:
            usage_events.append(evt)

    if not usage_events:
        return json_response(400, {
            "error": "All events failed validation",
            "validation_errors": validation_errors,
        })

    # Write valid events to DynamoDB
    result = _write_to_dynamodb(usage_events)

    # Best-effort Timestream write
    _write_to_timestream(usage_events)

    # Audit log for batch
    write_audit_entry(
        tenant_id=tenant_id,
        event_type="llm.usage.batch_recorded",
        actor_type="user" if auth["role"] != "system" else "integration",
        actor_id=auth["user_id"],
        action="ingested",
        resource_type="usage_event_batch",
        details={
            "total_events": len(raw_events),
            "accepted": result["success"],
            "rejected": len(validation_errors) + len(result["errors"]),
        },
    )

    return json_response(201, {
        "status": "accepted",
        "accepted": result["success"],
        "rejected": len(validation_errors),
        "event_ids": [e.event_id for e in usage_events],
        "validation_errors": validation_errors[:10],  # Cap to avoid huge responses
    })


# ═══════════════════════════════════════════════════
# LAMBDA ENTRY POINT — Routes to correct handler
# ═══════════════════════════════════════════════════
def lambda_handler(event, context):
    """Main Lambda entry point. Routes based on the HTTP path."""
    path = event.get("rawPath", event.get("path", ""))

    if "/batch" in path:
        return ingest_batch(event, context)
    else:
        return ingest_single(event, context)
