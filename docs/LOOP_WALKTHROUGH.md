# Loop 逐步讲解（对照源码）

> 这份文档从一条命令开始，按执行顺序讲完一道题在 Loop 里的全过程，每一步都给出：**做什么 → 源码位置和代码 → 真实数据 → 为什么这样设计**。
> 整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，设计取舍见 [LOOP_DESIGN.md](LOOP_DESIGN.md)。
> 文中的数据全部来自真实运行 `runs/phase6/targeted-self-20260925T105746-eb2ade`（开发集 30 题，glm-4-flash）。

---

## 0. 全景：一次运行经过哪些步骤

```
scripts/phase6.py  main()                      Step 0  装配
└─ evaluation/loop_run.py  run_arm()           Step 1  逐题循环，只把 agent_view 交给 Loop
   └─ loop_engineer/controller.py  run()
        ├─ retriever.retrieve()                Step 2  检索
        ├─ generator.generate()                Step 3  生成 attempt 1
        └─ for n in 1..max_attempts:
             ├─ _execute()                     Step 4  执行
             ├─ verifier.verify()              Step 5  校验
             ├─ PASS 或预算用完 → break         Step 6  终止判断
             ├─ observe()                      Step 7  观察（白名单）
             ├─ diagnoser.diagnose()           Step 8  诊断
             ├─ policy.route()                 Step 9  路由
             ├─ skill.repair()                 Step 10 修复
             └─ 写回诊断 + 组装 attempt n+1     Step 11 状态交接
        └─ 选出最终答案                         Step 12 Final 选择
   ├─ judge(rows)  ← 循环结束后才用 Gold 判分   Step 13 事后判分
   └─ summarize()                              Step 14 指标汇总
scripts/publish_run.py → Delta + MLflow → Console  Step 15 发布与回放
```

贯穿全程的**上下文对象**只有一个：`attempt`（一个 dict）。每一步往里加字段，最后整个 dict 就是这次尝试的 trace。下表是字段在哪一步被写入：

| 步骤 | 写入 `attempt` 的字段 |
|---|---|
| Step 3 生成 | `case_id, attempt_id, question, retrieved_tables, generated_sql, parse_status, strategy, input_tokens, output_tokens, llm_latency_ms, used_llm, diag_tokens` |
| Step 4 执行 | `execution_status, execution_error, result_row_count, result_preview, exec_latency_ms` |
| Step 5 校验 | `verifier_mode, verifier_decision, verifier_signals` |
| Step 11 写回（失败的那次） | `failure_type, diagnosis_confidence, diagnosis_reason, diagnosis_source, repair_hints, repaired_sql, repair_skill` |
| Step 10/11（新的一次） | `repair_skill, repair_action, repair_reason, repair_fallback, tables` 以及新的 SQL 和 token |
| Step 12 | `final_status`（FINAL / SUPERSEDED） |

**注意**：表里没有任何"对不对"的字段。对错在 Step 13 才算，而且存在另一个地方。

---

## Step 0 装配：一条命令把所有部件接起来

**做什么**：读配置，加载题目、schema、few-shot 示例，连 Databricks，建 LLM 客户端，把各部件注入 `LoopController`。

**源码** `scripts/phase6.py:86-103`

```python
meter = UsageMeter(max_calls=int(lc["max_calls"]), max_tokens=int(lc["max_tokens"]))   # 预算护栏
inner = ZhipuChatClient.from_env(max_output_tokens=int(lc["max_output_tokens"]), meter=meter)
client = CachingChatClient(inner, Path(lc["cache"]))                                   # 指纹缓存
policy = Policy(mode=args.policy, disabled={s for s in args.disable.split(",") if s})  # 消融开关
controller = LoopController(BM25TableRetriever(catalog), FewShotGenerator(client, catalog, examples), dbx,
                            Diagnoser(catalog, client), policy, RepairContext(catalog, client, examples),
                            LoopConfig(strategy=args.strategy, top_k=int(cfg["retrieval"]["top_k"]),
                                       max_result_rows=int(d["max_result_rows"])))
verifier_for = (lambda cid: SelfVerifier()) if args.verifier == "self" else (lambda cid: OracleVerifier(judges[cid]))
```

**参数**（`loop_engineer/controller.py:38-43`）

