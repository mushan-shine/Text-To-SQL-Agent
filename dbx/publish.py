"""Publish a local run directory (runs/phase1/<run_id>/) to Delta and MLflow.

Row building is pure (``build_rows``) so it is unit-testable; the side effects
(Delta writes, MLflow logging) live in ``publish_run``. Re-publishing a run is
idempotent: existing rows for the run_id are deleted first, and an MLflow run
tagged with the same run_id is reused instead of duplicated.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from dbx import tables
from dbx.catalog import Layout, table_exists, write_rows

log = logging.getLogger(__name__)

MLFLOW_EXPERIMENT = "/Users/{user}/self_healing_text2sql"


def load_run(run_dir: Path) -> tuple[dict, dict, list[dict]]:
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return meta, summary, records


def split_of(meta: dict) -> str:
    return "dev" if meta.get("mode") == "dev" else "eval"


def build_rows(meta: dict, summary: dict, records: list[dict], experiment_id: str,
               mlflow_run_id: str | None = None) -> dict[str, list[dict]]:
    now = dt.datetime.now(dt.timezone.utc)
    split = split_of(meta)
    run_id = meta["run_id"]
    traces, evals = [], []
    for r in records:
        exec_result = None
        if r.get("execution_status") == "SUCCESS":
            exec_result = json.dumps({"row_count": r.get("result_row_count"),
                                      "preview": json.loads(r["result_preview"]) if r.get("result_preview") else None},
                                     ensure_ascii=False)
        traces.append({
            "run_id": run_id, "experiment_id": experiment_id, "case_id": r["case_id"], "split": split,
            "attempt_id": r.get("attempt_id", 1), "question": r["question"],
            "retrieved_tables": json.dumps(r.get("retrieved_tables") or []),
            "retrieved_columns": None, "retrieved_context": None,
            "generated_sql": r.get("generated_sql"), "parse_status": r.get("parse_status"),
            "execution_status": r.get("execution_status"), "execution_error": r.get("execution_error"),
            "execution_result": exec_result,
            # baseline: no verifier / diagnosis / repair yet (phases 4-6 fill these)
            "verifier_mode": None, "verifier_decision": None, "verifier_signals": None,
            "failure_type": None, "diagnosis_confidence": None, "diagnosis_reason": None,
            "repair_skill": None, "repair_reason": None, "repaired_sql": None,
            "final_status": r.get("execution_status"),
            "model": r.get("model"), "prompt_version": meta.get("prompt_version"),
            "latency_ms": r.get("latency_ms"), "llm_latency_ms": r.get("llm_latency_ms"),
            "exec_latency_ms": r.get("exec_latency_ms"),
            "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
            "total_tokens": r.get("total_tokens"), "llm_cached": r.get("llm_cached"),
            "created_at": now,
        })
        evals.append({
            "run_id": run_id, "experiment_id": experiment_id, "case_id": r["case_id"], "split": split,
            "attempt_id": r.get("attempt_id", 1), "correct": bool(r.get("correct")),
            "eval_message": r.get("eval_message"), "category": r.get("category"),
            "eval_table_recall": r.get("eval_table_recall"),
            "eval_all_gold_tables_retrieved": r.get("eval_all_gold_tables_retrieved"),
            "created_at": now,
        })
    run = {
        "run_id": run_id, "experiment_id": experiment_id, "split": split, "mode": meta.get("mode"),
        "model": meta.get("model"), "prompt_version": meta.get("prompt_version"), "top_k": meta.get("top_k"),
        "cases": summary.get("cases"), "correct": summary.get("correct"),
        "first_pass_accuracy": summary.get("first_pass_accuracy"), "executable_rate": summary.get("executable_rate"),
        "tokens_total": summary.get("tokens_total"),
        "summary_json": json.dumps(summary, ensure_ascii=False),
        "meta_json": json.dumps({k: v for k, v in meta.items() if k != "config"}, ensure_ascii=False),
        "mlflow_run_id": mlflow_run_id, "created_at": now,
    }
    return {"execution_traces": traces, "evaluation_results": evals, "runs": [run]}


def mlflow_params(meta: dict, summary: dict) -> dict[str, Any]:
    return {"run_id": meta["run_id"], "mode": meta.get("mode"), "split": split_of(meta),
            "model": meta.get("model"), "prompt_version": meta.get("prompt_version"), "top_k": meta.get("top_k"),
            "n_few_shot": len(meta.get("few_shot") or []),
            "few_shot_ids": ",".join(e.get("id", "") for e in meta.get("few_shot") or [])[:250],
            "cases": summary.get("cases")}


def mlflow_metrics(summary: dict) -> dict[str, float]:
    keys = ("correct", "first_pass_accuracy", "executable_rate", "mean_table_recall", "tokens_total",
            "tokens_mean", "llm_latency_ms_median", "latency_ms_median")
    return {k: float(summary[k]) for k in keys if isinstance(summary.get(k), (int, float))}


def log_mlflow(meta: dict, summary: dict, run_dir: Path, experiment_name: str, experiment_id: str) -> str | None:
    import mlflow

    mlflow.set_tracking_uri("databricks")
    exp = mlflow.get_experiment_by_name(experiment_name)
    exp_id = exp.experiment_id if exp else mlflow.create_experiment(experiment_name)
    existing = mlflow.search_runs([exp_id], filter_string=f"tags.run_id = '{meta['run_id']}'", output_format="list")
    if existing:
        log.info("mlflow run for %s already exists: %s", meta["run_id"], existing[0].info.run_id)
        return existing[0].info.run_id
    with mlflow.start_run(experiment_id=exp_id, run_name=meta["run_id"],
                          tags={"run_id": meta["run_id"], "experiment_id": experiment_id,
                                "phase": "1", "split": split_of(meta)}) as run:
        mlflow.log_params(mlflow_params(meta, summary))
        mlflow.log_metrics(mlflow_metrics(summary))
        for name in ("summary.json", "run_meta.json", "results.jsonl"):
            try:
                mlflow.log_artifact(str(run_dir / name))
            except Exception as e:  # artifact storage may be restricted (e.g. Free Edition DBFS root)
                log.warning("could not upload artifact %s: %s", name, str(e)[:200])
        return run.info.run_id


def publish_run(runner: Any, layout: Layout, run_dir: Path, experiment_id: str,
                mlflow_experiment: str | None) -> dict[str, Any]:
    meta, summary, records = load_run(run_dir)
    mlflow_run_id = log_mlflow(meta, summary, run_dir, mlflow_experiment, experiment_id) if mlflow_experiment else None
    rows = build_rows(meta, summary, records, experiment_id, mlflow_run_id)
    targets = {"execution_traces": (layout.traces, tables.EXECUTION_TRACES),
               "evaluation_results": (layout.evaluation, tables.EVALUATION_RESULTS),
               "runs": (layout.evaluation, tables.RUNS)}
    for name, (schema, arrow) in targets.items():
        if table_exists(runner, layout, schema, name):
            runner.run(f"DELETE FROM {layout.fq(schema, name)} WHERE run_id = '{meta['run_id']}'")
        write_rows(runner, layout, schema, name, rows[name], arrow)
    return {"run_id": meta["run_id"], "split": split_of(meta), "traces": len(rows["execution_traces"]),
            "mlflow_run_id": mlflow_run_id}
