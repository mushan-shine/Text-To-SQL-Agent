"""Verifier — decides whether an attempt is accepted or goes to repair.

* ``SelfVerifier`` (main experiments): signals the agent can observe on its own
  result — no gold. This is what a production system has.
* ``OracleVerifier`` (upper bound only): compares with the frozen gold result.
  It leaks one bit of gold ("this attempt is wrong"), so every result produced
  with it is labelled as an upper bound (docs/PROJECT_POSITIONING.md §1.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

SELF_SIGNALS = ("no_sql", "execution_error", "too_many_rows", "empty_result", "all_null_column")


@dataclass(frozen=True)
class VerifierDecision:
    passed: bool
    mode: str                      # self | oracle
    signals: tuple[str, ...] = ()  # why it failed (empty when passed)


@dataclass
class SelfVerifier:
    """Fails an attempt on explicit, gold-free signals. ``signals`` selects which ones are active."""

    signals: tuple[str, ...] = SELF_SIGNALS
    mode: str = "self"

    def verify(self, attempt: dict[str, Any], rows: list[tuple] | None) -> VerifierDecision:
        hits: list[str] = []
        status = attempt.get("execution_status")
        if status in ("NO_SQL", "EMPTY_RESPONSE") and "no_sql" in self.signals:
            hits.append("no_sql")
        elif status == "TOO_MANY_ROWS" and "too_many_rows" in self.signals:
            hits.append("too_many_rows")
        elif status not in ("SUCCESS", "NO_SQL", "EMPTY_RESPONSE", "TOO_MANY_ROWS") and "execution_error" in self.signals:
            hits.append("execution_error")
        elif status == "SUCCESS":
            rows = rows or []
            if not rows and "empty_result" in self.signals:
                hits.append("empty_result")
            elif rows and "all_null_column" in self.signals and any(all(r[i] is None for r in rows)
                                                                    for i in range(len(rows[0]))):
                hits.append("all_null_column")
        return VerifierDecision(not hits, self.mode, tuple(hits))


@dataclass
class OracleVerifier:
    """UPPER BOUND ONLY: accepts an attempt iff it matches the gold result."""

    judge: Callable[[list], tuple[bool, str]] = field(repr=False)
    mode: str = "oracle"

    def verify(self, attempt: dict[str, Any], rows: list[tuple] | None) -> VerifierDecision:
        if attempt.get("execution_status") != "SUCCESS":
            return VerifierDecision(False, self.mode, ("oracle_wrong",))
        ok, _ = self.judge(rows or [])
        return VerifierDecision(ok, self.mode, () if ok else ("oracle_wrong",))
