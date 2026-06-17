"""
ValueOS v0.1 — AI Agent: Cost Forecaster
PRD Requirement: VI-008 (Predictive cost forecasting with scenario modeling)
Runs daily at 7am UTC via EventBridge (after the insight generator).

Uses Claude to analyze historical trends and generate 30/60/90-day forecasts.
The AI-agentic approach replaces traditional time-series forecasting models
(Prophet, ARIMA, etc.) with LLM reasoning — ships in days, not months.
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

COST_TABLE = os.environ.get("COST_TABLE")
TENANT_TABLE = os.environ.get("TENANT_TABLE")
INSIGHTS_TABLE = os.environ.get("INSIGHTS_TABLE")
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


def get_historical_daily_costs(tenant_id, days_back=60):
    """Get daily cost aggregates for trend analysis."""
    table = dynamodb.Table(COST_TABLE)
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    resp = table.query(IndexName="tenant-dateprovider-index",
        KeyConditionExpression=Key("tenant_id").eq(tenant_id), Limit=50000)
    items = [i for i in resp.get("Items", []) if i.get("recorded_at", "") >= since]

    daily = {}
    for item in items:
        date = item.get("recorded_at", "")[:10]
        daily.setdefault(date, {"cost_usd": 0, "events": 0, "tokens": 0, "by_provider": {}})
        d = daily[date]
        cost = float(item.get("total_cost_usd", 0))
        d["cost_usd"] += cost
        d["events"] += 1
        d["tokens"] += int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))
        prov = item.get("provider", "unknown")
        d["by_provider"][prov] = round(d["by_provider"].get(prov, 0) + cost, 4)

    for d in daily.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
    return dict(sorted(daily.items()))


def generate_forecast(tenant_id, daily_data, api_key):
    """Use Claude to generate cost forecasts based on historical trends."""
    import urllib.request

    prompt = f"""You are the ValueOS Cost Forecasting Agent. Analyze historical LLM cost data 
and generate 30/60/90-day cost projections with confidence intervals.

TENANT: {tenant_id}
HISTORICAL DAILY COSTS (up to 60 days):
{json.dumps(daily_data, indent=2)}

Generate forecasts. Respond ONLY with JSON:
{{
  "current_monthly_run_rate_usd": number,
  "trend_direction": "increasing|stable|decreasing",
  "trend_weekly_change_pct": number,
  "forecasts": {{
    "30_day": {{
      "projected_cost_usd": number,
      "confidence_low_usd": number,
      "confidence_high_usd": number,
      "key_drivers": ["driver 1", "driver 2"]
    }},
    "60_day": {{
      "projected_cost_usd": number,
      "confidence_low_usd": number,
      "confidence_high_usd": number,
      "key_drivers": ["driver 1"]
    }},
    "90_day": {{
      "projected_cost_usd": number,
      "confidence_low_usd": number,
      "confidence_high_usd": number,
      "key_drivers": ["driver 1"]
    }}
  }},
  "scenarios": {{
    "optimistic": {{
      "90_day_cost_usd": number,
      "assumption": "description of optimistic scenario"
    }},
    "baseline": {{
      "90_day_cost_usd": number,
      "assumption": "current trends continue"
    }},
    "pessimistic": {{
      "90_day_cost_usd": number,
      "assumption": "description of pessimistic scenario"
    }}
  }},
  "cost_optimization_potential_usd": number,
  "optimization_suggestions": ["specific suggestion 1", "suggestion 2"],
  "narrative": "2-3 paragraph forecast narrative with specific projections and reasoning"
}}

Rules:
- Base projections on observable trends in the data (growth rate, seasonality)
- Confidence intervals should widen for longer horizons
- Be specific with dollar amounts
- If data is sparse (<7 days), flag low confidence and provide wider intervals
- Cost optimization suggestions should reference specific models or patterns"""

    body = json.dumps({"model": AGENT_MODEL, "max_tokens": 3000,
        "messages": [{"role": "user", "content": prompt}]}).encode()
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
        print(f"[ERROR] Claude forecast failed: {e}")
        return None


def lambda_handler(event, context):
    print("[INFO] Cost Forecaster Agent starting")
    api_key = get_anthropic_key()
    tenants = get_active_tenants() or ["demo-tenant"]
    insights_table = dynamodb.Table(INSIGHTS_TABLE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated = 0

    for tenant_id in tenants:
        try:
            daily_data = get_historical_daily_costs(tenant_id, days_back=60)
            if len(daily_data) < 3:
                print(f"[INFO] Tenant {tenant_id}: insufficient data for forecasting")
                continue

            result = generate_forecast(tenant_id, daily_data, api_key)
            if not result:
                continue

            insight = Insight(tenant_id=tenant_id, insight_type="forecast",
                title=f"Cost Forecast — {result.get('trend_direction', 'stable').title()} Trend",
                summary=f"30-day projection: ${result.get('forecasts', {}).get('30_day', {}).get('projected_cost_usd', 0):,.2f} "
                        f"(current run rate: ${result.get('current_monthly_run_rate_usd', 0):,.2f}/mo)",
                detail=result.get("narrative", ""),
                severity="warning" if result.get("trend_direction") == "increasing" else "info",
                data_points={
                    "current_monthly_run_rate": result.get("current_monthly_run_rate_usd"),
                    "trend_direction": result.get("trend_direction"),
                    "trend_weekly_change_pct": result.get("trend_weekly_change_pct"),
                    "forecasts": result.get("forecasts"),
                    "scenarios": result.get("scenarios"),
                    "optimization_potential": result.get("cost_optimization_potential_usd"),
                },
                recommendations=result.get("optimization_suggestions", []),
                agent_model=AGENT_MODEL)
            insights_table.put_item(Item=insight.to_dynamo())

            write_audit_entry(tenant_id=tenant_id, event_type="forecast.generated",
                actor_type="agent", actor_id="cost-forecaster", action="forecasted",
                resource_type="insight", resource_id=insight.insight_id,
                details={"trend": result.get("trend_direction"), "date": today})

            generated += 1
            print(f"[INFO] Tenant {tenant_id}: forecast generated — {result.get('trend_direction')}")
        except Exception as e:
            print(f"[ERROR] Tenant {tenant_id}: {e}")

    print(f"[INFO] Cost Forecaster complete: {generated}/{len(tenants)}")
    return {"tenants_processed": len(tenants), "forecasts_generated": generated}
