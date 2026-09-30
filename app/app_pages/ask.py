"""Ask: the product page. A user asks their own question, the inner loop answers, the user rates the answer.

No gold exists for these questions, so only the self-check decides whether to repair. The rating (+ an
optional corrected SQL) is stored in Delta ``experience.feedback``; the outer loop (scripts/outer_loop.py)
turns it into proposals that a data engineer reviews on the Review page.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import data, runner  # noqa: E402
from app.trace_view import render_case  # noqa: E402

REASONS = ["结果不对", "用错了表", "条件 / 过滤不对", "统计口径不对", "SQL 报错", "其他"]
EXAMPLES = [
    "How many rooms does each building have? Show the building name and the room count.",
    "List the departments that offer more than 50 subjects, with the number of subjects.",
    "How many students are in the student directory for each department?",
]

runner.load_dotenv()


def current_user() -> str:
    """Databricks Apps forwards the signed-in user; locally there is none."""
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username") or "local-user"
    except Exception:
        return "local-user"


@st.cache_resource(show_spinner="加载 schema 和示例库…")
def bundle():
    return runner.load_bundle()


st.title("提问", anchor=False)
st.caption("用自然语言问数仓（BEAVER dw：MIT 的院系、课程、学生目录、楼宇房间、财务等 97 张表）。系统检索相关表、生成 SQL、执行并自检，"
           "自检不通过就诊断后修复。你的 👍 / 👎 会进入外层循环，由数据工程师审核后才会改变系统。")

try:
    B = bundle()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

ask: runner.AskRun | None = st.session_state.get("ask")
running = ask is not None and ask.status == "running"

with st.form("ask_form", border=True):
    question = st.text_area("问题（英文效果更好，数仓里的字段和样例都是英文）", key="question", height=90,
                            placeholder=EXAMPLES[0])
    c1, c2 = st.columns([1.2, 2])
    model = c1.segmented_control("模型", list(runner.MODELS), default="glm-4-flash", key="ask_model")
    max_repairs = c2.segmented_control("最大修复次数", list(range(0, runner.MAX_REPAIRS + 1)), default=2,
                                       key="ask_repairs", format_func=lambda k: f"{k} 次")
    with st.expander("高级设置"):
        dynamic = st.toggle("相似题示例（dynamic few-shot）", value=True, key="ask_dynamic")
        knowledge = st.toggle("数仓使用说明（统计知识 kb.json）", value=True, key="ask_kb")
        curated = st.toggle("已审核知识（数据工程师批准的条目）", value=True, key="ask_curated",
                            help="审核页批准的表选择偏好、关联规则和已验证查询；停用后下一次提问即不再使用。")
    go = st.form_submit_button("提问", icon=":material/send:", type="primary", disabled=running)

if go:
    if not question.strip():
        st.warning("请输入问题。")
    elif not model or max_repairs is None:
        st.warning("请选择模型和最大修复次数。")
    elif not runner.model_available(model):
        st.warning(f"没有配置 {model} 的 API key。")
    else:
        a = runner.AskRun(question=question.strip(), asked_by=current_user(), model=model,
                          max_repairs=int(max_repairs), few_shot="dynamic" if dynamic else "static",
                          knowledge=bool(knowledge), curated=bool(curated))
        st.session_state["ask"] = runner.start_ask(a, B, data.connect)
        st.session_state.pop("feedback_done", None)
        st.rerun()


def answer_block(a: runner.AskRun) -> None:
    fin = a.final
    ok = fin.get("execution_status") == "SUCCESS"
    passed = fin.get("verifier_decision") == "PASS"
    with st.container(horizontal=True):
        st.metric("自检", "通过" if passed else ("未通过" if ok else "未能执行"), border=True)
        st.metric("尝试次数", len(a.attempts), border=True)
        st.metric("结果行数", fin.get("result_row_count") if ok else "—", border=True)
        st.metric("用时", f"{a.elapsed:.0f} 秒", border=True)
    if ok and not passed:
        st.warning("SQL 能执行，但自检发现可疑之处（见下方执行过程的“自检”步骤）。请核对结果。", icon=":material/warning:")
    elif not ok:
        st.error("修复次数用完仍没有能执行的 SQL。可以换个说法再问，或在下面给出修正的 SQL。", icon=":material/error:")
    st.markdown("**最终 SQL**")
    st.code(fin.get("generated_sql") or "(没有生成 SQL)", language="sql", wrap_lines=True)
    if ok:
        st.markdown(f"**结果**（最多显示 {runner.ASK_ROWS} 行）")
        st.dataframe(pd.DataFrame(a.rows, columns=a.columns or None), hide_index=True, height=300)


def feedback_block(a: runner.AskRun) -> None:
    done = st.session_state.get("feedback_done")
    with st.container(border=True):
        st.markdown("**这个答案对吗？**")
        if done:
            msg = "已记录，谢谢！反馈会在下一次外层循环中被使用（审核通过后才生效）。"
            if done.get("corrected_sql_status"):
                msg += f" 修正 SQL 执行结果：{done['corrected_sql_status']}。"
            st.success(msg, icon=":material/check_circle:")
            if done.get("error"):
                st.caption(f"修正 SQL 报错：{done['error'][:300]}")
            return
        rating = st.feedback("thumbs", key=f"rating-{a.query_id}")
        if rating is None:
            return
        up = rating == 1
        with st.form(f"fb-{a.query_id}", border=False):
            reason, corrected = "", ""
            if not up:
                reason = st.pills("哪里不对", REASONS, key=f"reason-{a.query_id}") or ""
                corrected = st.text_area("正确的 SQL（可选，会先执行校验）", key=f"sql-{a.query_id}", height=120,
                                         placeholder="SELECT ... FROM ...")
            comment = st.text_input("备注（可选）", key=f"comment-{a.query_id}")
            if st.form_submit_button("提交反馈", icon=":material/rate_review:"):
                with st.spinner("保存反馈…"):
                    st.session_state["feedback_done"] = runner.submit_feedback(
                        a, B, data.connect, "up" if up else "down", reason, corrected, comment, current_user())
                st.rerun()


def ask_panel() -> None:
    a: runner.AskRun | None = st.session_state.get("ask")
    if a is None:
        st.info("示例问题：\n\n" + "\n".join(f"- {q}" for q in EXAMPLES), icon=":material/lightbulb:")
        return
    st.markdown(f"**问题**：{a.question}")
    if a.status == "running":
        st.status(":shimmer[正在检索、生成、执行、自检…]", type="step", state="running")
    elif a.status == "error":
        st.error(a.error)
    else:
        answer_block(a)
    with st.expander("执行过程（内层循环 trace）", expanded=a.status == "running"):
        render_case(a.snapshot(), a.status == "running")
    if a.status == "done":
        feedback_block(a)
    if a.status != "running" and st.session_state.get("ask_rendered") != id(a):
        st.session_state["ask_rendered"] = id(a)
        st.rerun(scope="app")


st.fragment(ask_panel, run_every=1 if running else None)()
