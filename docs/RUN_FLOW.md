# 项目运行流程（对照代码）

> 从一台新机器开始，把项目**完整跑一遍**要经过哪些阶段、每个阶段执行哪条命令、调用哪段代码、产出什么。
> 模块的设计原理见 [ARCHITECTURE.md](ARCHITECTURE.md)；Loop 内部每一步的逐行讲解见 [LOOP_WALKTHROUGH.md](LOOP_WALKTHROUGH.md)；每次运行的真实数据见 [EXECUTION_LOG.md](EXECUTION_LOG.md)。
> 所有命令在项目根目录执行，Python 用上一级目录的虚拟环境（`../.venv`）。

---

## 0. 全景

```
阶段 0  环境准备        .env · Databricks 登录 · HF 访问 · 本地 MySQL
阶段 1  数据底座        fetch_beaver_db → phase0.py all（导入 → 复制 → 兼容性 → 冻结 Gold → 报告）
阶段 2  开发集与 schema  phase1.py build-dev（开发集 30 题 + schema 缓存）
阶段 3  生成端基线      phase1.py dev / baseline（只生成一次，不修复）
阶段 4  失败分析        phase3.py label / intervene · analyze_run.py
阶段 5  诊断与修复评估   phase4.py · phase5.py
阶段 6  内循环 Loop     phase6.py（命令行）  或  网页"运行 Loop"
阶段 7  自检方法评估     verifier_eval · judge_eval · validator_eval
阶段 8  外循环知识      build_knowledge.py → 再跑阶段 3 / 6 对比
阶段 9  发布与观察      publish_run.py → Delta / MLflow → Loop Debug Console
阶段 10 部署平台        build_app_bundle.py → deploy_app.py → Databricks App
```

阶段 1–2 只做一次；日常迭代是 **3 → 4 → 8 → 3 / 6** 的循环，也就是外循环：衡量 → 找错因 → 补知识 → 再衡量。阶段 6、9、10 用来演示和观察。

**产物目录**

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `data/beaver/`、`data/beaver_db/` | BEAVER 题目和 MySQL dump（门控数据） | 否 |
| `runs/phase0/` | 数据底座各步的报告 | 否 |
| `runs/phase1/` | 开发集 `devset.json`、schema 缓存、Gold 缓存、LLM 缓存、基线运行目录 | 否 |
| `runs/phase3/`…`phase6/` | 失败标注、干预实验、诊断、修复、Loop 运行目录 | 否 |
| `runs/verifier_eval/`、`runs/analysis/`、`runs/knowledge/` | 自检评估、错误分析、外循环知识 `kb.json` | 否 |
| `app/bundle/` | 部署用的数据包 | 否 |

---

## 阶段 0：环境准备

| 事项 | 做法 | 为什么 |
|---|---|---|
| BEAVER 访问 | 在 HuggingFace 申请 `beaverbench/beaver-query`、`beaverbench/beaver-table` 的访问权限，然后执行 `hf auth login` | 门控数据集，必须本人申请，不能绕过条款 |
| Databricks | `databricks auth login`，profile 用 `DEFAULT`；准备一个 SQL Warehouse | 所有 SQL 在这里执行，结果写进 Delta |
| MySQL 8 | 本地安装，作为 BEAVER 官方口径的参照库 | 用来验证迁移到 Databricks 后答案是否还成立，也用来计算开发集的 Gold |
| `.env` | 参照 `.env.example`，填入 `MYSQL_PASSWORD`、`ZHIPUAI_API_KEY`，可选 `DEEPSEEK_API_KEY` | key 只放在这里；`.env` 在 `.gitignore` 里 |

各脚本启动时都会读取 `.env`，比如 [scripts/phase0.py:36](../scripts/phase0.py) `load_dotenv`。LLM 客户端从环境变量里取 key，并检查 key 里有没有不可见字符（[agent/llm.py:130](../agent/llm.py) `check_api_key`），这是之前踩过的坑。

---

## 阶段 1：数据底座（Phase 0）

```bash
python scripts/fetch_beaver_db.py          # 下载 beaver_db.zip，并打印导入 MySQL 的命令
python scripts/phase0.py all               # env → import → replicate → compat → gold → report
```

**调用链**：[scripts/phase0.py:195](../scripts/phase0.py) 按顺序执行 6 步：

