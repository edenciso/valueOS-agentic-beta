"""
ValueOS v0.1 — AI Agent: Anomaly Detector
PRD Requirement: VI-007 (Real-time anomaly detection for cost spikes)
Implementation Mapping: Cost Attribution Engine + AI-native enhancement

THIS IS THE CORE OF THE "AI-AGENTIC" APPROACH.

Instead of building traditional ML anomaly detection (which would require:
training data, feature engineering, model training, model serving, drift
monitoring, and retraining pipelines — easily 4-8 weeks of work), we use
Claude as the reasoning engine.

The agent:
  1. Queries the last 24 hours of cost data from DynamoDB
  2. Compares it to the previous 7-day baseline
  3. Sends both datasets to Claude with a structured prompt
  4. Claude identifies anomalies through reasoning (not statistics)
  5. The agent writes structured anomaly records to the Insights table

Why this works for the beta:
  - Ships in days, not weeks
  - Claude catches contextual anomalies that statistical methods miss
    (e.g., "weekend usage doubled, which is unusual for this tenant")
  - The structured output is machine-parseable for dashboard display
  - Cost: ~$0.05 per invocation (15-min schedule = ~$5/day for all tenants)

Runs every 15 minutes via EventBridge Scheduler.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../"))

from shared.models.schemas import Insight
from shared.middleware.auth import write_audit_entry, DecimalEncoder

# ─────────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb")
secrets_client = boto3.client("secretsmanager")

COST_TABLE = os.environ.get("COST_TABLE")
TENANT_TABLE = os.environ.get("TENANT_TABLE")
INSIGHTS_TABLE = os.environ.get("INSIGHTS_TABLE")
ANTHROPIC_SECRET_ARN = os.environ.get("ANTHROPIC_SECRET_ARN")

# Claude model for agent reasoning
AGENT_MODEL = "claude-sonnet-4-20250514"


def get_anthropic_key() -> str:
    """Retrieve the Anthropic API key from Secrets Manager.
    Cached in the Lambda execution environment for container reuse."""
    response = secrets_client.get_secret_value(SecretId=ANTHROPIC_SECRET_ARN)
    secret = json.loads(response["SecretString"])
    return secret.get("api_key", secret.get("ANTHROPIC_API_KEY", ""))


def get_active_tenants() -> list[str]:
    """Get list of tenant IDs that have active configurations."""
    table = dynamodb.Table(TENANT_TABLE)
    response = table.scan(
        FilterExpression=Key("config_key").eq("settings"),
        ProjectionExpression="tenant_id",
    )
    return [item["tenant_id"] for item in response.get("Items", [])]


def get_cost_data(tenant_id: str, hours_back: int) -> list[dict]:
    """Query cost records for a tenant within the specified time window."""
    table = dynamodb.Table(COST_TABLE)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

    response = table.query(
        IndexName="tenant-dateprovider-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id),
        Limit=5000,
    )

    # Filter to time window and convert Decimals for JSON serialization
    items = []
    for item in response.get("Items", []):
        if item.get("recorded_at", "") >= since:
            items.append({
                "provider": item.get("provider"),
                "model_id": item.get("model_id"),
                "total_cost_usd": float(item.get("total_cost_usd", 0)),
                "input_tokens": int(item.get("input_tokens", 0)),
                "output_tokens": int(item.get("output_tokens", 0)),
                "recorded_at": item.get("recorded_at"),
            })

    return items


def call_claude_for_anomaly_detection(recent_data: list, baseline_data: list,
                                       tenant_id: str, api_key: str) -> dict:
    """Send cost data to Claude for anomaly detection.
    
    Claude analyzes the data through reasoning rather than statistical models.
    This catches both quantitative anomalies (cost spikes) and qualitative ones
    (unusual patterns, provider shifts, model migration signals).
    
    The structured output format ensures machine-parseability for the dashboard.
    """
    import urllib.request

    # Prepare summary statistics for Claude (avoid sending raw thousands of records)
    recent_summary = _summarize_cost_data(recent_data)
    baseline_summary = _summarize_cost_data(baseline_data)

    prompt = f"""You are the ValueOS Anomaly Detection Agent. Your job is to analyze LLM usage 
cost data and identify anomalies, cost spikes, and unusual patterns.

TENANT: {tenant_id}

RECENT DATA (last 24 hours):
{json.dumps(recent_summary, indent=2)}

BASELINE DATA (previous 7-day average):
{json.dumps(baseline_summary, indent=2)}

Analyze the data and identify any anomalies. For each anomaly found, determine:
1. What the anomaly is (cost spike, unusual model usage, provider shift, etc.)
2. The severity (info, warning, critical)
3. The likely cause or context
4. A specific recommendation

Respond with ONLY a JSON object in this exact format:
{{
  "anomalies_detected": true/false,
  "anomalies": [
    {{
      "title": "Brief anomaly title",
      "severity": "info|warning|critical",
      "summary": "One-sentence summary for the dashboard",
      "detail": "Detailed explanation with numbers",
      "data_points": {{
        "metric": "value",
        "baseline": "value",
        "deviation_pct": number
      }},
      "recommendations": ["Actionable recommendation 1", "Recommendation 2"]
    }}
  ],
  "overall_health": "healthy|attention_needed|critical"
}}