| 参数 | 值 | 含义 |
|---|---|---|
| `strategy` | `targeted` / `generic` | 失败后走定向修复还是通用重试 |
| `max_attempts` | 2 | 每题最多 2 次 SQL 尝试（1 次生成 + 1 次修复） |
| `top_k` | 20 | 检索返回的表数 |
| `max_result_rows` | 500,000 | 超过即判 `TOO_MANY_ROWS` |

命令行组合出 4 个实验组：

```bash
python scripts/phase6.py --strategy targeted --verifier self
```

`--strategy generic`、`--verifier oracle`、`--policy generic`、`--disable SchemaSearch` 分别对应对照组、上界、无策略消融、单技能消融。

**为什么这样设计**：所有部件通过构造函数注入（依赖注入），换模型、换 verifier、禁用技能都只改参数不改代码。两组实验用**同一个** controller 类，差别只在 `strategy`，对比才公平。

---

## Step 1 逐题循环：Loop 只拿到"题面"

**源码** `evaluation/loop_run.py:44-48`

```python
for i, case in enumerate(cases, 1):
    res = controller.run(case.agent_view(), verifier_for(case.case_id))
    judge = judges[case.case_id]                        # Gold 判定器留在 Loop 外面
    correct = [judge(rows)[0] if a["execution_status"] == "SUCCESS" else False
               for a, rows in zip(res.attempts, res.rows)]
```

`agent_view()` 的定义 `benchmark/beaver/dataset.py:75-76`：

```python
def agent_view(self) -> AgentTask:
    return AgentTask(case_id=self.case_id, question=self.question, db=self.db)
```

**为什么**：`BeaverCase` 里有 Gold SQL 和标注，`AgentTask` 只有三个字段。Loop 在类型上就拿不到 Gold。判分用的 `judge` 在 `controller.run()` 返回**之后**才调用。

---

## Step 2 检索：选出 20 张候选表

**源码** `loop_engineer/controller.py:96` → `agent/retriever.py:147-149`

```python
retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)
# retriever.py
ranked = sorted(self.score(question).items(), key=lambda kv: (-kv[1], kv[0]))[:k]
return Retrieval(tuple(t for t, _ in ranked), tuple(round(s, 4) for _, s in ranked))
```

BM25 的文档由表名、列名、样例值组成，字段权重 `table:3, column:2, value:1`（`agent/retriever.py:33`）。排序时分数相同按表名排，保证结果确定。

**真实数据**（dw_4188）：20 张表，前几张是 `tip_detail, library_material_status, cis_course_catalog, course_catalog_subject_offered, academic_terms, library_subject_offered, academic_terms_all, ...`

---

## Step 3 生成：attempt 1

**源码** `loop_engineer/controller.py:97-102`

```python
gen = self.generator.generate(task, retrieval.tables)
attempt = {"case_id": task.case_id, "attempt_id": 1, "question": task.question,
           "retrieved_tables": list(retrieval.tables), "generated_sql": gen.sql,
           "parse_status": gen.parse_status, "strategy": self.cfg.strategy,
           "input_tokens": gen.llm.input_tokens, "output_tokens": gen.llm.output_tokens,
           "llm_latency_ms": gen.llm.latency_ms, "used_llm": True, "diag_tokens": 0}
```

`FewShotGenerator.generate`（`agent/generator.py:130-137`）拼 prompt（规则 + 20 张表的 schema + 3 个 few-shot 示例 + 问题）→ 调 LLM → 从 ```` ```sql ```` 代码块里抽 SQL。

LLM 调用经过缓存层 `agent/llm.py:195-208`：

```python
key = prompt_fingerprint(self.inner.model, self.inner.params, system, prompt)   # SHA-256
if key in self._cache:
    r = LlmResponse(**{**self._cache[key], "cached": True})
    self.inner.meter.record(r)
    return r
