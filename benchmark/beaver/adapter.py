"""Benchmark Adapter — case B of the qualification plan.

Only for gold SQL that is NOT ``COMPATIBLE``. The original gold SQL is never
modified; an adaptation is a separate record:

    original_gold_sql | adapted_sql | adaptation_rule | semantic_validation

``semantic_validation`` is ``RESULT_EQUIVALENT`` only when the adapted SQL,
run on Databricks, reproduces the MySQL (official engine) gold result with a
stable result across repeats. That is equivalence on the benchmark data
instance — not a proof of general semantic equivalence — so adapted cases are
kept OUT of primary evaluation unless explicitly admitted by config.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import sqlglot

from benchmark.beaver.compatibility import EngineRuns, run_repeated
from benchmark.beaver.dataset import BeaverCase
from benchmark.beaver.evaluator import canonical_match
from execution.base import SqlExecutor

RULE_SQLGLOT = "sqlglot_transpile_mysql_to_databricks"

RESULT_EQUIVALENT = "RESULT_EQUIVALENT"
RESULT_DIFFERS = "RESULT_DIFFERS"
ADAPTED_SQL_FAILS = "ADAPTED_SQL_FAILS"
NO_ADAPTATION = "NO_ADAPTATION"  # transpiler produced identical text / could not transpile


@dataclass
class AdaptationRecord:
    case_id: str
    original_gold_sql: str
    adapted_sql: str | None
    adaptation_rule: str
    semantic_validation: str
    detail: str

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def transpile(sql: str) -> str | None:
    try:
        out = sqlglot.transpile(sql, read="mysql", write="databricks", unsupported_level=sqlglot.ErrorLevel.RAISE)
    except (sqlglot.errors.ParseError, sqlglot.errors.UnsupportedError):
        return None
    return out[0] if len(out) == 1 else None


def try_adapt(case: BeaverCase, reference_runs: EngineRuns, candidate: SqlExecutor, repeats: int) -> AdaptationRecord:
    adapted = transpile(case.gold_sql)
    if adapted is None or _norm(adapted) == _norm(case.gold_sql):
        return AdaptationRecord(case.case_id, case.gold_sql, adapted, RULE_SQLGLOT, NO_ADAPTATION,
                                "transpiler produced no different SQL")
    runs = run_repeated(candidate, adapted, case.db, repeats)
    if runs.status != "SUCCESS":
        return AdaptationRecord(case.case_id, case.gold_sql, adapted, RULE_SQLGLOT, ADAPTED_SQL_FAILS,
                                f"{runs.status}: {runs.error_class or (runs.error or '')[:200]}")
    cmp = canonical_match(runs.rows, reference_runs.rows)
    ok = cmp.set_match and runs.stable and reference_runs.stable
    return AdaptationRecord(case.case_id, case.gold_sql, adapted, RULE_SQLGLOT,
                            RESULT_EQUIVALENT if ok else RESULT_DIFFERS,
                            f"{cmp.reason}; stable={runs.stable}")


def _norm(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").lower()
