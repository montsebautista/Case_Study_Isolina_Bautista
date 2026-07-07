# %% [markdown]
# # Teil 4: Sparplan Deep Dive
#
# Goal: characterize recurring investment behavior — how many customers
# are genuinely "active" recurring investors, typical ticket size and
# cadence, and what gaps in the execution sequence might mean. Recurring
# investors matter commercially because they're typically stickier,
# more predictable revenue than one-off traders (see Teil 2 Q9).
#
# Carries forward Teil 1 handling: duplicate rows dropped (8.04% of this
# table, matching the Teil 1 finding exactly), orphaned customer IDs
# excluded.

# %%
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt

pd.set_option('display.max_columns', None)
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
FIG_DIR = Path(__file__).resolve().parent.parent / 'reports' / 'figures'
FIG_DIR.mkdir(parents=True, exist_ok=True)

customers = pd.read_csv(DATA_DIR / 'dim_customer_synthetic.csv', sep=';')
sp_raw = pd.read_csv(DATA_DIR / 'fct_saving_plan_anonymized.csv', sep=';')

sp = sp_raw.drop_duplicates().copy()
n_dupes = len(sp_raw) - len(sp)
print(f"Dropped {n_dupes:,} duplicate rows ({n_dupes / len(sp_raw):.2%})")
sp = sp[sp['synthetic_customer_id'].isin(customers['synthetic_customer_id'])]
sp['trade_date'] = pd.to_datetime(sp['date_id'], format='%Y%m%d')

# %% [markdown]
# ## Q: How many active Sparplans are there?
# Definition given: >=2 executions for the same customer + instrument.

# %%
series_counts = sp.groupby(['synthetic_customer_id', 'instrument_id']).size()
n_total_series = len(series_counts)
n_active = (series_counts >= 2).sum()
active_keys = series_counts[series_counts >= 2].index
active = sp.set_index(['synthetic_customer_id', 'instrument_id']).loc[active_keys].reset_index()
print(f"Total customer+instrument series: {n_total_series:,}")
print(f"Active Sparplans (>=2 executions): {n_active:,} ({n_active / n_total_series:.1%})")
print(f"One-time only: {(series_counts == 1).sum():,} ({(series_counts == 1).mean():.1%})")

# %% [markdown]
# **Finding:** only 11.2% of series ever repeat — the vast majority of
# Sparplan starts execute exactly once and never again. Before taking this
# at face value, checked two more mundane explanations:
#
# 1. **Right-censoring** (a plan just hasn't had time to execute a 2nd
#    installment yet): ruled out — median "days since first execution" is
#    **identical (282 days)** for one-time and active series. If recency
#    were the driver, one-time series would skew much more recent.
# 2. **Definition too narrow** (customer reallocates across different
#    instruments each cycle, so no single instrument repeats even though
#    the customer is a genuine recurring investor): also ruled out — of
#    customers where no instrument ever repeated, only 22.7% even tried a
#    second, different instrument. The other 77.3% executed exactly one
#    Sparplan installment, ever, on one instrument, and stopped.
#
# **Conclusion:** this is a genuine behavioral finding, not a measurement
# artifact — most attempted Sparplans are abandoned after the first
# installment. Worth escalating as a product/onboarding question (why do
# so few convert to a second installment?).
#
# *(Note on data reliability: this comparison only needs one date per
# series compared against other series' dates — safe, and confirmed to
# vary meaningfully across series, 2021-2026. This is different from
# comparing dates *within* one series to measure a gap, which the
# investigation further below shows is not reliable in this table.)*

# %% [markdown]
# ## Q: Average Sparplan amount and interval

# %%
print("gross_volume_eur (all executions):")
print(sp['gross_volume_eur'].describe(percentiles=[.5, .75, .9]).round(2))

# %% [markdown]
# **Finding:** median ticket size is only EUR 1.45 vs. a mean of EUR 49.75
# — extreme right skew, and the median is far below what's typically seen
# even for micro-investing/fractional-share products (real-world Sparplan
# minimums are usually EUR 25-50/month). Flagged as worth verifying against
# the source system rather than accepted at face value — could reflect
# genuine fractional-share micro-investing, or a unit/currency handling
# issue upstream.

# %%
# inter_val is structurally 0 for every first execution (no prior interval
# exists yet) - excluding those to get a meaningful cadence figure.
non_first = sp[sp['execution_sequence_number'] != 1]
print(f"inter_val, excluding first executions (n={len(non_first):,}):")
print(non_first['inter_val'].describe(percentiles=[.5, .75, .9]).round(1))
print()
print("Most common non-zero intervals (days):")
print(non_first[non_first['inter_val'] > 0]['inter_val'].value_counts().head(6))

# %% [markdown]
# **Finding:** excluding first executions, the most common intervals
# cluster around 7 (weekly), 14-17 (biweekly), and 28-32 (monthly) days —
# consistent with genuine recurring investment cadences. Still, 46% of
# non-first executions show `inter_val == 0` even after excluding seq=1 —
# investigated below, since a same-day repeat contradicts "recurring."

# %% [markdown]
# ### Investigating `inter_val == 0` beyond the first execution
#
# This investigation uncovered something more fundamental than expected —
# worth walking through carefully rather than glossing over.

# %%
sp_sorted = sp.sort_values(['synthetic_customer_id', 'instrument_id', 'execution_sequence_number'])
sp_sorted['prev_date'] = sp_sorted.groupby(['synthetic_customer_id', 'instrument_id'])['trade_date'].shift(1)
sp_sorted['prev_qty'] = sp_sorted.groupby(['synthetic_customer_id', 'instrument_id'])['exec_qty'].shift(1)
sp_sorted['actual_gap_days'] = (sp_sorted['trade_date'] - sp_sorted['prev_date']).dt.days

