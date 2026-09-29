# Loop 逐步讲解（对照源码）

> 从一次运行的入口开始，按执行顺序讲完一道题在 Loop 里的全过程。每一步给出：**做什么 → 源码位置和代码 → 真实数据 → 为什么这样设计**。
> 整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，设计取舍见 [LOOP_DESIGN.md](LOOP_DESIGN.md)，每次实验的数据见 [EXECUTION_LOG.md](EXECUTION_LOG.md)。
>
> 代码和行号以 2026-09-29 的代码为准。示例数据来自两次真实运行：
> - `runs/phase6/targeted-self-20260925T105746-eb2ade`：开发集 30 题，glm-4-flash，固定示例，自检 v1；
> - `console-targeted-self-20260928T105340-eef1a9`：在线平台运行，开发集 30 题，deepseek-flash，自检 v2，最多修复 4 次。

---

## 0. 全景：两个入口，一条执行路径

```
入口 A：命令行                                       入口 B：网页平台（Databricks App）
scripts/phase6.py  main()          Step 0 装配       app/app_pages/run_loop.py  点"执行"
                                                     └ app/runner.py  start_run → 后台线程 → _execute（装配同 A）
        └───────────────┬────────────────────────────────────────┘
evaluation/loop_run.py  run_arm()                    Step 1  逐题循环，只把题面交给 Loop
└─ loop_engineer/controller.py  LoopController.run()
     ├─ retriever.retrieve()                         Step 2  检索候选表
     ├─ generator.generate()                         Step 3  生成第 1 次 SQL（固定示例 / 相似题示例）
     └─ for n in 1..max_attempts:                    max_attempts = 最大修复次数 + 1
          ├─ _execute()                              Step 4  执行
          ├─ verifier.verify()                       Step 5  自检（v2：显式失败 + 结构规则 + 数值一致性）
          ├─ 自检通过 或 已达上限 → break              Step 6  终止判断
          ├─ observe()                               Step 7  观察（白名单）
          ├─ diagnoser.diagnose()                    Step 8  诊断
          ├─ policy.route()                          Step 9  路由
          ├─ skill.repair()                          Step 10 修复
          └─ 写回诊断 + 组装下一次尝试                 Step 11 状态交接
     └─ 选出最终答案                                  Step 12
├─ judge(rows)：Loop 结束后才用 Gold 判分           Step 13
└─ summarize()                                       Step 14 指标汇总
发布到 Delta / MLflow → Console 回放；网页运行另有实时 trace 和分析报告   Step 15
```

**一个上下文对象贯穿全程**：`attempt`（一个 dict）。每一步往里加字段，最后整个 dict 就是这次尝试的 trace：

| 步骤 | 写入 `attempt` 的字段 |
|---|---|
| Step 3 生成 | `case_id, attempt_id, question, retrieved_tables, generated_sql, parse_status, strategy, input_tokens, output_tokens, llm_latency_ms, used_llm, diag_tokens` |
| Step 4 执行 | `execution_status, execution_error, result_row_count, result_preview, exec_latency_ms` |
| Step 5 自检 | `verifier_mode, verifier_decision, verifier_signals, verifier_findings, verifier_advisories` |
| Step 11 写回（失败的那次） | `failure_type, diagnosis_confidence, diagnosis_reason, diagnosis_source, repair_hints, repaired_sql, repair_skill` |
| Step 10/11（新的一次） | `repair_skill, repair_action, repair_reason, repair_fallback, tables` 以及新的 SQL 和 token |
| Step 12 | `final_status`（FINAL / SUPERSEDED） |

**表里没有任何"对不对"的字段**。对错在 Step 13 才计算，并且存在另一个地方。

**实时事件**：`run(..., on_event=cb)` 在每一步调用 `cb(step, payload)`。事件包括 retrieve、generate、execute、verify、observe、diagnose、route、repair、final，网页的实时时间线和分析报告都来自这些事件。事件只做通知，不改变 Loop 的行为；回调出错也不会中断 Loop（[controller.py:108-114](../loop_engineer/controller.py)）。

---

## Step 0 装配：把所有部件接起来

**做什么**：读配置，加载题目、schema、示例；连接 Databricks；创建 LLM 客户端；把各部件注入 `LoopController`。

**源码** [scripts/phase6.py:90-111](../scripts/phase6.py)

