# ③ 生成端 Agent

> 一句话：对一道新问题，**选出相关的表 → 组装上下文 → 让 LLM 写出一条 SQL**。
> 它是内循环的第 1 次尝试，也是外循环知识真正"生效"的地方。

---

## 1. 设计原理与价值

### 1.1 要解决的问题

dw 库有 97 张表、1,530 列，全部塞进 prompt 既贵又会干扰模型。而且表名高度相似（`subject_offered` / `subject_offered_summary` / `tip_subject_offered`），模型很容易选错。基线数据说明了问题在哪：

- glm-4-flash 一次生成，评测集 2/89 答对；
- 缺失的 Gold 表里，**已检索到但没用上 132 次，没检索到只有 32 次**（Phase 3）：瓶颈不是"找不到表"，而是"在一堆候选里选错"。

所以生成端的设计重点是：**给模型的上下文里，要有"这个数仓通常怎么用"的信息**。

### 1.2 设计原则

| 原则 | 做法 |
|---|---|
| **只用 Agent 可见的信息** | schema 来自 Databricks `information_schema` + BEAVER 表样例值；不读任何 Gold 标注 |
| **上下文分层** | 固定规则 → 表结构 → 审核知识 → 统计说明 → 相似题示例 → 问题 |
| **检索只做召回，选择交给上下文** | BM25 取前 20 张表保证召回；用示例和说明帮模型在候选里选对 |
| **可复现、可计费** | 贪心解码、prompt 指纹缓存、调用次数和 token 预算 |
| **每种上下文都能单独开关** | `few_shot: static/dynamic`、`knowledge: on/off`、`curated`，prompt 版本号记录组合 |

### 1.3 价值（开发集 30 题，glm-4-flash，一次生成不修复）

| 配置 | prompt 版本 | 答对 | 能执行 |
|---|---|---|---|
| 固定 3 个示例 | `baseline-v2` | 0 | 4 |
| 相似题示例（dynamic few-shot） | `baseline-v3-dynfs` | 3 | 16 |
| 相似题示例 + 数仓使用说明 | `baseline-v3-dynfs+kb` | **5** | 16 |

deepseek-flash + Loop（最多修复 4 次）：固定示例最终答对 3，相似题示例 **8**。**只改生成端的上下文，不换模型，准确率就明显提升。**

---

## 2. 模块清单

| 模块 | 文件 | 职责 |
|---|---|---|
| 表结构目录 + 表检索 | [agent/retriever.py](../../agent/retriever.py) | `SchemaCatalog`（Agent 可见的 schema）、`BM25TableRetriever` |
| 生成器 | [agent/generator.py](../../agent/generator.py) | 规则、prompt 组装、调用 LLM、抽取 SQL |
| 相似题示例库 | [agent/examples.py](../../agent/examples.py) | 已解题库 + BM25 相似题检索（dynamic few-shot） |
| 数仓使用说明 | [agent/knowledge.py](../../agent/knowledge.py) | 从已解题统计出的用表 / 相似表 / 关联约定（`kb.json`） |
| 已审核知识 | [agent/curated.py](../../agent/curated.py) | 数据工程师批准的知识条目（外循环产出） |
| LLM 客户端 | [agent/llm.py](../../agent/llm.py) | 多厂商、贪心解码、重试、缓存、预算 |
| SQL 结构分析 | [agent/sql_analysis.py](../../agent/sql_analysis.py) | 解析 SQL 用到的表、列、等值关联（中性模块，不含 Gold） |
| 关联候选 | [agent/join_graph.py](../../agent/join_graph.py) | 只从 schema 推断可关联的键列（修复技能使用） |

---

## 3. 各模块的设计原理与代码实现

### 3.1 表结构目录：Agent 眼里的数据库

**原理**：列类型用 **Databricks 实际类型**（BEAVER 表元数据里是 Oracle 的 `VARCHAR2`，与执行引擎不符）；每列附最多 3 个样例值，帮模型理解编码（如学期代码 `'2014FA'`）。

