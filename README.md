# Self-Healing Text-to-SQL Agent on Databricks

Research question: when a Text-to-SQL agent produces wrong SQL, can the system
**observe → diagnose → apply a targeted repair → re-verify**, and does that beat
a generic retry that uses the same budget? BEAVER is the benchmark, Databricks
is the platform, and loop engineering is the actual contribution.

This is a **Loop Engineering** project. Text-to-SQL is only the test vehicle. Loop design, background, value and interview narrative: [docs/PROJECT_POSITIONING.md](docs/PROJECT_POSITIONING.md). Step-by-step execution plan: [docs/ROADMAP.md](docs/ROADMAP.md). Top-down architecture and design principles of every module (double loop): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). End-to-end run flow with the code each stage executes: [docs/RUN_FLOW.md](docs/RUN_FLOW.md). Step-by-step loop walkthrough mapped to source code: [docs/LOOP_WALKTHROUGH.md](docs/LOOP_WALKTHROUGH.md). How the loop works (flow, trace, context, skills, design rationale): [docs/LOOP_DESIGN.md](docs/LOOP_DESIGN.md). Execution record: [docs/EXECUTION_LOG.md](docs/EXECUTION_LOG.md).

**Status: Phase 0 (BEAVER → Databricks qualification).** No agent code exists
yet. The strict phase order is in the project brief.

## Phase 0 design

```
BEAVER (HF, gated)  ──► benchmark.cases (original gold SQL, sha256-fingerprinted, never overwritten)
BEAVER MySQL dump   ──► local MySQL 8 (BEAVER's official engine = REFERENCE ORACLE)
                          │  replicate: typed Parquet → UC Volume → Delta, per-table fidelity check
                          ▼
                     <catalog>.<db>.*  (same schema name as the MySQL db, so gold SQL runs unmodified)
gold SQL (original) ──► MySQL ×3  and  Databricks SQL ×3 (result cache off)
                          ▼
            COMPATIBLE only if it executes unmodified AND reproduces the MySQL result AND is stable
                          ▼
                     benchmark.gold_results (frozen ground truth, PRIMARY / SECONDARY / EXCLUDED)
```

Main decisions:

| Decision | Why |
|---|---|
| MySQL is the oracle, Databricks is the candidate | The only way to show that gold SQL keeps its meaning is to compare it against the engine BEAVER was built on. Executing without error proves nothing. |
| Canonical comparison for MySQL vs Databricks, official BEAVER comparison for EX | Drivers print the same value differently (`AVG` → `12.5000` vs `12.5`). Later, gold and generated SQL both run on Databricks, so the official `str()`-set comparison applies (`benchmark/beaver/evaluator.py`). |
| `_ci` MySQL columns → `STRING COLLATE UTF8_LCASE` | This restores MySQL's case-insensitive string semantics in the schema, so the SQL text stays untouched. Accent-insensitivity and PAD SPACE are not mirrored; result comparison would catch any drift they cause. |
| `ANSI_MODE=false`, `DATETIME → TIMESTAMP_NTZ` | These match MySQL behaviour: `x/0 → NULL`, and no session time-zone shift. One global environment serves both gold and generated SQL. |
| Adapter = documented rules only (MySQL `VARIANCE/STD/STDDEV` → `VAR_POP/STDDEV_POP`; drop frame clauses that MySQL ignores on `RANK/ROW_NUMBER/LAG/…`), stored as separate records and admitted to PRIMARY only when the adapted SQL reproduces the MySQL result (decision B, 2026-09-25) | The MySQL manual fixes the meaning of each rule, and the per-case result check guards the implementation. Generic transpilation (sqlglot) was dropped: it repaired 0 of 63 cases because it copies `VARIANCE` verbatim. |
| Whole database replicated, not just gold tables | Replicating only the gold tables would leak ground truth into retrieval. |
| `benchmark.cases_agent_view` + `AgentTask` | Agents can only reach `case_id / question / db` (setting=0). |
| Package `dbx/` instead of `databricks/` | A top-level `databricks` package would shadow `databricks.sql` and `databricks.sdk`. |

## Runbook

One-time prerequisites (these need your accounts and credentials):

1. Accept the dataset terms for **beaverbench/beaver-query** and **beaverbench/beaver-table** on huggingface.co, then run `hf auth login`.
2. `databricks auth login --host https://dbc-2beb6eae-4266.cloud.databricks.com`. The CLI's cached token from an older version is no longer accepted.
3. `cp .env.example .env` and fill in `MYSQL_PASSWORD` for the local MySQL80 service.
4. `python scripts/fetch_beaver_db.py`, then run the printed `mysql ... < dump.sql` commands.

Then:

```bash
python scripts/phase0.py env        # 01 connectivity, UC layout, collation / ANSI checks
python scripts/phase0.py import     # 02 100 dw cases (BEAVER's seed-77 sample) + table metadata
python scripts/phase0.py replicate  # 02b MySQL → Delta, fidelity report
python scripts/phase0.py compat     # 03 compatibility of original gold SQL
python scripts/phase0.py gold       # 04 frozen gold results (fresh session = cross-session stability)
python scripts/phase0.py report     # reports/phase0_report.md: answers Q1–Q5 from evidence
```

Notebooks `notebooks/01–04` inspect and re-check the evidence inside Databricks.
Notebook 04 re-executes gold SQL on notebook compute against the frozen hashes.

Tests: `python -m pytest` (offline, uses fake executors).

## Tables

| Table | Content |
|---|---|
| `benchmark.cases` | original BEAVER record per case + `source_sha256` |
| `benchmark.cases_agent_view` | case_id, question, db only |
| `benchmark.tables_meta` | beaver-table schema metadata (agent-visible context) |
| `benchmark.replication_report` | per-table row counts / column profiles, MySQL vs Databricks |
| `benchmark.sql_compatibility` | per-case compatibility verdict with evidence (errors, hashes, hazards) |
| `benchmark.gold_adaptations` | adapter attempts: original vs adapted SQL, rule, validation |
| `benchmark.gold_results` | frozen ground truth + eligibility + exclusion reason |

## Known limitations (Phase 0)

- Result equivalence is checked on the BEAVER data instance only. Adaptations are therefore restricted to rules whose meaning the MySQL manual defines.
- Cross-engine comparison compares a DECIMAL at its own displayed scale (MySQL `AVG`/division keep 4 decimals) and floats with relative tolerance 1e-6 (`cross_engine_v2` in `benchmark/beaver/evaluator.py`).
- Gold SQL whose result depends on how ties are ordered (window functions or `LIMIT` over non-unique keys) differs between engines and is excluded, not repaired.
- `UTF8_LCASE` does not reproduce accent-insensitive or PAD SPACE collations.
- MySQL zero-dates (`0000-00-00`) become NULL. They are counted per table and tolerated only on temporal columns.
- The Databricks gold result is hashed with numeric values canonicalised to 6 decimals.
