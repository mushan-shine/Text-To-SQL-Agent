"""Loop Controller — Generate -> Execute -> Verify -> (Observe -> Diagnose -> Policy -> Repair) -> Execute -> Verify.

One controller drives both experiment arms; they differ only in how the next
attempt is produced after a failed verification:

* ``targeted`` — Observer -> Diagnoser -> Policy -> Repair Skill;
* ``generic``  — Generic Retry: the previous SQL and what was observed go back
  to the model with a generic "correct it" instruction. No diagnosis, no
  targeted schema information. Same verifier, same attempt budget.

Attempt 1 is identical across arms (same prompt; the LLM cache replays it).
Budget = ``max_attempts`` SQL attempts; LLM calls made by diagnosis are not
attempts but are counted as cost.

Final answer: the last attempt that passed verification; otherwise the last
attempt that executed; otherwise the last attempt.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.generator import RULES, SYSTEM, extract_sql, render_schema
from agent.retriever import BM25TableRetriever
from benchmark.beaver.dataset import AgentTask
from loop_engineer.diagnose import Diagnoser
from loop_engineer.observer import observe
from loop_engineer.policy import Policy
from skills.base import REPAIR_TEMPLATE, RepairContext, observed_text

log = logging.getLogger(__name__)

GENERIC_INSTRUCTION = "The query above is wrong. Write a corrected query."


@dataclass
class LoopConfig:
    strategy: str = "targeted"      # targeted | generic
    max_attempts: int = 2
    top_k: int = 20
    max_result_rows: int = 500_000


@dataclass
class LoopResult:
    attempts: list[dict[str, Any]]
    rows: list[list[tuple]] = field(repr=False)
    final_index: int = 0

    @property
    def final(self) -> dict[str, Any]:
        return self.attempts[self.final_index]


def _serialize_preview(rows: list[tuple]) -> str:
    from benchmark.beaver.evaluator import serialize_rows  # agent-visible formatting only (no gold)
    return serialize_rows(rows[:5])


@dataclass
class LoopController:
    retriever: BM25TableRetriever
    generator: Any                  # FewShotGenerator
    executor: Any
    diagnoser: Diagnoser
    policy: Policy
    ctx: RepairContext
    cfg: LoopConfig = field(default_factory=LoopConfig)

    def _execute(self, sql: str, db: str, parse_status: str) -> tuple[dict[str, Any], list[tuple]]:
        if not sql:
            return {"execution_status": parse_status, "execution_error": None, "result_row_count": None,
                    "result_preview": None, "exec_latency_ms": 0}, []
        ex = self.executor.execute(sql, db, max_rows=self.cfg.max_result_rows)
        rows = ex.rows if ex.ok else []
        return {"execution_status": ex.status, "execution_error": (ex.error or "")[:2000] or None,
                "result_row_count": len(rows) if ex.ok else None,
                "result_preview": _serialize_preview(rows) if ex.ok else None, "exec_latency_ms": ex.elapsed_ms}, rows

    def _generic_retry(self, attempt: dict[str, Any], tables: tuple[str, ...]) -> dict[str, Any]:
        obs = observe(attempt, "")
        prompt = REPAIR_TEMPLATE.format(rules=RULES,
                                        schema=render_schema(self.ctx.catalog, tables), question=obs.question,
                                        sql=obs.generated_sql or "(no SQL was produced)", observed=observed_text(obs),
                                        diagnosis="(not diagnosed)", instruction=GENERIC_INSTRUCTION)
        r = self.ctx.client.complete(prompt, system=SYSTEM)
        sql, status = extract_sql(r.text)
        return {"generated_sql": sql, "parse_status": status, "repair_skill": "GenericRetry",
                "repair_action": "generic correction prompt", "repair_reason": None, "repair_fallback": False,
                "used_llm": True, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "llm_latency_ms": r.latency_ms, "tables": list(tables)}

    def run(self, task: AgentTask, verifier: Any) -> LoopResult:
        retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)
        gen = self.generator.generate(task, retrieval.tables)
        attempt = {"case_id": task.case_id, "attempt_id": 1, "question": task.question,
                   "retrieved_tables": list(retrieval.tables), "generated_sql": gen.sql,
                   "parse_status": gen.parse_status, "strategy": self.cfg.strategy,
                   "input_tokens": gen.llm.input_tokens, "output_tokens": gen.llm.output_tokens,
                   "llm_latency_ms": gen.llm.latency_ms, "used_llm": True, "diag_tokens": 0}
        attempts, all_rows = [], []
        for n in range(1, self.cfg.max_attempts + 1):
            exe, rows = self._execute(attempt["generated_sql"], task.db, attempt["parse_status"])
            attempt.update(exe)
            decision = verifier.verify(attempt, rows)
            attempt.update({"verifier_mode": decision.mode, "verifier_decision": "PASS" if decision.passed else "FAIL",
                            "verifier_signals": list(decision.signals)})
            attempts.append(attempt)
            all_rows.append(rows)
            if decision.passed or n == self.cfg.max_attempts:
                break
            # ---- produce the next attempt
            nxt: dict[str, Any] = {"case_id": task.case_id, "attempt_id": n + 1, "question": task.question,
                                   "retrieved_tables": attempt["retrieved_tables"], "strategy": self.cfg.strategy,
                                   "diag_tokens": 0}
            if self.cfg.strategy == "generic":
                nxt.update(self._generic_retry(attempt, tuple(attempt["retrieved_tables"])))
            else:
                obs = observe(attempt, task.db)   # whitelist: nothing gold-derived reaches diagnosis / repair
                diag, usage = self.diagnoser.diagnose(obs)
                route = self.policy.route(diag)
                res = self.policy.skill(route.skill).repair(obs, diag, self.ctx)
                attempt.update({"failure_type": diag.failure_type, "diagnosis_confidence": diag.confidence,
                                "diagnosis_reason": diag.reason, "diagnosis_source": diag.source,
                                "repair_hints": json.dumps(diag.repair_hints, ensure_ascii=False)})
                nxt.update({"generated_sql": res.repaired_sql, "parse_status": res.parse_status,
                            "repair_skill": route.skill, "repair_action": res.repair_action,
                            "repair_reason": res.repair_reason, "repair_fallback": route.fallback,
                            "used_llm": res.used_llm, "input_tokens": res.input_tokens,
                            "output_tokens": res.output_tokens, "llm_latency_ms": res.latency_ms,
                            "diag_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                            "tables": list(res.tables)})
            attempt["repaired_sql"] = nxt["generated_sql"]
            attempt["repair_skill"] = nxt["repair_skill"]
            attempt = nxt
        passed = [i for i, a in enumerate(attempts) if a["verifier_decision"] == "PASS"]
        executed = [i for i, a in enumerate(attempts) if a["execution_status"] == "SUCCESS"]
        final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
        for i, a in enumerate(attempts):
            a["final_status"] = "FINAL" if i == final else "SUPERSEDED"
        return LoopResult(attempts, all_rows, final)