```python
meter = UsageMeter(max_calls=int(lc["max_calls"]), max_tokens=int(lc["max_tokens"]))    # 预算上限
inner = make_client(lc, max_output_tokens=int(lc["max_output_tokens"]), meter=meter)    # 智谱 / DeepSeek
client = CachingChatClient(inner, Path(lc["cache"]))                                    # prompt 指纹缓存
policy = Policy(mode=args.policy, disabled={s for s in args.disable.split(",") if s})   # 消融开关
generator = FewShotGenerator(client, catalog, examples,
                             index=build_generator_index(queries, eval_ids, dev_ids, fs),  # 相似题示例（可选）
                             k=int(fs.get("dynamic_k", 4)), max_extra_tables=int(fs.get("dynamic_max_extra_tables", 6)))
controller = LoopController(BM25TableRetriever(catalog), generator, dbx,
                            Diagnoser(catalog, client), policy, RepairContext(catalog, client, examples),
                            LoopConfig(strategy=args.strategy, max_attempts=args.max_repairs + 1,
                                       top_k=int(cfg["retrieval"]["top_k"]),
                                       max_result_rows=int(d["max_result_rows"])))
verifier_for = (lambda cid: SelfVerifier()) if args.verifier == "self" else (lambda cid: OracleVerifier(judges[cid]))
run_id, summary = run_arm(cases, judges, controller, verifier_for, arm, Path("runs/phase6"), meta)
```

网页平台的装配在 [app/runner.py:190-235](../app/runner.py) `_execute`，写法相同。另外两点：
- 每次点击的调用上限按题数和修复次数放大：`calls = (1 + 2 × 修复次数) × 题数 + 4`；
- [runner.py:260](../app/runner.py) `start_run` 在后台线程里运行 `_execute`，页面每秒拉取一次事件（[run_loop.py:430](../app/app_pages/run_loop.py)）。

**命令行参数**（[phase6.py:44-53](../scripts/phase6.py)）

| 参数 | 取值 | 含义 |
|---|---|---|
| `--strategy` | `targeted` / `generic` | 失败后走定向修复，还是通用重试（对照组） |
| `--verifier` | `self` / `oracle` | 无 Gold 自检；Oracle 用 Gold，只作上界 |
| `--max-repairs` | 默认 1 | 每题最多修复几轮，SQL 尝试次数 = 修复次数 + 1 |
| `--few-shot` | `static` / `dynamic` | 固定 3 个示例，或按问题检索最相似的已解题 |
| `--policy` / `--disable` | | 消融：全部走 RepairSQL / 禁用某个技能 |
| `--split` / `--eval` | 默认 dev | 评测集需要 `--eval` 确认（决策 D2） |

`LoopConfig` 的默认值见 [controller.py:47-52](../loop_engineer/controller.py)：`top_k=20`，结果超过 50 万行判为"结果过大"。

```bash
python scripts/phase6.py --strategy targeted --verifier self --max-repairs 2 --few-shot dynamic --limit 3
```

**为什么这样设计**：所有部件通过构造函数注入。换模型、换自检、换示例方式、禁用技能，都只改参数不改代码。两个实验组用**同一个** controller，差别只在 `strategy`，对比才公平；命令行和网页也走同一条路径，所以从哪个入口跑都是同一个实验。

---

## Step 1 逐题循环：Loop 只拿到"题面"

**源码** [evaluation/loop_run.py:47-64](../evaluation/loop_run.py)

```python
for i, case in enumerate(cases, 1):
    cb = (lambda step, payload, cid=case.case_id: on_event(cid, step, payload)) if on_event else None
    res = controller.run(case.agent_view(), verifier_for(case.case_id), on_event=cb)
    judge = judges[case.case_id]                        # Gold 判定器留在 Loop 外面
    correct = [judge(rows)[0] if a["execution_status"] == "SUCCESS" else False
               for a, rows in zip(res.attempts, res.rows)]
    ...
    on_event(case.case_id, "judged", {...})              # 判分事件来自评测侧，在 Loop 结束之后
```

`agent_view()` 定义在 [benchmark/beaver/dataset.py:75](../benchmark/beaver/dataset.py)：

```python
def agent_view(self) -> AgentTask:
    return AgentTask(case_id=self.case_id, question=self.question, db=self.db)
```

**为什么**：`BeaverCase` 里有 Gold SQL 和标注，`AgentTask` 只有三个字段，Loop 在类型上就拿不到 Gold。判分用的 `judge` 在 `controller.run()` 返回**之后**才调用。

---

## Step 2 检索：选出 20 张候选表

**源码** [controller.py:116](../loop_engineer/controller.py) → [agent/retriever.py](../agent/retriever.py) `retrieve`

```python
retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)
```

BM25 的文档由表名、列名、样例值组成，字段权重为 `table:3, column:2, value:1`（[retriever.py:33](../agent/retriever.py)）。分数相同时按表名排序，保证结果确定。

**真实数据**：开发集平均表召回率 0.92。deepseek 运行的错误分析显示，Gold 用到而生成 SQL 没用的表共 41 次，其中 **34 次其实已经在这 20 张里**。瓶颈不在检索，而在下一步选哪张表。

---

## Step 3 生成：第 1 次 SQL

**源码** [controller.py:118-133](../loop_engineer/controller.py)

```python
gen = self.generator.generate(task, retrieval.tables)
# dynamic few-shot may add the tables of similar solved questions to the schema the model saw
shown = list(getattr(gen, "schema_tables", ()) or retrieval.tables)
attempt = {"case_id": task.case_id, "attempt_id": 1, "question": task.question,
           "retrieved_tables": shown, "generated_sql": gen.sql, ...}
```