```python
steps = ["env", "import", "replicate", "compat", "gold", "report"] if args.step == "all" else [args.step]
```

| 步骤 | 函数 | 做什么 | 产出 |
|---|---|---|---|
| env | `step_env`（[phase0.py:103](../scripts/phase0.py)） | 检查两个引擎连得上、会话设置正确，创建 catalog、schema 和 staging volume（[dbx/catalog.py:42](../dbx/catalog.py) `ensure_layout`） | `self_healing_text2sql` 下的 5 个 schema |
| import | `step_import` → [benchmark/beaver/phase0.py:47](../benchmark/beaver/phase0.py) `import_cases` | 按种子 77 抽取官方样本 100 题，题目和表元数据写入 Delta | `benchmark.cases`、`benchmark.tables_meta` |
| replicate | `step_replicate` → `replicate_databases`（[phase0.py:94](../benchmark/beaver/phase0.py)） | MySQL 97 张表按列映射类型（`_ci` → `UTF8_LCASE`），写 parquet → staging → Delta，并逐表核对行数和列画像 | `dw.*` 97 张表、`benchmark.replication_report` |
| compat | `step_compat` → `run_compatibility`（[phase0.py:158](../benchmark/beaver/phase0.py)） | 每道题的 Gold SQL 在两个引擎上各跑一次；不一致时，用适配规则改写（[benchmark/beaver/adapter.py:40](../benchmark/beaver/adapter.py) `RULES`）再比较（[evaluator.py:231](../benchmark/beaver/evaluator.py) `cross_engine_match`） | `benchmark.sql_compatibility`、`benchmark.gold_adaptations` |
| gold | `step_gold` → `build_gold_results`（[phase0.py:201](../benchmark/beaver/phase0.py)） | **新开一个会话**重新执行合格的 SQL，结果必须和兼容性运行时记录的哈希一致（检查漂移），然后冻结 | `benchmark.gold_results`（89 道 PRIMARY） |
| report | `step_report` | 汇总成报告 | `runs/phase0/*.json` |

**要点**：
- 原始 Gold SQL 永远不改，适配规则只作用于另存的"执行版本"。
- 冻结后，判分只读 `benchmark.gold_results`，之后不再需要 MySQL（开发集除外）。

---

## 阶段 2：开发集与 schema

```bash
python scripts/phase1.py build-dev
```

**代码**：[scripts/phase1.py:109-121](../scripts/phase1.py)

```python
exclude = eval_raw_ids | {e.source_id for e in examples}        # 排除评测集和固定示例
devset = build_devset(queries, exclude, int(cfg["dev"]["n_cases"]), int(cfg["dev"]["seed"]),
                      mysql, dbx, b["db"], int(cfg["dev"]["max_candidates"]))
save_devset(devset, dev_path)                                   # runs/phase1/devset.json
```

[evaluation/devset.py:67](../evaluation/devset.py) `build_devset`：按种子 20260926 从评测集以外抽题；Gold 在 MySQL 上实时计算；只保留在 Databricks 上能复现的题，凑够 30 题。

schema 缓存在第一次运行时生成：[phase1.py:68](../scripts/phase1.py) `load_catalog` 从 Databricks 读取表结构和样例值，存到 `runs/phase1/schema_dw.json`。之后所有阶段都读这个文件，这就是 Agent 能看到的全部 schema。

**为什么需要开发集**：所有调参只在开发集上做，评测集只在配置冻结后跑一次（决策 D2），否则等于在考题上调参。

---

## 阶段 3：生成端基线（只生成一次，不修复）

```bash
python scripts/phase1.py dev                                     # 开发集，固定示例（baseline-v2）
python scripts/phase1.py dev --few-shot dynamic                  # 相似题示例（baseline-v3-dynfs）
python scripts/phase1.py dev --few-shot dynamic --knowledge on   # + 数仓使用说明（+kb）
python scripts/phase1.py baseline                                # 评测集 89 题（冻结后只跑一次）
```

**装配**：[scripts/phase1.py:140-158](../scripts/phase1.py)

```python
catalog = load_catalog(dbx, cfg, tables_meta)
client = make_client(lc, max_output_tokens=..., meter=meter)          # 智谱 / DeepSeek，由配置决定
chat = client if args.no_cache else CachingChatClient(client, Path(lc["cache"]))
index = build_generator_index(queries, eval_raw_ids, dev_ids, fs)     # 相似题示例库（dynamic 时才建）
generator = FewShotGenerator(chat, catalog, examples, index=index, ..., knowledge=knowledge_for(cfg, ROOT))
run_id, summary = run_baseline(selected, judges, BM25TableRetriever(catalog), generator, dbx, ...)
```

