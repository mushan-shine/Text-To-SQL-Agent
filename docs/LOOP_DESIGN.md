# Loop Engineering 设计说明

> 本文说明 Inner Loop（单题自我修复）**怎么运行、怎么实现、为什么这样设计**。
> - A 部分：流程视角，不看代码也能读懂。
> - B 部分：技术视角，包括代码、context 机制、Skill 协作方式和所有参数。
> - C 部分：从错误分析、归因到 Skill 调用，每一步设计背后的数据依据。
>
> 模块、类关系、时序和数据流的架构图见 [LOOP_ARCHITECTURE.md](LOOP_ARCHITECTURE.md)。
>
> 代码以 `phase1-baseline` 分支 `9c65ac3` 为准。项目定位见 [PROJECT_POSITIONING.md](PROJECT_POSITIONING.md)，执行过程和数据来源见 [EXECUTION_LOG.md](EXECUTION_LOG.md)。

---

## A. 流程视角

### A1. 一道题在 Loop 里怎么走

```
                         ┌──────────────────────────────┐
  问题 ──► ① 检索 ──► ② 生成 ──► ③ 执行 ──► ④ 验证 ───┤ PASS ──────────────► ⑨ 选最终答案
                                        ▲              │
                                        │              │ FAIL，且还有尝试次数
                                        │              ▼
                                        │        ⑤ 观察（Observer）
                                        │              │  整理这次失败的现场
                                        │              ▼
                                        │        ⑥ 归因（Diagnoser）
                                        │              │  判断失败类型 + 给出修复线索
                                        │              ▼
                                        │        ⑦ 选技能（Policy）
                                        │              │  失败类型 → Skill
                                        │              ▼
                                        └──────── ⑧ 修复（Skill）
                                                 产出下一次尝试的 SQL

   FAIL 且尝试次数用完 ─────────────────────────────────────────────► ⑨ 选最终答案
```

- **预算**：最多 2 次尝试，也就是最多修复 1 次（`max_attempts = 2`）。
- **第 1 次尝试**：贪心解码加 prompt 缓存，同一配置重跑时逐字相同，实验可复现。
- **③ 和 ④ 每次尝试都会执行**：修复后的 SQL 同样要经过执行和验证。
- **⑨ 选最终答案**：优先取最后一次**通过验证**的尝试；都没通过时，取最后一次**能执行**的；都不能执行时，取最后一次尝试。

### A2. 分工：每个角色负责什么

| 步骤 | 角色 | 回答的问题 | 输入 | 输出 | 用 LLM？ |
|---|---|---|---|---|---|
| ① | Retriever | 这道题可能用到哪些表？ | 问题 | 20 张候选表 | ❌（BM25） |
| ② | Generator | SQL 怎么写？ | 问题 + 表结构 + 示例 | 第 1 次 SQL | ✅ |
| ③ | Executor | 在 Databricks 上跑出什么结果？ | SQL | 执行状态、报错、结果行 | ❌ |
| ④ | **Verifier** | **这次错了吗？** | 执行状态和结果 | PASS / FAIL + 触发信号 | ❌（SelfVerifier） |
| ⑤ | **Observer** | 失败现场有哪些信息？ | 这次尝试的记录 | Observation（只读，不含 Gold） | ❌ |
| ⑥ | **Diagnoser** | **为什么错？** | Observation + schema | Diagnosis：类型、置信度、原因、修复线索 | 规则优先；没有报错信号时才用 LLM |
| ⑦ | **Policy** | **用哪个 Skill 修？** | Diagnosis | Route：Skill 名称 | ❌（查映射表） |
| ⑧ | **Skill** | **怎么修？** | Observation + Diagnosis + 共享资源 | RepairResult：新 SQL | 部分用（见 B7） |
| ⑨ | Controller | 最终用哪次的答案？ | 所有尝试 | 最终答案 + 完整 trace | ❌ |

**关键点：④ 到 ⑧ 之间传递的信息都经过设计，而且全程不接触 Gold。** Loop 只能看到它自己的执行结果。Gold 只在 Loop 结束后用于判分，OracleVerifier 是唯一例外，它只用于上界分析，结果也会单独标注。

### A3. 两层闭环：这一题修好，下一题不再错

| | Inner Loop（本文的主要内容） | Outer Loop（Phase 10–12，尚未自动化） |
|---|---|---|
| 修改什么 | **这一题的 SQL** | **系统本身**：prompt、示例、检索、诊断规则、Policy、Skill |
| 何时发生 | 运行时，几秒到几十秒 | 离线，跑完一批题之后 |
| 修改的效果 | 只对这一题有效 | 以后所有题都受益 |
| 能看到 Gold 吗 | 不能 | 只能在开发集上用来验证改进 |
| 目前的实现 | ✅ 自动 | 人工完成：prompt v2、SchemaSearch v2、修复关联条件恒真的 bug |

### A4. Trace：每次尝试留下哪些记录、存到哪里

```
Controller.run() 为每次尝试维护一个 attempt 字典
      │  （在 ③④⑥⑦⑧ 各步中逐步填入字段）
      ▼
runs/phase6/<run_id>/results.jsonl       ← 本地：每题一行，包含 attempts 列表、每次尝试的对错
      │
      ▼  scripts/publish_run.py（flatten_loop_records：每次尝试展开为一行）
      ├──► traces.execution_traces        ← Loop 自己看到和做的事（不含任何 Gold 信息）
      ├──► evaluation.evaluation_results  ← 每次尝试的 Gold 判分（Loop 看不到）
      ├──► evaluation.runs                ← 每次运行的汇总指标
      └──► MLflow 实验                    ← 参数、指标、产出文件
                 │
                 ▼
         Loop Debug Console（app/dashboard.py）的"逐题追踪"页面
```

