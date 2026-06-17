# ValueOS Agentic Beta

## AI-Native Serverless Deployment for AWS

This is the minimal AI-native AWS deployment of ValueOS. It covers the Phase 1 Foundation requirements (VI-001, VI-002, GF-006, GF-007) plus early Phase 2 capabilities (VI-007 anomaly detection) using a fully serverless AWS architecture with an AI-agentic approach to accelerate development.

---

### What Ships in This v0.1

| PRD Requirement | What It Does | Implementation |
|-----------------|-------------|----------------|
| **VI-001** | Ingest LLM usage data (OpenAI, Anthropic, Bedrock) | API Gateway → Lambda → DynamoDB Streams → TimeStream |
| **VI-002** | Calculate token costs with real-time pricing | Cost Engine Lambda (event-driven from ingestion) |
| **VI-007** | Real-time anomaly detection for cost spikes | AI Agent: Claude-powered anomaly detector on schedule |
| **GF-006** | RBAC with SSO (Cognito + OIDC) | Amazon Cognito User Pool with JWT-based auth |
| **GF-007** | Immutable audit log | DynamoDB append-only table with hash-chain integrity |
| *Bonus* | AI-generated executive insights | AI Agent: Claude insight generator on daily schedule |
| *Bonus* | Predictive cost forecasting | AI Agent: Claude cost forecaster with trend analysis |

### Architecture Philosophy

**Serverless-first.** No EKS, no EC2, no containers to manage. Every component runs on managed AWS services that scale to zero when idle and auto-scale under load. This lets a 2-person team deploy and operate the platform.

**AI-agentic.** Instead of building complex rule engines and ML pipelines for anomaly detection, forecasting, and insight generation, we use Claude as the reasoning engine. Three AI agents run on EventBridge schedules, read from the data stores, and write structured analysis back. This replaces months of ML engineering with prompt engineering.

**Event-driven.** DynamoDB Streams feed the cost engine. EventBridge Pipes connect services. No polling, no cron, no orchestration overhead.

### AWS Services Used

- **API Gateway (HTTP API)** — Request routing, JWT validation, rate limiting
- **Lambda (Python 3.12)** — All compute (ingestion, cost engine, dashboard API, AI agents)
- **DynamoDB** — Usage events, cost records, audit log, tenant config
- **Timestream** — Time-series queries for dashboards and trend analysis
- **Cognito** — User pools, OIDC federation, JWT issuance
- **EventBridge** — Scheduled AI agent invocations, event routing
- **Secrets Manager** — API keys for LLM providers, Anthropic key
- **S3** — Generated reports, audit log archival
- **CloudWatch** — Observability, alarms, dashboards
- **SAM/CloudFormation** — Infrastructure as Code

---

### Project Structure

```
valueos-beta/
├── infra/                          # Terraform + SAM templates
│   ├── template.yaml               # Main SAM template (all resources)
│   ├── modules/                     # Terraform modules (alternative IaC)
│   └── environments/               # Per-stage configs
├── services/
│   ├── ingestion-pipeline/          # VI-001: LLM usage ingestion
│   │   └── src/handler.py
│   ├── cost-engine/                 # VI-002: Token cost calculation
│   │   └── src/handler.py
│   ├── dashboard-api/               # Dashboard query endpoints
│   │   └── src/handler.py
│   └── ai-agents/                   # AI-native agentic layer
│       ├── anomaly-detector/        # VI-007: Cost anomaly detection
│       │   └── src/handler.py
│       ├── insight-generator/       # Bonus: Executive AI insights
│       │   └── src/handler.py
│       └── cost-forecaster/         # Bonus: Predictive forecasting
│           └── src/handler.py
├── shared/
│   ├── utils/                       # Shared utilities
│   ├── models/                      # Data models & schemas
│   └── middleware/                   # Auth, logging, tenant isolation
├── scripts/
│   ├── deploy.sh                    # One-command deployment
│   ├── seed-data.sh                 # Load sample data for demo
│   └── test-integration.sh          # End-to-end smoke tests
└── docs/
    └── api-reference.md             # API documentation
```

### Deployment

```bash
# Prerequisites: AWS CLI configured, SAM CLI installed, Python 3.12
./scripts/deploy.sh --stage dev --region us-east-1
```

### Cost Estimate (Dev/Beta)

At beta scale (< 10K events/day, 3 tenants), the entire platform runs for approximately **$45–80/month** on AWS, thanks to the serverless architecture scaling to zero between requests.
