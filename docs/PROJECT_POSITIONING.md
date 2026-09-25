# 项目定位：Loop Engineering

> Self-Healing Text-to-SQL Agent with Failure Diagnosis and Targeted Repair
>
> 本文档说明这个项目**为什么做、核心贡献是什么、价值靠什么证明**。技术实现见 [README](../README.md)。

---

## 0. 一句话

> **这是一个 Loop Engineering 项目，Text-to-SQL 只是实验载体。**
>
> 项目要回答的问题是：当 Agent 失败时，一个**设计过的闭环**（观察 → 诊断 → 针对性修复 → 验证），能否比**盲目重试**恢复更多失败？每恢复一次要付出多少代价？闭环里的哪个部件真正起了作用？

各部分的角色：

| 组成 | 角色 |
|---|---|
| **Loop Engineering** | 主角，项目的核心贡献 |
| Text-to-SQL | 实验载体：对错能自动判定，所以闭环效果可以精确量化 |
| BEAVER | 失败来源：企业级难度，失败多且类型丰富，让闭环有东西可修 |
| Databricks | 运行底座：执行、trace、实验记录和展示都在一个平台内完成 |

---

## 1. 什么是 Loop Engineering

### 1.1 和 Retry 的区别

大多数 Agent 处理失败的方式是 **Retry**：失败了就再调一次 LLM，最多是把报错信息附上。

**Loop Engineering** 把"失败后怎么办"当作一个**需要设计和度量的工程系统**，而不是一行 `for` 循环：

| | Retry | Loop Engineering |
|---|---|---|
| 失败后做什么 | 同样的方式再来一次 | 先判断**为什么失败**，再选**对应的修复手段** |
| 使用的信息 | 最多附上报错 | 结构化的观测：检索了什么、生成了什么、执行结果如何 |
| 停止条件 | 固定次数 | 明确的预算和终止条件 |
| 是否可度量 | 只看最终准确率 | 每个环节都有指标：诊断准不准、修复成功率、单位成本 |
| 能否改进 | 不能 | 成功的修复成为下一轮系统改进的数据 |

### 1.2 两层循环

