"""Financial risk calculations shared by the data-processor (training) and the risk-engine (live analysis).

Keeping one implementation guarantees the ML model sees features computed exactly the same way
during training and when a user's portfolio is analysed.
"""
import numpy as np
import pandas as pd

TRADING_DAYS = 252
LOOKBACK = 252          # 1 trading year of history used for features
HORIZON = 126           # ~6 months ahead used for the training label
MAX_DAILY_MOVE = 0.35   # |daily return| above this is a stock split / bonus / data error, not a real move

FEATURES = [
    "volatility", "avg_return", "max_drawdown", "sector_concentration", "n_stocks",
    "avg_correlation", "trading_volume", "market_volatility", "beta", "var_95",
]

FEATURE_LABELS = {
    "volatility": "Portfolio volatility",
    "avg_return": "Average return",
    "max_drawdown": "Maximum drawdown",
    "sector_concentration": "Sector concentration",
    "n_stocks": "Number of stocks",
    "avg_correlation": "Average correlation",
    "trading_volume": "Trading volume (liquidity)",
    "market_volatility": "Market volatility",
    "beta": "Beta",
    "var_95": "Value at Risk (95%)",
}

SECTOR_NAMES = {
    "FINANCIAL SERVICES": "Banking & Finance",
    "IT": "IT",
    "ENERGY": "Energy",
    "CONSUMER GOODS": "FMCG & Consumer",
    "AUTOMOBILE": "Automobile",
    "METALS": "Metals",
    "PHARMA": "Pharma",
    "CEMENT & CEMENT PRODUCTS": "Cement",
    "TELECOM": "Telecom",
    "CONSTRUCTION": "Construction",
    "FERTILISERS & PESTICIDES": "Agri-chemicals",
    "MEDIA & ENTERTAINMENT": "Media",
    "SERVICES": "Services",
}


def clean_stock_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Daily returns + traded value for one stock CSV from the Kaggle NIFTY50 dataset."""
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date")
    ret = df["Close"].pct_change()
    # Prices in this dataset are NOT adjusted for splits/bonuses, so those days show fake
    # -50%..-90% "crashes". Treat such days as no price change.
    ret = ret.where(ret.abs() <= MAX_DAILY_MOVE, 0.0)
    out = pd.DataFrame({
        "ret": ret.values,
        "value": (df["Volume"] * df["VWAP"]).values,  # rupees traded that day
    }, index=df["Date"].values)
    return out.iloc[1:]


def max_drawdown(daily_returns: np.ndarray) -> float:
    """Largest peak-to-trough fall of a growth path, as a positive fraction (0.25 = 25%)."""
    wealth = np.cumprod(1 + daily_returns)
    peak = np.maximum.accumulate(np.concatenate([[1.0], wealth]))[1:]
    return float(np.max(1 - wealth / peak)) if len(wealth) else 0.0


def sector_weights(weights: np.ndarray, sectors: list) -> dict:
    out = {}
    for w, s in zip(weights, sectors):
        out[s] = out.get(s, 0.0) + float(w)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def weighted_avg_correlation(corr: np.ndarray, weights: np.ndarray) -> float:
    n = len(weights)
    if n < 2:
        return 1.0
    ww = np.outer(weights, weights)
    mask = ~np.eye(n, dtype=bool)
    denom = ww[mask].sum()
    return float((corr[mask] * ww[mask]).sum() / denom) if denom > 0 else 1.0


def portfolio_features(stock_rets: np.ndarray, weights: np.ndarray, sectors: list,
                       market_rets: np.ndarray, traded_value: np.ndarray) -> dict:
    """The 10 model features from the project design, computed on a LOOKBACK window.

    stock_rets   : (days, n) daily returns of the holdings
    weights      : (n,) portfolio weights summing to 1
    sectors      : n sector names
    market_rets  : (days,) NIFTY50 equal-weight market proxy returns
    traded_value : (n,) average daily rupees traded per holding, divided by the median stock's
                   traded value in the same window (so 2004 and 2021 are comparable)
    """
    port = stock_rets @ weights
    n = len(weights)
    corr = np.corrcoef(stock_rets, rowvar=False) if n > 1 else np.ones((1, 1))
    corr = np.nan_to_num(corr, nan=0.0)
    mvar = np.var(market_rets)
    beta = float(np.cov(port, market_rets)[0, 1] / mvar) if mvar > 0 else 1.0
    return {
        "volatility": float(np.std(port) * np.sqrt(TRADING_DAYS)),
        "avg_return": float(np.mean(port) * TRADING_DAYS),
        "max_drawdown": max_drawdown(port),
        "sector_concentration": max(sector_weights(weights, sectors).values()),
        "n_stocks": float(n),
        "avg_correlation": weighted_avg_correlation(corr, weights),
        "trading_volume": float(np.log10(max(np.dot(weights, traded_value), 1e-6))),
        "market_volatility": float(np.std(market_rets) * np.sqrt(TRADING_DAYS)),
        "beta": beta,
        "var_95": float(-np.percentile(port, 5)),
    }


def diversification_score(weights: np.ndarray, sectors: list, avg_corr: float) -> float:
    """0-100. Rewards spreading money across stocks AND sectors AND low co-movement."""
    stock_hhi = float(np.sum(weights ** 2))
    sector_hhi = float(sum(v ** 2 for v in sector_weights(weights, sectors).values()))
    spread = (1 - stock_hhi) * 0.35 + (1 - sector_hhi) * 0.35 + (1 - max(avg_corr, 0.0)) * 0.30
    return round(100 * max(0.0, min(1.0, spread)), 1)


def risk_level(score: float) -> str:
    if score >= 60:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def score_from_proba(proba: np.ndarray) -> float:
    """Ensemble class probabilities (low, medium, high) -> 0..100 risk score."""
    return float(np.clip(100 * (0.5 * proba[1] + 1.0 * proba[2]), 0, 100))
