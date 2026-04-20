"""Kingshot Gift Code Auto-Redeemer — Web App.

Flask app that lets users add Kingshot player accounts and redeem
all active gift codes across all saved accounts with one click.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from functools import wraps

import requests as http_requests
from flask import Flask, Response, jsonify, make_response, render_template, request

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "kingshot.db"))
API_BASE = "https://kingshot-giftcode.centurygame.com/api"
SALT = "mN4!pQs6JrYwV9"
COOKIE_NAME = "kingshot_session"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year

FALLBACK_CODES = ["BUNNY405", "NOFOOLIN", "OFFICIALSTORE04", "VIP777"]

# Default accounts — auto-added for every new session
DEFAULT_ACCOUNTS = ["117357420", "133903370", "162814071"]

ERR_MESSAGES = {
    "20000": "Redeemed successfully",
    "40004": "Timeout, retry",
    "40007": "Code expired",
    "40008": "Already claimed",
    "40014": "Code not found",
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            fid TEXT NOT NULL,
            nickname TEXT NOT NULL,
            kingdom TEXT,
            added_at TEXT NOT NULL,
            UNIQUE(session_id, fid)
        );
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            attempted TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Session middleware
# ---------------------------------------------------------------------------

def get_session_id():
    """Get or create a session ID from the cookie."""
    return request.cookies.get(COOKIE_NAME)


def ensure_session(f):
    """Decorator that ensures a valid session exists."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        session_id = get_session_id()
        if not session_id:
            return jsonify(ok=False, error="No session. Please refresh the page."), 401
        conn = get_db()
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify(ok=False, error="Session expired. Please refresh the page."), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Kingshot API helpers
# ---------------------------------------------------------------------------

def make_sign(params: dict) -> str:
    sorted_items = sorted(params.items())
    query = "&".join(f"{k}={v}" for k, v in sorted_items)
    return hashlib.md5((query + SALT).encode()).hexdigest()


def kingshot_login(fid: str) -> dict | None:
    params = {"fid": fid, "time": str(int(time.time() * 1000))}
    params["sign"] = make_sign(params)
    try:
        resp = http_requests.post(f"{API_BASE}/player", data=params, timeout=60)
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data", {})
    except Exception:
        pass
    return None


def kingshot_redeem(fid: str, code: str) -> tuple[str, str]:
    params = {
        "fid": fid,
        "cdk": code,
        "captcha_code": "",
        "time": str(int(time.time() * 1000)),
    }
    params["sign"] = make_sign(params)
    try:
        resp = http_requests.post(f"{API_BASE}/gift_code", data=params, timeout=60)
        data = resp.json()
        err = str(data.get("err_code", ""))
        msg = data.get("msg", "")
        status = ERR_MESSAGES.get(err, msg or f"Unknown ({err})")
        return err, status
    except Exception as e:
        return "error", str(e)


# ---------------------------------------------------------------------------
# Code scraping
# ---------------------------------------------------------------------------

def scrape_codes() -> list[str]:
    codes = set()
    try:
        resp = http_requests.get(
            "https://kingshot.net/gift-codes",
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        text = resp.text
        active_start = text.find(">Active Gift Codes<")
        expired_start = text.find(">Expired Gift Codes<")
        if active_start >= 0 and expired_start >= 0:
            active_section = text[active_start:expired_start]
        elif active_start >= 0:
            active_section = text[active_start:active_start + 50000]
        else:
            active_section = text
        found = re.findall(
            r'font-mono[^>]*tracking-wider[^>]*>([A-Za-z0-9]+)<', active_section
        )
        codes.update(found)
    except Exception:
        pass
    codes = {c for c in codes if len(c) >= 4}
    return sorted(codes) if codes else FALLBACK_CODES


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    session_id = get_session_id()
    now = datetime.utcnow().isoformat()
    conn = get_db()

    if not session_id:
        session_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sessions (id, created_at, last_seen) VALUES (?, ?, ?)",
            (session_id, now, now),
        )
        conn.commit()
    else:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO sessions (id, created_at, last_seen) VALUES (?, ?, ?)",
                (session_id, now, now),
            )
        else:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, session_id))
        conn.commit()

    # Auto-add default accounts if this session has none
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    if existing_count == 0:
        for fid in DEFAULT_ACCOUNTS:
            player = kingshot_login(fid)
            if player:
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (session_id, fid, nickname, kingdom, added_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, fid, player.get("nickname", "Unknown"), str(player.get("kid", "")), now),
                )
        conn.commit()

    accounts = conn.execute(
        "SELECT id, fid, nickname, kingdom FROM accounts WHERE session_id = ? ORDER BY added_at",
        (session_id,),
    ).fetchall()
    conn.close()

    resp = make_response(render_template("index.html", accounts=[dict(a) for a in accounts]))
    resp.set_cookie(COOKIE_NAME, session_id, max_age=COOKIE_MAX_AGE, httponly=True, samesite="Lax")
    return resp