生成器 [agent/generator.py:147-159](../agent/generator.py) 有两种示例方式：

```python
examples = self.examples                                        # static：固定 3 个示例（baseline-v2）
if self.index is not None:                                      # dynamic：相似题示例（baseline-v3-dynfs）
    hits = self.index.top(task.question, self.k)                # 最相似的 4 道已解题
    examples = [h.example for h in hits]
    extra = [t for h in hits for t in h.tables if t in self.catalog.tables and t not in tables]
    tables = tuple(tables) + tuple(dict.fromkeys(extra))[: self.max_extra_tables]   # 示例用到的表补进 schema
prompt = build_prompt(task, render_schema(self.catalog, tables), examples, oracle_hints)
```

- **相似题示例库**（[agent/examples.py](../agent/examples.py)）：dw 全部已解题，**排除评测集和开发集**，共 5,508 道。Gold SQL 经过 Phase 0 的适配规则，要求能解析、不超过 2,500 字符。按问题文本做 BM25 检索。
- **prompt 结构**：规则 + schema + 示例 + 问题。规则里有这个数仓的通用约定，比如题干里的 STDDEV / VARIANCE 对应 STDDEV_POP / VAR_POP，见 [generator.py:37](../agent/generator.py)。
- **LLM 缓存**：调用经过缓存层 [agent/llm.py:262](../agent/llm.py)，按 (模型, 参数, system, prompt) 的 SHA-256 指纹缓存。同一个 prompt 直接重放。

**真实数据**：dw_4188（glm，固定示例）输入 13,353 token，输出 321 token，耗时 22.3 秒。

| glm-4-flash，开发集 30 题 | 固定示例 | 相似题示例 |
|---|---|---|
| 首次答对 | 0 | **3** |
| SQL 能执行 | 4 | **16** |

**为什么**：
1. 贪心解码加缓存，意味着各实验组的第 1 次尝试**逐字相同**，组间差别只来自后续策略。
2. 相似题示例是为了解决"在相似表之间选错"的问题：同类问题在这个数仓里用哪些表、怎么关联，直接由已解题示范给模型。

---

## Step 4 执行：在 Databricks 上运行

**源码** [controller.py:81-89](../loop_engineer/controller.py)

```python
def _execute(self, sql, db, parse_status):
    if not sql:                                       # 模型没给出 SQL：不执行，状态沿用 parse_status
        return {"execution_status": parse_status, ...}, []
    ex = self.executor.execute(sql, db, max_rows=self.cfg.max_result_rows)
    rows = ex.rows if ex.ok else []
    return {"execution_status": ex.status, "execution_error": (ex.error or "")[:2000] or None,
            "result_row_count": len(rows) if ex.ok else None,
            "result_preview": _serialize_preview(rows) if ex.ok else None,   # 只保留前 5 行
            "exec_latency_ms": ex.elapsed_ms}, rows
```

执行器 [execution/databricks_sql.py](../execution/databricks_sql.py) 的 `execute`：只读检查 → 切换 schema → 执行 → 超过行数上限返回 `TOO_MANY_ROWS` → 出错时提取错误类别。

**真实数据**（dw_4188）：

```
execution_status: ERROR
execution_error : [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column ... with name `ata`.`DEPARTMENT_CODE` cannot be
                  resolved. Did you mean one of the following? [`sd`.`DEPARTMENT_CODE`, `sd`.`DEPARTMENT_NAME`, ...]
```

**为什么**：完整结果 `rows` 不写进 `attempt`，只放在单独的列表里，供自检和 Step 13 判分使用；`attempt` 里只有行数和前 5 行预览。这样 trace 体积可控。

---

## Step 5 自检：不看 Gold，判断要不要修

**源码** [controller.py:139-147](../loop_engineer/controller.py) → [loop_engineer/verifier.py:44-71](../loop_engineer/verifier.py)

```python
# SelfVerifier.verify（v2）
if status in ("NO_SQL", "EMPTY_RESPONSE"):                      hits.append("no_sql")
elif status == "TOO_MANY_ROWS":                                  hits.append("too_many_rows")
elif status not in ("SUCCESS", "NO_SQL", "EMPTY_RESPONSE", "TOO_MANY_ROWS"):
                                                                 hits.append("execution_error")
elif status == "SUCCESS":
    if not rows:                                                 hits.append("empty_result")
    elif any(all(r[i] is None for r in rows) for i in range(len(rows[0]))):
                                                                 hits.append("all_null_column")
    elif rows:                                                   # v2：能执行、有结果时再做语义检查
        for f in check_all(question, generated_sql, rows):
            if f.signal in TRIGGER_SIGNALS: findings.append(f); hits.append(f.signal)   # 触发修复
            else:                           advisories.append(f)                        # 只作提示
return VerifierDecision(not hits, self.mode, tuple(hits), tuple(findings), tuple(advisories))
```

