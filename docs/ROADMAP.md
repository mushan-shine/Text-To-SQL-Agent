# 执行路线图

> 项目定位和设计依据见 [PROJECT_POSITIONING.md](PROJECT_POSITIONING.md)。本文档只回答**做什么、按什么顺序、做到什么程度算完成**。
>
> 状态图例：✅ 完成 · 🔄 进行中 · ⏸ 阻塞 · ⬜ 未开始

---

## 一、整体结构

项目分 6 个阶段、13 个 Phase。每个阶段结束时有一道**关卡**：关卡不通过，不进入下一阶段。

```
阶段一  可信基座          Phase 0            闭环的"裁判"：Gold 结果可信
   │    关卡：Q1–Q5 有答案，确定方案 A/B/C
   ▼
阶段二  基线与观测        Phase 1–3          闭环的"起点"：失败从哪来、是什么
   │    关卡：Baseline 失败数量足够、类型有分布
   ▼
阶段三  闭环本体          Phase 4–6          Observe → Diagnose → Repair → Verify
   │    关卡：端到端跑通，不接触 Gold
   ▼
阶段四  实验证明          Phase 7–8          闭环到底有没有用、哪个部件有用
   │    关卡：主实验和消融结果齐全、可复现
   ▼
阶段五  展示与交付        Phase 9 + 作品集    面试官能看到、学员能讲出来
   │
   ▼
阶段六  外层循环（加分）   Phase 10–12        Self-Healing → Self-Improving
```

| 阶段 | Phase | 在 Loop 中的角色 | 状态 |
|---|---|---|---|
| 一 可信基座 | 0 Benchmark Qualification | 裁判 | 🔄 代码完成，等待运行 |
| 二 基线与观测 | 1 Baseline · 2 Trace · 3 Failure Taxonomy | 起点、Observer 的数据、诊断的标准答案 | ⬜ |
| 三 闭环本体 | 4 Diagnosis · 5 Repair Skills + Policy · 6 Controller + Verifier | 闭环本身 | ⬜ |
| 四 实验证明 | 7 对照实验 · 8 消融 | 证明 | ⬜ |
| 五 展示与交付 | 9 Loop Debug Console · 作品集材料 | 展示 | ⬜ |
| 六 外层循环 | 10 Experience Store · 11 Learning Loop · 12 Regression | 自我改进 | ⬜ 加分项 |

**全程不变的约束**

- Agent、Diagnoser、Repair Skill **不接触任何 Gold 信息**（Gold SQL、Gold Tables、Join Keys、Column Mapping、Domain Knowledge、Decomposition）
- 主实验用 **Self-verified** 触发，Oracle 只作标注过的上界
- LLM：智谱 `glm-4-flash`，`do_sample=False`；所有实验组使用同一个模型
- 实验结果全部来自实际运行，不预填、不编造

---

## 二、阶段一：可信基座（Phase 0）

**目标**：证明 BEAVER 的 Gold 结果能在 Databricks 上可信复现，而且不修改 Gold SQL。

### Phase 0 · BEAVER → Databricks Qualification

| 步骤 | 做什么 | 谁 | 产出 | 完成标准 | 状态 |
|---|---|---|---|---|---|
| 0.1 | 编写导入、复制、兼容性、Gold 冻结、报告的代码，以及单元测试 | Claude | `benchmark/beaver/*`、`scripts/phase0.py`、`tests/` | 离线测试全部通过 | ✅ 45 个测试通过 |
| 0.2 | 申请 HF 数据访问并登录（A1） | 你 | — | `hf auth whoami` 能显示用户名 | ⏸ |
| 0.3 | 重新登录 Databricks（A2） | 你 | — | `databricks warehouses list` 至少有一个 warehouse | ⏸ |
| 0.4 | 填写 `.env`：MySQL 密码和智谱 key（A3） | 你 | `.env` | `mysql` 能登录 | ⏸ |
| 0.5 | 下载 BEAVER 数据库并导入 MySQL（A4） | 你 | 本机 MySQL 里的 BEAVER 库 | `SHOW DATABASES` 能看到 BEAVER 的库 | ⏸ |
| 0.6 | `phase0.py env`：环境检查，建好 catalog、schema、volume | Claude | `runs/phase0/01_environment.json` | 连接正常；排序规则和 ANSI 设置检查通过 | ⬜ |
| 0.7 | `phase0.py import`：导入 100 个 case 和表结构信息 | Claude | `benchmark.cases`、`tables_meta`、`cases_agent_view` | **Q1** 有答案 | ⬜ |
| 0.8 | `phase0.py replicate`：MySQL → Delta，逐表核对 | Claude | BEAVER schema、`benchmark.replication_report` | **Q2** 有答案 | ⬜ |
| 0.9 | `phase0.py compat`：Gold SQL 双引擎兼容性 | Claude | `benchmark.sql_compatibility`、`gold_adaptations` | **Q3** 有答案 | ⬜ |
| 0.10 | `phase0.py gold`：冻结 Gold 结果 | Claude | `benchmark.gold_results` | **Q4、Q5** 有答案 | ⬜ |
| 0.11 | `phase0.py report` 生成报告，一起评审 | 一起 | `reports/phase0_report.md` | 确定方案 A、B 或 C | ⬜ |