**每道题**：[evaluation/baseline.py:52](../evaluation/baseline.py) `run_case`

```python
task = case.agent_view()                       # ← Agent 只看到这些：编号、问题、库名
retrieval = retriever.retrieve(task.question, cfg.top_k)
gen = generator.generate(task, retrieval.tables)
ex = executor.execute(gen.sql, task.db, max_rows=cfg.max_result_rows)
correct, message = judge(rows)                 # 执行完才判分
# 之后计算的表召回率等指标，只用于评测，Agent 看不到
```

**产出**：`runs/phase1/baseline-<时间>-<id>/`，包含 `run_meta.json`（模型、prompt 版本、配置）、`results.jsonl`（每题一行）、`summary.json`（准确率、可执行率、报错分类、token）。

**真实结果**（开发集，glm-4-flash）：固定示例 0/30 → 相似题示例 3/30 → 再加使用说明 5/30。

---

## 阶段 4：失败分析

```bash
python scripts/phase3.py label runs/phase1/<run_id>                    # 失败标注：每道错题的主因
python scripts/phase3.py intervene runs/phase1/<run_id> --variants all # 干预实验：给全部 Gold 提示能否修好
python scripts/analyze_run.py <已发布的 run_id>                        # 逐题对照 Gold（适用于 Loop 和网页运行）
```

| 命令 | 代码 | 做什么 | 用途 |
|---|---|---|---|
| label | [scripts/phase3.py:60](../scripts/phase3.py) `label_run` → [benchmark/beaver/subtasks.py:90](../benchmark/beaver/subtasks.py) `label_failure` | 把生成的 SQL 和 Gold SQL 解析成语法树，逐项比较用表、列、关联、字面值、运算，按优先级给出主因 | 诊断准确率的参照标签 |
| intervene | [phase3.py:124](../scripts/phase3.py) `intervene` → [evaluation/intervention.py:86](../evaluation/intervention.py) | 每次补一类 Gold 提示后重新生成、判分 | 区分"缺信息"还是"模型能力不够"（只作上界，glm 0/30，deepseek 7/27） |
| analyze_run | [scripts/analyze_run.py](../scripts/analyze_run.py) | 从 Delta 拉取一次运行的最终答案，重新执行，和 Gold 对比行数、列数、主因 | 决定外循环下一步补哪类知识（例如"选错表占 38%"） |

**界限**：分析结果只用于**决定改进方向**，不能直接交给 Loop 修对应的题；那等于看答案改考卷，原因见 [ARCHITECTURE.md §8](ARCHITECTURE.md)。

---

## 阶段 5：诊断与修复的离线评估

```bash
python scripts/phase4.py runs/phase1/<run_id> [--llm] [--publish]   # 诊断准确率
python scripts/phase5.py runs/phase1/<run_id>                       # 每个修复技能单独跑一遍
```

- [scripts/phase4.py:77](../scripts/phase4.py) 对基线的每道错题运行 `Diagnoser`，[evaluation/diagnosis_eval.py:18](../evaluation/diagnosis_eval.py) `score` 对照阶段 4 的标签，计算严格准确率和宽松准确率（规则诊断宽松 76.5%）。
- [scripts/phase5.py:82](../scripts/phase5.py) 每题走一次"观察 → 诊断 → 路由 → 技能修复 → 执行"，按技能统计修复后能执行、答对的数量。

这两步是在把内循环组装起来之前，先分别验证诊断和修复两个部件。

---

## 阶段 6：内循环 Loop

两个入口，**执行路径相同**，都是 `run_arm` → `LoopController.run`。

**入口 A：命令行**

```bash
python scripts/phase6.py --verifier self --max-repairs 2 --few-shot dynamic --knowledge on
python scripts/phase6.py --verifier oracle        # 上界：Oracle 自检
```

[scripts/phase6.py:90-111](../scripts/phase6.py) 装配生成器、控制器、自检器，然后调用 `run_arm`。

**入口 B：网页"运行 Loop"页**

