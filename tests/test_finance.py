"""Unit tests for the shared risk maths. Run: python -m pytest -q tests"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fintel_common.finance import (FEATURES, clean_stock_frame, diversification_score, max_drawdown,  # noqa: E402
                                   portfolio_features, risk_level, score_from_proba, sector_weights)


def test_max_drawdown_simple_path():
    # 100 -> 110 -> 88 -> 99 : worst fall is 110 -> 88 = 20%
    rets = np.array([0.10, -0.20, 0.125])
    assert abs(max_drawdown(rets) - 0.20) < 1e-9


def test_max_drawdown_only_rising_is_zero():
    assert max_drawdown(np.array([0.01, 0.02, 0.03])) == 0.0


def test_split_days_are_neutralised():
    df = pd.DataFrame({"Date": ["2020-01-01", "2020-01-02", "2020-01-03"],
                       "Close": [1000.0, 200.0, 210.0],   # 1:5 split on day 2
                       "Volume": [10, 50, 50], "VWAP": [1000.0, 200.0, 205.0]})
    out = clean_stock_frame(df)
    assert out["ret"].iloc[0] == 0.0
    assert abs(out["ret"].iloc[1] - 0.05) < 1e-9


def test_sector_weights_sum_to_one():
    w = sector_weights(np.array([0.4, 0.3, 0.3]), ["IT", "IT", "Energy"])
    assert abs(sum(w.values()) - 1) < 1e-9 and abs(w["IT"] - 0.7) < 1e-9


def test_features_complete_and_sane():
    rng = np.random.default_rng(0)
    mkt = rng.normal(0, 0.01, 252)
    stocks = np.column_stack([mkt + rng.normal(0, 0.01, 252) for _ in range(4)])
    f = portfolio_features(stocks, np.full(4, 0.25), ["IT", "IT", "Energy", "Pharma"], mkt, np.ones(4))
    assert set(f) == set(FEATURES)
    assert 0 < f["volatility"] < 1 and 0.5 < f["beta"] < 1.5 and f["sector_concentration"] == 0.5


def test_concentrated_portfolio_is_less_diversified():
    spread = diversification_score(np.full(8, 1 / 8), ["A", "B", "C", "D", "E", "F", "G", "H"], 0.2)
    narrow = diversification_score(np.array([0.5, 0.5]), ["IT", "IT"], 0.8)
    assert spread > narrow


def test_score_and_level():
    assert score_from_proba(np.array([1, 0, 0])) == 0
    assert score_from_proba(np.array([0, 0, 1])) == 100
    assert risk_level(72) == "HIGH" and risk_level(40) == "MEDIUM" and risk_level(10) == "LOW"