@app.route("/api/accounts", methods=["GET"])
@ensure_session
def list_accounts():
    session_id = get_session_id()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, fid, nickname, kingdom FROM accounts WHERE session_id = ? ORDER BY added_at",
        (session_id,),
    ).fetchall()
    conn.close()
    return jsonify(accounts=[dict(r) for r in rows])


@app.route("/api/accounts", methods=["POST"])
@ensure_session
def add_account():
    session_id = get_session_id()
    data = request.get_json(force=True)
    fid = (data.get("fid") or "").strip()

    if not fid:
        return jsonify(ok=False, error="Player ID is required."), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM accounts WHERE session_id = ? AND fid = ?",
        (session_id, fid),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify(ok=False, error="This account is already added."), 409

    player = kingshot_login(fid)
    if not player:
        conn.close()
        return jsonify(ok=False, error="Could not verify player ID. Check the ID and try again."), 400

    nickname = player.get("nickname", "Unknown")
    kingdom = str(player.get("kid", ""))
    now = datetime.utcnow().isoformat()

    cur = conn.execute(
        "INSERT INTO accounts (session_id, fid, nickname, kingdom, added_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, fid, nickname, kingdom, now),
    )
    conn.commit()
    account_id = cur.lastrowid
    conn.close()

    return jsonify(ok=True, account={"id": account_id, "fid": fid, "nickname": nickname, "kingdom": kingdom})


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
@ensure_session
def delete_account(account_id):
    session_id = get_session_id()
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE id = ? AND session_id = ?",
        (account_id, session_id),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Account not found."), 404

    conn.execute("DELETE FROM redemptions WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/redeem", methods=["POST"])
@ensure_session
def redeem_all():
    session_id = get_session_id()
    conn = get_db()
    accounts = conn.execute(
        "SELECT id, fid, nickname, kingdom FROM accounts WHERE session_id = ? ORDER BY added_at",
        (session_id,),
    ).fetchall()

    if not accounts:
        conn.close()
        return jsonify(ok=False, error="Add at least one account before redeeming."), 400

    codes = scrape_codes()
    now = datetime.utcnow().isoformat()
    results = []

    for acct in accounts:
        # Must login before redeeming (API requires it)
        kingshot_login(acct["fid"])

        acct_results = []
        for code in codes:
            err_code, status_msg = kingshot_redeem(acct["fid"], code)

            if err_code == "20000":
                status = "redeemed"
            elif err_code == "40008":
                status = "already_claimed"
            elif err_code == "40007":
                status = "expired"
            elif err_code == "40014":
                status = "not_found"
            else:
                status = "error"

            conn.execute(
                "INSERT INTO redemptions (account_id, code, status, message, attempted) VALUES (?, ?, ?, ?, ?)",
                (acct["id"], code, status, status_msg, now),
            )
            acct_results.append({"code": code, "status": status, "message": status_msg})
            time.sleep(2)

        results.append({
            "fid": acct["fid"],
            "nickname": acct["nickname"],
            "codes": acct_results,
        })

    conn.commit()
    conn.close()
    return jsonify(codes_tried=codes, results=results)


@app.route("/api/history", methods=["GET"])
@ensure_session
def get_history():
    session_id = get_session_id()
    conn = get_db()
    rows = conn.execute("""
        SELECT r.code, a.nickname, r.status, r.message, r.attempted
        FROM redemptions r
        JOIN accounts a ON a.id = r.account_id
        WHERE a.session_id = ?
        ORDER BY r.attempted DESC, a.nickname, r.code
        LIMIT 500
    """, (session_id,)).fetchall()
    conn.close()
    return jsonify(history=[dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5015, debug=True)
