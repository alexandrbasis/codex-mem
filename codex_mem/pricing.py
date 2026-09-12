"""Deterministic token estimates from a reviewed, versioned public rate card.

This reprices observed usage using the named snapshot. It is not an invoice or
a claim about the rates in effect when a historical response was generated.
Prices and public credit rates deliberately use separate calculations.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Mapping

SNAPSHOT_VERSION = "openai-2026-09-12.1"
SNAPSHOT_REVIEWED_AT = "2026-09-12"
API_SOURCE = "https://developers.openai.com/api/docs/pricing"
CREDIT_SOURCE = "https://learn.chatgpt.com/docs/pricing#token-rates"
SPEED_SOURCE = "https://learn.chatgpt.com/docs/agent-configuration/speed"
MILLION = Decimal(1_000_000)
LONG_CONTEXT_THRESHOLD = 272_000
TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


@dataclass(frozen=True)
class Rates:
    input: Decimal
    cache_read: Decimal
    cache_write: Decimal
    output: Decimal
    credit_input: Decimal
    credit_cache_read: Decimal
    credit_output: Decimal


def _rates(*values: str) -> Rates:
    return Rates(*(Decimal(value) for value in values))


# Exact public model identifiers only. An unlisted alias is not assumed to be
# the same backend or price. All numbers are per million tokens.
CATALOG = {
    "gpt-6-astra": _rates("10", "1", "12.5", "50", "250", "25", "1250"),
    "gpt-5.6-sol": _rates("4", ".4", "5", "20", "100", "10", "500"),
    "gpt-5.6-terra": _rates("2", ".2", "2.5", "12", "50", "5", "300"),
    "gpt-5.6-luna": _rates(".2", ".02", ".25", "1.2", "5", ".5", "30"),
}


def decimal_text(value: Decimal) -> str:
    """Exact JSON-safe decimal; no binary floats or per-event rounding."""
    return format(value, "f")


def snapshot_metadata() -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "reviewed_at": SNAPSHOT_REVIEWED_AT,
        "historical_policy": "reprice_with_snapshot_not_historical_invoice",
        "effective_from": None,
        "supported_models": sorted(CATALOG),
        "sources": [API_SOURCE, CREDIT_SOURCE, SPEED_SOURCE],
        "model_sources": {model: f"https://developers.openai.com/api/docs/models/{model}" for model in CATALOG},
        "api_long_context": {"input_tokens_greater_than": LONG_CONTEXT_THRESHOLD, "input_and_cache_multiplier": "2", "output_multiplier": "1.5", "applies_to": "entire_response"},
        "api_fast_multiplier": "2",
        "codex_fast_multiplier": "2.5",
        "codex_credit_policy": "published_flat_token_rates; cache_write_rate_unavailable",
        "exclusions": ["subscription_payments", "actual_credit_purchases", "taxes", "regional_processing_uplift", "tool_fees", "account_discounts"],
        "notes": [
            "Sol promotional rates were published as available at least through 2026-11-21; refresh the snapshot to use later rates.",
            "The rate card review date does not establish an effective date for historical billing.",
            "Model and requested tier attribution in local logs is not proof of the provider's billed model or tier.",
        ],
    }


def normalize_tier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {"standard": "standard", "default": "standard", "fast": "fast", "priority": "fast"}.get(value.strip().lower())


def token_counts(event: Mapping[str, Any]) -> dict[str, int] | None:
    """Validate subset semantics before using any monetary formula."""
    counts = {field: event.get(field) for field in TOKEN_FIELDS}
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1 for value in counts.values()):
        return None
    if counts["cached_input_tokens"] + counts["cache_write_input_tokens"] > counts["input_tokens"]:
        return None
    if counts["reasoning_output_tokens"] > counts["output_tokens"]:
        return None
    if counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]:
        return None
    return counts


def has_partial_counters(event: Mapping[str, Any]) -> bool:
    quality = event.get("quality")
    return isinstance(quality, str) and "partial_counters" in quality


def _amounts(standard: Decimal | None, multiplier: Decimal, tier: str | None, reason: str | None) -> dict[str, Any]:
    fast = standard * multiplier if standard is not None else None
    selected = fast if tier == "fast" else standard if tier == "standard" else None
    return {
        "selected": decimal_text(selected) if selected is not None else None,
        "standard": decimal_text(standard) if standard is not None else None,
        "fast": decimal_text(fast) if fast is not None else None,
        "reason": reason or ("unknown_service_tier" if tier is None else None),
    }


def price_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Price one response, retaining uncertainty instead of inventing zeroes.

    ``service_tier`` means provider-confirmed tier in the v2 usage schema.
    Callers reading the older schema must move its tier to requested_service_tier.
    Unknown tiers have explicit hypothetical Standard/Fast scenarios. These are
    not guaranteed bounds for unsupported service tiers or unknown account fees.
    """
    with localcontext() as context:
        context.prec = 50
        return _price_event(event)


def _price_event(event: Mapping[str, Any]) -> dict[str, Any]:
    counts = token_counts(event)
    model = event.get("model")
    rates = CATALOG.get(model) if isinstance(model, str) else None
    tier_source = event.get("service_tier_source")
    provider_evidence = isinstance(tier_source, str) and tier_source in {"token_usage_record", "response", "provider"}
    confirmed_raw = event.get("service_tier") if provider_evidence else None
    requested_raw = event.get("requested_service_tier") or (event.get("service_tier") if not provider_evidence else None)
    confirmed = normalize_tier(confirmed_raw)
    requested = normalize_tier(requested_raw)
    # An explicitly reported, unsupported provider tier must not be replaced by
    # a different requested tier. It remains unpriced until its rate is known.
    tier = confirmed if confirmed_raw else requested
    tier_status = "confirmed" if confirmed else "requested" if tier else "unknown"
    partial_counters = has_partial_counters(event)
    reason = "invalid_or_unavailable_usage" if counts is None else "partial_usage_counters" if partial_counters else "unknown_model_rate" if rates is None else None
    api_standard = credit_standard = None
    long_context = counts is not None and counts["input_tokens"] > LONG_CONTEXT_THRESHOLD
    if counts is not None and rates is not None and not partial_counters:
        uncached = counts["input_tokens"] - counts["cached_input_tokens"] - counts["cache_write_input_tokens"]
        input_cost = (uncached * rates.input + counts["cached_input_tokens"] * rates.cache_read + counts["cache_write_input_tokens"] * rates.cache_write)
        output_cost = counts["output_tokens"] * rates.output
        api_standard = (input_cost * (2 if long_context else 1) + output_cost * (Decimal("1.5") if long_context else 1)) / MILLION
        if counts["cache_write_input_tokens"] == 0:
            credit_standard = (uncached * rates.credit_input + counts["cached_input_tokens"] * rates.credit_cache_read + counts["output_tokens"] * rates.credit_output) / MILLION
    credit_reason = reason or ("cache_write_credit_rate_unavailable" if counts and counts["cache_write_input_tokens"] else None)
    return {
        "model": model,
        "model_rate_known": rates is not None,
        "model_source": event.get("model_source") or "unknown",
        "tier": {"confirmed": confirmed_raw, "requested": requested_raw, "selected": tier, "status": tier_status, "source": tier_source if confirmed_raw else event.get("requested_service_tier_source") or ("legacy_usage_schema" if event.get("service_tier") else None)},
        "tokens": counts,
        "long_context": long_context,
        "api_equivalent_usd": _amounts(api_standard, Decimal(2), tier, reason),
        "estimated_codex_credits": _amounts(credit_standard, Decimal("2.5"), tier, credit_reason),
    }
