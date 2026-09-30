"""Outer loop, one iteration: propose improvements, gate them on the dev set, queue them for review.

    python scripts/outer_loop.py                      # 40 training questions, glm-4-flash, write proposals
    python scripts/outer_loop.py --train-n 80 --dry-run

Steps (evaluation/outer_loop.py): sample the training split -> run the current system -> judge against gold
and label failures -> mine candidate knowledge (+ verified queries from user feedback) -> dev-set regression
(current vs current + candidates) -> write PENDING proposals to experience.proposals. A data engineer
approves or rejects them on the Review page; only approved items go live. The evaluation set is never used.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import yaml  # noqa: E402

from agent.curated import CuratedKnowledge, attach_curated, load_curated  # noqa: E402
from agent.examples import ExampleIndex  # noqa: E402
from agent.generator import FewShotGenerator, select_few_shot  # noqa: E402
from agent.knowledge import load_knowledge  # noqa: E402
from agent.llm import CachingChatClient, UsageMeter, make_client  # noqa: E402
from agent.retriever import BM25TableRetriever, SchemaCatalog  # noqa: E402
from benchmark.beaver.dataset import BeaverCase  # noqa: E402
from benchmark.beaver.loader import load_cases, load_from_local_json  # noqa: E402
from dbx import experience  # noqa: E402
from dbx.catalog import Layout  # noqa: E402
from evaluation import outer_loop as ol  # noqa: E402
from evaluation.devset import dev_cases, dev_judges, load_devset  # noqa: E402
from execution.databricks_sql import DatabricksSqlExecutor  # noqa: E402

log = logging.getLogger("outer_loop")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train-n", type=int, default=40, help="training questions to run this iteration")
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--min-support", type=int, default=2, help="failures needed before a pattern becomes a proposal")
    ap.add_argument("--model", default=None, help="default: config llm.model (glm-4-flash)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="do not write proposals to Delta")
    ap.add_argument("--config", default="config/phase1.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("databricks", "urllib3", "py4j"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    cfg = yaml.safe_load((ROOT / args.config).read_text(encoding="utf-8"))
    b, lc, fs, d = cfg["beaver"], cfg["llm"], cfg["few_shot"], cfg["databricks"]
    catalog = SchemaCatalog.from_json((ROOT / f"runs/phase1/schema_{b['db']}.json").read_text(encoding="utf-8"))
    schema = {t: {c.name.lower(): c.type for c in tb.columns} for t, tb in catalog.tables.items()}
    queries, _ = load_from_local_json(str(ROOT / b["local_dir"]), b["split"])
    eval_ids = {c.case_id.split(":", 1)[1] for c in load_cases("local_json", b["split"], int(b["sample_size"]),
                                                               int(b["sample_seed"]), str(ROOT / b["local_dir"]))[0]}
    devset = load_devset(ROOT / cfg["dev"]["path"])
    dev_ids = {str(c["id"]) for c in devset["cases"]}
    kb = load_knowledge(ROOT / cfg.get("knowledge", {}).get("path", "runs/knowledge/kb.json"))
    examples = select_few_shot(queries, eval_ids, int(fs["n_examples"]), int(fs["seed"]), int(fs["max_tables"]),
                               int(fs["max_sql_chars"]))

    dbx = DatabricksSqlExecutor(catalog=d["catalog"], profile=d.get("profile"), ansi_mode=bool(d["ansi_mode"]),
                                statement_timeout_s=int(d["statement_timeout_s"]))
    layout = Layout(d["catalog"])
    max_rows = int(d["max_result_rows"])
    meter = UsageMeter(max_calls=int(lc["max_calls"]), max_tokens=int(lc["max_tokens"]))
    inner = make_client(model=args.model, max_output_tokens=int(lc["max_output_tokens"]), meter=meter) if args.model \
        else make_client(lc, max_output_tokens=int(lc["max_output_tokens"]), meter=meter)
    client = CachingChatClient(inner, ROOT / lc["cache"])
    retriever = BM25TableRetriever(catalog)
    current = load_curated(dbx, layout)             # knowledge already approved and live
    log.info("model=%s, active reviewed items=%d", inner.model, len(current.items))

    def generator(pool_exclude: set[str], extra: CuratedKnowledge | None = None) -> FewShotGenerator:
        g = FewShotGenerator(client, catalog, examples,
                             index=ExampleIndex.build(queries, pool_exclude, int(fs.get("dynamic_max_sql_chars", 2500))),
                             k=int(fs.get("dynamic_k", 4)), max_extra_tables=int(fs.get("dynamic_max_extra_tables", 6)),
                             knowledge=kb)
        items = list(current.items) + (list(extra.items) if extra else [])
        return attach_curated(g, CuratedKnowledge(items))

    # ① + ② training sample: the sampled questions must not be their own few-shot examples
    sample = ol.sample_training(queries, eval_ids | dev_ids, args.train_n, args.seed)
    train_cases, train_judges = [], {}
    for q in sample:
        c = BeaverCase.from_beaver(q, b["split"])
        j = ol.gold_judge_from_sql(dbx, q["sql"], c.db, max_rows)
        if j:
            train_cases.append(c)
            train_judges[c.case_id] = j
    log.info("training sample: %d questions (%d with runnable gold)", len(sample), len(train_cases))
    train_gen = generator(eval_ids | dev_ids | {str(q["id"]) for q in sample})
    train = ol.run_and_judge(train_cases, train_judges, retriever, train_gen, dbx, schema,
                             int(cfg["retrieval"]["top_k"]), max_rows, args.workers)
    failures = [r for r in train if not r.correct]
    log.info("training: %s", ol.failure_summary(train))

    # ③ candidates from failures + user feedback
    candidates = ol.mine_table_preferences(failures, kb, catalog, args.min_support) + \
        ol.mine_missing_tables(failures, kb, args.min_support) + \
        ol.mine_join_rules(failures, kb, args.min_support)
    existing_q = {i["content"]["question"].lower() for i in current.queries}
    candidates += ol.mine_verified_queries(experience.list_feedback(dbx, layout), existing_q)
    log.info("candidates: %d (%s)", len(candidates), dict(ol.Counter(c["kind"] for c in candidates)))

    # ④ dev-set regression gate: current vs current + all candidates of this batch
    dcases, djudges = dev_cases(devset, b["split"]), dev_judges(devset, b["split"])
    before = ol.run_and_judge(dcases, djudges, retriever, generator(eval_ids | dev_ids), dbx, schema,
                              int(cfg["retrieval"]["top_k"]), max_rows, args.workers)
    if candidates:
        after = ol.run_and_judge(dcases, djudges, retriever, generator(eval_ids | dev_ids, ol.as_curated(candidates)),
                                 dbx, schema, int(cfg["retrieval"]["top_k"]), max_rows, args.workers)
    else:
        after = before
    regression = {**ol.compare(before, after), "model": inner.model, "outer_loop": ol.OUTER_LOOP_VERSION,
                  "train": ol.failure_summary(train)}

    # ⑤ queue for review
    batch_id = experience.new_id("batch")
    rows = ol.proposal_rows(batch_id, candidates, regression)
    out = ROOT / "runs" / "outer_loop" / batch_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposals.json").write_text(json.dumps({"regression": regression, "candidates": candidates,
                                                    "train_failures": ol.failure_rows(failures, kb, catalog)},
                                                   indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if not args.dry_run:
        experience.save_proposals(dbx, layout, rows)
    dbx.close()
    print(json.dumps({"batch_id": batch_id, "proposals": len(rows), "written_to_delta": not args.dry_run,
                      "dev": {k: regression[k] for k in ("correct_before", "correct_after", "fixed", "harmed",
                                                         "token_increase", "gate_passed")},
                      "recommendation": ol.recommendation(regression), "llm_usage": meter.snapshot(),
                      "local": str(out)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
