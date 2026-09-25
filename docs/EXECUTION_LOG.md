# 执行记录

> 按步骤执行，每做完一步就把结果填进对应的「执行结果」里。
> 路线图和每步的完成标准见 [ROADMAP.md](ROADMAP.md)。
>
> ⚠️ **不要把密码、token、API key 写进这个文件**。需要记录时只写"已配置"或长度（如"key 长度 49"）。
>
> 状态图例：⬜ 未开始 · 🔄 进行中 · ✅ 完成 · ❌ 失败 · ⏸ 等待中

---

## Phase 0 · 准备与运行

命令默认在 `D:\Course\DataBricksTest\self-healing-text2sql` 目录下运行。

### 进度总览

| 步骤 | 内容 | 谁 | 状态 | 完成日期 |
|---|---|---|---|---|
| 1 | 申请 BEAVER 数据访问（HuggingFace） | 你 | ✅ | 2026-09-25 |
| 2 | 重新登录 Databricks | 你 | ✅ | 2026-09-25 |
| 3 | 配置 `.env`（MySQL 密码、智谱 key） | 你 | ✅ | 2026-09-25 |
| 4 | 下载 BEAVER 数据库并导入 MySQL | 你 | ✅ | 2026-09-25 |
| 5 | 运行 Phase 0 并评审报告 | Claude | ✅ | 2026-09-25 |

---

### 第 1 步：申请 BEAVER 数据访问（HuggingFace）

**作用：** 拿到 BEAVER 题库和数据库的下载权限。

- [ ] 1.1 注册或登录 HuggingFace：https://huggingface.co/join
- [ ] 1.2 打开题库页面，点 **"Agree and access repository"**：https://huggingface.co/datasets/beaverbench/beaver-query
- [ ] 1.3 打开表结构和数据库页面，点 **"Agree and access repository"**：https://huggingface.co/datasets/beaverbench/beaver-table
- [ ] 1.4 创建 **Read** 类型的 token：https://huggingface.co/settings/tokens
- [ ] 1.5 终端登录，按提示粘贴 token（建议鼠标右键粘贴）：
  ```bash
  ..\.venv\Scripts\hf.exe auth login
  ```
- [ ] 1.6 检查：显示用户名；两个数据集页面能看到文件列表
  ```bash
  ..\.venv\Scripts\hf.exe auth whoami
  ```

> 如果页面显示 "pending"，说明需要作者人工审批，可以先做第 2、3 步。

**执行结果：**

| 项目 | 结果 |
|---|---|
| 执行日期 | 2026-09-25 |
| beaver-query 访问状态（已通过 / pending） | ✅ 已通过（`auth_check` 验证）。文件：`data/dw`（6.3 MB）、`dw_real`、`neutron`、`nova` 四个 split 的 parquet |
| beaver-table 访问状态（已通过 / pending） | ✅ 已通过（`auth_check` 验证）。文件：`beaver_db.zip`（261.6 MB），以及 `dw`、`neutron`、`nova` 的表结构 parquet |
| `hf auth whoami` 输出 | `✓ Logged in  user: MuShan795` |
| 遇到的问题与处理 | 浏览器方式登录时终端一直显示 waiting：需要在浏览器打开 https://hf.co/oauth/device 输入终端显示的代码完成授权，终端才会继续（见问题记录 #1） |

---

### 第 2 步：重新登录 Databricks

**作用：** 让本机能连上你的 Databricks 工作区。

- [ ] 2.1 确认浏览器能登录工作区：https://dbc-2beb6eae-4266.cloud.databricks.com
- [ ] 2.2 终端登录（会弹出浏览器，登录并授权）：
  ```bash
  databricks auth login --host https://dbc-2beb6eae-4266.cloud.databricks.com
  ```
- [ ] 2.3 检查：能显示你的邮箱
  ```bash
  databricks current-user me
  ```
- [ ] 2.4 检查：至少有一个 SQL warehouse
  ```bash
  databricks warehouses list
  ```

**执行结果：**

| 项目 | 结果 |
|---|---|
| 执行日期 | 2026-09-25 |
| `current-user me` 是否成功 | ✅ `mushan.ysl@gmail.com`；Python SDK（profile `DEFAULT`）也认证成功 |
| SQL warehouse 名称和状态 | Serverless Starter Warehouse（2X-Small，`/sql/1.0/warehouses/0e96e1d4ab3f34b0`），STOPPED。serverless 有查询时会自动启动 |
| 遇到的问题与处理 | 登录时提示 `Databricks profile name [dbc-2beb6eae-4266]`：要输入 `DEFAULT`，与 `config/phase0.yaml` 的 `profile: DEFAULT` 保持一致（见问题记录 #2）。现有 catalog：`workspace`、`system`、`samples` |

---

### 第 3 步：配置 `.env`（MySQL 密码、智谱 key）

**作用：** 本机 MySQL 用作 BEAVER 官方引擎的对照；智谱 key 留给 Phase 1 调用模型（`glm-4-flash`）。

- [ ] 3.1 复制模板：
  ```bash
  copy .env.example .env
  ```
- [ ] 3.2 用记事本打开 `.env`，填上 `MYSQL_PASSWORD=`（安装 MySQL 时设的 root 密码）
- [ ] 3.3 登录智谱开放平台：https://open.bigmodel.cn ，进入控制台的 **「API Keys」** 页面创建 key
- [ ] 3.4 把 key 填到 `.env` 的 `ZHIPUAI_API_KEY=`，确认长度是 **49 位**
- [ ] 3.5 检查 MySQL（输入密码后能显示 `8.0.25`）：
  ```bash
  & "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u root -p -e "SELECT VERSION();"
  ```

