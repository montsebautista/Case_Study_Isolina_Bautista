# %% [markdown]
# # Teil 2: Revenue-Analyse
#
# Goal: trades carry no revenue figure directly — it must be reconstructed
# by matching each trade against the pricing rules in
# `int_revenue_condition.csv` (Q5), then aggregated by instrument type
# (Q6), issuer (Q7), platform (Q8), and customer segment (Q9).
#
# Builds on the data-quality findings from Teil 1: duplicate rows are
# dropped and orphaned customer references are excluded here too, using
# the same logic established in `01_data_exploration.py`.
#
# Rule-matching/pricing logic lives in `src/revenue_engine.py` (reusable,
# will also be needed for Sparplan revenue in a future extension of Teil 4).

# %%
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.revenue_engine import load_and_clean_rules, build_issuer_mapping, match_and_price_trades

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

customers = pd.read_csv(DATA_DIR / 'dim_customer_synthetic.csv', sep=';')
instruments = pd.read_csv(DATA_DIR / 'dim_instrument.csv', sep=';')
trades_raw = pd.read_csv(DATA_DIR / 'fct_trade_anonymized.csv', sep=';')

# %% [markdown]
# ## Pre-processing: carry forward Teil 1 data quality handling
#
# Two Teil 1 findings materially affect revenue totals if left unhandled:
# duplicate rows would overstate revenue by ~5%, and orphaned customer IDs
# have no attributes available for segmentation (Q9).

# %%
n_before = len(trades_raw)
trades = trades_raw.drop_duplicates().copy()
print(f"Dropped {n_before - len(trades)} duplicate rows ({(n_before - len(trades)) / n_before:.2%})")

trades['cust_matched'] = trades['synthetic_customer_id'].isin(customers['synthetic_customer_id'])
print(f"Orphaned customer trades: {(~trades['cust_matched']).sum()} ({(~trades['cust_matched']).mean():.2%}) "
      f"-- kept for Q5-Q8 (instrument/platform-level), excluded from Q9 (customer-level)")

# %% [markdown]
# ## Q5: Build the revenue engine & apply it to all trades
#
# **Documented assumptions** (see `src/revenue_engine.py` docstrings for
# implementation detail):
#
# - **Scope:** `order_type == 'TRADE'` only. The same rulebook also prices
#   `SAVINGSPLAN`/`AUM`, out of scope here.
# - **Issuer matching:** `dim_instrument.emittent` is only populated for
#   DERIVAT/ETF (verified: 0% for AKTIE/FONDS/KRYPTO — structural, not a
#   data bug: stocks/funds don't have a "structuring issuer"). DERIVAT/ETF
#   trades are matched on `instrument_type` + issuer; AKTIE/FONDS on
#   `instrument_type` only; KRYPTO has no `TRADE` rules at all.
# - **Masked ISINs:** ~830 derivative/ETF ISINs use an anonymization
#   placeholder pattern (`...XXXX`), causing 112 ISINs to collide multiple
#   products onto one code. Modal emittent taken as best effort; checked
#   impact below.
# - **Rule priority:** trade-specific volume band > staffel band (approximated
#   using the trade's own volume, since true cumulative per-customer volume
#   tracking is out of scope) > generic catch-all rule; ties broken by most
#   recent `valid_from`.
# - Unmatched trades (no instrument, or no rule) are counted and reported,
#   never silently dropped.

# %%
rules = load_and_clean_rules()
isin_map = build_issuer_mapping(instruments)
priced, coverage = match_and_price_trades(trades, rules, isin_map)

print("Coverage:")
for k, v in coverage.items():
    print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v:,.2f}")

# %% [markdown]
# **Findings:** of 95,692 de-duplicated trades, 22,210 (23.2%) have no
# instrument match (consistent with the 22.04% orphan rate found on the
# raw table in Teil 1 — the gap is proportional, not dedup-related), a
# further 28,451 are matched to an instrument but no rule applies, leaving
# **50,114 priced trades and EUR 45,630.74 total computed revenue**. This
# is a documented **lower bound**, not a complete P&L — see Q6 for why.
#
# The 112 emittent-ambiguous masked ISINs (`ambiguous_issuer_trades: 0`)
# turn out not to affect any actual trade — checked directly, not assumed.