```
┌─────────────────────── Outer Loop：Learning（跨 case，Phase 10+）───────────────────────┐
│                                                                                         │
│   ┌──────────────── Inner Loop：Self-Healing（单个 case 内）────────────────┐           │
│   │                                                                         │           │
│   │   Generate ──► Execute ──► Verify ──PASS──► END                         │           │
│   │      ▲                        │                                         │           │
│   │      │                       FAIL                                       │           │
│   │      │                        ▼                                         │           │
│   │   Repair Skill ◄── Policy ◄── Diagnose ◄── Observe                      │           │
│   │   (bounded by attempt budget)                                           │           │
│   └─────────────────────────────────────────────────────────────────────────┘           │
│                     │ successful repair traces                                          │
│                     ▼                                                                   │
│   Experience Store ──► Pattern Mining ──► Update Retriever / Examples / Policy          │
│                     ──► Regression Benchmark（改进不能让旧 case 退步）                   │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Inner Loop（Self-Healing）**：一个问题内，失败后自我修复。这是项目的主体。
- **Outer Loop（Self-Improving）**：跨问题积累经验，让系统下一次少犯同样的错。作为加分项。

### 1.3 闭环的部件和各自的设计问题

一个闭环好不好，取决于每个部件的设计。**这张表就是项目的技术核心**，也是面试深挖时的主要话题：

| 部件 | 职责 | 关键设计问题 | 对应指标 |
|---|---|---|---|
| **Observer** | 采集失败现场 | 记录哪些信号才足以诊断？（检索的表、SQL、报错、结果形状） | Trace 完整性 |
| **Verifier** | 判断这次对不对，决定是否进入修复 | **触发信号从哪来？**（见 1.4，最关键的设计决策） | 触发精度 / 召回、误伤率 |
| **Diagnoser** | 判断失败根因（6 类 Failure Type） | 只用非 Gold 信息，能诊断多准？ | Diagnosis Accuracy |
| **Policy** | 把根因映射到修复手段 | 固定映射，还是按置信度选择？ | 修复选择的正确率 |
| **Repair Skills** | 针对性修复（RetrieveAgain、FindJoinPath……） | 每个 Skill 能拿到什么信息？修复什么？ | 每个 Skill 的恢复率 |
| **Controller** | 编排与终止 | 预算多大？什么时候放弃？ | Avg Attempts、成本 |
| **Learner**（Outer） | 把成功修复变成系统改进 | 如何防止过拟合和退步？ | 回归通过率 |

### 1.4 最关键的设计决策：Verifier 的触发信号 ⚠️

闭环在"PASS?"这一步需要知道这次对不对。**这个信号从哪来，决定了实验结论是否成立。**

**决策：主实验采用 Self-verified，Oracle 只做明确标注的上界分析。**

| 模式 | 触发信号 | 地位 |
|---|---|---|
| **Self-verified** | 不看 Gold。只用执行报错、空结果、结果形状异常，可选再加 LLM Judge | **主实验**。与生产环境拿到的信号一致，所有主结论都来自这里 |
| **Oracle-triggered** | 与 Gold Result 比对 | **补充分析**。泄漏了"这次错了"这 1 bit 的 Gold 信息，只回答"如果系统知道自己错了，修复能力上限是多少"，不能用作主结论 |

#### 为什么这样定：业界和学界的共识

| 来源 | 结论 |
|---|---|
| Huang et al., *Large Language Models Cannot Self-Correct Reasoning Yet*（ICLR 2024） | 早期不少 self-correction 工作用 Gold 决定何时停止修正，效果因此被高估；去掉 oracle 后，仅靠模型自我反思往往没有提升，甚至会把对的改错 |
| Kamoi et al., *When Can LLMs Actually Correct Their Own Mistakes?*（TACL 2024，综述） | 反馈分三类：oracle 反馈不现实；**外部反馈**（执行、测试、工具）可靠有效；纯内在反思多数无效，除非"验证"明显比"生成"容易 |
| Text-to-SQL 方法（DIN-SQL、MAC-SQL、Self-Debugging、CHESS、ReFoRCE 等） | 修正都由执行反馈、自我解释或多候选一致性驱动，不接触 Gold；BIRD、Spider 的规则也禁止推理时使用 Gold |
| 工业界（Databricks Genie、Snowflake Cortex Analyst、开源 Text-to-SQL 工具） | 常见做法是 SQL 执行报错后把错误信息喂回去重试，再叠加 LLM Judge 校验或用户反馈 |

> 以上文献结论整理自记忆，写入正式材料前请核对原文。

#### 两种模式合起来看，能回答什么

- **Self-verified** 衡量"**发现错误 + 修复**"的真实能力。
- **Oracle** 衡量"**修复**"能力的上限。
- **两者的差距**揭示瓶颈所在：是"发现不了错"，还是"修不好"。

#### 新增风险指标：误伤率（Harm Rate）

Self-verified 会误判：可能把本来正确的结果判为"有问题"，再把它"修"坏。Huang et al. 指出的正是这种现象。因此必须单独度量：

```
误伤率 (Harm Rate) = 第一次正确、但闭环结束后变错的 case 数 / 第一次正确的 case 数
```

**净收益**（Net Gain）= 恢复的 case 数 − 误伤的 case 数。

只报告 Recovery Rate 而不报误伤率，会掩盖闭环的真实代价。

#### 公平对比的要求

- Generic Retry 与 Targeted Loop 必须使用**同一种触发信号、同一调用预算**。
- 主实验的 Generic Retry 就是业界现状："执行报错后带错误信息重试"。本项目的增量在于：在同样真实的信号下，多了"**诊断根因 + 针对性修复**"。实验要证明的正是这一步的价值和代价。

> 这一点能把项目和"用答案判卷后再重做"的 demo 区分开。面试官如果问"生产环境没有 Gold，你的 Loop 怎么知道该修？"，这里就是答案。

---

## 2. 为什么是现在，为什么对求职有用

### 2.1 求职市场

海外 AI 岗位（AI Engineer、Applied AI、Agent Engineer、Data + AI Engineer）的面试官已经见过大量"LLM 生成 X"的 demo，他们追问的是：

1. **Agent 失败了怎么办？**：这正是 Loop Engineering 要回答的
2. **你怎么证明它有效？**：对照实验和消融
3. **代价是什么？**：单位恢复成本

### 2.2 技术趋势

Agent 的能力瓶颈已经从"能不能生成"转向"**失败后能否可靠恢复**"。Agent 越长、越自主，单步错误就越会累积，**闭环的设计质量决定了系统的可靠性上限**。

### 2.3 项目性质

这是面向**海外求职学员**的项目背书课程的交付物，需要同时服务两类人：

| 对象 | 看重什么 |
|---|---|
| 面试官 | 闭环设计是否有想法、是否有对照实验和真实数据、是否理解失败与成本 |
| 学员 | 在有限时间内能复现，有能讲的成品和故事 |

---

## 3. 三个价值，都落在 Loop 上

### 价值 1：端到端成品，核心展示的是"闭环在工作"

| 面试官会看的 | 项目中的对应物 |
|---|---|
| **一个真实的闭环过程** | **Loop Debug Console** 的 Case Trace（示例）：Attempt #1 失败 → 诊断为 JOIN_KEY_FAILURE（置信度 0.87）→ Policy 选择 FindJoinPath → Attempt #2 修复前后 SQL 对比 → RECOVERED |
| 闭环的整体效果 | 总览页：First-pass、Final Accuracy、Recovery Rate、**Harm Rate**、Net Gain、Diagnosis Accuracy、单位恢复成本 |
| 能读的代码 | `loop_engineer/`（observer、diagnose、policy、controller）和 `skills/` 目录结构，清楚表明闭环是设计出来的 |
| 能复现的实验 | Databricks Jobs 一键重跑三组实验，MLflow 记录 |

> **面试时最有力的一步：打开一个真实失败的 case，讲它如何被观察、诊断、修复、验证。** 这 30 秒比任何架构图都更能说明"我做的是 Loop Engineering"。

### 价值 2：AI 技术功底，体现在"如何证明闭环有效"

| 能力 | 在 Loop 上的体现 |
|---|---|
| **闭环设计** | 6 类 Failure Taxonomy → Policy → 6 个 Repair Skill；有预算和终止条件 |
| **对照实验** | Baseline / Generic Retry / Targeted Loop 三组，**同调用预算**，排除"只是多调一次 LLM" |
| **消融实验** | 逐个移除 Diagnosis、Policy、各个 Repair Skill，定位**闭环中真正产生价值的部件** |
| **诊断能力的独立度量** | Diagnosis Accuracy 单独计算：不仅证明"能修"，还要证明"知道为什么错" |
| **防泄漏** | Diagnoser 和 Repair Skill 不接触 Gold；主实验的触发信号也不接触 Gold（Self-verified），Oracle 结果单独标注为上界 |
| **理解闭环的风险** | 除恢复率外，同时度量误伤率（把对的改错），报告净收益 |
| **可观测性** | 每次尝试完整 trace，是 Observer 的输入，也是 Outer Loop 的训练数据 |

> **Phase 0（Benchmark Qualification）在这个叙事里的位置**：它构建的是闭环的**可信裁判**。Loop 效果的每个数字都依赖 Gold Result 可靠，所以要用 BEAVER 官方引擎 MySQL 做对照，验证 Databricks 上 Gold 的语义没有漂移，并且不修改任何 Gold SQL。

### 价值 3：落地意义，用数据说明闭环值不值

**业务语言：**

- **痛点**：企业自然语言查数据，第一次生成的 SQL 常常出错。出错后要么由数据分析师人工修，要么用户放弃。
- **闭环的价值**：系统自己发现错误、判断错因、针对性修复，减少人工介入。
- **落地必须回答**：每多恢复一个 case 要多花多少 token 和延迟？哪类失败值得进入闭环，哪类应该直接转人工？

**主实验对比表：Self-verified 触发**（数字全部来自实际运行，当前为空）：

| 实验 | First-pass | Final | Recovery Rate | Harm Rate | Net Gain | Avg Attempts | Tokens | Latency |
|---|---|---|---|---|---|---|---|---|
| A. Baseline | ? | = First-pass | — | — | — | 1 | ? | ? |
| B. Generic Retry（业界现状：报错后带错误信息重试） | 同 A | ? | ? | ? | ? | ? | ? | ? |
| C. Targeted Loop（诊断 + 针对性修复） | 同 A | ? | ? | ? | ? | ? | ? | ? |

**补充分析表：Oracle 触发（上界，不作为主结论）**

| 实验 | Final | Recovery Rate | 与主实验的差距 |
|---|---|---|---|
| B'. Generic Retry（Oracle） | ? | ? | ? |
| C'. Targeted Loop（Oracle） | ? | ? | ? |

Oracle 触发时只修"确实错了"的 case，所以不存在误伤，Harm Rate 恒为 0，表中不列。

**闭环专属的拆解分析：**

1. **按 Failure Type 拆 Recovery Rate**：闭环在哪类失败上有效，在哪类上无效。
2. **诊断 × 修复矩阵**：诊断对且修复成功、诊断对但修复失败、诊断错但碰巧修好……定位闭环的瓶颈在哪一环。
3. **Verifier 的混淆矩阵**：触发了且确实错（命中）、触发了但其实对（误报，误伤的来源）、没触发但其实错（漏报）。这张表说明 Self-verified 信号的质量。
4. **单位恢复成本**：(C 的额外 tokens) ÷ (C 的净收益 case 数)，与 B 对比。
5. **Oracle 与 Self-verified 的差距**：瓶颈是"发现不了错"还是"修不好"。
6. **消融表**：去掉每个部件后净收益的下降幅度。

> **如果 Targeted Loop 并不比 Generic Retry 好，不算失败。**
> 能用诊断 × 修复矩阵讲清楚"闭环卡在哪一环、为什么"，恰恰是 Loop Engineering 的专业体现。前提是结果真实，不能凑数字。

---

## 4. 面试叙事模板（围绕 Loop）

### 4.1 2 分钟版

1. **问题**：Agent 失败后，业界常见做法是"执行报错后带错误信息重试"，既不判断为什么错，也很少度量重试是否有效、会不会把对的改错。
2. **我的做法**：我把失败处理设计成一个闭环系统：Observer 采集失败现场，Diagnoser 判断 6 类根因，Policy 选择对应的 Repair Skill，Controller 在预算内编排并验证。
3. **关键设计**：生产环境没有标准答案，所以主实验只用 self-verification 触发（执行报错、空结果、结果异常），与业界现状使用同一种信号。Oracle 触发只作为上界，两者的差距揭示瓶颈在"发现错误"还是"修复错误"。
4. **怎么证明有效**：在企业级 benchmark BEAVER 上，与同触发信号、同预算的 Generic Retry 对照；同时报告误伤率和净收益，并逐个消融闭环部件。
5. **结果**：Targeted Loop 的净收益比 Generic Retry 高 X 个百分点，误伤率 H%，诊断准确率 Y%，每次净恢复成本 Z token。瓶颈在 W 环节，因为……

### 4.2 简历 bullet 模板

数字留空，跑完实验后再填：

- Designed a **self-healing loop** for a Text-to-SQL agent (observe → diagnose → targeted repair → verify) that uses only production-available signals, recovering **X%** of first-pass failures on the BEAVER enterprise benchmark vs **Y%** for budget-matched error-feedback retry, at a **H%** harm rate.
- Built failure diagnosis over a 6-class taxonomy (**Z%** diagnosis accuracy) and ablated each loop component to isolate where the net gain comes from.
- Quantified the gap between self-verified and oracle-triggered loops to separate error *detection* from error *repair* as the bottleneck.

### 4.3 高频追问

| 追问 | 答案来源 |
|---|---|
| 生产环境没有 Gold，你的 Loop 怎么知道该修？ | 主实验本来就只用 Self-verified 触发；Verifier 混淆矩阵；与 Oracle 的差距分析（1.4） |
| 你的结果会不会像早期 self-correction 论文那样被高估？ | 不用 Gold 触发（Huang et al. 2024 指出的问题）；同时报告误伤率和净收益 |
| 闭环会不会把对的改错？ | Harm Rate 和 Verifier 混淆矩阵中的误报 |
| 这和 retry 有什么本质区别？ | 同触发信号、同预算的对照实验（B vs C）和诊断 × 修复矩阵 |
| 诊断本身准不准？错了怎么办？ | Diagnosis Accuracy；诊断错时的修复结果 |
| 哪个部件最有用？ | 消融表 |
| 闭环会不会无限循环、成本失控？ | Attempt 预算、终止条件、单位恢复成本 |
| 怎么让系统越来越好？ | Outer Loop：Experience Store 和 Regression Benchmark |
| 怎么保证没有泄漏？ | setting=0、`AgentTask`、`cases_agent_view`；Diagnoser 和 Repair 无 Gold 访问 |

---

## 5. 对原计划的影响（待决策）

### 5.1 Verifier 触发信号写进计划 ✅ 已定

原计划的 Loop Controller 写的是"Evaluate → PASS?"，但没有说明这个判断是否使用 Gold Result。

**已定方案**（依据见 1.4）：

- **Phase 6**：Controller 中的 Verifier 设计为可插拔部件。
  - `SelfVerifier`（主实验）：执行报错、空结果、结果形状异常，可选加 LLM Judge。**不接触 Gold。**
  - `OracleVerifier`（补充分析）：与 Gold Result 比对，结果必须标注为上界。
- **Phase 2**：Trace 记录每次 Verifier 的判定和依据，以便计算 Verifier 混淆矩阵。
- **Phase 7**：主实验 A / B / C 使用 Self-verified；补充实验 B' / C' 使用 Oracle。
- **指标**：在原有指标之上新增 **Harm Rate（误伤率）** 和 **Net Gain（净收益）**；消融以净收益为准。

### 5.2 学员投入要集中在 Loop 上

| 阶段 | 与 Loop 的关系 | 学员投入建议 |
|---|---|---|
| Phase 0 | 构建闭环的可信裁判 | 严谨，但由课程提供指南或脚本，尽量省时 |
| Phase 1–2（Baseline、Trace） | 闭环的起点和 Observer | 必做，保持简单 |
| **Phase 3–8（Taxonomy、Diagnosis、Repair、Controller、对照实验、消融）** | **闭环本体** | **主要投入** |
| Phase 9（Dashboard） | 闭环的展示窗口 | 必做，重点是 Case Trace |
| Phase 10+（Learning Loop） | Outer Loop | 加分项 |

### 5.3 学员复现门槛过高

Phase 0 需要 HuggingFace 数据申请（可能人工审批）、本机 MySQL 导入约 262 MB 数据、配置 Databricks，是课程最大的流失点。

**建议**：课程方提供验证过的指南或一键脚本。数据仍须学员自行申请，不能绕过 BEAVER 的访问条款。

### 5.4 Baseline 可能过低，闭环没有东西可修

BEAVER 难度很高。few-shot baseline 如果几乎全错，大部分失败可能超出任何修复手段的能力，闭环效果就难以体现。

**建议**：Phase 1 先跑小样本试点，确认失败类型的分布，再确定样本规模和分析口径。任何调整都必须如实披露，不能挑对自己有利的 case。

### 5.5 作品集交付清单

- [ ] README 与闭环架构图（1.2 节的两层循环）
- [ ] 实验报告（第 3 节"价值 3"的主实验表、上界表和 6 项拆解分析）
- [ ] Loop Debug Console 与 Demo 录屏（重点是 Case Trace）
- [ ] 2 分钟和 10 分钟面试话术
- [ ] 简历 bullet（填入真实数字）
- [ ] 高频追问的答案准备

---

## 6. 待决策事项

| # | 事项 | 状态 |
|---|---|---|
| 1 | 主实验采用 Self-verified，Oracle 仅作上界分析；新增 Harm Rate 和 Net Gain | ✅ 已定 |
| 1b | LLM 使用外部 API：**智谱**（详见第 7 节） | ✅ 已定 |
| 1c | 当前使用免费的 `glm-4-flash`；Phase 1 试点后再评估是否升级 | ✅ 已定（可复议） |
| 2 | Phase 0 改为"课程提供指南或脚本、学员按步骤完成" | 待定 |
| 3 | Phase 1 先做小样本 baseline 试点，再确定样本规模 | 待定 |
| 4 | 作品集交付清单纳入课程要求 | 待定 |

---

## 7. LLM 选型：外部 API（智谱）✅ 已定

### 7.1 决策

所有 LLM 调用（生成、诊断、修复、可选的 LLM Judge）都走**智谱开放平台**的 OpenAI 兼容接口：

- 地址：`https://open.bigmodel.cn/api/paas/v4`
- 凭据：`ZHIPUAI_API_KEY`。本机放在 `.env`，Databricks 上放在 secret scope。不写进代码，也不写进日志

