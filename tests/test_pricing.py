"""Rate-card arithmetic and missing-information boundaries."""
import unittest
from decimal import Decimal, localcontext

from codex_mem.pricing import price_event, snapshot_metadata


def event(**changes):
    row = dict(model="gpt-6-astra", model_source="turn_context", service_tier="standard", service_tier_source="token_usage_record", input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0, output_tokens=100, reasoning_output_tokens=50, total_tokens=1100)
    row.update(changes)
    return row


class PricingTests(unittest.TestCase):
    def test_cached_input_and_reasoning_are_subsets(self):
        priced = price_event(event())
        self.assertEqual(Decimal(".0078"), Decimal(priced["api_equivalent_usd"]["selected"]))
        self.assertEqual(Decimal(".195"), Decimal(priced["estimated_codex_credits"]["selected"]))
        self.assertEqual(priced["api_equivalent_usd"], price_event(event(reasoning_output_tokens=0))["api_equivalent_usd"])

    def test_cache_writes_use_their_rate_without_double_count(self):
        priced = price_event(event(cached_input_tokens=700, cache_write_input_tokens=100))
        self.assertEqual(Decimal(".00895"), Decimal(priced["api_equivalent_usd"]["selected"]))
        self.assertIsNone(priced["estimated_codex_credits"]["selected"])
        self.assertEqual("cache_write_credit_rate_unavailable", priced["estimated_codex_credits"]["reason"])

    def test_long_context_starts_strictly_above_272000_and_applies_to_full_request(self):
        short = price_event(event(input_tokens=272000, cached_input_tokens=0, total_tokens=272100))
        long = price_event(event(input_tokens=272001, cached_input_tokens=0, total_tokens=272101))
        self.assertFalse(short["long_context"])
        self.assertTrue(long["long_context"])
        self.assertEqual(Decimal("2.725"), Decimal(short["api_equivalent_usd"]["selected"]))
        self.assertEqual(Decimal("5.44752"), Decimal(long["api_equivalent_usd"]["selected"]))
        self.assertEqual(Decimal("68.12525"), Decimal(long["estimated_codex_credits"]["selected"]))

    def test_api_fast_and_codex_fast_have_different_multipliers(self):
        standard = price_event(event())
        fast = price_event(event(service_tier="priority"))
        self.assertEqual(Decimal(standard["api_equivalent_usd"]["selected"]) * 2, Decimal(fast["api_equivalent_usd"]["selected"]))
        self.assertEqual(Decimal(standard["estimated_codex_credits"]["selected"]) * Decimal("2.5"), Decimal(fast["estimated_codex_credits"]["selected"]))

    def test_confirmed_tier_wins_but_requested_tier_is_only_an_estimate(self):
        self.assertEqual("standard", price_event(event(requested_service_tier="fast"))["tier"]["selected"])
        requested = price_event(event(service_tier=None, requested_service_tier="priority"))
        self.assertEqual("requested", requested["tier"]["status"])
        self.assertEqual("fast", requested["tier"]["selected"])

    def test_unknown_tier_has_scenarios_and_no_selected_amount(self):
        for unknown in (None, "auto", "ultrafast"):
            with self.subTest(tier=unknown):
                priced = price_event(event(service_tier=unknown))
                self.assertIsNone(priced["api_equivalent_usd"]["selected"])
                self.assertEqual(Decimal(".0078"), Decimal(priced["api_equivalent_usd"]["standard"]))
                self.assertEqual(Decimal(".0156"), Decimal(priced["api_equivalent_usd"]["fast"]))
        self.assertIsNone(price_event(event(service_tier="ultrafast", requested_service_tier="standard"))["api_equivalent_usd"]["selected"])

    def test_old_or_settings_tier_without_provider_evidence_remains_requested(self):
        for source in (None, "thread_settings"):
            with self.subTest(source=source):
                priced = price_event(event(service_tier="fast", service_tier_source=source))
                self.assertIsNone(priced["tier"]["confirmed"])
                self.assertEqual("requested", priced["tier"]["status"])

    def test_unknown_model_is_unpriced_including_auto_review(self):
        for model in (None, "codex-auto-review", "gpt-6-astra-unknown-snapshot"):
            with self.subTest(model=model):
                priced = price_event(event(model=model))
                self.assertEqual("unknown_model_rate", priced["api_equivalent_usd"]["reason"])
                self.assertIsNone(priced["api_equivalent_usd"]["standard"])

    def test_invalid_or_missing_counters_never_become_free_usage(self):
        changes = [{"cached_input_tokens": 1001}, {"reasoning_output_tokens": 101}, {"input_tokens": True}, {"output_tokens": -1}, {"total_tokens": 1111}, {"cached_input_tokens": None}]
        for change in changes:
            with self.subTest(change=change):
                priced = price_event(event(**change))
                self.assertIsNone(priced["tokens"])
                self.assertIsNone(priced["api_equivalent_usd"]["selected"])

    def test_partial_counter_quality_is_not_assumed_to_be_uncached(self):
        for quality in ("response_partial_counters", "root_metadata_conflict_partial_counters"):
            with self.subTest(quality=quality):
                priced = price_event(event(quality=quality, cached_input_tokens=0))
                self.assertEqual(1000, priced["tokens"]["input_tokens"])
                self.assertIsNone(priced["api_equivalent_usd"]["standard"])
                self.assertEqual("partial_usage_counters", priced["api_equivalent_usd"]["reason"])

    def test_all_four_reviewed_models_match_independent_known_costs(self):
        expected = {"gpt-6-astra": ".0078", "gpt-5.6-sol": ".00312", "gpt-5.6-terra": ".00176", "gpt-5.6-luna": ".000176"}
        for model, amount in expected.items():
            with self.subTest(model=model):
                self.assertEqual(Decimal(amount), Decimal(price_event(event(model=model))["api_equivalent_usd"]["selected"]))

    def test_exact_arithmetic_does_not_depend_on_caller_decimal_precision(self):
        expected = price_event(event(input_tokens=123456789, cached_input_tokens=123000000, total_tokens=123456889))
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(expected, price_event(event(input_tokens=123456789, cached_input_tokens=123000000, total_tokens=123456889)))

    def test_snapshot_has_sources_and_no_invented_historical_effective_date(self):
        snapshot = snapshot_metadata()
        self.assertEqual("2026-09-12", snapshot["reviewed_at"])
        self.assertIsNone(snapshot["effective_from"])
        self.assertIn("reprice_with_snapshot", snapshot["historical_policy"])
        self.assertEqual(4, len(snapshot["model_sources"]))


if __name__ == "__main__":
    unittest.main()