**执行结果：**

| 项目 | 结果 |
|---|---|
| 执行日期 | 2026-09-25 |
| `.env` 已创建（是 / 否） | ✅ 是；各项值都没有引号和控制字符 |
| MySQL 版本输出 | ✅ `8.0.25`（项目代码 `MySqlExecutor` 连接成功）。已有库 `itstack`、`wechat`、`qgydb` 都在；`lower_case_table_names=1`；`max_allowed_packet=4MB` |
| 智谱 key 已配置（只写长度） | ✅ 长度 49；`glm-4-flash` 试调用 HTTP 200，26 tokens，约 2.8 秒 |
| 遇到的问题与处理 | root 密码未知，已通过 Workbench 保存的连接执行 `ALTER USER` 重设（问题记录 #3）。Windows 终端 GBK 编码打印模型回复里的 emoji 报错，`scripts/phase0.py` 已改为 UTF-8 输出（问题记录 #4） |

---

### 第 4 步：下载 BEAVER 数据库并导入 MySQL

**作用：** 把 BEAVER 的真实数据放进本机 MySQL，作为对照基准。**需要第 1 步审批已通过。**

- [ ] 4.1 下载并解压（约 262 MB，解压到 `data\beaver_db\`，并打印每个 `.sql` 文件的导入命令）：
  ```bash
  ..\.venv\Scripts\python.exe scripts\fetch_beaver_db.py
  ```
- [ ] 4.2 打开 **cmd 窗口**（PowerShell 不支持 `<` 重定向），对打印出的每个 `.sql` 文件执行一次，每次都要输入密码：
  ```
  "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u root -p --default-character-set=utf8mb4 < "D:\...\xxx.sql"
  ```
- [ ] 4.3 检查：能看到 BEAVER 的数据库（如 `dw`）
  ```bash
  & "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u root -p -e "SHOW DATABASES;"
  ```

> 如果报错 "No database selected"，说明导出文件里没有建库语句，把报错贴到下面，交给 Claude 处理。

**执行结果：**

| 项目 | 结果 |
|---|---|
| 执行日期 | 2026-09-25 |
| 下载是否成功（文件大小） | ✅ `beaver_db.zip` 261.6 MB（由 Claude 运行 `fetch_beaver_db.py` 下载） |
| 解压出的 `.sql` 文件列表 | `dw.sql` 92.6 MB（97 张表，153 条 INSERT）、`neutron.sql` 6.1 MB（175 张表）、`nova.sql` 2.05 GB（109 张表） |
| 每个文件的导入耗时 / 是否成功 | ✅ 只导入了 `dw.sql`（你在 cmd 里手动执行）。验证：97 张表，共 421,707 行，单表最多 10,000 行；5 张空表（`estimated_surcharges_estonly`、`fund_center_hierarchy`、`opa_person_current`、`profit_center_group`、`subject_selector`）在 dump 里本来就没有 INSERT，属于正常。`neutron`、`nova` 暂不导入 |
| 遇到的问题与处理 | 压缩包在 macOS 上打的，带有 `__MACOSX/`、`._*`、`.DS_Store` 垃圾文件，脚本已改为跳过（问题记录 #5）。dump 由 MySQL 9.1/9.3 导出，`dw.sql` 只用了 varchar/int/float、`utf8mb4_0900_ai_ci`、InnoDB，与本机 8.0.25 兼容；最长一行约 1 MB，小于 `max_allowed_packet`=4 MB，**不需要调大** |

`SHOW DATABASES` 输出：

```
dw, information_schema, itstack, mysql, performance_schema, qgydb, sys, wechat
```

---

### 第 5 步：运行 Phase 0（Claude 执行）

**作用：** 回答 Q1–Q5，判定走方案 A、B 还是 C。第 1–4 步完成后告诉 Claude"准备完成"。

| 小步骤 | 命令 | 产出 | 回答 | 状态 | 结果摘要 |
|---|---|---|---|---|---|
| 5.1 环境检查 | `phase0.py env` | catalog、schema、volume | 前置检查 | ✅ | catalog `self_healing_text2sql` 创建成功（不需要改用 `workspace`）；4 个 schema 和 staging volume 已建；DBSQL 2026.36；`UTF8_LCASE` 可用 |
| 5.2 导入题库 | `phase0.py import` | `benchmark.cases`（100 个 case） | Q1 | ✅ | 从 dw 全部 5,787 题中按官方种子 77 抽样 100 题：复杂查询 43、领域复杂查询 48、领域查询 9；57 题含领域知识；每题 Gold 涉及 2–6 张表。`tables_meta` 97 行；`cases_agent_view` 已建。Gold SQL 大量使用窗口函数和多层 CTE，有 `VARIANCE`（MySQL 是总体方差，Databricks 是样本方差，属于潜在语义差异） |
| 5.3 复制数据库 | `phase0.py replicate` | Databricks 里的 BEAVER 表、逐表核对报告 | Q2 | ✅ | 97 张表全部复制，行数一致（421,707 = 421,707），无零日期。95 张表逐列核对完全一致；2 张表的"不同值个数"差 1–6 个（`course_catalog_subject_offered.SUBJECT_DESCRIPTION`、`tip_material.TITLE / AUTHOR`）。原因已确认是**重音不敏感**：MySQL `utf8mb4_0900_ai_ci` 把 `Gödel`/`Godel`、`Gérard`/`Gerard` 视为同一个值，`UTF8_LCASE` 只忽略大小写（已知限制，共涉及 8 个值）。是否影响 Gold 结果由 5.4 判定 |
| 5.4 兼容性验证 | `phase0.py compat` | 每条 Gold SQL 的兼容性分类 | Q3 | ✅ | **第 2 次（方案 B，正式结果）** run `compat-20260925T075328-c39909`：原始 Gold SQL 不改直接执行能跑通 94/100；**结果与 MySQL 一致 46/100**（新比较规则多认出 9 条只差数字精度的）。分类：COMPATIBLE 46、INCOMPATIBLE_SEMANTICS 48、INCOMPATIBLE_FUNCTION 5、INCOMPATIBLE_SCHEMA 1、UNKNOWN 0。规则改写后结果与 MySQL 一致 **43 条**（总体方差规则 38、窗口帧规则 5；包括之前卡住的 dw_4004）。**可用 89/100**；**排除 11 条**：6 条没有适用规则（并列值排序或日期解析导致结果不唯一：dw_4876、dw_817、dw_1737、dw_4522、dw_3868、dw_2443），4 条用了方差规则但结果仍不同（改写后仍受窗口并列值影响：dw_1616、dw_2394、dw_232、dw_4339），1 条 `UNION` 排序规则冲突（dw_1795）。<br>**第 1 次（严格版，已归档为 `runs/phase0/03_compatibility_run1_strict.json`）** run `compat-20260925T062933-a95884`。**不改一个字直接执行：能执行 94/100，结果与 MySQL 一致 37/100。** 分类：COMPATIBLE 37、INCOMPATIBLE_SEMANTICS 57、UNKNOWN 6；两边结果各自重复执行都稳定。sqlglot 自动改写：0 条通过（47 条结果仍不同，16 条执行失败）。**57 条结果不同的原因（逐条复查）：** ① 37 条：MySQL 的 `VARIANCE/STD/STDDEV` 是**总体**统计量，Databricks 的同名函数是**样本**统计量；② 9 条：只是数字精度不同（MySQL `AVG`/除法只保留 4 位小数）；③ 11 条：窗口函数或 `LIMIT` 的排序键有并列值，并列行的先后顺序两个引擎处理不同（Gold 本身有歧义），另有日期解析差异等。**6 条 UNKNOWN：** 5 条是排名函数带了 `ROWS` 窗口帧（MySQL 忽略帧，Databricks 报错），1 条是 `UNION` 两边排序规则冲突（`UTF8_LCASE` 与普通 STRING），是我们环境设置带来的 |
| 5.5 冻结 Gold | `phase0.py gold` | `benchmark.gold_results` | Q4、Q5 | ✅ | run `gold-20260925T080708-5ee3bd`（基于 `compat-20260925T075328-c39909`），用新的数据库连接每条执行 3 次：**PRIMARY 89**（原始 Gold SQL 46 + 规则改写 43）、EXCLUDED 11；**漂移 0**；没有空结果；结果行数中位数 35，最多 130,080 |
| 5.6 生成报告 | `phase0.py report` | `reports\phase0_report.md` | 汇总 | ✅ | 判定为**方案 B**。Q2 改为三档判定（yes / partial / no）；Q5 分开列出原始 SQL 和改写 SQL 的数量 |

完整命令格式（把 `env` 换成对应步骤名）：

```bash
..\.venv\Scripts\python.exe scripts\phase0.py env
```

**Phase 0 结论（评审后填写）：**

| 问题 | 答案 |
|---|---|
| Q1 BEAVER Dataset 是否成功进入 Databricks？ | ✅ **是**。100 个 case（dw，官方种子 77 抽样）写入 `benchmark.cases`，原始 Gold SQL 带指纹保存、拒绝覆盖；97 张表的结构信息写入 `tables_meta`；Agent 只能通过 `cases_agent_view` 读到 case_id / question / db |
| Q2 BEAVER Schema 是否能在 Databricks 中正确还原？ | 🟡 **基本还原（partial）**。97/97 张表、421,707 行全部一致；95 张表逐列核对一致；2 张表的 3 个列（`course_catalog_subject_offered.SUBJECT_DESCRIPTION`、`tip_material.TITLE/AUTHOR`）有 8 个值因重音不敏感排序规则不同而去重结果不同（`UTF8_LCASE` 不忽略重音）。**100 个 case 的 Gold SQL 都没有用到这 3 个列，不影响评测** |
| Q3 多少 Gold SQL 可以直接在 Databricks 执行？ | 不改直接执行能跑通 **94/100**；结果与 MySQL 一致 **46/100**。另有 43 条经两条有文档依据的规则改写后结果一致，共 **89/100 可用** |
| Q4 Gold SQL 的执行结果是否稳定？ | ✅ **是**。同一次运行中两个引擎各执行 3 次，结果都一致；换新连接重新执行，89 条漂移为 0 |
| Q5 能否在不修改 Ground Truth 的情况下完成 Evaluation？ | ✅ **是**。`benchmark.cases` 里的 Gold SQL 保持原文；改写只存在 `gold_adaptations` 里，并附规则名、文档链接和结果验证；89 条的冻结答案都经过和 MySQL 官方引擎的结果比对 |
| **方案判定（A / B / C）** | **B**：部分 SQL 经过有文档依据、逐条验证的规则适配；11 条排除并计数 |
| 可用于主评测（PRIMARY）的 case 数 | **89**（原始 46 + 改写 43）；排除 11 |
| 是否进入 Phase 1 | ✅ 关卡一通过（89 ≥ 50），待你确认后进入 Phase 1 |

---

## 决策记录

| # | 日期 | 决策 | 理由 |
|---|---|---|---|
| D1 | 2026-09-25 | **Phase 0 采用方案 B（规则化适配）**：① 跨引擎比较按 DECIMAL 自身的小数位比较，浮点数相对误差 1e-6；② 改写规则只有两条，都有 MySQL 官方文档依据：`VARIANCE/STD/STDDEV` → `VAR_POP/STDDEV_POP`，删除排名类窗口函数上被 MySQL 忽略的窗口帧；③ 改写后的 SQL 必须跑出与 MySQL 一致的结果才算通过，通过的进入主评测（PRIMARY）；④ 结果依赖并列值排序的 Gold 直接排除并计数。sqlglot 自动改写弃用 | 严格方案只有约 46 个可用 case，达不到关卡一要求的 50；方案 B 每条改写都有文档依据，并且逐条做了结果验证 |
| D2 | 2026-09-25 | **① prompt 升级为 `baseline-v2`**：规则 4 改为明确的 MySQL→Databricks 统计函数对照（题干写 STDDEV/VARIANCE 或 "never STDDEV_POP" 时，一律写 `STDDEV_POP`/`VAR_POP`）。**② 建立开发集**：从评测集以外的 dw 题中固定抽 30 题（排除 few-shot 示例，种子 20260926），Gold 结果在 MySQL 上实时计算，只保留改写后在 Databricks 上能复现的题；从此 prompt、模型和参数的迭代只在开发集上做，89 道评测题只在配置冻结后跑一次 | ① 26/89 道评测题（dw 全集 2,116/5,787）题干带 MySQL 函数名，v1 规则和这些题干冲突，照题干写必然判错。这是基于全局事实的环境说明，不含 Gold 信息。② 1.6 试点是在评测题上做的，继续在上面改 prompt 就等于在考题上调参 |
| D3 | 2026-09-25 | **方案 A：保持 `glm-4-flash`，冻结 baseline 配置**（prompt `baseline-v2`、k=20、3 个 few-shot 示例），在 89 道评测题上跑一次正式 baseline，然后进入 Phase 2–6 搭 Inner Loop；**整个流程跑通后再换模型**，用同一套流程重跑对比 | 先让端到端流程跑起来。失败的结构清楚（20/21 个列报错是列挂错了表），Loop 有足够的可修素材。已知限制：首次准确率接近 0 时，Harm Rate 基本无法测量，换模型后补测 |
| D4 | 2026-09-25 | **维持 D3**：干预实验证明 glm-4-flash 在 5 类 Gold 提示全部给出时也修不好（0/30），但仍继续用它搭完 Phase 4–6，最后再换模型。Phase 3 标签：以确定性标注器（第一轮抽检一致率严格 75% / 宽松 90%）作为 Diagnosis Accuracy 的参照，因为干预实验在这个模型上给不出结论；换模型后重跑干预实验来校准 | 优先把 Loop 的工程结构和评测体系跑通。**已知后果**：这一轮 Loop 的恢复率可预判接近 0，实验数据只用来验证流程，不作为结论；换模型后用同一套流程重跑 |

## 问题记录

执行中遇到的问题统一记在这里，方便以后整理成学员指南。

| # | 日期 | 步骤 | 现象（报错原文） | 原因 | 解决办法 |
|---|---|---|---|---|---|
| 1 | 2026-09-25 | 1.5 | `hf auth login` 选浏览器登录后，终端一直显示 `Waiting for authorization....` | 设备授权流程：终端在等浏览器那边完成授权，不会自动跳转 | 浏览器打开 https://hf.co/oauth/device ，登录后输入终端显示的代码（如 `XXXX-XXXX`）并授权；代码过期就 `Ctrl+C` 重新运行。网络不通时改用"粘贴 token"方式登录 |
| 2 | 2026-09-25 | 2.2 | `databricks auth login` 提示 `Databricks profile name [dbc-2beb6eae-4266]:` | 新版 CLI 默认用工作区名作为 profile 名，项目配置读取的是 `DEFAULT` | 输入 `DEFAULT` 回车（覆盖旧 profile）；或者保留默认名，再把 `config/phase0.yaml` 的 `profile` 改成同名 |
| 3 | 2026-09-25 | 3.2 | 不记得安装过 MySQL，不知道 root 密码 | 本机在 2021-06-05 用官方安装程序装过 MySQL 8.0.25（服务 `MySQL80`，端口 3306，已有库 `itstack`、`wechat`、`qgydb`） | 先试 Workbench 里保存的连接；不行就用官方的 `--init-file` 方法重置 root 密码（不影响已有数据），完成后删除含明文密码的 init 文件。**学员指南需要补充"安装 MySQL"这一步** |
| 4 | 2026-09-25 | 3 | 试调用智谱时 `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f44b'` | Windows 终端默认 GBK 编码，打印不了模型回复里的 emoji（调用本身是成功的） | `scripts/phase0.py` 启动时把 stdout/stderr 改为 UTF-8；其他脚本可设置环境变量 `PYTHONIOENCODING=utf-8` |
| 5 | 2026-09-25 | 4.1 | 脚本列出了 `__MACOSX\beaver_db\._dw.sql` 等 6 个"dump" | 压缩包在 macOS 上生成，带资源分叉垃圾文件 | `fetch_beaver_db.py` 解压时跳过 `__MACOSX/`、`._*`、`.DS_Store`；已删除之前解压出的垃圾文件 |
| 6 | 2026-09-25 | 5.4 | 按方案 B 重跑时停在 88/100，日志约 20 分钟没有更新；MySQL 和 Databricks 上都没有正在执行的查询 | 第 89 个 case（dw_4004）返回 15,881 行，结果不一致时比较代码逐行两两比较（O(n²)，约 2.5 亿次），CPU 一直在算 | 大分组改成先排序、再逐行对齐（O(n log n)），16k 行约 0.5–1.5 秒；停掉原进程后从头重跑 |
| 7 | 2026-09-25 | 5.4 | `03_compatibility.json` 里的 `adaptations` 变成了改写明细列表，汇总统计丢失 | 保存本地文件时，明细和汇总用了同一个键名，明细覆盖了汇总（Delta 表不受影响） | 明细改存为 `adaptation_records`；已按原数据修复本次运行的 JSON 文件 |
| 8 | 2026-09-25 | 5.6b / 6 | dw_4188：SchemaSearch v2 确定性修复后 SQL 能执行，但返回 0 行；修复后的关联条件变成了 `sd.DEPARTMENT_CODE = sd.DEPARTMENT_CODE`、`ata.ACADEMIC_YEAR = ata.ACADEMIC_YEAR` | 错误的列引用正好在 JOIN ON 条件里，按"改到同一作用域里唯一拥有该列的表"修改后，它和等号另一侧变成了同一张表，关联条件恒为真，表之间失去关联（学期表和院系表本来就没有直接的关联键）。Phase 5 评测把它算成"报错 → 可执行"，统计虚高 | `fix_column_refs` 改完后检查所有"列 比较 列"的条件，改完后两边是同一个别名的就撤销，标记为"需要真正的关联键"，连同 schema 推断的候选关联键一起交给 LLM。补了 2 个回归测试。v2 结果归档为 `runs/phase5/repair-targeted-v2-tautology-bug.json`，Phase 6 的旧运行移到 `runs/phase6/_superseded/`，全部重跑 |
| 9 | | | | | |