**trace 表和 evaluation 表必须分开**：Observer 和 Diagnoser 会读 trace。如果 trace 里有"答对 / 答错"这类用 Gold 算出来的字段，自检模式下的 Loop 就等于间接看到了答案。`tests/test_phase2.py` 专门检查了这一点。

### A5. 用一道真实题目看完整过程：dw_4188

| 步骤 | 实际发生了什么 |
|---|---|
| ② 生成 | 模型写了 `JOIN SIS_DEPARTMENT sd ON ata.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`，其中 `ata` 是学期表 |
| ③ 执行 | `[UNRESOLVED_COLUMN] ata.DEPARTMENT_CODE cannot be resolved. Did you mean sd.DEPARTMENT_CODE …` |
| ④ 验证 | 信号 `execution_error` → FAIL |
| ⑤ 观察 | 解析出：列 `DEPARTMENT_CODE`、别名 `ata`、引擎建议 `sd.DEPARTMENT_CODE` |
| ⑥ 归因 | 查 schema：这一列存在于 SQL 已用的 `sis_department` → `COLUMN_MAPPING_FAILURE`（别名挂错），置信度 0.9，规则判断 |
| ⑦ 选技能 | 列映射 → SchemaSearch |
| ⑧ 修复 | 先做确定性修复：检查整条 SQL 的列引用。三处错误的列引用都在关联条件里，如果改到另一张表上，关联条件会变成"自己等于自己"，所以**撤销这些修改**，标记为"需要真正的关联键"，连同 schema 推断出的候选关联键交给 LLM |
| 第 2 次 ③④ | LLM 重写后仍然报错 → FAIL，预算用完 |
| ⑨ | 两次都没通过验证，也都不能执行 → 取最后一次作为最终答案 |

修复"关联条件恒真"这个 bug 之前，这道题的确定性修复会让 SQL 能执行但返回 0 行（`sd.X = sd.X`），被错误地算作"修复后可执行"。第 2 次验证发现了空结果，这个 bug 才暴露出来（问题记录 #8）。

---

## B. 技术视角

### B1. 模块结构

```
agent/
  retriever.py        SchemaCatalog + BM25TableRetriever            ① 检索
  generator.py        FewShotGenerator, RULES, PROMPT_VERSION        ② 生成
  llm.py              ZhipuChatClient, CachingChatClient, UsageMeter    所有 LLM 调用都经过这里
  sql_analysis.py     sql_facts(), alias_map()                          中性的 SQL 结构分析
  join_graph.py       join_candidates(), connect()                      从 schema 推断关联键
execution/
  databricks_sql.py   DatabricksSqlExecutor.execute()                ③ 执行（只读校验、行数上限）
loop_engineer/
  verifier.py         SelfVerifier, OracleVerifier                   ④ 验证
  observer.py         observe() → Observation                         ⑤ 观察
  diagnose.py         diagnose_by_rules(), Diagnoser → Diagnosis      ⑥ 归因
  policy.py           Policy.route() → Route, SKILLS                  ⑦ 选技能
  controller.py       LoopController.run() → LoopResult               ⑨ 编排整个闭环
skills/
  base.py             RepairContext, RepairResult, REPAIR_TEMPLATE, llm_repair()
  schema_search.py    SchemaSearch, fix_column_refs()                ⑧ 修复
  retrieve_again.py   RetrieveAgain
  find_join_path.py   FindJoinPath
  replan_query.py     ReplanQuery
  repair_sql.py       RepairSQL
  retrieve_knowledge.py  （占位，尚未实现）
evaluation/
  loop_run.py         run_arm(), summarize()                          Loop 结束后的判分和指标
dbx/publish.py        flatten_loop_records(), build_rows()            trace 发布
```

**主要函数的功能**

