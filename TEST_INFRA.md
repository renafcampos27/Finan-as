# E2E Test Infrastructure & Architecture Specification

## 1. Executive Summary & Mission
This document defines the architectural specification, isolation principles, and test harness design for the **Finanças Pessoais — Liquid Glass** opaque-box End-to-End (E2E) test suite.
The primary objective is to validate all core application capabilities across **Features F1 through F11** (defined in `PROJECT.md`) without coupling tests to private implementation details or relying on brittle UI state.

The suite is implemented in `tests/test_e2e_requirements.py` following a **4-Tier Hierarchical Testing Architecture**.

---

## 2. 4-Tier Test Architecture

```
+-----------------------------------------------------------------------------------+
|                        TIER 4: REAL-WORLD WORKLOAD SCENARIOS                      |
| (Full Lifecycle: 1000+ txns ingestion, split categorizations, portfolio valuation,|
|  FIRE decumulation, encrypted export, database compaction, and restore audit)     |
+-----------------------------------------------------------------------------------+
                                         ^
                                         |
+-----------------------------------------------------------------------------------+
|                     TIER 3: CROSS-FEATURE COMBINATIONS                            |
| (Pairwise Inter-service workflows: Backup + Restore + Monte Carlo; Splits +       |
|  Budgets + Category Spend; Cashflow Forecast + Recurring + Change Journal Undo)   |
+-----------------------------------------------------------------------------------+
                                         ^
                                         |
+-----------------------------------------------------------------------------------+
|                     TIER 2: BOUNDARY & CORNER CASES                               |
| (Zero/negative cashflows, empty ledgers, 150% extreme volatility, single-txn      |
|  portfolios, 100-year horizons, truncated & corrupted backups, tamper rejection)  |
+-----------------------------------------------------------------------------------+
                                         ^
                                         |
+-----------------------------------------------------------------------------------+
|                     TIER 1: FEATURE SPECIFICATION COVERAGE                        |
| (>=5 genuine tests per functional domain: Financial Math, Monte Carlo, XIRR,     |
|  Cashflow Forecasting, SQL Splits, SQLite WAL/Vacuum, Change Journal, Encrypted   |
|  Backups, and Decimal Precision Invariants)                                       |
+-----------------------------------------------------------------------------------+
```

### Tier 1: Feature Specification Coverage
Covers foundational functional behaviors across all 9 domain areas with $\ge 5$ distinct test cases per domain:
1. **Financial Math & Returns**: Effective periodic compounding, discount factors, PV of annuity contributions, CAGR, TWRR, Modified Dietz, Sharpe ratio, Sortino ratio, Max Drawdown, Cornish-Fisher VaR, and Expected Shortfall CVaR.
2. **Monte Carlo Stochastic Engine**: Log-drift calibration, single vs monthly regular contribution schedules, asset weight diversification matrix, inflation and tax adjustments, and P10/P50/P90 percentile distributions.
3. **XIRR & Rentability Solvers**: Annualized XIRR convergence, percentage formatting, irregular multi-year cash flow streams, positive/negative cash flow validation, and non-convergence guards.
4. **Cashflow & Recurring Forecasting**: Normalization and fuzzy matching, payment frequency detection, 30/60/90-day cash calendar projections, tolerance matching, and negative balance alerting.
5. **SQL Split Aggregations & Query Optimizations**: Single-pass category spend with split allocation, dynamic grouping by merchant/account/category, purchase vs payment accounting basis, and monthly totals grouping.
6. **SQLite Engine & Incremental Vacuuming**: SQLite WAL mode configuration, memory cache pragmas, `PRAGMA integrity_check`, `auto_vacuum=INCREMENTAL`, vacuum step routines, and read-only connection isolation.
7. **Snapshot Compaction & Change Journal**: Reversible mutations (create, edit, delete), JSON snapshot compression/decompression, attachment transaction rollback, table whitelist security, and undo row limit enforcement.
8. **Encrypted Backup Packaging & Disaster Recovery**: AES-256-GCM authenticated encryption, PBKDF2 key derivation, SHA-256 integrity digest verification, wrong password rejection, and secondary backup mirroring.
9. **Decimal Precision & Monetary Invariants**: Penny-perfect split allocations summing to 100%, currency conversion stability, zero-loss float-to-cents reconciliation, and epsilon boundaries ($\Delta \le 10^{-5}$).

