# Loop 逐步讲解（对照源码）

> 从一次运行的入口开始，按**代码的调用层级**讲完一道题在 Loop 里的全过程。标题层级与调用层级一致：同一层级的代码段用同一级标题。
>
> 每个代码段按同样的顺序讲：
> 1. **功能**：这个函数具体完成什么事；
> 2. **调用位置**：它被哪个文件、哪一行、哪句代码调用；
> 3. **函数定义**：签名，以及输入、输出的名称、类型和含义；
> 4. **内部实现**：源码和逐行说明；内部再调用多个函数时，往下一级标题继续拆；
> 5. **真实数据**：真实运行里的输入输出；
> 6. **设计原因**：为什么这样做。
>
> 整体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，类关系与数据流图见 [LOOP_ARCHITECTURE.md](LOOP_ARCHITECTURE.md)，设计取舍见 [LOOP_DESIGN.md](LOOP_DESIGN.md)，模型每次看到什么见 [CONTEXT_DESIGN.md](CONTEXT_DESIGN.md)，实验数据见 [EXECUTION_LOG.md](EXECUTION_LOG.md)。
>
> 代码行号以 2026-10-02 的代码为准。示例数据来自两次真实运行：
> - `runs/phase6/targeted-self-20260925T105746-eb2ade`：开发集 30 题，glm-4-flash，固定示例，自检 v1；
> - `console-targeted-self-20260928T105340-eef1a9`：在线平台运行，开发集 30 题，deepseek-flash，自检 v2，最多修复 4 次。

---

<a id="s0"></a>

## 0. 全景

<a id="s0-1"></a>

### 0.1 调用树

括号里是本文对应的章节：

```
入口 A：命令行                                         入口 B：网页平台
scripts/phase6.py  main()                (1.1)         app/app_pages/run_loop.py  点"执行"
                                                       └ app/runner.py  start_run → 后台线程 → _execute   (1.2)
        └────────────────────┬───────────────────────────────┘
evaluation/loop_run.py  run_arm()                       (2)  逐题循环
├─ case.agent_view()                                    (2.1) 只把题面交给 Loop
├─ LoopController.run()                                 (3)  单题 Loop
│   ├─ retriever.retrieve()                             (3.1) 检索
│   ├─ generator.generate()                             (3.2) 生成第 1 次 SQL
│   ├─ for n in 1..max_attempts:                        (3.3) 循环体，max_attempts = 最大修复次数 + 1
│   │   ├─ _execute()                                   (3.3.1) 执行
│   │   ├─ verifier.verify()                            (3.3.2) 自检
│   │   ├─ 通过 或 次数用完 → break                       (3.3.3) 终止判断
│   │   ├─ observe()                                    (3.3.4) 观察
│   │   ├─ diagnoser.diagnose()                         (3.3.5) 诊断
│   │   ├─ policy.route()                               (3.3.6) 路由
│   │   ├─ skill.repair()                               (3.3.7) 修复
│   │   └─ 写回诊断，组装下一次尝试                         (3.3.8) 状态交接
│   └─ 选出最终答案                                      (3.4)
├─ judge(rows)：Loop 结束后才用 Gold 判分               (2.3)
└─ summarize()                                          (2.4) 指标汇总
发布到 Delta / MLflow → Console 回放；网页另有实时 trace 和分析报告   (4)
```