```

**真实数据**（dw_4188，节选）：输入 13,353 token，输出 321 token，耗时 22.3 秒。

```sql
FROM ACADEMIC_TERMS_ALL ata
JOIN SIS_DEPARTMENT sd ON ata.DEPARTMENT_CODE = sd.DEPARTMENT_CODE
JOIN SUBJECT_OFFERED lib_subjects ON ata.TERM_CODE = lib_subjects.TERM_CODE AND ata.ACADEMIC_YEAR = lib_subjects.ACADEMIC_YEAR
JOIN LIBRARY_SUBJECT_OFFERED lso ON lib_subjects.SUBJECT_ID = lso.SUBJECT_ID
JOIN LIBRARY_MATERIAL_STATUS lms ON lso.LIBRARY_MATERIAL_STATUS_KEY = lms.LIBRARY_MATERIAL_STATUS_KEY
```

**为什么**：贪心解码 + 缓存意味着 targeted 组和 generic 组的 attempt 1 **逐字相同**（第二组直接重放缓存），两组的差别只来自修复策略。

---

## Step 4 执行：在 Databricks 上跑

**源码** `loop_engineer/controller.py:72-80`

```python
def _execute(self, sql, db, parse_status):
    if not sql:                                       # 模型没给出 SQL：不执行，状态沿用 parse_status
        return {"execution_status": parse_status, ...}, []
    ex = self.executor.execute(sql, db, max_rows=self.cfg.max_result_rows)
    rows = ex.rows if ex.ok else []
    return {"execution_status": ex.status, "execution_error": (ex.error or "")[:2000] or None,
            "result_row_count": len(rows) if ex.ok else None,
            "result_preview": _serialize_preview(rows) if ex.ok else None,   # 只留前 5 行
            "exec_latency_ms": ex.elapsed_ms}, rows