```
点"执行" → app/app_pages/run_loop.py:112   runner.start_run(req, B, data.connect)
        → app/runner.py:260 start_run       启动后台线程
        → app/runner.py:190 _execute        装配（同入口 A）→ run_arm(..., on_event=live.push)
        → 页面每秒刷新（run_loop.py:430 st.fragment(run_every=1)）显示事件
        → 结束：生成分析报告（app/report.py:142 build_report）→ 保存 report.md → 自动发布到 Delta
```

**共同的执行路径**：

```
evaluation/loop_run.py:35  run_arm            逐题；Loop 只拿到题面
  └ loop_engineer/controller.py:107  run      检索 → 生成 → [执行 → 自检 → 观察 → 诊断 → 路由 → 修复] × N → 选最终答案
  └ loop_run.py:52  judge(rows)               Loop 结束后才用 Gold 判分
evaluation/loop_run.py:76  summarize          恢复率、误伤率、自检混淆矩阵、单技能统计
```

每一步的代码和数据见 **[LOOP_WALKTHROUGH.md](LOOP_WALKTHROUGH.md)** Step 0–15。

**产出**：`runs/phase6/<arm>-<时间>-<id>/`，网页运行放在 App 的临时目录，并已发布到 Delta。

---

## 阶段 7：自检方法的离线评估

任何想加进自检的方法，先在这里测"抓到多少错题、误伤多少正确答案"，全部是离线评估：

```bash
python scripts/verifier_eval.py --reexecute --mysql-pool 400   # 题干规则 + 数值一致性（不调用 LLM）
python scripts/judge_eval.py --model glm-4-flash               # LLM 裁判（63 次调用）
python scripts/validator_eval.py --pool 300                    # 查数据库的验证器（不调用 LLM）
```

| 脚本 | 被测的方法 | 错题样本 | 正确样本 |
|---|---|---|---|
| [verifier_eval.py](../scripts/verifier_eval.py) | [loop_engineer/checks.py](../loop_engineer/checks.py) 的静态规则、结果检查、数值检查 | 已有运行中 30 个"能执行但答错"的尝试 | 开发集 Gold + 5,687 道 Gold SQL（静态检查）+ 399 道在 MySQL 上执行的 Gold（数值检查） |
| [judge_eval.py](../scripts/judge_eval.py) | [loop_engineer/judge.py](../loop_engineer/judge.py) LLM 裁判 | 同上 | 开发集 Gold + 模型答对的尝试 |
| [validator_eval.py](../scripts/validator_eval.py) | [loop_engineer/validators.py](../loop_engineer/validators.py) 查数据库的验证器 | 同上 | 同上 + 300 道 Gold |

**决策规则**：误报接近 0 → 加入 `TRIGGER_SIGNALS`，可以触发修复（[checks.py:46-48](../loop_engineer/checks.py)）；误报高 → 只作提示或不用。四轮的结果见 [ARCHITECTURE.md §8](ARCHITECTURE.md)。

---

## 阶段 8：外循环知识

```bash
python scripts/build_knowledge.py                                 # 约 1–2 分钟，不调用 LLM
python scripts/phase1.py dev --few-shot dynamic --knowledge on    # 回到开发集衡量
```

**代码**：[scripts/build_knowledge.py](../scripts/build_knowledge.py) → [agent/knowledge.py](../agent/knowledge.py) `WarehouseKnowledge.build`

```python
eval_ids = {...评测集...}; dev_ids = {...开发集...}
kb = WarehouseKnowledge.build(queries, eval_ids | dev_ids, catalog)   # 只用训练集
out.write_text(kb.to_json())                                          # runs/knowledge/kb.json
```

**统计的三类知识**：
- 题干词 → 已解题常用的表；
- 相似表组，以及同类问题在组内的使用比例；
- 每对表常用的关联键和 INNER / LEFT 比例。

**使用**：生成时 `FewShotGenerator` 调用 `knowledge.notes_for(question, tables)`，取出本题相关的 7–13 行（多数 12–13 行，见疑问与价值·疑问 13），放在 prompt 的 schema 后面（[agent/generator.py](../agent/generator.py) `build_prompt` 的 `notes` 参数）。开关：配置 `knowledge.mode`、命令行 `--knowledge on`、网页"数仓使用说明"。

相似题示例库不需要单独构建：用 `--few-shot dynamic` 时，[agent/examples.py](../agent/examples.py) `build_generator_index` 会在运行时从训练集现场建立；网页使用 `app/bundle/examples_pool.json.gz`。