---

## Phase 1 · Few-shot Baseline

分支 `phase1-baseline`。步骤见 [ROADMAP.md](ROADMAP.md) 第三节。配置：[config/phase1.yaml](../config/phase1.yaml)。

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 1.1 | LLM 客户端 `agent/llm.py` | ✅ | 智谱 OpenAI 兼容接口，`do_sample=False`；408/429/5xx 退避重试；key 控制字符检查；调用次数和 token 预算上限；按 (模型, 参数, system, prompt) 指纹的本地缓存 |
| 1.2 | 表检索 `agent/retriever.py` | ✅ | BM25（表名 ×3、列名 ×2、示例值 ×1）。schema 用 Databricks 实际列类型（beaver-table 里是 Oracle 类型 `VARCHAR2`）加示例取值。**k 在 300 道非评测题上调参**（`random.Random(20260925)`）：k=10 召回 0.73，k=15 0.84，**k=20 0.91（67% 的题能拿全 Gold 表）**，k=25 0.94，k=30 0.95，取拐点 k=20 |
| 1.3 | 生成器 `agent/generator.py` | ✅ | prompt 版本 `baseline-v1`：规则 + schema + 3 个 few-shot 示例 + 问题。示例取自评测集之外的 dw 题（≤3 张表、≤900 字符，套用 Phase 0 改写规则后能按 Databricks 语法解析） |
| 1.4 | 执行器 | ✅ | 复用 `execution/databricks_sql.py`，新增结果行数上限（50 万行，超出记为 `TOO_MANY_ROWS`） |
| 1.5 | 评测 `evaluation/baseline.py` | ✅ | 生成结果先统一格式，再和冻结的 Gold 按 BEAVER 官方规则比较（`evaluate_against_gold`）；表召回率只作为评估诊断，不交给 Agent。测试共 71 个，全部通过 |
| 1.5b | 冒烟测试（2 题） | ✅ | 链路跑通。2 题都执行报错，是模型错误：编造了不存在的表 `FAC_BUILDING` 和列 `COURSE_LEVEL`，并且没按规则用了 `STDDEV`。每次调用约 1.4 万 token、26 秒 |
| 1.6 | 小样本试点（20 题） | ⏸ 待决策 | run `baseline-20260925T082731-3b1b4d`（prompt `baseline-v1`，glm-4-flash，k=20）：**0/20 答对**；能执行 9/20（11 题 `UNRESOLVED_COLUMN`，模型编造列名）；能执行的 9 题中 7 题数值不对、1 题列数不对、1 题返回空。平均表召回 0.85，Gold 表全部选到的 11 题也是 0 题答对，**瓶颈在生成**。每题约 1.38 万 token，中位耗时 14.7 秒。**发现方言冲突：** 89 道主评测题中 26 题（dw 全集 5,787 题中 2,116 题）题干写着 "using STDDEV only and never STDDEV_POP"。这是 MySQL 语义（MySQL `STDDEV` = 总体），在 Databricks 照原文写就是样本标准差，必然判错；prompt 规则 4 又要求用 `STDDEV_POP`，两者冲突。已人工核对评测代码无误 |
| 1.6b | 开发集 + prompt v2 | ✅ 决策 D3 | **开发集**：看了 34 道候选题入选 30 题（淘汰 4 题：3 题在 Databricks 上无法复现，1 题报错）；领域复杂查询 17、复杂查询 7、领域查询 6。**baseline-v2**（run `baseline-20260925T084206-51d855`，glm-4-flash）：**0/30 答对**；能执行 4/30；报错 26 题（`UNRESOLVED_COLUMN` 21、`TABLE_OR_VIEW_NOT_FOUND` 2、语法错误 2、其他 1）；表召回 0.92，Gold 表全部选到的 22 题也是 0 题答对；仍有 3 题用了样本统计函数。**21 个列报错中有 20 个，这一列其实存在于另一张已选出的表里**，是把列挂错了表别名，并非凭空编造，Inner Loop 的 `SchemaSearch` 可以用查表方式修复。待决策：是否先用更强的智谱模型在开发集上探测一次 |
| 1.7 | 正式 Baseline（89 题） | ✅ | run `baseline-20260925T085517-69b4ac`（配置 `baseline-v2` / glm-4-flash / k=20，只跑一次）：**首次准确率 2/89 = 2.25%**（dw_461、dw_104，都是领域复杂查询）；可执行 21/89 = 23.6%；执行报错 68 题（`UNRESOLVED_COLUMN` 58、表不存在 3、语法错误 3、其他 4）；能执行的 21 题中 14 题数值不对、4 题返回空、1 题列数不对、2 题答对。表召回 0.887（按标注计算，偏保守）；Gold 表全部选到的 58 题中答对 1 题，有缺表的 31 题中答对 1 题。共 89 次 LLM 调用、122 万 token（平均 1.37 万/题），中位耗时 16.5 秒。已发布到 Delta 和 MLflow（MLflow run `e062ead6e8634f2e845b46239fb48a21`）；v1 试点那次也已补发 |