| 函数 | 位置 | 功能 |
|---|---|---|
| `BM25TableRetriever.retrieve` | `agent/retriever.py` | 用 BM25 按问题文本给 97 张表打分，返回最相关的前 20 张候选表及分数 |
| `FewShotGenerator.generate` | `agent/generator.py` | 挑选示例、补充示例和审核知识里用到的表，拼出 prompt 调用 LLM，从回复中抽取第一版 SQL |
| `CachingChatClient.complete` | `agent/llm.py` | 所有 LLM 调用的入口：按（模型, 参数, system, prompt）指纹查缓存，未命中才真正调用，并计入调用预算 |
| `sql_facts` / `alias_map` | `agent/sql_analysis.py` | 解析 SQL 语法树：`sql_facts` 提取用了哪些表、列、关联、运算；`alias_map` 只给出"别名 → 表名"对应关系 |
| `join_candidates` / `connect` | `agent/join_graph.py` | 只根据 schema 推断表之间可能的关联键：`join_candidates` 列出一组表之间的候选关联，`connect` 找出一张新表接到已用表上的方式 |
| `DatabricksSqlExecutor.execute` | `execution/databricks_sql.py` | 只读检查后在 Databricks 上执行 SQL，超过行数上限返回 `TOO_MANY_ROWS`，出错时提取错误类别 |
| `SelfVerifier.verify` | `loop_engineer/verifier.py` | 不看 Gold，根据执行状态、SQL 结构和结果数值判断这次尝试是否可疑，返回是否通过和触发的信号 |
| `OracleVerifier.verify` | `loop_engineer/verifier.py` | 用 Gold 判对错，只用于估算"自检完美时 Loop 能到多好"的上界 |
| `observe` | `loop_engineer/observer.py` | 按白名单从尝试记录中取运行时可见的字段，并从报错文本解析出错误类别、找不到的列、候选列、不存在的表 |
| `diagnose_by_rules` | `loop_engineer/diagnose.py` | 规则诊断：根据报错信号和 schema 判断失败类型并给出修复线索；判断不了返回 `None` |
| `Diagnoser.diagnose` | `loop_engineer/diagnose.py` | 诊断入口：先走规则，规则没有结论再调用 LLM，返回 `Diagnosis` 和 LLM 用量 |
| `Policy.route` / `Policy.skill` | `loop_engineer/policy.py` | `route` 按"失败类型 → 技能"映射表选出技能名（被禁用的退回 RepairSQL）；`skill` 按名字取出技能对象 |
| `LoopController.run` | `loop_engineer/controller.py` | 编排单题闭环：检索 → 生成 → 循环（执行 → 自检 → 观察 → 诊断 → 路由 → 修复）→ 选出最终答案 |
| `RepairSkill.repair` | `skills/*.py` | 每个技能修复一类失败：先做确定性修改，解决不了的部分再带定向指令调用一次 LLM，返回新 SQL 和修复过程 |
| `fix_column_refs` | `skills/schema_search.py` | SchemaSearch 的确定性部分：把挂错别名的列改到同一作用域里唯一拥有它的表上，并撤回会让关联条件失效的改动 |
| `llm_repair` / `observed_text` | `skills/base.py` | `llm_repair` 用统一的修复模板调用 LLM 并抽取 SQL；`observed_text` 把报错或自检发现的问题写成 prompt 中的一段 |
| `run_arm` / `summarize` | `evaluation/loop_run.py` | `run_arm` 逐题运行 Loop，Loop 返回后再用 Gold 判分；`summarize` 汇总恢复率、误伤率、可执行数、成本等指标 |
| `flatten_loop_records` / `build_rows` | `dbx/publish.py` | 把每题的每次尝试展平成一行，再拆成 trace 行（`traces.*`）和对错行（`evaluation.*`）写入 Delta |

### B2. Controller：闭环的核心代码

**功能**：`LoopController.run` 对一道题完成"生成 → 自检 → 诊断 → 修复"的闭环：先检索候选表、生成第一版 SQL；然后每一轮执行 SQL 并自检，不通过就观察失败现象、诊断失败类型、路由到对应技能修复，得到新的 SQL 进入下一轮；自检通过或次数用完后，从所有尝试中选出最终答案，返回 `LoopResult`。`_execute` 负责执行一条 SQL，返回执行摘要和完整结果行。

`loop_engineer/controller.py`（有删节，只保留主干）：

```python
@dataclass
class LoopConfig:
    max_attempts: int = 2           # 预算：最多几次 SQL 尝试
    top_k: int = 20                 # 检索的表数
    max_result_rows: int = 500_000  # 结果行数上限，超出记为 TOO_MANY_ROWS

def run(self, task: AgentTask, verifier) -> LoopResult:
    # ① 检索 ② 生成：第 1 次尝试
    retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)
    gen = self.generator.generate(task, retrieval.tables)
    attempt = {"case_id": ..., "attempt_id": 1, "question": ..., "retrieved_tables": ...,
               "generated_sql": gen.sql, "parse_status": gen.parse_status, ...}
    attempts, all_rows = [], []
    for n in range(1, self.cfg.max_attempts + 1):
        # ③ 执行
        exe, rows = self._execute(attempt["generated_sql"], task.db, attempt["parse_status"])
        attempt.update(exe)
        # ④ 验证
        decision = verifier.verify(attempt, rows)
        attempt.update({"verifier_mode": decision.mode,
                        "verifier_decision": "PASS" if decision.passed else "FAIL",
                        "verifier_signals": list(decision.signals)})
        attempts.append(attempt); all_rows.append(rows)
        if decision.passed or n == self.cfg.max_attempts:
            break
        # 产生下一次尝试
        nxt = {"case_id": ..., "attempt_id": n + 1, ...}
        obs = observe(attempt, task.db)                          # ⑤ 观察（白名单）
        diag, usage = self.diagnoser.diagnose(obs)               # ⑥ 归因
        route = self.policy.route(diag)                          # ⑦ 选技能
        res = self.policy.skill(route.skill).repair(obs, diag, self.ctx)   # ⑧ 修复
        attempt.update({"failure_type": diag.failure_type, "diagnosis_confidence": diag.confidence,
                        "diagnosis_reason": diag.reason, "diagnosis_source": diag.source,
                        "repair_hints": json.dumps(diag.repair_hints)})
        nxt.update({"generated_sql": res.repaired_sql, "repair_skill": route.skill,
                    "repair_action": res.repair_action, "used_llm": res.used_llm, ...})
        attempt["repaired_sql"] = nxt["generated_sql"]
        attempt["repair_skill"] = nxt["repair_skill"]
        attempt = nxt
    # ⑨ 选最终答案
    passed   = [i for i, a in enumerate(attempts) if a["verifier_decision"] == "PASS"]
    executed = [i for i, a in enumerate(attempts) if a["execution_status"] == "SUCCESS"]
    final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
    ...
    return LoopResult(attempts, all_rows, final)
```

