"""Product and outer-loop state in Delta (schema ``experience``).

* ``user_queries``    — questions asked on the Ask page and how the inner loop answered them;
* ``feedback``        — thumbs up / down, reason, optional corrected SQL (weak labels, never gold);
* ``proposals``       — candidate improvements produced by the outer loop, waiting for review;
* ``knowledge_items`` — approved knowledge the online system uses (``active``) or has rolled back.

``runner`` is anything with ``run(sql)``, ``query_df(sql)`` and ``workspace_config`` (DatabricksSqlExecutor).
Reads return plain dicts so the pages and scripts do not depend on pandas types.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from dbx import tables
from dbx.catalog import Layout, table_exists, write_rows

STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED = "pending", "approved", "rejected"
ITEM_ACTIVE, ITEM_INACTIVE = "active", "inactive"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def sql_str(v: Any) -> str:
    """A SQL string literal (Databricks: backslash-escaped)."""
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _rows(runner: Any, sql: str) -> list[dict]:
    df = runner.query_df(sql)
    return [{k: (None if (isinstance(v, float) and v != v) else v) for k, v in r.items()}
            for r in df.to_dict("records")]


def _select(runner: Any, layout: Layout, table: str, where: str = "", order: str = "") -> list[dict]:
    if not table_exists(runner, layout, layout.experience, table):
        return []
    sql = f"SELECT * FROM {layout.fq(layout.experience, table)}"
    return _rows(runner, sql + (f" WHERE {where}" if where else "") + (f" ORDER BY {order}" if order else ""))


# ---------------------------------------------------------------- user queries + feedback

def save_user_query(runner: Any, layout: Layout, row: dict) -> None:
    write_rows(runner, layout, layout.experience, "user_queries", [{**row, "created_at": row.get("created_at") or now()}],
               tables.USER_QUERIES)


def save_feedback(runner: Any, layout: Layout, row: dict) -> str:
    fid = row.get("feedback_id") or new_id("fb")
    write_rows(runner, layout, layout.experience, "feedback", [{**row, "feedback_id": fid, "created_at": now()}],
               tables.FEEDBACK)
    return fid


def list_feedback(runner: Any, layout: Layout) -> list[dict]:
    """Latest feedback per query (a user may change their mind)."""
    rows = _select(runner, layout, "feedback", order="created_at")
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["query_id"]] = r
    return list(latest.values())


def list_user_queries(runner: Any, layout: Layout, limit: int = 200) -> list[dict]:
    rows = _select(runner, layout, "user_queries", order="created_at DESC")
    return rows[:limit]


# ---------------------------------------------------------------- proposals (outer loop -> review)

def save_proposals(runner: Any, layout: Layout, rows: list[dict]) -> None:
    if rows:
        write_rows(runner, layout, layout.experience, "proposals",
                   [{"status": STATUS_PENDING, "reviewer": None, "review_note": None, "reviewed_at": None,
                     "created_at": now(), **r} for r in rows], tables.PROPOSALS)


def list_proposals(runner: Any, layout: Layout, status: str | None = None) -> list[dict]:
    where = f"status = {sql_str(status)}" if status else ""
    rows = _select(runner, layout, "proposals", where=where, order="created_at DESC, proposal_id")
    for r in rows:
        for k in ("content_json", "evidence_json", "regression_json"):
            r[k.removesuffix("_json")] = json.loads(r.get(k) or "{}")
    return rows


def review_proposal(runner: Any, layout: Layout, proposal: dict, approve: bool, reviewer: str, note: str = "") -> None:
    """Record the decision; an approved proposal becomes an active knowledge item (effective immediately)."""
    fq = layout.fq(layout.experience, "proposals")
    status = STATUS_APPROVED if approve else STATUS_REJECTED
    runner.run(f"UPDATE {fq} SET status = {sql_str(status)}, reviewer = {sql_str(reviewer)}, "
               f"review_note = {sql_str(note)}, reviewed_at = current_timestamp() "
               f"WHERE proposal_id = {sql_str(proposal['proposal_id'])} AND status = {sql_str(STATUS_PENDING)}")
    if approve:
        write_rows(runner, layout, layout.experience, "knowledge_items", [{
            "item_id": new_id("kn"), "proposal_id": proposal["proposal_id"], "batch_id": proposal["batch_id"],
            "kind": proposal["kind"], "content_json": proposal["content_json"], "status": ITEM_ACTIVE,
            "approved_by": reviewer, "approved_at": now(), "deactivated_by": None, "deactivated_at": None,
        }], tables.KNOWLEDGE_ITEMS)


# ---------------------------------------------------------------- knowledge items (what the online system uses)

def list_knowledge_items(runner: Any, layout: Layout, status: str | None = ITEM_ACTIVE) -> list[dict]:
    where = f"status = {sql_str(status)}" if status else ""
    rows = _select(runner, layout, "knowledge_items", where=where, order="approved_at")
    for r in rows:
        r["content"] = json.loads(r.get("content_json") or "{}")
    return rows


def deactivate_item(runner: Any, layout: Layout, item_id: str, by: str) -> None:
    """Roll back one approved item: the online system stops using it at the next question."""
    runner.run(f"UPDATE {layout.fq(layout.experience, 'knowledge_items')} SET status = {sql_str(ITEM_INACTIVE)}, "
               f"deactivated_by = {sql_str(by)}, deactivated_at = current_timestamp() "
               f"WHERE item_id = {sql_str(item_id)}")