## Phase 2 · Trace / 可观测性

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 2.1 | Delta 表 | ✅ | `traces.execution_traces`：每次尝试一行，已预留 Verifier / 诊断 / 修复字段，供 Phase 4–6 填写。`evaluation.evaluation_results`：判分结果（correct、表召回等）。`evaluation.runs`：运行汇总。**边界：用 Gold 算出来的字段只放 evaluation.\*，trace 里没有**，因为自检模式下的 Observer / Diagnoser 会读 trace（有测试保证） |
| 2.2 | MLflow | ✅ | 实验 `/Users/mushan.ysl@gmail.com/self_healing_text2sql`（id 1654657461219271）。每次运行记录参数（模型、prompt 版本、k、few-shot 示例 id）、指标（准确率、可执行率、表召回、token、延迟）和产出文件（summary / run_meta / results） |
| 2.3 | 发布脚本 | ✅ | `scripts/publish_run.py <run_dir>`：同一个 run_id 重复发布时，先删旧行再写，MLflow 按 run_id 标签复用，保证幂等。已发布开发集运行 `baseline-20260925T084206-51d855`（30 条 trace）。生成记录新增 `result_preview`（结果前 5 行），这是 Agent 自己能观察到的执行结果，不含 Gold |
| 2.4 | 测试 | ✅ | 全部测试共 77 个，通过 |

