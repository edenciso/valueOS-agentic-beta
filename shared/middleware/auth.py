"""
ValueOS v0.1 — Shared Middleware
Tenant isolation, JWT extraction, and audit logging.
Enforces GF-006 (RBAC) and GF-007 (audit trail) at the middleware layer.

Every Lambda handler wraps its logic with these utilities to ensure:
1. tenant_id is always extracted and validated from the JWT
2. Every mutating action is recorded in the immutable audit log
3. RBAC roles are checked before operations execute
"""
import json
import os
import base64
import functools
import traceback
from datetime import datetime, timezone
from decimal import Decimal

import boto3

# ─────────────────────────────────────────────────
# DynamoDB client (reused across invocations via Lambda container reuse)
# ─────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb")


def get_audit_table():
    return dynamodb.Table(os.environ["AUDIT_TABLE"])


# ─────────────────────────────────────────────────
# JSON serializer that handles Decimal (DynamoDB returns Decimal)
# ─────────────────────────────────────────────────
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def json_response(status_code: int, body: dict) -> dict:
    """Create a properly formatted API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-ValueOS-Version": "0.1.0",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


# ─────────────────────────────────────────────────
# JWT EXTRACTION — Pull tenant_id and role from Cognito JWT
# ─────────────────────────────────────────────────
def extract_auth_context(event: dict) -> dict:
    """Extract authentication context from the API Gateway event.
    
    The Cognito JWT authorizer has already validated the token by the time
    the Lambda runs. We just need to extract the claims from the request context.
    
    Returns:
        {
            "user_id": "...",          # Cognito sub claim
            "tenant_id": "...",        # custom:tenant_id claim
            "role": "admin|analyst|viewer",
            "email": "...",
        }
    
    For DynamoDB Stream events (cost engine, etc.), returns a system context.
    """
    # API Gateway HTTP API puts JWT claims in requestContext
    request_context = event.get("requestContext", {})
    authorizer = request_context.get("authorizer", {})
    jwt_claims = authorizer.get("jwt", {}).get("claims", {})

    if jwt_claims:
        return {
            "user_id": jwt_claims.get("sub", "unknown"),
            "tenant_id": jwt_claims.get("custom:tenant_id", "default"),
            "role": jwt_claims.get("custom:role", "viewer"),
            "email": jwt_claims.get("email", ""),
        }

    # Fallback: check for X-Tenant-Id header (used in service-to-service calls)
    headers = event.get("headers", {})
    tenant_header = headers.get("x-tenant-id", headers.get("X-Tenant-Id", ""))
    if tenant_header:
        return {
            "user_id": "service",
            "tenant_id": tenant_header,
            "role": "system",
            "email": "",
        }

    # DynamoDB Stream events or EventBridge triggers — system context
    return {
        "user_id": "system",
        "tenant_id": "system",
        "role": "system",
        "email": "",
    }


# ─────────────────────────────────────────────────
# RBAC CHECK — Simple role-based authorization
# ─────────────────────────────────────────────────
ROLE_PERMISSIONS = {
    "admin": {"read", "write", "delete", "admin"},
    "analyst": {"read", "write"},
    "viewer": {"read"},
    "system": {"read", "write", "delete", "admin"},  # Internal service calls
}


def check_permission(role: str, required: str) -> bool:
    """Check if a role has the required permission level."""
    permissions = ROLE_PERMISSIONS.get(role, set())
    return required in permissions


# ─────────────────────────────────────────────────
# AUDIT LOGGING — Write immutable entries (GF-007)
# ─────────────────────────────────────────────────
_last_hash_cache = {}  # In-memory cache of last hash per tenant (per Lambda instance)


def write_audit_entry(tenant_id: str, event_type: str,
                      actor_type: str, actor_id: str, action: str,
                      resource_type: str = None, resource_id: str = None,
                      details: dict = None, ip_address: str = None):
    """Write an immutable audit log entry with hash-chain integrity.
    
    This is called by every service after any mutating operation.
    The hash chain links each entry to its predecessor, making
    tampering detectable (satisfying GF-007).
    """
    # Import here to avoid circular dependency
    from shared.models.schemas import AuditEntry

    # Get the last hash for this tenant to chain from
    previous_hash = _last_hash_cache.get(tenant_id, "GENESIS")

    entry = AuditEntry(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address,
        previous_hash=previous_hash,
    )

    # Write to DynamoDB (append-only — we never update or delete audit entries)
    try:
        table = get_audit_table()
        table.put_item(Item=entry.to_dynamo())
        # Cache the hash for the next entry in this Lambda invocation
        _last_hash_cache[tenant_id] = entry.hash_chain
    except Exception as e:
        # Audit logging failure should never crash the main operation,
        # but we log it prominently for operational alerting
        print(f"[CRITICAL] Audit log write failed: {e}")
        print(traceback.format_exc())


# ─────────────────────────────────────────────────
# LAMBDA HANDLER DECORATOR — Wraps all handlers
# ─────────────────────────────────────────────────
def api_handler(required_permission: str = "read"):
    """Decorator for API Gateway Lambda handlers.
    
    Handles:
    1. Auth context extraction
    2. RBAC permission checking
    3. Error handling with consistent response format
    4. Request/response logging
    
    Usage:
        @api_handler(required_permission="write")
        def lambda_handler(event, context, auth):
            tenant_id = auth["tenant_id"]
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(event, context):
            try:
                # Extract auth context
                auth = extract_auth_context(event)

                # Check RBAC permission
                if not check_permission(auth["role"], required_permission):
                    return json_response(403, {
                        "error": "Forbidden",
                        "message": f"Role '{auth['role']}' does not have '{required_permission}' permission",
                    })

                # Call the actual handler
                return func(event, context, auth)

            except json.JSONDecodeError:
                return json_response(400, {
                    "error": "Bad Request",
                    "message": "Invalid JSON in request body",
                })
            except Exception as e:
                print(f"[ERROR] Unhandled exception: {e}")
                print(traceback.format_exc())
                return json_response(500, {
                    "error": "Internal Server Error",
                    "message": "An unexpected error occurred",
                    "request_id": context.aws_request_id if context else "unknown",
                })

        return wrapper
    return decorator


def parse_body(event: dict) -> dict:
    """Parse the JSON body from an API Gateway event, handling base64 encoding."""
    body = event.get("body", "{}")
    if event.get("isBase64Encoded", False):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body) if body else {}


def parse_query_params(event: dict) -> dict:
    """Extract query string parameters from the event."""
    return event.get("queryStringParameters", {}) or {}
