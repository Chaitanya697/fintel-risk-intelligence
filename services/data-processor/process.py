"""Fintel data processor + ensemble model trainer.

Runs as a one-off job (Kubernetes Job in the cloud, `docker compose run` locally):

  1. Reads the raw Kaggle NIFTY50 CSVs from S3  (raw/)
  2. Cleans them and builds a daily-returns matrix + market proxy   -> processed/
  3. Builds thousands of historical sample portfolios, computes the 10 risk
     features for each, and labels them with what ACTUALLY happened over the
     next 6 months (risk relative to the market -> LOW / MEDIUM / HIGH)
  4. Trains Random Forest + Gradient Boosting + XGBoost, averages them (ensemble)
  5. Saves the model and a model card to S3                         -> models/
"""
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score
from scipy.stats import spearmanr
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root when run from source
from fintel_common.finance import (FEATURES, HORIZON, LOOKBACK, SECTOR_NAMES, clean_stock_frame,  # noqa: E402
                                   max_drawdown, portfolio_features)
from fintel_common.storage import Storage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("data-processor")

N_SAMPLES = 9000
TRAIN_END = pd.Timestamp("2016-12-31")   # train on 2001-2016 anchors
TEST_START = pd.Timestamp("2017-07-01")  # test on 2017-2020 anchors (gap > horizon: no overlap leakage)
SEED = 42


def load_raw(storage: Storage):
    keys = [k for k in storage.list("raw/") if k.lower().endswith(".csv")]
    if not keys:
        raise SystemExit(f"No CSV files found under raw/ in {storage.location}. Upload the Kaggle dataset first.")
    meta_key = next((k for k in keys if k.lower().endswith("stock_metadata.csv")), None)
    if not meta_key:
        raise SystemExit("raw/stock_metadata.csv is missing.")
    meta = pd.read_csv(storage.open_read(meta_key))
    meta = meta.set_index("Symbol")

    rets, values = {}, {}
    for key in keys:
        symbol = Path(key).stem
        if symbol in ("stock_metadata", "NIFTY50_all"):
            continue
        df = pd.read_csv(storage.open_read(key))
        if df.empty:
            log.warning("Skipping %s: file has no rows", key)
            continue
        clean = clean_stock_frame(df)
        rets[symbol] = clean["ret"]
        values[symbol] = clean["value"]
    returns = pd.DataFrame(rets).sort_index()
    traded = pd.DataFrame(values).reindex(returns.index)
    stocks = []
    for s in returns.columns:
        industry = meta.loc[s, "Industry"] if s in meta.index else "OTHER"
        stocks.append({
            "symbol": s,
            "company": meta.loc[s, "Company Name"] if s in meta.index else s,
            "industry": industry,
            "sector": SECTOR_NAMES.get(industry, str(industry).title()),
            "first_date": str(returns[s].first_valid_index().date()),
        })
    log.info("Loaded %d stocks, %d trading days (%s to %s)", returns.shape[1], returns.shape[0],
             returns.index.min().date(), returns.index.max().date())
    return returns, traded, stocks


def save_processed(storage: Storage, returns, traded, stocks, market):
    def put_csv(key, frame):
        buf = io.BytesIO()
        frame.to_csv(buf, compression={"method": "gzip"} if key.endswith(".gz") else None,
                     float_format="%.6g", index_label="Date")
        storage.write_bytes(key, buf.getvalue())

    put_csv("processed/returns.csv.gz", returns)
    put_csv("processed/market.csv.gz", market.to_frame("market"))
    # yearly volatility table per stock (useful for reports / the S3 demo)
    yearly_vol = returns.groupby(returns.index.year).std() * np.sqrt(252)
    yearly_vol.index.name = "Year"
    buf = io.BytesIO()
    yearly_vol.round(4).to_csv(buf)
    storage.write_bytes("processed/volatility.csv", buf.getvalue())

    last = traded.iloc[-LOOKBACK:].mean()
    median = float(last.median())
    for s in stocks:
        v = last.get(s["symbol"])
        s["avg_traded_value"] = float(v) if pd.notna(v) else 0.0
        s["relative_liquidity"] = round(float(v) / median, 4) if pd.notna(v) and median > 0 else 0.0
    storage.write_bytes("processed/stocks.json", json.dumps(stocks, indent=2).encode())
    log.info("Wrote processed/returns.csv.gz, market.csv.gz, volatility.csv, stocks.json")


