"""Outer loop (evaluation/outer_loop.py) and reviewed knowledge (agent/curated.py): mining, gate, prompt use."""
import json

from agent.curated import CuratedKnowledge, attach_curated
from agent.examples import ExampleIndex, PoolEntry
from agent.generator import FewShotExample, FewShotGenerator
from agent.llm import LlmResponse
from agent.retriever import SchemaCatalog
from benchmark.beaver.dataset import AgentTask, BeaverCase
from benchmark.beaver.subtasks import FailureLabel
from dbx.experience import sql_str
from evaluation import outer_loop as ol

CAT = SchemaCatalog.build("dw", [
    ("dept", "CODE", "STRING"), ("dept", "NAME", "STRING"),
    ("course_desc", "DEPT", "STRING"), ("course_desc", "LEVEL", "STRING"), ("course_desc", "TITLE", "STRING"),
    ("course_desc_hist", "DEPT", "STRING"), ("course_desc_hist", "LEVEL", "STRING"),
    ("course_desc_hist", "TITLE", "STRING"),
], {})

PREF = {"item_id": "kn-1", "kind": "table_preference",
        "content": {"prefer": "course_desc", "instead_of": "course_desc_hist", "keywords": ["graduate"],
                    "text": "For questions about graduate, this warehouse uses table course_desc."}}
JOIN = {"item_id": "kn-2", "kind": "join_rule",
        "content": {"tables": ["course_desc", "dept"], "condition": "course_desc.DEPT = dept.CODE", "join": "LEFT",
                    "text": "Join course_desc and dept on course_desc.DEPT = dept.CODE (LEFT JOIN)."}}
VQ = {"item_id": "kn-3", "kind": "verified_query",
      "content": {"question": "how many graduate courses per department", "tables": ["course_desc", "dept"],
                  "sql": "SELECT d.NAME, COUNT(*) FROM dept d JOIN course_desc c ON c.DEPT = d.CODE GROUP BY d.NAME"}}


class Chat:
    model = "m"

    def __init__(self):
        self.prompts = []

    def complete(self, prompt, system=None):
        self.prompts.append(prompt)
        return LlmResponse("```sql\nSELECT 1\n```", "m", 10, 2, 1)


# ---------------------------------------------------------------- reviewed knowledge in the prompt

def test_curated_notes_match_shown_tables_and_keywords():
    cur = CuratedKnowledge([PREF, JOIN, VQ])
    notes = cur.notes_for("list graduate courses of each department", ["dept", "course_desc"])
    assert "Reviewed usage notes" in notes and "uses table course_desc" in notes and "LEFT JOIN" in notes
    assert "uses table" not in cur.notes_for("list undergrad courses", ["dept", "course_desc"])   # keyword gate
    assert cur.notes_for("graduate courses", ["dept"]) == ""                                   # tables not shown
    assert len(cur.notes) == 2 and len(cur.queries) == 1 and not CuratedKnowledge([])


def test_attach_curated_adds_verified_queries_to_the_pool_and_notes_to_the_prompt():
    pool = ExampleIndex([PoolEntry(FewShotExample("names of buildings", "SELECT 1", "7"), ("dept",))])
    chat = Chat()
    gen = attach_curated(FewShotGenerator(chat, CAT, [], index=pool, k=1), CuratedKnowledge([PREF, JOIN, VQ]))
    assert len(gen.index.entries) == 2 and gen.prompt_version.endswith("+cur")
    g = gen.generate(AgentTask("u:1", "graduate courses per department", "dw"), ("dept",))
    assert g.example_ids == ("verified:kn-3",)                 # the reviewed query is retrieved as the example
    assert "course_desc" in g.schema_tables                    # ... and its tables join the schema shown
    assert "Reviewed usage notes" in chat.prompts[0]
    assert attach_curated(gen, CuratedKnowledge([])) is gen    # nothing approved -> unchanged


# ---------------------------------------------------------------- mining

def result(cid, question, gen_tables, gold_tables, missing_joins=()):
    case = BeaverCase(cid, "dw", question, "dw", "")
    label = FailureLabel(cid, "JOIN_KEY_FAILURE", [], {"missing_join_keys": list(missing_joins)})
    return ol.CaseResult(case, "SELECT 1", "SUCCESS", False, [], 10, label, set(gen_tables), set(gold_tables))


def test_table_preferences_need_support_and_look_alike_tables():
    fails = [result("dw:1", "graduate courses", {"course_desc_hist"}, {"course_desc"}),
             result("dw:2", "graduate course titles", {"course_desc_hist", "dept"}, {"course_desc", "dept"}),
             result("dw:3", "department names", {"course_desc"}, {"dept"})]        # not look-alike tables
    got = ol.mine_table_preferences(fails, None, CAT, min_support=2)
    assert len(got) == 1
    c = got[0]
    assert c["content"]["prefer"] == "course_desc" and c["content"]["instead_of"] == "course_desc_hist"
    assert c["evidence"]["support"] == 2 and c["evidence"]["case_ids"] == ["dw:1", "dw:2"]
    assert ol.mine_table_preferences(fails, None, CAT, min_support=3) == []