**三层信号**（[verifier.py:23-25](../loop_engineer/verifier.py)、[checks.py:46-48](../loop_engineer/checks.py)）：

| 层 | 信号 | 如何判断 |
|---|---|---|
| 显式失败（v1） | 没有 SQL、执行报错、结果过大、空结果、整列 NULL | 执行状态 |
| 结构规则 | 关联条件恒为真、JOIN 缺条件、缺少分组、四舍五入与题意相反 | 解析 SQL，对照题干（[checks.py:142](../loop_engineer/checks.py) `check_static`） |
| 数值一致性 | 统计量为负、计数非整数、min > max、平均值不在 [min, max]、方差 ≠ 标准差²、标准差 > 极差 | 把输出列对应到产生它的统计函数，由**代码**核对（[checks.py:319](../loop_engineer/checks.py) `check_numeric`），不用 LLM 推理 |

**"自检通过"只表示没发现问题，不代表答案正确**：Loop 运行时看不到标准答案。

**为什么只有这些信号能触发修复**：每个信号都先在开发集上离线评估过（`scripts/verifier_eval.py`），**只有在正确答案上误报接近 0 的才能触发修复**；误报较高的只作为提示。这 10 个触发信号的误报：4 条结构规则在 5,687 道 Gold SQL 上最高 0.3%（缺少分组 17 道，其余接近 0），数值规则在 378 个正确结果上为 0。

**真实数据**：
- glm 固定示例运行，第 1 次自检：触发且确实错 27，通过但其实错 3（漏报），误报 0。
- deepseek 在线运行：修复后能执行的从 24 升到 29，但答对始终是 3。**26 道"能执行但答错"几乎全部通过了自检**，见附录 B。

**为什么**：这些信号在生产环境都拿得到。`OracleVerifier`（[verifier.py:74](../loop_engineer/verifier.py)）用 Gold 判断，相当于告诉 Loop"这题错了"，只作为上界单独标注。想退回 v1 的行为：`SelfVerifier(signals=BASIC_SIGNALS)`。

---

## Step 6 终止判断

**源码** [controller.py:148-151](../loop_engineer/controller.py)

```python
attempts.append(attempt)
all_rows.append(rows)
if decision.passed or n == self.cfg.max_attempts:
    break
```

两个出口：自检通过，或已达最大尝试次数（最大修复次数 + 1）。最后一次尝试如果失败，不再诊断和修复。

---

## Step 7 观察：用白名单把失败"翻译"成结构化信号

从这里开始是 targeted 组；generic 组见 Step 10b。

**源码** [controller.py:161-166](../loop_engineer/controller.py) → [loop_engineer/observer.py:16, 51](../loop_engineer/observer.py)

```python
OBSERVABLE_FIELDS = ("case_id", "attempt_id", "question", "retrieved_tables", "generated_sql", "parse_status",
                     "execution_status", "execution_error", "result_row_count", "result_preview",
                     "verifier_signals", "verifier_findings")     # 自检的发现也可观察（不含 Gold）

def observe(record, db):
    r = {k: record.get(k) for k in OBSERVABLE_FIELDS}  # whitelist: nothing else passes
    m_cls = _ERROR_CLASS.search(err)     # [UNRESOLVED_COLUMN.WITH_SUGGESTION] → UNRESOLVED_COLUMN
    m_col = _UNRESOLVED.search(err)      # name `ata`.`DEPARTMENT_CODE` cannot be resolved
    m_sug = _SUGGEST.search(err)         # Did you mean one of the following? [...]
    ...
```

**真实数据**（dw_4188）：错误类别 `UNRESOLVED_COLUMN`；找不到的列 `ata.DEPARTMENT_CODE`；引擎候选 `sd.DEPARTMENT_CODE, sd.DEPARTMENT_NAME, ...`。

**为什么是白名单**：如果将来 attempt 里多出一个 Gold 衍生字段（比如有人加了 `correct`），黑名单会漏掉，白名单天然挡住。Observer 是 Loop 的"防火墙"。

---

## Step 8 诊断：错在哪（规则优先，LLM 兜底）

**源码** [controller.py:167](../loop_engineer/controller.py) → [loop_engineer/diagnose.py:186](../loop_engineer/diagnose.py) `diagnose`：先走规则 `diagnose_by_rules`（[:83](../loop_engineer/diagnose.py)），规则无结论才调用 LLM。

**核心规则：在 schema 里为报错的列定位**（[diagnose.py:88-109](../loop_engineer/diagnose.py)）

