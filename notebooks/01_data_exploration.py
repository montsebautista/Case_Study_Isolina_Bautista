# %% [markdown]
# # Teil 1: Data Exploration & Data Quality
#
# Goal: understand the schema and scale of all 5 source tables, identify
# material data quality issues that could distort downstream revenue and
# customer analysis, and map out how the tables relate to one another.

# %%
from pathlib import Path
import pandas as pd

pd.set_option('display.max_columns', None)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

customers = pd.read_csv(DATA_DIR / 'dim_customer_synthetic.csv', sep=';')
trades = pd.read_csv(DATA_DIR / 'fct_trade_anonymized.csv', sep=';')
saving_plans = pd.read_csv(DATA_DIR / 'fct_saving_plan_anonymized.csv', sep=';')
revenue_conditions = pd.read_csv(DATA_DIR / 'int_revenue_condition.csv', sep=';')
instruments = pd.read_csv(DATA_DIR / 'dim_instrument.csv', sep=';')

# %% [markdown]
# ## Q1-Q2: Schema overview & entity counts

# %%
for name, df in [('customers', customers), ('trades', trades),
                  ('saving_plans', saving_plans),
                  ('revenue_conditions', revenue_conditions),
                  ('instruments', instruments)]:
    print(f"--- {name} ---")
    print(f"shape: {df.shape}")
    print(df.dtypes)
    print()

# %% [markdown]
# **Findings:**
# - 29,093 customers, 100,775 trades, 39,459 saving plan executions
# - Date/timestamp columns (`registration_date`, `exec_datetime`, etc.) load
#   as `str`, since `read_csv` never auto-parses dates — will convert
#   explicitly with `pd.to_datetime()` before any time-based analysis
#   (Teil 3), not a data quality issue on its own.

# %% [markdown]
# ## Q3: Data Quality Issue #1 — Incomplete instrument master data
#
# `fct_trade_anonymized.instrument_id` should map to `dim_instrument.isin`.
# We test the match rate and investigate the cause of any gap.

# %%
trades['is_matched'] = trades['instrument_id'].isin(instruments['isin'])
match_rate = trades['is_matched'].mean()
n_null_ids = trades['instrument_id'].isna().sum()
n_orphaned = (~trades['is_matched'] & trades['instrument_id'].notna()).sum()

print(f"Match rate (trades -> dim_instrument): {match_rate:.2%}")
print(f"Null instrument_id: {n_null_ids}")
print(f"Orphaned (non-null, unmatched) instrument_id: {n_orphaned}")

# Cross-check against saving_plans, which joins to the same master table
sp_match_rate = saving_plans['instrument_id'].isin(instruments['isin']).mean()
print(f"Match rate (saving_plans -> dim_instrument): {sp_match_rate:.2%}")

# Do orphaned trade instruments ever appear in saving_plans?
orphaned_trade_ids = trades.loc[~trades['is_matched'], 'instrument_id'].dropna().unique()
overlap = saving_plans['instrument_id'].isin(orphaned_trade_ids).sum()
print(f"Orphaned trade instrument_ids also seen in saving_plans: {overlap} rows")

# %% [markdown]
# **Findings:** 22.04% of trades (22,210 of 100,775) cannot be matched to
# `dim_instrument`: 479 have a null `instrument_id`, 21,731 reference a
# properly-formatted ISIN absent from the master table. Crucially,
# `saving_plans` — which joins to the *same* master table — has only a
# 0.43% orphan rate, and only 6 of its rows reference any of the 429
# orphaned trade instruments. This rules out a general `dim_instrument`
# completeness problem and points to a maintenance gap specific to
# instruments that are only ever traded ad-hoc (not via Sparplan).
#
# **Handling:** in Teil 2, unmatched trades will be bucketed as
# "Unknown/Unmatched" instrument type and reported separately, rather
# than silently dropped, since they represent a material 22% of trade
# volume.

# %% [markdown]
# ## Q3: Data Quality Issue #2 — Orphaned customer references

# %%
trades['cust_matched'] = trades['synthetic_customer_id'].isin(customers['synthetic_customer_id'])
sp_cust_matched = saving_plans['synthetic_customer_id'].isin(customers['synthetic_customer_id'])

n_orphan_trades = (~trades['cust_matched']).sum()
n_orphan_sp = (~sp_cust_matched).sum()
n_distinct_orphan_custs = trades.loc[~trades['cust_matched'], 'synthetic_customer_id'].nunique()

