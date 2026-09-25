"""Result comparison.

Two comparators, used for different purposes:

* :func:`official_match` — a faithful port of BEAVER ``eval/utils/ex_acc.py``
  ``compare_results``: values → ``str`` → strip, compare the *set* of rows,
  column order matters, column names ignored, both-empty counts as a match.
  This is the Execution Accuracy metric for all later experiments, where gold
  and generated results come from the SAME engine (Databricks).

* :func:`canonical_match` — engine-neutral comparison used only in phase 0 to
  decide whether Databricks reproduces the MySQL (official engine) result.
  Drivers render the same value differently (``AVG`` → ``DECIMAL 12.5000`` in
  MySQL vs ``DOUBLE 12.5`` in Spark), so values are canonicalised first.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

Row = Sequence[Any]

NUMERIC_DIGITS = 6  # decimal places kept by the canonical form


# --------------------------------------------------------------------------- official


def _official_str(v: Any) -> str:
    # pandas ``astype(str)`` renders None as 'None' and NaN as 'nan'.
    return str(v).strip()


def official_match(pred: list[Row] | None, gold: list[Row] | None) -> tuple[bool, str]:
    pred_empty = not pred
    gold_empty = not gold
    if pred_empty and gold_empty:
        return True, "Both empty"
    if pred_empty or gold_empty:
        return False, "One is empty, other is not"
    pred_rows = [tuple(_official_str(v) for v in r) for r in pred]
    gold_rows = [tuple(_official_str(v) for v in r) for r in gold]
    if len(pred_rows[0]) != len(gold_rows[0]):
        return False, f"Column count mismatch: pred {len(pred_rows[0])} vs gold {len(gold_rows[0])}"
    if set(pred_rows) == set(gold_rows):
        return True, "Match (values match, ignoring column names)"
    return False, "Values mismatch"


# --------------------------------------------------------------------------- canonical


def canonical_value(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        v = Decimal(repr(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        try:
            q = v.quantize(Decimal(1).scaleb(-NUMERIC_DIGITS))
        except InvalidOperation:  # too many digits to quantize — keep as is
            q = v
        s = format(q.normalize(), "f")
        return "0" if s in ("-0", "0") else s
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.timedelta):  # MySQL TIME
        total = int(v.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v).strip()


def canonical_rows(rows: list[Row] | None) -> list[tuple[str | None, ...]]:
    return [tuple(canonical_value(v) for v in r) for r in (rows or [])]


@dataclass(frozen=True)
class CanonicalComparison:
    set_match: bool
    multiset_match: bool
    ordered_match: bool
    reason: str


def canonical_match(a: list[Row] | None, b: list[Row] | None) -> CanonicalComparison:
    ca, cb = canonical_rows(a), canonical_rows(b)
    if not ca and not cb:
        return CanonicalComparison(True, True, True, "Both empty")
    if not ca or not cb:
        return CanonicalComparison(False, False, False, "One is empty, other is not")
    if len(ca[0]) != len(cb[0]):
        return CanonicalComparison(False, False, False, f"Column count mismatch: {len(ca[0])} vs {len(cb[0])}")
    s = set(ca) == set(cb)
    m = Counter(ca) == Counter(cb)
    o = ca == cb
    reason = "match" if m else ("set match, duplicate counts differ" if s else "values differ")
    return CanonicalComparison(s, m, o, reason)


def result_hash(rows: list[Row] | None) -> str:
    """Order- and duplicate-insensitive fingerprint (official EX semantics)."""
    canon = sorted(set(canonical_rows(rows)), key=lambda r: json.dumps(r))
    payload = json.dumps(canon, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def serialize_rows(rows: list[Row] | None) -> str:
    """Canonical JSON, lossless enough to recompute the hash and re-run official_match."""
    return json.dumps(canonical_rows(rows), ensure_ascii=False)