**设计要点**：
- Controller 本身**不包含任何失败处理逻辑**，只负责编排。判断、归因、选技能、修复分别交给 Verifier、Diagnoser、Policy 和 Skill。每个组件都可以单独测试和替换，消融实验也可以直接替换组件。
- `verifier` 是 `run()` 的参数，不是 Controller 的成员。所以同一个 Controller 可以按题目换用不同的验证器，例如 OracleVerifier 需要每题各自的 Gold 判分函数。

### B3. Context 机制：各步骤之间传递什么

Loop 里有 5 种 context 对象，各自有明确的**读写权限**：

```
             写入者                  读取者                        内容
attempt 字典 Controller、③④⑥⑧     Observer、⑨、trace 发布        一次尝试的全部过程（工作记忆）
Observation  Observer（从 attempt   Diagnoser、Skill               attempt 的白名单只读视图 + 解析出的信号
             按白名单生成）
Diagnosis    Diagnoser              Policy、Skill                  失败类型、置信度、原因、修复线索
RepairContext 启动时构造一次        所有 Skill                     共享资源：schema 目录、LLM 客户端、示例
RepairResult Skill                  Controller（写成下一个 attempt）新 SQL 和修复动作说明
```

#### ① attempt 字典：一次尝试的工作记忆，也是 trace 的来源

| 字段组 | 字段 | 由哪一步写入 |
|---|---|---|
| 身份 | `case_id`, `attempt_id`, `question`, `strategy` | Controller |
| 生成 | `retrieved_tables`, `generated_sql`, `parse_status` | ② 或上一次的 ⑧ |
| 执行 | `execution_status`, `execution_error`（截断到 2000 字符）, `result_row_count`, `result_preview`（前 5 行）, `exec_latency_ms` | ③ |
| 验证 | `verifier_mode`, `verifier_decision`, `verifier_signals` | ④ |
| 归因 | `failure_type`, `diagnosis_confidence`, `diagnosis_reason`, `diagnosis_source`, `repair_hints` | ⑥（写在**失败的这次**尝试上） |
| 修复 | `repair_skill`, `repair_action`, `repair_reason`, `repair_fallback`, `used_llm`, `repaired_sql` | ⑧（修复动作写在**下一次**尝试上；`repaired_sql` 同时写在失败的这次上，方便对照） |
| 成本 | `input_tokens`, `output_tokens`, `llm_latency_ms`, `diag_tokens` | ② / ⑥ / ⑧ |
| 结果 | `final_status`（FINAL / SUPERSEDED） | ⑨ |

**attempt 里没有任何 Gold 字段。** 每次尝试的对错是在 Loop 结束后，由 `evaluation/loop_run.py` 另外计算的。

#### ② Observation：诊断和修复唯一能看到的"现场"

**功能**：`observe` 把一次尝试的记录转换成诊断可用的观察结果：只按白名单保留运行时可见的字段，再用正则从 Databricks 报错文本中解析出错误类别、找不到的列和它的别名、数据库给的候选列、不存在的表名。

```python
OBSERVABLE_FIELDS = ("case_id", "attempt_id", "question", "retrieved_tables", "generated_sql",
                     "parse_status", "execution_status", "execution_error",
                     "result_row_count", "result_preview")

def observe(record: dict, db: str) -> Observation:
    r = {k: record.get(k) for k in OBSERVABLE_FIELDS}   # 白名单：其余字段一律进不来
    # 从报错文本中解析出结构化信号（纯正则）
    ... error_class, unresolved_qualifier, unresolved_column, suggestions, missing_table
```

- **白名单**：即使传入的记录里带有 `correct`、`eval_*` 或 Gold 字段，也会被过滤掉（`tests/test_phase4.py`）。
- **`frozen=True`**：Observation 不可修改。Skill 如果要基于"修好一部分的 SQL"继续处理，会通过 `dataclasses.replace` 生成一份新的 Observation。

#### ③ Diagnosis：归因和 Skill 之间的交接格式

```python
@dataclass(frozen=True)
class Diagnosis:
    failure_type: str      # 6 类失败之一，或 UNKNOWN
    confidence: float      # 0~1
    reason: str            # 给人看、也会写进修复 prompt 的一句话
    source: str            # rule | llm | fallback
    repair_hints: dict     # ★ 交给 Skill 的结构化线索
    version: str = "diagnoser-v1"
```

`repair_hints` 是 context 机制的核心。诊断时查到的证据不会丢掉，而是**原样交给 Skill**，Skill 不用重新分析一遍：

```json
{"signal": "unresolved_column", "column": "DEPARTMENT_CODE",
 "qualifier": "ata", "qualifier_table": "academic_terms_all",
 "owner_tables": ["sis_department", "..."], "suggestions": ["sd.DEPARTMENT_CODE", "..."],
 "case": "wrong_alias"}                     // wrong_alias | table_not_used | not_retrieved | hallucinated
```

#### ④ RepairContext：Skill 共享的资源