## Phase 3 · Failure Taxonomy（失败分类，仅用于评测）

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 3.1 | 确定性标注器 `benchmark/beaver/subtasks.py` | ✅ | 把生成的 SQL 解析成语法树（别名还原为真实表名），提取表 / 列 / 关联等式 / 常量 / 运算，和 Gold 参照逐项比较。**参照 = BEAVER 标注 ∩ Gold SQL 实际用到的内容**：dw 中 1,060/5,787 题（18%）的 `tables` 标注列了 Gold SQL 没用到的表（开发集 10/30，评测样本 15/100），领域知识标注也有类似情况。自检：Gold SQL 对照自身，开发集 30/30、评测样本 100/100 没有任何误报。测试 9 个 |
| 3.1b | 正式 baseline 的失败标签 | ✅ | 87 个失败。**主因（按优先级取最上游）**：选表 76、列映射 7、关联键 1、领域知识 1、执行 2。**所有未通过的检查（多标签）**：选表 76、执行 68、查询拆解 40、列映射 38、领域知识 30、关联键 21。缺失的 Gold 表：**已检索但没用上 132**，没检索到 32。已发布到 `evaluation.failure_labels` |
| 3.2 | 人工抽检 | 🔄 Claude 第一轮完成，等待人工复核 | 20 题（`spotcheck.md`）：agree 15、partial 3、disagree 2 → **一致率严格 75%，宽松 90%**。发现的系统性问题：① 领域知识误报：模型用等价的编号写法（`DEPARTMENT_CODE='18'` ≈ 'Mathematics'）时仍判为缺失，只影响多标签；② 查询拆解检查只比运算类型，既有误报（`HAVING COUNT>0` 这种无实际作用的条件）也有漏报（整个子问题没做）；③ 根因和直接报错原因会不同（选表 vs 列名 / 语法），建议单独记录直接报错原因；④ 选表偏差有时源自 Gold 自身的写法 |
| 3.3 | 干预实验（开发集 30 道失败题） | ✅ | 结果目录 `runs/phase3/intervention-baseline-20260925T084206-51d855/`。每题分别只补一类 Gold 提示（setting=1/2 的做法，只用于离线分析），另有一个 5 类全补的上界。**单独补一类能修好：表 0/30、列 0/30、关联键 0/30、领域知识 1/21、查询拆解 1/20；5 类全补 0/30**（全补后仍有 18/30 执行报错）。因果类型：UNRESOLVED 28、领域知识 1、查询拆解 1。161 次调用，222 万 token。**结论：glm-4-flash 的瓶颈在模型能力，不在信息**。Gold 级别的信息全部给出也修不好，所以① 干预实验在这个模型上无法校准标签；② 以补充信息为主的 Inner Loop 在这个模型上恢复率可预判接近 0。决策 D3 预见的风险被数据证实 → 待决策：是否提前换模型 |

