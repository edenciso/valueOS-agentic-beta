"""
ValueOS v0.1 — AI Agent: Executive Insight Generator
Generates daily AI-written executive summaries for each tenant.
Runs daily at 6am UTC via EventBridge Scheduler.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../"))
from shared.models.schemas import Insight
from shared.middleware.auth import write_audit_entry

dynamodb = boto3.resource("dynamodb")
secrets_client = boto3.client("secretsmanager")
s3_client = boto3.client("s3")

COST_TABLE = os.environ.get("COST_TABLE")
TENANT_TABLE = os.environ.get("TENANT_TABLE")
INSIGHTS_TABLE = os.environ.get("INSIGHTS_TABLE")
REPORT_BUCKET = os.environ.get("REPORT_BUCKET")
ANTHROPIC_SECRET_ARN = os.environ.get("ANTHROPIC_SECRET_ARN")
AGENT_MODEL = "claude-sonnet-4-20250514"


def get_anthropic_key():
    resp = secrets_client.get_secret_value(SecretId=ANTHROPIC_SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    return secret.get("api_key", secret.get("ANTHROPIC_API_KEY", ""))


def get_active_tenants():
    table = dynamodb.Table(TENANT_TABLE)
    resp = table.scan(FilterExpression=Key("config_key").eq("settings"), ProjectionExpression="tenant_id")
    return [i["tenant_id"] for i in resp.get("Items", [])]


def get_period_costs(tenant_id, days_back):
    table = dynamodb.Table(COST_TABLE)
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    resp = table.query(IndexName="tenant-dateprovider-index", KeyConditionExpression=Key("tenant_id").eq(tenant_id), Limit=10000)
    items = [i for i in resp.get("Items", []) if i.get("recorded_at", "") >= since]
    daily = {}
    for item in items:
        date = item.get("recorded_at", "")[:10]
        daily.setdefault(date, {"cost_usd": 0, "events": 0, "providers": {}, "models": {}})
        d = daily[date]
        cost = float(item.get("total_cost_usd", 0))
        d["cost_usd"] += cost
        d["events"] += 1
        prov = item.get("provider", "unknown")
        d["providers"][prov] = round(d["providers"].get(prov, 0) + cost, 4)
        model = item.get("model_id", "unknown")
        d["models"][model] = round(d["models"].get(model, 0) + cost, 4)
    for d in daily.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
    return dict(sorted(daily.items()))


def get_recent_anomalies(tenant_id):
    table = dynamodb.Table(INSIGHTS_TABLE)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    resp = table.query(IndexName="tenant-time-index", KeyConditionExpression=Key("tenant_id").eq(tenant_id), Limit=20)
    return [{"title": i["title"], "severity": i["severity"], "summary": i["summary"]}
            for i in resp.get("Items", []) if i.get("insight_type") == "anomaly" and i.get("generated_at", "") >= since]


def generate_daily_insight(tenant_id, daily_data, anomalies, api_key):
    import urllib.request
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"""You are the ValueOS Executive Insight Agent. Generate a daily intelligence briefing.

TENANT: {tenant_id}
DATE: {today}

DAILY COST DATA (last 30 days):
{json.dumps(daily_data, indent=2)}

ANOMALIES (last 24h):
{json.dumps(anomalies, indent=2)}

Respond ONLY with JSON:
{{
  "headline": "12-word max data-driven headline",
  "summary": "2-3 sentence executive summary with specific numbers.",
  "key_metrics": {{"today_cost_usd": 0, "week_over_week_change_pct": 0, "top_cost_driver": "", "total_events_today": 0, "cost_efficiency_trend": "stable"}},
  "narrative": "3-4 paragraph detailed analysis with numbers and percentages.",
  "board_ready_paragraph": "Single polished paragraph for board presentations.",
  "recommendations": ["Specific recommendation 1", "Specific recommendation 2", "Specific recommendation 3"],
  "risk_flags": []
}}"""

    body = json.dumps({"model": AGENT_MODEL, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode())
            content = result["content"][0]["text"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)
    except Exception as e:
        print(f"[ERROR] Claude insight generation failed: {e}")
        return None


def lambda_handler(event, context):
    print("[INFO] Insight Generator Agent starting")
    api_key = get_anthropic_key()
    tenants = get_active_tenants() or ["demo-tenant"]
    insights_table = dynamodb.Table(INSIGHTS_TABLE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated = 0

    for tenant_id in tenants:
        try:
            daily_data = get_period_costs(tenant_id, 30)
            anomalies = get_recent_anomalies(tenant_id)
            if not daily_data:
                continue

            result = generate_daily_insight(tenant_id, daily_data, anomalies, api_key)
            if not result:
                continue

            insight = Insight(tenant_id=tenant_id, insight_type="daily_summary",
                title=result.get("headline", f"Daily Summary — {today}"),
                summary=result.get("summary", ""), detail=result.get("narrative", ""),
                severity="info", data_points=result.get("key_metrics", {}),
                recommendations=result.get("recommendations", []), agent_model=AGENT_MODEL)
            insights_table.put_item(Item=insight.to_dynamo())

            if result.get("board_ready_paragraph"):
                board = Insight(tenant_id=tenant_id, insight_type="recommendation",
                    title=f"Board-Ready Summary — {today}", summary=result["board_ready_paragraph"],
                    detail=result.get("narrative", ""), severity="info",
                    data_points=result.get("key_metrics", {}), recommendations=result.get("risk_flags", []))
                insights_table.put_item(Item=board.to_dynamo())

            # Archive to S3
            try:
                s3_client.put_object(Bucket=REPORT_BUCKET, Key=f"insights/{tenant_id}/{today}/daily.json",
                    Body=json.dumps(result, indent=2), ContentType="application/json", ServerSideEncryption="AES256")
            except Exception:
                pass

            write_audit_entry(tenant_id=tenant_id, event_type="insight.generated",
                actor_type="agent", actor_id="insight-generator", action="generated",
                resource_type="insight", resource_id=insight.insight_id,
                details={"headline": result.get("headline", ""), "date": today})

            generated += 1
            print(f"[INFO] Tenant {tenant_id}: {result.get('headline', 'N/A')}")
        except Exception as e:
            print(f"[ERROR] Tenant {tenant_id}: {e}")

    print(f"[INFO] Insight Generator complete: {generated}/{len(tenants)}")
    return {"tenants_processed": len(tenants), "insights_generated": generated}