```python
@dataclass
class RepairContext:
    catalog: SchemaCatalog          # 97 张表的列名、Databricks 类型、示例值（不含 Gold）
    client: ChatClient              # 带缓存和预算的 LLM 客户端
    examples: list[FewShotExample]  # 与生成阶段相同的 few-shot 示例
    max_schema_tables: int = 24     # 修复 prompt 里最多展示几张表
```

#### ⑤ 修复 prompt：LLM 实际看到的 context

需要 LLM 的 Skill 都用同一个模板（`skills/base.py`），**只在"修复指令"这一段有所不同**：

```
{rules}            ← 与生成阶段相同的 baseline-v2 规则（不修改）
Schema:
{schema}           ← 使用的表 + 检索到的表 + Skill 补充的表（去重，最多 24 张）
Question: {question}
A previous attempt produced this SQL:
{sql}              ← 上一次的 SQL（SchemaSearch 会先放入修好一部分的版本）
{observed}         ← 报错原文（截断到 1200 字符），或"返回了 N 行，结果可能不对"
Diagnosis: {diagnosis}      ← 失败类型: 原因
{instruction}      ← ★ 每个 Skill 不同
Return the corrected query as ONE read-only SQL query ...
```

**这个 prompt 只在这一次调用中使用，不会保存，也不会改变系统的生成 prompt。** 永久修改系统属于 Outer Loop 的范畴（A3）。

#### 哪些信息不进入 context，以及如何保证

| 不允许进入的信息 | 保证方式 |
|---|---|
| Gold SQL、Gold 表、关联键、列映射、领域知识标注 | Agent 只拿到 `AgentTask(case_id, question, db)`；`tests/test_phase5.py` 扫描 `skills/`、`policy`、`diagnose`、`observer` 的 import，确保不引用 `benchmark` 或 `evaluation` |
| 这次尝试的对错 | 对错只写在 `evaluation.*` 表里；Observation 使用白名单 |
| 其他题目的信息 | 每题一个独立的 attempt 链，Skill 不会修改共享状态 |

### B4. Verifier

