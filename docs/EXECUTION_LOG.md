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
| 5 | 运行 Phase 0 并评审报告 | Claude | 🔄 | |

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
| 5.4 兼容性验证 | `phase0.py compat` | 每条 Gold SQL 的兼容性分类 | Q3 | 🔄 按方案 B 重跑中 | **第 1 次（严格版，已归档为 `runs/phase0/03_compatibility_run1_strict.json`）** run `compat-20260925T062933-a95884`。**不改一个字直接执行：能执行 94/100，结果与 MySQL 一致 37/100。** 分类：COMPATIBLE 37、INCOMPATIBLE_SEMANTICS 57、UNKNOWN 6；两边结果各自重复执行都稳定。sqlglot 自动改写：0 条通过（47 条结果仍不同，16 条执行失败）。**57 条结果不同的原因（逐条复查）：** ① 37 条：MySQL 的 `VARIANCE/STD/STDDEV` 是**总体**统计量，Databricks 的同名函数是**样本**统计量；② 9 条：只是数字精度不同（MySQL `AVG`/除法只保留 4 位小数）；③ 11 条：窗口函数或 `LIMIT` 的排序键有并列值，并列行的先后顺序两个引擎处理不同（Gold 本身有歧义），另有日期解析差异等。**6 条 UNKNOWN：** 5 条是排名函数带了 `ROWS` 窗口帧（MySQL 忽略帧，Databricks 报错），1 条是 `UNION` 两边排序规则冲突（`UTF8_LCASE` 与普通 STRING），是我们环境设置带来的 |
| 5.5 冻结 Gold | `phase0.py gold` | `benchmark.gold_results` | Q4、Q5 | ⬜ | |
| 5.6 生成报告 | `phase0.py report` | `reports\phase0_report.md` | 汇总 | ⬜ | |

完整命令格式（把 `env` 换成对应步骤名）：

```bash
..\.venv\Scripts\python.exe scripts\phase0.py env
```

**Phase 0 结论（评审后填写）：**

| 问题 | 答案 |
|---|---|
| Q1 BEAVER Dataset 是否成功进入 Databricks？ | |
| Q2 BEAVER Schema 是否能在 Databricks 中正确还原？ | |
| Q3 多少 Gold SQL 可以直接在 Databricks 执行？ | |
| Q4 Gold SQL 的执行结果是否稳定？ | |
| Q5 能否在不修改 Ground Truth 的情况下完成 Evaluation？ | |
| **方案判定（A / B / C）** | |
| 可用于主评测（PRIMARY）的 case 数 | |
| 是否进入 Phase 1 | |

---

## 决策记录

| # | 日期 | 决策 | 理由 |
|---|---|---|---|
| D1 | 2026-09-25 | **Phase 0 采用方案 B（规则化适配）**：① 跨引擎比较按 DECIMAL 自身的小数位比较，浮点数相对误差 1e-6；② 改写规则只有两条，都有 MySQL 官方文档依据：`VARIANCE/STD/STDDEV` → `VAR_POP/STDDEV_POP`，删除排名类窗口函数上被 MySQL 忽略的窗口帧；③ 改写后的 SQL 必须跑出与 MySQL 一致的结果才算通过，通过的进入主评测（PRIMARY）；④ 结果依赖并列值排序的 Gold 直接排除并计数。sqlglot 自动改写弃用 | 严格方案只有约 46 个可用 case，达不到关卡一要求的 50；方案 B 每条改写都有文档依据，并且逐条做了结果验证 |

## 问题记录

执行中遇到的问题统一记在这里，方便以后整理成学员指南。

| # | 日期 | 步骤 | 现象（报错原文） | 原因 | 解决办法 |
|---|---|---|---|---|---|
| 1 | 2026-09-25 | 1.5 | `hf auth login` 选浏览器登录后，终端一直显示 `Waiting for authorization....` | 设备授权流程：终端在等浏览器那边完成授权，不会自动跳转 | 浏览器打开 https://hf.co/oauth/device ，登录后输入终端显示的代码（如 `XXXX-XXXX`）并授权；代码过期就 `Ctrl+C` 重新运行。网络不通时改用"粘贴 token"方式登录 |
| 2 | 2026-09-25 | 2.2 | `databricks auth login` 提示 `Databricks profile name [dbc-2beb6eae-4266]:` | 新版 CLI 默认用工作区名作为 profile 名，项目配置读取的是 `DEFAULT` | 输入 `DEFAULT` 回车（覆盖旧 profile）；或者保留默认名，再把 `config/phase0.yaml` 的 `profile` 改成同名 |
| 3 | 2026-09-25 | 3.2 | 不记得安装过 MySQL，不知道 root 密码 | 本机在 2021-06-05 用官方安装程序装过 MySQL 8.0.25（服务 `MySQL80`，端口 3306，已有库 `itstack`、`wechat`、`qgydb`） | 先试 Workbench 里保存的连接；不行就用官方的 `--init-file` 方法重置 root 密码（不影响已有数据），完成后删除含明文密码的 init 文件。**学员指南需要补充"安装 MySQL"这一步** |
| 4 | 2026-09-25 | 3 | 试调用智谱时 `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f44b'` | Windows 终端默认 GBK 编码，打印不了模型回复里的 emoji（调用本身是成功的） | `scripts/phase0.py` 启动时把 stdout/stderr 改为 UTF-8；其他脚本可设置环境变量 `PYTHONIOENCODING=utf-8` |
| 5 | 2026-09-25 | 4.1 | 脚本列出了 `__MACOSX\beaver_db\._dw.sql` 等 6 个"dump" | 压缩包在 macOS 上生成，带资源分叉垃圾文件 | `fetch_beaver_db.py` 解压时跳过 `__MACOSX/`、`._*`、`.DS_Store`；已删除之前解压出的垃圾文件 |
| 6 | | | | | |

---

## 后续 Phase

Phase 0 通过后，在这里追加 Phase 1 的执行记录（步骤见 [ROADMAP.md](ROADMAP.md) 第三节）。