```python
if cls == "UNRESOLVED_COLUMN" and obs.unresolved_column:
    owner = column_owners(catalog, col)             # schema 里有这个列的所有表
    in_used = [t for t in owner if t in used]       # SQL 已经用了的表
    in_retrieved = [t for t in owner if t in retrieved and t not in used]
    if in_used:      -> COLUMN_MAPPING  0.9   case="wrong_alias"      # 列在已用的表里，只是挂错了别名
    if in_retrieved: -> TABLE_RETRIEVAL 0.8   case="table_not_used"   # 列在检索到但没用的表里
    if owner:        -> TABLE_RETRIEVAL 0.85  case="not_retrieved"    # 列只在没检索到的表里
    else:            -> COLUMN_MAPPING  0.7   case="hallucinated"     # 没有任何表有这个列
```

其他规则：表不存在 → 选表；结果过大 → 关联键；语法、聚合等 SQL 层面的错误 → 执行错误（[:111-124](../loop_engineer/diagnose.py)）。

**自检发现的语义问题也由规则映射**（v2，[diagnose.py:51-60, 126-131](../loop_engineer/diagnose.py)）：

```python
_VERIFIER_TYPES = {
    "join_tautology": (JOIN_KEY, 0.85), "join_without_condition": (JOIN_KEY, 0.85),        # -> FindJoinPath
    "missing_grouping": (QUERY_DECOMPOSITION, 0.8),                                          # -> ReplanQuery
    "rounding": (EXECUTION, 0.8),                                                            # -> RepairSQL
    "avg_outside_min_max": (QUERY_DECOMPOSITION, 0.75), ...                                  # 数值矛盾 -> ReplanQuery
}
```

**真实数据**（dw_4188）：`DEPARTMENT_CODE` 所在的表有 9 张，其中 `sis_department` 已经被 SQL 使用（别名 `sd`），命中 `in_used`：

```
failure_type : COLUMN_MAPPING_FAILURE   confidence : 0.9   source : rule
reason       : DEPARTMENT_CODE exists in sis_department, which the SQL already uses, but was referenced through another alias
repair_hints : {"column": "DEPARTMENT_CODE", "qualifier": "ata", "owner_tables": [...], "suggestions": [...], "case": "wrong_alias"}
```

glm 那次运行共 27 次诊断，26 次由规则完成。

**为什么能这样归因**：Databricks 已经告诉我们"哪个列、在哪个别名下找不到"，剩下的只是"这个列实际在哪张表"。这是一次 schema 查找，结果是确定的。LLM 在这类问题上反而更差：评测集上 LLM 诊断的严格准确率是 0/19，规则诊断的宽松准确率是 76.5%。`repair_hints` 把查找过程的证据原样传给下一步，技能不需要重新推理。

---

## Step 9 路由：失败类型 → 技能（映射是数据）

**源码** [controller.py:171](../loop_engineer/controller.py) → [loop_engineer/policy.py:27, 52](../loop_engineer/policy.py)

```python
TARGETED = {
    TABLE_RETRIEVAL: "RetrieveAgain",    COLUMN_MAPPING: "SchemaSearch",   JOIN_KEY: "FindJoinPath",
    DOMAIN_KNOWLEDGE: "ReplanQuery",     QUERY_DECOMPOSITION: "ReplanQuery",
    EXECUTION: "RepairSQL",              UNKNOWN: "RepairSQL",
}
def route(self, diagnosis):
    if self.mode == "generic":  return Route(FALLBACK, False, "policy disabled: generic repair")
    skill = self.mapping.get(diagnosis.failure_type, FALLBACK)
    if skill in self.disabled:  return Route(FALLBACK, True, f"{skill} disabled (ablation)")
    return Route(skill, diagnosis.failure_type in FALLBACK_ROUTES, f"{diagnosis.failure_type} -> {skill}")
```

**真实数据**：glm 那次运行的路由分布：RetrieveAgain 14、SchemaSearch 9、RepairSQL 3、ReplanQuery 1。

**为什么**：路由表是一个 dict，消融只换表或禁用技能，不改代码。`fallback=True` 会写进 trace，看板上能看出"这次走了兜底"。

---

## Step 10 修复：技能执行

所有技能实现同一个接口（[skills/base.py:47](../skills/base.py)）：

```python
class RepairSkill(Protocol):
    name: str
    def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult: ...
```

- `RepairContext`（[base.py:23](../skills/base.py)）只包含 Agent 可见的资源：schema、LLM 客户端、示例，给模型看的表最多 24 张。
- `RepairResult`（[base.py:31](../skills/base.py)）除了新 SQL，还有 **`details`**：技能内部做了什么，包括确定性修改、规则无法决定的部分、候选列、关联键、给 LLM 的指令和完整 prompt。网页的修复步骤和分析报告展示的就是这些。

需要 LLM 时，所有技能共用一个模板（[base.py:53](../skills/base.py)），**技能之间的差别只在 `{instruction}` 和给哪些表的 schema**：