def build_samples(returns, traded, market, sectors_by_symbol, rng):
    R = returns.values
    TV = traded.values
    M = market.values
    dates = returns.index
    symbols = np.array(returns.columns)
    sym_sector = np.array([sectors_by_symbol[s] for s in symbols])
    T = len(dates)
    rows, labels, anchors = [], [], []
    tries = 0
    while len(rows) < N_SAMPLES and tries < N_SAMPLES * 5:
        tries += 1
        t = int(rng.integers(LOOKBACK, T - HORIZON))
        window = R[t - LOOKBACK:t + HORIZON]
        eligible = np.where(~np.isnan(window).any(axis=0))[0]
        if len(eligible) < 12:
            continue
        k = int(rng.integers(2, 11))
        if rng.random() < 0.35:  # deliberately sector-concentrated portfolio
            sec_choices = [sec for sec in set(sym_sector[eligible]) if (sym_sector[eligible] == sec).sum() >= 2]
            sec = rng.choice(sec_choices)
            pool_main = eligible[sym_sector[eligible] == sec]
            n_main = min(len(pool_main), max(2, int(round(k * rng.uniform(0.5, 1.0)))))
            picks = list(rng.choice(pool_main, n_main, replace=False))
            rest = [i for i in eligible if i not in picks]
            if k > n_main:
                picks += list(rng.choice(rest, k - n_main, replace=False))
        else:
            picks = list(rng.choice(eligible, k, replace=False))
        picks = np.array(picks)
        w = rng.dirichlet(np.ones(len(picks)) * rng.uniform(0.5, 3.0))
        past = R[t - LOOKBACK:t][:, picks]
        tv_all = np.nanmean(TV[t - LOOKBACK:t], axis=0)
        tv = tv_all[picks] / np.nanmedian(tv_all[eligible])  # liquidity relative to the market
        feats = portfolio_features(past, w, list(sym_sector[picks]), M[t - LOOKBACK:t], np.nan_to_num(tv))
        future = R[t:t + HORIZON][:, picks] @ w
        fut_mkt = M[t:t + HORIZON]
        rows.append([feats[f] for f in FEATURES])
        labels.append({
            "future_drawdown": max_drawdown(future),
            "market_future_drawdown": max_drawdown(fut_mkt),
            "future_volatility": float(np.std(future) * np.sqrt(252)),
            "market_future_volatility": float(np.std(fut_mkt) * np.sqrt(252)),
        })
        anchors.append(dates[t])
    X = pd.DataFrame(rows, columns=FEATURES)
    return X, pd.DataFrame(labels), pd.DatetimeIndex(anchors)


def relative_risk(L: pd.DataFrame) -> np.ndarray:
    """Training label: how risky the portfolio turned out to be COMPARED WITH THE MARKET over the next 6 months.

    1.0 = as risky as the market. Average of (future volatility / market volatility) and
    (future max drawdown / market max drawdown). Comparing with the market removes crashes that hit
    everyone (2008, 2020), which no portfolio feature could predict, and keeps what the portfolio's
    own make-up (concentration, correlation, beta...) explains.
    """
    vol_ratio = L["future_volatility"] / L["market_future_volatility"].clip(lower=0.02)
    dd_ratio = (L["future_drawdown"] / L["market_future_drawdown"].clip(lower=0.02)).clip(upper=3.0)
    return (0.5 * vol_ratio + 0.5 * dd_ratio).values