# Check: for NON-zero inter_val rows, does the recorded interval match the
# actual date_id gap between consecutive executions?
non_first_nonzero = sp_sorted[(sp_sorted['execution_sequence_number'] != 1) & (sp_sorted['inter_val'] > 0)].dropna(
    subset=['actual_gap_days'])
mismatch_rate = (non_first_nonzero['inter_val'] != non_first_nonzero['actual_gap_days']).mean()
print(f"Non-zero inter_val rows where recorded interval != actual date_id gap: {mismatch_rate:.1%}")

# %% [markdown]
# **Finding:** 99.9% mismatch — `inter_val` almost never matches the
# actual gap between consecutive `date_id` values. Investigated a full
# example series to understand why:

# %%
active_example_keys = series_counts[series_counts >= 5].index
ex_key = active_example_keys[0]
example = sp_sorted[(sp_sorted['synthetic_customer_id'] == ex_key[0]) &
                     (sp_sorted['instrument_id'] == ex_key[1])]
print(example[['execution_sequence_number', 'date_id', 'inter_val', 'exec_qty', 'exec_price']].to_string(index=False))

# %% [markdown]
# **Finding:** every row in this series shares the **same `date_id`**
# (2025-09-15) and **identical** `exec_qty`/`exec_price`, regardless of
# `execution_sequence_number` (1, 2, 3, 4, 9). Checked how common this is
# across all active series, not just this one example:

# %%
def check_series_dates(g):
    return pd.Series({'n_distinct_dates': g['date_id'].nunique(), 'n_distinct_prices': g['exec_price'].nunique()})


date_diag = active.groupby(['synthetic_customer_id', 'instrument_id']).apply(check_series_dates, include_groups=False)
pct_same_date = (date_diag['n_distinct_dates'] == 1).mean()
pct_same_price = (date_diag['n_distinct_prices'] == 1).mean()
print(f"Active series where ALL executions share the same date_id: {pct_same_date:.1%}")
print(f"Active series where ALL executions share the same exec_price: {pct_same_price:.1%}")

# %% [markdown]
# **Finding (important correction to an earlier draft of this analysis):**
# 99.4% of active series have every execution stamped with the *same*
# `date_id`, and 89.4% also share an identical `exec_price` across all
# executions. This means `date_id`/`trade_date` in this table behaves like
# a **snapshot/export date attached to the whole plan**, not a genuine
# per-installment execution timestamp — closer to "the date this record
# was captured" than "the date this specific payment happened."
#
# **Practical consequence:** `execution_sequence_number` and `inter_val`
# (likely reflecting the plan's *configured* cadence) remain usable — the
# weekly/monthly clustering reported above still stands. But **any
# metric computed from `date_id` at the individual-execution level (e.g.
# "days since last execution," "actual gap between installments") is
# not reliable** and should not be presented as measuring real elapsed
# time. This overturned a stronger claim in an earlier pass of this
# analysis (that sequence gaps predict plan dormancy via
# days-since-last-execution) — that comparison was inadvertently
# measuring snapshot-date artifacts, not customer behavior, and has been
# removed below rather than left in with false confidence.

# %% [markdown]
# ## Q: Execution sequence number — where are the gaps, and what might they mean?

# %%
def find_gaps(group):
    seqs = sorted(group['execution_sequence_number'].unique())
    n_missing = (seqs[-1] - seqs[0] + 1) - len(seqs)
    return pd.Series({'n_missing': n_missing})


gap_summary = active.groupby(['synthetic_customer_id', 'instrument_id']).apply(find_gaps, include_groups=False)
n_with_gaps = (gap_summary['n_missing'] > 0).sum()
print(f"Active Sparplans analyzed: {len(gap_summary):,}")
print(f"Series with at least one gap in execution_sequence_number: "
      f"{n_with_gaps:,} ({n_with_gaps / len(gap_summary):.1%})")
print(gap_summary[gap_summary['n_missing'] > 0]['n_missing'].describe(percentiles=[.5, .75, .9]).round(1))

# %% [markdown]
# **Finding:** 20.4% of active Sparplans have at least one gap in
# `execution_sequence_number` (median 4 missing numbers among those with
# gaps) — e.g. sequence 1, 2, 3, 4, 9 skips 5-8 entirely. This part of the
# finding is solid, since it only depends on the sequence numbers
# themselves (integers), not on the unreliable `date_id` field.
#
# **What the gap could mean (business interpretation, disclosed as
# hypothesis rather than proven, given the date-field limitation above):**
# a paused plan (temporarily suspended, e.g. insufficient funds) that
# later resumes without resetting the counter, or a scheduled installment
# that failed/was skipped. **We cannot currently confirm whether gaps
# predict eventual plan abandonment**, since the only date field available
# per execution isn't a trustworthy timestamp at the individual-row level
# — this would need a genuinely reliable per-installment date to test
# properly, which is flagged as a data request rather than answered with
# invented confidence.

# %% [markdown]
# ## Visualizing execution frequency per series

# %%
fig, ax = plt.subplots(figsize=(8, 4.5))
capped = series_counts.clip(upper=10)
ax.hist(capped, bins=range(1, 12), color='#2E5A88', edgecolor='white', align='left')
ax.set_xlabel('Executions per customer+instrument series (capped at 10+)')
ax.set_ylabel('Number of series')
ax.set_title('Sparplan Execution Frequency', fontweight='bold')
ax.spines[['top', 'right']].set_visible(False)
plt.tight_layout()
plt.savefig(FIG_DIR / 'q16_sparplan_execution_frequency.png', dpi=150, bbox_inches='tight')
plt.close()
print(f"Saved chart to {FIG_DIR / 'q16_sparplan_execution_frequency.png'}")