~~~text
{rules}
Schema:
{schema}
Question: {question}
A previous attempt produced this SQL:
```sql
{sql}
```
{observed}                 ← 报错原文；或"能执行，但检查发现：<自检的英文提示>"
Diagnosis: {diagnosis}     ← 例如 "COLUMN_MAPPING_FAILURE: DEPARTMENT_CODE exists in ..."
{instruction}              ← 每个技能自己的定向指令
Return the corrected query as ONE read-only SQL query inside a ```sql code fence. No explanation.
~~~

`{observed}` 由 `observed_text`（[base.py:73](../skills/base.py)）生成。SQL 能执行但自检发现问题时，会把每条发现的英文提示写进去，例如 *"The condition sd.X = sd.X compares a column with itself..."*，修复模型因此知道具体错在哪。调用封装在 `llm_repair`（[base.py:85](../skills/base.py)）。

### 10a. SchemaSearch（列映射）

**源码** [skills/schema_search.py:104-133](../skills/schema_search.py)

```python
fix = fix_column_refs(obs.generated_sql, ctx.catalog)        # ① 确定性修复
if fix.changes and not fix.unresolved:
    return RepairResult(fix.sql, ..., used_llm=False, details={"deterministic_changes": fix.changes, ...})
# ② 规则决定不了的部分交给 LLM，从部分修好的 SQL 开始；需要关联键时附上从 schema 推断的候选关联键
return llm_repair(self.name, start, diagnosis, ctx, [...], instruction, action,
                  {"deterministic_changes": ..., "unresolved": problems, "candidate_columns": ..., "join_candidates": ...})
```

确定性部分 `fix_column_refs`（[schema_search.py:39](../skills/schema_search.py)）做两件事：
1. **逐个作用域检查每一个带别名的列**（[:48](../skills/schema_search.py)）：`a.COL` 的表里没有这个列，而同一作用域恰好只有一张表有，就改过去。检查全部而不只是报错的那一个，是因为引擎一次只报第一个错。
2. **关联条件守卫**（[:70](../skills/schema_search.py)）：改完后如果关联条件两边变成同一张表（如 `sd.X = sd.X`），SQL 能跑，但两张表失去了关联。这种修改会被撤回，标为"需要真正的关联键"。

**真实数据**（dw_4188）：所有候选改动都被守卫撤回，3 个关联问题连同推断的关联键交给 LLM。没有守卫的话，第一处会变成 `sd.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`，SQL 能执行但答案错。这就是问题 #8：Phase 5 的"可执行 4→10"因此虚高，修正后为 4→8。现在自检 v2 的 `join_tautology` 规则会从自检这一侧再兜一次底。

### 10b. 对照：Generic Retry

**源码** [controller.py:91-105](../loop_engineer/controller.py)

```python
GENERIC_INSTRUCTION = "The query above is wrong. Write a corrected query."
prompt = REPAIR_TEMPLATE.format(..., diagnosis="(not diagnosed)", instruction=GENERIC_INSTRUCTION)
```

同一个模板、同样的报错信息、同样的检索表，只是**没有诊断、没有定向指令、没有额外的表**，这保证两组的差别只来自"诊断 + 定向修复"。

### 10c. RetrieveAgain（选表）

**源码** [skills/retrieve_again.py:25-43](../skills/retrieve_again.py)

```python
add = list(h.get("tables_to_add") or h.get("owner_tables") or [])[:4]     # 直接用诊断找到的表
joins = [j.sql() for t in add for j in connect(ctx.catalog, t, used)][:8]   # 新表怎么接到已用的表上
instruction = f"Column {h.get('column')} is not in the table you used; it lives in {', '.join(add)}. ..."
```

诊断找到的证据（`repair_hints["tables_to_add"]`），修复直接使用。

其他技能：FindJoinPath 给出候选关联键；ReplanQuery 让模型先拆子问题再写 CTE；RepairSQL 按错误类别附上 Databricks 语法注意事项。设计说明见 [LOOP_DESIGN.md](LOOP_DESIGN.md)。

---

## Step 11 状态交接：写回诊断，组装下一次尝试

**源码** [controller.py:153-192](../loop_engineer/controller.py)

```python
nxt = {"case_id": task.case_id, "attempt_id": n + 1, "question": task.question,
       "retrieved_tables": attempt["retrieved_tables"], "strategy": self.cfg.strategy, "diag_tokens": 0}