# %% [markdown]
# ### Sanity check: computed revenue vs. the pre-existing `cost` column
#
# `fct_trade_anonymized.cost` already exists — worth checking whether it's
# actually the same thing we just computed (in which case our engine would
# be redundant) or something else.

# %%
chk = priced.dropna(subset=['cost', 'revenue_eur'])
print(f"n = {len(chk):,}")
print(f"correlation(revenue_eur, cost) = {chk['revenue_eur'].corr(chk['cost']):.3f}")
print(chk[['revenue_eur', 'cost']].describe().round(3))

# %% [markdown]
# **Finding:** weak correlation (r=0.16); `cost` is tightly distributed
# around ~EUR 0.51 (std 0.05) regardless of trade size, while `revenue_eur`
# scales with volume. This indicates `cost` reflects the broker's own flat
# execution/clearing cost, not customer-facing revenue — confirming
# `revenue_eur` measures a genuinely distinct, correct concept. Bonus
# metric this unlocks: **gross margin per trade = revenue_eur - cost**.

# %% [markdown]
# ## Q6: Total revenue by instrument type

# %%
q6 = (priced.groupby('instrument_type')
      .agg(total_revenue_eur=('revenue_eur', 'sum'),
           n_priced_trades=('revenue_eur', 'count'),
           avg_revenue_eur=('revenue_eur', 'mean'))
      .sort_values('total_revenue_eur', ascending=False)
      .round(2))
print(q6)

# %% [markdown]
# **Findings:** AKTIE (EUR 28,238) and FONDS (EUR 15,004) dominate — not
# because they're inherently more profitable, but because the rulebook has
# ~99% coverage there. **DERIVAT computed revenue is EUR 15 from just 14 of
# 11,518 trades (0.1% coverage)** — investigated below, this is a real
# pricing-coverage gap, not a matching bug. **KRYPTO has zero `TRADE`
# rules defined at all** — 0 revenue is a rulebook gap, not a calculation
# error.

# %% [markdown]
# ### Investigating the DERIVAT coverage gap

# %%
derivat = trades.merge(isin_map, left_on='instrument_id', right_on='isin', how='left')
derivat = derivat[derivat['instrument_type'] == 'DERIVAT']
top_issuer = derivat['emittent'].value_counts().idxmax()
top_issuer_trades = derivat[derivat['emittent'] == top_issuer]

rhea_rule = rules[(rules['instrument_type'] == 'DERIVAT') & (rules['issuer'] == top_issuer)]
print(f"Dominant DERIVAT issuer: {top_issuer} ({len(top_issuer_trades):,} of {len(derivat):,} DERIVAT trades)")
print(f"Its only rule requires gross_volume_eur >= "
      f"{rhea_rule['trade_gross_volume_from_eur'].iloc[0]:,.2f}")
print(f"Median trade size for this issuer: EUR {top_issuer_trades['gross_volume_eur'].median():,.2f}")
pct_above = (top_issuer_trades['gross_volume_eur'] > rhea_rule['trade_gross_volume_from_eur'].iloc[0]).mean()
print(f"Share of its trades clearing that threshold: {pct_above:.2%}")

# %% [markdown]
# **Finding:** {top_issuer} accounts for ~85% of all DERIVAT trades, but
# its only defined rule only prices trades above roughly EUR 9,500 — its
# median trade is under EUR 800. This is a genuine rulebook coverage gap
# for the dominant derivative issuer, worth escalating to a pricing/product
# stakeholder rather than engineering around it.

# %% [markdown]
# ## Q7: Top 5 issuers (Emittenten) by revenue

# %%
q7 = (priced.groupby('emittent')
      .agg(total_revenue_eur=('revenue_eur', 'sum'), n_priced_trades=('revenue_eur', 'count'))
      .sort_values('total_revenue_eur', ascending=False)
      .head(5).round(2))
print(q7)

# %% [markdown]
# **Finding:** top issuer by revenue is an ETF issuer, not a DERIVAT one —
# consistent with Q6 (DERIVAT revenue is structurally suppressed by the
# coverage gap above, so issuer-level revenue rankings should not be read
# as "which issuer is most valuable," only "which issuer is best-covered
# by the current rulebook."

