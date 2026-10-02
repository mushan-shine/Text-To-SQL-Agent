# Context 设计：原则、内容与代码

> 本文回答：**每次调用 LLM 时，模型看到了什么？为什么是这些？由哪段代码拼出来？以后要怎么演进？**
> 相关文档：整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，各部分实现见 [deep_dive/](deep_dive/00_总览.md)，相关讨论见 [疑问与价值.md](疑问与价值.md)（疑问 11、17、18、20）。
> 代码引用以 `product-loop-review` 分支为准；token 数据为 2026-10-02 实测（方法见第 7 节）。

---

## 1. 定义与范围

本项目里的 **context** 指**每次调用 LLM 时送进去的全部内容**（system 提示 + user prompt）。项目里共有 4 种 LLM 调用：

| 调用 | 何时发生 | 拼装代码 | 在线 / 离线 |
|---|---|---|---|
| **生成** | 每道题第 1 次尝试 | [agent/generator.py:102](../agent/generator.py#L102) `build_prompt` | 在线 |
| **修复** | 自检不通过后，修复技能调用 LLM 改写 SQL | [skills/base.py:85](../skills/base.py#L85) `llm_repair` | 在线 |
| **诊断（LLM 级）** | 规则判断不了失败原因时 | [loop_engineer/diagnose.py:186](../loop_engineer/diagnose.py#L186) | 在线 |
| LLM 裁判、干预实验 | 离线评估 | `loop_engineer/judge.py`、`evaluation/intervention.py` | 离线，不进入 Loop |

本文以前三种为主；第四种只在第 6 节说明它们为什么被隔离在在线 context 之外。

### 1.1 与 Agent 领域 context 概念的对应

上面的定义对应 Agent 领域里**上下文窗口**的含义：大模型本身没有记忆，每次调用只依据这一次送进去的内容作答。在 Agent 领域，context 通常还有另外两层含义，本项目都有对应：

| 层面 | 含义 | 本项目对应 |
|---|---|---|
| **上下文窗口** | 模型这一次能看到的全部内容，受 token 上限约束 | 每次调用的 system 提示 + user prompt（第 4 节） |
| **Agent 的信息状态** | 完成任务所需的全部信息，存放在模型之外，每一步挑选一部分写进输入 | **短期记忆**：尝试记录（上次的 SQL、报错、自检发现、诊断）；**长期记忆**：示例库、数仓使用说明、审核知识；**环境信息**：表结构、数据库执行结果 |
| **上下文工程** | 每一步给模型看什么：选什么、怎么压缩、怎么排列、怎么隔离、产生的信息写到哪里 | 本文第 2–4 节的原则和拼装方式 |

上下文工程的五个问题在本项目里的做法：

| 问题 | 本项目的做法 |
|---|---|
| **选什么** | BM25 选 20 张表、相似题取 4 道、使用说明按题取相关几行、审核知识按条件触发 |
| **怎么压缩** | 报错 ≤ 1,200 字、结果只给前 5 行、每列样例值 ≤ 3 个 |
| **怎么排列** | 规则在前、审核知识先于统计说明、示例紧挨问题 |
| **怎么隔离** | Observer 白名单挡住标准答案；生成、诊断、修复各自组装 prompt |
| **写到哪里** | 尝试记录写进 trace；外循环把知识写进知识库 |

**本项目的特点**：Loop 由代码编排，下一步做什么由控制器按固定顺序决定，而不是由模型自己决定调用哪个工具。所以工具定义不进入 context，执行、检索、诊断都由代码直接调用；上一步的信息也不靠累积的对话历史传递，而是**由代码挑选需要的部分，重新写进下一次的 prompt**（例如把上次的 SQL、报错和诊断写进修复 prompt）。这样每次调用的 context 完全确定、可复现、可缓存，每一步能看到什么也可以精确控制（例如诊断和修复看不到标准答案），成本不会随步骤增加而膨胀；代价是模型不能自己决定"再查一下数据库确认一个值"。

---

## 2. 设计原则

| # | 原则 | 含义 | 代码体现 |
|---|---|---|---|
| 1 | **Gold 隔离** | context 只含 Agent 能合法看到的信息 | `AgentTask` 只有编号、问题、库名；Observer 白名单；Gold 提示只在离线实验里使用（第 6 节） |
| 2 | **分层、按生命周期管理** | 不同来源、不同更新频率的内容分开维护 | 结构事实 / 通用约定 / 经验知识 / 运行时观察（第 3 节） |
| 3 | **按题检索，不全量注入** | 只放和这道题相关的部分 | 表取前 20、示例取 4 道、说明只取相关几行、审核知识按条件触发 |
| 4 | **按可信度排序** | 越可信的越靠前、措辞越强 | 审核知识（"follow them"）> 统计说明（"conventions, not rules"）> 示例 |
| 5 | **设上限，控成本** | 每块内容都有数量或长度上限 | 第 4 节各表的"上限"列 |
| 6 | **给证据，不只给指令** | 修复时带上结构化证据，而不是只说"改正" | 报错原文、候选列、推断的关联键，由诊断的 `repair_hints` 和技能整理后写进修复指令 |
| 7 | **可复现、可追溯** | 同样输入拼出同样的 prompt，每次都留档 | 确定性拼装、prompt 版本号、完整 prompt 写进 trace、缓存键就是 prompt |
| 8 | **可开关、可对照** | 每一层能单独开关，做对照实验 | `few_shot` static / dynamic、`knowledge` on / off、`curated`、Policy 可禁用单个技能 |

---

## 3. Context 分层与生命周期

```
            更新频率低 ───────────────────────────────────────▶ 更新频率高
  ┌──────────────┬──────────────┬──────────────────────┬──────────────────┐
  │  结构事实      │  通用约定      │  经验知识               │  运行时观察        │
  │  表、列、类型   │  系统提示      │  相似题示例库            │  问题              │
  │  样例值        │  6 条规则      │  数仓使用说明            │  上次的 SQL、报错   │
  │               │              │  审核知识               │  自检发现、诊断     │
  │               │              │                        │  技能整理的证据     │
  ├──────────────┼──────────────┼──────────────────────┼──────────────────┤
  │ 从数据库读取    │ 人写           │ 外循环产出               │ 内循环每次尝试产生   │
  │ 结构变了才重建  │ 改 prompt 版本 │ 重建或审核批准时          │ 每次尝试           │
  │ schema_dw.json │ 代码常量       │ examples_pool / kb.json │ 只在本次 prompt     │
  │               │              │ / knowledge_items       │ 和 trace 里        │
  └──────────────┴──────────────┴──────────────────────┴──────────────────┘
```

| 层 | 内容 | 产生方式 | 存储 | 代码 |
|---|---|---|---|---|
| **结构事实** | 97 张表的列名、Databricks 类型、每列 ≤ 3 个样例值 | 读 `information_schema` + BEAVER 表样例 | `runs/phase1/schema_dw.json` | [retriever.py:89](../agent/retriever.py#L89) `from_databricks`、[scripts/phase1.py:68](../scripts/phase1.py#L68) |
| **通用约定** | 系统提示、6 条规则 | 人写，随 prompt 版本变化 | 代码常量 | [generator.py:34](../agent/generator.py#L34) `SYSTEM`、[:37](../agent/generator.py#L37) `RULES` |
| **经验知识** | 相似题示例库（5,508 道）、数仓使用说明、审核知识 | 外循环（全量统计 + 错误驱动迭代 + 人工审核） | `examples_pool.json.gz`、`kb.json`、Delta `experience.knowledge_items` | `agent/examples.py`、`agent/knowledge.py`、`agent/curated.py` |
| **运行时观察** | 问题、上次的 SQL、执行结果和报错、自检发现、诊断、修复证据 | 内循环 | 不持久化到 context 资产，只进入 trace | `loop_engineer/observer.py`、`diagnose.py`、`skills/` |

**原则**：低频层由人或离线流程维护，高频层由运行时生成；**外循环只往"经验知识"层叠加，不改结构事实和通用约定**。

---

## 4. 三种在线 context

### 4.1 生成 context

**拼装**：[generator.py:153](../agent/generator.py#L153) `generate` → [generator.py:102](../agent/generator.py#L102) `build_prompt`

```python
# generate(task, tables)：tables 是 BM25 检索出的 20 张表
if self.index is not None:                                  # 相似题示例（dynamic 模式）
    hits = self.index.top(task.question, self.k)            # 4 道最相似的已解题
    examples = [h.example for h in hits]
    extra = [t for h in hits for t in h.tables if t in self.catalog.tables and t not in tables]
    tables = tuple(tables) + tuple(dict.fromkeys(extra))[: self.max_extra_tables]      # 补表 ≤ 6 张
if self.curated:                                            # 审核知识里的补表提示
    tables = tuple(tables) + tuple(t for t in self.curated.extra_tables(task.question, tables)
                                   if t in self.catalog.tables)
notes    = self.knowledge.notes_for(task.question, tables) if self.knowledge is not None else ""
reviewed = self.curated.notes_for(task.question, tables) if self.curated else ""
notes    = "\n\n".join(n for n in (reviewed, notes) if n)   # 审核知识在前
prompt = build_prompt(task, render_schema(self.catalog, tables), examples, oracle_hints, notes)
r = self.client.complete(prompt, system=SYSTEM)
```

```python
def build_prompt(task, schema_text, examples, oracle_hints=None, notes=""):
    parts = [RULES, "", "Schema:", schema_text, ""]
    if notes:
        parts += [notes, ""]
    if examples:
        parts.append("Examples (from the same warehouse):")
        for ex in examples:
            parts += [f"Question: {ex.question}", f"```sql\n{ex.sql}\n```", ""]
    if oracle_hints:                                         # 只有离线干预实验会传
        parts.append("Verified facts about this question (use them):")
        parts += [f"- {h}" for h in oracle_hints]
        parts.append("")
    parts += [f"Question: {task.question}", "SQL:"]
    return "\n".join(parts)
```

**内容清单**（按 prompt 中的顺序）：

| 顺序 | 块 | 来源 | 层 | 选择方式 | 上限 | 开关 |
|---|---|---|---|---|---|---|
| 0 | 系统提示 | `SYSTEM` | 通用约定 | 固定 | — | — |
| 1 | 规则 | `RULES` | 通用约定 | 固定 | 6 条 | 随 prompt 版本 |
| 2 | Schema | `render_schema`（[:90](../agent/generator.py#L90)） | 结构事实（选哪些表由经验知识影响） | BM25 前 20 张 + 相似题补表 + 补表提示 | 补表 ≤ 6；每列样例 ≤ 3 个、每个 ≤ 40 字 | `few_shot`、`curated` |
| 3 | 审核知识说明 | `CuratedKnowledge.notes_for`（[curated.py:57](../agent/curated.py#L57)） | 经验知识 | 按条件：表在 schema 里、问题含关键词 | 只取触发的条目 | `curated` |
| 4 | 数仓使用说明 | `WarehouseKnowledge.notes_for`（[knowledge.py:148](../agent/knowledge.py#L148)） | 经验知识 | 按本题词 → 表得分取相关部分 | ≤ 6 张表、≤ 8 对关联（实测 7–13 行） | `knowledge` |
| 5 | 示例 | 相似题（[examples.py:90](../agent/examples.py#L90)）或固定 3 道 | 经验知识 | BM25 问题相似度 | 4 道；SQL ≤ 2,500 字 | `few_shot` |
| 6 | 本题问题 | `AgentTask.question` | 运行时 | — | — | — |

**顺序的理由**：规则是全局约束，放最前；schema 告诉模型"有什么可用"；说明紧跟 schema，便于对照"这些表通常怎么用"，审核知识在统计说明之前；示例离问题最近，模型更容易模仿；以 `SQL:` 结尾，引导直接输出。

**prompt 版本号**（[generator.py:149](../agent/generator.py#L149)）记录组合：`baseline-v2`（固定示例）/ `baseline-v3-dynfs`（相似题）+ `+kb` + `+cur`。

**真实样例**（开发集 dw_2277，相似题 + 使用说明配置；本地重建，schema 只保留开头）：

````
Rules:
1. Answer with exactly ONE read-only SQL query (SELECT, CTEs allowed) inside a ```sql code fence. No explanation.
2. Use only the tables and columns listed in the schema. Qualify columns with table aliases when joining.
3. The dialect is Databricks SQL. String comparisons on table columns are case-insensitive.
4. Questions were written for MySQL. In MySQL, STDDEV(), STD() and VARIANCE() compute POPULATION statistics, ...
5. Many codes, years and dates are stored as strings (for example term codes like '2014FA').
6. Return only the columns the question asks for, in the order it mentions them.

Schema:
TABLE dw.course_catalog_subject_offered
  ACADEMIC_YEAR STRING  -- e.g. 2024, 2021, 2025
  TERM_CODE STRING  -- e.g. 2021SP, 2025SP, 2022SP
  SUBJECT_ID STRING  -- e.g. 18.03, 18.02, 5.111
  ...
... （共 22 张表：检索 20 张 + 相似题补 2 张）...

Warehouse usage notes (counted from 5656 solved questions of this warehouse; conventions, not rules):
- Tables that solved questions with similar wording use most (strongest first): sis_department (100%),
  course_catalog_subject_offered (82%), tip_subject_offered (50%), academic_terms (45%), ...
- Look-alike tables (similar columns) - for this kind of question solved queries used: academic_terms 55%, academic_terms_all 45%
- Look-alike tables (similar columns) - for this kind of question solved queries used: tip_subject_offered 72%, library_subject_offered 28%
- Join sis_department - tip_subject_offered: usually sis_department.DEPARTMENT_CODE = tip_subject_offered.OFFER_DEPT_CODE
  (899/900 of 750 queries), INNER JOIN (98%)
- ...（共 8 对关联）

Examples (from the same warehouse):
Question: For each subject offered in the 2022 academic year with a total of at least 6 units in the Mathematics
department (Course 18), show the subject code, ...
```sql
...
```
...（共 4 道）

Question: For each subject offered in the 2022 academic year with a total of at least 6 units in the Mathematics
department (Course 18), show the subject code, subject number, department name, ... the count of students in the
department, and the subject title, ...
SQL:
````

### 4.2 修复 context

**拼装**：[skills/base.py:85](../skills/base.py#L85) `llm_repair`，模板 [skills/base.py:53](../skills/base.py#L53) `REPAIR_TEMPLATE`

```python
def llm_repair(skill, obs, diagnosis, ctx, tables, instruction, action, details=None):
    tables = [t for t in dict.fromkeys(tables) if t in ctx.catalog.tables][: ctx.max_schema_tables]   # ≤ 24 张
    prompt = REPAIR_TEMPLATE.format(rules=RULES, schema=render_schema(ctx.catalog, tuple(tables)),
                                    question=obs.question, sql=obs.generated_sql or "(no SQL was produced)",
                                    observed=observed_text(obs),
                                    diagnosis=f"{diagnosis.failure_type}: {diagnosis.reason}",
                                    instruction=instruction)
    r = ctx.client.complete(prompt, system=SYSTEM)
```

````
{rules}
Schema:
{schema}
Question: {question}
A previous attempt produced this SQL:
```sql
{sql}
```
{observed}
Diagnosis: {diagnosis}
{instruction}
Return the corrected query as ONE read-only SQL query inside a ```sql code fence. No explanation.
````

**内容清单**：

| 块 | 来源 | 层 | 上限 |
|---|---|---|---|
| 系统提示 + 规则 | `SYSTEM`、`RULES` | 通用约定 | 与生成相同 |
| Schema | SQL 已用的表 + 技能补充的表（RetrieveAgain）+ 检索到的表，按此顺序去重后截断 | 结构事实 | ≤ 24 张（`RepairContext.max_schema_tables`） |
| 问题 | `obs.question` | 运行时 | — |
| 上次的 SQL | `obs.generated_sql` | 运行时 | — |
| 观察到的问题 | `observed_text(obs)`（[base.py:73](../skills/base.py#L73)）：报错原文，或自检发现的问题（带修复提示） | 运行时 | 报错 ≤ 1,200 字 |
| 诊断 | `失败类型: 原因` | 运行时 | — |
| 定向指令 | 各技能自己写，带证据 | 运行时 | 候选关联 ≤ 8–12 条 |

各技能指令里带的证据：

| 技能 | 指令中的证据 |
|---|---|
| SchemaSearch | 改不了的列引用、数据库给的候选列、名字相近的列、（若涉及关联条件）共享键列 |
| RetrieveAgain | 缺失列属于哪些表、这些表与已用表的推断关联键；或名字相近的真实表 |
| FindJoinPath | SQL 中各表共享的键列 |
| ReplanQuery | 固定的"拆成子问题 / CTE 并逐项核对"指令 |
| RepairSQL | Databricks 语法限制说明 |

**真实样例**（同一道 dw_2277，固定示例基线的失败尝试 → 规则诊断 → RetrieveAgain；本地重建，schema 20 张已省略）：

````
...（规则、schema 同上）...

Question: For each subject offered in the 2022 academic year ... the count of students in the department, ...

A previous attempt produced this SQL:
```sql
WITH ranked_subjects AS (
  SELECT ccso.SUBJECT_CODE, ..., SUM(ccso.NUM_ENROLLED_STUDENTS) OVER (PARTITION BY sd.DEPARTMENT_NAME) AS student_count, ...
  FROM COURSE_CATALOG_SUBJECT_OFFERED ccso JOIN SIS_DEPARTMENT sd ON ccso.DEPARTMENT_CODE = sd.DEPARTMENT_CODE
  WHERE ccso.ACADEMIC_YEAR = '2022' AND ...
) SELECT ...
```
Executing it failed with:
[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter with name `ccso`.`NUM_ENROLLED_STUDENTS`
cannot be resolved. Did you mean one of the following? [`ccso`.`GRADE_RULE_DESC`, ...]. SQLSTATE: 42703; line 8 pos 8

Diagnosis: TABLE_RETRIEVAL_FAILURE: NUM_ENROLLED_STUDENTS lives in ['library_subject_offered', 'subject_offered',
'subject_offered_summary', 'tip_subject_offered'], retrieved but not used by the SQL

Column NUM_ENROLLED_STUDENTS is not in the table you used; it lives in library_subject_offered, subject_offered,
subject_offered_summary, tip_subject_offered. Join the appropriate one of these tables. Join candidates inferred from
the schema: course_catalog_subject_offered.MASTER_SUBJECT_ID = library_subject_offered.MASTER_SUBJECT_ID; ...
Check that every column the question needs comes from a table that really has it.

Return the corrected query as ONE read-only SQL query inside a ```sql code fence. No explanation.
````

这个样例说明了两点：
- **原则 6 的价值**：修复 context 带了具体证据：哪一列出错、这一列实际在哪 4 张表、怎么关联，而不是只说"改正"。
- **修复的局限，以及缺口 G1 的影响**：这道题的标准答案统计学生人数用的是学生目录表 `mit_student_directory`，4 张候选表都不对。错误发生在上游：在固定示例的配置下，模型把"院系学生人数"理解成了"课程选课人数"（这恰好也是这个数仓的多数用法：已解题中问题带 "student" 时最常用 `tip_subject_offered`，占 54%，学生目录表排第 13，BM25 检索的前 20 张表里也没有它），诊断只能顺着报错里的列名找候选表。改用相似题示例后，检索到的 4 道相似已解题都用了学生目录表，生成的 SQL 随之改对了这个概念。可见这类问题靠生成阶段的知识解决；修复 context 也应该带上这些知识（缺口 G1），否则修复时可能把已经对了的方向改回去。

### 4.3 诊断 context（LLM 级）

**拼装**：[diagnose.py:197](../loop_engineer/diagnose.py#L197)，模板 `LLM_PROMPT`（[diagnose.py:137](../loop_engineer/diagnose.py#L137)），系统提示 `LLM_SYSTEM`。

```python
prompt = LLM_PROMPT.format(question=obs.question, tables=", ".join(obs.retrieved_tables),
                           sql=obs.generated_sql, status=obs.execution_status,
                           rows=obs.result_row_count, preview=json.dumps(obs.result_preview)[:600])
```

| 块 | 来源 | 上限 |
|---|---|---|
| 6 种失败类型的定义 | 模板 | 固定 |
| 问题、可用的表（只有表名，**没有 schema**） | `obs` | — |
| SQL、执行状态、行数、前几行结果 | `obs` | 预览 ≤ 600 字 |
| 输出格式要求 | 模板 | 只输出 JSON |

**只在规则判断不了时调用**，典型是 SQL 能跑但结果为空、某列全是 NULL。它是三种 context 里最小的（实测平均约 610 token），也是效果最差的：glm 上严格准确率 0/19。

---

## 5. Context 资产的生产

| 资产 | 生产命令 / 入口 | 输入 | 排除 | 产物 |
|---|---|---|---|---|
| 表结构目录 | `scripts/phase1.py`（首次运行时生成并缓存） | Databricks `information_schema` + `dev_tables.json` 样例值 | 不读任何 Gold 标注 | `runs/phase1/schema_dw.json` |
| 相似题示例库 | 运行时 `build_generator_index`；网页用 `scripts/build_app_bundle.py` 打包 | dw 已解题的问题 + Gold SQL（经 Phase 0 规则适配、可解析、≤ 2,500 字） | 评测集、开发集（外循环时再排除挖掘样本和验证集） | 5,508 道；`app/bundle/examples_pool.json.gz` |
| 数仓使用说明 | `python scripts/build_knowledge.py` | 训练集 Gold SQL 与问题 | 评测集、开发集 | `runs/knowledge/kb.json`（词 → 表、相似表组、134 对表的关联约定） |
| 审核知识 | `python scripts/outer_loop.py` → 审核页批准 | 训练集错题 + 用户反馈 | 评测集；门禁用验证集 / 开发集 | Delta `experience.knowledge_items`（active） |

运行时加载：网页端在每次运行时由 [app/runner.py:193](../app/runner.py#L193) `build_controller` 组装；审核知识每次都从 Delta 重新读取，**批准或停用对下一次提问生效**。

---

## 6. 边界与隔离

这些机制本身不进入 context，但决定了**什么不可能进入 context**：

| 机制 | 作用 | 代码 |
|---|---|---|
| `AgentTask` | Agent 只接收编号、问题、库名；Gold 字段在类型上就拿不到 | [benchmark/beaver/dataset.py:30](../benchmark/beaver/dataset.py#L30) |
| Observer 白名单 | 诊断和修复只能看到 12 个白名单字段；"是否答对"等 Gold 派生字段进不来 | [loop_engineer/observer.py:16](../loop_engineer/observer.py#L16) |
| 导入限制 | 修复技能、诊断、Policy、Observer 不能 import `benchmark` / `evaluation`（测试强制）。**（修复技能、诊断、Policy、Observer 是 Loop 在运行时处理失败的组件，它们本该只看到 Agent 自己能观察到的信息。如果其中某个文件导入了这两个包，它就有能力拿到标准答案。哪怕是无意的，比如为了图方便调用一个工具函数，也可能让诊断或修复"偷看答案"。那样实验结果就作废了：线上没有标准答案，系统的真实能力会被高估。）** | `tests/test_phase5.py` |
| 数据三分 | 示例库和使用说明只来自训练集；评测集只在配置冻结后使用，开发集用于调测系统 | `agent/examples.py`、`agent/knowledge.py` |
| 存储分离 | trace 里没有对错字段，对错只在 `evaluation.*` | `dbx/publish.py` |

LLM 裁判（`judge.py`）和干预实验离线使用。裁判的 context 里另有一段"仓库约定"（`CONVENTIONS`），避免它把正确的 `STDDEV_POP` 写法判为错误；它误报太高（DeepSeek 31/33），没有接入 Loop。

---

## 7. 实测：token 构成

### 7.1 方法

- **生成 context**：LLM 缓存只存 prompt 的哈希、不存原文，但 prompt 拼装是确定性的。因此用同一套代码、同一份表结构 / 示例库 / 使用说明，在本地**重建**开发集 30 题的生成 prompt：用一个只记录 prompt 的假客户端，**不调用 LLM**。按 `build_prompt` 写入的固定标题切分出各块，统计字符数；再用三次历史运行里每道题**真实的输入 token 数**校准（每题 token / 字符比约 0.34–0.36），按字符占比折算各块 token。
- **修复与诊断 context**：依赖每次运行时的观察结果，不重建，直接用 Phase 6 Loop 运行记录里每次调用的真实 token 数。
- 局限：按字符折算假设各块 token 密度相同；schema 里大写标识符多，实际 token 密度可能略有不同。

### 7.2 生成 context 的构成（开发集 30 题，glm-4-flash，平均每题）

| 块 | 固定示例 `baseline-v2` | 相似题 `v3-dynfs` | 相似题 + 使用说明 `v3-dynfs+kb` |
|---|---|---|---|
| 系统提示 | 39（0.3%） | 38（0.3%） | 38（0.2%） |
| 规则 | 330（2.4%） | 322（2.1%） | 318（2.1%） |
| **Schema** | **11,762（86.9%）** | **11,758（78.5%）** | **11,605（75.1%）** |
| 数仓使用说明 | — | — | 680（4.4%） |
| 示例 | 1,185（8.8%，3 道） | 2,647（17.7%，4 道） | 2,614（16.9%，4 道） |
| 问题 | 212（1.6%） | 207（1.4%） | 205（1.3%） |
| **合计（真实输入 token）** | **13,528** | **14,972** | **15,458** |
| schema 平均表数 | 20 | 20.9 | 20.9 |

运行记录：`baseline-20260925T084206-51d855`、`baseline-20260928T110813-2524c9`、`baseline-20260928T171726-e8ae97`。

### 7.3 修复与诊断 context（Phase 6 开发集 Loop，glm-4-flash，固定示例配置）

| 调用 | 次数 | 平均输入 token |
|---|---|---|
| 生成（第 1 次尝试） | 30 | 13,528 |
| 修复 · RetrieveAgain | 14 | 13,336 |
| 修复 · SchemaSearch（LLM 部分） | 9 | 13,284 |
| 修复 · RepairSQL | 3 | 13,683 |
| 修复 · ReplanQuery | 1 | 13,248 |
| 诊断（LLM 级，输入 + 输出） | 1 | 610 |

运行记录：`targeted-self-20260925T105746-eb2ade`。

### 7.4 结论

1. **Schema 占 75%–87%**，是成本的绝对主体；规则、问题、说明加起来不到 10%。
2. 相似题示例让示例块从 1.2k 增到 2.6k token，总量 +11%，换来开发集答对 0 → 3；使用说明再 +3%，答对 3 → 5。**经验知识层性价比很高**。
3. 相似题平均只补 0.9 张表：检索已经覆盖了示例用到的大部分表，相似题的主要价值在示例 SQL 本身，而不是补表。
4. **修复 context 几乎和生成一样大**（约 1.33 万 token），主要也是 schema；修复和生成的区别在内容（报错、诊断、定向证据），不在长度。
5. 优化成本的方向应该是 **schema**（见 F4），而不是压缩规则或说明。

---

## 8. 现状缺口

| # | 缺口 | 影响 | 证据 |
|---|---|---|---|
| G1 | **修复 context 没有经验知识**：无相似题、无使用说明、无审核知识 | 修复时看不到"这一类问题在这个数仓里怎么写"；生成阶段已经理解对的概念，修复时可能被改回去 | 4.2 节样例（dw_2277）：相似题示例让生成改用了正确的表，而修复 prompt 不带示例 |
| G2 | `RepairContext.examples` 是闲置字段 | 代码和设计不一致 | 所有技能只用 `catalog`、`client`、`max_schema_tables` |
| G3 | LLM 诊断的 context 没有 schema，给修复的线索只有 `{"signal": "llm"}` | LLM 诊断准确率低，后续修复缺少依据 | 严格准确率 0/19 |
| G4 | 没有会话上下文（提问页单轮） | 不能追问 | — |
| G5 | 没有个人经验层 | 用户确认过的答案要等审核后才能被利用 | 疑问 17 |
| G6 | 命令行 Loop 不加载审核知识 | 无法用命令行衡量审核知识的效果 | `scripts/phase6.py` 未调用 `attach_curated` |
| G7 | 审核人不能编辑知识的措辞 | 措辞不好只能驳回 | 审核页只有批准 / 驳回 |
| G8 | Schema 每列都全量列出 | 占 75%–87% 的 token，且无关列会分散注意力 | 第 7 节 |
| G9 | 经验知识和表结构存为 JSON 文件（只有审核知识在 Delta） | 更新要重新打包部署；网页和命令行各有副本；无法从运行记录追溯用了哪个知识版本；表结构变化不会被自动感知 | 疑问与价值·疑问 23；决策 D7（**暂不迁移，待优化**） |

---

## 9. 未来设计

### F1 修复 context 复用生成时的经验知识（对应 G1、G2）

- 生成时把本题的相似题示例 id、使用说明、审核知识随 `Generation` 一起保存（`example_ids`、`notes` 已经有）；
- 修复时由控制器把它们带进 `RepairContext` 或修复模板，`examples` 字段换成"本题的相似题"；
- 修复模板新增两段："Warehouse usage notes"、"Similar solved questions"。为控制长度，可以只放和技能相关的部分，例如 RetrieveAgain 只放涉及候选表的说明；
- **先在开发集上做对照实验**：修复成功率、token 变化。

### F2 LLM 诊断补充 schema 摘要和结构化输出（对应 G3）

- 诊断 context 加上 SQL 用到的表的列名摘要；
- 要求 LLM 除了失败类型，还输出"可疑的列 / 表 / 关联"，转成 `repair_hints`。

### F3 会话上下文与个人经验层（对应 G4、G5，详见疑问 17）

| 层 | 内容 | 写入 | 生效范围 | 进入 context 的方式 |
|---|---|---|---|---|
| 会话上下文 | 本次对话前几轮的问题、最终 SQL、结果摘要 | 自动 | 当前对话 | 生成 prompt 新增"Conversation so far"段，最多保留最近 N 轮 |
| 个人经验 | 用户 👍 过的答案、自己修正过的 SQL | 自动，不审核 | 只对这个用户 | 检索相似条目放进示例段，标注"previously confirmed by you" |
| 组织知识 | 现有的审核知识 | 审核后 | 所有人 | 不变 |

数据模型统一为一条经验记录：`question, sql, rating, corrected_sql, user, scope(user / org), status(raw / candidate / verified)`；被多个用户确认的个人经验，作为候选进入外循环。

### F4 Schema 压缩（对应 G8）

- **列级检索**：只列出和问题相关的列，加上主键 / 关联键列；表名仍保留，避免模型以为表不存在；
- 样例值只对字符串编码类列保留；
- 风险：漏掉需要的列会直接导致报错。必须在开发集和验证集上对比准确率和 token，**准确率不降才采用**。

### F5 其他

- 命令行 `phase6.py` 增加 `--curated` 开关（G6）；
- 审核页支持"编辑后批准"，记录修改人和原文（G7）；
- 产品侧"解释我的理解"：把模型对口径的理解作为输出的一部分展示给用户核对（疑问 18）。

### F6 知识资产迁移到 Delta 表（对应 G9，待优化，暂不实施）

> 决策 D7（2026-10-02）：当前继续用文件存储，以下为以后的迁移方案。

| 资产 | 唯一来源 | 运行时形式 |
|---|---|---|
| 规则、prompt 模板 | 代码 / Git（不变） | 代码常量 |
| 表结构 | Unity Catalog 元数据：`information_schema` + 表 / 列注释 | 进程内缓存，定时刷新 |
| 数仓使用说明 | Delta 表（如 `experience.kb_stats`），带 `kb_version` | 启动时加载当前版本 |
| 相似题示例库 | Delta 表（如 `experience.example_pool`），带版本 | 内存 BM25；规模大了换向量检索 |
| 审核知识 | `experience.knowledge_items`（已是 Delta） | 每次运行读 active 行 |

配套：运行元数据记录所用知识版本，保证可复现；服务启动时加载、定期刷新，不按题查表。改动点：`scripts/build_knowledge.py` 与示例库构建改写 Delta；`load_knowledge`、`build_generator_index`、`load_bundle` 改为读表（保留文件兜底）。

---

## 10. 面试讲法

> "我把 context 按生命周期分四层：结构事实（表结构，从数据库读）、通用约定（人写的规则）、经验知识（外循环从 5,600 道已解题和审核中产出）、运行时观察（内循环每次尝试产生）。外循环只往经验知识层叠加，不动结构和规则。
> 拼装遵循几条原则：Gold 隔离（类型上只给 Agent 问题和库名，诊断修复走白名单）、按题检索而不是全量注入、按可信度排序、每块设上限、修复时给证据而不只是指令，并且每层都能开关做对照。
> 我实测过构成：一次生成约 1.5 万 token，schema 占四分之三以上，经验知识只多花 14% 就把开发集从 0/30 提到 5/30。梳理时也发现了缺口：修复 prompt 没有带生成时的经验知识。比如 dw_2277 问'院系学生人数'，固定示例时模型理解成了课程选课人数，这也是这个数仓的多数用法，报错后诊断只能顺着错的列名找候选表；换成相似题示例后，4 道相似已解题都用学生目录表，生成就改对了。所以这类概念错误要靠生成阶段的知识解决，修复也应该带上这些知识，否则可能把对的方向改回去。下一步是让修复复用这些知识，并做 schema 列级压缩，都先在开发集上做对照实验。"