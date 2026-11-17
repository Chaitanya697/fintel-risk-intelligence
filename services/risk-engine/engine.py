"""Fintel risk engine: the analytics + ML microservice.

POST /analyze   {"holdings":[{"symbol":"TCS","amount":40000}, ...]}  -> full risk report
POST /stress    {"holdings":[...], "market_pct":-20, "sector":"IT", "sector_pct":-25} -> custom stress test
GET  /stocks    the 49-stock universe with sectors
GET  /model     model card (accuracy, feature importance, training period)
GET  /health    liveness/readiness;   GET /metrics  Prometheus metrics
"""
import io
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fintel_common.finance import (FEATURE_LABELS, FEATURES, LOOKBACK, diversification_score,  # noqa: E402
                                   max_drawdown, portfolio_features, risk_level, score_from_proba,
                                   sector_weights, weighted_avg_correlation)
from fintel_common.storage import Storage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("risk-engine")
app = Flask(__name__)

REQS = Counter("fintel_engine_requests_total", "Risk engine requests", ["endpoint", "status"])
LAT = Histogram("fintel_engine_request_seconds", "Risk engine latency", ["endpoint"],
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
PREDICTIONS = Counter("fintel_ml_predictions_total", "Ensemble risk predictions", ["level"])
MODEL_LOADED = Gauge("fintel_model_loaded", "1 when the ML model and data are loaded")
MODEL_ACC = Gauge("fintel_model_test_accuracy", "Ensemble accuracy on the held-out test period")

EXPERTS = {"random_forest": "Random Forest", "gradient_boosting": "Gradient Boosting", "xgboost": "XGBoost"}

PRESETS = [
    {"id": "market_crash", "name": "Market Crash", "detail": "The whole market falls 20%. Each stock falls by 20% x its beta.",
     "market_pct": -20},
    {"id": "it_crash", "name": "IT Sector Crash", "detail": "IT stocks fall 25%. Other sectors unchanged.",
     "sectors": {"IT": -25}},
    {"id": "banking_crash", "name": "Banking Sector Crash", "detail": "Banking & Finance stocks fall 25%.",
     "sectors": {"Banking & Finance": -25}},
    {"id": "oil_shock", "name": "Oil Price Shock",
     "detail": "Crude spikes. Assumed impact: Energy -12%, Automobile -10%, FMCG & Consumer -6%, Cement -6%, everything else -3%.",
     "sectors": {"Energy": -12, "Automobile": -10, "FMCG & Consumer": -6, "Cement": -6, "*": -3}},
    {"id": "high_volatility", "name": "High Volatility",
     "detail": "Volatility doubles for a week. Loss on a bad (1-in-20) week at twice today's volatility."},
]

HISTORY = [
    {"id": "gfc_2008", "name": "Global Financial Crisis", "start": "2008-01-08", "end": "2008-10-27"},
    {"id": "euro_2011", "name": "Eurozone Debt Crisis", "start": "2011-01-03", "end": "2011-12-20"},
    {"id": "selloff_2015", "name": "2015-16 Global Sell-off", "start": "2015-03-03", "end": "2016-02-29"},
    {"id": "covid_2020", "name": "COVID-19 Crash", "start": "2020-01-17", "end": "2020-03-23"},
]


class State:
    ready = False
    error = "loading"
    returns = market = stocks = bundle = card = train_X = None
    lock = threading.Lock()


S = State()


def load_everything():
    storage = Storage()
    while not S.ready:
        try:
            returns = pd.read_csv(storage.open_read("processed/returns.csv.gz"), index_col=0, parse_dates=True,
                                  compression="gzip")
            market = pd.read_csv(storage.open_read("processed/market.csv.gz"), index_col=0, parse_dates=True,
                                 compression="gzip")["market"]
            stocks = {s["symbol"]: s for s in json.loads(storage.read_bytes("processed/stocks.json"))}
            bundle = joblib.load(io.BytesIO(storage.read_bytes("models/risk_model.joblib")))
            card = json.loads(storage.read_bytes("models/model_card.json"))
            train_X = pd.read_csv(storage.open_read("processed/training_samples.csv"), usecols=FEATURES)
            with S.lock:
                S.returns, S.market, S.stocks, S.bundle, S.card, S.train_X = returns, market, stocks, bundle, card, train_X
                S.ready, S.error = True, None
            MODEL_LOADED.set(1)
            MODEL_ACC.set(card["metrics"]["ensemble"]["accuracy"])
            log.info("Loaded data (%d stocks, up to %s) and model trained %s from %s", len(stocks),
                     returns.index.max().date(), card["trained_at"], storage.location)
        except Exception as exc:  # data-processor may not have run yet: keep retrying
            S.error = f"model/data not available yet: {exc.__class__.__name__}: {exc}"
            log.warning("%s - retrying in 20s", S.error)
            time.sleep(20)


def parse_holdings(payload):
    holdings = payload.get("holdings") if isinstance(payload, dict) else None
    if not holdings or not isinstance(holdings, list):
        raise ValueError("Provide holdings: [{symbol, amount}, ...]")
    merged = {}
    for h in holdings:
        sym = str(h.get("symbol", "")).upper().strip()
        try:
            amt = float(h.get("amount", 0))
        except (TypeError, ValueError):
            raise ValueError(f"Amount for {sym or 'a holding'} is not a number")
        if sym not in S.stocks:
            raise ValueError(f"Unknown stock symbol: {sym}")
        if amt <= 0:
            raise ValueError(f"Amount for {sym} must be greater than zero")
        merged[sym] = merged.get(sym, 0.0) + amt
    if len(merged) > 30:
        raise ValueError("A portfolio can hold at most 30 stocks")
    syms = list(merged)
    amounts = np.array([merged[s] for s in syms])
    return syms, amounts


def stock_betas(window: pd.DataFrame, mkt: pd.Series) -> np.ndarray:
    mv = np.var(mkt.values)
    return np.array([np.cov(window[c].values, mkt.values)[0, 1] / mv for c in window.columns])


def apply_shock(syms, amounts, betas, market_pct=0.0, sectors=None):
    sectors = sectors or {}
    rows = []
    for s, a, b in zip(syms, amounts, betas):
        sec = S.stocks[s]["sector"]
        pct = 0.0
        if market_pct:
            pct += market_pct * b
        if sec in sectors:
            pct += sectors[sec]
        elif "*" in sectors:
            pct += sectors["*"]
        pct = max(pct, -100.0)
        rows.append({"symbol": s, "sector": sec, "shock_pct": round(pct, 2), "loss": round(-a * pct / 100, 2)})
    loss = sum(r["loss"] for r in rows)
    value = float(amounts.sum())
    return {"loss": round(loss, 2), "new_value": round(value - loss, 2),
            "impact_pct": round(-100 * loss / value, 2), "by_stock": rows}


def historical_replay(syms, amounts):
    out = []
    w = amounts / amounts.sum()
    for ev in HISTORY:
        period = S.returns.loc[ev["start"]:ev["end"]]
        mkt = S.market.loc[ev["start"]:ev["end"]]
        if period.empty:
            continue
        per_stock, proxied = [], []
        for s in syms:
            r = period[s]
            if r.isna().mean() > 0.2:  # stock not listed yet -> use market as a stand-in
                r = mkt
                proxied.append(s)
            per_stock.append(float(np.prod(1 + r.fillna(0).values) - 1))
        port = float(np.dot(w, per_stock))
        mret = float(np.prod(1 + mkt.values) - 1)
        out.append({**ev, "portfolio_pct": round(100 * port, 2), "market_pct": round(100 * mret, 2),
                    "loss": round(-port * float(amounts.sum()), 2), "proxied": proxied})
    return out


def findings(m, sec_exp, pairs, syms, weights, var_amt, value):
    f = []
    top_sec, top_w = next(iter(sec_exp.items()))
    if top_w >= 0.40:
        f.append(("high", f"{top_w:.0%} of the portfolio is exposed to {top_sec}",
                  f"One sector holds {top_w:.0%} of your money. A downturn in {top_sec} alone would hit most of the portfolio."))
    elif top_w >= 0.30:
        f.append(("medium", f"{top_sec} exposure is elevated ({top_w:.0%})",
                  f"Consider whether {top_w:.0%} in one sector is intended."))
    hot = [p for p in pairs if p["corr"] >= 0.6]
    for p in hot[:2]:
        sev = "high" if p["corr"] >= 0.75 else "medium"
        f.append((sev, f"{p['a']} and {p['b']} are highly correlated ({p['corr']:.2f})",
                  "They tend to rise and fall together, so owning both adds little diversification."))
    ratio = m["volatility"] / max(m["market_volatility"], 1e-6)
    if ratio >= 1.35:
        f.append(("high", f"Portfolio volatility is high ({m['volatility']:.0%} a year)",
                  f"That is {ratio:.1f}x the market's volatility of {m['market_volatility']:.0%}."))
    elif ratio >= 1.1:
        f.append(("medium", f"Volatility is above the market ({m['volatility']:.0%} vs {m['market_volatility']:.0%})",
                  "Prices swing more than the overall NIFTY50 market."))
    if m["max_drawdown"] >= 0.25:
        f.append(("high", f"Maximum fall in the last year was {m['max_drawdown']:.0%}",
                  "The portfolio lost a quarter or more from a peak within the past 12 months."))
    elif m["max_drawdown"] >= 0.15:
        f.append(("medium", f"Maximum fall in the last year was {m['max_drawdown']:.0%}",
                  "A noticeable peak-to-trough decline in the past 12 months."))
    if m["beta"] >= 1.15:
        f.append(("medium", f"Beta of {m['beta']:.2f}: amplifies market moves",
                  f"If the market falls 10%, this portfolio would typically fall about {10 * m['beta']:.0f}%."))
    n = len(syms)
    if n <= 3:
        f.append(("high", f"Only {n} stock{'s' if n > 1 else ''}", "Too few holdings: company-specific bad news can do serious damage."))
    elif n <= 5:
        f.append(("medium", f"Only {n} stocks", "Adding holdings from other sectors would spread the risk."))
    big = int(np.argmax(weights))
    if weights[big] >= 0.35 and n > 1:
        f.append(("medium", f"{syms[big]} is {weights[big]:.0%} of the portfolio", "A single stock dominates the outcome."))
    f.append(("info", f"Value at Risk: about Rs {var_amt:,.0f} on a bad day",
              f"On 1 trading day in 20, the portfolio has historically lost {var_amt / value:.1%} or more."))
    if not any(sev in ("high", "medium") for sev, _, _ in f):
        f.insert(0, ("ok", "No major red flags", "Exposure is spread across sectors and holdings move fairly independently."))
    order = {"high": 0, "medium": 1, "ok": 2, "info": 3}
    f.sort(key=lambda x: order[x[0]])
    return [{"severity": s, "title": t, "detail": d} for s, t, d in f]


def summary_sentence(level, items):
    reasons = [i["title"] for i in items if i["severity"] == "high"][:2] or \
              [i["title"] for i in items if i["severity"] == "medium"][:2]
    if not reasons:
        return f"Your portfolio has {level} risk. No single factor dominates."
    joined = " and ".join(r[0].lower() + r[1:] for r in reasons)
    if level == "LOW":
        return f"Your portfolio has LOW risk overall, but note that {joined}."
    return f"Your portfolio has {level} risk primarily because {joined}."


def model_evidence(feats):
    """Which features push this portfolio's risk up, relative to the 9,000 training portfolios."""
    imp = S.card["feature_importance"]
    out = []
    for f in FEATURES:
        col = S.train_X[f].values
        pct = float((col < feats[f]).mean())
        out.append({"feature": f, "label": FEATURE_LABELS[f], "value": feats[f], "percentile": round(100 * pct),
                    "importance": imp.get(f, 0)})
    out.sort(key=lambda e: -e["importance"] * abs(e["percentile"] - 50))
    return out


def analyze(syms, amounts):
    value = float(amounts.sum())
    weights = amounts / value
    window = S.returns[syms].iloc[-LOOKBACK:].fillna(0.0)
    mkt = S.market.iloc[-LOOKBACK:]
    sectors = [S.stocks[s]["sector"] for s in syms]
    traded = np.array([S.stocks[s].get("relative_liquidity", 0.0) for s in syms])
    feats = portfolio_features(window.values, weights, sectors, mkt.values, traded)

    X = pd.DataFrame([[feats[f] for f in FEATURES]], columns=FEATURES)
    experts, probas = [], []
    for key, model in S.bundle["models"].items():
        p = model.predict_proba(X)[0]
        probas.append(p)
        sc = score_from_proba(p)
        experts.append({"name": EXPERTS.get(key, key), "proba": [round(float(v), 3) for v in p],
                        "score": round(sc, 1), "level": risk_level(sc)})
    ens = np.mean(probas, axis=0)
    score = score_from_proba(ens)
    level = risk_level(score)
    PREDICTIONS.labels(level).inc()

    corr = np.nan_to_num(np.corrcoef(window.values, rowvar=False)) if len(syms) > 1 else np.ones((1, 1))
    pairs = sorted(({"a": syms[i], "b": syms[j], "corr": round(float(corr[i, j]), 3)}
                    for i in range(len(syms)) for j in range(i + 1, len(syms))), key=lambda p: -p["corr"])
    port = window.values @ weights
    var95 = float(-np.percentile(port, 5))
    cvar95 = float(-port[port <= -var95].mean()) if (port <= -var95).any() else var95
    betas = stock_betas(window, mkt)
    sec_exp = sector_weights(weights, sectors)
    avg_corr = weighted_avg_correlation(corr, weights)
    metrics = {**feats, "var_95_amount": round(var95 * value, 2), "cvar_95_amount": round(cvar95 * value, 2),
               "diversification": diversification_score(weights, sectors, avg_corr)}
    items = findings(feats, sec_exp, pairs, syms, weights, var95 * value, value)

    # 5-year growth path ending at today's value, vs the market proxy (weekly points)
    hist = S.returns[syms].iloc[-5 * 252:].fillna(0.0).values @ weights
    mhist = S.market.iloc[-5 * 252:].values
    dates = S.returns.index[-5 * 252:]
    path = np.cumprod(1 + hist)
    mpath = np.cumprod(1 + mhist)
    path, mpath = value * path / path[-1], value * mpath / mpath[-1]
    step = 5
    growth = {"dates": [str(d.date()) for d in dates[::step]], "portfolio": [round(float(v)) for v in path[::step]],
              "market": [round(float(v)) for v in mpath[::step]]}

    holdings = []
    for i, s in enumerate(syms):
        r = window[s].values
        holdings.append({"symbol": s, "company": S.stocks[s]["company"], "sector": sectors[i],
                         "amount": round(float(amounts[i]), 2), "weight": round(float(weights[i]), 4),
                         "volatility": round(float(np.std(r) * np.sqrt(252)), 4),
                         "return_1y": round(float(np.prod(1 + r) - 1), 4), "beta": round(float(betas[i]), 3)})

    stress = []
    for p in PRESETS:
        if p["id"] == "high_volatility":
            loss = 1.645 * 2 * float(np.std(port)) * np.sqrt(5) * value
            stress.append({**p, "loss": round(loss, 2), "new_value": round(value - loss, 2),
                           "impact_pct": round(-100 * loss / value, 2)})
        else:
            res = apply_shock(syms, amounts, betas, p.get("market_pct", 0), p.get("sectors"))
            stress.append({**{k: v for k, v in p.items() if k not in ("sectors",)}, **res})

    return {
        "as_of": str(S.returns.index[-1].date()),
        "value": round(value, 2),
        "verdict": {"score": round(score, 1), "level": level, "proba": [round(float(v), 3) for v in ens],
                    "experts": experts, "summary": summary_sentence(level, items)},
        "metrics": metrics,
        "holdings": holdings,
        "sector_exposure": [{"sector": k, "weight": round(v, 4), "amount": round(v * value, 2)} for k, v in sec_exp.items()],
        "correlation": {"symbols": syms, "matrix": [[round(float(x), 3) for x in row] for row in corr],
                        "top_pairs": pairs[:6]},
        "findings": items,
        "evidence": model_evidence(feats),
        "growth": growth,
        "drawdown_5y": round(max_drawdown(hist), 4),
        "stress": stress,
        "history": historical_replay(syms, amounts),
        "model": {k: S.card[k] for k in ("trained_at", "data_from", "data_to", "n_samples")} |
                 {"accuracy": S.card["metrics"]["ensemble"]["accuracy"],
                  "baseline": S.card["metrics"]["baseline_majority_class"]["accuracy"]},
    }


def timed(endpoint):
    def deco(fn):
        def wrapper(*a, **kw):
            t0 = time.time()
            status = "200"
            try:
                resp = fn(*a, **kw)
                status = str(resp[1]) if isinstance(resp, tuple) else "200"
                return resp
            except Exception:
                status = "500"
                log.exception("Unhandled error in %s", endpoint)
                return jsonify(error="Internal error"), 500
            finally:
                REQS.labels(endpoint, status).inc()
                LAT.labels(endpoint).observe(time.time() - t0)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def not_ready():
    return jsonify(error="Risk engine is still loading data/model", detail=S.error), 503


@app.post("/analyze")
@timed("analyze")
def analyze_route():
    if not S.ready:
        return not_ready()
    try:
        syms, amounts = parse_holdings(request.get_json(silent=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(analyze(syms, amounts))


@app.post("/stress")
@timed("stress")
def stress_route():
    if not S.ready:
        return not_ready()
    payload = request.get_json(silent=True) or {}
    try:
        syms, amounts = parse_holdings(payload)
        market_pct = float(payload.get("market_pct") or 0)
        sector_pct = float(payload.get("sector_pct") or 0)
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    if not -100 <= market_pct <= 100 or not -100 <= sector_pct <= 100:
        return jsonify(error="Shocks must be between -100% and +100%"), 400
    sector = payload.get("sector") or ""
    window = S.returns[syms].iloc[-LOOKBACK:].fillna(0.0)
    betas = stock_betas(window, S.market.iloc[-LOOKBACK:])
    res = apply_shock(syms, amounts, betas, market_pct, {sector: sector_pct} if sector and sector_pct else None)
    return jsonify(res)


@app.get("/stocks")
@timed("stocks")
def stocks_route():
    if not S.ready:
        return not_ready()
    return jsonify(sorted(S.stocks.values(), key=lambda s: s["symbol"]))


@app.get("/model")
@timed("model")
def model_route():
    if not S.ready:
        return not_ready()
    return jsonify(S.card | {"storage": Storage().location})


@app.get("/health")
def health():
    return jsonify(status="ok", model_loaded=S.ready, detail=S.error), 200


@app.get("/ready")
def ready():
    """Kubernetes readiness: only send traffic once the model is loaded."""
    return (jsonify(ready=True), 200) if S.ready else (jsonify(ready=False, detail=S.error), 503)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


threading.Thread(target=load_everything, daemon=True).start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8001")))
