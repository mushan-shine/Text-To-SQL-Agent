"""Review: a data engineer approves or rejects what the outer loop proposes; approved items go live.

Proposals come from scripts/outer_loop.py (mined from training-split failures and user feedback, gated on the
dev set). Approving one writes an active row to ``experience.knowledge_items``; the Ask and Run-loop pages
read active items at every question, so an approval or a rollback (停用) takes effect on the next question.
Set ``SHT_REVIEWERS`` (comma-separated emails) to restrict who may decide; unset = anyone who can open the app.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import data, runner  # noqa: E402
from dbx import experience  # noqa: E402
from dbx.catalog import Layout  # noqa: E402

KIND_CN = {"table_preference": "选表偏好", "table_hint": "补表提示", "join_rule": "关联规则", "verified_query": "已验证查询"}
LAYOUT = Layout(runner.CATALOG)

runner.load_dotenv()


def current_user() -> str:
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username") or "local-user"
    except Exception:
        return "local-user"


def can_review(user: str) -> bool:
    allowed = {u.strip().lower() for u in os.environ.get("SHT_REVIEWERS", "").split(",") if u.strip()}
    return not allowed or user.lower() in allowed


def with_conn(fn, *args, **kw):
    conn = data.connect()
    try:
        return fn(conn, LAYOUT, *args, **kw)
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner="读取待审核提案…")
def proposals(status: str | None) -> list[dict]:
    return with_conn(experience.list_proposals, status)


@st.cache_data(ttl=60, show_spinner="读取已生效知识…")
def items(status: str | None) -> list[dict]:
    return with_conn(experience.list_knowledge_items, status)


@st.cache_data(ttl=60, show_spinner="读取用户反馈…")
def feedback() -> list[dict]:
    return with_conn(experience.list_feedback)


def refresh() -> None:
    proposals.clear()
    items.clear()
    feedback.clear()


def content_view(kind: str, c: dict) -> None:
    if kind == "table_preference":
        st.markdown(f"优先用 **`{c['prefer']}`**，而不是相似表 `{c['instead_of']}`；"
                    f"触发关键词：{', '.join(c.get('keywords') or []) or '（任何提到这两张表的问题）'}")
    elif kind == "table_hint":
        st.markdown(f"问题含关键词 {', '.join(c.get('keywords') or [])} 时，把表 **`{c['table']}`** 加进 schema 并提示使用"
                    + (f"；关联键 `{c['condition']}`" if c.get("condition") else "")
                    + (f"（仅当已选中 {', '.join(c['with'])} 之一）" if c.get("with") else ""))
    elif kind == "join_rule":
        st.markdown(f"`{' , '.join(c['tables'])}` 用 **{c['join']} JOIN** 关联：`{c['condition']}`")
    elif kind == "verified_query":
        st.markdown(f"问题：{c['question']}")
        st.code(c["sql"], language="sql", wrap_lines=True)
    if c.get("text"):
        st.caption("写进 prompt 的原文：")
        st.code(c["text"], language="text", wrap_lines=True)


def regression_view(reg: dict) -> None:
    if not reg:
        return
    cols = st.columns(4)
    cols[0].metric("开发集答对", f"{reg['correct_before']} → {reg['correct_after']}", border=True,
                   delta=reg["correct_after"] - reg["correct_before"])
    cols[1].metric("能执行", f"{reg['executable_before']} → {reg['executable_after']}", border=True)
    cols[2].metric("误伤（原来对、现在错）", len(reg.get("harmed") or []), border=True)
    cols[3].metric("token 变化", f"{reg['token_increase']:+.1%}", border=True)
    st.caption(f"回归门槛：{'通过' if reg['gate_passed'] else '未通过'}（答对不减少、零误伤、token 增幅 ≤ 20%）· "
               f"模型 {reg.get('model', '?')} · {reg.get('outer_loop', '')} · 开发集 {reg['cases']} 题。"
               "同一批提案一起做回归，结果是整批的效果。")
    if reg.get("fixed") or reg.get("harmed"):
        st.caption(f"新答对：{', '.join(reg.get('fixed') or []) or '—'}；误伤：{', '.join(reg.get('harmed') or []) or '—'}")


user = current_user()
st.title("审核", anchor=False)
st.caption("外层循环提出的改动不会自动上线：它们先在开发集上做回归，再由数据工程师在这里批准或驳回。"
           "批准后下一次提问就会使用；有问题随时在「已生效知识」里停用（回滚）。")
if not can_review(user):
    st.info(f"当前用户 {user} 不在审核人名单（SHT_REVIEWERS）中，只能查看。", icon=":material/lock:")
editable = can_review(user)

with st.container(horizontal=True, horizontal_alignment="right"):
    if st.button("刷新", icon=":material/refresh:"):
        refresh()
        st.rerun()

tab_p, tab_k, tab_f, tab_h = st.tabs([":material/pending_actions: 待审核", ":material/verified: 已生效知识",
                                      ":material/thumbs_up_down: 用户反馈", ":material/history: 审核记录"])

with tab_p:
    try:
        pending = proposals(experience.STATUS_PENDING)
    except Exception as e:
        st.error(f"读取失败：{e}")
        pending = []
    if not pending:
        st.info("没有待审核的提案。运行一次外层循环：`python scripts/outer_loop.py`", icon=":material/inbox:")
    batches: dict[str, list[dict]] = {}
    for p in pending:
        batches.setdefault(p["batch_id"], []).append(p)
    for batch_id, ps in batches.items():
        ps.sort(key=lambda p: p["proposal_id"])
        with st.container(border=True):
            st.markdown(f"**批次 `{batch_id}`** · {len(ps)} 条提案 · {ps[0]['recommendation']}")
            regression_view(ps[0]["regression"])
            if ps[0]["regression"].get("train"):
                tr = ps[0]["regression"]["train"]
                st.caption(f"训练抽样：{tr['cases']} 题，答对 {tr['correct']}，能执行 {tr['executable']}；错误类型："
                           + ", ".join(f"{k} {v}" for k, v in tr.get("primary_labels", {}).items()))
            for p in ps:
                with st.expander(f"{KIND_CN.get(p['kind'], p['kind'])} · {p['title']}"):
                    content_view(p["kind"], p["content"])
                    ev = p["evidence"]
                    st.caption("证据：" + (f"{ev['support']} 道训练题出现这个错误（{', '.join(ev.get('case_ids') or [])}）"
                                          if "support" in ev else ev.get("source", "")))
                    if ev.get("example_question"):
                        st.caption(f"例题：{ev['example_question']}")
                    if ev.get("example_wrong_sql"):
                        st.code(ev["example_wrong_sql"], language="sql", wrap_lines=True)
                    if ev.get("comment") or ev.get("reason"):
                        st.caption(f"用户说明：{ev.get('reason') or ''} {ev.get('comment') or ''}")
                    note = st.text_input("审核意见（可选）", key=f"note-{p['proposal_id']}")
                    c1, c2, _ = st.columns([1, 1, 4])
                    if c1.button("批准上线", key=f"ok-{p['proposal_id']}", type="primary", disabled=not editable,
                                 icon=":material/check:"):
                        with_conn(experience.review_proposal, p, True, user, note)
                        refresh()
                        st.toast("已批准，下一次提问生效")
                        st.rerun()
                    if c2.button("驳回", key=f"no-{p['proposal_id']}", disabled=not editable,
                                 icon=":material/close:"):
                        with_conn(experience.review_proposal, p, False, user, note)
                        refresh()
                        st.toast("已驳回")
                        st.rerun()

with tab_k:
    try:
        active = items(experience.ITEM_ACTIVE)
    except Exception as e:
        st.error(f"读取失败：{e}")
        active = []
    if not active:
        st.info("还没有已生效的知识。", icon=":material/inbox:")
    for it in active:
        with st.container(border=True):
            st.markdown(f"**{KIND_CN.get(it['kind'], it['kind'])}** · `{it['item_id']}` · 批准人 {it['approved_by']} · "
                        f"{str(it['approved_at'])[:16]}")
            content_view(it["kind"], it["content"])
            if st.button("停用（回滚）", key=f"off-{it['item_id']}", disabled=not editable,
                         icon=":material/undo:"):
                with_conn(experience.deactivate_item, it["item_id"], user)
                refresh()
                st.toast("已停用，下一次提问不再使用")
                st.rerun()

with tab_f:
    try:
        fb = feedback()
    except Exception as e:
        st.error(f"读取失败：{e}")
        fb = []
    if not fb:
        st.info("还没有用户反馈。在「提问」页提问后点 👍 / 👎。", icon=":material/inbox:")
    else:
        up = sum(f["rating"] == "up" for f in fb)
        st.caption(f"{len(fb)} 条（每个问题取最新一条）：👍 {up} · 👎 {len(fb) - up}。"
                   "外层循环会把 👍 的答案和能执行的修正 SQL 变成「已验证查询」提案。")
        st.dataframe(pd.DataFrame([{"时间": str(f["created_at"])[:16], "用户": f["user"],
                                    "评价": "👍" if f["rating"] == "up" else "👎", "原因": f.get("reason") or "",
                                    "问题": f["question"], "修正 SQL": f.get("corrected_sql") or "",
                                    "修正 SQL 执行": f.get("corrected_sql_status") or "",
                                    "备注": f.get("comment") or ""} for f in fb]), hide_index=True)

with tab_h:
    try:
        done = [p for p in proposals(None) if p["status"] != experience.STATUS_PENDING]
        inactive = items(experience.ITEM_INACTIVE)
    except Exception as e:
        st.error(f"读取失败：{e}")
        done, inactive = [], []
    if done:
        st.dataframe(pd.DataFrame([{"批次": p["batch_id"], "类型": KIND_CN.get(p["kind"], p["kind"]), "标题": p["title"],
                                    "结论": "批准" if p["status"] == "approved" else "驳回", "审核人": p["reviewer"],
                                    "意见": p.get("review_note") or "", "时间": str(p["reviewed_at"])[:16]}
                                   for p in done]), hide_index=True)
    if inactive:
        st.markdown("**已停用的知识**")
        st.dataframe(pd.DataFrame([{"条目": i["item_id"], "类型": KIND_CN.get(i["kind"], i["kind"]),
                                    "内容": json.dumps(i["content"], ensure_ascii=False)[:200],
                                    "停用人": i["deactivated_by"], "停用时间": str(i["deactivated_at"])[:16]}
                                   for i in inactive]), hide_index=True)
    if not done and not inactive:
        st.info("还没有审核记录。", icon=":material/inbox:")