**🚧 关卡一**：Q1–Q5 都有基于数据的答案；方案为 A 或 B，而且 PRIMARY case 数量足够支撑后续实验（目标 ≥ 50）。如果是方案 C，先停下来重新评估，不强行推进。

---

## 三、阶段二：基线与观测（Phase 1–3）

**目标**：得到一个"只生成一次"的 Baseline 和它的失败清单，并知道每个失败属于哪一类。

### Phase 1 · Few-shot Baseline

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 1.1 | **LLM 客户端**：智谱 OpenAI 兼容接口；`do_sample=False`；发请求前检查 key 里有没有控制字符；记录 token 和延迟；设置调用和 token 预算上限。可参考 `gmv-rca-agent` 的实现 | `agent/llm.py` | 同一 prompt 调用两次结果相同；超出预算会报错 |
| 1.2 | **表检索**（setting=0）：从 `tables_meta` 里给问题挑出 top-k 张表。BEAVER 一个库有上百张表，不能全部塞进 prompt | `agent/retriever.py` | 只读 `tables_meta`，不读 Gold；k 可配置 |
| 1.3 | **Few-shot 生成器**：prompt 模板、few-shot 示例、SQL 提取 | `agent/generator.py` | few-shot 示例**不能与评测样本重叠**（要做检查）；glm 把拒答包进代码块时能识别；prompt 写明目标方言是 Databricks SQL，并说明 BEAVER 中"方差/标准差"指**总体**统计量（用 `VAR_POP/STDDEV_POP`）。这是评测环境约定，不含任何 Gold 信息 |
| 1.4 | **执行器**：只读校验，在 Databricks 上执行 | `agent/executor.py`（复用 `execution/`） | 写操作会被拒绝 |
| 1.5 | **评测器**：与冻结的 `gold_results` 比较（官方比较规则） | `evaluation/baseline.py` | 只评 PRIMARY case |
| 1.6 | **小样本试点**：约 20 个 case | 试点报告 | 看首次准确率和失败类型分布，决定正式样本规模；是否换模型需要单独决策 |
| 1.7 | **正式 Baseline**：全部 PRIMARY case，每题只生成一次，不重试 | Baseline 结果 | First-pass Accuracy 来自实际运行 |

### Phase 2 · Trace / 可观测性

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 2.1 | 建 Trace 表：原计划的字段，加上 **Verifier 字段**（`verifier_mode`、`verifier_decision`、`verifier_signals`） | `traces.execution_traces` | 每次尝试一行 |
| 2.2 | 建 MLflow 实验：每次运行记录模型、prompt 版本、k 值等参数，以及指标 | MLflow experiment | 从 run 能找到对应的 trace |
| 2.3 | 在 LLM 调用里接入 MLflow Tracing | — | 每次调用的输入、输出、token、延迟可查 |
| 2.4 | 把 Phase 1 的 Baseline 接入 Trace，重跑一次 | — | 任意一次尝试都能靠 trace 还原现场 |

### Phase 3 · Failure Taxonomy（诊断的"标准答案"）

