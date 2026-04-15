#!/usr/bin/env python3
"""Kingshot gift code auto-redeem tool.

Scrapes active codes from kingshot.net, tracks what's been redeemed,
and only attempts new codes. Run manually or via cron.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

API_BASE = "https://kingshot-giftcode.centurygame.com/api"
SALT = "mN4!pQs6JrYwV9"
SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "redeem.log"
HISTORY_FILE = SCRIPT_DIR / "history.json"

PLAYER_IDS = ["117357420", "133903370", "162814071"]

# Fallback codes if scraping fails
FALLBACK_CODES = ["BUNNY405", "NOFOOLIN", "OFFICIALSTORE04", "VIP777"]

SCRAPE_URLS = [
    "https://kingshot.net/gift-codes",
]

ERR_MESSAGES = {
    "20000": "Redeemed successfully",
    "40004": "Timeout, retry",
    "40007": "Code expired",
    "40008": "Already claimed",
    "40014": "Code not found",
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def make_sign(params: dict) -> str:
    sorted_items = sorted(params.items())
    query = "&".join(f"{k}={v}" for k, v in sorted_items)
    return hashlib.md5((query + SALT).encode()).hexdigest()


def login(fid: str) -> dict | None:
    params = {"fid": fid, "time": str(int(time.time() * 1000))}
    params["sign"] = make_sign(params)
    try:
        resp = requests.post(f"{API_BASE}/player", data=params, timeout=60)
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data", {})
        log(f"  Login failed for {fid}: {data}")
    except Exception as e:
        log(f"  Login error for {fid}: {e}")
    return None


def redeem(fid: str, code: str) -> tuple[str, str]:
    params = {
        "fid": fid,
        "cdk": code,
        "captcha_code": "",
        "time": str(int(time.time() * 1000)),
    }
    params["sign"] = make_sign(params)
    try:
        resp = requests.post(f"{API_BASE}/gift_code", data=params, timeout=60)
        data = resp.json()
        err = str(data.get("err_code", ""))
        msg = data.get("msg", "")
        status = ERR_MESSAGES.get(err, msg or f"Unknown ({err})")
        return err, status
    except Exception as e:
        return "error", str(e)


def scrape_codes() -> list[str]:
    """Scrape active codes from kingshot.net."""
    codes = set()
    try:
        resp = requests.get(
            "https://kingshot.net/gift-codes",
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        text = resp.text
        # Find the Active section (before Expired section)
        active_start = text.find(">Active Gift Codes<")
        expired_start = text.find(">Expired Gift Codes<")
        if active_start >= 0 and expired_start >= 0:
            active_section = text[active_start:expired_start]
        elif active_start >= 0:
            active_section = text[active_start:active_start + 50000]
        else:
            active_section = text

        # Codes are in: <p class="font-mono text-xl font-bold tracking-wider">CODE</p>
        found = re.findall(
            r'font-mono[^>]*tracking-wider[^>]*>([A-Za-z0-9]+)<', active_section
        )
        codes.update(found)
    except Exception as e:
        log(f"  Scrape failed: {e}")

    codes = {c for c in codes if len(c) >= 4}
    return sorted(codes)


def main():
    log("=== Kingshot Auto-Redeem Started ===")

    # Scrape for codes
    codes = scrape_codes()
    if codes:
        log(f"Scraped {len(codes)} active codes: {', '.join(codes)}")
    else:
        codes = FALLBACK_CODES
        log(f"Scraping returned nothing, using {len(codes)} fallback codes")

    # Load history of successful redemptions
    history = load_history()
    new_redemptions = 0

    for fid in PLAYER_IDS:
        if fid not in history:
            history[fid] = {}

        player = login(fid)
        if not player:
            log(f"Skipping player {fid} (login failed)")
            continue

        name = player.get("nickname", "?")
        kingdom = player.get("kid", "?")
        log(f"Player: {name} ({fid}) | Kingdom: {kingdom}")

        # Filter to codes not yet successfully redeemed by this player
        new_codes = [c for c in codes if c not in history[fid]]
        if not new_codes:
            log(f"  No new codes to try")
            continue

        log(f"  Trying {len(new_codes)} new codes: {', '.join(new_codes)}")

        for code in new_codes:
            err_code, status = redeem(fid, code)

            if err_code == "20000":
                log(f"  [+] {code}: {status}")
                history[fid][code] = {"status": "redeemed", "date": datetime.now().isoformat()}
                new_redemptions += 1
            elif err_code == "40008":
                log(f"  [=] {code}: {status}")
                history[fid][code] = {"status": "already_claimed", "date": datetime.now().isoformat()}
            elif err_code in ("40007", "40014"):
                log(f"  [-] {code}: {status}")
                history[fid][code] = {"status": "invalid", "date": datetime.now().isoformat()}
            else:
                # Don't save to history so we retry next time
                log(f"  [?] {code}: {status}")

            time.sleep(2)

    save_history(history)
    log(f"Done. {new_redemptions} new redemptions.\n")


if __name__ == "__main__":
    main()