## Phase 4 · Diagnosis（诊断，运行时，不接触 Gold）

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 4.1 | Observer `loop_engineer/observer.py` | ✅ | 按**白名单**从 trace 取字段（问题、检索到的表、SQL、执行状态 / 报错 / 结果预览），correct、eval_\*、Gold 字段都进不来（有测试保证）。同时从报错文本解析出信号：错误类别、无法解析的列及其别名、Databricks 给出的列名建议、不存在的表 |
| 4.2 | Diagnoser `loop_engineer/diagnose.py`（`diagnoser-v1`） | ✅ | **规则级**：报错某列无法解析时，到 schema 里查这一列属于哪张表 → 在 SQL 已用的表里 = 列映射（别名挂错）；在已检索但没用上的表里 = 选表（表没用上）；只在未检索到的表里 = 选表（检索遗漏）；哪里都没有 = 列映射（编造列名）。表不存在 → 选表；语法 / 聚合 / 窗口等报错 → 执行；结果行数超限 → 关联键。**LLM 级**：只在没有明确报错信号时调用（SQL 能执行但结果可疑），要求输出严格 JSON。同时输出**修复线索**（repair_hints）供 Phase 5 使用。结果共享 `agent/sql_analysis.py`（从 Phase 3 标注器中拆出的中性模块）。测试 10 个 |
| 4.3 | 诊断准确率 `evaluation/diagnosis_eval.py` | ✅ | 严格口径 = 诊断结果等于标签主因；宽松口径 = 诊断结果在所有未通过的检查里。**开发集（30）**：严格 53.3%、宽松 56.7%；规则级 26 题：严格 61.5% / 宽松 65.4%；LLM 级 0/4。**评测集（87，冻结后只跑一次）**：**严格 40.2%、宽松 66.7%**；规则级 68 题：严格 51.5% / **宽松 76.5%**；LLM 级 19 题：严格 0/19、宽松 6/19。最大的混淆是"选表 → 列映射"（31 题）：诊断只看到直接报错（列挂错了别名），标注器看的是整体结构（还少用了表），即根因和直接原因不一致。LLM 级在 glm-4-flash 上基本无效，而且答错时置信度更高（0.84 vs 0.82）。已发布到 `traces.diagnoses`（Agent 侧）和 `evaluation.diagnosis_eval`（评测侧） |