```

执行器 `execution/databricks_sql.py:106-130`：只读检查 → 切 schema → 执行 → 超行数返回 `TOO_MANY_ROWS` → 异常时抽出错误类别。

**真实数据**（dw_4188）：

```
execution_status: ERROR
execution_error : [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter
                  with name `ata`.`DEPARTMENT_CODE` cannot be resolved. Did you mean one of the
                  following? [`sd`.`DEPARTMENT_CODE`, `sd`.`DEPARTMENT_NAME`, `ata`.`TERM_CODE`,
                  `sd`.`DEPT_BUDGET_CODE`, `ata`.`TERM_END_DATE`]. SQLSTATE: 42703; line 12 pos 25
```

**为什么**：完整结果 `rows` 不写进 `attempt`，只放进单独的列表（Step 13 判分用）；`attempt` 里只有行数和前 5 行预览。这样 trace 体积可控，也避免 Agent 侧误用完整结果。

---

## Step 5 校验：不看 Gold，自己判断要不要修

**源码** `loop_engineer/controller.py:107-111` → `loop_engineer/verifier.py:31-47`

```python
decision = verifier.verify(attempt, rows)
attempt.update({"verifier_mode": decision.mode, "verifier_decision": "PASS" if decision.passed else "FAIL",
                "verifier_signals": list(decision.signals)})
```

```python
# SelfVerifier.verify
if status in ("NO_SQL", "EMPTY_RESPONSE"):                    hits.append("no_sql")
elif status == "TOO_MANY_ROWS":                                hits.append("too_many_rows")
elif status not in ("SUCCESS", "NO_SQL", "EMPTY_RESPONSE", "TOO_MANY_ROWS"):
                                                               hits.append("execution_error")
elif status == "SUCCESS":
    if not rows:                                               hits.append("empty_result")
    elif any(all(r[i] is None for r in rows) for i in range(len(rows[0]))):
                                                               hits.append("all_null_column")
return VerifierDecision(not hits, self.mode, tuple(hits))
```

**参数**：`SELF_SIGNALS = ("no_sql", "execution_error", "too_many_rows", "empty_result", "all_null_column")`（`verifier.py:14`），可以按信号开关做消融。

**真实数据**：dw_4188 → `FAIL ['execution_error']`。全组 30 题的 attempt 1：触发且确实错 27、没触发但其实错 3（漏报）、误报 0。

**为什么**：这 5 个信号在生产环境都拿得到。`OracleVerifier`（`verifier.py` 下半部分）用 Gold 判断，等于告诉 Loop"这题错了"，只作为上界单独标注。漏报的 3 题说明 SelfVerifier 抓不到"能跑但答案错"的情况，这是已知局限（见文末）。

---

## Step 6 终止判断

**源码** `loop_engineer/controller.py:110-113`

```python
attempts.append(attempt)
all_rows.append(rows)
if decision.passed or n == self.cfg.max_attempts:
    break
```

两个出口：通过校验，或预算（`max_attempts=2`）用完。第 2 次尝试失败后不会再诊断和修复，所以最后一次的诊断字段为空。

---

## Step 7 观察：用白名单把失败"翻译"成结构化信号

从这里开始是 targeted 组；generic 组的分支见 Step 10b。

**源码** `loop_engineer/controller.py:121` → `loop_engineer/observer.py:16-17, 46-68`

```python
OBSERVABLE_FIELDS = ("case_id", "attempt_id", "question", "retrieved_tables", "generated_sql", "parse_status",
                     "execution_status", "execution_error", "result_row_count", "result_preview")

def observe(record, db):
    r = {k: record.get(k) for k in OBSERVABLE_FIELDS}  # whitelist: nothing else passes
    err = r["execution_error"] or ""
    m_cls = _ERROR_CLASS.search(err)     # [UNRESOLVED_COLUMN.WITH_SUGGESTION] → UNRESOLVED_COLUMN
    m_col = _UNRESOLVED.search(err)      # name `ata`.`DEPARTMENT_CODE` cannot be resolved
    m_sug = _SUGGEST.search(err)         # Did you mean one of the following? [...]
    m_tab = _TABLE_NOT_FOUND.search(err)
    ...
```

正则定义在 `observer.py:19-22`。

**真实数据**（dw_4188 的 Observation）：

| 字段 | 值 |
|---|---|
| `error_class` | `UNRESOLVED_COLUMN` |
| `unresolved_qualifier` | `ata` |
| `unresolved_column` | `DEPARTMENT_CODE` |
| `suggestions` | `sd.DEPARTMENT_CODE, sd.DEPARTMENT_NAME, ata.TERM_CODE, sd.DEPT_BUDGET_CODE, ata.TERM_END_DATE` |

**为什么是白名单而不是黑名单**：将来 attempt 记录里如果多了一个 Gold 衍生字段（比如有人加了 `correct`），黑名单会漏掉，白名单天然挡住。Observer 是 Loop 的"防火墙"。

---

## Step 8 诊断：错在哪？（规则优先，LLM 兜底）

**源码** `loop_engineer/controller.py:122` → `loop_engineer/diagnose.py:165-184`

```python
def diagnose(self, obs):
    d = diagnose_by_rules(obs, self.catalog)
    if d is not None:
        return d, {}                          # 规则命中：不花 token
    if self.client is None:
        return Diagnosis(UNKNOWN, 0.0, "no explicit signal and no LLM stage", "fallback"), {}
    prompt = LLM_PROMPT.format(...)           # 只有 SQL 跑通了、但被 verifier 怀疑时才走到这里
    r = self.client.complete(prompt, system=LLM_SYSTEM)
    ...
```

核心是**在 schema 里给报错的列定位** `loop_engineer/diagnose.py:75-97`：

```python
if cls == "UNRESOLVED_COLUMN" and obs.unresolved_column:
    col = obs.unresolved_column
    aliases = alias_map(obs.generated_sql)          # 别名 → 真实表名
    used = set(aliases.values())                    # SQL 实际用到的表
    owner = column_owners(catalog, col)             # schema 里有这个列的所有表
    retrieved = set(obs.retrieved_tables)
    in_used = [t for t in owner if t in used]
    in_retrieved = [t for t in owner if t in retrieved and t not in used]
    if in_used:      -> COLUMN_MAPPING  0.9   case="wrong_alias"      # 列在已用的表里，只是挂错别名
    if in_retrieved: -> TABLE_RETRIEVAL 0.8   case="table_not_used"   # 列在检索到但没用的表里
    if owner:        -> TABLE_RETRIEVAL 0.85  case="not_retrieved"    # 列只在没检索到的表里
    else:            -> COLUMN_MAPPING  0.7   case="hallucinated"     # 没有任何表有这个列
```

其它规则（`diagnose.py:99-111`）：表不存在 → TABLE_RETRIEVAL；结果超行数 → JOIN_KEY（0.6）；语法/聚合等 SQL 级错误 → EXECUTION。

**真实数据**（dw_4188）：`DEPARTMENT_CODE` 的拥有者有 9 张表，其中 `sis_department` 已经被 SQL 用了（别名 `sd`）→ 命中 `in_used`：

```
failure_type         : COLUMN_MAPPING_FAILURE
diagnosis_confidence : 0.9
diagnosis_source     : rule
diagnosis_reason     : DEPARTMENT_CODE exists in sis_department, which the SQL already uses,
                       but was referenced through another alias
repair_hints         : {"signal": "unresolved_column", "column": "DEPARTMENT_CODE", "qualifier": "ata",
                        "qualifier_table": "academic_terms_all",
                        "owner_tables": ["cis_course_catalog", ..., "sis_department", ...],
                        "suggestions": ["sd.DEPARTMENT_CODE", ...], "case": "wrong_alias"}
```

全组 27 次诊断中，26 次由规则完成，只有 1 次（dw_5183，SQL 跑通但结果可疑）调了 LLM。

**为什么能这样归因**：Databricks 已经告诉我们"哪个列、在哪个别名下找不到"，剩下的问题只是"这个列实际在哪张表"，这是一次 schema 查找，是确定性的。LLM 在这类问题上反而更差（评测集上 LLM 级诊断严格准确率 0/19，而规则级宽松准确率 76.5%）。`repair_hints` 把查找过程的证据（拥有者表、候选列、case）原样传给下一步，技能不用重新推理。

---

## Step 9 路由：失败类型 → 技能（映射是数据）

**源码** `loop_engineer/controller.py:123` → `loop_engineer/policy.py:27-35, 52-60`

```python
TARGETED = {
    TABLE_RETRIEVAL: "RetrieveAgain",
    COLUMN_MAPPING: "SchemaSearch",
    JOIN_KEY: "FindJoinPath",
    DOMAIN_KNOWLEDGE: "ReplanQuery",   # fallback until RetrieveKnowledge exists
    QUERY_DECOMPOSITION: "ReplanQuery",
    EXECUTION: "RepairSQL",
    UNKNOWN: "RepairSQL",
}

def route(self, diagnosis):
    if self.mode == "generic":
        return Route(FALLBACK, False, "policy disabled: generic repair")
    skill = self.mapping.get(diagnosis.failure_type, FALLBACK)
    if skill in self.disabled:
        return Route(FALLBACK, True, f"{skill} disabled (ablation)")
    return Route(skill, diagnosis.failure_type in FALLBACK_ROUTES, f"{diagnosis.failure_type} -> {skill}")
```

**真实数据**：dw_4188 → `Route("SchemaSearch", False, "COLUMN_MAPPING_FAILURE -> SchemaSearch")`。全组路由分布：RetrieveAgain 14、SchemaSearch 9、RepairSQL 3、ReplanQuery 1。

**为什么**：路由表是 dict，消融实验只换表或禁用技能（`--policy generic` 全部走 RepairSQL，`--disable X` 让 X 回退到 RepairSQL），不改代码。`fallback=True` 会写进 trace，看板上能看出"这次是走了兜底"。

---

## Step 10 修复：技能执行

所有技能实现同一个接口 `skills/base.py:44-47`：

```python
class RepairSkill(Protocol):
    name: str
    def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult: ...
```

`RepairContext`（`skills/base.py:22-27`）只包含 Agent 可见的资源：schema、LLM 客户端、few-shot 示例、`max_schema_tables=24`。

需要 LLM 时，所有技能共用一个模板 `skills/base.py:50-67`，**技能之间的差别只在 `{instruction}` 和给哪些表的 schema**：

~~~text
{rules}
Schema:
{schema}
Question: {question}
A previous attempt produced this SQL:
```sql
{sql}
```
{observed}                 ← 例如 "Executing it failed with: [UNRESOLVED_COLUMN...]"
Diagnosis: {diagnosis}     ← 例如 "COLUMN_MAPPING_FAILURE: DEPARTMENT_CODE exists in ..."
{instruction}              ← 每个技能自己的定向指令
Return the corrected query as ONE read-only SQL query inside a ```sql code fence. No explanation.
~~~

调用封装在 `skills/base.py:78-89` `llm_repair`：表去重、只保留 schema 里真实存在的表、截到 24 张。

### 10a. SchemaSearch（dw_4188 走的路径）

**源码** `skills/schema_search.py:104-133`

```python
fix = fix_column_refs(obs.generated_sql, ctx.catalog)        # ① 确定性修复
if fix.changes and not fix.unresolved:
    return RepairResult(fix.sql, self.name, "re-pointed ... deterministic", ..., used_llm=False)
# ② 规则决定不了的部分交给 LLM，从部分修好的 SQL 开始
...
if any("needs a real join key" in p for p in problems):
    cands = [j.sql() for j in join_candidates(ctx.catalog, used)][:10]
    joins = f"Join keys shared by the tables in the query (from the schema): {'; '.join(cands)}. ..."
instruction = ("These column references are wrong: " + "; ".join(problems) + ". " + joins +
               f"Candidate columns: {', '.join(candidates)}. Map every phrase of the question to a column "
               "that really exists in the schema; ...")
return llm_repair(self.name, start, diagnosis, ctx, [*used, *obs.retrieved_tables], instruction, action)
```

确定性部分 `fix_column_refs`（`skills/schema_search.py:39-91`）做两件事：

1. **逐作用域检查每一个带限定符的列**（`:48-69`）：如果 `a.COL` 的表没有 `COL`，而同一作用域里恰好只有一张表有它，就改过去；0 张或多张有它，就记为 unresolved。
   这一版修复所有列，而不是只修报错的那一个，因为引擎一次只报第一个错。
2. **Join 自等式守卫**（`:70-85`）：如果改完后一个比较两边变成同一张表（如 `sd.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`），SQL 能跑但两张表不再有关联。这种修改会被撤回，标记为"需要真正的关联键"。

**真实数据**（dw_4188 上重放 `fix_column_refs`）：

```
changes    : []   ← 所有候选改动都被守卫撤回了
unresolved :
  ata.DEPARTMENT_CODE in join/compare condition 'ata.DEPARTMENT_CODE = sd.DEPARTMENT_CODE' (needs a real join key)
  lso.LIBRARY_MATERIAL_STATUS_KEY in join/compare condition 'lso.LIBRARY_MATERIAL_STATUS_KEY = lms.LIBRARY_MATERIAL_STATUS_KEY' (needs a real join key)
  lib_subjects.ACADEMIC_YEAR in join/compare condition 'ata.ACADEMIC_YEAR = lib_subjects.ACADEMIC_YEAR' (needs a real join key)
```

没有守卫时，第一条会被改成 `sd.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`：SQL 能执行、verifier 会放行，但结果是笛卡尔式的错答案。这就是问题 #8，Phase 5 的"可执行 4→10"因此虚高，修正后为 4→8。

守卫之后，3 个问题连同 schema 推断的关联键一起交给 LLM，`repair_action = "LLM rewrite for 3 unresolved reference(s)"`。

### 10b. 对照：Generic Retry 组

**源码** `loop_engineer/controller.py:82-93, 118-119`

```python
GENERIC_INSTRUCTION = "The query above is wrong. Write a corrected query."
...
prompt = REPAIR_TEMPLATE.format(rules=RULES, schema=render_schema(self.ctx.catalog, tables), ...,
                                diagnosis="(not diagnosed)", instruction=GENERIC_INSTRUCTION)
```

同一个模板、同样的报错信息、同样的检索表，只是**没有诊断、没有定向 instruction、没有额外的表**。这保证两组的差别只来自"诊断 + 定向修复"。

### 10c. RetrieveAgain（另一条常见路径）

**源码** `skills/retrieve_again.py:35-43`

```python
add = list(h.get("tables_to_add") or h.get("owner_tables") or [])[:4]
joins = [j.sql() for t in add for j in connect(ctx.catalog, t, used)][:8]      # 新表怎么接到已用的表上
instruction = (f"Column {h.get('column')} is not in the table you used; it lives in {', '.join(add)}. "
               f"Join the appropriate one of these tables. Join candidates inferred from the schema: ...")
```

这里直接用了 Step 8 的 `repair_hints["tables_to_add"]`：诊断找到的证据，修复直接消费。

---

## Step 11 状态交接：写回诊断，组装下一次尝试

**源码** `loop_engineer/controller.py:115-137`

```python
nxt = {"case_id": task.case_id, "attempt_id": n + 1, "question": task.question,
       "retrieved_tables": attempt["retrieved_tables"], "strategy": self.cfg.strategy, "diag_tokens": 0}
...
attempt.update({"failure_type": diag.failure_type, "diagnosis_confidence": diag.confidence,     # 诊断写在失败的那次上
                "diagnosis_reason": diag.reason, "diagnosis_source": diag.source,
                "repair_hints": json.dumps(diag.repair_hints, ensure_ascii=False)})
nxt.update({"generated_sql": res.repaired_sql, "parse_status": res.parse_status,             # 修复写在新的那次上
            "repair_skill": route.skill, "repair_action": res.repair_action,
            "repair_reason": res.repair_reason, "repair_fallback": route.fallback,
            "used_llm": res.used_llm, "input_tokens": res.input_tokens, ...,
            "diag_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "tables": list(res.tables)})
attempt["repaired_sql"] = nxt["generated_sql"]     # 前后两次互相指向，trace 里能串起来
attempt["repair_skill"] = nxt["repair_skill"]
attempt = nxt                                      # 回到 Step 4
```

**约定**："为什么失败"记在失败的尝试上，"做了什么修复"记在新尝试上。看板按 `attempt_id` 排好，就是一条"失败 → 诊断 → 修复 → 结果"的时间线。

---

## Step 4'–6' 第二次执行和校验

回到循环顶部，对新 SQL 再执行一次、校验一次。因为 `n == max_attempts`，无论结果如何都退出。

**真实数据**（dw_4188 attempt 2）：LLM 只给表名加了 `dw.` 前缀，关联条件没改 → 同样的 `UNRESOLVED_COLUMN` → `FAIL`。输入 13,940 token，耗时 20.5 秒。

诊断和路由都是对的，技能也给出了正确的线索，但 glm-4-flash 没有按指令改写。这和干预实验的结论一致：**这个模型即使拿到完整的 Gold 提示也修不好（0/30）**，瓶颈在模型，不在 Loop 给的信息。

---

## Step 12 选出最终答案

**源码** `loop_engineer/controller.py:138-143`

```python
passed   = [i for i, a in enumerate(attempts) if a["verifier_decision"] == "PASS"]
executed = [i for i, a in enumerate(attempts) if a["execution_status"] == "SUCCESS"]
final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
for i, a in enumerate(attempts):
    a["final_status"] = "FINAL" if i == final else "SUPERSEDED"
return LoopResult(attempts, all_rows, final)
```

优先级：最后一个通过校验的 → 最后一个能执行的 → 最后一个。

**为什么不直接取最后一个**：如果修复把一个能跑的 SQL 改坏了（Harm），这条规则会保留原来能跑的那个。选择只用 verifier 和执行状态，不看 Gold。

---

## Step 13 事后判分：Loop 结束后才用 Gold

**源码** `evaluation/loop_run.py:46-55`

```python
judge = judges[case.case_id]
correct = [judge(rows)[0] if a["execution_status"] == "SUCCESS" else False
           for a, rows in zip(res.attempts, res.rows)]
rec = {..., "attempt_correct": correct, "final_index": res.final_index,
       "first_correct": correct[0], "final_correct": correct[res.final_index],
       "tokens": ..., "extra_tokens": sum(_tokens(a) for a in res.attempts[1:]), ...}
```

对错存在 `rec["attempt_correct"]`，**不写回 `attempts`**。发布时 `attempts` 进 `traces.*`，`correct` 进 `evaluation.*`。

---

## Step 14 指标汇总

**源码** `evaluation/loop_run.py:67-110`

| 指标 | 公式 | 回答的问题 |
|---|---|---|
| Recovery Rate | 首次错且最终对 / 首次错 | 修好了多少 |
| Harm Rate | 首次对且最终错 / 首次对 | 改坏了多少 |
| Net Gain | recovered − harmed | 净收益 |
| `executable_first/final` | 首次 / 最终能执行的题数 | 过程指标 |
| `verifier_confusion` | 触发且错 / 误报 / 漏报 / 放行且对 | Verifier 质量 |
| `per_skill` | 每个技能：修复次数、修复后能执行、恢复 | 技能质量 |
| `extra_tokens_per_net_recovery` | 额外 token / Net Gain | 成本效率 |

**真实数据**（开发集，两组对比）：

| | Targeted + Self | Generic + Self |
|---|---|---|
| 可执行 首次 → 最终 | 4 → **9** | 4 → 6 |
| 答对 首次 → 最终 | 0 → 0 | 0 → 0 |
| Verifier：命中 / 漏报 / 误报 | 27 / 3 / 0 | 27 / 3 / 0 |
| 额外 token | 369,635 | 361,964 |
| 单技能（修复后能执行 / 修复次数） | RetrieveAgain 3/14、SchemaSearch 2/9、RepairSQL 0/3、ReplanQuery 0/1 | GenericRetry 3/27 |

---

## Step 15 发布与回放

```bash
python scripts/publish_run.py runs/phase6/targeted-self-20260925T105746-eb2ade
```

`dbx/publish.py:35-45` `flatten_loop_records` 把每题的尝试展平成一行一次，写入 `traces.execution_traces`（字段见 `dbx/tables.py:53-67`）；`correct` 另写到 `evaluation.evaluation_results`；汇总写 `evaluation.runs` 和 MLflow。

Loop Debug Console（`app/dashboard.py`，本地 `.claude/launch.json` 里的 `loop-console`，端口 8502）从这些表读数据，可以选一个 run、选一道题，看到上面 Step 3–12 的每一个字段。

---

## 附录 A：两道题的完整轨迹

### A1. dw_4188：诊断对了，模型没修好

| 步骤 | 内容 |
|---|---|
| 生成 | 5 张表的 JOIN；`ata.DEPARTMENT_CODE`（`academic_terms_all` 没有这个列） |
| 执行 | `ERROR` `UNRESOLVED_COLUMN`，候选 `sd.DEPARTMENT_CODE` |
| 校验 | `FAIL ['execution_error']` |
| 观察 | qualifier=`ata`，column=`DEPARTMENT_CODE` |
| 诊断 | 规则：列在已使用的 `sis_department` 中 → `COLUMN_MAPPING_FAILURE` 0.9，`wrong_alias` |
| 路由 | `SchemaSearch` |
| 修复 | 确定性改动全部被自等式守卫撤回 → 3 个关联问题 + schema 关联键交给 LLM |
| 第 2 次 | LLM 只加了 `dw.` 前缀 → 同样报错 → `FAIL`，预算用完 |
| 最终 | attempt 2（两次都没执行成功，取最后一个） |
| 判分 | 错 / 错 |

**讲点**：Loop 的每个环节都给出了正确的信息，失败在模型执行指令的能力。守卫阻止了一次"能跑但错"的假修复。

### A2. dw_5478：修复让 SQL 能跑了，但答案仍然错（Verifier 漏报）

| 步骤 | 内容 |
|---|---|
| 生成 | 用 `sdp.GRADUATE_LEVEL` 过滤研究生 |
| 执行 | `ERROR` `UNRESOLVED_COLUMN`：`sdp`.`GRADUATE_LEVEL` |
| 诊断 | 规则：`GRADUATE_LEVEL` 只在 `sis_course_description`，这张表检索到了但没用 → `TABLE_RETRIEVAL_FAILURE` 0.8，`table_not_used`，`tables_to_add=['sis_course_description']` |
| 路由 | `RetrieveAgain` |
| 修复 | 加入 `sis_course_description` 和推断的关联键 → LLM 改写：`JOIN SIS_COURSE_DESCRIPTION scd ON lo.COURSE_NUMBER = scd.COURSE` |
| 第 2 次 | `SUCCESS`，1 行 `["Mathematics", "40.331854", "77.43414"]` → `PASS` |
| 判分 | 错 / 错 |

**讲点**：
1. 诊断 → 技能 → 证据传递完整：`tables_to_add` 从诊断直接进入技能的 instruction。
2. 模型改写时**丢掉了"研究生"这个条件**，换成了 `scd.IS_DEGREE_GRANTING = 'Y'`。SQL 能跑、结果非空，SelfVerifier 没有信号可以发现这个语义错误。这就是"能执行但答案错"的漏报，是下一步要补的能力（例如加一个检查问题里每个条件是否都在 SQL 里出现的 verifier）。

---

## 附录 B：已知局限与对应代码位置

| 局限 | 位置 | 可能的改进 |
|---|---|---|
| 每题只修 1 次 | `LoopConfig.max_attempts = 2`（`controller.py:41`） | 预算提高后需要同时看 Harm Rate |
| 诊断置信度没有校准 | `diagnose.py:84-97` 中的 0.9 / 0.8 / 0.85 / 0.7 是人工设定 | 用诊断准确率数据做校准 |
| 只看第一个报错 | 引擎只报第一个错；`SchemaSearch` 已用全量检查缓解 | 其它技能也加确定性预检 |
| 确定性预处理只在 `SchemaSearch` 有 | `skills/schema_search.py:39` | `RetrieveAgain` 可以确定性地补 JOIN |
| `RetrieveKnowledge` 是占位 | `skills/retrieve_knowledge.py:15`；路由暂走 `ReplanQuery`（`policy.py:31`） | 需要不含 Gold 的知识源 |
| 能跑但错的答案抓不到 | `SelfVerifier`（`verifier.py:25`），dw_5478 | 语义一致性检查 / LLM judge |