# %% [markdown]
# ## Q8: Average revenue per trade, by platform
#
# `platform` has 116 raw device-string values (e.g.
# `ios_26.2.1_iphone16,1`) — bucketed into coarse OS/channel groups first.

# %%
def bucket_platform(p: str) -> str:
    p = str(p).lower()
    if p == 'unknown':
        return 'Unknown'
    if p.startswith('ios'):
        return 'iOS App'
    if p.startswith('android'):
        return 'Android App'
    if p.startswith('mac'):
        return 'Web (Mac browser)'
    if p == 'web':
        return 'Web'
    return 'Other'


priced['platform_group'] = priced['platform'].apply(bucket_platform)
q8 = (priced.groupby('platform_group')
      .agg(avg_revenue_eur=('revenue_eur', 'mean'),
           total_revenue_eur=('revenue_eur', 'sum'),
           n_priced_trades=('revenue_eur', 'count'))
      .sort_values('avg_revenue_eur', ascending=False)
      .round(3))
print(q8)

# %% [markdown]
# **Finding:** fairly tight spread (EUR 0.73-1.19 avg/trade across
# platforms) — platform is not a strong revenue driver on its own.

# %% [markdown]
# ## Q9: Customer segments based on trading behaviour
#
# Segmentation uses the **full de-duplicated trade set**, excluding the
# 2.95% of trades with an orphaned customer ID (no attributes available —
# see Teil 1 DQ Issue #2), consistent with how Teil 1 says orphaned
# customers should be treated in customer-level analysis. Profitability is
# then measured on the priced subset. Segments are assigned by priority so
# they are mutually exclusive; thresholds are data-driven percentiles, not
# arbitrary round numbers.

# %%
trades_for_seg = trades[trades['cust_matched']].merge(
    isin_map, left_on='instrument_id', right_on='isin', how='left')

cust = trades_for_seg.groupby('synthetic_customer_id').agg(
    n_trades=('instrument_id', 'size'),
    total_volume_eur=('gross_volume_eur', 'sum'),
    n_derivat=('instrument_type', lambda s: (s == 'DERIVAT').sum()),
).reset_index()
cust['derivat_share'] = cust['n_derivat'] / cust['n_trades']

vol_p95 = cust['total_volume_eur'].quantile(0.95)
median_trades = cust['n_trades'].median()

conditions = [
    cust['n_trades'] == 1,
    cust['total_volume_eur'] >= vol_p95,
    cust['derivat_share'] >= 0.3,
    cust['n_trades'] <= median_trades,
]
choices = ['1. One-Time Traders', '2. High-Volume / Power Traders',
           '3. Derivative-Focused Traders', '4. Buy-and-Hold / Core Investors']
cust['segment'] = np.select(conditions, choices, default='5. Regular Active Traders')

rev_by_cust = priced.groupby('synthetic_customer_id')['revenue_eur'].agg(['sum', 'count']).reset_index()
rev_by_cust.columns = ['synthetic_customer_id', 'priced_revenue_eur', 'n_priced_trades']
seg = cust.merge(rev_by_cust, on='synthetic_customer_id', how='left').fillna(
    {'priced_revenue_eur': 0, 'n_priced_trades': 0})

q9 = seg.groupby('segment').agg(
    n_customers=('synthetic_customer_id', 'size'),
    total_trades=('n_trades', 'sum'),
    total_priced_revenue_eur=('priced_revenue_eur', 'sum'),
    mean_revenue_per_customer=('priced_revenue_eur', 'mean'),
    median_revenue_per_customer=('priced_revenue_eur', 'median'),
).sort_index().round(2)
print(q9)
print(f"\nMost profitable segment (mean & median revenue/customer): "
      f"{q9['mean_revenue_per_customer'].idxmax()}")

# %% [markdown]
# **Findings:** **High-Volume/Power Traders** is the most profitable
# segment on both a mean (EUR 3.18/customer) and median (EUR 2.57/customer)
# basis — roughly 2.5x the next-best segment — and the median holds up
# despite this segment containing the single largest trade in the dataset
# (~EUR 98.6M, likely institutional), ruling out a pure outlier artifact.
#
# **Caveat:** One-Time Traders show ~EUR 0 median revenue, but this is
# partly mechanical — many of their trades fall into the DERIVAT coverage
# gap from Q6, so "least profitable" here is confounded with "least
# priceable," not a clean behavioral finding.