print(f"Trades with unknown customer_id: {n_orphan_trades} ({n_orphan_trades/len(trades):.2%})")
print(f"Saving plans with unknown customer_id: {n_orphan_sp} ({n_orphan_sp/len(saving_plans):.2%})")
print(f"Distinct unknown customer_ids in trades: {n_distinct_orphan_custs}")

avg_trades_all = len(trades) / trades['synthetic_customer_id'].nunique()
avg_trades_orphaned = n_orphan_trades / n_distinct_orphan_custs
print(f"Avg trades/customer (all): {avg_trades_all:.2f}")
print(f"Avg trades/customer (orphaned only): {avg_trades_orphaned:.2f}")

# %% [markdown]
# **Findings:** 2.94% of trades (2,961 rows, 856 distinct customer IDs) and
# 2.11% of saving plan executions reference a `synthetic_customer_id` absent
# from `dim_customer_synthetic`. These orphaned customers trade at
# essentially the same average rate as the general population (3.46 vs.
# 3.52 trades/customer), ruling out a distinct behavioral segment (e.g.
# test accounts). Given the dataset is explicitly synthetic/anonymized,
# the most plausible cause is an inconsistency introduced during the
# anonymization process across tables, rather than a genuine business
# event.
#
# **Handling:** excluded from any customer-level analysis (segmentation,
# lifecycle, churn) in Teil 3-5, since no customer attributes are
# available for them. Exclusion rate disclosed rather than silently
# dropped.

# %% [markdown]
# ## Q3: Data Quality Issue #3 — Duplicate rows (likely ETL/export artifact)

# %%
n_dupe_trades = trades.duplicated().sum()
n_dupe_sp = saving_plans.duplicated().sum()
print(f"Fully identical duplicate rows - trades: {n_dupe_trades} ({n_dupe_trades/len(trades):.2%})")
print(f"Fully identical duplicate rows - saving_plans: {n_dupe_sp} ({n_dupe_sp/len(saving_plans):.2%})")

key_cols = ['synthetic_customer_id', 'instrument_id', 'exec_datetime']
n_key_dupes = trades.duplicated(subset=key_cols, keep=False).sum()
print(f"Trade rows sharing customer+instrument+exec_datetime: {n_key_dupes}")

# %% [markdown]
# **Findings:** 5,083 trade rows (5.04%) and 3,173 saving plan rows (8.04%)
# are fully identical duplicates across every column. Cross-checking
# against a narrower key (customer + instrument + exec_datetime) shows
# 10,164 rows in duplicate groups — almost exactly 2x the fully-identical
# count (10,166 expected), confirming duplicate groups differ in
# essentially no columns, not even price or quantity. This rules out
# legitimate co-timed events (e.g. partial fills, which would typically
# differ on quantity/price) and points to an ETL/export artifact — likely
# an overlapping-batch extraction that wasn't deduplicated.
#
# **Handling:** `drop_duplicates()` applied immediately after loading,
# upstream of all revenue/volume aggregation in Teil 2 — leaving this
# unhandled would materially overstate headline revenue figures.

# %% [markdown]
# ## Verification: `leverage` flag consistency (dim_instrument)
#
# Not a data quality issue, but worth verifying before trusting this flag
# in later segmentation.

# %%
print(instruments.groupby('instrument_type')['leverage'].mean().sort_values(ascending=False))

# %% [markdown]
# **Finding:** `leverage=True` is concentrated in DERIVAT (90.9%), present
# in a small legitimate minority of ETF (1.8%, consistent with real
# leveraged-ETF products), and 0% for AKTIE/FONDS/KRYPTO. No semantic
# inconsistency found — flag can be trusted for downstream use.

# %% [markdown]
# ## Q4: Table relationships
# See `README.md` for the full entity-relationship diagram. Summary:
# - `trades`/`saving_plans` → `dim_customer_synthetic` on `synthetic_customer_id`
#   (97.06% / 97.89% match — see DQ Issue #2)
# - `trades`/`saving_plans` → `dim_instrument` on `instrument_id = isin`
#   (77.96% / 99.57% match — see DQ Issue #1)
# - `trades` → `int_revenue_condition`: no direct key; a **conditional
#   match** on `instrument_type`, `order_type`, and whether
#   `gross_volume_eur` falls within `[trade_gross_volume_from_eur,
#   trade_gross_volume_to_eur]` — implemented in Teil 2, not a standard join.
