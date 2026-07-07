"""
Revenue calculation logic for Teil 2.

Reusable functions for matching trades against int_revenue_condition.csv
pricing rules and computing per-trade revenue. Kept separate from the
notebook so the rule-matching logic can be unit-tested / reused (e.g. for
a future Sparplan revenue calc in Teil 4).
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

ISSUER_MATCHED_TYPES = {'DERIVAT', 'ETF'}  # only types where dim_instrument.emittent is populated


def load_and_clean_rules() -> pd.DataFrame:
    """Load int_revenue_condition.csv, drop the one garbage row, scope to
    order_type == 'TRADE' (Sparplan/AUM conditions are out of scope here)."""
    rules_raw = pd.read_csv(DATA_DIR / 'int_revenue_condition.csv', sep=';')
    rules = rules_raw.dropna(subset=['order_type'])
    rules = rules[rules['order_type'] == 'TRADE'].copy()
    rules['valid_from'] = pd.to_datetime(rules['valid_from'])
    return rules


def build_issuer_mapping(instruments: pd.DataFrame) -> pd.DataFrame:
    """Best-effort ISIN -> (instrument_type, emittent) map.

    A subset of masked/anonymized derivative ISINs (pattern '...XXXX')
    collide multiple distinct products onto one code, producing a handful
    of ISINs with >1 distinct emittent. We take the modal emittent per ISIN
    and flag the ambiguous ones so the risk is quantified, not hidden.
    """
    isin_map = (instruments.groupby('isin')
                .agg(instrument_type=('instrument_type', lambda s: s.mode().iat[0]),
                     emittent=('emittent', lambda s: s.mode().iat[0] if s.notna().any() else np.nan),
                     n_distinct_emittent=('emittent', 'nunique'))
                .reset_index())
    isin_map['emittent_ambiguous'] = isin_map['n_distinct_emittent'] > 1
    return isin_map


def match_and_price_trades(trades: pd.DataFrame, rules: pd.DataFrame,
                            isin_map: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Match each trade to the best-fitting revenue rule and price it.

    Match priority per trade (documented in notebooks/02_revenue_analysis.py):
      Tier 1: rule's trade_gross_volume band contains this trade's volume
      Tier 2: rule's staffel band contains this trade's volume (approximation
              -- see notebook for why true cumulative volume isn't used)
      Tier 3: generic rule, no band constraints
    Ties within a tier broken by most recent valid_from.

    Returns (priced_trades_df, coverage_stats_dict).
    """
    trades = trades.copy()
    trades['exec_datetime'] = pd.to_datetime(trades['exec_datetime'])
    trades['trade_row_id'] = np.arange(len(trades))

    trades = trades.merge(isin_map, left_on='instrument_id', right_on='isin', how='left')
    n_no_instrument = trades['instrument_type'].isna().sum()
    n_ambiguous_issuer_trades = trades.loc[
        trades['emittent_ambiguous'] == True, 'trade_row_id'].nunique()

    scoped = trades.dropna(subset=['instrument_type']).copy()

    issuer_matched = scoped[scoped['instrument_type'].isin(ISSUER_MATCHED_TYPES)]
    issuer_agnostic = scoped[~scoped['instrument_type'].isin(ISSUER_MATCHED_TYPES)]

    cand_a = issuer_matched.merge(
        rules, left_on=['instrument_type', 'emittent'], right_on=['instrument_type', 'issuer'],
        how='left', suffixes=('', '_rule'))
    cand_b = issuer_agnostic.merge(rules, on='instrument_type', how='left', suffixes=('', '_rule'))
    candidates = pd.concat([cand_a, cand_b], ignore_index=True)

    candidates = candidates[candidates['valid_from'] <= candidates['exec_datetime']]

    vol = candidates['gross_volume_eur']
    in_vol_band = (
        (candidates['trade_gross_volume_from_eur'].isna() | (vol >= candidates['trade_gross_volume_from_eur'])) &
        (candidates['trade_gross_volume_to_eur'].isna() | (vol <= candidates['trade_gross_volume_to_eur'])) &
        candidates['trade_gross_volume_from_eur'].notna()
    )
    in_staffel_band = (
        (candidates['staffel_start'].isna() | (vol >= candidates['staffel_start'])) &
        (candidates['staffel_end'].isna() | (vol <= candidates['staffel_end'])) &
        candidates['staffel_start'].notna()
    )
    is_generic = candidates['trade_gross_volume_from_eur'].isna() & candidates['staffel_start'].isna()

    candidates['tier'] = np.select([in_vol_band, in_staffel_band, is_generic], [1, 2, 3], default=99)
    candidates = candidates[candidates['tier'] != 99]
    candidates = candidates.dropna(subset=['revenue_condition_id'])

    candidates = candidates.sort_values(['trade_row_id', 'tier', 'valid_from'], ascending=[True, True, False])
    best = candidates.drop_duplicates(subset='trade_row_id', keep='first').copy()

    n_unmatched_after_scoping = scoped['trade_row_id'].nunique() - best['trade_row_id'].nunique()

    def _compute_revenue(row):
        if row['condition_type'] == 'PER_EUR' and row['value_type'] == 'PERCENT':
            rev = row['gross_volume_eur'] * row['value'] / 100
        elif row['condition_type'] == 'PER_TRADE' and row['value_type'] == 'ABSOLUTE':
            rev = row['value']
        elif row['condition_type'] == 'PER_STCK' and row['value_type'] == 'ABSOLUTE':
            rev = row['exec_qty'] * row['value']
        else:
            return np.nan
        if pd.notna(row['cap_eur']):
            rev = min(rev, row['cap_eur'])
        return rev

    best['revenue_eur'] = best.apply(_compute_revenue, axis=1)

    coverage = {
        'total_trades': len(trades),
        'no_instrument_match': int(n_no_instrument),
        'ambiguous_issuer_trades': int(n_ambiguous_issuer_trades),
        'no_rule_found': int(n_unmatched_after_scoping),
        'priced_trades': int(best['revenue_eur'].notna().sum()),
        'total_revenue_eur': round(float(best['revenue_eur'].sum()), 2),
    }
    return best, coverage