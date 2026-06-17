# ValueOS v0.1 — API Reference

## Base URL

```
https://{api-id}.execute-api.{region}.amazonaws.com/{stage}
```

## Authentication

All endpoints require a Bearer JWT token from Cognito:

```
Authorization: Bearer {id_token}
```

The JWT must include `custom:tenant_id` and `custom:role` claims.

---

## Ingestion API

### POST `/api/v1/ingestion/llm-usage`

Ingest a single LLM usage event. **PRD: VI-001**

**Permission:** `write`

**Request Body:**
```json
{
  "provider": "openai",           // Required: openai|anthropic|google|bedrock|azure_openai
  "model_id": "gpt-4o",           // Required: model identifier
  "input_tokens": 1500,           // Required: prompt token count
  "output_tokens": 800,           // Required: completion token count
  "latency_ms": 1200,             // Optional: API call latency
  "agent_id": "agent-123",        // Optional: originating agent
  "metadata": {}                  // Optional: provider-specific metadata
}
```

**Response (201):**
```json
{
  "status": "accepted",
  "event_id": "018d7a3b...",
  "recorded_at": "2026-02-06T12:00:00Z"
}
```

### POST `/api/v1/ingestion/batch`

Ingest up to 100 events in a single request.

**Request Body:**
```json
{
  "events": [
    { "provider": "openai", "model_id": "gpt-4o", "input_tokens": 1500, "output_tokens": 800 },
    { "provider": "anthropic", "model_id": "claude-sonnet-4-20250514", "input_tokens": 2000, "output_tokens": 1200 }
  ]
}
```

---

## Dashboard API

### GET `/api/v1/usage/summary`

Aggregated usage statistics. **Permission:** `read`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 7 | Lookback period (max 90) |
| `provider` | string | - | Filter by provider |

### GET `/api/v1/costs/breakdown`

Detailed cost breakdown. **Permission:** `read`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Lookback period |
| `group_by` | string | provider | `provider` \| `model` \| `daily` |

### GET `/api/v1/anomalies`

AI-detected cost anomalies. **Permission:** `read`

### GET `/api/v1/insights`

AI-generated executive insights. **Permission:** `read`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | - | `anomaly` \| `daily_summary` \| `forecast` \| `recommendation` |
| `limit` | int | 20 | Max results (max 100) |

### GET `/api/v1/forecast`

AI-generated cost forecasts (30/60/90 day). **Permission:** `read`

### GET `/api/v1/audit/logs`

Immutable audit trail (GF-007). **Permission:** `admin`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results (max 200) |
| `event_type` | string | - | Filter by event type prefix |

---

## AI Agents (Scheduled — not user-callable)

| Agent | Schedule | Description | PRD |
|-------|----------|-------------|-----|
| Anomaly Detector | Every 15 min | Detects cost spikes via Claude reasoning | VI-007 |
| Insight Generator | Daily 6am UTC | Executive daily summaries via Claude | VI-006 |
| Cost Forecaster | Daily 7am UTC | 30/60/90-day projections via Claude | VI-008 |