**功能**：`SelfVerifier.verify` 在没有 Gold 的情况下判断一次尝试是否可疑：先看执行状态（没有 SQL、报错、结果过大、空结果、整列 NULL），能执行时再检查 SQL 结构和结果数值是否自相矛盾（自检 v2，详见 [LOOP_WALKTHROUGH.md](LOOP_WALKTHROUGH.md#s3-3-2) 3.3.2），返回是否通过、触发的信号以及每个问题的证据和修复提示。`OracleVerifier.verify` 直接用 Gold 判对错，只用于上界分析。下面的代码只列出基础信号。

```python
SELF_SIGNALS = ("no_sql", "execution_error", "too_many_rows", "empty_result", "all_null_column")

class SelfVerifier:                       # 主实验：只用 Loop 自己能看到的信号
    signals: tuple = SELF_SIGNALS         # 可以只启用其中一部分（参数）
    def verify(self, attempt, rows):
        # NO_SQL/EMPTY_RESPONSE → no_sql；TOO_MANY_ROWS → too_many_rows；其他非 SUCCESS → execution_error
        # SUCCESS 且 0 行 → empty_result；SUCCESS 且有一列全为 NULL → all_null_column
        ...

class OracleVerifier:                     # 只用于上界分析：和 Gold 比较
    judge: Callable[[rows], (bool, str)]
```

### B5. Diagnoser：规则优先，没有报错信号时才用 LLM

**功能**：根据观察结果判断失败属于哪一类（选表、列映射、关联键、查询拆解、领域知识、执行错误），给出置信度、原因，以及交给技能的修复线索 `repair_hints`。`diagnose_by_rules` 是规则部分，判断不了返回 `None`；`Diagnoser.diagnose` 是入口，规则没有结论时才调用 LLM。

```python
def diagnose_by_rules(obs, catalog) -> Diagnosis | None:
    if 没有生成 SQL:                         return Diagnosis(EXECUTION, 0.9, ...)
    if obs.error_class == "UNRESOLVED_COLUMN":
        owner = column_owners(catalog, col)             # 这一列属于哪些表（查 schema）
        used  = set(alias_map(obs.generated_sql).values())   # SQL 实际用了哪些表（解析语法树）
        if owner ∩ used:                      return Diagnosis(COLUMN_MAPPING, 0.9,  case="wrong_alias")
        if owner ∩ retrieved - used:          return Diagnosis(TABLE_RETRIEVAL, 0.8, case="table_not_used")
        if owner:                             return Diagnosis(TABLE_RETRIEVAL, 0.85, case="not_retrieved")
        return                                       Diagnosis(COLUMN_MAPPING, 0.7,  case="hallucinated")
    if error_class == "TABLE_OR_VIEW_NOT_FOUND": return Diagnosis(TABLE_RETRIEVAL, 0.8, ...)
    if execution_status == "TOO_MANY_ROWS":    return Diagnosis(JOIN_KEY, 0.6, ...)
    if execution_status == "ERROR":            return Diagnosis(EXECUTION, 0.8 或 0.6, ...)
    return None                                # 执行成功：没有报错信号 → 交给 LLM

class Diagnoser:
    def diagnose(self, obs):
        d = diagnose_by_rules(obs, self.catalog)
        if d is not None: return d, {}         # 规则有结论：不调用 LLM
        # LLM 阶段：问题、表、SQL、行数、前几行 → 严格 JSON {failure_type, confidence, reason}
```

### B6. Policy：失败类型到 Skill 的映射

**功能**：`Policy.route` 根据诊断出的失败类型查映射表，选出负责修复的技能名；被禁用的技能退回 RepairSQL，并标明是否为临时替代。`Policy.skill` 按名字从注册表 `SKILLS` 取出技能对象，交给 Controller 调用。

```python
TARGETED = {
    TABLE_RETRIEVAL:     "RetrieveAgain",
    COLUMN_MAPPING:      "SchemaSearch",
    JOIN_KEY:            "FindJoinPath",
    DOMAIN_KNOWLEDGE:    "ReplanQuery",   # 临时替代：RetrieveKnowledge 还没实现（标记为 fallback）
    QUERY_DECOMPOSITION: "ReplanQuery",
    EXECUTION:           "RepairSQL",
    UNKNOWN:             "RepairSQL",
}

@dataclass
class Policy:
    disabled: set[str] = set()        # 被禁用的 Skill 会退回 RepairSQL（用于消融）
    mapping: dict = TARGETED          # 映射表本身也可以替换
    def route(self, diagnosis) -> Route(skill, fallback, reason)
```

映射写成数据而不是写死在代码里，所以消融实验只需要换配置，不用改代码。

### B7. Skill：统一接口，各自负责一类修复

**功能**：每个 Skill 的 `repair` 针对一类失败修复 SQL：读取诊断给的修复线索，先做不需要 LLM 的确定性工作（改正列的别名、查出要补的表和候选关联键等），解决不了的部分再用统一的修复模板、带上自己的定向指令调用一次 LLM，返回修复后的 SQL 和修复过程（`RepairResult`）。

```python
class RepairSkill(Protocol):
    name: str
    def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult: ...

@dataclass(frozen=True)
class RepairResult:
    repaired_sql: str; repair_skill: str; repair_action: str; repair_reason: str
    tables: tuple[str, ...]; used_llm: bool
    input_tokens: int = 0; output_tokens: int = 0; latency_ms: int = 0; parse_status: str = "OK"
```

| Skill | 读取哪些 repair_hints | 做法 | 修复指令（instruction 的核心） | LLM |
|---|---|---|---|---|
| **SchemaSearch** | `column`, `qualifier`, `suggestions` | ① `fix_column_refs`：按作用域检查所有带别名的列引用，列不在别名对应的表里、但同一作用域只有一张表有它时，改到那张表；② 如果改完后比较条件两边变成同一张表，撤销并标记"需要真正的关联键"；③ 全部能确定时直接返回，否则把修好一部分的 SQL 交给 LLM | 错误的引用列表 + 候选列（引擎建议 + 名字相近的列，最多 8 个，相似度 ≥ 0.5）+ 需要关联键时附上候选关联键（最多 10 个） | 全部能确定时不用 |
| **RetrieveAgain** | `tables_to_add` / `owner_tables`, `missing_table` | 把拥有这一列的表（最多 4 张）加进 schema，并附上它们与现有表之间的候选关联条件（最多 8 个）；表不存在时，提供名字相近的真实表（最多 4 张，相似度 ≥ 0.4） | "这一列在表 T 里，用这些候选条件把 T 关联进来" | ✅ |
| **FindJoinPath** | — | 列出 SQL 中各表共有的键列（最多 12 个） | "逐条核对 JOIN ON 条件；会产生重复行时，先聚合再关联" | ✅ |
| **ReplanQuery** | — | 要求重新规划查询结构 | "先列出子问题，每个子问题写成一个 CTE，再逐项核对输出列、筛选、分组、排序" | ✅ |
| **RepairSQL** | `error_class` | 带上报错信息做最小修改 | "用最小改动修复报错" + Databricks 语法限制说明 | ✅ |

`fix_column_refs` 的核心（`skills/schema_search.py`）：

**功能**：在不调用 LLM 的情况下修正列引用：逐个作用域检查带别名的列，列不在别名对应的表里、而同一作用域只有一张表有它时，改到那张表；改完后关联条件两边变成同一张表的，撤回改动并标记"需要真正的关联键"。返回修改后的 SQL、改动列表和改不了的引用。

```python
for scope in traverse_scope(tree):                          # 按作用域处理（CTE、子查询分开）
    tables = {alias: table for alias, source in scope.sources.items() if 是真实的表}
    for col in scope.columns:
        if col 的别名不在 tables 里: continue                # 不带别名，或是 CTE / 子查询的别名：不处理
        if col.name in 该别名对应表的列: continue            # 本来就对
        owners = [同一作用域中拥有这一列的别名]
        if 只有一张表拥有:  改到那张表的别名，并记录改动
        elif 多张表拥有:    标记为"有歧义"，交给 LLM
        else:               标记为"作用域内没有表拥有"，交给 LLM
# 保护措施：改完后，比较条件的两边如果是同一个别名 → 撤销，标记"需要真正的关联键"
for cmp in tree.find_all(EQ, NEQ, GT, GTE, LT, LTE):
    if 两边都是列 且 别名相同 且 其中有被改动过的:  撤销，加入 unresolved
```

**Skill 之间怎么配合**：各 Skill 之间不直接调用，而是通过三种方式协作：
1. **共用基础能力**：`agent/join_graph.py` 的候选关联键同时被 SchemaSearch、RetrieveAgain 和 FindJoinPath 使用；`skills/base.py` 的 `llm_repair()` 和 `REPAIR_TEMPLATE` 被所有需要 LLM 的 Skill 使用。
2. **确定性修复先行，LLM 接手剩余部分**：SchemaSearch 把能确定的部分先修好，再把修好一部分的 SQL 交给 LLM，LLM 在更好的起点上继续修复。
3. **由 Policy 统一分派，接口一致**：Controller 不需要知道具体是哪个 Skill，替换或禁用某个 Skill 不会影响其他部分。

### B8. 参数一览

| 参数 | 默认值 | 位置 | 作用 |
|---|---|---|---|
| `max_attempts` | 2 | `LoopConfig` | 尝试预算 |
| `top_k` | 20 | `config/phase1.yaml` retrieval | 检索的表数（在 300 道非评测题上调出） |
| `max_result_rows` | 500,000 | `config/phase1.yaml` databricks | 结果行数上限 |
| `statement_timeout_s` | 120 | `config/phase1.yaml` databricks | 单条 SQL 超时 |
| `ansi_mode` | false | `config/phase1.yaml` databricks | 与 MySQL 行为对齐（x/0 返回 NULL） |
| `model` | `glm-4-flash` | `config/phase1.yaml` llm / `ZHIPU_MODEL` | LLM 模型 |
| `do_sample` | False | `agent/llm.py` | 关闭随机采样，结果可复现 |
| `max_output_tokens` | 2048 | `config/phase1.yaml` llm | 单次输出上限 |
| `max_calls` / `max_tokens` | 400 / 300 万 | `config/phase1.yaml` llm | 单次运行的预算上限 |
| LLM 缓存 | `runs/phase1/llm_cache.jsonl` | `config/phase1.yaml` llm | 按（模型, 参数, system, prompt）指纹重放 |
| `SelfVerifier.signals` | 全部 5 种 | `loop_engineer/verifier.py` | 启用哪些验证信号 |
| 诊断置信度 | 0.9 / 0.85 / 0.8 / 0.7 / 0.6 | `loop_engineer/diagnose.py` | 手工设定的先验值，**尚未校准** |
| `Policy.mode` / `disabled` | targeted / 空 | `--policy` / `--disable` | 消融实验开关 |
| `max_schema_tables` | 24 | `RepairContext` | 修复 prompt 最多展示几张表 |
| 报错截断 | 2000（trace）/ 1200（prompt） | controller / skills/base | 控制 context 长度 |
| 结果预览 | 5 行 | controller | 写进 trace，并交给 LLM 诊断 |
| 候选列 | 8 个，相似度 ≥ 0.5 | `skills/schema_search.py` | SchemaSearch |
| 补充的表 / 候选关联 | 4 张 / 8 个 | `skills/retrieve_again.py` | RetrieveAgain |
| 候选关联键 | 10 个（SchemaSearch）/ 12 个（FindJoinPath） | 各 Skill | 控制 prompt 长度 |
| 关联键的判定规则 | 名称以 `_CODE/_KEY/_ID/_NUMBER` 结尾等 | `agent/join_graph.py` | 只从 schema 推断 |

### B9. Trace 的代码路径

**功能**：`run_arm` 在 Loop 返回之后，用 Gold 判断每次尝试是否答对，和尝试记录一起写成逐题记录；`flatten_loop_records` 把每题的每次尝试展平成一行；`build_rows` 再把尝试字段写进 `traces.execution_traces`、把对错写进 `evaluation.evaluation_results`，两类数据分表存放。

```python
# evaluation/loop_run.py：Loop 结束后，在外部判分（Loop 看不到这一步）
res = controller.run(case.agent_view(), verifier_for(case.case_id))
correct = [judge(rows)[0] if a["execution_status"] == "SUCCESS" else False
           for a, rows in zip(res.attempts, res.rows)]
rec = {"attempts": res.attempts, "attempt_correct": correct, "final_index": res.final_index, ...}

# dbx/publish.py：每次尝试展开为一行 trace
def flatten_loop_records(records):
    for r in records:
        for k, a in enumerate(r["attempts"]):
            yield {**a, "correct": r["attempt_correct"][k], ...}
# build_rows()：attempt 字段 → traces.execution_traces
#               correct       → evaluation.evaluation_results
```

---

## C. 错误分析 → 归因 → Skill 调用：设计依据

每一个设计决定都对应前面阶段的实测数据（数据来源见 EXECUTION_LOG.md）。

### C1. 数据告诉我们失败长什么样

| 观察到的现象 | 数据 | 对设计的影响 |
|---|---|---|
| 大多数失败会**执行报错** | 评测集 baseline：87 个失败中 68 个是执行报错，其中 58 个是 `UNRESOLVED_COLUMN` | **报错信息是最主要的证据来源** → 归因以规则为主 |
| 报错里的列**大多存在于别的表** | 开发集 21 个列报错中，20 个列存在于另一张已检索到的表 | 多数是"挂错表"，而不是"编造列名" → 可以**查 schema 做确定性修复** |
| 缺失的表**大多已经检索到了** | 缺失的 Gold 表中，132 张已检索但没用上，32 张没检索到 | 选表失败的主要原因是"没用上" → RetrieveAgain 的重点是**把已有的表关联进来**，而不只是扩大检索 |
| 引擎**只报告第一个错误** | SchemaSearch v1 修完一处后，同一条 SQL 在下一处再次报错 | **一次修完所有能确定的列引用**（v2）：开发集可执行 4 → 8 |
| 自动修改可能**改变语义** | dw_4188：关联条件被改成 `sd.X = sd.X` | 确定性修复必须有**语义保护**（恒真条件检查） |
| 没有报错信号时，LLM 诊断**无效** | LLM 级诊断：评测集严格 0/19，而且答错时置信度更高 | LLM 只作为兜底；换模型后重新评估 |
| 瓶颈在**模型能力** | 干预实验：5 类 Gold 提示全部给出后仍是 0/30 | 现阶段以工程指标（能否执行）为主，准确率要换模型后才能体现 |

### C2. 为什么归因以规则为主

1. **证据充分**：Databricks 的报错直接给出了出错的列、所用的别名和候选列。拿这些信息查 schema，就能确定这一列属于哪张表，不需要推理。
2. **准确率更高**：规则级诊断的宽松准确率是 76.5%（评测集 68 题），LLM 级只有 6/19。
3. **结果可复现、可解释**：同样的输入总是得到同样的结论，每条结论都能说清依据（"这一列在 X 表里，而 SQL 没用这张表"）。
4. **成本为零，不受模型强弱影响**：换模型只影响 LLM 这一级。

规则的判断顺序依据的是**这一列在 schema 中的位置**（B5）：同样是"列无法解析"，它可能属于"已用的表""已检索但没用的表""没检索到的表"，或者"哪张表都没有"。这四种情况的根因不同，修法也不同，所以归因必须先分清是哪一种。

### C3. 为什么这样映射到 Skill

| 失败类型（细分情况） | Skill | 为什么这样修 | 依据 |
|---|---|---|---|
| 列映射：别名挂错 | SchemaSearch（确定性） | 正确的表已经在 SQL 里，只需要改别名；不需要 LLM，也就不会引入新的错误 | 20/21 的列报错属于这种情况 |
| 列映射：编造列名 | SchemaSearch（LLM） | schema 里没有这一列，只能由 LLM 根据候选列按题意选择 | 引擎建议 + 名字相近的列可以缩小选择范围 |
| 列映射：在关联条件中 | SchemaSearch → LLM + 候选关联键 | 改别名会让关联条件恒真；需要找到真正的关联路径 | 问题记录 #8 |
| 选表：表没用上 / 没检索到 | RetrieveAgain | 需要把新表关联进来，必须知道关联条件 → 附上 schema 推断的候选关联键 | 132 vs 32 |
| 选表：表不存在 | RetrieveAgain | 模型编造了表名 → 提供名字相近的真实表 | 评测集 3 例 `TABLE_OR_VIEW_NOT_FOUND` |
| 关联键 | FindJoinPath | 结果行数暴增通常是关联条件错了 → 提供共有键列，要求逐条核对 | 行数上限信号 |
| 查询拆解 | ReplanQuery | 结构性错误需要重新规划，局部修改解决不了 | 抽检 case 20：整个子问题被漏掉 |
| 领域知识 | ReplanQuery（临时） | 需要非 Gold 的知识来源，RetrieveKnowledge 还没实现 | ROADMAP 5.4 |
| 执行错误 | RepairSQL | 语法、聚合、窗口等问题，通常只需要局部修改 | 带上 Databricks 语法限制说明 |

### C4. 为什么允许 Loop 自动修改 SQL

Loop 修改的只是**这一题的 SQL**，而且有多层保护：

| 风险 | 保护措施 |
|---|---|
| 修改了数据 | 执行器只允许单条只读语句（`is_read_only`），其他语句一律拒绝 |
| 看到答案 | Observation 白名单 + import 检查；判分只在 Loop 结束后进行 |
| 确定性修改改变了语义 | 只在"同一作用域内只有一张表拥有这一列"时才修改；改完后检查比较条件，恒真就撤销 |
| 修复反而改坏了 | 最终答案优先取通过验证的、其次取能执行的尝试，第 2 次的错误结果不会覆盖第 1 次能执行的结果 |
| 陷入无限循环、成本失控 | `max_attempts = 2`，`UsageMeter` 限制调用次数和 token 总量 |
| 把对的答案改错（误伤） | Verifier 通过就立即停止；误伤率作为核心指标单独统计（换模型后才能测出） |
| 修改影响其他题 | 修复 prompt 只在当次调用中使用；系统层面的修改只能通过 Outer Loop 在开发集上验证后进行 |

### C5. 已知局限与后续改进

| 局限 | 影响 | 可以改进的方向 |
|---|---|---|
| 只修复 1 次，第 2 次失败后不再诊断 | dw_4188 这类"修复后出现新问题"的情况没有机会再修 | 预算放宽到 3 次，并让第 2 次失败重新诊断 |
| 诊断只看到第一个报错 | 诊断出的往往是直接原因，而不是根因 | 同时记录"根因"和"直接原因"两种标签 |
| 诊断置信度未校准 | 不能用置信度来决定是否修复 | 用开发集上每条规则的实际准确率替代手工设定的值 |
| 确定性修复只用在 SchemaSearch | 其他 Skill 的 LLM 修复仍从有多处错误的 SQL 开始 | 把 `fix_column_refs` 作为所有 Skill 的前置步骤 |
| RetrieveKnowledge 未实现 | 领域知识类失败没有对应的修复手段 | 值定位：在查询中出现的业务术语，到字符串列的取值里查找匹配 |
| Verifier 覆盖不到"能执行但答错" | 评测集估计有 15 个失败会被漏掉 | 增加结果形状检查，例如输出列数与问题要求是否一致 |
