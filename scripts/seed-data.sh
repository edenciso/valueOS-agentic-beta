#!/bin/bash
# ═══════════════════════════════════════════════════
# ValueOS v0.1 — Seed Data for Demo / Testing
# Loads 7 days of realistic LLM usage data via the API
# ═══════════════════════════════════════════════════
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-us-east-1}"
STACK_NAME="valueos-${STAGE}"

echo "→ Getting API endpoint..."
API_URL=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' --output text)

echo "  API: ${API_URL}"
echo ""
echo "→ Seeding 7 days of sample LLM usage data..."

# For demo: bypass auth with a direct DynamoDB write (or use a service token)
# In production, this would go through the authenticated API
TENANT="demo-tenant"
TABLE="valueos-${STAGE}-usage-events"

python3 -c "
import boto3, json, random, time, uuid
from datetime import datetime, timedelta, timezone

dynamodb = boto3.resource('dynamodb', region_name='${REGION}')
table = dynamodb.Table('${TABLE}')

models = [
    ('openai', 'gpt-4o', 800, 2000, 200, 1500),
    ('openai', 'gpt-4o-mini', 500, 3000, 100, 2000),
    ('anthropic', 'claude-sonnet-4-20250514', 700, 2500, 150, 1800),
    ('anthropic', 'claude-haiku-3-5', 400, 4000, 80, 2500),
    ('bedrock', 'anthropic.claude-3-5-sonnet', 600, 1500, 100, 1200),
    ('bedrock', 'meta.llama3-1-70b-instruct', 500, 2000, 200, 3000),
    ('google', 'gemini-2.0-flash', 300, 5000, 80, 3000),
]

print('  Generating events...')
count = 0
with table.batch_writer() as batch:
    for day_offset in range(7, 0, -1):
        base_date = datetime.now(timezone.utc) - timedelta(days=day_offset)
        # More events on weekdays
        is_weekday = base_date.weekday() < 5
        daily_events = random.randint(80, 150) if is_weekday else random.randint(20, 50)

        # Add a cost spike on day 3 (for anomaly detection demo)
        if day_offset == 3:
            daily_events = daily_events * 3

        for _ in range(daily_events):
            provider, model, min_in, max_in, min_out, max_out = random.choice(models)
            hour = random.randint(8, 22) if is_weekday else random.randint(10, 18)
            minute = random.randint(0, 59)
            ts = base_date.replace(hour=hour, minute=minute, second=random.randint(0, 59))

            batch.put_item(Item={
                'tenant_id': '${TENANT}',
                'event_id': f'{int(ts.timestamp() * 1000):012x}-{uuid.uuid4().hex[:16]}',
                'provider': provider,
                'model_id': model,
                'input_tokens': random.randint(min_in, max_in),
                'output_tokens': random.randint(min_out, max_out),
                'latency_ms': random.randint(200, 5000),
                'recorded_at': ts.isoformat(),
                'ttl': int(time.time()) + (90 * 24 * 3600),
            })
            count += 1

print(f'  ✓ Loaded {count} usage events for tenant ${TENANT}')
print(f'  → Cost engine will auto-calculate costs via DynamoDB Streams')

# Also create tenant config entry
config_table = dynamodb.Table('valueos-${STAGE}-tenant-config')
config_table.put_item(Item={
    'tenant_id': '${TENANT}',
    'config_key': 'settings',
    'company_name': 'Demo Corp',
    'created_at': datetime.now(timezone.utc).isoformat(),
})
print(f'  ✓ Created tenant config for ${TENANT}')
"

echo ""
echo "✓ Seed data loaded. The cost engine will process events automatically."
echo "  AI agents will generate insights on their next scheduled run."
echo "  To trigger immediately:"
echo "    aws lambda invoke --function-name valueos-${STAGE}-agent-anomaly /dev/null"
echo "    aws lambda invoke --function-name valueos-${STAGE}-agent-insights /dev/null"
echo "    aws lambda invoke --function-name valueos-${STAGE}-agent-forecaster /dev/null"
