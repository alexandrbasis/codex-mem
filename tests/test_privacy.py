"""Regression coverage for the privacy boundary before SQLite persistence."""

from __future__ import annotations

import unittest

from codex_mem.privacy import REDACTED, redact_tags, redact_text


class PrivacyTests(unittest.TestCase):
    def test_redacts_prefixed_env_and_json_assignments(self) -> None:
        raw = (
            "DATABASE_PASSWORD=hunter2 OPENAI_API_KEY=not-sk-shaped "
            "CUSTOM_ACCESS_TOKEN=abc123\n"
            '{"db_password":"hunter2","nested":{"api_key":"plain-secret"}}'
        )

        result = redact_text(raw)

        for secret in ("hunter2", "not-sk-shaped", "abc123", "plain-secret"):
            self.assertNotIn(secret, result)
        self.assertIn("DATABASE_PASSWORD=" + REDACTED, result)
        self.assertIn('"db_password":"' + REDACTED + '"', result)

    def test_redacts_bearer_common_tokens_and_database_url_userinfo(self) -> None:
        # Synthetic token shape, assembled so the repository contains no token literal.
        token_body = "abcdefghijklmnopqrstuvwxyz123456"
        raw = (
            "Bearer abcdefghijklmnop "
            f"ghp_{token_body} "
            "postgres://alice:correct-horse-battery-staple@db.example/app?token=query-secret"
        )

        result = redact_text(raw)

        for secret in (
            "abcdefghijklmnop",
            "abcdefghijklmnopqrstuvwxyz123456",
            "correct-horse-battery-staple",
            "query-secret",
        ):
            self.assertNotIn(secret, result)
        self.assertIn("postgres://" + REDACTED + "@db.example", result)

    def test_private_blocks_are_redacted_when_nested_or_unclosed(self) -> None:
        nested = "before <private>outer <private>inner secret</private> outer secret</private> after"
        unclosed = "before [private]this must not survive"

        self.assertEqual(
            "before <private>" + REDACTED + "</private> after", redact_text(nested)
        )
        self.assertEqual("before [private]" + REDACTED + "[/private]", redact_text(unclosed))
        self.assertEqual(
            "before <private>" + REDACTED + "</private>",
            redact_text("before <private unfinished secret"),
        )

    def test_multiline_quoted_assignment_and_tags_are_redacted(self) -> None:
        raw = "OPENAI_API_KEY='first line\nsecond secret line'"
        result = redact_text(raw)

        self.assertNotIn("second secret", result)
        self.assertEqual("OPENAI_API_KEY='" + REDACTED + "'", result)
        self.assertEqual(["normal", "token=" + REDACTED], redact_tags(["normal", "token=abc", "normal"]))

    def test_structured_payloads_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            redact_text({"token": "secret"})  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
