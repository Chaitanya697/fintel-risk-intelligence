"""Fintel web: dashboard, user accounts and saved portfolios.

Talks to the risk-engine microservice over HTTP; stores users + portfolios in SQLite (on a
Kubernetes persistent volume in the cloud).
"""
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)
DB_PATH = os.getenv("DB_PATH", "data/fintel.db")
ENGINE_URL = os.getenv("ENGINE_URL", "http://127.0.0.1:8001").rstrip("/")
GRAFANA_URL = os.getenv("GRAFANA_URL", "")

REQS = Counter("fintel_web_requests_total", "Web requests", ["endpoint", "method", "status"])
LAT = Histogram("fintel_web_request_seconds", "Web response time", ["endpoint"],
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
ENGINE_CALLS = Counter("fintel_engine_calls_total", "Calls from web to the risk engine", ["path", "outcome"])

SPECIMEN = {"name": "Sample Portfolio: Reliance, TCS, HDFC Bank, Infosys, ITC",
            "holdings": [{"symbol": "RELIANCE", "amount": 50000}, {"symbol": "TCS", "amount": 40000},
                         {"symbol": "HDFCBANK", "amount": 30000}, {"symbol": "INFY", "amount": 30000},
                         {"symbol": "ITC", "amount": 20000}]}


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            holdings TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        """)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- engine client
_stock_cache = {"t": 0, "data": None}


def engine(method, path, payload=None, timeout=20):
    try:
        resp = requests.request(method, f"{ENGINE_URL}{path}", json=payload, timeout=timeout)
        ENGINE_CALLS.labels(path.split("?")[0], "ok" if resp.ok else f"http_{resp.status_code}").inc()
        try:
            body = resp.json()
        except ValueError:
            body = {"error": "Risk engine returned an unreadable response"}
        return resp.status_code, body
    except requests.RequestException as exc:
        ENGINE_CALLS.labels(path, "unreachable").inc()
        log.warning("Risk engine unreachable: %s", exc)
        return 503, {"error": "The risk engine is not reachable right now."}


def stock_universe():
    if _stock_cache["data"] and time.time() - _stock_cache["t"] < 600:
        return _stock_cache["data"]
    code, body = engine("GET", "/stocks")
    if code == 200:
        _stock_cache.update(t=time.time(), data=body)
        return body
    return _stock_cache["data"] or []


# ---------------------------------------------------------------- security helpers
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def start_timer_and_csrf():
    g.t0 = time.time()
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your form expired. Please go back, refresh the page and try again.")


@app.after_request
def record_metrics(resp):
    endpoint = request.endpoint or "unknown"
    if endpoint not in ("metrics", "static"):
        REQS.labels(endpoint, request.method, str(resp.status_code)).inc()
        LAT.labels(endpoint).observe(time.time() - g.get("t0", time.time()))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return get_db().execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()


app.jinja_env.globals["current_user"] = current_user


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def own_case(case_id):
    row = get_db().execute("SELECT * FROM portfolios WHERE id = ? AND user_id = ?",
                           (case_id, session["uid"])).fetchone()
    if row is None:
        abort(404)
    return row


# ---------------------------------------------------------------- template helpers
@app.template_filter("inr")
def inr(value, decimals=0):
    """Indian digit grouping: 200000 -> 2,00,000."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    neg = v < 0
    v = abs(v)
    whole = int(round(v)) if decimals == 0 else int(v)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = f"{head},{tail}"
    if decimals:
        s += f"{v - int(v):.{decimals}f}"[1:]
    return ("-" if neg else "") + "₹" + s


@app.template_filter("pct")
def pct(value, decimals=1):
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return value


@app.template_filter("datefmt")
def datefmt(value):
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------- auth
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not USERNAME_RE.match(username):
            flash("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != request.form.get("confirm", ""):
            flash("Passwords do not match.", "error")
        else:
            db = get_db()
            try:
                cur = db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                                 (username, generate_password_hash(password), now()))
                db.execute("INSERT INTO portfolios (user_id, name, holdings, created_at, updated_at) VALUES (?,?,?,?,?)",
                           (cur.lastrowid, SPECIMEN["name"], json.dumps(SPECIMEN["holdings"]), now(), now()))
                db.commit()
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
                return render_template("auth.html", mode="signup"), 400
            session.clear()
            session["uid"] = cur.lastrowid
            flash("Welcome! A sample portfolio has been added so you can explore the analysis.", "ok")
            return redirect(url_for("docket"))
        return render_template("auth.html", mode="signup"), 400
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute("SELECT * FROM users WHERE username = ?",
                                (request.form.get("username", "").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = user["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("docket"))
        flash("Incorrect username or password.", "error")
        return render_template("auth.html", mode="login"), 401
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- pages
@app.get("/")
def home():
    return redirect(url_for("docket") if current_user() else url_for("login"))


@app.get("/dashboard")
@login_required
def docket():
    rows = get_db().execute("SELECT * FROM portfolios WHERE user_id = ? ORDER BY updated_at DESC",
                            (session["uid"],)).fetchall()
    cases = []
    for r in rows:
        h = json.loads(r["holdings"])
        cases.append({"id": r["id"], "name": r["name"], "n": len(h), "value": sum(x["amount"] for x in h),
                      "updated": r["updated_at"], "symbols": [x["symbol"] for x in h][:6]})
    return render_template("docket.html", cases=cases)


def parse_case_form():
    name = request.form.get("name", "").strip()[:80] or "Untitled portfolio"
    symbols = request.form.getlist("symbol")
    amounts = request.form.getlist("amount")
    known = {s["symbol"] for s in stock_universe()}
    holdings, errors = {}, []
    for sym, amt in zip(symbols, amounts):
        sym = sym.strip().upper()
        if not sym and not amt.strip():
            continue
        try:
            value = float(amt.replace(",", ""))
        except ValueError:
            errors.append(f"Amount for {sym or 'a row'} is not a number.")
            continue
        if known and sym not in known:
            errors.append(f"Unknown stock: {sym}.")
        elif not 1 <= value <= 1e10:
            errors.append(f"Amount for {sym} must be between ₹1 and ₹1,000 crore.")
        else:
            holdings[sym] = holdings.get(sym, 0) + value
    if not holdings and not errors:
        errors.append("Add at least one stock.")
    if len(holdings) > 30:
        errors.append("A portfolio can hold at most 30 stocks.")
    return name, [{"symbol": s, "amount": a} for s, a in holdings.items()], errors


@app.route("/portfolios/new", methods=["GET", "POST"])
@login_required
def new_case():
    if request.method == "POST":
        name, holdings, errors = parse_case_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("case_form.html", stocks=stock_universe(), name=name, holdings=holdings), 400
        db = get_db()
        cur = db.execute("INSERT INTO portfolios (user_id, name, holdings, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (session["uid"], name, json.dumps(holdings), now(), now()))
        db.commit()
        return redirect(url_for("case", case_id=cur.lastrowid))
    return render_template("case_form.html", stocks=stock_universe(), name="", holdings=[])


@app.route("/portfolios/<int:case_id>/edit", methods=["GET", "POST"])
@login_required
def edit_case(case_id):
    row = own_case(case_id)
    if request.method == "POST":
        name, holdings, errors = parse_case_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("case_form.html", stocks=stock_universe(), name=name, holdings=holdings,
                                   case_id=case_id), 400
        db = get_db()
        db.execute("UPDATE portfolios SET name = ?, holdings = ?, updated_at = ? WHERE id = ?",
                   (name, json.dumps(holdings), now(), case_id))
        db.commit()
        return redirect(url_for("case", case_id=case_id))
    return render_template("case_form.html", stocks=stock_universe(), name=row["name"],
                           holdings=json.loads(row["holdings"]), case_id=case_id)


@app.post("/portfolios/<int:case_id>/delete")
@login_required
def delete_case(case_id):
    own_case(case_id)
    db = get_db()
    db.execute("DELETE FROM portfolios WHERE id = ?", (case_id,))
    db.commit()
    flash("Portfolio deleted.", "ok")
    return redirect(url_for("docket"))


@app.get("/portfolios/<int:case_id>")
@login_required
def case(case_id):
    row = own_case(case_id)
    holdings = json.loads(row["holdings"])
    code, report = engine("POST", "/analyze", {"holdings": holdings})
    if code != 200:
        return render_template("case.html", case=row, report=None, error=report.get("error"),
                               detail=report.get("detail")), (503 if code >= 500 else 400)
    sectors = sorted({s["sector"] for s in stock_universe()})
    return render_template("case.html", case=row, report=report, sectors=sectors, error=None)


@app.get("/architecture")
@login_required
def records():
    code, card = engine("GET", "/model")
    return render_template("records.html", card=card if code == 200 else None, grafana_url=GRAFANA_URL,
                           engine_url=ENGINE_URL)


# ---------------------------------------------------------------- JSON API
@app.get("/api/stocks")
@login_required
def api_stocks():
    return jsonify(stock_universe())


@app.get("/api/portfolios")
@login_required
def api_cases():
    rows = get_db().execute("SELECT id, name, holdings, updated_at FROM portfolios WHERE user_id = ?",
                            (session["uid"],)).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "holdings": json.loads(r["holdings"]),
                     "updated_at": r["updated_at"]} for r in rows])


@app.get("/api/portfolios/<int:case_id>/analysis")
@login_required
def api_analysis(case_id):
    row = own_case(case_id)
    code, body = engine("POST", "/analyze", {"holdings": json.loads(row["holdings"])})
    return jsonify(body), code


@app.post("/api/portfolios/<int:case_id>/stress")
@login_required
def api_stress(case_id):
    row = own_case(case_id)
    payload = request.get_json(silent=True) or {}
    body = {"holdings": json.loads(row["holdings"]), "market_pct": payload.get("market_pct"),
            "sector": payload.get("sector"), "sector_pct": payload.get("sector_pct")}
    code, res = engine("POST", "/stress", body)
    return jsonify(res), code


# ---------------------------------------------------------------- ops
@app.get("/health")
def health():
    code, body = engine("GET", "/health", timeout=3)
    return jsonify(status="ok", engine=body if code == 200 else "unreachable"), 200


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(500)
def error_page(err):
    code = getattr(err, "code", 500)
    msg = getattr(err, "description", "Something went wrong.")
    if request.path.startswith("/api/"):
        return jsonify(error=msg), code
    return render_template("error.html", code=code, message=msg), code


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
