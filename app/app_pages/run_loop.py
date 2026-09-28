"""Run the loop from the browser and watch its trace grow step by step."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import data, runner  # noqa: E402

TYPE_CN = {"TABLE_RETRIEVAL_FAILURE": "选表", "COLUMN_MAPPING_FAILURE": "列映射", "JOIN_KEY_FAILURE": "关联键",
           "DOMAIN_KNOWLEDGE_FAILURE": "领域知识", "QUERY_DECOMPOSITION_FAILURE": "查询拆解",
           "EXECUTION_FAILURE": "执行错误", "UNKNOWN": "未知"}
STRATEGY = {"targeted": "Targeted Loop（诊断 → 定向修复）", "generic": "Generic Retry（通用重试）"}
VERIFIER = {"self": "Self（无 Gold）", "oracle": "Oracle（上界）"}

runner.load_dotenv()


@st.cache_resource(show_spinner="加载 schema、few-shot 示例和开发集…")
def bundle():
    return runner.load_bundle()


@st.cache_data(ttl=600, show_spinner=False)
def eval_options():
    conn = data.connect()
    try:
        return runner.eval_choices(conn)
    finally:
        conn.close()


st.title("运行 Loop", anchor=False)
st.caption("选题目、模型和策略后点执行。Loop 每走一步，下面的 trace 就追加一步；Gold 判分在每题的 Loop 结束后才出现，Loop 本身看不到。")

try:
    B = bundle()
except FileNotFoundError as e:
    st.error(f"{e}")
    st.stop()

live: runner.LiveRun | None = st.session_state.get("live")
running = live is not None and live.status == "running"

# ------------------------------------------------------------------ form

split = st.segmented_control("数据集", ["dev", "eval"], default="dev", key="split",
                             format_func=lambda s: "开发集（30 题）" if s == "dev" else
                             ("评测集（89 题）" if runner.eval_allowed() else "评测集（已锁定）"))
if split == "eval" and not runner.eval_allowed():
    st.caption("评测集已锁定（决策 D2：配置冻结后才在评测集上跑）。部署时设置环境变量 SHT_ALLOW_EVAL=1 即可解锁。")
    options = []
elif split == "eval":
    options = eval_options()
else:
    options = runner.dev_choices(B)
by_id = {o["case_id"]: o for o in options}

with st.form("run_form", border=True):
    picked = st.multiselect(f"题目（每次最多 {runner.MAX_CASES} 题）", list(by_id), max_selections=runner.MAX_CASES,
                            default=[c for c in ["dw:dw_5478"] if c in by_id],
                            format_func=lambda c: f"{c.split(':')[-1]} · {by_id[c]['question'][:110]}")
    c1, c2, c3 = st.columns([1.2, 1.4, 1])
    model = c1.segmented_control("模型", list(runner.MODELS), default="glm-4-flash", key="model")
    strategy = c2.segmented_control("策略", list(STRATEGY), default="targeted", key="strategy",
                                    format_func=lambda s: STRATEGY[s].split("（")[0])
    verifier = c3.segmented_control("Verifier", list(VERIFIER), default="self", key="verifier",
                                    format_func=VERIFIER.get)
    publish = st.checkbox("跑完发布到 Delta（之后可在 Console 的「逐题追踪」里查看）", value=True)
    st.caption("glm-4-flash 免费；deepseek-flash 按量计费（每题约几分钱人民币）。每题最多 2 次 SQL 尝试。"
               "Oracle 用 Gold 判断要不要修，只作上界参考。")
    go = st.form_submit_button("执行", icon=":material/play_arrow:", type="primary", disabled=running)

if go:
    problem = None
    if split == "eval" and not runner.eval_allowed():
        problem = "评测集已锁定（决策 D2）。"
    elif not split:
        problem = "请选择数据集。"
    elif not picked:
        problem = "请至少选择一道题。"
    elif not model or not strategy or not verifier:
        problem = "请选择模型、策略和 Verifier。"
    elif not runner.model_available(model):
        problem = f"没有配置 {model} 的 API key。"
    if problem:
        st.warning(problem)
    else:
        req = runner.RunRequest(split=split, case_ids=picked, model=model, strategy=strategy, verifier=verifier,
                                publish=publish)
        st.session_state["live"] = runner.start_run(req, B, data.connect)
        st.rerun()


# ------------------------------------------------------------------ live trace

def fmt_ms(ms) -> str:
    return f"{(ms or 0) / 1000:.1f} 秒"


def render_case(events: list[dict], active: bool) -> None:
    for e in events:
        s, n = e["step"], e.get("attempt_id")
        if s == "retrieve":
            box = st.status(f"检索 · 选出 {len(e['tables'])} 张候选表", type="step", state="complete")
            box.write(", ".join(e["tables"]))
        elif s == "generate":
            cached = " · 缓存重放" if e.get("cached") else ""
            box = st.status(f"生成 SQL（第 1 次）· {e['tokens']:,} token · {fmt_ms(e['latency_ms'])}{cached}",
                            type="step", state="complete")
            box.code(e["sql"] or "(没有生成 SQL)", language="sql")
        elif s == "execute":
            ok = e["execution_status"] == "SUCCESS"
            cls = re.search(r"\[([A-Z][A-Z0-9_]+)", e.get("execution_error") or "")
            label = (f"执行（第 {n} 次）· 成功，{e['result_row_count']} 行" if ok
                     else f"执行（第 {n} 次）· {e['execution_status']}" + (f" · {cls.group(1)}" if cls else ""))
            box = st.status(label, type="step", state="complete" if ok else "error")
            if ok:
                box.code(e.get("result_preview") or "[]", language="json")
            elif e.get("execution_error"):
                box.code(e["execution_error"][:1500], language="text")
        elif s == "verify":
            sig = "、".join(e["signals"])
            label = f"校验（第 {n} 次，{e['mode']}）· " + ("PASS" if e["passed"] else f"FAIL：{sig}")
            if not e["passed"] and e.get("last"):
                label += " · 预算用完，不再修复"
            st.status(label, type="step", state="complete" if e["passed"] else "error")
        elif s == "diagnose":
            box = st.status(f"诊断 · {TYPE_CN.get(e['failure_type'], e['failure_type'])} · 置信度 "
                            f"{e['confidence']:.2f} · 来源 {'规则' if e['source'] == 'rule' else e['source']}",
                            type="step", state="complete")
            box.write(e["reason"])
            if e.get("repair_hints"):
                box.code(json.dumps(e["repair_hints"], ensure_ascii=False, indent=1), language="json")
        elif s == "route":
            st.status(f"路由 → {e['skill']}" + ("（兜底）" if e["fallback"] else "") + f" · {e['reason']}",
                      type="step", state="complete")
        elif s == "repair":
            how = "LLM 改写" if e.get("used_llm") else "确定性修复（不调 LLM）"
            box = st.status(f"修复 · {e['skill']} · {how} · 生成第 {n} 次尝试" +
                            (f" · {e['tokens']:,} token" if e.get("tokens") else ""), type="step", state="complete")
            if e.get("action"):
                box.caption(e["action"])
            box.code(e["sql"] or "(没有生成 SQL)", language="sql")
        elif s == "final":
            ok = e["verifier_decision"] == "PASS"
            st.status(f"最终答案 · 取第 {e['final_attempt']} 次尝试（共 {e['attempts']} 次）· Verifier "
                      f"{e['verifier_decision']} · {e['execution_status']}", type="step",
                      state="complete" if ok else "error")
    if active:
        st.status(":shimmer[等待下一步…]", type="step", state="running")
    judged = [e for e in events if e["step"] == "judged"]
    if judged:
        j = judged[0]
        marks = " → ".join("对" if c else "错" for c in j["attempt_correct"])
        msg = (f"Gold 判分（Loop 不可见）：各次尝试 {marks}；最终 {'对' if j['final_correct'] else '错'} · "
               f"{j['tokens']:,} token · {fmt_ms(j['latency_ms'])}")
        (st.success if j["final_correct"] else st.info)(msg, icon=":material/grading:")


def render_live(lv: runner.LiveRun) -> None:
    ev = lv.snapshot()
    req = lv.request
    starts = [e for e in ev if e["step"] == "start"]
    done = sum(e["step"] == "judged" for e in ev)
    with st.container(horizontal=True):
        st.metric("状态", {"running": "运行中", "done": "完成", "error": "出错"}[lv.status], border=True)
        st.metric("进度", f"{done} / {len(req.case_ids)} 题", border=True)
        st.metric("已用时间", f"{lv.elapsed:.0f} 秒", border=True)
        if lv.summary:
            s = lv.summary
            st.metric("答对 首次 → 最终", f"{s['first_correct']} → {s['final_correct']}", border=True)
            st.metric("可执行 首次 → 最终", f"{s['executable_first']} → {s['executable_final']}", border=True)
    st.caption(f"{req.model} · {STRATEGY[req.strategy]} · Verifier {VERIFIER[req.verifier]} · "
               f"{'开发集' if req.split == 'dev' else '评测集'}")
    for e in ev:
        if e["step"] == "error":
            st.error(e["message"])
            with st.expander("错误详情"):
                st.code(e.get("trace", ""), language="text")
    for k, sev in enumerate(starts):
        cid = sev["case_id"]
        case_events = [e for e in ev if e["case_id"] == cid and e["step"] not in ("start",)]
        active = lv.status == "running" and not any(e["step"] == "judged" for e in case_events)
        with st.container(border=True):
            st.markdown(f"**第 {sev['index']} / {sev['total']} 题 · `{cid.split(':')[-1]}`**")
            st.caption(sev["question"])
            render_case(case_events, active)
    if lv.status == "running" and not starts:
        st.status(":shimmer[连接 SQL Warehouse、准备模型…]", type="step", state="running")
    pub = [e for e in ev if e["step"] == "publish"]
    if pub and pub[-1]["state"] == "done":
        st.success(f"已发布到 Delta：`{pub[-1]['run_id']}`。可在 Console 的「逐题追踪」里选择这次运行查看"
                   "（Console 数据缓存 5 分钟，可用右上角菜单 Rerun 刷新）。", icon=":material/cloud_done:")
    elif pub and lv.status == "running":
        st.status(":shimmer[发布到 Delta…]", type="step", state="running")


def live_panel() -> None:
    lv: runner.LiveRun | None = st.session_state.get("live")
    if lv is None:
        st.info("还没有运行。选择题目后点「执行」。", icon=":material/info:")
        return
    render_live(lv)
    if lv.status != "running" and st.session_state.get("rendered_final") != id(lv):
        st.session_state["rendered_final"] = id(lv)
        st.rerun(scope="app")  # re-enable the form and stop polling


st.subheader("实时 Trace", anchor=False)
st.fragment(live_panel, run_every=1 if running else None)()