**实现**：[retriever.py:89](../../agent/retriever.py#L89) `SchemaCatalog.from_databricks`

```python
rows = runner.run("SELECT table_name, column_name, full_data_type FROM <catalog>.information_schema.columns "
                  "WHERE table_schema = 'dw' ORDER BY table_name, ordinal_position")
cols = [(t, c, re.sub(r"\s+COLLATE\s+\w+", "", typ)) for t, c, typ in rows]   # 去掉 COLLATE UTF8_LCASE
return cls.build(db, cols, tables_meta)       # build（:73）把 beaver-table 的 example_columns 挂到列上
```

生成一次后缓存为 `runs/phase1/schema_dw.json`（[scripts/phase1.py:68](../../scripts/phase1.py#L68) `load_catalog`），之后所有组件（检索、生成、诊断、修复、网页）都读这个文件。它是"结构事实"，运行时**只读**，外循环也不修改它。

`render_schema`（[generator.py:90](../../agent/generator.py#L90)）把选中的表渲染成 prompt 文本：

```
TABLE dw.academic_terms
  TERM_CODE STRING  -- e.g. 2014FA, 2015SP, 2015SU
  ACADEMIC_YEAR STRING  -- e.g. 2015, 2016
  ...
```

### 3.2 BM25 表检索：保证召回

**原理**：问题里的词和表名、列名、样例值做词频相关度匹配。表名最能代表概念，所以权重最高。

**实现**：[retriever.py:57](../../agent/retriever.py#L57) `Table.document` 和 [retriever.py:119](../../agent/retriever.py#L119) `BM25TableRetriever`

```python
FIELD_WEIGHTS = {"table": 3, "column": 2, "value": 1}     # 表名词重复 3 次、列名 2 次、样例值 1 次
toks  = tokenize(table.name) * 3
toks += tokenize(col.name) * 2   for every column
toks += tokenize(value)[:6] * 1  for every example value

# 标准 BM25（k1=1.2, b=0.75）
idf(w) = log(1 + (N - df + 0.5) / (df + 0.5))
score(t) = Σ_w idf(w) · tf·(k1+1) / (tf + k1·(1 - b + b·|doc|/avgdl))
retrieve(question, k=20) → 按分数取前 20 张表
```

`tokenize`（[retriever.py:36](../../agent/retriever.py#L36)）：小写、下划线拆词、去停用词（`show`、`each`、`number`…），简单去复数（`departments` → `department`）。

**为什么 k=20**：在 300 道非评测题上调参，k=10 召回 0.73，k=15 0.84，**k=20 0.91**（67% 的题能拿全 Gold 表），k=25 0.94，k=30 0.95。k=20 是收益拐点。评测题最多用 8 张表，20 张足够覆盖；再多会增加 token 和干扰。

### 3.3 相似题示例库（dynamic few-shot）：告诉模型"同类问题用哪些表"

**原理**：错误分析显示选错表是主因，而已解题（训练集）里有大量"同类问题用哪些表、怎么关联"的现成答案。检索最相似的几道已解题作为示例，模型就能照着这个数仓的习惯写。验证：前 5 道相似题的用表，平均覆盖开发集 Gold 用表的 88.5%。

**实现**：[examples.py:57](../../agent/examples.py#L57) `ExampleIndex.build`

```python
for q in queries:                                    # dw 全部题
    if q["id"] in exclude_ids: continue              # 排除评测集 + 开发集（+ 外循环时排除当批训练题）
    sql, _ = apply_rules(q["sql"])                   # MySQL Gold → Databricks 可解析（Phase 0 规则）
    if len(sql) > 2500: continue                     # 太长的不要
    sqlglot.parse_one(sql, read="databricks")        # 解析不了的不要
    tables = alias_map(sql).values()                 # 这道题用到的表
    entries.append(PoolEntry(FewShotExample(question, sql, id), tables))
```

结果：5,508 道已解题。检索只看问题文本（BM25，[examples.py:90](../../agent/examples.py#L90) `top`）。网页版把它压缩成 `app/bundle/examples_pool.json.gz`（1.6 MB）。

生成时（[generator.py:153](../../agent/generator.py#L153)）：

```python
hits = self.index.top(task.question, self.k)           # k=4 道最相似的已解题
examples = [h.example for h in hits]                   # 替换固定示例
extra = [t for h in hits for t in h.tables if t not in tables]
tables = tables + extra[: self.max_extra_tables]       # 示例用到、但检索没选出的表（最多 6 张）补进 schema
```

**泄漏检查**：开发集 30 题在示例库前 4 条里没有相同的 Gold SQL 或相同题干。

### 3.4 数仓使用说明（kb.json）：统计出来的约定

**原理**：示例只给了 4 道题，统计能覆盖全部 5,656 道已解题。统计三类约定：

1. **概念 → 表**：每个题干词，其已解题用了哪些表（P(表 | 词)）；
2. **相似表组**：列名 Jaccard ≥ 0.5 且共享 ≥ 4 列的表归为一组（并查集）；
3. **关联约定**：每对表常用的关联键，以及 INNER / LEFT 的比例。

**构建**（离线，`python scripts/build_knowledge.py`）：[knowledge.py:76](../../agent/knowledge.py#L76) `WarehouseKnowledge.build`

```python
for q in 训练集:
    used, edges = _scope_joins(apply_rules(q.sql), tables)     # :35，按作用域解析别名，取出 JOIN ... ON a.x = b.y
    tf.update(used)                                            # 表使用频次
    for w in set(tokenize(q.question)):
        wdf[w] += 1;  wt[w].update(used)                       # 词 → 表 共现
    for (t1, c1, t2, c2, kind) in edges:
        jn["a|b"]["keys"]["a.X = b.Y"] += 1;  jn["a|b"]["kinds"][kind] += 1
kb.word_df = {w: n for w, n in wdf.items() if n >= 5}         # 只保留出现 ≥ 5 次的词
kb.groups  = 并查集(列名 Jaccard ≥ 0.5 且交集 ≥ 4)
```

**使用**（运行时，每题）：[knowledge.py:148](../../agent/knowledge.py#L148) `notes_for`

```python
scores[t] = Σ_{w∈问题} idf(w) · P(t | w)          # table_scores（:136），忽略出现在一半以上题目里的泛词
ranked = 本题 schema 里得分最高的 ≤ 6 张表
输出：
  - 用得最多的表（带相对分数）
  - 本题涉及的相似表组，以及同类问题在组内的使用比例
  - ranked 表两两之间的常用关联键和 INNER/LEFT（最多 8 对）
```

实测每题 7–13 行（多数 12–13 行）。只含聚合统计，不含当前题的任何 Gold 信息。开关：`knowledge.mode: on`、`--knowledge on`、网页开关；prompt 版本加 `+kb`。

### 3.5 已审核知识（curated）：人批准过的规则

**原理**：统计说明是"约定，不是规则"，而外循环提出、数据工程师批准的条目是**明确的规则**，在 prompt 里排在统计说明前面。每次运行都从 Delta 读取 active 条目，所以批准和停用都在下一次提问时生效。

**实现**：[curated.py:31](../../agent/curated.py#L31) `CuratedKnowledge`，四种条目：

| 类型 | 何时生效 | 作用 |
|---|---|---|
| `table_preference`（选表偏好） | 两张相似表之一在 schema 里，且问题含关键词 | 说明中写"用 Y，不用相似表 X" |
| `table_hint`（补表提示） | 问题含关键词，且伴随表之一已在 schema 里 | **把漏掉的表加进 schema**（`extra_tables`，:50），并写一条说明 |
| `join_rule`（关联规则） | 两张表都在 schema 里 | 说明中写关联键和 JOIN 方式 |
| `verified_query`（已验证查询） | 始终 | 加入相似题示例库（`attach_curated`，:81），相似问题会检索到它 |

生成器接入（[generator.py:153](../../agent/generator.py#L153)）：

```python
if self.curated:   # 补表提示：弱模型常漏的表
    tables += tuple(t for t in self.curated.extra_tables(task.question, tables) if t in catalog.tables)
notes    = self.knowledge.notes_for(question, tables) if self.knowledge else ""
reviewed = self.curated.notes_for(question, tables) if self.curated else ""
notes    = "\n\n".join(n for n in (reviewed, notes) if n)      # 审核知识在前
```

### 3.6 prompt 组装与 SQL 抽取

**规则**（[generator.py:37](../../agent/generator.py#L37) `RULES`）是环境约定，不是题目信息：

1. 只输出一条只读 SQL，放在 ```sql 代码块里；
2. 只用 schema 里的表和列，关联时加别名；
3. 方言是 Databricks SQL，字符串比较不区分大小写；
4. **统计函数口径**：题目写 `STDDEV` / `VARIANCE` / "never STDDEV_POP" 时，一律写 `STDDEV_POP` / `VAR_POP`（MySQL 这些函数是总体统计。2,116/5,787 道 dw 题带这句话，决策 D2）；
5. 很多代码、年份存成字符串；
6. 只返回问题要求的列，按提到的顺序。

**组装**（[generator.py:102](../../agent/generator.py#L102) `build_prompt`）：

```
[RULES]
Schema:            ← render_schema(检索 20 张 + 示例补充 ≤6 张 + 补表提示)
[Reviewed usage notes ...]      ← 已审核知识（如有）
[Warehouse usage notes ...]     ← kb 统计说明（如开）
Examples (from the same warehouse):   ← 4 道相似题（dynamic）或 3 道固定题（static）
  Question: ... ```sql ... ```
Question: <用户问题>
SQL:
```

`extract_sql`（[generator.py:119](../../agent/generator.py#L119)）：取第一个代码块；内容不是以 `SELECT` / `WITH` / `(` 开头的判为 `NO_SQL`（glm 有时把拒答也包进 ```sql 块里）。

返回 `Generation`（[generator.py:59](../../agent/generator.py#L59)）：`sql`、`parse_status`、LLM 用量、完整 `prompt`、`example_ids`、`schema_tables`、`notes`。这些都会进入 trace 和网页时间线，方便排查"模型看到了什么"。

prompt 版本号（[generator.py:149](../../agent/generator.py#L149)）自动拼出组合：`baseline-v2` / `baseline-v3-dynfs` + `+kb` + `+cur`。

### 3.7 LLM 客户端：可复现、可计费、可换厂商

**实现**：[llm.py](../../agent/llm.py)

| 能力 | 代码 | 说明 |
|---|---|---|
| 多厂商 | `PROVIDERS`（[:104](../../agent/llm.py#L104)） | 智谱 `glm-*`（`do_sample=False`）、DeepSeek `deepseek-flash`（关闭思考、temperature 0）；都是 OpenAI 兼容接口 |
| 选择模型 | `make_client`（[:209](../../agent/llm.py#L209)） | 配置 `llm.provider/model`，环境变量 `LLM_PROVIDER` / `LLM_MODEL` 可临时覆盖；复现旧运行时按模型名推断厂商 |
| 重试 | `complete`（[:172](../../agent/llm.py#L172)） | 408 / 429 / 5xx 指数退避重试（最多 5 次），同一个 prompt |
| key 检查 | `check_api_key`（[:130](../../agent/llm.py#L130)） | 拒绝含控制字符的 key（隐藏输入里按 Ctrl+V 会存进 `\x16`） |
| 预算 | `UsageMeter`（[:65](../../agent/llm.py#L65)） | 调用次数和 token 上限，超出抛 `LlmBudgetExceeded`；线程安全 |
| 缓存 | `CachingChatClient`（[:240](../../agent/llm.py#L240)） | 按 (模型, 参数, system, prompt) 的 SHA-256 指纹缓存到 `llm_cache.jsonl`，相同 prompt 直接重放，不计调用 |

**缓存的三个作用**：
1. 实验可复现：同一配置重跑，得到完全相同的回答；
2. 对比公平：不同配置下第 1 次尝试的 prompt 只要相同，就命中同一条缓存，差异只来自被改动的部分；
3. 外循环省钱：逐条门禁时，prompt 没变的开发集题直接重放（[05](05_外循环.md)）。

### 3.8 两个辅助模块

- [sql_analysis.py:51](../../agent/sql_analysis.py#L51) `sql_facts`：用 sqlglot 解析 SQL，按作用域还原别名，得到用到的表、(表, 列)、等值关联、常量、运算类型。它是中性模块，不含 Gold：诊断（运行时）和失败标注（评测）都用它。`alias_map`（[:93](../../agent/sql_analysis.py#L93)）只返回别名 → 表。
- [join_graph.py:29](../../agent/join_graph.py#L29) `join_candidates`：两张表共享、且像键的同名列（`*_CODE`、`*_KEY`、`*_ID`…）视为可关联。**只从 schema 推断，从不用 BEAVER 的 join_keys 标注**。修复技能 FindJoinPath、RetrieveAgain、SchemaSearch 用它提示关联方式。

---

## 4. 数据流转

**离线准备（一次）**

```
dw.information_schema.columns + dev_tables.json 样例值 ──▶ runs/phase1/schema_dw.json（SchemaCatalog）
训练集（dw 全部题 − 评测集 − 开发集）──▶ ExampleIndex（5,508 道）──▶ app/bundle/examples_pool.json.gz
                                  └──▶ WarehouseKnowledge.build ──▶ runs/knowledge/kb.json
外循环 + 审核 ──▶ experience.knowledge_items（active）
```

**运行时（每道题）**

```
AgentTask(case_id, question, db)
   │
   ├─▶ BM25TableRetriever.retrieve(question, k=20) ──▶ 20 张候选表 + 分数
   │
   └─▶ FewShotGenerator.generate(task, tables)
          ├─ ExampleIndex.top(question, 4) ──▶ 4 道相似题 + 它们的表（补进 schema ≤6 张）
          ├─ CuratedKnowledge.extra_tables ──▶ 补表提示的表（补进 schema）
          ├─ CuratedKnowledge.notes_for + WarehouseKnowledge.notes_for ──▶ 说明文本
          ├─ render_schema(tables) + build_prompt(...) ──▶ prompt（约 1.5 万 token）
          ├─ CachingChatClient.complete(prompt) ──▶ 命中缓存则重放，否则调 API 并记账
          └─ extract_sql(text) ──▶ Generation(sql, parse_status, prompt, example_ids, schema_tables, notes, llm 用量)
   │
   ▼
交给内循环执行（[04](04_内循环Loop.md)）
```

## 5. 讲解要点

- "错误分析显示瓶颈是选表：缺失的表 80% 其实已经检索到了。所以我没有去调检索，而是给模型补'同类问题用哪些表'的上下文。"
- "上下文分四层：固定规则、表结构、从 5,600 道已解题统计的使用说明、人工审核过的规则，外加 4 道相似题示例。每层都能单独开关，prompt 版本号记录组合。"
- "免费的 glm 只改上下文，开发集就从 0/30 提到 5/30；换 deepseek 加 Loop 到 8/30。"
- "LLM 调用全部走指纹缓存和预算，实验可复现、成本可控。"
