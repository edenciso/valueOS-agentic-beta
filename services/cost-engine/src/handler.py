"""
ValueOS v0.1 — Cost Calculation Engine
PRD Requirement: VI-002 (Calculate token costs with real-time pricing)
Implementation Mapping: Cost Attribution Engine (Section 2.1.2)

This Lambda is TRIGGERED BY DynamoDB Streams — it runs automatically
whenever new usage events land in the UsageEventsTable.

The event flow:
  UsageEventsTable (DynamoDB Stream)
      → This Lambda
          → CostRecordsTable (DynamoDB)
          → Timestream (cost metrics for dashboards)
          → AuditLogTable (GF-007 compliance)

This is the "event-driven" pattern from the architecture doc: no polling,
no cron, no REST call needed. The cost engine fires within seconds of
ingestion, giving near-real-time cost visibility.
"""
import json
import os
import sys
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer

# Add shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../"))

from shared.models.schemas import CostRecord
from shared.utils.pricing import calculate_cost, PRICING_VERSION
from shared.middleware.auth import write_audit_entry

# ─────────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb")
timestream_write = boto3.client("timestream-write")
deserializer = TypeDeserializer()

COST_TABLE = os.environ.get("COST_TABLE", "valueos-dev-cost-records")
TIMESTREAM_DB = os.environ.get("TIMESTREAM_DB", "valueos-dev")
TIMESTREAM_TABLE = os.environ.get("TIMESTREAM_USAGE_TABLE", "llm_usage_metrics")


def _deserialize_dynamo_record(record: dict) -> dict:
    """Convert DynamoDB Stream record (with type descriptors) to plain Python dict.
    DynamoDB Streams use a different format than the resource API:
    {'S': 'value'} instead of just 'value'."""
    return {
        key: deserializer.deserialize(value)
        for key, value in record.items()
    }


def _write_cost_to_timestream(cost: CostRecord):
    """Write cost metric to Timestream for time-series dashboards."""
    try:
        timestream_write.write_records(
            DatabaseName=TIMESTREAM_DB,
            TableName=TIMESTREAM_TABLE,
            Records=[{
                "Dimensions": [
                    {"Name": "tenant_id", "Value": cost.tenant_id},
                    {"Name": "provider", "Value": cost.provider},
                    {"Name": "model_id", "Value": cost.model_id},
                    {"Name": "metric_type", "Value": "cost"},
                ],
                "MeasureName": "cost_usd",
                "MeasureValueType": "MULTI",
                "MeasureValues": [
                    {"Name": "input_cost", "Value": str(float(cost.input_cost_usd)), "Type": "DOUBLE"},
                    {"Name": "output_cost", "Value": str(float(cost.output_cost_usd)), "Type": "DOUBLE"},
                    {"Name": "total_cost", "Value": str(float(cost.total_cost_usd)), "Type": "DOUBLE"},
                    {"Name": "input_tokens", "Value": str(cost.input_tokens), "Type": "BIGINT"},
                    {"Name": "output_tokens", "Value": str(cost.output_tokens), "Type": "BIGINT"},
                ],
                "Time": cost.recorded_at,
                "TimeUnit": "MILLISECONDS",
            }],
        )
    except Exception as e:
        # Timestream failure is non-blocking — DynamoDB is the source of truth
        print(f"[WARN] Timestream cost write failed: {e}")


# ═══════════════════════════════════════════════════
# MAIN HANDLER — DynamoDB Stream trigger
# ═══════════════════════════════════════════════════
def lambda_handler(event, context):
    """Process DynamoDB Stream events and calculate costs.
    
    Each stream record contains a NEW_IMAGE of an inserted usage event.
    We calculate the cost, write a CostRecord, and update metrics.
    
    This handler processes records in batches (configured to 25 in SAM template)
    for throughput efficiency.
    """
    cost_table = dynamodb.Table(COST_TABLE)
    records_processed = 0
    errors = []

    for record in event.get("Records", []):
        try:
            # Only process INSERT events (we filter in SAM, but double-check)
            if record["eventName"] != "INSERT":
                continue

            # Deserialize the DynamoDB Stream record
            new_image = record["dynamodb"]["NewImage"]
            usage = _deserialize_dynamo_record(new_image)

            tenant_id = usage["tenant_id"]
            provider = usage["provider"]
            model_id = usage["model_id"]
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            event_id = usage["event_id"]
            recorded_at = usage["recorded_at"]

            # ─── CORE LOGIC: Calculate the cost ───
            cost_result = calculate_cost(
                provider=provider,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            # Create the cost record
            cost_record = CostRecord(
                tenant_id=tenant_id,
                event_id=event_id,
                provider=provider,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_cost_usd=cost_result["input_cost_usd"],
                output_cost_usd=cost_result["output_cost_usd"],
                total_cost_usd=cost_result["total_cost_usd"],
                recorded_at=recorded_at,
                pricing_version=cost_result["pricing_version"],
                agent_id=usage.get("agent_id"),
                user_id=usage.get("user_id"),
            )

            # Write to DynamoDB (this is the authoritative cost record)
            cost_table.put_item(Item=cost_record.to_dynamo())

            # Write to Timestream (analytics, best-effort)
            _write_cost_to_timestream(cost_record)

            # Audit trail for cost calculation (GF-007)
            write_audit_entry(
                tenant_id=tenant_id,
                event_type="cost.calculated",
                actor_type="system",
                actor_id="cost-engine",
                action="calculated",
                resource_type="cost_record",
                resource_id=cost_record.cost_record_id,
                details={
                    "source_event_id": event_id,
                    "provider": provider,
                    "model_id": model_id,
                    "total_cost_usd": float(cost_record.total_cost_usd),
                    "is_estimated": cost_result["is_estimated"],
                    "pricing_version": PRICING_VERSION,
                },
            )

            records_processed += 1

            # Log estimated pricing for operational visibility
            if cost_result["is_estimated"]:
                print(f"[INFO] Used estimated pricing for {provider}/{model_id} "
                      f"— add to pricing table for accuracy")

        except Exception as e:
            # Log error but continue processing other records
            # DynamoDB Streams will retry failed records via the bisect-on-error behavior
            print(f"[ERROR] Failed to process record: {e}")
            errors.append(str(e))

    print(f"[INFO] Cost engine processed {records_processed} records, "
          f"{len(errors)} errors")

    # If all records failed, raise to trigger DynamoDB Stream retry
    if errors and records_processed == 0:
        raise Exception(f"All {len(errors)} records failed: {errors[0]}")

    return {
        "processed": records_processed,
        "errors": len(errors),
    }
