"""Verifier — decides whether an attempt is accepted or goes to repair.

* ``SelfVerifier`` (main experiments): signals the agent can observe on its own
  result — no gold. This is what a production system has.
* ``OracleVerifier`` (upper bound only): compares with the frozen gold result.
  It leaks one bit of gold ("this attempt is wrong"), so every result produced
  with it is labelled as an upper bound (docs/PROJECT_POSITIONING.md §1.4).

SelfVerifier v2 adds semantic checks (loop_engineer/checks.py) on attempts that
ran and returned rows: does the SQL deliver what the question asks? Only
checks with ~0 false alarms on gold SQL trigger a repair (``TRIGGER_SIGNALS``,
measured by scripts/verifier_eval.py); the noisier ones are kept as
``advisories`` for the console and the report but never fail an attempt.
"passed" therefore means "no problem found", never "the answer is correct".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from loop_engineer.checks import TRIGGER_SIGNALS, Finding, check_all

BASIC_SIGNALS = ("no_sql", "execution_error", "too_many_rows", "empty_result", "all_null_column")
SELF_SIGNALS = BASIC_SIGNALS + TRIGGER_SIGNALS
VERIFIER_VERSION = "self-v2"          # v1 = BASIC_SIGNALS only (phase 6 runs)


@dataclass(frozen=True)
class VerifierDecision:
    passed: bool
    mode: str                      # self | oracle
    signals: tuple[str, ...] = ()  # why it failed (empty when passed)
    findings: tuple[Finding, ...] = ()    # semantic findings that failed the attempt (with evidence / hints)
    advisories: tuple[Finding, ...] = ()  # semantic findings too noisy to fail an attempt (display only)


@dataclass
class SelfVerifier:
    """Fails an attempt on explicit, gold-free signals. ``signals`` selects which ones are active."""

    signals: tuple[str, ...] = SELF_SIGNALS
    mode: str = "self"

    def verify(self, attempt: dict[str, Any], rows: list[tuple] | None) -> VerifierDecision:
        hits: list[str] = []
        findings: list[Finding] = []
        advisories: list[Finding] = []
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
            elif rows:
                for f in check_all(attempt.get("question") or "", attempt.get("generated_sql") or "", rows):
                    if f.signal in self.signals and f.signal in TRIGGER_SIGNALS:
                        findings.append(f)
                        if f.signal not in hits:
                            hits.append(f.signal)
                    else:
                        advisories.append(f)
        return VerifierDecision(not hits, self.mode, tuple(hits), tuple(findings), tuple(advisories))


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
