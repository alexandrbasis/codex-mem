#!/usr/bin/env python3
"""Live, synthetic eligibility smoke test. Never reads the user's memory store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem.jev_filter import filter_claim, JevFilterError

CASES = [
    ("ack_en", "discard", "hook:Stop", "OK, thanks."),
    ("ack_ru", "discard", "hook:Stop", "Хорошо, спасибо."),
    ("listing", "discard", "hook:PostToolUse", "Directory listing: README.md, src/, tests/."),
    ("progress", "discard", "hook:Stop", "I am reading the files now."),
    ("decision_en", "retain", "hook:Stop", "We chose SQLite transactions because a crash between note and source writes otherwise leaves notes without provenance."),
    ("decision_ru", "retain", "hook:UserPromptSubmit", "Решили сохранять исходники после отсева, чтобы проверять ложные отказы классификатора."),
    ("open_work", "retain", "hook:Stop", "The fix passed unit tests, but capture in a fresh native session is still unverified and must be checked before release."),
    ("invariant", "retain", "hook:PostToolUse", "Regression test confirms that a rejected source ID cannot be cited by a generated note. The unfiltered-history test failed: rejected raw text still reaches the worker."),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        _, audit = filter_claim({"sources": [
            {"id": identifier, "source": source, "title": "Observation", "body": body}
            for identifier, _, source, body in CASES
        ], "context": [], "summary_required": False}, key_file=args.key_file)
        expected = {identifier: route for identifier, route, _, _ in CASES}
        results = [{"case": row["source_id"], "expected": expected[row["source_id"]],
                    "actual": row["route"], "matched": row["route"] == expected[row["source_id"]]}
                   for row in audit["decisions"]]
        report = {"status": "completed", "scope": "synthetic_smoke_not_calibration",
                  "results": results, "matched": sum(row["matched"] for row in results), "audit": audit}
    except JevFilterError as exc:
        report = {"status": "failed", "code": exc.code, "audit": getattr(exc, "audit", {})}
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
