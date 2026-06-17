"""
ValueOS v0.1 — LLM Pricing Engine
Maps to PRD requirement VI-002: Calculate token costs with real-time pricing.

This module contains the pricing lookup table for all supported LLM providers.
Prices are in USD per 1M tokens. The beta uses a static table updated monthly;
the full ValueOS v1.0 will pull from provider APIs in real-time.

The pricing_version field on CostRecords tracks which price table was used,
enabling retroactive recalculation if prices change.
"""
from decimal import Decimal


# ═══════════════════════════════════════════════════
# PRICING TABLE — USD per 1M tokens (February 2026)
# Source: Provider pricing pages, checked monthly
# ═══════════════════════════════════════════════════
PRICING_VERSION = "2026-02"

PRICING_TABLE = {
    # ── OpenAI ──
    "openai": {
        "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
        "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
        "gpt-4-turbo": {"input": Decimal("10.00"), "output": Decimal("30.00")},
        "gpt-4": {"input": Decimal("30.00"), "output": Decimal("60.00")},
        "gpt-3.5-turbo": {"input": Decimal("0.50"), "output": Decimal("1.50")},
        "o1": {"input": Decimal("15.00"), "output": Decimal("60.00")},
        "o1-mini": {"input": Decimal("3.00"), "output": Decimal("12.00")},
        "o3-mini": {"input": Decimal("1.10"), "output": Decimal("4.40")},
        # Embedding models (output tokens = 0 for embeddings)
        "text-embedding-3-small": {"input": Decimal("0.02"), "output": Decimal("0.00")},
        "text-embedding-3-large": {"input": Decimal("0.13"), "output": Decimal("0.00")},
    },

    # ── Anthropic ──
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": Decimal("3.00"), "output": Decimal("15.00")},
        "claude-opus-4-20250514": {"input": Decimal("15.00"), "output": Decimal("75.00")},
        "claude-haiku-3-5": {"input": Decimal("0.80"), "output": Decimal("4.00")},
        "claude-3-5-sonnet-20241022": {"input": Decimal("3.00"), "output": Decimal("15.00")},
        "claude-3-opus-20240229": {"input": Decimal("15.00"), "output": Decimal("75.00")},
        "claude-3-haiku-20240307": {"input": Decimal("0.25"), "output": Decimal("1.25")},
    },

    # ── Google (Gemini via Vertex AI) ──
    "google": {
        "gemini-2.0-flash": {"input": Decimal("0.10"), "output": Decimal("0.40")},
        "gemini-2.0-pro": {"input": Decimal("1.25"), "output": Decimal("10.00")},
        "gemini-1.5-pro": {"input": Decimal("1.25"), "output": Decimal("5.00")},
        "gemini-1.5-flash": {"input": Decimal("0.075"), "output": Decimal("0.30")},
    },

    # ── AWS Bedrock ──
    "bedrock": {
        # Bedrock wraps multiple providers; prices reflect Bedrock's on-demand rates
        "anthropic.claude-3-5-sonnet": {"input": Decimal("3.00"), "output": Decimal("15.00")},
        "anthropic.claude-3-haiku": {"input": Decimal("0.25"), "output": Decimal("1.25")},
        "amazon.titan-text-express": {"input": Decimal("0.20"), "output": Decimal("0.60")},
        "amazon.titan-text-lite": {"input": Decimal("0.15"), "output": Decimal("0.20")},
        "meta.llama3-1-70b-instruct": {"input": Decimal("0.72"), "output": Decimal("0.72")},
        "meta.llama3-1-8b-instruct": {"input": Decimal("0.22"), "output": Decimal("0.22")},
        "mistral.mistral-large": {"input": Decimal("4.00"), "output": Decimal("12.00")},
        "cohere.command-r-plus": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    },

    # ── Azure OpenAI ──
    "azure_openai": {
        # Azure prices generally match OpenAI's, sometimes with regional variation
        "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
        "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
        "gpt-4-turbo": {"input": Decimal("10.00"), "output": Decimal("30.00")},
        "gpt-4": {"input": Decimal("30.00"), "output": Decimal("60.00")},
    },
}

# Default pricing for models we don't have specific pricing for.
# This ensures we never skip a cost calculation — we flag it for review instead.
DEFAULT_PRICING = {"input": Decimal("5.00"), "output": Decimal("15.00")}


def calculate_cost(provider: str, model_id: str,
                   input_tokens: int, output_tokens: int) -> dict:
    """Calculate the cost of an LLM API call.
    
    Args:
        provider: LLM provider name (openai, anthropic, etc.)
        model_id: Model identifier (e.g., gpt-4o, claude-sonnet-4-20250514)
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
    
    Returns:
        {
            "input_cost_usd": Decimal,
            "output_cost_usd": Decimal,
            "total_cost_usd": Decimal,
            "pricing_version": str,
            "is_estimated": bool,  # True if we used default pricing
        }
    """
    provider_prices = PRICING_TABLE.get(provider.lower(), {})

    # Try exact model match first, then prefix match for versioned model names
    model_prices = provider_prices.get(model_id)
    if not model_prices:
        # Try prefix matching: "gpt-4o-2024-08-06" should match "gpt-4o"
        for known_model, prices in provider_prices.items():
            if model_id.startswith(known_model):
                model_prices = prices
                break

    is_estimated = model_prices is None
    if is_estimated:
        model_prices = DEFAULT_PRICING

    # Calculate: (tokens / 1,000,000) * price_per_1M_tokens
    input_cost = (Decimal(str(input_tokens)) / Decimal("1000000")) * model_prices["input"]
    output_cost = (Decimal(str(output_tokens)) / Decimal("1000000")) * model_prices["output"]
    total_cost = input_cost + output_cost

    return {
        "input_cost_usd": input_cost.quantize(Decimal("0.000001")),
        "output_cost_usd": output_cost.quantize(Decimal("0.000001")),
        "total_cost_usd": total_cost.quantize(Decimal("0.000001")),
        "pricing_version": PRICING_VERSION,
        "is_estimated": is_estimated,
    }


def get_model_price_info(provider: str, model_id: str) -> dict:
    """Get the pricing info for a specific model (for dashboard display)."""
    provider_prices = PRICING_TABLE.get(provider.lower(), {})
    model_prices = provider_prices.get(model_id, DEFAULT_PRICING)
    return {
        "provider": provider,
        "model_id": model_id,
        "input_per_1m_tokens": float(model_prices["input"]),
        "output_per_1m_tokens": float(model_prices["output"]),
        "pricing_version": PRICING_VERSION,
    }