class KB:  # the parts of WarehouseKnowledge the miners read
    n_queries = 100
    word_df = {"graduate": 10, "level": 40}
    word_tables = {"graduate": {"course_desc": 9}, "level": {"course_desc": 5, "dept": 30}}
    groups: list = []
    joins = {"course_desc|dept": {"keys": {"course_desc.DEPT = dept.CODE": 7}, "kinds": {"LEFT": 7}}}


def test_missing_tables_become_table_hints_that_extend_the_schema():
    fails = [result("dw:1", "graduate level courses", {"dept"}, {"dept", "course_desc"}),
             result("dw:2", "graduate course count", {"dept"}, {"dept", "course_desc"}),
             result("dw:3", "names", set(), {"course_desc"})]                    # nothing parsed: no evidence
    got = ol.mine_missing_tables(fails, KB, min_support=2)
    assert len(got) == 1
    c = got[0]["content"]
    assert c["table"] == "course_desc" and c["keywords"] == ["graduate"] and c["with"] == ["dept"]
    assert c["condition"] == "course_desc.DEPT = dept.CODE" and got[0]["evidence"]["case_ids"] == ["dw:1", "dw:2"]
    cur = ol.as_curated(got)
    assert cur.extra_tables("list graduate students by department", ["dept"]) == ["course_desc"]
    assert cur.extra_tables("list departments", ["dept"]) == []                  # keyword gate
    assert "also need table course_desc" in cur.notes_for("graduate students", ["dept", "course_desc"])
    chat = Chat()
    g = attach_curated(FewShotGenerator(chat, CAT, []), cur).generate(AgentTask("u:1", "graduate totals", "dw"), ("dept",))
    assert g.schema_tables == ("dept", "course_desc") and "TABLE dw.course_desc" in chat.prompts[0]


def test_join_rules_from_missing_join_keys():
    cond = "course_desc.dept = dept.code"
    fails = [result(f"dw:{i}", "q", {"dept", "course_desc"}, {"dept", "course_desc"}, [cond]) for i in range(2)]
    got = ol.mine_join_rules(fails, None, min_support=2)
    assert len(got) == 1 and got[0]["content"]["condition"] == "course_desc.DEPT = dept.CODE"
    assert got[0]["content"]["tables"] == ["course_desc", "dept"] and got[0]["content"]["join"] == "INNER"


def test_verified_queries_from_feedback():
    fb = [{"query_id": "q1", "rating": "up", "question": "Dept names?", "final_sql": "SELECT NAME FROM dept"},
          {"query_id": "q2", "rating": "down", "question": "Courses?", "final_sql": "SELECT 1",
           "corrected_sql": "SELECT TITLE FROM course_desc", "corrected_sql_status": "SUCCESS"},
          {"query_id": "q3", "rating": "down", "question": "Broken?", "corrected_sql": "SELEC x",
           "corrected_sql_status": "ERROR"},                                   # corrected SQL does not run
          {"query_id": "q4", "rating": "down", "question": "No fix?"},        # thumbs down without a fix
          {"query_id": "q5", "rating": "up", "question": "dept names?", "final_sql": "SELECT 1"}]  # already known
    got = ol.mine_verified_queries(fb, existing_questions={"dept names?"})
    assert [g["content"]["question"] for g in got] == ["Courses?"]
    assert got[0]["content"]["sql"] == "SELECT TITLE FROM course_desc" and got[0]["content"]["tables"] == ["course_desc"]
    assert len(ol.mine_verified_queries(fb, set())) == 2


# ---------------------------------------------------------------- regression gate + proposals

def runs(correct, tokens=10):
    return [ol.CaseResult(BeaverCase(f"dw:{i}", "dw", "q", "dw", ""), "S", "SUCCESS", ok, [], tokens)
            for i, ok in enumerate(correct)]


def test_gate_requires_no_harm_and_bounded_tokens():
    better = ol.compare(runs([False, True, False]), runs([True, True, False], 11))
    assert better["gate_passed"] and better["fixed"] == ["dw:0"] and "批准" in ol.recommendation(better)
    harmed = ol.compare(runs([False, True]), runs([True, False]))            # same total, but one harmed
    assert not harmed["gate_passed"] and harmed["harmed"] == ["dw:1"] and "驳回" in ol.recommendation(harmed)
    costly = ol.compare(runs([False, True]), runs([True, True], 13))         # +30% tokens
    assert not costly["gate_passed"]
    same = ol.compare(runs([True]), runs([True]))
    assert same["gate_passed"] and "中性" in ol.recommendation(same)


def test_proposal_rows_carry_json_and_recommendation():
    reg = ol.compare(runs([False]), runs([True]))
    rows = ol.proposal_rows("batch-x", [{"kind": "join_rule", "title": "t", "content": JOIN["content"],
                                          "evidence": {"support": 2}}], reg)
    assert rows[0]["proposal_id"] == "batch-x-00" and json.loads(rows[0]["content_json"]) == JOIN["content"]
    assert json.loads(rows[0]["regression_json"])["gate_passed"] and rows[0]["recommendation"]
    assert ol.as_curated([{"kind": "join_rule", "content": JOIN["content"]}]).notes[0]["item_id"] == "cand-0"


def test_sql_str_escapes_quotes_and_backslashes():
    assert sql_str(None) == "NULL" and sql_str("it's") == "'it\\'s'" and sql_str("a\\b") == "'a\\\\b'"