## Phase 5 · Repair Skills + Policy

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 5.1 | Policy `loop_engineer/policy.py` | ✅ | 诊断到技能的映射写成配置：选表 → RetrieveAgain，列映射 → SchemaSearch，关联键 → FindJoinPath，查询拆解 → ReplanQuery，执行错误 / 未知 → RepairSQL；领域知识**临时**走 ReplanQuery（记为 fallback）。支持 `generic`（全部走 RepairSQL）和逐个禁用技能，供 Phase 8 消融 |
| 5.2 | 修复技能 `skills/*.py` | ✅ | SchemaSearch：别名挂错时**确定性**改写语法树（不调用 LLM），编造列名时把引擎建议和相似列名交给 LLM；RetrieveAgain：补上拥有该列的表，并附上从 schema 推断的关联条件；FindJoinPath：列出共享键列，让 LLM 核对关联条件；ReplanQuery：拆成子问题 / CTE；RepairSQL：带报错信息做最小修改，附 Databricks 语法限制说明 |
| 5.3 | 关联信息来源 | ✅ | `agent/join_graph.py`：只从 schema 推断（同名的 `*_CODE / *_KEY / *_ID` 键列），不用 BEAVER join_keys |
| 5.4 | 领域知识来源 | ⏸ | RetrieveKnowledge 未实现，等待确定不属于 Gold 的知识来源（值定位 / 非评测题整理出的术语表） |
| 5.5 | 测试 | ✅ | 新增 16 个，共 114 个。包括"skills / policy / diagnose / observer 不 import benchmark 或 evaluation 模块" |
| 5.6 | 技能评测（开发集 30 个失败） | ✅ | 结果 `runs/phase5/repair-targeted-*.json`：**可执行 4 → 8，答对 0**。RetrieveAgain 14 题（报错 → 可执行 3/14）、SchemaSearch 11 题（2/9，其中 6 题确定性修复）、RepairSQL 3 题（0/3）、ReplanQuery 2 题（原本就能执行）。28 次 LLM 调用、33 万 token。**发现**：确定性改别名修好第一处错误后，同一条 SQL 常在下一处列引用再报错 → 需要一次修完所有能查到的列引用 |
| 5.6b | SchemaSearch v2：一次修完所有能确定的列引用 | ✅ | `fix_column_refs` 按作用域检查每个带别名的列引用，列不在别名对应的表里、但同一作用域只有一张表有它时，改到那张表的别名；有歧义或找不到归属的，连同已修好一部分的 SQL 一起交给 LLM。**开发集可执行 8 → 10**；SchemaSearch 报错 → 可执行 2/9 → 4/9（dw_4188 一次确定性改了 3 处，不调用 LLM 就能执行）；LLM 调用 5 → 9（修不完的部分不再被丢下）；答对仍为 0。v1 结果归档为 `runs/phase5/repair-targeted-v1-single-fix.json`。测试 117 个 |
| 5.6c | 修复问题 #8 后重跑（v3） | ✅ | 可执行 **4 → 8**（v2 有 bug 时是 10，其中 2 题的"可执行"是关联条件被改成恒真造成的，属于虚高）；SchemaSearch 报错 → 可执行 2/9；答对 0 |
| 5.7 | 可视化报告 | ✅ | Phase 0–5 静态报告（含开发集 30 题逐题追踪、修复前后 SQL 对比）：https://claude.ai/artifact/GPw7xVDTVXBn3gidBHg2hr （私有；本地文件 `reports/loop_report.html`） |

