# ④ 内循环 Loop

> 一句话：**单道题、运行时、不看 Gold** 的自我修复：执行 → 自检 → 观察 → 诊断 → 路由 → 修复 → 再执行，直到自检通过或用完次数。
> 入口：[loop_engineer/controller.py:107](../../loop_engineer/controller.py#L107) `LoopController.run`。评测、网页"运行 Loop"、网页"提问"用的是同一个控制器。

---

## 1. 设计原理与价值

### 1.1 要解决的问题

一次生成的 SQL 经常跑不起来：基线评测集 89 题里 68 题执行报错，其中 58 题是 `UNRESOLVED_COLUMN`。而 Phase 1 发现，**21 个列报错里有 20 个，列其实存在于另一张已经选出的表里**，只是挂错了别名。这类错误有明确信号（报错），可以针对性地修。

难点在于：
- 运行时**没有 Gold**，系统只能靠自己能观察到的信号判断"错了没有"；
- "错了就重试"效率低，要知道**为什么错**，才能给出针对性的修复信息；
- 修复本身可能把对的改错（**误伤**），需要度量。

### 1.2 设计原则

| 原则 | 做法 |
|---|---|
| **只看 Agent 可见的信号** | 自检只用执行状态、结果和 SQL 结构；Observer 用白名单取字段；技能不能 import benchmark / evaluation |
| **先规则，后 LLM** | 报错能说明问题的，用规则诊断、确定性修复；只有没有明确信号时才调 LLM |
| **诊断和修复解耦** | 诊断输出"失败类型 + 修复线索"，Policy 查表决定用哪个技能，映射是数据，可以做消融 |
| **自检只接零误报的信号** | 每个候选检查都离线测"抓错率 / 误报率"，误报≈0 才能触发修复 |
| **可观测** | 每一步通过 `on_event` 推送给网页，并写入尝试记录 |

### 1.3 价值（开发集 30 题）

| 运行 | 可执行（首次 → 最终） | 说明 |
|---|---|---|
| glm · 自检 | 4 → **9** | 诊断 + 定向修复 |
| deepseek + 相似题示例 · 最多修复 4 次 | 可执行 29/30，最终答对 **8** | 生成端改进后 Loop 的效果 |

**结论**：诊断 + 定向修复让更多 SQL 能执行（glm 4 → 9）；但"能执行但语义错"靠自检发现不了（见 3.2），这部分交给外循环。这正是"内循环修显式失败、外循环学隐式约定"分工的依据。

---

## 2. 模块清单

| 模块 | 文件 | 职责 |
|---|---|---|
| 控制器 | [loop_engineer/controller.py](../../loop_engineer/controller.py) | 驱动整个循环、选最终答案、推送事件 |
| 自检 | [loop_engineer/verifier.py](../../loop_engineer/verifier.py) | `SelfVerifier`（无 Gold）/ `OracleVerifier`（上界） |
| 语义检查 | [loop_engineer/checks.py](../../loop_engineer/checks.py) | 结构检查 + 数值一致性检查（"计算器"） |
| 观察 | [loop_engineer/observer.py](../../loop_engineer/observer.py) | 白名单取字段，从报错解析信号 |
| 诊断 | [loop_engineer/diagnose.py](../../loop_engineer/diagnose.py) | 规则诊断 + LLM 诊断，输出失败类型和修复线索 |
| 路由 | [loop_engineer/policy.py](../../loop_engineer/policy.py) | 失败类型 → 修复技能（映射表） |
| 修复技能 | [skills/](../../skills) | 5 个技能 + 统一接口 `skills/base.py` |
| 候选自检（未接入） | [loop_engineer/judge.py](../../loop_engineer/judge.py)、[loop_engineer/validators.py](../../loop_engineer/validators.py) | LLM 裁判、查数据库的验证器；离线评估后只作提示 |

---

## 3. 各模块的设计原理与代码实现

### 3.1 控制器：循环主体

**实现**：[controller.py:107](../../loop_engineer/controller.py#L107) `run(task, verifier, on_event)`

```python
retrieval = self.retriever.retrieve(task.question, top_k)          # ③ 检索 20 张表
gen = self.generator.generate(task, retrieval.tables)               # ③ 第 1 次生成
attempt = {attempt_id: 1, generated_sql, parse_status, retrieved_tables, tokens...}

for n in 1..max_attempts:
    exe, rows = self._execute(sql, db, parse_status)                # ② 只读执行，限 50 万行
    decision  = verifier.verify(attempt, rows)                      # 自检：通过 / 不通过 + 信号
    attempts.append(attempt)
    if decision.passed or n == max_attempts: break

    obs   = observe(attempt, db)                                    # 1. 观察（白名单）
    diag, usage = self.diagnoser.diagnose(obs)                      # 2. 诊断
    route = self.policy.route(diag)                                 # 3. 路由
    res   = self.policy.skill(route.skill).repair(obs, diag, ctx)   # 4. 执行技能
    nxt   = {generated_sql: res.repaired_sql, repair_skill: route.skill, ...}
    attempt = nxt

# 选最终答案：最后一次自检通过的 > 最后一次能执行的 > 最后一次
final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
return LoopResult(attempts, rows_per_attempt, final)
```

- `max_attempts = 最大修复次数 + 1`（网页可设 0–4 次修复）。
- **预算按"SQL 尝试"计**，诊断的 LLM 调用不算尝试，但计入 token 成本（`diag_tokens`）。
- **事件**：每一步调用 `emit(step, payload)`（retrieve / generate / execute / verify / observe / diagnose / route / repair / final），payload 是写进 trace 的内容的副本；回调出错只记日志，不会打断循环（[controller.py:108](../../loop_engineer/controller.py#L108)）。
- `LoopResult`（[:56](../../loop_engineer/controller.py#L56)）保存每次尝试的记录和结果行；**对错不在这里**，由评测在 Loop 结束后另算（[06](06_评测.md)）。

### 3.2 自检（Verifier）：判断"有没有明显问题"，而不是"对不对"

**原理**：线上没有 Gold，自检只能发现"明显有问题"的尝试。因此"通过"的含义是**没发现问题**，不等于答对。网页和报告里统一写成"自检通过"。

**实现**：[verifier.py:38](../../loop_engineer/verifier.py#L38) `SelfVerifier.verify`，信号分两层：

| 层 | 信号 | 条件 |
|---|---|---|
| 基础（v1） | `no_sql`、`execution_error`、`too_many_rows`、`empty_result`、`all_null_column` | 没生成 SQL、报错、行数超限、结果为空、某列全是 NULL |
| 结构检查（v2） | `join_tautology`、`join_without_condition`、`missing_grouping`、`rounding` | 关联条件恒为真（x = x）、JOIN 无条件、"对每个"却没有 GROUP BY、题目要求不四舍五入却用了 ROUND |
| 数值一致性（v2） | `negative_statistic`、`count_not_integer`、`min_greater_than_max`、`avg_outside_min_max`、`std_var_mismatch`、`std_exceeds_range` | 统计量之间必然成立的关系被违反 |

结构和数值检查只在 SQL 能执行、有结果时才运行（`check_all`，[checks.py:381](../../loop_engineer/checks.py#L381)）。**只有 `TRIGGER_SIGNALS`（[checks.py:48](../../loop_engineer/checks.py#L48)）会判为不通过**，其他检查结果作为"提示"（advisories）显示，不触发修复。

**数值一致性检查（"计算器"）**：[checks.py:319](../../loop_engineer/checks.py#L319) `check_numeric`。数值全部由数据库算好，代码只核对关系，不让大模型推理：

```
把每个输出列追溯到产生它的统计函数（顺着 CTE 别名；ROUND、COUNT(DISTINCT) 等不确定的跳过）
检查：统计量非负；计数是整数；min ≤ max；min ≤ avg ≤ max；
      同族（都是 _POP 或都是 _SAMP）方差 = 标准差²；标准差 ≤ 极差
```

**哪些信号能接入，由离线评估决定**（`scripts/verifier_eval.py`：抓错看开发集 30 个"能执行但答错"的尝试，误报看 Gold SQL）：

| 候选方法 | 抓到错题 | 误报 | 结论 |
|---|---|---|---|
| 重复行、"对每个"只有 1 行、输出列数 | 3–7 / 30 | 正确答案上同样常见 | 不接入 |
| 题干中的值缺失、缺统计函数 | 0–1 / 30 | 4.2%–7.5% | 只作提示 |
| 结构检查 4 条 | 0 / 30 | ≈ 0 | **接入**（安全，能发现结构性错误） |
| 数值一致性 6 条 | 0 / 22 | **0 / 378** | **接入**（零成本、零误报） |
| LLM 裁判（`judge.py`） | glm 10/30，deepseek 27/30 | glm 4/33，deepseek **31/33** | 不接入（决策 V1） |
| 查数据库的验证器（`validators.py`） | 过滤值 0/30，关联放大 5/30 | 关联放大在 Gold 抽样中 22% | 只作提示（决策 V2） |

**关键发现**：BEAVER 的题干和 Gold SQL 本身常不一致（先有 SQL、后生成问题），所以"拿题干核对 SQL"的方法在这个基准上有天花板。**能执行但语义错**的答案，自检无法可靠发现。

**OracleVerifier**（[verifier.py:80](../../loop_engineer/verifier.py#L80)）直接对照 Gold，只用于"如果自检完美，Loop 能到多好"的**上界实验**，结果一律标注为上界。

### 3.3 观察（Observer）：白名单 + 报错解析

**原理**：诊断和修复只能看到"Agent 自己能观察到的东西"。用白名单而不是黑名单：即使上游记录里混进了 Gold 字段，也传不进来。

**实现**：[observer.py:16](../../loop_engineer/observer.py#L16) `OBSERVABLE_FIELDS` 和 [observer.py:59](../../loop_engineer/observer.py#L59) `observe`

```python
OBSERVABLE_FIELDS = ("case_id", "attempt_id", "question", "retrieved_tables", "generated_sql", "parse_status",
                     "execution_status", "execution_error", "result_row_count", "result_preview",
                     "verifier_signals", "verifier_findings")
r = {k: record.get(k) for k in OBSERVABLE_FIELDS}          # 其余字段一律丢弃
# 从报错文本解析：
error_class          ← "[UNRESOLVED_COLUMN.WITH_SUGGESTION]"  → UNRESOLVED_COLUMN
unresolved_qualifier ← "name `sd`.`TERM_CODE` cannot be resolved" → sd
unresolved_column    ←                                        → TERM_CODE
suggestions          ← "Did you mean one of the following? [`at`.`TERM_CODE`, ...]"
missing_table        ← "The table or view `xxx` cannot be found"
```

### 3.4 诊断（Diagnoser）：为什么错

**原理**：两级。有明确信号的用规则（快、准、免费）；没有信号时才问 LLM。输出 `Diagnosis(failure_type, confidence, reason, source, repair_hints)`，其中 `repair_hints` 把证据带给修复技能。

**规则级**：[diagnose.py:83](../../loop_engineer/diagnose.py#L83) `diagnose_by_rules`。核心是"列找不到"时，去 schema 里查这一列属于哪张表：

| 这一列属于 | 诊断 | 置信度 | 修复线索 |
|---|---|---|---|
| SQL 已经用了的表 | 列映射（别名挂错） | 0.9 | `case: wrong_alias` |
| 检索到但 SQL 没用的表 | 选表（表没用上） | 0.8 | `tables_to_add` |
| 只在没检索到的表里 | 选表（检索遗漏） | 0.85 | `tables_to_add` |
| 没有任何表有这一列 | 列映射（编造列名） | 0.7 | 候选列 |

其他规则：表不存在 → 选表；结果超 50 万行 → 关联键；语法 / 聚合 / 窗口报错 → 执行错误；自检的结构 / 数值信号 → 按 `_VERIFIER_TYPES`（[diagnose.py:51](../../loop_engineer/diagnose.py#L51)）映射，如 `join_tautology` → 关联键，`avg_outside_min_max` → 查询拆解。

**LLM 级**：[diagnose.py:186](../../loop_engineer/diagnose.py#L186)。SQL 能执行但被判可疑、又没有规则信号时，把问题、候选表、SQL、结果摘要交给 LLM，要求输出严格 JSON（`failure_type / confidence / reason`），解析失败记为 `UNKNOWN`。

**准确率**（对照 Phase 3 标注器，评测集 87 个失败）：整体严格 40.2% / 宽松 66.7%；**规则级宽松 76.5%**；LLM 级严格 0/19。最大混淆是"选表 → 列映射"：诊断看到的是直接报错（列挂错别名），标注器看的是根因（还少用了表）。

### 3.5 路由（Policy）：映射是数据

**实现**：[policy.py:28](../../loop_engineer/policy.py#L28) `TARGETED`

```python
TARGETED = {
    TABLE_RETRIEVAL:     "RetrieveAgain",
    COLUMN_MAPPING:      "SchemaSearch",
    JOIN_KEY:            "FindJoinPath",
    DOMAIN_KNOWLEDGE:    "ReplanQuery",   # 临时替代（RetrieveKnowledge 还没有不含 Gold 的知识来源），标记为 fallback
    QUERY_DECOMPOSITION: "ReplanQuery",
    EXECUTION:           "RepairSQL",
    UNKNOWN:             "RepairSQL",
}
```

`Policy(disabled={...})` 可以逐个关掉技能，被禁用的技能退回 RepairSQL，用于消融实验，不用改代码（[policy.py:48](../../loop_engineer/policy.py#L48)）。

### 3.6 修复技能：先确定性，再 LLM

**统一接口**：[skills/base.py](../../skills/base.py)

- 输入：`Observation`、`Diagnosis`、`RepairContext`（schema、LLM 客户端、示例，都是 Agent 可见资源）；
- 输出：`RepairResult(repaired_sql, repair_action, tables, used_llm, tokens, details)`。`details` 记录技能内部做了什么，比如确定性修改、候选列、给 LLM 的指令和完整 prompt，供网页展示；
- 需要 LLM 时统一走 `llm_repair`（[base.py:85](../../skills/base.py#L85)），prompt 模板 `REPAIR_TEMPLATE`（[base.py:53](../../skills/base.py#L53)）：规则 + schema + 问题 + 上次的 SQL + **观察到的问题** + **诊断** + **技能的定向指令**。

| 技能 | 处理 | 做法 |
|---|---|---|
| **SchemaSearch**（[schema_search.py](../../skills/schema_search.py)） | 列映射 | **确定性优先**：`fix_column_refs`（[:39](../../skills/schema_search.py#L39)）按作用域检查**每一个**带别名的列引用，列不在别名对应的表里、但同一作用域只有一张表有它，就改到那张表的别名，不调用 LLM。有歧义或找不到归属的，连同已修好一部分的 SQL、引擎建议、相似列名交给 LLM |
| **RetrieveAgain**（[retrieve_again.py](../../skills/retrieve_again.py)） | 选表 | 把拥有缺失列的表加进 schema，附上从 schema 推断的关联条件；表不存在时给出名字相近的真实表 |
| **FindJoinPath**（[find_join_path.py](../../skills/find_join_path.py)） | 关联键 | 列出 SQL 里各表共享的键列，要求逐个核对 `JOIN ... ON`，会重复行时先聚合再关联 |
| **ReplanQuery**（[replan_query.py](../../skills/replan_query.py)） | 查询拆解 / 领域知识 | 把问题拆成子问题，每个写成一个 CTE，再逐项核对输出列、过滤、分组、排序 |
| **RepairSQL**（[repair_sql.py](../../skills/repair_sql.py)） | 执行错误 / 未知 | 带报错做最小修改，附 Databricks 语法限制说明（窗口函数不能 DISTINCT、排名函数不能带帧等） |

**一个真实的坑和修复**（问题记录 #8）：确定性改别名时，如果错的列引用正好在 `JOIN ON` 里，改完后等号两边变成同一个别名（`sd.X = sd.X`），关联条件恒为真，SQL 能跑但表之间失去了关联。修复：`fix_column_refs` 改完后检查所有"列 比较 列"的条件（[schema_search.py:74](../../skills/schema_search.py#L74)），两边落到同一张表的就撤销，标记"需要真正的关联键"，连同候选关联键交给 LLM。这个 bug 还导致 Phase 5 的"可执行"统计虚高（10 → 实际 8），后来也作为自检规则 `join_tautology` 加进了 Verifier。

---

## 4. 数据流转

一道题的一次失败 → 修复的完整数据（以"列挂错别名"为例；SQL 和行数是**示意**，报错格式和各步字段与真实运行一致）：

```
attempt 1
  generated_sql:  SELECT sd.DEPARTMENT_NAME, at.TERM_CODE ... JOIN sis_department sd ... WHERE sd.TERM_CODE = '2015FA'
  ─ execute ─▶ status=ERROR, error="[UNRESOLVED_COLUMN.WITH_SUGGESTION] `sd`.`TERM_CODE` ... Did you mean [`at`.`TERM_CODE`]"
  ─ verify  ─▶ FAIL, signals=[execution_error]
  ─ observe ─▶ Observation(error_class=UNRESOLVED_COLUMN, qualifier=sd, column=TERM_CODE, suggestions=[at.TERM_CODE])
  ─ diagnose ─▶ schema 查 TERM_CODE 的归属 → academic_terms（SQL 已用，别名 at）
               Diagnosis(COLUMN_MAPPING_FAILURE, 0.9, "wrong alias", repair_hints={column, qualifier, owner_tables})
  ─ route   ─▶ SchemaSearch
  ─ repair  ─▶ fix_column_refs：sd.TERM_CODE → at.TERM_CODE（确定性，不调 LLM）
attempt 2
  generated_sql:  ... WHERE at.TERM_CODE = '2015FA'
  ─ execute ─▶ SUCCESS, 35 rows
  ─ verify  ─▶ PASS（没发现问题）
final = attempt 2
```

每次尝试的记录（`attempt` 字典）最终写入 `traces.execution_traces` 一行：SQL、执行状态 / 报错、自检结论 / 信号、诊断类型 / 置信度 / 原因、修复技能 / 动作、token、耗时。**不含对错**。

在三个入口里的使用方式：

| 入口 | 调用 | Verifier | 结束后 |
|---|---|---|---|
| 评测 `evaluation/loop_run.py` `run_arm` | 每道题一次 `controller.run` | Self 或 Oracle（上界） | 对照 Gold 判分（[06](06_评测.md)） |
| 网页"运行 Loop" `app/runner.py` `_execute` | 同上，后台线程，事件推给页面 | 可选 | 判分 + 分析报告 |
| 网页"提问" `app/runner.py` `_ask` | 对用户的问题调用一次 | 只能 Self（没有 Gold） | 存 `experience.user_queries`，等用户反馈 |

## 5. 讲解要点

- "内循环只看 Agent 自己能观察到的信号：报错、结果形状、SQL 结构。用白名单保证 Gold 进不来，测试强制技能不能 import 评测代码。"
- "诊断是两级：报错信息能说明问题的用规则，规则诊断宽松准确率 76.5%；LLM 诊断在 glm 上几乎无效，所以只作兜底。"
- "自检接入什么由数据决定：我评估了规则、LLM 裁判、数值一致性、查库验证四类方法，只接入零误报的。LLM 裁判抓错 27/30，但误报 31/33，不能用。"
- "诊断 + 定向修复让 glm 在开发集上可执行从 4 道提到 9 道；但'能执行但语义错'自检发现不了，这部分交给外循环。"
