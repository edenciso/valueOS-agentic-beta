"""
ValueOS v0.1 — Dashboard Query API
Implementation Mapping: Reporting & Export Service + read endpoints from all pillars

This Lambda serves all GET endpoints for the dashboard:
  GET /api/v1/usage/summary     — Aggregated usage by provider/model
  GET /api/v1/costs/breakdown   — Cost breakdown by provider, model, time period
  GET /api/v1/anomalies         — AI-detected anomalies (from Insights table)
  GET /api/v1/insights          — Daily executive insights
  GET /api/v1/forecast          — Cost forecasts
  GET /api/v1/audit/logs        — Audit log query (paginated)

All queries are tenant-scoped: the tenant_id from the JWT becomes the
DynamoDB partition key, so a tenant can never see another tenant's data.
This is the "row-level security" approach from the Implementation Mapping
(Section 8, Cross-Cutting Concerns), adapted for DynamoDB.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../"))

from shared.middleware.auth import (
    api_handler, json_response, parse_query_params, DecimalEncoder
)

# ─────────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb")
timestream_query = boto3.client("timestream-query")

USAGE_TABLE = os.environ.get("USAGE_TABLE", "valueos-dev-usage-events")
COST_TABLE = os.environ.get("COST_TABLE", "valueos-dev-cost-records")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "valueos-dev-audit-log")
INSIGHTS_TABLE = os.environ.get("INSIGHTS_TABLE", "valueos-dev-insights")
TIMESTREAM_DB = os.environ.get("TIMESTREAM_DB", "valueos-dev")
TIMESTREAM_USAGE_TABLE = os.environ.get("TIMESTREAM_USAGE_TABLE", "llm_usage_metrics")


# ═══════════════════════════════════════════════════
# GET /api/v1/usage/summary
# ═══════════════════════════════════════════════════
@api_handler(required_permission="read")
def get_usage_summary(event, context, auth):
    """Return aggregated usage statistics for the tenant.
    
    Query params:
      - days: Number of days to look back (default: 7, max: 90)
      - provider: Filter by provider (optional)
    """
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]
    days = min(int(params.get("days", "7")), 90)
    provider_filter = params.get("provider")

    # Calculate the time boundary
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Query the cost records (which have pre-calculated dollar values)
    table = dynamodb.Table(COST_TABLE)
    query_kwargs = {
        "IndexName": "tenant-dateprovider-index",
        "KeyConditionExpression": Key("tenant_id").eq(tenant_id),
        "Limit": 10000,
    }

    response = table.query(**query_kwargs)
    items = response.get("Items", [])

    # Aggregate in-memory (for beta scale this is fine; prod uses Timestream SQL)
    totals = {
        "total_cost_usd": Decimal("0"),
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_events": 0,
        "by_provider": {},
        "by_model": {},
        "daily": {},
    }

    for item in items:
        if item.get("recorded_at", "") < since:
            continue
        if provider_filter and item.get("provider") != provider_filter:
            continue

        cost = item.get("total_cost_usd", Decimal("0"))
        provider = item.get("provider", "unknown")
        model = item.get("model_id", "unknown")
        date_str = item.get("recorded_at", "")[:10]

        totals["total_cost_usd"] += cost
        totals["total_input_tokens"] += int(item.get("input_tokens", 0))
        totals["total_output_tokens"] += int(item.get("output_tokens", 0))
        totals["total_events"] += 1

        # By provider
        if provider not in totals["by_provider"]:
            totals["by_provider"][provider] = {"cost_usd": Decimal("0"), "events": 0}
        totals["by_provider"][provider]["cost_usd"] += cost
        totals["by_provider"][provider]["events"] += 1

        # By model
        key = f"{provider}/{model}"
        if key not in totals["by_model"]:
            totals["by_model"][key] = {"cost_usd": Decimal("0"), "events": 0, "tokens": 0}
        totals["by_model"][key]["cost_usd"] += cost
        totals["by_model"][key]["events"] += 1
        totals["by_model"][key]["tokens"] += int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))

        # Daily trend
        if date_str not in totals["daily"]:
            totals["daily"][date_str] = {"cost_usd": Decimal("0"), "events": 0}
        totals["daily"][date_str]["cost_usd"] += cost
        totals["daily"][date_str]["events"] += 1

    return json_response(200, {
        "tenant_id": tenant_id,
        "period_days": days,
        "summary": {
            "total_cost_usd": totals["total_cost_usd"],
            "total_input_tokens": totals["total_input_tokens"],
            "total_output_tokens": totals["total_output_tokens"],
            "total_events": totals["total_events"],
            "avg_cost_per_event": (
                totals["total_cost_usd"] / totals["total_events"]
                if totals["total_events"] > 0 else Decimal("0")
            ),
        },
        "by_provider": totals["by_provider"],
        "by_model": totals["by_model"],
        "daily_trend": dict(sorted(totals["daily"].items())),
    })


# ═══════════════════════════════════════════════════
# GET /api/v1/costs/breakdown
# ═══════════════════════════════════════════════════
@api_handler(required_permission="read")
def get_cost_breakdown(event, context, auth):
    """Detailed cost breakdown with drill-down by provider, model, and time."""
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]
    days = min(int(params.get("days", "30")), 90)
    group_by = params.get("group_by", "provider")  # provider | model | daily

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    table = dynamodb.Table(COST_TABLE)

    response = table.query(
        IndexName="tenant-dateprovider-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id),
        Limit=10000,
    )
    items = [i for i in response.get("Items", []) if i.get("recorded_at", "") >= since]

    # Group and aggregate
    groups = {}
    for item in items:
        if group_by == "provider":
            key = item.get("provider", "unknown")
        elif group_by == "model":
            key = f"{item.get('provider', 'unknown')}/{item.get('model_id', 'unknown')}"
        else:
            key = item.get("recorded_at", "")[:10]

        if key not in groups:
            groups[key] = {
                "total_cost_usd": Decimal("0"),
                "input_cost_usd": Decimal("0"),
                "output_cost_usd": Decimal("0"),
                "total_tokens": 0,
                "event_count": 0,
            }
        g = groups[key]
        g["total_cost_usd"] += item.get("total_cost_usd", Decimal("0"))
        g["input_cost_usd"] += item.get("input_cost_usd", Decimal("0"))
        g["output_cost_usd"] += item.get("output_cost_usd", Decimal("0"))
        g["total_tokens"] += int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))
        g["event_count"] += 1

    return json_response(200, {
        "tenant_id": tenant_id,
        "period_days": days,
        "group_by": group_by,
        "breakdown": groups,
    })


# ═══════════════════════════════════════════════════
# GET /api/v1/anomalies
# ═══════════════════════════════════════════════════
@api_handler(required_permission="read")
def get_anomalies(event, context, auth):
    """Return AI-detected anomalies for the tenant."""
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]
    limit = min(int(params.get("limit", "20")), 100)

    table = dynamodb.Table(INSIGHTS_TABLE)
    response = table.query(
        IndexName="tenant-time-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id),
        FilterExpression=Attr("insight_type").eq("anomaly"),
        ScanIndexForward=False,
        Limit=limit,
    )

    return json_response(200, {
        "tenant_id": tenant_id,
        "anomalies": response.get("Items", []),
        "count": len(response.get("Items", [])),
    })


# ═══════════════════════════════════════════════════
# GET /api/v1/insights
# ═══════════════════════════════════════════════════
@api_handler(required_permission="read")
def get_insights(event, context, auth):
    """Return AI-generated executive insights."""
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]
    insight_type = params.get("type")  # anomaly | daily_summary | forecast | recommendation
    limit = min(int(params.get("limit", "20")), 100)

    table = dynamodb.Table(INSIGHTS_TABLE)
    query_kwargs = {
        "IndexName": "tenant-time-index",
        "KeyConditionExpression": Key("tenant_id").eq(tenant_id),
        "ScanIndexForward": False,
        "Limit": limit,
    }

    if insight_type:
        query_kwargs["FilterExpression"] = Attr("insight_type").eq(insight_type)

    response = table.query(**query_kwargs)

    return json_response(200, {
        "tenant_id": tenant_id,
        "insights": response.get("Items", []),
        "count": len(response.get("Items", [])),
    })


# ═══════════════════════════════════════════════════
# GET /api/v1/forecast
# ═══════════════════════════════════════════════════
@api_handler(required_permission="read")
def get_forecast(event, context, auth):
    """Return AI-generated cost forecasts."""
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]

    table = dynamodb.Table(INSIGHTS_TABLE)
    response = table.query(
        IndexName="tenant-time-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id),
        FilterExpression=Attr("insight_type").eq("forecast"),
        ScanIndexForward=False,
        Limit=5,
    )

    items = response.get("Items", [])

    return json_response(200, {
        "tenant_id": tenant_id,
        "forecasts": items,
        "latest": items[0] if items else None,
    })


# ═══════════════════════════════════════════════════
# GET /api/v1/audit/logs
# ═══════════════════════════════════════════════════
@api_handler(required_permission="admin")
def get_audit_logs(event, context, auth):
    """Query the immutable audit log (GF-007). Admin-only endpoint."""
    params = parse_query_params(event)
    tenant_id = auth["tenant_id"]
    limit = min(int(params.get("limit", "50")), 200)
    event_type_filter = params.get("event_type")

    table = dynamodb.Table(AUDIT_TABLE)
    query_kwargs = {
        "IndexName": "tenant-time-index",
        "KeyConditionExpression": Key("tenant_id").eq(tenant_id),
        "ScanIndexForward": False,
        "Limit": limit,
    }

    if event_type_filter:
        query_kwargs["FilterExpression"] = Attr("event_type").begins_with(event_type_filter)

    response = table.query(**query_kwargs)

    return json_response(200, {
        "tenant_id": tenant_id,
        "entries": response.get("Items", []),
        "count": len(response.get("Items", [])),
        "has_more": "LastEvaluatedKey" in response,
    })


# ═══════════════════════════════════════════════════
# LAMBDA ENTRY POINT — Route based on path
# ═══════════════════════════════════════════════════
def lambda_handler(event, context):
    """Route to the correct handler based on the HTTP path."""
    path = event.get("rawPath", event.get("path", ""))

    if "/usage/summary" in path:
        return get_usage_summary(event, context)
    elif "/costs/breakdown" in path:
        return get_cost_breakdown(event, context)
    elif "/anomalies" in path:
        return get_anomalies(event, context)
    elif "/insights" in path:
        return get_insights(event, context)
    elif "/forecast" in path:
        return get_forecast(event, context)
    elif "/audit/logs" in path:
        return get_audit_logs(event, context)
    else:
        return json_response(404, {"error": "Not Found", "path": path})