### Tier 2: Boundary & Corner Cases
Audits system behavior under adversarial, extreme, and edge conditions:
- Zero starting capital with zero ongoing contributions.
- Negative returns and severe market crashes (150% annual volatility, 90% drawdowns).
- Empty ledgers (0 accounts, 0 transactions, 0 holdings).
- Portfolios with only a single transaction or single asset.
- Extended financial projection horizons (50-year and 100-year FIRE simulations).
- Corrupted, truncated, or zero-byte backup archives.
- Tampered ciphertext / invalid GCM authentication tags.

### Tier 3: Cross-Feature Combinations
Validates emergent behavior and data consistency across module boundaries:
- **Pair 1**: Backup $\rightarrow$ Restore $\rightarrow$ Monte Carlo Simulation & Analytics on restored data.
- **Pair 2**: Transaction Splits $\rightarrow$ Budget Buckets $\rightarrow$ Category Spend Aggregations.
- **Pair 3**: Cashflow Forecast $\rightarrow$ Recurring Subscriptions $\rightarrow$ Change Journal Undo/Redo.
- **Pair 4**: Goal Milestones $\rightarrow$ Portfolio Net Worth $\rightarrow$ Financial State Snapshots.
- **Pair 5**: Encrypted Backup Archive $\rightarrow$ Change Journal History $\rightarrow$ Managed Attachments.

### Tier 4: Real-World Workload Scenarios
Validates high-volume, end-to-end user workflows:
- Bulk ingestion of 1,000+ realistic Portuguese banking transactions across multiple checking, savings, and investment accounts.
- Automated and split categorization of supermarket and utility expenditures.
- Asset valuation, cost-basis tracking, and net gain computation for investment holdings.
- FIRE retirement planning calculations (Standard, Lean, Fat, Barista, Coast FIRE) and Monte Carlo stress testing.
- Encrypted export creation with full relational payload packaging.
- SQLite incremental compaction, database vacuuming, and integrity validation.
- Clean database restoration with 100% relational fidelity, zero financial loss, and exact decimal balance verification.

---

## 3. Test Isolation & Environment Strategy

### Ephemeral State Sandboxing
All tests use pytest fixtures (`tmp_path`, custom database sandboxes) ensuring that:
1. No tests write to the user's `%LOCALAPPDATA%` or production directories.
2. Each test creates an independent `FinanceDB` SQLite database in WAL mode with distinct directories for `anexos` (attachments), `historico_alteracoes` (change journals), and `backups`.
3. Background threads and database connections are explicitly closed and cleaned up in teardown phases.

### Opaque-Box Expected Value Derivation
All test assertions are derived from mathematical formulas and formal specifications:
- **Compound Periodic Rate**: $r_{\text{period}} = (1 + r_{\text{annual}})^{1/n} - 1 = \exp(\frac{\ln(1 + r_{\text{annual}})}{n}) - 1$.
- **PV of Annuity**: $PV = C \cdot \frac{1 - (1 + r)^{-n}}{r}$.
- **CAGR**: $\text{CAGR} = \left(\frac{V_{\text{end}}}{V_{\text{start}}}\right)^{1 / t} - 1$.
- **Sharpe Ratio**: $S = \frac{\mu - R_f}{\sigma} \cdot \sqrt{P}$.
- **Max Drawdown**: $\max_{t} \left(\frac{\max_{\tau \le t} V_\tau - V_t}{\max_{\tau \le t} V_\tau}\right) \times 100\%$.
- **XIRR**: Root $r$ satisfying $\text{NPV}(r) = \sum_{i=0}^{N} \frac{C_i}{(1 + r)^{(d_i - d_0)/365}} = 0$.
- **Trinity FIRE**: $\text{Capital Target} = \frac{\text{Annual Expenses}}{\text{SWR}}$.

---

## 4. Execution & Verification

### Running the E2E Test Suite
```bash
pytest tests/test_e2e_requirements.py -v
```

### Running Specific Test Tiers
```bash
# Tier 1 only
pytest tests/test_e2e_requirements.py -k "TestTier1" -v

# Tier 2 only
pytest tests/test_e2e_requirements.py -k "TestTier2" -v

# Tier 3 only
pytest tests/test_e2e_requirements.py -k "TestTier3" -v

# Tier 4 only
pytest tests/test_e2e_requirements.py -k "TestTier4" -v
```

---

## 5. Architectural Quality Standards
- **Zero Flakiness**: Deterministic RNG seeds (`seed=42`) for Monte Carlo simulations.
- **Memory Safety**: Clean connection closing and temp file unlinking.
- **Lossless Invariants**: Verification of zero-cent rounding discrepancies.
