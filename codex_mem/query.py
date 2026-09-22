"""Separate explicit skill routing metadata from the user's memory question."""
from __future__ import annotations

import re


_SKILL_LINK = re.compile(
    r"\[\$[A-Za-z0-9][A-Za-z0-9_.:-]*\]"
    r"\((?P<target><[^>\r\n]+>|[^)\r\n]+)\)"
)
_LOCAL_PATH = re.compile(r"(?:/|~/|[A-Za-z]:[\\/])")


def normalize_retrieval_query(query: str) -> str:
    """Ignore local ``[$skill](.../SKILL.md)`` routing links only.

    Ordinary document links, URLs, file paths, code symbols, versions, and
    bare dollar-prefixed terms retain their existing search constraints.
    Normalization changes retrieval input, never captured source evidence.
    """
    def replace(match: re.Match[str]) -> str:
        target = match.group("target").strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if _LOCAL_PATH.match(target) and target.replace("\\", "/").endswith("/SKILL.md"):
            return " "
        return match.group(0)

    normalized = _SKILL_LINK.sub(replace, query)
    return normalized.strip() if normalized != query else query