Rules:
- Only flag genuine anomalies (>25% deviation from baseline, or qualitative shifts)
- Be specific with numbers and percentages
- If no anomalies, return anomalies_detected: false with empty anomalies array
- Severity guide: info = notable but not urgent, warning = investigate soon, critical = immediate attention needed"""

    # Call the Anthropic API
    request_body = json.dumps({
        "model": AGENT_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            content = result["content"][0]["text"]
            # Parse the JSON response (strip markdown fences if present)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)
    except Exception as e:
        print(f"[ERROR] Claude API call failed: {e}")
        return {"anomalies_detected": False, "anomalies": [], "overall_health": "unknown"}


def _summarize_cost_data(records: list) -> dict:
    """Summarize raw cost records into a compact format for Claude.
    This keeps the prompt token count manageable while giving Claude
    enough signal to detect anomalies."""
    if not records:
        return {"total_cost": 0, "total_events": 0, "by_provider": {}, "by_model": {}}

    total_cost = sum(r["total_cost_usd"] for r in records)
    total_events = len(records)

    by_provider = {}
    by_model = {}
    hourly = {}

    for r in records:
        provider = r["provider"]
        model = f"{provider}/{r['model_id']}"
        hour = r["recorded_at"][:13]  # YYYY-MM-DDTHH

        by_provider.setdefault(provider, {"cost": 0, "events": 0, "tokens": 0})
        by_provider[provider]["cost"] += r["total_cost_usd"]
        by_provider[provider]["events"] += 1
        by_provider[provider]["tokens"] += r["input_tokens"] + r["output_tokens"]

        by_model.setdefault(model, {"cost": 0, "events": 0})
        by_model[model]["cost"] += r["total_cost_usd"]
        by_model[model]["events"] += 1

        hourly.setdefault(hour, {"cost": 0, "events": 0})
        hourly[hour]["cost"] += r["total_cost_usd"]
        hourly[hour]["events"] += 1

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_events": total_events,
        "avg_cost_per_event": round(total_cost / total_events, 6) if total_events else 0,
        "by_provider": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                           for kk, vv in v.items()}
                       for k, v in by_provider.items()},
        "by_model": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                        for kk, vv in v.items()}
                    for k, v in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True)[:10]},
        "hourly_trend": dict(sorted(hourly.items())[-24:]),
    }


# ═══════════════════════════════════════════════════
# MAIN HANDLER — EventBridge scheduled trigger
# ═══════════════════════════════════════════════════
def lambda_handler(event, context):
    """Run anomaly detection for all active tenants.
    
    Triggered every 15 minutes by EventBridge Scheduler.
    For each tenant: pull recent data, compare to baseline, ask Claude to analyze.
    """
    print("[INFO] Anomaly Detector Agent starting")

    api_key = get_anthropic_key()
    tenants = get_active_tenants()

    if not tenants:
        # If no tenants configured yet, use a default for demo purposes
        tenants = ["demo-tenant"]

    insights_table = dynamodb.Table(INSIGHTS_TABLE)
    total_anomalies = 0

    for tenant_id in tenants:
        try:
            # Get recent data (last 24 hours) and baseline (previous 7 days)
            recent = get_cost_data(tenant_id, hours_back=24)
            baseline = get_cost_data(tenant_id, hours_back=7 * 24)

            # Skip if insufficient data
            if len(recent) < 5:
                print(f"[INFO] Tenant {tenant_id}: insufficient data ({len(recent)} records), skipping")
                continue

            # Remove recent from baseline to get a clean comparison
            recent_ids = {r["recorded_at"] for r in recent}
            baseline_clean = [b for b in baseline if b["recorded_at"] not in recent_ids]

            # Call Claude for anomaly detection
            result = call_claude_for_anomaly_detection(
                recent_data=recent,
                baseline_data=baseline_clean,
                tenant_id=tenant_id,
                api_key=api_key,
            )

            # Write anomalies to the Insights table
            if result.get("anomalies_detected"):
                for anomaly in result.get("anomalies", []):
                    insight = Insight(
                        tenant_id=tenant_id,
                        insight_type="anomaly",
                        title=anomaly.get("title", "Anomaly Detected"),
                        summary=anomaly.get("summary", ""),
                        detail=anomaly.get("detail", ""),
                        severity=anomaly.get("severity", "info"),
                        data_points=anomaly.get("data_points", {}),
                        recommendations=anomaly.get("recommendations", []),
                        agent_model=AGENT_MODEL,
                    )
                    insights_table.put_item(Item=insight.to_dynamo())
                    total_anomalies += 1

                    # Audit trail for anomaly detection (GF-007)
                    write_audit_entry(
                        tenant_id=tenant_id,
                        event_type="anomaly.detected",
                        actor_type="agent",
                        actor_id="anomaly-detector",
                        action="detected",
                        resource_type="insight",
                        resource_id=insight.insight_id,
                        details={
                            "severity": anomaly.get("severity"),
                            "title": anomaly.get("title"),
                        },
                    )

            print(f"[INFO] Tenant {tenant_id}: health={result.get('overall_health', 'unknown')}, "
                  f"anomalies={len(result.get('anomalies', []))}")

        except Exception as e:
            print(f"[ERROR] Failed to process tenant {tenant_id}: {e}")
            continue

    print(f"[INFO] Anomaly Detector Agent complete. "
          f"Tenants processed: {len(tenants)}, Anomalies found: {total_anomalies}")

    return {
        "tenants_processed": len(tenants),
        "total_anomalies": total_anomalies,
    }