**跳转**：[1.1 命令行](#s1-1)　[1.2 网页](#s1-2)　[第 2 节 逐题循环](#s2)　[2.1 交出题面](#s2-1)　[第 3 节 单题 Loop](#s3)　[3.1 检索](#s3-1)　[3.2 生成](#s3-2)　[3.3 循环体](#s3-3)　[3.4 选出最终答案](#s3-4)　[2.3 判分](#s2-3)　[2.4 指标汇总](#s2-4)　[第 4 节 发布](#s4)

<a id="s0-2"></a>

### 0.2 贯穿全程的尝试记录

一个字典 `attempt` 贯穿全程。每一步往里加字段，最后整个字典就是这次尝试的 trace：

| 写入步骤 | 写入 `attempt` 的字段 |
|---|---|
| [3.2](#s3-2) 生成 | `case_id, attempt_id, question, retrieved_tables, generated_sql, parse_status, strategy, input_tokens, output_tokens, llm_latency_ms, used_llm, diag_tokens` |
| [3.3.1](#s3-3-1) 执行 | `execution_status, execution_error, result_row_count, result_preview, exec_latency_ms` |
| [3.3.2](#s3-3-2) 自检 | `verifier_mode, verifier_decision, verifier_signals, verifier_findings, verifier_advisories` |
| [3.3.8](#s3-3-8) 写回（失败的那次） | `failure_type, diagnosis_confidence, diagnosis_reason, diagnosis_source, repair_hints, repaired_sql, repair_skill` |
| [3.3.8](#s3-3-8) 组装（新的一次） | `repair_skill, repair_action, repair_reason, repair_fallback, tables`，以及新的 SQL 和 token |
| [3.4](#s3-4) 选最终答案 | `final_status`（FINAL / SUPERSEDED） |

**表里没有任何"对不对"的字段**。对错在 [2.3](#s2-3) 才计算，并且存在另一个地方。

<a id="s0-3"></a>

### 0.3 实时事件

`run(..., on_event=cb)` 在每一步调用 `cb(step, payload)`。事件包括 retrieve、generate、execute、verify、observe、diagnose、route、repair、final，网页的实时时间线和分析报告都来自这些事件。事件只做通知，不改变 Loop 的行为；回调出错也不会中断 Loop（[controller.py:108](../loop_engineer/controller.py#L108) `emit`）。

---

<a id="s1"></a>

## 1. 入口与装配：把所有部件接起来

<a id="s1-1"></a>

### 1.1 命令行：`scripts/phase6.py` `main`

**功能**：命令行入口。读取命令行参数和配置，加载题目、表结构和示例，创建 LLM 客户端和数据库连接，把检索器、生成器、诊断器、路由策略、修复上下文组装成一个 `LoopController`，再交给 `run_arm` 逐题运行；结果写入运行目录，并在终端打印汇总。

**调用位置**

在命令行直接运行：

```bash
python scripts/phase6.py --verifier self --max-repairs 2 --few-shot dynamic --limit 3
```

**函数定义**

```python
def main() -> None:      # scripts/phase6.py:42
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | — | — | 没有函数参数；运行设置来自命令行参数和 `config/phase1.yaml` |
| 输出 | — | `None` | 没有返回值；结果写入 `runs/phase6/<run_id>/`，并在终端打印汇总 |

**内部实现**

读配置，加载题目、schema、示例；连接 Databricks；创建 LLM 客户端；把各部件注入 `LoopController`，再交给 `run_arm` 逐题运行（[scripts/phase6.py:93](../scripts/phase6.py#L93) 起）：

```python
meter = UsageMeter(max_calls=int(lc["max_calls"]), max_tokens=int(lc["max_tokens"]))    # 预算上限
inner = make_client(lc, max_output_tokens=int(lc["max_output_tokens"]), meter=meter)    # 智谱 / DeepSeek
client = CachingChatClient(inner, Path(lc["cache"]))                                    # prompt 指纹缓存
policy = Policy(mode=args.policy, disabled={s for s in args.disable.split(",") if s})   # 可禁用单个技能（消融）
generator = FewShotGenerator(client, catalog, examples,
                             index=build_generator_index(queries, eval_ids, dev_ids, fs),  # 相似题示例（可选）
                             k=int(fs.get("dynamic_k", 4)), max_extra_tables=int(fs.get("dynamic_max_extra_tables", 6)),
                             knowledge=knowledge_for(cfg, ROOT))                           # 数仓使用说明（可选）
controller = LoopController(BM25TableRetriever(catalog), generator, dbx,
                            Diagnoser(catalog, client), policy, RepairContext(catalog, client, examples),
                            LoopConfig(strategy=args.strategy, max_attempts=args.max_repairs + 1,
                                       top_k=int(cfg["retrieval"]["top_k"]),
                                       max_result_rows=int(d["max_result_rows"])))
verifier_for = (lambda cid: SelfVerifier()) if args.verifier == "self" else (lambda cid: OracleVerifier(judges[cid]))
run_id, summary = run_arm(cases, judges, controller, verifier_for, arm, Path("runs/phase6"), meta)   # → 第 2 节 逐题循环
```

命令行参数（[phase6.py:44](../scripts/phase6.py#L44) 起）：

| 参数 | 取值 | 含义 |
|---|---|---|
| `--verifier` | `self` / `oracle` | 无 Gold 自检；Oracle 用 Gold，只作上界 |
| `--max-repairs` | 默认 1 | 每题最多修复几轮，SQL 尝试次数 = 修复次数 + 1 |
| `--few-shot` | `static` / `dynamic` | 固定 3 个示例，或按问题检索最相似的已解题 |
| `--knowledge` | `off` / `on` | 是否加入数仓使用说明 |
| `--disable` | 技能名，逗号分隔 | 消融：禁用某个技能，退回 RepairSQL |
| `--split` / `--eval` | 默认 dev | 评测集需要 `--eval` 确认（决策 D2） |
| `--limit` | 整数 | 只跑前 N 题（试跑） |

`LoopConfig` 的默认值见 [controller.py:48](../loop_engineer/controller.py#L48)：`top_k=20`，结果超过 50 万行判为"结果过大"。

**设计原因**

所有部件通过构造函数注入。换模型、换自检、换示例方式、禁用技能，都只改参数不改代码；命令行和网页走同一条路径，从哪个入口跑都是同一个实验。

<a id="s1-2"></a>

### 1.2 网页：`app/runner.py`

**功能**：网页入口。用户在"运行 Loop"页选好题目和设置后点"执行"，`app/runner.py` 在后台跑完与命令行相同的流程，并把每一步的事件实时推给页面。由三个函数配合完成：`start_run` 启动后台线程，`_execute` 设置预算并运行逐题循环，`build_controller` 组装控制器。

**调用位置**

网页"运行 Loop"页点击"执行"后，[app/app_pages/run_loop.py:117](../app/app_pages/run_loop.py#L117)：

```python
st.session_state["live"] = runner.start_run(req, B, data.connect)
```

**内部实现**

<a id="s1-2-1"></a>

#### 1.2.1 `start_run`：在后台线程里运行

**功能**：接收网页提交的运行设置，创建记录实时状态的 `LiveRun` 对象，在后台线程里启动本次运行，然后立即返回。页面因此不会卡住，可以一边运行一边刷新进度。

**调用位置**：[app/app_pages/run_loop.py:117](../app/app_pages/run_loop.py#L117)，网页上点击"执行"后：`runner.start_run(req, B, data.connect)`。

**函数定义**

```python
def start_run(request: RunRequest, bundle: Bundle, connect) -> LiveRun:      # app/runner.py:289
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `request` | `RunRequest` | 一次运行的设置：`split`（dev / eval）、`case_ids`、`model`、`strategy`、`verifier`、`max_repairs`、`few_shot`、`knowledge`、`curated`、`publish` |
| 输入 | `bundle` | `Bundle` | 应用数据包：表结构目录、固定示例、开发集、配置、相似题示例库路径 |
| 输入 | `connect` | 无参函数，返回 `DatabricksSqlExecutor` | 建立数据库连接；每个后台线程各自建一个连接 |
| 输出 | — | `LiveRun` | 本次运行的实时状态：`events`（事件列表）、`status`（running / done / error）、`run_id`、`summary`、`report` 等；页面通过 `snapshot()` 读取事件 |


**内部实现**

[app/runner.py:289](../app/runner.py#L289)：校验参数，创建 `LiveRun`（内含事件列表），启动后台线程运行 `_execute`（[:300](../app/runner.py#L300)），然后立即返回。

```python
live = LiveRun(request)
threading.Thread(target=_execute, args=(live, bundle, connect), daemon=True, name="loop-run").start()
```

页面在运行期间每秒重绘一次，读取 `LiveRun` 的事件列表（[run_loop.py:301](../app/app_pages/run_loop.py#L301)）：

```python
st.fragment(live_panel, run_every=1 if running else None)()
```

**设计原因**：一道题要跑 30–60 秒。放到后台线程，页面不会卡住，能边跑边看。

<a id="s1-2-2"></a>

#### 1.2.2 `_execute`：设置调用预算，运行逐题循环

**功能**：后台线程真正执行的函数。加载本次要跑的题和判定函数，按题数和修复次数算出 LLM 调用上限，组装控制器，调用 `run_arm` 逐题运行并把事件推给页面；运行结束后生成分析报告，按设置发布到 Delta，最后把结果和状态（完成 / 出错）写回 `LiveRun`。

**调用位置**：[app/runner.py:300](../app/runner.py#L300)，由 `start_run` 启动的后台线程运行：`threading.Thread(target=_execute, args=(live, bundle, connect), ...)`。

**函数定义**

```python
def _execute(live: LiveRun, bundle: Bundle, connect) -> None:      # app/runner.py:234
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `live` | `LiveRun` | 本次运行的状态对象；运行中把事件写进去（`live.push`） |
| 输入 | `bundle` | `Bundle` | 同 1.2.1 |
| 输入 | `connect` | 无参函数 | 同 1.2.1 |
| 输出 | — | `None` | 没有返回值；结果写回 `live`：`run_id`、`summary`、`report`、`status` |


**内部实现**

[app/runner.py:234](../app/runner.py#L234)：

```python
calls = (1 + 2 * r) * n + 4                                   # :249，r = 最大修复次数，n = 题数
meter = UsageMeter(max_calls=calls, max_tokens=30_000 * calls)
controller, generator = build_controller(bundle, inner, conn, req.strategy, req.max_repairs, ...)   # :252 → 1.2.3 组装
run_id, summary = run_arm(cases, judges, controller, verifier_for, arm, _out_root(), meta,
                          on_event=live.push)                 # :264 → 第 2 节 逐题循环
```

调用上限的含义：每题 1 次生成 + 每轮修复最多 2 次 LLM 调用（LLM 诊断 + 技能修复），再留 4 次余量。它是安全阀，正常运行碰不到；命中缓存的调用不计入。

<a id="s1-2-3"></a>

#### 1.2.3 `build_controller`：组装

**功能**：按运行设置把各部件组装成 `LoopController`：给 LLM 客户端加缓存层，按示例方式创建生成器，按开关加载数仓使用说明和审核通过的知识，再把检索器、执行器、诊断器、路由策略和修复上下文注入控制器。运行页和提问页共用它，保证两处的 Loop 完全一样。

**调用位置**：[app/runner.py:252](../app/runner.py#L252)，在 `_execute` 里；提问页的 `_ask` 也调用它（[app/runner.py:366](../app/runner.py#L366)）。

**函数定义**

```python
def build_controller(bundle: Bundle, inner: Any, conn: Any, strategy: str, max_repairs: int,
                     few_shot: str, knowledge: bool, curated: bool):      # app/runner.py:193
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `bundle` | `Bundle` | 提供表结构目录、固定示例、配置、示例库 |
| 输入 | `inner` | `OpenAICompatibleChatClient` | 未加缓存的 LLM 客户端；函数内部再包一层 `CachingChatClient` |
| 输入 | `conn` | `DatabricksSqlExecutor` | 执行 SQL；开启审核知识时也用它读 `experience.knowledge_items` |
| 输入 | `strategy` | `str` | 固定为 `"targeted"` |
| 输入 | `max_repairs` | `int` | 最大修复次数；`max_attempts = max_repairs + 1` |
| 输入 | `few_shot` | `str` | `"static"` 固定示例 / `"dynamic"` 相似题示例 |
| 输入 | `knowledge` | `bool` | 是否加载数仓使用说明（`kb.json`） |
| 输入 | `curated` | `bool` | 是否加载审核通过的知识 |
| 输出 | — | `tuple[LoopController, FewShotGenerator]` | 组装好的控制器，以及其中的生成器（调用方用它取 `prompt_version`）；代码里没有标注返回类型 |


**内部实现**

[app/runner.py:193](../app/runner.py#L193)，写法与命令行相同。网页"运行 Loop"的 `_execute` 和"提问"页的 `_ask` 都调用它；网页端还会加载审核通过的知识（`load_curated` + `attach_curated`）。

---

<a id="s2"></a>

## 2. 逐题循环：`run_arm`

**功能**：对一批题目逐题运行 Loop 并判分。每道题只把题面交给 Loop，Loop 返回之后再用 Gold 判断每次尝试是否答对，写成一行记录；全部跑完后计算恢复率、误伤率等指标，生成运行编号和汇总，写入运行目录。

**调用位置**

- 命令行：[scripts/phase6.py:121](../scripts/phase6.py#L121)；
- 网页：[app/runner.py:264](../app/runner.py#L264) `_execute` 里。

```python
run_id, summary = run_arm(cases, judges, controller, verifier_for, arm, out_root, meta, on_event=...)
```

**函数定义**

```python
def run_arm(cases: list[BeaverCase], judges: dict[str, Callable], controller: LoopController,
            verifier_for: Callable[[str], Any], arm: str, out_root: Path, meta: dict,
            on_event: Callable[[str, str, dict], None] | None = None) -> tuple[str, dict]:      # evaluation/loop_run.py:36
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `cases` | `list[BeaverCase]` | 要跑的题；含 Gold，但只在本函数里用于判分，交给 Loop 的只有题面 |
| 输入 | `judges` | `dict[str, Callable]` | 题号 → 判定函数（见 2.3） |
| 输入 | `controller` | `LoopController` | 组装好的控制器 |
| 输入 | `verifier_for` | `Callable[[str], Any]` | 题号 → 自检器（`SelfVerifier` 或 `OracleVerifier`） |
| 输入 | `arm` | `str` | 运行名前缀，如 `targeted-self` |
| 输入 | `out_root` | `Path` | 运行目录的上级目录 |
| 输入 | `meta` | `dict` | 运行元数据：模型、prompt 版本、自检版本、开关等，写进 `run_meta.json` |
| 输入 | `on_event` | `Callable[[str, str, dict], None] \| None` | 事件回调 `(题号, 步骤, 内容)`；网页传 `live.push`，命令行不传 |
| 输出 | — | `tuple[str, dict]` | `(run_id, summary)`：运行编号和指标汇总；逐题结果写入 `results.jsonl` |

**内部实现**

[evaluation/loop_run.py:36](../evaluation/loop_run.py#L36)。每道题依次做：交出题面 → 运行单题 Loop → 判分 → 写一行结果；全部跑完后汇总指标。

```python
for i, case in enumerate(cases, 1):                                                  # :48
    cb = (lambda step, payload, cid=case.case_id: on_event(cid, step, payload)) if on_event else None
    res = controller.run(case.agent_view(), verifier_for(case.case_id), on_event=cb)   # :53 → 2.1 交出题面、2.2 运行单题 Loop
    judge = judges[case.case_id]                                                     # :54 → 2.3 判分
    correct = [judge(rows)[0] if a["execution_status"] == "SUCCESS" else False
               for a, rows in zip(res.attempts, res.rows)]
    ...
    on_event(case.case_id, "judged", {...})                                          # 判分事件来自评测侧
summary = {"run_id": run_id, "arm": arm, **summarize(records)}                       # → 2.4 指标汇总
```

**跳转**：[2.1 交出题面](#s2-1)　[2.2 运行单题 Loop](#s2-2)　[2.3 判分](#s2-3)　[2.4 指标汇总](#s2-4)

<a id="s2-1"></a>

### 2.1 只把题面交给 Loop：`agent_view`

**功能**：把一道完整的题目记录（含 Gold SQL 和各种标注）裁剪成只有题号、问题、库名的 `AgentTask`。这是 Loop 唯一能拿到的输入，从源头保证 Loop 看不到答案。

**调用位置**：[loop_run.py:53](../evaluation/loop_run.py#L53)，作为 `controller.run` 的第一个参数。

**函数定义**

```python
def agent_view(self) -> AgentTask:      # benchmark/beaver/dataset.py:75
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `BeaverCase` | 完整的题目记录，含 Gold SQL 和各种标注 |
| 输出 | — | `AgentTask` | 只有三个字段：`case_id: str`、`question: str`、`db: str` |

**内部实现**：[benchmark/beaver/dataset.py:75](../benchmark/beaver/dataset.py#L75)

```python
def agent_view(self) -> AgentTask:
    return AgentTask(case_id=self.case_id, question=self.question, db=self.db)
```

**设计原因**：`BeaverCase` 里有 Gold SQL 和各种标注，`AgentTask` 只有三个字段。Loop 在类型上就拿不到 Gold。

<a id="s2-2"></a>

### 2.2 运行单题 Loop：`controller.run`

**功能**：对一道题运行完整的内循环：检索候选表、生成 SQL，然后反复执行、自检、诊断、修复，直到自检通过或次数用完，返回所有尝试和最终答案。

**调用位置**：[loop_run.py:53](../evaluation/loop_run.py#L53)。

**内部实现**：见[第 3 节](#s3)。

<a id="s2-3"></a>

### 2.3 Loop 结束后判分

**功能**：Loop 返回之后，用这道题的判定函数把每次能执行的尝试的结果与 Gold 结果比较，得出每次尝试是否答对，以及首次、最终是否答对。判分结果只用于评测，不回流给 Loop。

**调用位置**：[loop_run.py:54](../evaluation/loop_run.py#L54)，紧跟在 `controller.run` 返回之后。

**函数定义**

```python
Judge = Callable[[list], tuple[bool, str]]      # evaluation/baseline.py:45（类型）
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `rows` | `list[tuple]` | 某次尝试执行返回的完整结果行 |
| 输出 | — | `tuple[bool, str]` | `(是否答对, 原因)`，如 `(False, "values differ")` |

判定函数由 `dev_judges(devset, split)`（`evaluation/devset.py:108`，开发集）或 `gold_judge(gold_json)`（`evaluation/baseline.py:48`，评测集）生成，每道题一个。

**内部实现**：对每次尝试的完整结果行调用判定器（开发集对照 MySQL 上算出的 Gold 行，评测集对照冻结的 Gold 结果）。对错存进 `rec["attempt_correct"]`，**不写回 `attempts`**。发布时 `attempts` 进 `traces.*`，对错进 `evaluation.*`。网页上的"Gold 判分（Loop 不可见）"来自这里发出的 `judged` 事件。

**设计原因**：判定器 `judge` 只在 Loop 返回**之后**才被取出和调用，Loop 运行期间无从得知对错。

<a id="s2-4"></a>

### 2.4 指标汇总：`summarize`

**功能**：把所有题目的逐题记录汇总成一次运行的指标：首次和最终的答对数、可执行数，恢复率、误伤率、净收益，自检的混淆矩阵，每个技能的使用次数和效果，以及 token 和耗时成本。

**调用位置**：`run_arm` 末尾，全部题目跑完之后。

**函数定义**

```python
def summarize(records: list[dict]) -> dict[str, Any]:      # evaluation/loop_run.py:78
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `records` | `list[dict]` | 每题一条：`attempts`、`attempt_correct`、`final_index`、`first_correct`、`final_correct`、`n_attempts`、`tokens`、`extra_tokens`、`latency_ms` |
| 输出 | — | `dict[str, Any]` | 指标汇总，字段见下表 |

**内部实现**：[loop_run.py:78](../evaluation/loop_run.py#L78)

| 指标 | 公式 | 回答的问题 |
|---|---|---|
| Recovery Rate | 首次错且最终对 / 首次错 | 修好了多少 |
| Harm Rate | 首次对且最终错 / 首次对 | 改坏了多少 |
| Net Gain | 恢复 − 误伤 | 净收益 |
| `executable_first / final` | 首次 / 最终能执行的题数 | 过程指标 |
| `verifier_confusion` | 触发且错 / 误报 / 漏报 / 通过且对 | 自检的质量 |
| `per_skill` | 每个技能的修复次数、修复后能执行、恢复 | 技能的质量 |

**真实数据**

| 运行 | 可执行 首次 → 最终 | 答对 首次 → 最终 |
|---|---|---|
| glm，固定示例，自检 v1，最多修复 1 次 | 4 → 9 | 0 → 0 |
| deepseek，固定示例，自检 v2，最多修复 4 次（在线） | 24 → 29 | **3 → 3** |

修复让更多 SQL 能执行，但两个模型上答对的题数都没有因为修复而增加，原因见[附录 B](#appB)。

---

<a id="s3"></a>

## 3. 单题 Loop：`LoopController.run`

**功能**：内循环的主体，对一道题完成"生成 → 自检 → 诊断 → 修复"的闭环：先检索候选表、生成第一版 SQL；然后每一轮执行 SQL 并自检，不通过就观察失败现象、诊断失败类型、路由到对应技能修复，得到新的 SQL 进入下一轮；循环结束后从所有尝试中选出最终答案。全程只依赖题面和运行时可观察的信息，不接触 Gold。

**调用位置**

- 评测和网页"运行 Loop"：[evaluation/loop_run.py:53](../evaluation/loop_run.py#L53)；
- 网页"提问"：[app/runner.py:369](../app/runner.py#L369) `_ask`，只用 `SelfVerifier`（用户问题没有 Gold）。

```python
res = controller.run(case.agent_view(), verifier_for(case.case_id), on_event=cb)
```

**函数定义**

```python
def run(self, task: AgentTask, verifier: Any, on_event: EventCallback | None = None) -> LoopResult:      # loop_engineer/controller.py:107
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `LoopController` | 持有 `retriever`、`generator`、`executor`、`diagnoser`、`policy`、`ctx`、`cfg` |
| 输入 | `task` | `AgentTask` | 题面：`case_id`、`question`、`db` |
| 输入 | `verifier` | 有 `verify(attempt, rows) -> VerifierDecision` 方法的对象 | `SelfVerifier`（无 Gold）或 `OracleVerifier`（上界） |
| 输入 | `on_event` | `EventCallback \| None` | `Callable[[str, dict], None]`：`(步骤名, 内容)`；可不传 |
| 输出 | — | `LoopResult` | `attempts: list[dict]`（每次尝试的记录）、`rows: list[list[tuple]]`（每次尝试的完整结果行）、`final_index: int`（最终答案是第几次）；属性 `final` 返回最终那次的记录 |

**内部实现**

[loop_engineer/controller.py:107](../loop_engineer/controller.py#L107)。主体结构：

```python
retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)        # :117 → 3.1 检索
gen = self.generator.generate(task, retrieval.tables)                     # :120 → 3.2 生成
attempt = {...}                                                           # :123 第 1 次尝试的记录
for n in range(1, self.cfg.max_attempts + 1):                             # :139 → 3.3 循环体
    exe, rows = self._execute(...)                                        # :141 → 3.3.1 执行
    decision = verifier.verify(attempt, rows)                             # :144 → 3.3.2 自检
    if decision.passed or n == self.cfg.max_attempts: break               # :156 → 3.3.3 终止判断
    obs = observe(attempt, task.db)                                       # :169 → 3.3.4 观察
    diag, usage = self.diagnoser.diagnose(obs)                            # :176 → 3.3.5 诊断
    route = self.policy.route(diag)                                       # :181 → 3.3.6 路由
    res = self.policy.skill(route.skill).repair(obs, diag, self.ctx)      # :184 → 3.3.7 修复
    attempt = nxt                                                         # :203 → 3.3.8 状态交接
final = passed[-1] if passed else executed[-1] if executed else ...       # :206 → 3.4 选出最终答案
return LoopResult(attempts, all_rows, final)                              # :213 返回结果
```

**跳转**：[3.1 检索](#s3-1)　[3.2 生成](#s3-2)　[3.3 循环体](#s3-3)　[3.3.1 执行](#s3-3-1)　[3.3.2 自检](#s3-3-2)　[3.3.3 终止判断](#s3-3-3)　[3.3.4 观察](#s3-3-4)　[3.3.5 诊断](#s3-3-5)　[3.3.6 路由](#s3-3-6)　[3.3.7 修复](#s3-3-7)　[3.3.8 状态交接](#s3-3-8)　[3.4 选出最终答案](#s3-4)

<a id="s3-1"></a>

### 3.1 检索：`retriever.retrieve`

**功能**：从数仓 97 张表中挑出和问题最相关的候选表：用 BM25 按问题文本给每张表打分，返回分数最高的前 20 张及其分数，作为生成 SQL 时给模型看的 schema 范围。

**调用位置**：[controller.py:117](../loop_engineer/controller.py#L117)

```python
retrieval = self.retriever.retrieve(task.question, self.cfg.top_k)
```

**函数定义**

```python
def retrieve(self, question: str, k: int = 10) -> Retrieval:      # agent/retriever.py:147
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `BM25TableRetriever` | 持有表结构目录和预先算好的 BM25 统计 |
| 输入 | `question` | `str` | 问题原文 |
| 输入 | `k` | `int` | 取前几张表；默认 10，控制器调用时传 `cfg.top_k = 20` |
| 输出 | — | `Retrieval` | `tables: tuple[str, ...]`（按分数排序的表名）、`scores: tuple[float, ...]`（对应的 BM25 分数） |

**内部实现**：[agent/retriever.py:147](../agent/retriever.py#L147)。BM25 对每张表打分，取前 20 张。每张表的"文档"由表名、列名、样例值组成，字段权重为 `table:3, column:2, value:1`（[retriever.py:33](../agent/retriever.py#L33)）。分数相同时按表名排序，保证结果确定。

**真实数据**：开发集平均表召回率 0.92。deepseek 运行的错误分析显示，Gold 用到而生成 SQL 没用的表共 41 次，其中 **34 次其实已经在这 20 张里**。瓶颈不在检索，而在下一步选哪张表（见《疑问与价值》疑问 29）。

<a id="s3-2"></a>

### 3.2 生成：`generator.generate`

**功能**：根据问题和候选表生成第一版 SQL：挑选示例（固定示例或相似题示例），补充示例和审核知识里用到的表，拼出包含规则、schema、说明、示例和问题的 prompt，调用 LLM，再从回复中抽取 SQL。

**调用位置**：[controller.py:120](../loop_engineer/controller.py#L120)

```python
gen = self.generator.generate(task, retrieval.tables)
shown = list(getattr(gen, "schema_tables", ()) or retrieval.tables)   # 相似题和补表提示可能补充了表
attempt = {"case_id": task.case_id, "attempt_id": 1, "question": task.question,
           "retrieved_tables": shown, "generated_sql": gen.sql, ...}
```

**函数定义**

```python
def generate(self, task: AgentTask, tables: tuple[str, ...],
             oracle_hints: list[str] | None = None) -> Generation:      # agent/generator.py:153
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `FewShotGenerator` | 持有 LLM 客户端、表结构目录、固定示例、示例库 `index`、使用说明 `knowledge`、审核知识 `curated` |
| 输入 | `task` | `AgentTask` | 题面 |
| 输入 | `tables` | `tuple[str, ...]` | 检索出的候选表 |
| 输入 | `oracle_hints` | `list[str] \| None` | Gold 提示，**只在离线干预实验中使用**；Loop 从不传 |
| 输出 | — | `Generation` | `sql: str`（取不出时为空串）、`raw: str`（模型原文）、`parse_status: str`（OK / NO_SQL / EMPTY_RESPONSE）、`llm: LlmResponse`（token、耗时、是否命中缓存）、`prompt: str`、`example_ids: tuple[str, ...]`、`schema_tables: tuple[str, ...]`（实际给模型看的表）、`notes: str`（说明文本） |

**内部实现**：[agent/generator.py:153](../agent/generator.py#L153)

```python
examples = self.examples                                        # static：固定 3 个示例
if self.index is not None:                                      # dynamic：相似题示例
    hits = self.index.top(task.question, self.k)                # 最相似的 4 道已解题
    examples = [h.example for h in hits]
    extra = [t for h in hits for t in h.tables if t in self.catalog.tables and t not in tables]
    tables = tuple(tables) + tuple(dict.fromkeys(extra))[: self.max_extra_tables]   # 示例用到的表补进 schema
if self.curated:                                                # 审核知识里的补表提示
    tables = tuple(tables) + tuple(t for t in self.curated.extra_tables(task.question, tables) ...)
notes = 审核知识说明 + 数仓使用说明                               # :174–176
prompt = build_prompt(task, render_schema(self.catalog, tables), examples, oracle_hints, notes)   # :178
r = self.client.complete(prompt, system=SYSTEM)                 # :180，经过缓存层
```

- **相似题示例库**（[agent/examples.py](../agent/examples.py)）：dw 全部已解题，**排除评测集和开发集**，共 5,508 道；Gold SQL 经过 Phase 0 的适配规则，要求能解析、不超过 2,500 字符；按问题文本做 BM25 检索。
- **prompt 结构**：规则 + schema + 说明 + 示例 + 问题。规则里有这个数仓的通用约定，比如题干里的 STDDEV / VARIANCE 对应 STDDEV_POP / VAR_POP（[generator.py:37](../agent/generator.py#L37)）。详见 [CONTEXT_DESIGN.md](CONTEXT_DESIGN.md) 4.1 节。
- **LLM 缓存**（[agent/llm.py:240](../agent/llm.py#L240)）：按 (模型, 参数, system, prompt) 的 SHA-256 指纹缓存，同一个 prompt 直接重放。

**真实数据**：dw_4188（glm，固定示例）输入 13,353 token，输出 321 token，耗时 22.3 秒。

| glm-4-flash，开发集 30 题 | 固定示例 | 相似题示例 | 相似题示例 + 使用说明 |
|---|---|---|---|
| 首次答对 | 0 | 3 | **5** |
| SQL 能执行 | 4 | 16 | 16 |

**设计原因**：
1. 贪心解码加缓存，同一配置重跑时第 1 次尝试逐字相同，实验可复现；
2. 相似题示例是为了解决"在相似表之间选错"：同类问题在这个数仓里用哪些表、怎么关联，直接由已解题示范给模型。

<a id="s3-3"></a>

### 3.3 循环体：执行、自检、修复

**功能**：内循环中重复的部分。每一轮执行当前 SQL 并自检；通过或次数用完就结束，否则依次观察、诊断、路由、修复，生成下一次尝试的 SQL。

**调用位置**：[controller.py:139](../loop_engineer/controller.py#L139)

```python
for n in range(1, self.cfg.max_attempts + 1):     # max_attempts = 最大修复次数 + 1
```

**内部实现**：每一轮依次执行下面 8 步。自检通过或次数用完就在 [3.3.3](#s3-3-3) 退出；否则走完观察 → 诊断 → 路由 → 修复，产生下一次尝试，回到 [3.3.1](#s3-3-1)。

<a id="s3-3-1"></a>

#### 3.3.1 执行：`_execute`

**功能**：在 Databricks 上执行当前 SQL，返回两部分：写进尝试记录的执行摘要（状态、报错、行数、前 5 行预览、耗时），以及单独保存的完整结果行。模型没给出 SQL 时不执行，直接沿用抽取 SQL 时的状态。

**调用位置**：[controller.py:141](../loop_engineer/controller.py#L141)

```python
exe, rows = self._execute(attempt["generated_sql"], task.db, attempt["parse_status"])
attempt.update(exe)
```

**函数定义**

```python
def _execute(self, sql: str, db: str, parse_status: str) -> tuple[dict[str, Any], list[tuple]]:      # loop_engineer/controller.py:81
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `sql` | `str` | 本次尝试的 SQL；为空表示模型没给出 SQL |
| 输入 | `db` | `str` | 库名，即 `dw` |
| 输入 | `parse_status` | `str` | 抽取 SQL 的状态；没有 SQL 时直接作为执行状态 |
| 输出 | 第 1 项 | `dict[str, Any]` | 执行摘要，并入尝试记录：`execution_status`、`execution_error`、`result_row_count`、`result_preview`、`exec_latency_ms` |
| 输出 | 第 2 项 | `list[tuple]` | 完整结果行；执行失败时为空列表 |

**内部实现**：[controller.py:81](../loop_engineer/controller.py#L81)

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

执行器 [execution/databricks_sql.py](../execution/databricks_sql.py) 的 `execute`：只读检查 → 切换到 `dw` → 执行 → 超过行数上限返回 `TOO_MANY_ROWS` → 出错时提取错误类别。

**真实数据**（dw_4188）：

```
execution_status: ERROR
execution_error : [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column ... with name `ata`.`DEPARTMENT_CODE` cannot be
                  resolved. Did you mean one of the following? [`sd`.`DEPARTMENT_CODE`, `sd`.`DEPARTMENT_NAME`, ...]
```

**设计原因**：完整结果 `rows` 不写进 `attempt`，只放在单独的列表里，供自检和 [2.3](#s2-3) 判分使用；`attempt` 里只有行数和前 5 行预览，trace 体积可控。

<a id="s3-3-2"></a>

#### 3.3.2 自检：`verifier.verify`

**功能**：在没有 Gold 的情况下判断这次尝试是否可疑：先看执行状态（没有 SQL、报错、结果过大、空结果、整列 NULL），能执行时再检查 SQL 结构和结果数值是否自相矛盾。返回是否通过、触发不通过的信号，以及每个问题的证据和修复提示。

**调用位置**：[controller.py:144](../loop_engineer/controller.py#L144)

```python
decision = verifier.verify(attempt, rows)
attempt.update({"verifier_mode": ..., "verifier_decision": "PASS" if decision.passed else "FAIL", ...})
```

**函数定义**

```python
def verify(self, attempt: dict[str, Any], rows: list[tuple] | None) -> VerifierDecision:      # loop_engineer/verifier.py:44
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `SelfVerifier` | `signals`：启用哪些信号（默认基础 5 个 + 触发级 10 个） |
| 输入 | `attempt` | `dict[str, Any]` | 本次尝试的记录（已含执行摘要） |
| 输入 | `rows` | `list[tuple] \| None` | 完整结果行 |
| 输出 | — | `VerifierDecision` | `passed: bool`、`mode: str`（self / oracle）、`signals: tuple[str, ...]`（不通过的原因）、`findings: tuple[Finding, ...]`（触发不通过的检查）、`advisories: tuple[Finding, ...]`（只作提示的检查） |

`Finding` 的字段：`signal: str`、`message: str`（给人看）、`evidence: dict`（证据）、`hint: str`（写进修复 prompt 的英文提示）。

**内部实现**：[loop_engineer/verifier.py:44](../loop_engineer/verifier.py#L44) `SelfVerifier.verify`（v2）

```python
if status in ("NO_SQL", "EMPTY_RESPONSE"):                      hits.append("no_sql")
elif status == "TOO_MANY_ROWS":                                  hits.append("too_many_rows")
elif status not in ("SUCCESS", "NO_SQL", "EMPTY_RESPONSE", "TOO_MANY_ROWS"):
                                                                 hits.append("execution_error")
elif status == "SUCCESS":
    if not rows:                                                 hits.append("empty_result")
    elif any(all(r[i] is None for r in rows) for i in range(len(rows[0]))):
                                                                 hits.append("all_null_column")
    elif rows:                                                   # 能执行、有结果时再做语义检查
        for f in check_all(question, generated_sql, rows):
            if f.signal in TRIGGER_SIGNALS: findings.append(f); hits.append(f.signal)   # 触发修复
            else:                           advisories.append(f)                        # 只作提示
return VerifierDecision(not hits, self.mode, tuple(hits), tuple(findings), tuple(advisories))
```

三层信号（[verifier.py:23](../loop_engineer/verifier.py#L23)、[checks.py:48](../loop_engineer/checks.py#L48)）：

| 层 | 信号 | 如何判断 |
|---|---|---|
| 显式失败 | 没有 SQL、执行报错、结果过大、空结果、整列 NULL | 执行状态 |
| 结构规则 | 关联条件恒为真、JOIN 缺条件、缺少分组、四舍五入与题意相反 | 解析 SQL，对照题干（[checks.py:142](../loop_engineer/checks.py#L142) `check_static`） |
| 数值一致性 | 统计量为负、计数非整数、min > max、平均值不在 [min, max]、方差 ≠ 标准差²、标准差 > 极差 | 把输出列对应到产生它的统计函数，由**代码**核对（[checks.py:319](../loop_engineer/checks.py#L319) `check_numeric`） |

**"自检通过"只表示没发现问题，不代表答案正确**：Loop 运行时看不到标准答案。

**真实数据**：
- glm 固定示例运行，第 1 次自检：触发且确实错 27，通过但其实错 3（漏报），误报 0；
- deepseek 在线运行：修复后能执行的从 24 升到 29，但答对始终是 3。**26 道"能执行但答错"几乎全部通过了自检**，见[附录 B](#appB)。

**设计原因**：这些信号在生产环境都拿得到。每个信号都先在开发集上离线评估过（`scripts/verifier_eval.py`），**只有在正确答案上误报接近 0 的才能触发修复**；误报较高的只作提示。这 10 个触发信号中，4 条结构规则在 5,687 道 Gold SQL 上误报最高 0.3%，数值规则在 378 个正确结果上为 0。`OracleVerifier`（[verifier.py:80](../loop_engineer/verifier.py#L80)）用 Gold 判断，只作为上界单独标注。

<a id="s3-3-3"></a>

#### 3.3.3 终止判断

**功能**：决定是否结束循环：先把本次尝试和结果行存下来，自检通过或已达最大尝试次数就退出；否则继续往下诊断和修复。

**调用位置**：[controller.py:156](../loop_engineer/controller.py#L156)，紧跟在自检之后。

**函数定义**：这是 `LoopController.run` 内部的代码，不是独立函数；用到的变量都是 `run` 的局部变量。

**内部实现**：

```python
attempts.append(attempt)
all_rows.append(rows)
if decision.passed or n == self.cfg.max_attempts:
    break
```

两个出口：自检通过，或已达最大尝试次数（最大修复次数 + 1）。最后一次尝试即使失败，也不再诊断和修复。

<a id="s3-3-4"></a>

#### 3.3.4 观察：`observe`

**功能**：把一次尝试的记录转换成诊断可用的观察结果：只按白名单保留 12 个运行时可见的字段，并从 Databricks 报错文本中解析出错误类别、找不到的列和它的别名、数据库给出的候选列、不存在的表名。

**调用位置**：[controller.py:169](../loop_engineer/controller.py#L169)

```python
obs = observe(attempt, task.db)
```

**函数定义**

```python
def observe(record: dict[str, Any], db: str) -> Observation:      # loop_engineer/observer.py:59
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `record` | `dict[str, Any]` | 一次尝试的记录；只有白名单里的 12 个字段会被读取 |
| 输入 | `db` | `str` | 库名 |
| 输出 | — | `Observation` | 白名单字段（`question`、`retrieved_tables`、`generated_sql`、`execution_status`、`execution_error`、`result_row_count`、`result_preview`、`verifier_signals`、`verifier_findings` 等）+ 从报错解析出的 `error_class`、`unresolved_qualifier`、`unresolved_column`、`suggestions`、`missing_table` + `db` |

**内部实现**：[loop_engineer/observer.py:59](../loop_engineer/observer.py#L59)。按白名单（[observer.py:16](../loop_engineer/observer.py#L16)）取字段，再用正则从报错文本中提取结构化信号：

```python
OBSERVABLE_FIELDS = ("case_id", "attempt_id", "question", "retrieved_tables", "generated_sql", "parse_status",
                     "execution_status", "execution_error", "result_row_count", "result_preview",
                     "verifier_signals", "verifier_findings")

def observe(record, db):
    r = {k: record.get(k) for k in OBSERVABLE_FIELDS}  # 白名单：其他字段一律丢弃
    m_cls = _ERROR_CLASS.search(err)     # [UNRESOLVED_COLUMN.WITH_SUGGESTION] → UNRESOLVED_COLUMN
    m_col = _UNRESOLVED.search(err)      # name `ata`.`DEPARTMENT_CODE` cannot be resolved
    m_sug = _SUGGEST.search(err)         # Did you mean one of the following? [...]
    m_tab = _TABLE_NOT_FOUND.search(err) # 表不存在；table_name() 取表引用的最后一段
    ...
```

**真实数据**（dw_4188）：错误类别 `UNRESOLVED_COLUMN`；找不到的列 `ata.DEPARTMENT_CODE`；数据库给的候选 `sd.DEPARTMENT_CODE, sd.DEPARTMENT_NAME, ...`。

**设计原因**：用白名单而不是黑名单。将来 `attempt` 里多出一个由 Gold 算出的字段，黑名单会漏掉，白名单天然挡住。12 个字段的含义见《疑问与价值》疑问 25。

<a id="s3-3-5"></a>

#### 3.3.5 诊断：`diagnoser.diagnose`

**功能**：根据观察结果判断失败属于哪一类（选表、列映射、关联键、查询拆解、领域知识、执行错误），给出置信度、原因，以及交给修复技能的证据（如这一列实际在哪些表里）。先用规则判断，规则判断不了再调用 LLM。

**调用位置**：[controller.py:176](../loop_engineer/controller.py#L176)

```python
diag, usage = self.diagnoser.diagnose(obs)
```

**函数定义**

```python
def diagnose(self, obs: Observation) -> tuple[Diagnosis, dict[str, Any]]:
def diagnose_by_rules(obs: Observation, catalog: SchemaCatalog) -> Diagnosis | None:      # loop_engineer/diagnose.py:186 / :83
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `Diagnoser` | 持有表结构目录 `catalog` 和 LLM 客户端 `client`（为 None 时只用规则） |
| 输入 | `obs` | `Observation` | 观察结果 |
| 输出 | 第 1 项 | `Diagnosis` | `failure_type: str`（6 种失败类型或 UNKNOWN）、`confidence: float`、`reason: str`、`source: str`（rule / llm / fallback）、`repair_hints: dict`（交给技能的证据）、`version: str` |
| 输出 | 第 2 项 | `dict[str, Any]` | LLM 用量 `{input_tokens, output_tokens, latency_ms, cached}`；没调 LLM 时为 `{}` |

`diagnose_by_rules` 是规则部分：命中规则返回 `Diagnosis`，判断不了返回 `None`，交给 LLM。

**内部实现**：[loop_engineer/diagnose.py:186](../loop_engineer/diagnose.py#L186)。先走规则 `diagnose_by_rules`（[:83](../loop_engineer/diagnose.py#L83)），规则没有结论才调用 LLM。

核心规则：在 schema 里为报错的列定位（[diagnose.py:88](../loop_engineer/diagnose.py#L88)）：

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

其他规则：表不存在 → 选表（[:111](../loop_engineer/diagnose.py#L111)）；结果过大 → 关联键（[:115](../loop_engineer/diagnose.py#L115)）；语法、聚合等 SQL 层面的错误 → 执行错误。

自检发现的语义问题也由规则映射（[diagnose.py:51](../loop_engineer/diagnose.py#L51)、[:126](../loop_engineer/diagnose.py#L126)）：

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

**设计原因**：Databricks 已经告诉我们"哪个列、在哪个别名下找不到"，剩下的只是"这个列实际在哪张表"，一次 schema 查找就能确定。LLM 在这类问题上反而更差：评测集上 LLM 诊断的严格准确率 0/19，规则诊断的宽松准确率 76.5%。`repair_hints` 把查找过程的证据原样传给下一步，技能不需要重新推理。

<a id="s3-3-6"></a>

#### 3.3.6 路由：`policy.route`

**功能**：根据诊断出的失败类型选出负责修复的技能：查"失败类型 → 技能"映射表得到技能名，被禁用的技能退回 RepairSQL；再由 `skill(name)` 按名字取出技能对象，供下一步调用。

**调用位置**：[controller.py:181](../loop_engineer/controller.py#L181)

```python
route = self.policy.route(diag)
```

**函数定义**

```python
def route(self, diagnosis: Diagnosis) -> Route:
def skill(self, name: str):      # loop_engineer/policy.py:53 / :63
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 | `self` | `Policy` | `disabled: set[str]`（被禁用的技能）、`mapping: dict`（失败类型 → 技能名，默认 `TARGETED`） |
| 输入 | `diagnosis` | `Diagnosis` | 诊断结果，只用其中的 `failure_type` |
| 输出 | — | `Route` | `skill: str`（技能名）、`fallback: bool`（是否临时替代）、`reason: str` |

`skill(name)` 按名字从注册表 `SKILLS` 取出技能对象（实现 `RepairSkill` 接口），代码里没有标注返回类型。

**内部实现**：[loop_engineer/policy.py:53](../loop_engineer/policy.py#L53)，查映射表 `TARGETED`（[:28](../loop_engineer/policy.py#L28)）：

```python
TARGETED = {
    TABLE_RETRIEVAL: "RetrieveAgain",    COLUMN_MAPPING: "SchemaSearch",   JOIN_KEY: "FindJoinPath",
    DOMAIN_KNOWLEDGE: "ReplanQuery",     QUERY_DECOMPOSITION: "ReplanQuery",
    EXECUTION: "RepairSQL",              UNKNOWN: "RepairSQL",
}
def route(self, diagnosis):
    skill = self.mapping.get(diagnosis.failure_type, FALLBACK)
    if skill in self.disabled:  return Route(FALLBACK, True, f"{skill} disabled (ablation)")
    return Route(skill, diagnosis.failure_type in FALLBACK_ROUTES, f"{diagnosis.failure_type} -> {skill}")
```

**真实数据**：glm 那次运行的路由分布：RetrieveAgain 14、SchemaSearch 9、RepairSQL 3、ReplanQuery 1。

**设计原因**：路由表是一个字典，消融只需换表或禁用技能，不改代码。`fallback=True`（如领域知识暂时走 ReplanQuery）会写进 trace，看板上能看出"这次走了兜底"。

<a id="s3-3-7"></a>

#### 3.3.7 修复：`skill.repair`

**功能**：由路由选出的技能修复 SQL，返回修复后的 SQL 和修复过程。每个技能针对一类失败：先做不需要 LLM 的确定性工作（如改正列的别名、查出要补的表和关联键），解决不了的部分再带着定向指令调用一次 LLM。

**调用位置**：[controller.py:184](../loop_engineer/controller.py#L184)

```python
res = self.policy.skill(route.skill).repair(obs, diag, self.ctx)
```

`policy.skill(name)` 从注册表 `SKILLS`（[policy.py:24](../loop_engineer/policy.py#L24)）按名字取出技能对象。

**内部实现**：5 个技能实现同一个接口，各自先做不需要 LLM 的工作，需要时再调一次 LLM。下面先讲公共部分，再讲各技能。

##### 公共部分：接口、上下文、修复模板

**功能**：所有技能共享的部分：统一的 `repair` 接口，技能可用的资源 `RepairContext`，统一的返回结构 `RepairResult`，以及需要 LLM 时共用的修复模板和调用函数 `llm_repair`；`observed_text` 负责把报错或自检发现的问题写成 prompt 里的一段文字。

**调用位置**：[controller.py:184](../loop_engineer/controller.py#L184)：`self.policy.skill(route.skill).repair(obs, diag, self.ctx)`，路由选出的技能对象被调用 `repair`。

**函数定义**

```python
def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult: ...
def llm_repair(skill: str, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext, tables: list[str],
               instruction: str, action: str, details: dict[str, Any] | None = None) -> RepairResult:
def observed_text(obs: Observation) -> str:      # skills/base.py:50 / :85 / :73
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| `repair` 输入 | `obs` | `Observation` | 观察结果 |
| `repair` 输入 | `diagnosis` | `Diagnosis` | 诊断结果，技能主要读 `repair_hints` |
| `repair` 输入 | `ctx` | `RepairContext` | `catalog: SchemaCatalog`、`client: ChatClient`、`examples: list[FewShotExample]`（未使用）、`max_schema_tables: int = 24` |
| `repair` 输出 | — | `RepairResult` | `repaired_sql: str`、`repair_skill: str`、`repair_action: str`、`repair_reason: str`、`tables: tuple[str, ...]`、`used_llm: bool`、`input_tokens / output_tokens / latency_ms: int`、`parse_status: str`、`details: dict` |
| `llm_repair` 输入 | `skill`、`tables`、`instruction`、`action`、`details` | `str`、`list[str]`、`str`、`str`、`dict \| None` | 技能名、给模型看的表、定向指令、动作描述、技能内部过程（并入返回值的 `details`） |
| `observed_text` 输出 | — | `str` | 写进修复 prompt 的"观察到的问题"：报错原文，或自检发现的问题和提示 |


**内部实现**

接口（[skills/base.py:47](../skills/base.py#L47)）：

```python
class RepairSkill(Protocol):
    name: str
    def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult: ...
```

- `RepairContext`（[base.py:23](../skills/base.py#L23)）只包含 Agent 可见的资源：schema、LLM 客户端；给模型看的表最多 24 张。
- `RepairResult`（[base.py:31](../skills/base.py#L31)）除了新 SQL，还有 `details`：技能内部做了什么，包括确定性修改、规则无法决定的部分、候选列、关联键、给 LLM 的指令和完整 prompt。网页的修复步骤和分析报告展示的就是这些。

需要 LLM 时，所有技能共用一个模板（[base.py:53](../skills/base.py#L53)），调用封装在 `llm_repair`（[base.py:85](../skills/base.py#L85)）。**技能之间的差别只在 `{instruction}` 和给哪些表的 schema**：

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

`{observed}` 由 `observed_text`（[base.py:73](../skills/base.py#L73)）生成。SQL 能执行但自检发现问题时，会写进每条发现的英文提示，例如 *"The condition sd.X = sd.X compares a column with itself..."*，修复模型因此知道具体错在哪。

##### SchemaSearch（列映射）

**功能**：修复"列挂错了表或别名"。先逐个检查 SQL 里带别名的列，能唯一确定正确表的直接改过去；会让关联条件失效的改动撤回；剩下改不了的部分，连同候选列和候选关联键交给 LLM。

**调用位置**：诊断为列映射（`COLUMN_MAPPING_FAILURE`）时，3.3.7 的 `repair` 调用落到 `SKILLS["SchemaSearch"]`。

**函数定义**

```python
def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult:
def fix_column_refs(sql: str, catalog: SchemaCatalog) -> ColumnFix:      # skills/schema_search.py:104 / :39
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| `repair` | 同公共部分 | 同公共部分 | 全部确定性修好时 `used_llm=False`，否则调 `llm_repair` |
| `fix_column_refs` 输入 | `sql`、`catalog` | `str`、`SchemaCatalog` | 要修的 SQL、表结构目录 |
| `fix_column_refs` 输出 | — | `ColumnFix` | `sql: str`（修改后的 SQL）、`changes: list[str]`（如 `a.COL -> b.COL`）、`unresolved: list[str]`（改不了的引用）、`parsed: bool` |

**内部实现**：[skills/schema_search.py:104](../skills/schema_search.py#L104)

```python
fix = fix_column_refs(obs.generated_sql, ctx.catalog)        # ① 确定性修复
if fix.changes and not fix.unresolved:
    return RepairResult(fix.sql, ..., used_llm=False, details={"deterministic_changes": fix.changes, ...})
# ② 规则决定不了的部分交给 LLM，从部分修好的 SQL 开始；需要关联键时附上从 schema 推断的候选关联键
return llm_repair(self.name, start, diagnosis, ctx, [...], instruction, action,
                  {"deterministic_changes": ..., "unresolved": problems, "candidate_columns": ..., "join_candidates": ...})
```

确定性部分 `fix_column_refs`（[schema_search.py:39](../skills/schema_search.py#L39)）做两件事：
1. **逐个作用域检查每一个带别名的列**（[:48](../skills/schema_search.py#L48)）：`a.COL` 的表里没有这一列，而同一作用域恰好只有一张表有，就改过去。检查全部而不只是报错的那一个，因为引擎一次只报第一个错；
2. **关联条件守卫**（[:74](../skills/schema_search.py#L74)）：改完后如果关联条件两边变成同一张表（如 `sd.X = sd.X`），SQL 能跑，但两张表失去了关联。这种修改会被撤回，标为"需要真正的关联键"。

**真实数据**（dw_4188）：所有候选改动都被守卫撤回，3 个关联问题连同推断的关联键交给 LLM。没有守卫的话，第一处会变成 `sd.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`，SQL 能执行但答案错。这就是问题记录 #8：Phase 5 的"可执行 4→10"因此虚高，修正后为 4→8。现在自检 v2 的 `join_tautology` 规则会从自检这一侧再兜一次底。

##### RetrieveAgain（选表）

**功能**：修复"少用了表"或"用了不存在的表"。直接使用诊断找到的、真正包含该列的表，从 schema 推断这些表怎么关联到 SQL 已用的表上，再让 LLM 把它们加进查询；表不存在时，改为找名字相近的真实表。

**调用位置**：诊断为选表（`TABLE_RETRIEVAL_FAILURE`）时，3.3.7 的 `repair` 调用落到 `SKILLS["RetrieveAgain"]`。

**函数定义**

```python
def repair(self, obs: Observation, diagnosis: Diagnosis, ctx: RepairContext) -> RepairResult:      # skills/retrieve_again.py:25
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| 输入 / 输出 | 同公共部分 | 同公共部分 | 从 `diagnosis.repair_hints` 读 `tables_to_add` / `owner_tables`（列找不到）或 `missing_table`（表不存在） |

**内部实现**：[skills/retrieve_again.py:25](../skills/retrieve_again.py#L25)

```python
add = list(h.get("tables_to_add") or h.get("owner_tables") or [])[:4]     # 直接用诊断找到的表
joins = [j.sql() for t in add for j in connect(ctx.catalog, t, used)][:8]   # 新表怎么接到已用的表上
instruction = f"Column {h.get('column')} is not in the table you used; it lives in {', '.join(add)}. ..."
```

诊断找到的证据（`repair_hints["tables_to_add"]`），修复直接使用。表不存在时，改为找名字相近的真实表。

##### 其他技能

**功能**：其余三个技能分别处理关联键错误、查询结构错误和执行报错（以及兜底），做法见下表。

| 技能 | 处理 | 做法 |
|---|---|---|
| FindJoinPath | 关联键 | 列出 SQL 已用各表之间共享的键列，要求逐个核对 JOIN ON |
| ReplanQuery | 查询拆解、领域知识（临时） | 让模型先拆子问题，每个写成一个 CTE，再逐项核对 |
| RepairSQL | 执行错误、兜底 | 用最小改动修好报错，附上 Databricks 语法注意事项 |

设计说明见 [LOOP_DESIGN.md](LOOP_DESIGN.md)；"自检信号 → 失败类型 → 技能"的完整对照见 [ARCHITECTURE.md](ARCHITECTURE.md)。

<a id="s3-3-8"></a>

#### 3.3.8 状态交接：写回诊断，组装下一次尝试

**功能**：把这一轮的诊断和修复写进尝试记录，并组装下一次尝试：诊断结果写在失败的那次尝试上，修复后的 SQL 和技能信息写在新的尝试上，两条记录互相指向；然后用新的尝试进入下一轮。

**调用位置**：[controller.py:159](../loop_engineer/controller.py#L159) 起，修复完成后、进入下一轮之前。

**函数定义**：这是 `LoopController.run` 内部的代码，不是独立函数；用到的变量都是 `run` 的局部变量。

**内部实现**：

```python
nxt = {"case_id": task.case_id, "attempt_id": n + 1, "question": task.question,
       "retrieved_tables": attempt["retrieved_tables"], "strategy": self.cfg.strategy, "diag_tokens": 0}
attempt.update({"failure_type": ..., "diagnosis_confidence": ..., "diagnosis_reason": ..., "repair_hints": ...})  # 诊断写在失败的那次上
nxt.update({"generated_sql": res.repaired_sql, "repair_skill": route.skill, "repair_action": res.repair_action, ...})  # 修复写在新的那次上
attempt["repaired_sql"] = nxt["generated_sql"]     # :196，前后两次互相指向，trace 能串起来
attempt["repair_skill"] = nxt["repair_skill"]
emit("repair", ..., before_sql=..., sql=..., details=details)
attempt = nxt                                      # :203，回到 3.3.1
```

约定："为什么失败"记在失败的那次尝试上，"做了什么修复"记在新的尝试上。按 `attempt_id` 排好，就是一条"失败 → 诊断 → 修复 → 结果"的时间线。允许多轮修复时，每一轮都重复 [3.3.1](#s3-3-1)–[3.3.8](#s3-3-8)。

**真实数据**（dw_4188，最多修复 2 次）：3 次尝试都报 `UNRESOLVED_COLUMN`，两轮都诊断为列映射并交给 SchemaSearch，但 glm 两次都只给表名加了 `dw.` 前缀。分析报告自动得出："第 2 次之后的修复轮次没有带来新的可执行题，增加轮次主要增加成本。"

<a id="s3-4"></a>

### 3.4 选出最终答案

**功能**：循环结束后，从所有尝试中选出作为最终答案的一次：优先选最后一个自检通过的，其次选最后一个能执行的，都没有就选最后一次；标记每次尝试是否为最终答案，返回 `LoopResult`。

**调用位置**：[controller.py:204](../loop_engineer/controller.py#L204)，循环结束之后。

**函数定义**：这是 `LoopController.run` 内部的代码，不是独立函数；用到的变量都是 `run` 的局部变量。

**内部实现**：

```python
passed   = [i for i, a in enumerate(attempts) if a["verifier_decision"] == "PASS"]
executed = [i for i, a in enumerate(attempts) if a["execution_status"] == "SUCCESS"]
final = passed[-1] if passed else executed[-1] if executed else len(attempts) - 1
```

优先级：最后一个自检通过的 → 最后一个能执行的 → 最后一个。每次尝试标上 `final_status`（FINAL / SUPERSEDED），发出 `final` 事件，返回 `LoopResult(attempts, all_rows, final)`。

**设计原因**：如果修复把能跑的 SQL 改坏了，这条规则会保留原来能跑的那个。选择只看自检和执行状态，不看 Gold。

---

<a id="s4"></a>

## 4. 发布、回放与分析

**功能**：运行结束后的处理：把逐次尝试的记录展平写入 Delta 和 MLflow，供看板回放每道题的完整过程；网页运行还会根据事件自动生成分析报告；另有脚本把运行结果逐题与 Gold 对照，做错误分析。

**调用位置**：运行结束之后。命令行运行需要手动发布；网页运行默认自动发布。

**函数定义**

```python
def flatten_loop_records(records: list[dict]) -> list[dict]:
def publish_run(runner: Any, layout: Layout, run_dir: Path, experiment_id: str,
                mlflow_experiment: str | None) -> dict[str, Any]:
def build_report(events: list[dict], request: dict, summary: dict | None, run_id: str | None,
                 elapsed_s: float | None = None) -> str:      # dbx/publish.py:35 / :179，app/report.py:159
```

| | 名称 | 类型 | 含义 |
|---|---|---|---|
| `flatten_loop_records` | `records` | `list[dict]` → `list[dict]` | 每题一条（含 attempts 列表）→ 每次尝试一条，附上是否答对、是否最终答案 |
| `publish_run` | `runner`、`layout`、`run_dir`、`experiment_id`、`mlflow_experiment` | → `dict[str, Any]` | 执行器、catalog 布局、运行目录、实验编号、MLflow 实验名；返回发布摘要（run_id、trace 行数、MLflow run id） |
| `build_report` | `events`、`request`、`summary`、`run_id`、`elapsed_s` | → `str` | 运行事件、运行设置、指标汇总、运行编号、用时；返回 Markdown 分析报告 |

**内部实现**：

- **发布**：`python scripts/publish_run.py runs/phase6/<run_id>`。[dbx/publish.py:35](../dbx/publish.py#L35) `flatten_loop_records` 把每题的每次尝试展平成一行，写入 `traces.execution_traces`；对错写入 `evaluation.evaluation_results`；汇总写入 `evaluation.runs` 和 MLflow（[publish.py:179](../dbx/publish.py#L179) `publish_run`）。
- **回放**：Loop Debug Console（[app/dashboard.py](../app/dashboard.py)）的"逐题追踪"可以选运行、选题，看 [3.2](#s3-2)–[3.4](#s3-4) 的每个字段。网页发起的运行标为"[运行页]"。
- **实时 trace 和分析报告**：网页"运行 Loop"页按事件逐步展示检索（BM25 分数）、生成（prompt、示例、补充的表）、执行、自检（触发项和提示项）、观察、诊断、路由、修复（技能内部过程、指令、SQL 对比）。运行结束后，[app/report.py:159](../app/report.py#L159) `build_report` 不调用 LLM，直接生成分析报告：修复前后对比柱状图、逐次尝试折线图、逐题 × 逐次状态格子图，以及总体结果、各环节表现、成本、发现与建议。
- **错误分析**：`python scripts/analyze_run.py <run_id>` 把一次开发集运行逐题和 Gold 对照：用表、列、关联、过滤值、统计运算、行数和列数。[附录 B](#appB) 的结论就来自它。

---

<a id="appA"></a>

## 附录 A：两道题的完整轨迹（glm，固定示例）

### A1. dw_4188：诊断对了，模型没修好

| 步骤 | 内容 |
|---|---|
| [3.2](#s3-2) 生成 | 5 张表的 JOIN；`ata.DEPARTMENT_CODE`（`academic_terms_all` 里没有这个列） |
| [3.3.1](#s3-3-1) 执行 | `ERROR` `UNRESOLVED_COLUMN`，候选 `sd.DEPARTMENT_CODE` |
| [3.3.2](#s3-3-2) 自检 | 未通过：`execution_error` |
| [3.3.4](#s3-3-4) 观察 | 别名 `ata`，列 `DEPARTMENT_CODE` |
| [3.3.5](#s3-3-5) 诊断 | 规则：列在已使用的 `sis_department` 里 → 列映射，0.9，`wrong_alias` |
| [3.3.6](#s3-3-6) 路由 | SchemaSearch |
| [3.3.7](#s3-3-7) 修复 | 确定性改动全部被关联条件守卫撤回 → 3 个关联问题和推断的关联键交给 LLM |
| 下一次 | LLM 只加了 `dw.` 前缀 → 同样报错 |
| [2.3](#s2-3) 判分 | 全部错误 |

**讲点**：Loop 的每个环节都给出了正确信息，失败在模型执行指令的能力上。守卫阻止了一次"能跑但错"的假修复。

### A2. dw_5478：修复后能跑了，但答案仍然错（自检漏报）

| 步骤 | 内容 |
|---|---|
| [3.2](#s3-2) 生成 | 用 `sdp.GRADUATE_LEVEL` 过滤研究生 |
| [3.3.1](#s3-3-1) 执行 | `ERROR` `UNRESOLVED_COLUMN`：`sdp.GRADUATE_LEVEL` |
| [3.3.5](#s3-3-5) 诊断 | 规则：`GRADUATE_LEVEL` 只在 `sis_course_description` 里，这张表检索到了但没用 → 选表，0.8，`table_not_used` |
| [3.3.6](#s3-3-6) 路由 | RetrieveAgain |
| [3.3.7](#s3-3-7) 修复 | 加入 `sis_course_description` 和推断的关联键 → LLM 改写 |
| 下一次 | `SUCCESS`，1 行 → **自检通过** |
| [2.3](#s2-3) 判分 | 错误 |

**讲点**：
1. 诊断 → 技能 → 证据传递完整：`tables_to_add` 从诊断直接进入技能的指令；
2. 模型改写时**丢掉了"研究生"这个条件**。SQL 能跑、结果非空、数值自洽，自检没有依据发现这个语义错误。网页上会标黄："自检通过，但答案是错的（Verifier 漏报）"。

---

<a id="appB"></a>

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

**结论**：所有不看标准答案的检查，衡量的都是"通常意义上的正确"；而这类错误的根源是**不知道这个数仓的约定**，也就是该用哪张表、怎么关联。这只能从已解题中学。所以改进放在生成端：[3.2](#s3-2) 的相似题示例和数仓使用说明，在 glm 上把答对从 0 提升到 5、能执行从 4 提升到 16。

这引出**双循环**的设计方向：
- **内循环**（运行时，不接触 Gold）：兜住显式失败和结构性错误，也就是本文档描述的流程；
- **外循环**（离线，使用标注）：从错误分析和执行反馈中归纳数仓知识（示例库、易混表说明、补表提示），生成阶段直接使用。

详细数据见 [EXECUTION_LOG.md](EXECUTION_LOG.md) 的"补强 Verifier"和"提升正确率"两节。

---

<a id="appC"></a>

## 附录 C：已知局限与对应代码位置

| 局限 | 位置 | 可能的改进 |
|---|---|---|
| 能执行但语义错的答案，自检发现不了 | [verifier.py](../loop_engineer/verifier.py)；[附录 B](#appB) | 生成端补充知识（外循环）；多候选一致性作为运行时的可疑信号 |
| 诊断置信度没有校准 | [diagnose.py:88](../loop_engineer/diagnose.py#L88) 起，0.9 / 0.8 / 0.85 / 0.7 是人工设定的 | 用诊断准确率数据校准 |
| 执行超时、被拒绝执行没有对应规则，会落到 LLM 诊断 | [diagnose.py:83](../loop_engineer/diagnose.py#L83) `diagnose_by_rules` | 超时 → 关联键；被拒绝 → 执行错误 |
| 确定性预处理只有 SchemaSearch 有 | [schema_search.py:39](../skills/schema_search.py#L39) | RetrieveAgain 可以用规则直接补 JOIN |
| 修复 prompt 没有带生成时的相似题示例和说明 | [skills/base.py:85](../skills/base.py#L85) `llm_repair` | 修复复用生成阶段的经验知识（[CONTEXT_DESIGN.md](CONTEXT_DESIGN.md) 缺口 G1） |
| 修复效果受模型能力限制 | dw_4188 多轮修复同样报错 | 反复出现的同类错误改成确定性修复；换更强的模型 |
| RetrieveKnowledge 还是占位 | [skills/retrieve_knowledge.py](../skills/retrieve_knowledge.py)；领域知识暂时路由到 ReplanQuery | 由外循环生成的知识库提供，不含 Gold |
| 相似题示例库取自 BEAVER 已解题 | [agent/examples.py](../agent/examples.py) | 生产中应只收录人工审核过的查询；修复后能运行的 SQL 不能直接入库 |
