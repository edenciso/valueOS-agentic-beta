#!/bin/bash
# ═══════════════════════════════════════════════════
# ValueOS v0.1 — One-Command Deployment
# Deploys the entire serverless stack to AWS
# ═══════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──
STAGE="${1:-dev}"
REGION="${2:-us-east-1}"
STACK_NAME="valueos-${STAGE}"
TEMPLATE="infra/template.yaml"

echo "╔══════════════════════════════════════════════╗"
echo "║  ValueOS v0.1 — Serverless Deployment        ║"
echo "║  Stage: ${STAGE}  |  Region: ${REGION}            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Validate prerequisites ──
echo "→ Checking prerequisites..."
command -v aws >/dev/null 2>&1 || { echo "✗ AWS CLI not installed"; exit 1; }
command -v sam >/dev/null 2>&1 || { echo "✗ SAM CLI not installed"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ Python 3 not installed"; exit 1; }
echo "  ✓ All prerequisites met"

# ── Step 2: Create/verify Anthropic API key in Secrets Manager ──
SECRET_NAME="valueos/${STAGE}/anthropic-api-key"
echo ""
echo "→ Checking Anthropic API key secret..."
SECRET_ARN=$(aws secretsmanager describe-secret \
    --secret-id "${SECRET_NAME}" \
    --region "${REGION}" \
    --query 'ARN' --output text 2>/dev/null || true)

if [ -z "${SECRET_ARN}" ] || [ "${SECRET_ARN}" == "None" ]; then
    echo "  ⚠ Secret '${SECRET_NAME}' not found."
    echo "  Creating placeholder secret... (update with real key before using AI agents)"
    SECRET_ARN=$(aws secretsmanager create-secret \
        --name "${SECRET_NAME}" \
        --region "${REGION}" \
        --secret-string '{"api_key":"sk-ant-REPLACE-WITH-YOUR-KEY"}' \
        --query 'ARN' --output text)
    echo "  ✓ Secret created: ${SECRET_ARN}"
    echo "  ⚠ UPDATE THIS SECRET with your real Anthropic API key:"
    echo "    aws secretsmanager put-secret-value \\"
    echo "      --secret-id ${SECRET_NAME} \\"
    echo "      --secret-string '{\"api_key\":\"sk-ant-your-real-key\"}'"
else
    echo "  ✓ Secret exists: ${SECRET_ARN}"
fi

# ── Step 3: Copy shared modules into each Lambda's source ──
echo ""
echo "→ Copying shared modules into Lambda packages..."
for svc_dir in services/*/src services/ai-agents/*/src; do
    if [ -d "${svc_dir}" ]; then
        mkdir -p "${svc_dir}/shared/models" "${svc_dir}/shared/middleware" "${svc_dir}/shared/utils"
        cp shared/models/*.py "${svc_dir}/shared/models/" 2>/dev/null || true
        cp shared/middleware/*.py "${svc_dir}/shared/middleware/" 2>/dev/null || true
        cp shared/utils/*.py "${svc_dir}/shared/utils/" 2>/dev/null || true
        touch "${svc_dir}/shared/__init__.py" "${svc_dir}/shared/models/__init__.py" \
              "${svc_dir}/shared/middleware/__init__.py" "${svc_dir}/shared/utils/__init__.py"
        echo "  ✓ ${svc_dir}"
    fi
done

# ── Step 4: SAM Build ──
echo ""
echo "→ Building SAM application..."
sam build \
    --template-file "${TEMPLATE}" \
    --build-dir .aws-sam/build \
    --use-container \
    --parallel

echo "  ✓ Build complete"

# ── Step 5: SAM Deploy ──
echo ""
echo "→ Deploying to AWS (${REGION})..."
sam deploy \
    --template-file .aws-sam/build/template.yaml \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --parameter-overrides \
        Stage="${STAGE}" \
        AnthropicApiKeyArn="${SECRET_ARN}" \
        CognitoDomain="valueos-beta" \
    --no-confirm-changeset \
    --no-fail-on-empty-changeset \
    --tags \
        Project=ValueOS \
        Stage="${STAGE}" \
        ManagedBy=SAM

echo ""
echo "  ✓ Deployment complete"

# ── Step 6: Print outputs ──
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Deployment Outputs                          ║"
echo "╚══════════════════════════════════════════════╝"
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table

echo ""
echo "════════════════════════════════════════════════"
echo "Next steps:"
echo "  1. Update Anthropic API key in Secrets Manager (if not done)"
echo "  2. Create a test user: aws cognito-idp admin-create-user ..."
echo "  3. Run integration tests: ./scripts/test-integration.sh"
echo "  4. Load sample data: ./scripts/seed-data.sh"
echo "════════════════════════════════════════════════"