> 这一步**允许读 Gold 标注**，因为它产出的是**评测用的标签**，只给 Evaluation 用，Agent 和 Diagnoser 看不到。

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 3.1 | **自动标注**：对比 Gold 标注和生成结果，按固定优先级（表检索 → 列映射 → Join Key → 领域知识 → 查询分解 → 执行）给每个失败打一个主因 | `benchmark/beaver/subtasks.py` | 每个失败 case 有且只有一个 `actual_failure_type` |
| 3.2 | **人工抽检**：约 20 个 case，确认标注合理 | 抽检记录 | 记录不一致的情况，并修正规则 |
| 3.3 | 出失败类型分布报告 | 分布表 | 知道哪几类失败最多，Repair 优先做哪几个 Skill |

**🚧 关卡二**：Baseline 失败数量足够（目标 ≥ 30 个），而且至少分布在 3 类以上。如果几乎全错或几乎全对，回到 1.6 重新评估样本和模型。

---

## 四、阶段三：闭环本体（Phase 4–6）

**目标**：实现 Observe → Diagnose → Repair → Verify，全程不接触 Gold。

### Phase 4 · Diagnosis

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 4.1 | **Observer**：从 trace 组装诊断输入。输入只能是问题、Schema、检索到的表和列、SQL、执行结果和报错 | `loop_engineer/observer.py` | 类型上就拿不到 Gold 字段（有测试保证） |
| 4.2 | **Diagnoser**：先用规则处理明确信号（执行报错、找不到表或列），再让 LLM 分类；输出 `{failure_type, confidence, reason}` | `loop_engineer/diagnose.py` | 输出格式固定，能被解析 |
| 4.3 | **诊断准确率**：与 Phase 3 的标签对比，出混淆矩阵 | `traces.diagnoses` | Diagnosis Accuracy 来自实际运行 |

### Phase 5 · Repair Skills + Policy

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 5.1 | **Policy**：失败类型 → Repair Skill 的映射，写成配置 | `loop_engineer/policy.py` | 映射可替换，方便做消融 |
| 5.2 | **6 个 Repair Skill**：RetrieveAgain、SchemaSearch、FindJoinPath、RetrieveKnowledge、ReplanQuery、RepairSQL | `skills/*.py` | 每个都输出 `repair_reason / repair_action / repaired_sql / repair_skill` |
| 5.3 | ⚠️ **确定 FindJoinPath 的 Join 信息来源**：只能来自 Schema（MySQL 外键约束、列名匹配），**不能用 BEAVER 的 join_keys 标注** | 设计说明 | 来源经过审查，确认不是 Gold |
| 5.4 | ⚠️ **确定 RetrieveKnowledge 的知识来源**：BEAVER 的 `domain_knowledge` 是 Gold，**不能用**。需要另建非 Gold 的知识库（列取值样例、数据字典，或只用评测样本以外 case 整理出的知识） | 设计说明 | 来源经过审查，确认不是 Gold |
| 5.5 | 每个 Skill 的单元测试，加上"拿不到 Gold"的测试 | `tests/` | 测试通过 |

### Phase 6 · Loop Controller + Verifier

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 6.1 | **SelfVerifier**（主实验）：执行报错、空结果、结果形状异常，可选加 LLM Judge | `loop_engineer/verifier.py` | 不接触 Gold |
| 6.2 | **OracleVerifier**（上界）：与 Gold Result 比对 | 同上 | 结果里带"上界"标记 |
| 6.3 | **Controller**：编排整个闭环；最多 2 次尝试；有预算和终止条件；每一步都写 trace | `loop_engineer/controller.py` | 不会无限循环 |
| 6.4 | **Generic Retry**：与闭环使用同一个 Verifier、同一预算，失败后用通用纠错 prompt（带上报错）重试 | `evaluation/generic_retry.py` | 与 Targeted Loop 的唯一区别是有没有"诊断 + 针对性修复" |
| 6.5 | 在试点 case 上端到端冒烟测试 | — | 能在 trace 里完整看到一个"失败 → 诊断 → 修复 → 验证"的过程 |

**🚧 关卡三**：闭环端到端跑通；代码审查确认 Agent、Diagnoser、Repair、SelfVerifier 都不接触 Gold。

---

## 五、阶段四：实验证明（Phase 7–8）