**一次外循环迭代（手工版）**：阶段 3 或 6 跑开发集 → 阶段 4 用 `analyze_run` 分类错因 → 在训练集上归纳对应的知识 → 回到阶段 3 或 6，对比同一批题的变化 → 写进 EXECUTION_LOG。

### 阶段 8b：外循环自动迭代 + 人工审核（产品化）

```bash
python scripts/outer_loop.py --train-n 6 --val-n 0 --dry-run   # 试跑：开发集门禁，不写 Delta
python scripts/outer_loop.py --train-n 200 --val-n 200          # 一次迭代：验证集门禁，提案写入 experience.proposals
```

**代码**：[scripts/outer_loop.py](../scripts/outer_loop.py) `main` → [evaluation/outer_loop.py](../evaluation/outer_loop.py)

```python
current = load_curated(dbx, layout)                       # 已批准、生效中的知识
sample = ol.sample_training(queries, eval_ids | dev_ids, n, seed)          # ① 训练集抽题
judge = ol.gold_judge_from_sql(dbx, q["sql"], db, max_rows)                # 在 Databricks 上执行 Gold
train = ol.run_and_judge(cases, judges, retriever, generator(...), ...)    # ② 生成 + 判分 + 错因标注
candidates = ol.mine_table_preferences(failures, kb, catalog) \
           + ol.mine_missing_tables(failures, kb) \
           + ol.mine_join_rules(failures, kb) \
           + ol.mine_verified_queries(experience.list_feedback(...), ...)  # ③ 候选（含用户反馈）
candidates, dropped = ol.resolve_conflicts(candidates)                     #     方向相反的选表偏好
val = ol.sample_training(queries, eval | dev | 挖掘样本, val_n, val_seed)     # ④ 验证集（与挖掘样本不重叠）
before = ol.run_and_judge(val, ..., generator(...))                        #     验证集：当前系统
run_with = lambda extra: ol.run_and_judge(val, ..., generator(..., extra), reuse=before_by_case)
per_item, combined = ol.gate_candidates(candidates, before, run_with)
dev_check = ol.compare(dev_before, run(dev, 当前系统 + 建议批准的条目))        #     开发集复核
#   每条单独回归：答对不减少、零误伤、token 增幅 ≤ 20%；prompt 没变的题复用结果（不调 LLM、不执行）
#   单条建议批准的 ≥ 2 条时，合在一起再回归一次（知识之间可能互相影响）
experience.save_proposals(dbx, layout, ol.proposal_rows(batch_id, candidates, regression))  # ⑤ pending
```

- 训练题生成时，相似题示例库会排除这些训练题本身，避免"拿自己的答案当示例"。
- 候选至少要有 `--min-support 2` 道错题支持；已验证查询来自 👍 的答案或能执行的修正 SQL。
- 本地同时保存 `runs/outer_loop/<batch_id>/proposals.json`。

**审核与上线**：网页「审核」页（[app/app_pages/review.py](../app/app_pages/review.py)）→ `experience.review_proposal`：批准时把提案状态改为 approved，并插入一条 `experience.knowledge_items`（active）。提问页和运行页在每次运行时调用 `load_curated` + `attach_curated`（[agent/curated.py](../agent/curated.py)），所以**下一次提问就生效**；「已生效知识」里点停用 → `deactivate_item`，下一次提问即不再使用。

**用户提问**：网页「提问」页（[app/app_pages/ask.py](../app/app_pages/ask.py)）→ [app/runner.py](../app/runner.py) `start_ask` → 后台线程 `_ask`：

```python
controller, generator = build_controller(bundle, inner, conn, "targeted", max_repairs, few_shot, knowledge, curated)
res = controller.run(AgentTask(case_id=query_id, question=question, db="dw"), SelfVerifier(), on_event=ask.push)
experience.save_user_query(conn, layout, {...})          # experience.user_queries
# 用户点 👍 / 👎 后：submit_feedback → 修正 SQL 先只读执行 → experience.save_feedback
```

---

## 阶段 9：发布与观察

```bash
python scripts/publish_run.py runs/phase6/<run_id>             # 命令行运行需要手动发布；网页运行自动发布
python scripts/publish_run.py runs/phase3/intervention-<run_id> # 干预实验只发布汇总
```

**代码**：[dbx/publish.py:179](../dbx/publish.py) `publish_run`