### 7.2 对实验设计的要求

| 要求 | 原因 |
|---|---|
| **关闭采样**（`do_sample=False`，贪婪解码） | 同一个 prompt 两次必须得到同一个答案，否则 A/B/C 的差异无法归因。`gmv-rca-agent` 实测过：不关采样时，同样发 "hi" 两次回复不同 |
| **所有实验组使用同一个模型** | 生成、诊断、修复、Generic Retry 都用同一个模型，对比才公平。换模型要作为单独的实验变量 |
| **模型名写进 run 记录和 MLflow 参数** | 不同模型的结果不能混在一起比较 |
| **每次调用记录 token 和延迟** | 单位恢复成本的数据来源 |
| **设置调用和 token 预算上限** | 防止闭环或批量实验失控 |

### 7.3 模型选择：当前使用 `glm-4-flash` ✅

- **默认 `glm-4-flash`**：免费，学员复现零成本，`gmv-rca-agent` 已在同一个 Databricks 环境里验证过可用。
- **风险**：模型偏弱，BEAVER 又很难。baseline 可能接近全错，而且大部分失败超出修复能力，闭环就体现不出价值。
- **做法**：Phase 1 先用 `glm-4-flash` 跑小样本试点，看首次准确率和失败类型分布，再决定是否换更强的智谱模型。如果换模型，要如实披露理由，并在全部实验组统一使用新模型。

### 7.4 已知的坑（来自 `gmv-rca-agent` 的实测记录）

| 坑 | 处理 |
|---|---|
| Databricks Free Edition 能否访问外网 | 已实测：serverless 可以访问 `open.bigmodel.cn` |
| 在 cmd 的隐藏输入提示里按 Ctrl+V 粘贴 API key，会把控制字符一起存进去，结果请求被网关返回 HTML 400 | 客户端发请求前检查 key 里是否有控制字符（智谱 key 正常是 49 位）；写入 secret 时改用 PowerShell 变量加 `--string-value` |
| `glm-4-flash` 会把拒答也包进 ```` ```sql ```` 代码块里 | 提取 SQL 后要做只读校验和语法校验，不能假设代码块里一定是 SQL |

### 7.5 数据出境说明

BEAVER 的问题、表结构和示例行会发送给智谱 API。BEAVER 数据本身已经匿名化，BEAVER 官方仓库也提供基于外部 API 的 baseline；即便如此，课程材料里仍应提醒学员：**确认自己的数据使用方式符合 BEAVER 的访问条款**。
