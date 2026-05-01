#!/usr/bin/env python3
"""
backtest.py — Pure DCA vs Smart DCA on Taiwan ETFs

Usage:
    python backtest.py --ticker 0050.TW --start 2014-07 --end 2026-04 --monthly 3000

Requires:
    pip install yfinance pandas numpy matplotlib
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Transaction cost constants (Taiwan market) ─────────────────────────────────
BUY_FEE  = 0.001425   # brokerage fee on buy  (0.1425%)
SELL_FEE = 0.001425   # brokerage fee on sell (0.1425%)
SELL_TAX = 0.003      # securities transaction tax on sell (0.3%)

# Indicator warm-up: MA200 needs 200 trading days, 6M momentum needs 126
# Fetch extra data so the backtest window starts with valid signals
WARMUP_DAYS = 300


# ── 1. CLI ─────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest Pure DCA vs Smart DCA on a Taiwan ETF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ticker",  default="0050.TW",  help="Yahoo Finance ticker  (default: 0050.TW)")
    p.add_argument("--start",   default="2014-07",  help="Start month YYYY-MM   (default: 2014-07)")
    p.add_argument("--end",     default="2026-04",  help="End month YYYY-MM     (default: 2026-04)")
    p.add_argument("--monthly", type=float, default=3000, help="Monthly budget TWD (default: 3000)")
    p.add_argument("--output",  default="output",   help="Output folder          (default: output/)")
    return p.parse_args()


# ── 2. Data fetching ───────────────────────────────────────────────────────────
def fetch_prices(ticker: str, start_month: str, end_month: str) -> pd.Series:
    """
    Download dividend-adjusted daily close prices from Yahoo Finance.
    Fetches extra history before start_month so MA200 and momentum are valid
    on the first backtest date.
    """
    fetch_start = (pd.Timestamp(start_month + "-01")
                   - pd.DateOffset(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    fetch_end   = (pd.Timestamp(end_month + "-01")
                   + pd.DateOffset(months=2)).strftime("%Y-%m-%d")

    raw = yf.download(ticker, start=fetch_start, end=fetch_end,
                      auto_adjust=True, progress=False)
    if raw.empty:
        sys.exit(f"[ERROR] No data returned for '{ticker}'. Check the ticker symbol.")

    # yfinance may return MultiIndex columns; flatten to Series
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"].iloc[:, 0]
    else:
        close = raw["Close"]

    close = close.squeeze().dropna()
    close.index = pd.to_datetime(close.index)
    return close.sort_index()


def check_quality(prices: pd.Series, start_month: str) -> None:
    """
    Warn on suspicious single-day price moves within the backtest window.
    Skips the warm-up period because adjusted-price recalculations from
    yfinance can produce artefactual large moves in older data.
    """
    start = pd.Timestamp(start_month + "-01")
    ret = prices.loc[start:].pct_change().dropna()
    drops  = ret[ret < -0.30]
    spikes = ret[ret >  0.30]
    for label, series in [("DROP", drops), ("SPIKE", spikes)]:
        for dt, r in series.items():
            print(f"[WARNING] Unusual {label} on {dt.date()}: {r:+.1%} — verify data")


# ── 3. Monthly schedule ────────────────────────────────────────────────────────
def get_monthly_dates(prices: pd.Series,
                      start_month: str,
                      end_month: str) -> pd.DatetimeIndex:
    """
    Return the first actual trading day of each month in [start_month, end_month].
    This is when each DCA purchase is executed.
    """
    start = pd.Timestamp(start_month + "-01")
    end   = pd.Timestamp(end_month   + "-01") + pd.DateOffset(months=1)

    trading_days = prices.loc[start:end].index
    result = []
    for month_start in pd.date_range(start, end - pd.Timedelta(days=1), freq="MS"):
        month_end = month_start + pd.DateOffset(months=1)
        days_in_month = trading_days[(trading_days >= month_start) &
                                     (trading_days <  month_end)]
        if len(days_in_month) > 0:
            result.append(days_in_month[0])
    return pd.DatetimeIndex(result)


# ── 4. Smart DCA signal ────────────────────────────────────────────────────────
def compute_signal(prices: pd.Series) -> pd.Series:
    """
    Compute the Smart DCA monthly investment multiplier.

    Two factors (both use t-1 data — no look-ahead):
      1. MA200 distance: price relative to its 200-day MA
         → price below MA200 (cheap) → buy more
      2. 6-month momentum: trailing 126-day return
         → negative momentum (falling) → buy more

    Each factor is normalised to [-1, +1] and combined equally.
    Multiplier is clamped to [0.5, 2.0]:
      1.0 = normal month, 2.0 = maximum top-up, 0.5 = pull back
    """
    # Shift by 1 day: signal computed from yesterday's price, executed today
    p = prices.shift(1)

    ma200     = p.rolling(200, min_periods=150).mean()
    ma_dist   = (p - ma200) / ma200          # + above MA, - below

    mom_6m    = p / p.shift(126) - 1        # 126 trading days ≈ 6 months

    # Invert so "below/falling" maps to positive (more buying)
    ma_score  = (-ma_dist).clip(-0.30, 0.30) / 0.30   # [-1, +1]
    mom_score = (-mom_6m ).clip(-0.30, 0.30) / 0.30   # [-1, +1]

    combined   = 0.5 * ma_score + 0.5 * mom_score     # [-1, +1]
    multiplier = (1.0 + combined).clip(0.5, 2.0)

    return multiplier


# ── 5. Backtest engine ─────────────────────────────────────────────────────────
def run_backtest(monthly_dates: pd.DatetimeIndex,
                 prices: pd.Series,
                 monthly_amount: float,
                 multipliers: Optional[pd.Series] = None) -> pd.DataFrame:
    """
    Simulate monthly DCA purchases.

    Pure DCA:  multipliers=None  → invest exactly monthly_amount each month.
    Smart DCA: multipliers given → invest monthly_amount * multiplier each month.

    No look-ahead guarantee: multiplier at date t uses prices.shift(1),
    so the signal is based on t-1 closing data; execution is at t open/close.

    Transaction cost on buy: BUY_FEE applied per purchase.
    Fractional shares allowed (models continuous investment; in practice
    round to whole shares or lots as your broker permits).
    """
    records = []
    shares  = 0.0
    total_invested = 0.0

    for buy_date in monthly_dates:
        price = float(prices.asof(buy_date))
        if np.isnan(price) or price <= 0:
            continue

        mult   = float(multipliers.asof(buy_date)) if multipliers is not None else 1.0
        invest = monthly_amount * mult

        # Shares acquired after brokerage fee
        shares_bought  = invest / (price * (1.0 + BUY_FEE))
        shares        += shares_bought
        total_invested += invest

        market_value = shares * price

        records.append({
            "date":             buy_date,
            "price":            round(price, 2),
            "multiplier":       round(mult, 4),
            "invest_this_month":round(invest, 2),
            "shares_bought":    round(shares_bought, 6),
            "total_shares":     round(shares, 6),
            "total_invested":   round(total_invested, 2),
            "market_value":     round(market_value, 2),
            "unrealized_pnl":   round(market_value - total_invested, 2),
            "pnl_pct":          round((market_value / total_invested - 1) * 100, 2),
        })

    return pd.DataFrame(records).set_index("date")


# ── 6. Performance metrics ─────────────────────────────────────────────────────
def compute_metrics(df: pd.DataFrame) -> dict:
    """
    Compute summary statistics for one strategy's backtest result.

    Note on CAGR: DCA's true return metric is XIRR (money-weighted).
    This function reports a simplified annualised return:
        (final_value / total_invested) ^ (1 / years) - 1
    This slightly overstates performance vs XIRR because early invested
    capital compounds longer, but it is comparable between the two strategies.
    """
    final_value    = df["market_value"].iloc[-1]
    total_invested = df["total_invested"].iloc[-1]
    n_years        = (df.index[-1] - df.index[0]).days / 365.25

    total_return = final_value / total_invested - 1
    ann_return   = (1 + total_return) ** (1 / n_years) - 1

    # Max drawdown on portfolio value curve
    mv     = df["market_value"]
    peak   = mv.cummax()
    max_dd = ((mv - peak) / peak).min()

    if max_dd < -0.50:
        print(f"[STOP WARNING] Max drawdown {max_dd:.1%} exceeds 50% — review strategy before live use")

    return {
        "total_invested": total_invested,
        "final_value":    final_value,
        "profit":         final_value - total_invested,
        "total_return":   total_return * 100,
        "ann_return":     ann_return * 100,
        "max_drawdown":   max_dd * 100,
        "n_months":       len(df),
        "n_years":        round(n_years, 1),
    }


# ── 7. Output ──────────────────────────────────────────────────────────────────
def save_csv(df_pure: pd.DataFrame, df_smart: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df_pure.to_csv(out_dir  / "pure_dca.csv")
    df_smart.to_csv(out_dir / "smart_dca.csv")
    print(f"[OK] CSV  → {out_dir}/pure_dca.csv, smart_dca.csv")


def save_png(df_pure: pd.DataFrame, df_smart: pd.DataFrame,
             ticker: str, out_dir: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True,
                                    gridspec_kw={"height_ratios": [3, 1]})

    # ── Top panel: portfolio value ──
    ax1.plot(df_pure.index,  df_pure["market_value"],
             label="Pure DCA value",  color="#1976D2", linewidth=2)
    ax1.plot(df_smart.index, df_smart["market_value"],
             label="Smart DCA value", color="#F57C00", linewidth=2)
    ax1.plot(df_pure.index,  df_pure["total_invested"],
             label="Cumulative invested", color="#78909C",
             linewidth=1.5, linestyle="--")
    ax1.set_ylabel("TWD", fontsize=11)
    ax1.set_title(f"Pure DCA vs Smart DCA — {ticker}", fontsize=13, fontweight="bold")
    ax1.legend(fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax1.grid(alpha=0.25)

    # ── Bottom panel: Smart DCA multiplier ──
    mult = df_smart["multiplier"]
    ax2.plot(mult.index, mult, color="#388E3C", linewidth=1.5, label="Multiplier")
    ax2.axhline(1.0, color="#9E9E9E", linestyle="--", linewidth=1)
    ax2.fill_between(mult.index, mult, 1.0,
                     where=(mult >= 1.0), alpha=0.20, color="#388E3C", label="Buy more")
    ax2.fill_between(mult.index, mult, 1.0,
                     where=(mult <  1.0), alpha=0.20, color="#E53935", label="Buy less")
    ax2.set_ylim(0.3, 2.3)
    ax2.set_ylabel("Multiplier", fontsize=11)
    ax2.set_xlabel("Date", fontsize=11)
    ax2.legend(fontsize=9, loc="upper right")
    ax2.grid(alpha=0.25)

    plt.tight_layout()
    out_path = out_dir / "backtest.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[OK] Chart → {out_path}")


def print_summary(m_pure: dict, m_smart: dict, args: argparse.Namespace) -> None:
    W = 48
    def row(label, pure_val, smart_val, fmt="{:>12}"):
        return (f"  {label:<20}"
                f"{fmt.format(pure_val)}"
                f"  │  "
                f"{fmt.format(smart_val)}")

    print()
    print("═" * W)
    print(f"  {args.ticker}  {args.start} → {args.end}  (monthly {args.monthly:,.0f} TWD)")
    print("═" * W)
    print(f"  {'':20}{'Pure DCA':>12}  │  {'Smart DCA':>12}")
    print("─" * W)
    print(row("Total invested",
              f"{m_pure['total_invested']:>12,.0f}",
              f"{m_smart['total_invested']:>12,.0f}", fmt="{}"))
    print(row("Final value",
              f"{m_pure['final_value']:>12,.0f}",
              f"{m_smart['final_value']:>12,.0f}", fmt="{}"))
    print(row("Profit",
              f"{m_pure['profit']:>12,.0f}",
              f"{m_smart['profit']:>12,.0f}", fmt="{}"))
    print(row("Total return",
              f"{m_pure['total_return']:>11.2f}%",
              f"{m_smart['total_return']:>11.2f}%", fmt="{}"))
    print(row("Ann. return",
              f"{m_pure['ann_return']:>11.2f}%",
              f"{m_smart['ann_return']:>11.2f}%", fmt="{}"))
    print(row("Max drawdown",
              f"{m_pure['max_drawdown']:>11.2f}%",
              f"{m_smart['max_drawdown']:>11.2f}%", fmt="{}"))
    print(row("Months",
              f"{m_pure['n_months']:>12}",
              f"{m_smart['n_months']:>12}", fmt="{}"))
    print("─" * W)
    delta = m_smart["ann_return"] - m_pure["ann_return"]
    print(f"  Smart DCA edge: {delta:+.2f}% annualised return")
    print("═" * W)
    print()


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    args = parse_args()
    out_dir = Path(args.output)

    print(f"[INFO] Fetching {args.ticker} (includes {WARMUP_DAYS}-day warm-up) ...")
    prices = fetch_prices(args.ticker, args.start, args.end)
    print(f"[INFO] {len(prices)} trading days  "
          f"({prices.index[0].date()} → {prices.index[-1].date()})")
    check_quality(prices, args.start)

    monthly_dates = get_monthly_dates(prices, args.start, args.end)
    print(f"[INFO] {len(monthly_dates)} monthly purchase dates "
          f"({monthly_dates[0].date()} → {monthly_dates[-1].date()})")

    print("[INFO] Computing Smart DCA signal (MA200 + 6M momentum) ...")
    multipliers = compute_signal(prices)

    print("[INFO] Running Pure DCA ...")
    df_pure  = run_backtest(monthly_dates, prices, args.monthly, multipliers=None)

    print("[INFO] Running Smart DCA ...")
    df_smart = run_backtest(monthly_dates, prices, args.monthly, multipliers=multipliers)

    m_pure  = compute_metrics(df_pure)
    m_smart = compute_metrics(df_smart)

    save_csv(df_pure, df_smart, out_dir)
    save_png(df_pure, df_smart, args.ticker, out_dir)
    print_summary(m_pure, m_smart, args)


if __name__ == "__main__":
    main()