## Phase 6 · Loop Controller + Verifier + Generic Retry

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 6.1 | Verifier `loop_engineer/verifier.py` | ✅ | **SelfVerifier**（主实验，不接触 Gold）：没生成 SQL、执行报错、结果行数超限、结果为空、某列全是 NULL。**OracleVerifier**（上界）：和 Gold 结果比对，结果一律标注为上界 |
| 6.2 | Controller `loop_engineer/controller.py` | ✅ | 生成 → 执行 → 验证 →（不通过时）观察 → 诊断 → Policy → 修复 → 执行 → 验证，最多 2 次尝试。**targeted** 与 **generic**（Generic Retry：只把上一次的 SQL 和观察结果交回模型，要求"改正"，不诊断，不给额外 schema 信息）共用同一套控制逻辑。第 1 次尝试各组完全相同（命中 LLM 缓存）。预算按尝试次数计，诊断的 LLM 调用计入成本。最终答案：最后一次通过验证的 > 最后一次能执行的 > 最后一次 |
| 6.3 | 指标 `evaluation/loop_run.py` | ✅ | 首次 / 最终准确率、恢复率、误伤率、净收益、平均尝试次数、额外 token、每净恢复一题的 token、Verifier 混淆矩阵（命中 / 误报 / 漏报）、各技能的恢复情况 |
| 6.4 | 发布 | ✅ | `publish_run.py` 支持 Loop 运行：每次尝试一行 trace（含 Verifier、诊断、修复字段），评测表按尝试记录对错；MLflow 记录实验组参数和 Loop 指标。评测集运行必须显式加 `--eval` |
| 6.5 | 开发集 4 个实验组（修复问题 #8 之后） | ✅ | 见下表。已发布到 Delta 和 MLflow。**4 组恢复都是 0**（开发集首次准确率 0/30，所以误伤率测不出；这是 D4 的已知后果） |

| 实验组（开发集 30 题） | 可执行（第 1 次 → 最终） | 平均尝试 | 额外 token | Verifier：命中 / 漏报 / 误报 | 恢复 |
|---|---|---|---|---|---|
| Targeted Loop + Self | 4 → **9** | 1.9 | 36.96 万 | 27 / 3 / 0 | 0 |
| Generic Retry + Self | 4 → 6 | 1.9 | 36.20 万 | 27 / 3 / 0 | 0 |
| Targeted Loop + Oracle（上界） | 4 → **9** | 2.0 | 41.20 万 | 30 / 0 / 0 | 0 |
| Generic Retry + Oracle（上界） | 4 → 6 | 2.0 | 40.17 万 | 30 / 0 / 0 | 0 |

**解读**：token 成本相近时，Targeted Loop 让更多 SQL 变得可执行（9 vs 6），这是工程层面的差异；但 glm-4-flash 生成的 SQL 即使能执行，结果也不对，所以恢复都是 0，与干预实验的结论一致。SelfVerifier 在开发集上漏报 3 题（能执行但答错），Oracle 会把这 3 题也送去修复，但同样修不好。

## Phase 9 · Loop Debug Console（先于 Phase 7 / 8 实现）

| 步骤 | 内容 | 状态 | 结果摘要 |
|---|---|---|---|
| 9.1 | 数据层 `app/data.py` | ✅ | 直接读取 Delta 表（evaluation.runs、traces.execution_traces、evaluation.evaluation_results、failure_labels、diagnoses、diagnosis_eval、benchmark.*），不读本地文件，新运行发布后自动出现。本地通过 profile 认证；在 Databricks Apps 中使用 App 自己的凭据加上 `DATABRICKS_WAREHOUSE_ID` |
| 9.2 | 面板 `app/dashboard.py`（Streamlit） | ✅ 本地可用 | 5 个页签：**总览**（阶段状态、关键指标、全部运行）；**对照实验**（Phase 6 开发集 4 组的表格和图表 + **Phase 7 评测集预留区**）；**消融实验**（**Phase 8 预留表**，已列出 7 个计划消融组，结果发布后自动从"未运行"变为"已运行"）；**逐题追踪**（按运行和题目查看每次尝试的 SQL、执行结果、Verifier、诊断、修复技能、前后 SQL 对比，Gold 判分单独标注"Loop 不可见"）；**失败与诊断**（标注器主因与运行时诊断的分布、诊断准确率、按来源的准确率、混淆表）。启动：`streamlit run app/dashboard.py`，或用预览配置 `loop-console`（端口 8502） |
| 9.3 | 验证 | ✅ | 本地预览 5 个页签都能渲染，没有报错。修复了两个问题：数据库空值读成 NaN 后被当作"真"（逐题追踪页报错），以及图表颜色映射和高度 |
| 9.4 | 部署为 Databricks App | ⬜ | 待定：Free Edition 每个账号只能有 1 个 App，需要确认是否占用 gmv-rca-agent 的名额 |
