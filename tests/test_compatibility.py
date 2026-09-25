from decimal import Decimal

from benchmark.beaver import adapter, compatibility as C
from benchmark.beaver.compatibility import classify_error, static_hazards, validate_case
from execution.base import ExecutionResult, is_read_only
from tests.conftest import FakeExecutor, err, ok


class TestClassifyError:
    def test_categories(self):
        assert classify_error("PARSE_SYNTAX_ERROR", "") == C.INCOMPATIBLE_SYNTAX
        assert classify_error("UNRESOLVED_ROUTINE", "") == C.INCOMPATIBLE_FUNCTION
        assert classify_error("DATATYPE_MISMATCH.BINARY_OP_DIFF_TYPES", "") == C.INCOMPATIBLE_FUNCTION
        assert classify_error("TABLE_OR_VIEW_NOT_FOUND", "") == C.INCOMPATIBLE_SCHEMA
        assert classify_error("UNRESOLVED_COLUMN.WITH_SUGGESTION", "") == C.INCOMPATIBLE_SCHEMA
        assert classify_error("MISSING_AGGREGATION", "") == C.INCOMPATIBLE_SEMANTICS
        assert classify_error("DIVIDE_BY_ZERO", "") == C.INCOMPATIBLE_SEMANTICS
        assert classify_error(None, "something odd") == C.UNKNOWN


class TestStaticHazards:
    def test_flags(self):
        hz = static_hazards("SELECT a, COUNT(*) FROM t WHERE name = 'X' GROUP BY b LIMIT 5")
        assert {"LIMIT_WITHOUT_ORDER_BY", "NONAGGREGATED_COLUMN_IN_GROUP_BY",
                "STRING_COMPARISON_COLLATION"} <= set(hz)

    def test_clean_query(self):
        assert static_hazards("SELECT a, COUNT(*) FROM t GROUP BY a ORDER BY a LIMIT 5") == []

    def test_qualified_table(self):
        assert "DB_QUALIFIED_TABLE" in static_hazards("SELECT * FROM dw.t")


class TestReadOnlyGuard:
    def test_select_ok(self):
        assert is_read_only("SELECT 1", "mysql")
        assert is_read_only("WITH x AS (SELECT 1) SELECT * FROM x", "mysql")

    def test_rejects_writes_and_multi(self):
        assert not is_read_only("DELETE FROM t", "mysql")
        assert not is_read_only("SELECT 1; DROP TABLE t", "mysql")
        assert not is_read_only("CREATE TABLE x AS SELECT 1", "mysql")


class TestValidateCase:
    def test_compatible_when_results_match_across_representations(self, make_case):
        case = make_case("SELECT AVG(x) FROM t")
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [(Decimal("2.5000"),)])})
        cand = FakeExecutor("databricks", {case.gold_sql: ok("databricks", [(2.5,)])})
        rec, _, _ = validate_case(case, ref, cand, repeats=3)
        assert rec.compatibility_status == C.COMPATIBLE
        assert rec.reference_stable and rec.databricks_stable
        assert len(ref.calls) == 3 and len(cand.calls) == 3

    def test_executes_but_differs_is_semantics(self, make_case):
        case = make_case("SELECT COUNT(*) FROM t WHERE name = 'abc'")
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [(3,)])})     # case-insensitive
        cand = FakeExecutor("databricks", {case.gold_sql: ok("databricks", [(1,)])})
        rec, _, _ = validate_case(case, ref, cand)
        assert rec.compatibility_status == C.INCOMPATIBLE_SEMANTICS
        assert rec.execution_status == "SUCCESS" and rec.set_match is False

    def test_error_is_classified(self, make_case):
        case = make_case("SELECT DATE_FORMAT(d, '%Y') FROM t")
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [("2020",)])})
        cand = FakeExecutor("databricks", {case.gold_sql: err("databricks", "UNRESOLVED_ROUTINE")})
        rec, _, _ = validate_case(case, ref, cand)
        assert rec.compatibility_status == C.INCOMPATIBLE_FUNCTION

    def test_reference_failure(self, make_case):
        case = make_case("SELECT broken")
        ref = FakeExecutor("mysql", {case.gold_sql: err("mysql", "1054")})
        cand = FakeExecutor("databricks", {case.gold_sql: ok("databricks", [(1,)])})
        rec, _, _ = validate_case(case, ref, cand)
        assert rec.compatibility_status == C.REFERENCE_FAILED

    def test_unstable_result_is_not_compatible(self, make_case):
        case = make_case("SELECT id FROM t LIMIT 1")
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [(1,)])})
        cand = FakeExecutor("databricks", {case.gold_sql: lambda n: ok("databricks", [(n % 2,)])})
        rec, _, _ = validate_case(case, ref, cand, repeats=3)
        assert rec.compatibility_status == C.INCOMPATIBLE_SEMANTICS
        assert not rec.databricks_stable
        assert "LIMIT_WITHOUT_ORDER_BY" in rec.static_hazards


class TestAdapter:
    def test_adaptation_validated_by_result_equivalence(self, make_case):
        case = make_case("SELECT IFNULL(a, 0) FROM t")
        adapted = adapter.transpile(case.gold_sql)
        assert adapted and adapted != case.gold_sql
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [(0,)])})
        _, ref_runs, _ = validate_case(case, ref, FakeExecutor("databricks", {case.gold_sql: err("databricks", "X")}))
        cand = FakeExecutor("databricks", {adapted: ok("databricks", [(0,)])})
        rec = adapter.try_adapt(case, ref_runs, cand, repeats=2)
        assert rec.semantic_validation == adapter.RESULT_EQUIVALENT
        assert rec.original_gold_sql == case.gold_sql  # original is never modified

    def test_adaptation_rejected_when_result_differs(self, make_case):
        case = make_case("SELECT IFNULL(a, 0) FROM t")
        adapted = adapter.transpile(case.gold_sql)
        ref = FakeExecutor("mysql", {case.gold_sql: ok("mysql", [(0,)])})
        _, ref_runs, _ = validate_case(case, ref, FakeExecutor("databricks", {case.gold_sql: err("databricks", "X")}))
        rec = adapter.try_adapt(case, ref_runs, FakeExecutor("databricks", {adapted: ok("databricks", [(9,)])}), 2)
        assert rec.semantic_validation == adapter.RESULT_DIFFERS

    def test_no_adaptation_when_text_unchanged(self, make_case):
        case = make_case("SELECT a FROM t")
        rec = adapter.try_adapt(case, None, FakeExecutor("databricks", {}), 1)  # type: ignore[arg-type]
        assert rec.semantic_validation == adapter.NO_ADAPTATION


def test_execution_result_ok_flag():
    assert ExecutionResult("x", "SUCCESS").ok and not ExecutionResult("x", "ERROR").ok
