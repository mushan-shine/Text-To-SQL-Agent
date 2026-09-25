"""Phase 3 CLI — failure taxonomy labels for a run (EVALUATION ONLY).

    python scripts/phase3.py label runs/phase1/<run_id>            # labels + distribution
    python scripts/phase3.py label runs/phase1/<run_id> --publish  # also write evaluation.failure_labels
    python scripts/phase3.py spotcheck runs/phase1/<run_id> --n 20 # markdown sheet for human review

Labels are written under the run directory (``failure_labels.json``) and,
with --publish, to ``evaluation.failure_labels`` — never to traces.*.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import pyarrow as pa  # noqa: E402

from agent.retriever import SchemaCatalog  # noqa: E402
from benchmark.beaver.loader import load_cases  # noqa: E402
from benchmark.beaver.subtasks import PRIORITY, UNKNOWN, label_failure  # noqa: E402
from evaluation.devset import dev_cases, load_devset  # noqa: E402

FAILURE_LABELS = pa.schema([
    ("run_id", pa.string()), ("case_id", pa.string()), ("split", pa.string()), ("attempt_id", pa.int64()),
    ("actual_failure_type", pa.string()), ("failed_checks", pa.string()), ("evidence", pa.string()),
    ("labeler_version", pa.string()), ("created_at", pa.timestamp("us", tz="UTC")),
])
LABELER_VERSION = "taxonomy-v1 (annotation ∩ gold SQL)"


def load(run_dir: Path):
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    recs = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if meta.get("mode") == "dev":
        cases = {c.case_id: c for c in dev_cases(load_devset(Path(meta["config"]["dev"]["path"])))}
        split = "dev"
    else:
        b = meta["config"]["beaver"]
        cases = {c.case_id: c for c in load_cases("local_json", b["split"], int(b["sample_size"]),
                                                  int(b["sample_seed"]), b["local_dir"])[0]}
        split = "eval"
    catalog = SchemaCatalog.from_json(Path(f"runs/phase1/schema_{meta['config']['beaver']['db']}.json").read_text(encoding="utf-8"))
    schema = {t: {c.name.lower(): c.type for c in tb.columns} for t, tb in catalog.tables.items()}
    return meta, recs, cases, schema, split


def label_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta, recs, cases, schema, split = load(run_dir)
    out = []
    for r in recs:
        if r["correct"]:
            continue
        lab = label_failure(cases[r["case_id"]], r["generated_sql"], r["execution_status"], r["retrieved_tables"], schema)
        out.append({"run_id": meta["run_id"], "case_id": r["case_id"], "split": split,
                    "attempt_id": r.get("attempt_id", 1), "actual_failure_type": lab.primary,
                    "failed_checks": lab.failed_checks, "evidence": lab.evidence, "labeler_version": LABELER_VERSION})
    prim = Counter(x["actual_failure_type"] for x in out)
    checks = Counter(c for x in out for c in x["failed_checks"])
    missing = Counter(k for x in out for k in x["evidence"].get("missing_tables", {}).values())
    summary = {"run_id": meta["run_id"], "split": split, "attempts": len(recs), "failures": len(out),
               "primary_distribution": {k: prim.get(k, 0) for k in (*PRIORITY, UNKNOWN)},
               "all_failed_checks": dict(checks),
               "missing_gold_tables": dict(missing),
               "labeler_version": LABELER_VERSION}
    (run_dir / "failure_labels.json").write_text(json.dumps({"summary": summary, "labels": out}, indent=2,
                                                            ensure_ascii=False), encoding="utf-8")
    return summary, out


def spotcheck(run_dir: Path, n: int, seed: int) -> Path:
    meta, recs, cases, schema, _ = load(run_dir)
    labels = {x["case_id"]: x for x in json.loads((run_dir / "failure_labels.json").read_text(encoding="utf-8"))["labels"]}
    ids = sorted(labels)
    pick = sorted(random.Random(seed).sample(ids, min(n, len(ids))))
    by_id = {r["case_id"]: r for r in recs}
    lines = [f"# Failure label spot check — {meta['run_id']}", "",
             "For each case: does `actual_failure_type` name the most important reason the attempt failed?",
             "Fill in **agree / disagree** and, when disagreeing, the label you would give.", ""]
    for i, cid in enumerate(pick, 1):
        lab, r, c = labels[cid], by_id[cid], cases[cid]
        lines += [f"## {i}. {cid}", "", f"**Question:** {c.question}", "",
                  f"**Label:** `{lab['actual_failure_type']}` · all failed checks: {', '.join(lab['failed_checks'])}", "",
                  "**Evidence:**", "```json", json.dumps(lab["evidence"], indent=2, ensure_ascii=False), "```", "",
                  f"**Execution:** {r['execution_status']} {(r.get('execution_error') or '')[:300]}", "",
                  "**Generated SQL:**", "```sql", r["generated_sql"] or "(none)", "```", "",
                  "**Gold SQL (evaluation only):**", "```sql", c.gold_sql, "```", "",
                  "**Review:** agree / disagree → ______", "", "---", ""]
    out = run_dir / "spotcheck.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def publish(labels: list[dict]) -> None:
    import logging
    logging.getLogger("databricks").setLevel(logging.WARNING)
    from dbx.catalog import Layout, table_exists, write_rows
    from execution.databricks_sql import DatabricksSqlExecutor

    dbx = DatabricksSqlExecutor(catalog="self_healing_text2sql")
    layout = Layout("self_healing_text2sql")
    now = dt.datetime.now(dt.timezone.utc)
    rows = [{**x, "failed_checks": json.dumps(x["failed_checks"]), "evidence": json.dumps(x["evidence"], ensure_ascii=False),
             "created_at": now} for x in labels]
    if rows and table_exists(dbx, layout, layout.evaluation, "failure_labels"):
        dbx.run(f"DELETE FROM {layout.fq(layout.evaluation, 'failure_labels')} WHERE run_id = '{rows[0]['run_id']}'")
    if rows:
        write_rows(dbx, layout, layout.evaluation, "failure_labels", rows, FAILURE_LABELS)
    dbx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["label", "spotcheck"])
    ap.add_argument("run_dir")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260925)
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    if args.mode == "label":
        summary, labels = label_run(run_dir)
        if args.publish:
            publish(labels)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(spotcheck(run_dir, args.n, args.seed))


if __name__ == "__main__":
    main()