attempt.update({"failure_type": ..., "diagnosis_confidence": ..., "diagnosis_reason": ..., "repair_hints": ...})  # 诊断写在失败的那次上
nxt.update({"generated_sql": res.repaired_sql, "repair_skill": route.skill, "repair_action": res.repair_action, ...})  # 修复写在新的那次上
attempt["repaired_sql"] = nxt["generated_sql"]     # 前后两次互相指向，trace 能串起来
attempt["repair_skill"] = nxt["repair_skill"]
emit("repair", ..., before_sql=..., sql=..., details=details)
attempt = nxt                                      # 回到 Step 4；最多循环 max_attempts 次
```

**约定**："为什么失败"记在失败的那次尝试上，"做了什么修复"记在新的尝试上。按 `attempt_id` 排好，就是一条"失败 → 诊断 → 修复 → 结果"的时间线。允许多轮修复时，每一轮都重复 Step 4–11。

**真实数据**（dw_4188，最多修复 2 次）：3 次尝试都报 `UNRESOLVED_COLUMN`，两轮都诊断为列映射并交给 SchemaSearch，但 glm 两次都只给表名加了 `dw.` 前缀。分析报告自动得出："第 2 次之后的修复轮次没有带来新的可执行题，增加轮次主要增加成本。"

---

## Step 12 选出最终答案

**源码** [controller.py:193-202](../loop_engineer/controller.py)

```python
passed   = [i for i, a in enumerate(attempts) if a["verifier_decision"] == "PASS"]
executed = [i for i, a in enumerate(attempts) if a["execution_status"] == "SUCCESS"]
final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
```

优先级：最后一个自检通过的 → 最后一个能执行的 → 最后一个。

**为什么不直接取最后一个**：如果修复把能跑的 SQL 改坏了，这条规则会保留原来能跑的那个。选择只看自检和执行状态，不看 Gold。

---

## Step 13 事后判分：Loop 结束后才用 Gold

**源码** [loop_run.py:52-64](../evaluation/loop_run.py)。对错存在 `rec["attempt_correct"]`，**不写回 `attempts`**。发布时 `attempts` 进 `traces.*`，`correct` 进 `evaluation.*`。网页上的"Gold 判分（Loop 不可见）"来自这里的 `judged` 事件。

---

## Step 14 指标汇总

**源码** [loop_run.py:76](../evaluation/loop_run.py) `summarize`

| 指标 | 公式 | 回答的问题 |
|---|---|---|
| Recovery Rate | 首次错且最终对 / 首次错 | 修好了多少 |
| Harm Rate | 首次对且最终错 / 首次对 | 改坏了多少 |
| Net Gain | 恢复 − 误伤 | 净收益 |
| `executable_first / final` | 首次 / 最终能执行的题数 | 过程指标 |
| `verifier_confusion` | 触发且错 / 误报 / 漏报 / 通过且对 | 自检的质量 |
| `per_skill` | 每个技能的修复次数、修复后能执行、恢复 | 技能的质量 |

**真实数据**：

| 运行 | 可执行 首次 → 最终 | 答对 首次 → 最终 |
|---|---|---|
| glm，固定示例，Targeted + Self，修复 1 次 | 4 → 9 | 0 → 0 |
| glm，固定示例，Generic + Self，修复 1 次 | 4 → 6 | 0 → 0 |
| deepseek，固定示例，Targeted + Self v2，最多修复 4 次（在线） | 24 → 29 | **3 → 3** |

同样的预算下，定向修复比通用重试多修好 3 道可执行题。但两个模型上答对的题数都没有因为修复而增加，原因见附录 B。

---

## Step 15 发布、回放与分析

- **发布**：命令行运行用 `python scripts/publish_run.py runs/phase6/<run_id>`；网页运行默认自动发布。`dbx/publish.py:35` `flatten_loop_records` 把每题的每次尝试展平成一行，写入 `traces.execution_traces`；对错写入 `evaluation.evaluation_results`；汇总写入 `evaluation.runs` 和 MLflow。
- **回放**：Loop Debug Console（[app/dashboard.py](../app/dashboard.py)）的"逐题追踪"可以选 run、选题，看 Step 3–12 的每个字段。网页发起的运行标为"[运行页]"。
- **实时 trace 和分析报告**：网页的"运行 Loop"页（[run_loop.py](../app/app_pages/run_loop.py)）按事件逐步展示检索（BM25 分数）、生成（prompt、示例、补充的表）、执行、自检（触发项和提示项）、观察、诊断、路由、修复（技能内部过程、指令、SQL 对比）。运行结束后，[app/report.py](../app/report.py) 不调用 LLM，直接生成分析报告：修复前后对比柱状图、逐次尝试折线图、逐题 × 逐次状态格子图，以及总体结果、各环节表现、成本、发现与建议。
- **错误分析**：`python scripts/analyze_run.py <run_id>` 把一次开发集运行逐题和 Gold 对照：用表、列、关联、过滤值、统计运算、行数和列数。附录 B 的结论就来自它。

---

## 附录 A：两道题的完整轨迹（glm，固定示例）

### A1. dw_4188：诊断对了，模型没修好

| 步骤 | 内容 |
|---|---|
| 生成 | 5 张表的 JOIN；`ata.DEPARTMENT_CODE`（`academic_terms_all` 里没有这个列） |
| 执行 | `ERROR` `UNRESOLVED_COLUMN`，候选 `sd.DEPARTMENT_CODE` |
| 自检 | 未通过：`execution_error` |
| 观察 | 别名 `ata`，列 `DEPARTMENT_CODE` |
| 诊断 | 规则：列在已使用的 `sis_department` 里 → 列映射，0.9，`wrong_alias` |
| 路由 | SchemaSearch |
| 修复 | 确定性改动全部被关联条件守卫撤回 → 3 个关联问题和推断的关联键交给 LLM |
| 下一次 | LLM 只加了 `dw.` 前缀 → 同样报错 |
| 判分 | 全部错误 |

**讲点**：Loop 的每个环节都给出了正确信息，失败在模型执行指令的能力上。守卫阻止了一次"能跑但错"的假修复。

### A2. dw_5478：修复后能跑了，但答案仍然错（自检漏报）

| 步骤 | 内容 |
|---|---|
| 生成 | 用 `sdp.GRADUATE_LEVEL` 过滤研究生 |
| 执行 | `ERROR` `UNRESOLVED_COLUMN`：`sdp.GRADUATE_LEVEL` |
| 诊断 | 规则：`GRADUATE_LEVEL` 只在 `sis_course_description` 里，这张表检索到了但没用 → 选表，0.8，`table_not_used` |
| 路由 | RetrieveAgain |
| 修复 | 加入 `sis_course_description` 和推断的关联键 → LLM 改写 |
| 下一次 | `SUCCESS`，1 行 → **自检通过** |
| 判分 | 错误 |

**讲点**：
1. 诊断 → 技能 → 证据传递完整：`tables_to_add` 从诊断直接进入技能的指令。
2. 模型改写时**丢掉了"研究生"这个条件**。SQL 能跑、结果非空、数值自洽，自检没有依据发现这个语义错误。网页上会标黄："自检通过，但答案是错的（Verifier 漏报）"。

---

## 附录 B："能执行但答错"为什么修不好：实验结论

deepseek 在线运行（最多修复 4 次）修复后能执行的从 24 升到 29，但答对始终是 3：**Loop 只能修复它能发现的错误**。`scripts/analyze_run.py` 的逐题对照显示：

- 26 道"能执行但答错"中，**22 道的主要原因是用的表和 Gold 不同**；
- Gold 用到而没被使用的表共 41 次，**34 次其实已经检索到了**，是模型在相似表之间选错了。

为了让自检发现这类错误，做了四轮离线实验，每个方法都测了"抓到错题"和"误伤正确答案"：

| 方法 | 抓到错题 | 误伤正确答案 | 结论 |
|---|---|---|---|
| 对照题干的规则 | 能抓的规则误报高（4–11%），安全的规则抓不到 | — | 只接入安全规则 |
| LLM 裁判（glm / deepseek） | 10/30 / 27/30 | 4/33 / **31/33** | 不接入：BEAVER 的题干和 Gold 本身常常不一致，裁判越严格误伤越多 |
| 数值一致性（代码计算） | 0/22 | 0/378 | 接入：零误报，但这批错题的数值都自洽 |
| 查数据库的验证器（过滤值、关联放大） | 0/30、5/30 | 0/33、**7/33**（Gold 抽样 22%） | 只作提示：BEAVER 的 Gold 本身常有"关联后放大"的写法 |

**结论**：所有不看标准答案的检查，衡量的都是"通常意义上的正确"；而这类错误的根源是**不知道这个数仓的约定**，也就是该用哪张表、怎么关联。这只能从已解题中学。所以改进放在生成端：Step 3 的相似题示例，在 glm 上把答对从 0 提升到 3、能执行从 4 提升到 16。

这引出**双循环**的设计方向：
- **内循环**（运行时，不接触 Gold）：兜住显式失败和结构性错误，也就是本文档描述的流程；
- **外循环**（离线，使用标注）：从错误分析和执行反馈中归纳数仓知识（示例库、易混表说明、列所属表），生成阶段直接使用。

详细数据见 [EXECUTION_LOG.md](EXECUTION_LOG.md) 的"补强 Verifier"和"提升正确率"两节。

---

## 附录 C：已知局限与对应代码位置

| 局限 | 位置 | 可能的改进 |
|---|---|---|
| 能执行但语义错的答案，自检发现不了 | [verifier.py](../loop_engineer/verifier.py)；附录 B | 生成端补充知识（外循环）；多候选一致性作为运行时的可疑信号 |
| 诊断置信度没有校准 | [diagnose.py:88-109](../loop_engineer/diagnose.py) 中 0.9 / 0.8 / 0.85 / 0.7 是人工设定的 | 用诊断准确率数据校准 |
| 确定性预处理只有 SchemaSearch 有 | [schema_search.py:39](../skills/schema_search.py) | RetrieveAgain 可以用规则直接补 JOIN |
| 修复效果受模型能力限制 | dw_4188 多轮修复同样报错 | 反复出现的同类错误改成确定性修复；换更强的模型 |
| RetrieveKnowledge 还是占位 | [skills/retrieve_knowledge.py](../skills/retrieve_knowledge.py)；领域知识暂时路由到 ReplanQuery | 由外循环生成的知识库提供，不含 Gold |
| 相似题示例库取自 BEAVER 已解题 | [agent/examples.py](../agent/examples.py) | 生产中应只收录人工审核过的查询；修复后能运行的 SQL 不能直接入库 |
