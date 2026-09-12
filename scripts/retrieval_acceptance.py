#!/usr/bin/env python3
"""Measure layered CLI retrieval on fictional records without a model or live data."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem.config import configure
from codex_mem.store import Store


def verify() -> dict:
    with tempfile.TemporaryDirectory(prefix="codex-mem-retrieval-") as temporary:
        base = Path(temporary)
        project, data = base / "project", base / "data"
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            expected = []
            for index in range(8):
                note = store.remember(
                    project, f"Aurora rollback finding {index}",
                    f"Aurora rollback case {index} passed locally. Production remains unverified.",
                    session_id="fictional-review",
                    observation={
                        "type": "bugfix", "concepts": ["atomicity"],
                        "facts": ["Source links and derived notes commit in one transaction."],
                        "narrative": ("The Aurora fixture injects a write failure after linking the source. "
                                      "The transaction rolls back, and the earlier source remains readable. " * 12).strip(),
                        "files_read": ["fictional_store.py"], "files_modified": ["fictional_store.py"],
                    },
                )
                expected.append(note["id"])
            store.remember(base / "other", "Aurora private project", "Foreign evidence must remain scoped.")

        def cli(command, *arguments):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/codex-mem.py"), command,
                 "--data-dir", str(data), "--project", str(project), *arguments],
                text=True, capture_output=True, timeout=20, check=True,
            )
            return json.loads(completed.stdout)

        compact = cli("search", "--query", "Aurora", "--mode", "lexical")
        full = cli("search", "--query", "Aurora", "--mode", "lexical", "--detail", "full")
        compact_ids = [record["id"] for record in compact["results"]]
        assert compact_ids == [record["id"] for record in full["results"]]
        assert set(compact_ids) == set(expected)
        assert all("metadata" not in record and "narrative" not in record.get("observation", {})
                   for record in compact["results"])
        window = cli("timeline", "--anchor-id", expected[3], "--before", "2", "--after", "2")
        assert [record["id"] for record in window] == expected[1:6]
        assert [record["is_anchor"] for record in window] == [False, False, True, False, False]
        detail = cli("get", "--id", expected[3])[0]
        assert "transaction rolls back" in detail["observation"]["narrative"]
        assert detail["provenance"]["verification"] == "not_assessed"
        compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode())
        full_bytes = len(json.dumps(full, ensure_ascii=False).encode())
        assert compact_bytes < full_bytes
        return {
            "status": "passed", "synthetic_only": True, "model_turns": 0,
            "records": len(compact_ids), "anchor_window_records": len(window),
            "compact_json_bytes": compact_bytes, "full_preview_json_bytes": full_bytes,
            "payload_reduction_percent": round((1 - compact_bytes / full_bytes) * 100, 1),
            "checks": ["same search ranking and project scope", "no duplicate structured text",
                       "exact chronological anchor window", "full evidence remains retrievable"],
            "measurement": "UTF-8 JSON payload bytes for this fixture, not account token savings",
        }


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