```
load_run → flatten_loop_records（每题每次尝试一行，dbx/publish.py:35）
→ log_mlflow（参数和指标）→ 先删除同一 run_id 的旧行 → write_rows 写入
   traces.execution_traces  （Agent 可见的过程）
   evaluation.evaluation_results（判分）
   evaluation.runs         （汇总和元数据）
```

**本地查看**：

```bash
streamlit run app/streamlit_app.py --server.port 8502
```

也可以用预览配置 `loop-console`。页面入口是 [app/streamlit_app.py](../app/streamlit_app.py)，有四页：
- **提问**（默认页）和 **审核**：见阶段 8b；
- **Loop Debug Console**（[app/dashboard.py](../app/dashboard.py)，数据来自 [app/data.py](../app/data.py) 的 Delta 查询）：总览、对照实验、消融、逐题追踪、失败与诊断；
- **运行 Loop**：见阶段 6 入口 B。

---

## 阶段 10：部署到 Databricks App

```bash
python scripts/build_app_bundle.py          # 生成 app/bundle/
python scripts/deploy_app.py                # 首次：secret + 上传 + 创建 App + 授权 + 部署
python scripts/deploy_app.py --skip-secrets # 之后改了代码：只上传和部署
```

**数据包** [scripts/build_app_bundle.py](../scripts/build_app_bundle.py)：`schema_dw.json`、`few_shot.json`、`devset.json`、`examples_pool.json.gz`（相似题示例库）、`kb.json`（使用说明）、`llm_cache.jsonl`、`config.json`。其中包含 BEAVER 的内容，所以已加入 `.gitignore`，只上传到自己的工作区。

**部署** [scripts/deploy_app.py:63](../scripts/deploy_app.py) `main` 分 5 步，可以重复执行：

| 步 | 代码 | 做什么 |
|---|---|---|
| 1 secret | [:80](../scripts/deploy_app.py) | 创建 scope `self-healing-text2sql`，从 `.env` 写入智谱和 DeepSeek 的 key（不打印内容） |
| 2 上传 | [:97](../scripts/deploy_app.py) | 只上传运行需要的代码目录、数据包、`app.yaml`、`requirements.txt`；不上传 `tests/`、`runs/`、`.env` |
| 3 App | [:106](../scripts/deploy_app.py) | 创建或更新 App，绑定资源：SQL Warehouse（CAN_USE）、两个 secret（READ） |
| 4 授权 | [:129](../scripts/deploy_app.py) | 给 App 的服务主体授予项目 catalog 的读写权限（包括 staging volume） |
| 5 部署 | 末尾 | `deploy_and_wait`，打印访问地址 |

**运行时**：[app.yaml](../app.yaml) 用 `streamlit run app/streamlit_app.py` 启动，环境变量从 secret 和 warehouse 资源注入；`SHT_ALLOW_EVAL=0` 表示评测集在网页上锁定。

**Free Edition 注意**：App 和 Warehouse 可能因为空闲或配额被平台停掉，页面会打不开。恢复方法：

```bash
databricks apps start self-healing-text2sql -p DEFAULT
```

```bash
databricks warehouses start 0e96e1d4ab3f34b0 -p DEFAULT
```

---

## 附录：常用命令速查

| 目的 | 命令 |
|---|---|
| 跑单元测试 | `python -m pytest -q` |
| 开发集生成端对比 | `python scripts/phase1.py dev --few-shot dynamic --knowledge on` |
| 外循环一次迭代（写入待审核提案） | `python scripts/outer_loop.py --train-n 200 --val-n 200` |
| 开发集 Loop（免费模型、前 3 题试跑） | `python scripts/phase6.py --max-repairs 2 --few-shot dynamic --limit 3` |
| 临时切换到 DeepSeek（花费几分钱到几毛钱） | 命令前加 `LLM_PROVIDER=deepseek` |
| 分析一次运行的错因 | `python scripts/analyze_run.py <run_id>` |
| 重建外循环知识 | `python scripts/build_knowledge.py` |
| 发布到 Delta / MLflow | `python scripts/publish_run.py runs/phase6/<run_id>` |
| 本地看板 | `streamlit run app/streamlit_app.py --server.port 8502` |
| 更新线上 App | `python scripts/build_app_bundle.py`，然后 `python scripts/deploy_app.py --skip-secrets` |