def train(X, L, anchors):
    rel_risk = relative_risk(L)
    train_mask = anchors <= TRAIN_END
    test_mask = anchors >= TEST_START
    cut_lo, cut_hi = np.quantile(rel_risk[train_mask], [1 / 3, 2 / 3])
    y = np.digitize(rel_risk, [cut_lo, cut_hi])  # 0 low, 1 medium, 2 high
    Xtr, ytr, Xte, yte = X[train_mask], y[train_mask], X[test_mask], y[test_mask]
    log.info("Samples: %d train (<= %s), %d test (>= %s). Label cut-offs (relative risk): %.2f / %.2f",
             len(Xtr), TRAIN_END.date(), len(Xte), TEST_START.date(), cut_lo, cut_hi)

    models = {
        "random_forest": RandomForestClassifier(n_estimators=300, max_depth=10, min_samples_leaf=5,
                                                n_jobs=-1, random_state=SEED),
        "gradient_boosting": GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                                        random_state=SEED),
        "xgboost": XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8,
                                 colsample_bytree=0.8, random_state=SEED, n_jobs=2, eval_metric="mlogloss"),
    }
    probas, report = [], {}
    for name, model in models.items():
        t0 = time.time()
        model.fit(Xtr, ytr)
        p = model.predict_proba(Xte)
        probas.append(p)
        pred = p.argmax(axis=1)
        report[name] = {"accuracy": round(accuracy_score(yte, pred), 3),
                        "macro_f1": round(f1_score(yte, pred, average="macro"), 3),
                        "train_seconds": round(time.time() - t0, 1)}
        log.info("%-18s accuracy %.3f  macro-F1 %.3f", name, report[name]["accuracy"], report[name]["macro_f1"])
    ens = np.mean(probas, axis=0)
    pred = ens.argmax(axis=1)
    score = 100 * (0.5 * ens[:, 1] + ens[:, 2])
    rho = spearmanr(score, rel_risk[test_mask]).statistic
    report["ensemble"] = {"accuracy": round(accuracy_score(yte, pred), 3),
                          "macro_f1": round(f1_score(yte, pred, average="macro"), 3),
                          "spearman_score_vs_actual_risk": round(float(rho), 3)}
    report["baseline_majority_class"] = {"accuracy": round(float(np.max(np.bincount(yte, minlength=3)) / len(yte)), 3)}
    log.info("%-18s accuracy %.3f  macro-F1 %.3f  spearman(score, actual relative risk) %.3f", "ENSEMBLE",
             report["ensemble"]["accuracy"], report["ensemble"]["macro_f1"], rho)

    imp_rf = models["random_forest"].feature_importances_
    imp_xgb = models["xgboost"].feature_importances_
    imp = (imp_rf / imp_rf.sum() + imp_xgb / imp_xgb.sum()) / 2
    importance = {f: round(float(v), 4) for f, v in sorted(zip(FEATURES, imp), key=lambda kv: -kv[1])}

    # refit on all samples up to the last anchor so the live model uses the most recent history
    for model in models.values():
        model.fit(X, y)
    return models, report, importance, (float(cut_lo), float(cut_hi)), int(train_mask.sum()), int(test_mask.sum())


def main():
    t0 = time.time()
    storage = Storage()
    log.info("Storage backend: %s", storage.location)
    rng = np.random.default_rng(SEED)

    returns, traded, stocks = load_raw(storage)
    counts = returns.notna().sum(axis=1)
    market = returns.mean(axis=1, skipna=True).where(counts >= 10).dropna()
    returns, traded = returns.loc[market.index], traded.loc[market.index]
    save_processed(storage, returns, traded, stocks, market)

    sectors = {s["symbol"]: s["sector"] for s in stocks}
    X, L, anchors = build_samples(returns, traded, market, sectors, rng)
    log.info("Built %d historical sample portfolios", len(X))
    buf = io.BytesIO()
    pd.concat([X, L], axis=1).assign(anchor_date=anchors, relative_risk=relative_risk(L)).to_csv(
        buf, index=False, float_format="%.6g")
    storage.write_bytes("processed/training_samples.csv", buf.getvalue())

    models, report, importance, cutoffs, n_train, n_test = train(X, L, anchors)

    bundle = {"models": models, "features": FEATURES, "cutoffs": cutoffs}
    buf = io.BytesIO()
    joblib.dump(bundle, buf, compress=3)
    storage.write_bytes("models/risk_model.joblib", buf.getvalue())

    card = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "Kaggle: rohanrao/nifty50-stock-market-data",
        "data_from": str(returns.index.min().date()),
        "data_to": str(returns.index.max().date()),
        "n_stocks": len(stocks),
        "n_samples": len(X), "n_train": n_train, "n_test": n_test,
        "label": ("Relative risk over the next 6 months = average of (portfolio volatility / market volatility) "
                  "and (portfolio max drawdown / market max drawdown); split into thirds LOW / MEDIUM / HIGH"),
        "label_cutoffs": [round(c, 3) for c in cutoffs],
        "test_period": f"{TEST_START.date()} onwards (never seen in training)",
        "metrics": report,
        "feature_importance": importance,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    storage.write_bytes("models/model_card.json", json.dumps(card, indent=2).encode())
    log.info("Saved models/risk_model.joblib and models/model_card.json in %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()