### Phase 7 · 对照实验

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 7.1 | 实验运行器：同一组 case、同一个模型、同一套配置 | `evaluation/targeted_loop.py` 等 | 一条命令跑一组实验 |
| 7.2 | 主实验：**A** Baseline、**B** Generic Retry、**C** Targeted Loop（Self-verified） | `evaluation.evaluation_results` | 三组都有结果 |
| 7.3 | 上界实验：**B'**、**C'**（Oracle） | 同上 | 结果标注为上界 |
| 7.4 | 指标：First-pass、Final、Recovery Rate、**Harm Rate**、**Net Gain**、Diagnosis Accuracy、Avg Attempts、Tokens、Latency、单位净恢复成本 | 指标表 | 全部来自实际运行 |
| 7.5 | 拆解分析：按失败类型的恢复率、诊断 × 修复矩阵、**Verifier 混淆矩阵**、Oracle 与 Self-verified 的差距 | 分析报告 | 能说清楚闭环的瓶颈在哪一环 |
| 7.6 | 稳定性：同一配置跑两次 | — | 结果一致；如有差异，要找到原因 |
| 7.7 | 把实验配成 Databricks Job | Job 定义 | 一键重跑 |

### Phase 8 · 消融实验

| 步骤 | 做什么 | 产出 | 完成标准 |
|---|---|---|---|
| 8.1 | 去掉 Diagnosis、去掉 Policy、逐个去掉 Repair Skill、去掉 Retry | `evaluation/ablation.py` | 每种消融都跑完 |
| 8.2 | 以 **Net Gain 的下降幅度**衡量每个部件的贡献 | `evaluation.ablation_results` | 能回答"哪个部件真正起作用" |

**🚧 关卡四**：主实验、上界实验和消融结果齐全、可复现。**不论 C 是否优于 B，都如实报告。**

---

## 六、阶段五：展示与交付（Phase 9 + 作品集）

### Phase 9 · Loop Debug Console

| 步骤 | 做什么 | 完成标准 |
|---|---|---|
| 9.1 | 总览页：Phase 7 的核心指标（包括 Harm Rate、Net Gain） | 数字直接读自 Delta 表 |
| 9.2 | **Case Trace 页**：Attempt #1 → 诊断 → Skill → Attempt #2，附修复前后 SQL 对比 | 能打开任意 case 查看 |
| 9.3 | 失败分析页：失败类型分布、诊断 × 修复矩阵 | — |
| 9.4 | 部署为 Databricks App | 注意 Free Edition 限制：只能有 1 个 App，运行 24 小时后自动停止 |

### 作品集材料

- [ ] README 和闭环架构图
- [ ] 实验报告（主实验表、上界表、6 项拆解分析、消融表）
- [ ] Demo 录屏（重点是 Case Trace）
- [ ] 2 分钟和 10 分钟面试话术、简历 bullet（填真实数字）、高频追问的答案

---

## 七、阶段六：外层循环（Phase 10–12，加分项）

| Phase | 做什么 | 关键要求 |
|---|---|---|
| 10 Experience Store | 把修复成功的 trace 存进 `experience.repair_cases` | 只存闭环自己产生的数据，不存 Gold |
| 11 Learning Loop | 分析修复模式，用来更新检索、few-shot 示例和 Policy | ⚠️ **必须划分学习集和测试集**：从一部分 case 学到的经验，只能在另一部分 case 上评估，否则就是数据泄漏 |
| 12 Regression Benchmark | 每次改进后跑回归测试 | 改进不能让原来做对的 case 退步 |

---

## 八、当前待办

| 优先级 | 事项 | 谁 |
|---|---|---|
| 1 | 0.2–0.5：HF 申请、Databricks 登录、`.env`（包括智谱 key）、导入 MySQL | 你 |
| 2 | 0.6–0.11：运行 Phase 0，评审报告 | Claude，之后一起评审 |
| 3 | Phase 0 运行期间可以并行：Phase 1 的 1.1–1.4 代码（LLM 客户端、检索、生成、执行），不依赖 Phase 0 的结果 | Claude |

## 九、待决策事项

| 事项 | 最晚什么时候定 |
|---|---|
| Phase 0 是否改成由课程提供操作指南或脚本 | 课程设计阶段 |
| 作品集交付清单是否纳入课程要求 | Phase 9 之前 |
| FindJoinPath 的 Join 信息来源（5.3） | Phase 5 之前 |
| RetrieveKnowledge 的知识来源（5.4） | Phase 5 之前 |
