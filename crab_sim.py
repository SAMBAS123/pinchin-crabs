#!/usr/bin/env python3
"""Crab trading terminal for $PINCHIN on Solana."""

import os
import sys
import time
import random
import shutil
import threading
import json
import math
import base64
import statistics
import importlib.util
import urllib.request
import asyncio
import logging
import logging.handlers

# --- Backend debug log (tail -f ~/.pinchin_debug.log) ---
_debug_log = logging.getLogger("crab_debug")
_debug_log.setLevel(logging.DEBUG)
_debug_fh = logging.handlers.RotatingFileHandler(
    os.path.expanduser("~/.pinchin_debug.log"), maxBytes=5_000_000, backupCount=2)
_debug_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_debug_log.addHandler(_debug_fh)


# --- Live price feed for $PINCHIN ---
PINCHIN_CONTRACT = "5xQibgLSix2ptJ4mvvcPPYmnBxEhbtR4DB2YxVs1pump"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# $PINCHIN only — all crabs focus on building this chart
APPROVED_TOKENS = {PINCHIN_CONTRACT: "PINCHIN"}

# Permanently blocked tokens — never buy or sell these
BLACKLISTED_TOKENS = {PINCHIN_CONTRACT}

# Trading config
TRADE_AMOUNT_SOL = 0.03  # Fixed lot size per trade
TRADE_COOLDOWN = 300  # seconds between trades per crab
SNIPE_COOLDOWN = 60   # seconds between trades for sniped (non-PINCHIN) tokens
SNIPE_TP = 0.50       # +50% take profit on snipes
SNIPE_SL = -0.20      # -20% stop loss on snipes
SNIPE_MAX_HOLD = 600  # 10 min max hold then force exit
INITIAL_BAG_SOL = 0.10  # SOL each crab spends on startup to get a bag
KEYS_FILE = os.path.expanduser("~/.pinchin_keys.json")
POSITIONS_FILE = os.path.expanduser("~/.pinchin_positions.json")
DEPOSITS_FILE = os.path.expanduser("~/.pinchin_deposits.json")
WL_FILE = os.path.expanduser("~/.pinchin_wl.json")

def load_deposits():
    """Load per-crab deposit totals from disk."""
    if os.path.exists(DEPOSITS_FILE):
        try:
            with open(DEPOSITS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_deposits(deposits):
    try:
        with open(DEPOSITS_FILE, "w") as f:
            json.dump(deposits, f, indent=2)
    except Exception:
        pass

CRAB_DEPOSITS = load_deposits()  # {crab_name: total_sol_deposited}

def load_wl():
    """Load per-crab wins/losses from disk."""
    if os.path.exists(WL_FILE):
        try:
            with open(WL_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_wl(wl):
    try:
        with open(WL_FILE, "w") as f:
            json.dump(wl, f, indent=2)
    except Exception:
        pass

CRAB_WL = load_wl()  # {crab_name: {"wins": N, "losses": N}}

class PriceFeed:
    """Multi-token price feed via DexScreener."""
    def __init__(self):
        self.prices = {}  # mint -> {price_usd, price_native, market_cap, change_5m, change_1h, trend, symbol}
        self.extra_mints = {}  # mint -> symbol (for sniped tokens)
        self.alive = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self):
        while self.alive:
            try:
                mints = list(APPROVED_TOKENS.keys()) + list(self.extra_mints.keys())
                mint_str = ",".join(mints)
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_str}"
                req = urllib.request.Request(url, headers={"User-Agent": "CrabSim/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                pairs = data.get("pairs", [])
                seen = set()
                for p in pairs:
                    base_mint = p.get("baseToken", {}).get("address", "")
                    all_tracked = {**APPROVED_TOKENS, **self.extra_mints}
                    if base_mint in all_tracked and base_mint not in seen:
                        seen.add(base_mint)
                        with self._lock:
                            old = self.prices.get(base_mint, {})
                            last_price = old.get("price_usd", 0.0)
                            new_price = float(p.get("priceUsd", 0))
                            changes = p.get("priceChange", {})
                            old_trend = old.get("trend", "")
                            if last_price > 0:
                                trend = "up" if new_price > last_price else "down" if new_price < last_price else old_trend
                            else:
                                trend = ""
                            self.prices[base_mint] = {
                                "price_usd": new_price,
                                "price_native": float(p.get("priceNative", 0) or 0),
                                "market_cap": float(p.get("marketCap", 0) or 0),
                                "change_5m": float(changes.get("m5", 0) or 0),
                                "change_1h": float(changes.get("h1", 0) or 0),
                                "trend": trend,
                                "symbol": p.get("baseToken", {}).get("symbol") or all_tracked.get(base_mint, base_mint[:6]),
                            }
            except Exception:
                pass
            time.sleep(15)

    def get(self, mint=None):
        """Get price data for a token. Defaults to PINCHIN for backward compat."""
        mint = mint or PINCHIN_CONTRACT
        with self._lock:
            d = self.prices.get(mint, {})
            return {
                "price": d.get("price_usd", 0.0),
                "price_sol": d.get("price_native", 0.0),
                "mc": d.get("market_cap", 0.0),
                "change_5m": d.get("change_5m", 0.0),
                "change_1h": d.get("change_1h", 0.0),
                "trend": d.get("trend", ""),
            }

    def get_all(self):
        """Get price data for all approved tokens."""
        with self._lock:
            result = {}
            for mint, d in self.prices.items():
                result[mint] = {
                    "price": d.get("price_usd", 0.0),
                    "price_sol": d.get("price_native", 0.0),
                    "mc": d.get("market_cap", 0.0),
                    "change_5m": d.get("change_5m", 0.0),
                    "change_1h": d.get("change_1h", 0.0),
                    "trend": d.get("trend", ""),
                    "symbol": d.get("symbol", ""),
                }
            return result

    def stop(self):
        self.alive = False

# --- Crab wallets (Solana) ---
CRAB_WALLETS = {
    "Mr.Krabs": "8ai1C3LzE3DsjWvNkaEWjvUVDeAJ7TB5QjYbdZ8vaPT4",
    "Pinchy": "AoPiw4R6omoF1xW3Hk4mpVdKJpBBA4hKx6w4tNaZcUNB",
    "Clawdia": "BXW25qYawV4UzCLKCaFGTCnFjARn1wUKaM2Z2FnRcYgz",
    "Sandy": "8hq9Ae8PVAtg4XuU8c92b977NLDiwT4e4euDznaJUbXq",
    "Snippy": "FgMn627wpHTkuQ5V8sAo4J1sN4GNjMhsnDWEL97KN1Cn",
    "Hermie": "BFjsms6Eb8bGNvDhRYT1i7xQiViaBcjsVvd7gzjx8etJ",
    "Bastian": "95CQLr3MCsGwQTGD9P1wuSLCK9AeGqx9HNFy5PxP8x3n",
}

BENCHED_CRABS = {"Clawdia", "Sandy", "Snippy", "Hermie", "Bastian"}

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
READ_RPC = SOLANA_RPC   # free RPC for reads (public Solana or QuickNode)
WRITE_RPC = SOLANA_RPC  # overwritten at runtime if Helius key exists
WEBHOOK_PORT = 3001
WEBHOOK_SECRET = ""     # optional auth header, set from keys file


class WalletFeed:
    def __init__(self):
        self.balances = {}  # wallet_addr -> SOL balance
        self.token_balances = {}  # wallet_addr -> {mint: balance}
        self.alive = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self):
        while self.alive:
            for addr in CRAB_WALLETS.values():
                if not self.alive:
                    break
                # SOL balance
                try:
                    payload = json.dumps({
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance",
                        "params": [addr]
                    }).encode()
                    req = urllib.request.Request(
                        READ_RPC, data=payload,
                        headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read())
                    lamports = data.get("result", {}).get("value", 0)
                    with self._lock:
                        self.balances[addr] = lamports / 1_000_000_000
                except Exception:
                    pass

                time.sleep(5)

                # Token balances for all approved tokens
                for mint in list(APPROVED_TOKENS.keys()):
                    if not self.alive:
                        break
                    try:
                        payload = json.dumps({
                            "jsonrpc": "2.0", "id": 2,
                            "method": "getTokenAccountsByOwner",
                            "params": [addr,
                                {"mint": mint},
                                {"encoding": "jsonParsed"}]
                        }).encode()
                        req = urllib.request.Request(
                            READ_RPC, data=payload,
                            headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                        )
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            data = json.loads(resp.read())
                        accounts = data.get("result", {}).get("value", [])
                        ui_amount = 0.0
                        if accounts:
                            info = accounts[0]["account"]["data"]["parsed"]["info"]
                            ui_amount = float(info["tokenAmount"]["uiAmount"] or 0)
                        with self._lock:
                            if addr not in self.token_balances:
                                self.token_balances[addr] = {}
                            self.token_balances[addr][mint] = ui_amount
                    except Exception:
                        pass
                    time.sleep(5)
            time.sleep(120)

    def fetch_all_now(self):
        """Blocking one-shot fetch of all wallet balances (no sleeps)."""
        mints = list(APPROVED_TOKENS.keys())
        for addr in CRAB_WALLETS.values():
            # SOL balance
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getBalance",
                    "params": [addr]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                lamports = data.get("result", {}).get("value", 0)
                with self._lock:
                    self.balances[addr] = lamports / 1_000_000_000
            except Exception:
                pass
            # Token balances
            for mint in mints:
                try:
                    payload = json.dumps({
                        "jsonrpc": "2.0", "id": 2,
                        "method": "getTokenAccountsByOwner",
                        "params": [addr, {"mint": mint}, {"encoding": "jsonParsed"}]
                    }).encode()
                    req = urllib.request.Request(
                        READ_RPC, data=payload,
                        headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read())
                    accounts = data.get("result", {}).get("value", [])
                    ui_amount = 0.0
                    if accounts:
                        info = accounts[0]["account"]["data"]["parsed"]["info"]
                        ui_amount = float(info["tokenAmount"]["uiAmount"] or 0)
                    with self._lock:
                        if addr not in self.token_balances:
                            self.token_balances[addr] = {}
                        self.token_balances[addr][mint] = ui_amount
                except Exception:
                    pass

    def refresh_wallet(self, addr):
        """Targeted single-wallet refresh — called by webhook on activity."""
        # SOL balance
        try:
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [addr]
            }).encode()
            req = urllib.request.Request(
                READ_RPC, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            lamports = data.get("result", {}).get("value", 0)
            with self._lock:
                self.balances[addr] = lamports / 1_000_000_000
        except Exception:
            pass
        # Token balances
        for mint in list(APPROVED_TOKENS.keys()):
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 2,
                    "method": "getTokenAccountsByOwner",
                    "params": [addr, {"mint": mint}, {"encoding": "jsonParsed"}]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                accounts = data.get("result", {}).get("value", [])
                ui_amount = 0.0
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]
                    ui_amount = float(info["tokenAmount"]["uiAmount"] or 0)
                with self._lock:
                    if addr not in self.token_balances:
                        self.token_balances[addr] = {}
                    self.token_balances[addr][mint] = ui_amount
            except Exception:
                pass

    def get_balance(self, addr):
        with self._lock:
            return self.balances.get(addr, 0.0)

    def get_token_balance(self, addr, mint=None):
        mint = mint or PINCHIN_CONTRACT
        with self._lock:
            return self.token_balances.get(addr, {}).get(mint, 0.0)

    def stop(self):
        self.alive = False


class HolderFeed:
    """On-demand top 20 PINCHIN holders via getTokenLargestAccounts.

    Fetches only when refresh() is called (e.g. when user navigates to holders room).
    Diffs balances between fetches to classify behavior.
    """

    def __init__(self):
        self._holders = []       # [{"address", "amount", "rank", "behavior", "pct_change"}, ...]
        self._prev_snapshot = {} # address -> amount (previous poll)
        self._lock = threading.Lock()
        self._ready = False      # True after first successful fetch
        self._fetching = False   # True while a fetch thread is running
        self._last_fetch = 0     # timestamp of last successful fetch
        self._error = ""         # last error message for display

    def refresh(self):
        """Trigger a fetch in a background thread. No-op if already fetching."""
        with self._lock:
            if self._fetching:
                return
            self._fetching = True
            self._error = ""
        t = threading.Thread(target=self._do_fetch, daemon=True)
        t.start()

    def _do_fetch(self):
        retries = 3
        for attempt in range(retries):
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenLargestAccounts",
                    "params": [PINCHIN_CONTRACT]
                }).encode()
                # Use WRITE_RPC (Helius) if available — public RPC rate-limits heavily
                rpc = WRITE_RPC if WRITE_RPC != SOLANA_RPC else READ_RPC
                req = urllib.request.Request(
                    rpc, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                accounts = data.get("result", {}).get("value", [])
                holders = []
                current_snapshot = {}
                for i, acct in enumerate(accounts[:20]):
                    amount = float(acct.get("uiAmount") or acct.get("amount", 0))
                    address = acct.get("address", "")
                    current_snapshot[address] = amount
                    prev = self._prev_snapshot.get(address)
                    if prev is None or prev == 0:
                        behavior = "diamond"
                        pct_change = 0.0
                    else:
                        pct_change = (amount - prev) / prev
                        if pct_change < -0.05:
                            behavior = "jeet"
                        elif pct_change > 0.05:
                            behavior = "accumulator"
                        else:
                            behavior = "diamond"
                    holders.append({
                        "address": address,
                        "amount": amount,
                        "rank": i + 1,
                        "behavior": behavior,
                        "pct_change": pct_change,
                    })
                with self._lock:
                    self._holders = holders
                    self._prev_snapshot = current_snapshot
                    self._ready = True
                    self._fetching = False
                    self._last_fetch = time.time()
                return  # success
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))  # 3s, 6s backoff
                else:
                    with self._lock:
                        self._error = str(e)[:60]
                        self._fetching = False

    def get_holders(self):
        with self._lock:
            return list(self._holders)

    def is_ready(self):
        with self._lock:
            return self._ready


class HeliusWebhook:
    """Push-based wallet updates via Helius enhanced webhooks."""
    def __init__(self, wallet_feed):
        self.wallet_feed = wallet_feed
        self._crab_addrs = set(CRAB_WALLETS.values())
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from socketserver import ThreadingMixIn

        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        webhook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    # Optional auth check
                    if WEBHOOK_SECRET:
                        auth = self.headers.get("Authorization", "")
                        if auth != WEBHOOK_SECRET:
                            self.send_response(401)
                            self.end_headers()
                            return
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    events = json.loads(body)
                    if not isinstance(events, list):
                        events = [events]
                    # Collect involved crab addresses
                    involved = set()
                    for event in events:
                        for nt in event.get("nativeTransfers", []):
                            for key in ("fromUserAccount", "toUserAccount"):
                                addr = nt.get(key, "")
                                if addr in webhook._crab_addrs:
                                    involved.add(addr)
                        for tt in event.get("tokenTransfers", []):
                            for key in ("fromUserAccount", "toUserAccount"):
                                addr = tt.get(key, "")
                                if addr in webhook._crab_addrs:
                                    involved.add(addr)
                    # Trigger targeted refresh for each involved wallet
                    for addr in involved:
                        try:
                            webhook.wallet_feed.refresh_wallet(addr)
                        except Exception:
                            pass
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args):
                pass  # silence request logs

        try:
            server = ThreadedHTTPServer(("0.0.0.0", WEBHOOK_PORT), Handler)
            server.serve_forever()
        except Exception:
            pass


# --- Auto Trader (Jupiter swap via api.jup.ag) ---
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# PumpPortal + Jito execution (primary path — faster, MEV-protected, 0.5% fee)
PUMPPORTAL_API = "https://pumpportal.fun/api/trade-local"
JITO_BUNDLE_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_MAX_BUNDLE = 5        # Max txs per Jito bundle
PP_BUY_SLIPPAGE = 25       # 25% — aggressive for pump.fun sniping
PP_BUY_TIP = 0.0001        # SOL priority fee (was 0.001 — 10x too high for RPC fallback)
PP_SELL_SLIPPAGE = 5              # Fixed 5% (500 bps) exit slippage
PP_SELL_TIP = [0.00005, 0.0001, 0.0005, 0.001]  # SOL priority fee — escalates with retries
SELL_PRIORITY_LAMPORTS = ["auto", 50_000, 100_000, 500_000, 1_000_000]  # Jupiter priority fee escalation
SELL_MAX_RETRIES = 5

EVOLVED_STRATEGY_PATH = os.path.expanduser("~/.pinchin_evolved_strategy.py")
EVOLVED_RELOAD_INTERVAL = 60  # seconds between hot-reload checks

class AutoTrader:
    """Buy the dip, sell the rip. Take 50% profit at 2x."""
    def __init__(self):
        self.keypairs = {}  # crab_name -> Keypair
        self.last_trade = {}  # crab_name -> timestamp
        self.trade_log = []
        self.jup_api_key = ""
        self.helius_api_key = ""
        # Position tracking per crab: {crab_name: {mint: {"tokens": int, "cost_sol": float, "avg_price": float}}}
        self.positions = {}
        self.price_feed = None  # set by main()
        self.price_history = None  # set by main() - PriceHistoryCollector
        self.wallet_feed = None  # set by main()
        self._lock = threading.Lock()
        # Evolved strategy support
        self._evolved_strategy = None
        self._evolved_mtime = 0
        self._evolved_last_check = 0
        self.pending_thoughts = {}  # crab_name -> {"action": str, "reason": str}
        self.crab_modifiers = {}  # crab_name -> {"trade_mult": 1.0, "cooldown_mult": 1.0, "expires": float}
        self.community_controlled = None  # crab name under chat control (skip AI trades)
        self._selling = set()  # (crab_name, mint) pairs with active sell thread — prevents duplicates
        self.gen_tracker = None  # set by main() — GenerationTracker instance
        self.chat_poster = None  # set by main() — PumpChatPoster instance
        self.twitter_poster = None  # set by main() — TwitterPoster instance
        self.crab_brain = None  # set by main() — CrabBrain instance
        self.win_flash = None  # {"text": str, "color": str, "ticks_left": int} — set by record_trade on wins
        self._load_evolved_strategy()
        self._load_keys()

    def _load_evolved_strategy(self):
        """Load or hot-reload the evolved strategy from disk."""
        if not os.path.exists(EVOLVED_STRATEGY_PATH):
            return
        try:
            mtime = os.path.getmtime(EVOLVED_STRATEGY_PATH)
            if mtime == self._evolved_mtime:
                return  # unchanged
            spec = importlib.util.spec_from_file_location("evolved_strat", EVOLVED_STRATEGY_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._evolved_strategy = mod.EvolvedCrabStrategy()
            self._evolved_mtime = mtime
        except Exception:
            self._evolved_strategy = None

    def _maybe_reload_evolved(self):
        """Check for evolved strategy updates (called periodically)."""
        now = time.time()
        if now - self._evolved_last_check < EVOLVED_RELOAD_INTERVAL:
            return
        self._evolved_last_check = now
        self._load_evolved_strategy()

    def _build_sensors(self, crab_name, mint):
        """Build the sensor dict for the evolved strategy."""
        price_data = self.price_feed.get(mint) if self.price_feed else None
        if not price_data or price_data["price"] <= 0:
            return None

        pos = self.positions.get(crab_name, {}).get(mint)
        tokens = pos["tokens"] if pos else 0
        avg_price = pos["avg_price"] if pos else 0.0
        has_position = tokens > 0

        unrealized_pnl_pct = 0.0
        if has_position and avg_price > 0:
            unrealized_pnl_pct = ((price_data["price"] - avg_price) / avg_price) * 100

        # Price history from collector (PINCHIN only — snipes don't have history)
        price_history_5m = []
        if self.price_history and mint == PINCHIN_CONTRACT:
            price_history_5m = self.price_history.get_5m_prices(12)

        # Volatility from recent prices
        volatility = 0.0
        if price_history_5m and len(price_history_5m) >= 3:
            mean = statistics.mean(price_history_5m)
            if mean > 0:
                volatility = statistics.stdev(price_history_5m) / mean

        # SOL balance from wallet feed
        sol_balance = 0.1  # default
        if self.wallet_feed and crab_name in CRAB_WALLETS:
            sol_balance = self.wallet_feed.get_balance(CRAB_WALLETS[crab_name])

        # Time since last trade
        last = self.last_trade.get(crab_name, 0)
        minutes_since = (time.time() - last) / 60 if last > 0 else 999.0

        # Total trades
        total_trades = sum(1 for t in self.trade_log if t.get("crab") == crab_name)

        return {
            "price_usd": price_data["price"],
            "change_5m": price_data["change_5m"],
            "change_1h": price_data["change_1h"],
            "trend": price_data["trend"],
            "has_position": has_position,
            "tokens": tokens,
            "avg_price": avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "price_history_5m": price_history_5m,
            "volatility": volatility,
            "sol_balance": sol_balance,
            "minutes_since_last_trade": minutes_since,
            "total_trades": total_trades,
        }

    def _try_evolved_strategy(self, crab_name, mint, current_price, trade_sol, pos):
        """Try using the evolved strategy. Returns True if it handled the trade."""
        if not self._evolved_strategy:
            return False

        sensors = self._build_sensors(crab_name, mint)
        if not sensors:
            return False

        try:
            decision = self._evolved_strategy.decide(sensors)
        except Exception:
            return False

        action = decision.get("action", "hold")
        fraction = max(0.0, min(1.0, decision.get("amount_fraction", 0.0)))
        reason = decision.get("reason", "evolved")

        # Always show the thought, even on hold
        if action == "buy":
            pct = int(fraction * 100)
            self.pending_thoughts[crab_name] = f"BUY {pct}% -- {reason}"
        elif action == "sell":
            pct = int(fraction * 100)
            self.pending_thoughts[crab_name] = f"SELL {pct}% -- {reason}"
        else:
            # Show interesting hold reasons instead of "no signal"
            hold_msgs = [
                "hmm... watching the chart",
                "not yet... waiting for dip",
                "patience... no edge here",
                "scanning... nothing good",
                "nah... market too calm",
                "holding... need more data",
                "thinking... risky right now",
            ]
            if reason == "no signal":
                self.pending_thoughts[crab_name] = random.choice(hold_msgs)
            else:
                self.pending_thoughts[crab_name] = f"... {reason}"

        # Minimum balance guard — don't trade dust wallets
        sol_balance = 0.1
        if self.wallet_feed and crab_name in CRAB_WALLETS:
            sol_balance = self.wallet_feed.get_balance(CRAB_WALLETS[crab_name])
        if action == "buy" and sol_balance < 0.005:
            self.pending_thoughts[crab_name] = "low funds... conserving"
            return True  # treat as hold

        if action == "buy" and fraction > 0:
            # Fixed lot size
            sol_amount = TRADE_AMOUNT_SOL
            self.last_trade[crab_name] = time.time()
            t = threading.Thread(
                target=self._execute_buy,
                args=(crab_name, mint, current_price, sol_amount),
                daemon=True
            )
            t.start()
            return True
        elif action == "sell" and mint == PINCHIN_CONTRACT:
            self.pending_thoughts[crab_name] = "diamond claws... never selling"
            return True  # block PINCHIN sells — hold only
        elif action == "sell" and fraction > 0 and pos and pos["tokens"] > 0:
            sell_tokens = int(pos["tokens"] * fraction)
            if sell_tokens > 0:
                self.last_trade[crab_name] = time.time()
                t = threading.Thread(
                    target=self._execute_sell,
                    args=(crab_name, mint, sell_tokens, reason[:16]),
                    daemon=True
                )
                t.start()
                return True

        return action == "hold"  # hold = handled (don't fall through to hardcoded)

    def _load_keys(self):
        if not os.path.exists(KEYS_FILE):
            return
        try:
            import base58
            from solders.keypair import Keypair
            with open(KEYS_FILE) as f:
                keys = json.load(f)
            if "JUP_API_KEY" in keys:
                self.jup_api_key = keys["JUP_API_KEY"]
            if "HELIUS_API_KEY" in keys and keys["HELIUS_API_KEY"]:
                helius_key = keys["HELIUS_API_KEY"]
                global WRITE_RPC
                WRITE_RPC = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
                self.helius_api_key = helius_key
                print(f"[HELIUS] Staked connections active: {helius_key[:8]}...")
            for name, privkey_str in keys.items():
                if name in ("JUP_API_KEY", "HELIUS_API_KEY"):
                    continue
                if not isinstance(privkey_str, str) or privkey_str.startswith("PASTE"):
                    continue
                try:
                    key_bytes = base58.b58decode(privkey_str)
                    kp = Keypair.from_bytes(key_bytes)
                    self.keypairs[name] = kp
                    if name not in self.positions:
                        self.positions[name] = {}
                except Exception:
                    pass
        except Exception:
            pass
        self._load_positions()

    def _load_positions(self):
        if not os.path.exists(POSITIONS_FILE):
            return
        try:
            with open(POSITIONS_FILE) as f:
                saved = json.load(f)
            for name, mints in saved.items():
                if isinstance(mints, dict):
                    self.positions[name] = mints
        except Exception:
            pass

    def save_positions(self):
        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self.positions, f, indent=2)
        except Exception:
            pass

    def can_trade(self, crab_name):
        if crab_name not in self.keypairs:
            return False
        now = time.time()
        last = self.last_trade.get(crab_name, 0)
        cooldown = TRADE_COOLDOWN
        mod = self.crab_modifiers.get(crab_name)
        if mod and now < mod["expires"]:
            cooldown *= mod["cooldown_mult"]
        elif mod:
            del self.crab_modifiers[crab_name]
        return (now - last) >= cooldown

    def decide_and_trade(self, crab_name):
        """Decide whether to buy or sell $PINCHIN using evolved strategy."""
        if PINCHIN_CONTRACT in BLACKLISTED_TOKENS:
            return False
        if crab_name in BENCHED_CRABS:
            return False
        if self.community_controlled and self.community_controlled == crab_name:
            return False  # chat controls this crab, skip AI
        mint = PINCHIN_CONTRACT

        # Check if crab has a losing position — bypass cooldown for exits
        pos = self.positions.get(crab_name, {}).get(mint)
        price_data = self.price_feed.get(mint) if self.price_feed else None
        has_losing_pos = False
        if pos and pos.get("tokens", 0) > 0 and price_data and price_data["price"] > 0:
            avg = pos.get("avg_price", 0)
            if avg > 0 and price_data["price"] < avg * 0.95:
                has_losing_pos = True

        if not has_losing_pos and not self.can_trade(crab_name):
            return False

        if not price_data or price_data["price"] <= 0:
            return False

        if not self._evolved_strategy:
            return False
        trade_sol = TRADE_AMOUNT_SOL
        mod = self.crab_modifiers.get(crab_name)
        if mod and time.time() < mod["expires"]:
            trade_sol *= mod["trade_mult"]
        current_price = price_data["price"]
        try:
            return self._try_evolved_strategy(crab_name, mint, current_price, trade_sol, pos)
        except Exception:
            return False

    def check_snipe_exits(self):
        """Check all positions for TP/SL/timeout exits (fast cycle)."""
        try:
            now = time.time()
            for crab_name in list(self.keypairs.keys()):
                if crab_name in BENCHED_CRABS:
                    continue
                positions = self.positions.get(crab_name, {})
                for mint, pos in list(positions.items()):
                    if mint in BLACKLISTED_TOKENS:
                        continue
                    is_pinchin = (mint == PINCHIN_CONTRACT)
                    if pos.get("tokens", 0) <= 0:
                        # Stuck position: cost tracked but no tokens — check on-chain then clean up
                        if pos.get("cost_sol", 0) > 0 and not is_pinchin:
                            real_bal = self._check_token_balance_rpc(crab_name, mint)
                            if real_bal > 0:
                                pos["tokens"] = real_bal
                                _debug_log.info(f"  fixed stuck pos: {crab_name} {mint[:8]} → {real_bal} tokens")
                                self.save_positions()
                            else:
                                # Truly empty — zero out cost so it stops showing
                                _debug_log.info(f"  clearing dead pos: {crab_name} {mint[:8]} (0 tokens, {pos['cost_sol']:.3f} cost)")
                                pos["tokens"] = 0
                                pos["cost_sol"] = 0
                                pos["sell_fails"] = 0
                                self.save_positions()
                        continue
                    # Skip if a sell thread is already running for this pair
                    if (crab_name, mint) in self._selling:
                        continue

                    # Cooldown — scales with failures, but timed-out positions get urgency
                    # PINCHIN uses a shorter base cooldown (5s) since evolved strategy
                    # handles normal exits; this path is the emergency safety net
                    fails = pos.get("sell_fails", 0)
                    entry_time = pos.get("entry_time", 0)
                    past_timeout = (not is_pinchin and entry_time > 0
                                    and now - entry_time >= SNIPE_MAX_HOLD)
                    if past_timeout:
                        # Urgent: position overstayed — retry faster, cap at 30s
                        cooldown = min(10 + fails * 5, 30)
                    else:
                        base_cd = 5 if is_pinchin else 10
                        cooldown = min(base_cd + fails * 10, 120)
                    last = self.last_trade.get(crab_name, 0)
                    if now - last < cooldown:
                        continue

                    if not is_pinchin:
                        # Re-register mint in price feed if missing (e.g. after restart)
                        if self.price_feed and mint not in self.price_feed.extra_mints:
                            self.price_feed.extra_mints[mint] = mint[:8]

                        # Timeout check FIRST — fires regardless of price data
                        entry_time = pos.get("entry_time", 0)
                        if entry_time <= 0:
                            pos["entry_time"] = now
                            self.save_positions()
                        elif now - entry_time >= SNIPE_MAX_HOLD:
                            # Super-stale: if open >3x max hold AND repeated sell failures,
                            # the crab probably can't afford fees — force zero the position
                            age = now - entry_time
                            fails = pos.get("sell_fails", 0)
                            if age > SNIPE_MAX_HOLD * 3 and fails >= 5:
                                sol_bal = 0
                                if self.wallet_feed and crab_name in CRAB_WALLETS:
                                    sol_bal = self.wallet_feed.get_balance(CRAB_WALLETS[crab_name])
                                if sol_bal < 0.003:
                                    _debug_log.warning(f"  STALE FORCE-CLOSE {crab_name} {mint[:8]}: "
                                                       f"age={age/60:.0f}min fails={fails} bal={sol_bal:.4f}")
                                    # Check on-chain one last time
                                    real_bal = self._check_token_balance_rpc(crab_name, mint)
                                    if real_bal <= 0:
                                        # Already sold or worthless — clean up
                                        self._zero_position(crab_name, mint, "STALE_CLEANUP", "force", 0)
                                    else:
                                        # Still holding but can't sell — log it, keep trying
                                        _debug_log.warning(f"  STALE {crab_name} {mint[:8]}: still {real_bal} tokens, no SOL for fees")
                                    continue
                            self._sell_snipe(crab_name, mint, pos, "TIMEOUT")
                            continue

                    # Get current price from price feed
                    try:
                        price_data = self.price_feed.get(mint) if self.price_feed else None
                        if not price_data or price_data["price"] <= 0:
                            continue
                        current_price = price_data["price"]
                    except Exception:
                        continue
                    # Check entry price — backfill from current price if missing
                    entry_price = pos.get("avg_price", 0)
                    if entry_price <= 0:
                        pos["avg_price"] = current_price
                        self.save_positions()
                        continue
                    # PnL check
                    pnl_pct = (current_price - entry_price) / entry_price

                    if is_pinchin:
                        # PINCHIN is hold-only — no sells
                        pass
                    else:
                        # Use evolved strategy for smarter snipe exits
                        # Strategy decides WHEN to sell, but snipes always sell 100%
                        # (positions too small for tiered selling — fees eat partial sells)
                        sold = False
                        if self._evolved_strategy:
                            try:
                                sensors = self._build_sensors(crab_name, mint)
                                if sensors:
                                    decision = self._evolved_strategy.decide(sensors)
                                    action = decision.get("action", "hold")
                                    fraction = max(0.0, min(1.0, decision.get("amount_fraction", 0.0)))
                                    reason = decision.get("reason", "evolved")
                                    if action == "sell" and fraction > 0:
                                        tag = f"EVO_{reason[:16]}"
                                        _debug_log.info(f"  snipe evolved sell: {crab_name} {mint[:8]} reason={reason}")
                                        self._sell_snipe(crab_name, mint, pos, tag, fraction=1.0)
                                        sold = True
                            except Exception as e:
                                _debug_log.warning(f"  snipe evolved err: {e}")
                        # Hard safety nets if strategy didn't sell
                        if not sold:
                            if pnl_pct >= SNIPE_TP:
                                self._sell_snipe(crab_name, mint, pos, f"TP +{pnl_pct*100:.0f}%")
                            elif pnl_pct <= SNIPE_SL:
                                self._sell_snipe(crab_name, mint, pos, f"SL {pnl_pct*100:.0f}%")
        except Exception:
            pass
        # Update cached position display for the board (no locks needed in draw)
        try:
            display = []
            active = {}
            for cn in list(self.positions.keys()):
                cp = self.positions.get(cn, {})
                for mt in list(cp.keys()):
                    p = cp.get(mt, {})
                    if mt == PINCHIN_CONTRACT or p.get("tokens", 0) <= 0:
                        continue
                    if mt not in active:
                        tk = mt[:6]
                        if self.price_feed:
                            tk = self.price_feed.prices.get(mt, {}).get("symbol", mt[:6])
                        active[mt] = {"ticker": tk, "avg_price": p.get("avg_price", 0)}
            for mt, info in list(active.items())[:4]:
                pnl_str = "..."
                if self.price_feed:
                    cur = self.price_feed.prices.get(mt, {}).get("price_usd", 0)
                    entry = info["avg_price"]
                    if cur > 0 and entry > 0:
                        pnl = (cur - entry) / entry * 100
                        pnl_str = f"+{pnl:.0f}%" if pnl >= 0 else f"{pnl:.0f}%"
                display.append((f"${info['ticker'][:8]} {pnl_str}", pnl_str))
            self._pos_display = display
        except Exception:
            pass

    def _sell_snipe(self, crab_name, mint, pos, reason, fraction=1.0):
        """Sell snipe position (full or partial via fraction 0.0-1.0)."""
        tokens = pos.get("tokens", 0)
        if tokens <= 0:
            return
        sell_tokens = int(tokens * max(0.01, min(1.0, fraction)))
        if sell_tokens <= 0:
            return
        # Guard: only one sell thread per (crab, mint) at a time
        if (crab_name, mint) in self._selling:
            return
        self._selling.add((crab_name, mint))
        threading.Thread(
            target=self._execute_sell,
            args=(crab_name, mint, sell_tokens, f"SNIPE_{reason}"),
            daemon=True,
        ).start()

    def buy_initial_bags(self):
        """On startup, each crab buys an initial bag of $PINCHIN if they don't hold much on-chain."""
        if PINCHIN_CONTRACT in BLACKLISTED_TOKENS:
            _debug_log.info("buy_initial_bags: PINCHIN is blacklisted, skipping")
            return
        price_data = self.price_feed.get() if self.price_feed else None
        if not price_data or price_data["price"] <= 0:
            return
        current_price = price_data["price"]
        for crab_name in list(self.keypairs.keys()):
            # Check actual on-chain balance, not stale positions file
            on_chain = 0
            if self.wallet_feed and crab_name in CRAB_WALLETS:
                on_chain = self.wallet_feed.get_token_balance(CRAB_WALLETS[crab_name])
            if on_chain > 0:
                continue  # already has a bag on-chain
            t = threading.Thread(
                target=self._execute_buy,
                args=(crab_name, PINCHIN_CONTRACT, current_price, INITIAL_BAG_SOL),
                daemon=True
            )
            t.start()
            self.last_trade[crab_name] = time.time()
            time.sleep(2)  # stagger requests

    def _jup_headers(self):
        headers = {"User-Agent": "CrabSim/1.0"}
        if self.jup_api_key:
            headers["x-api-key"] = self.jup_api_key
        return headers

    def _pp_request(self, payload):
        """POST to PumpPortal local API. payload can be dict (single) or list (bundle)."""
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            PUMPPORTAL_API, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    def _jito_send_bundle(self, encoded_signed_txs):
        """Submit signed txs as a Jito bundle. Returns bundle_id or None."""
        _debug_log.info(f"  JITO: submitting {len(encoded_signed_txs)} txs")
        try:
            data = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "sendBundle",
                "params": [encoded_signed_txs],
            }).encode()
            req = urllib.request.Request(
                JITO_BUNDLE_URL, data=data,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            bid = result.get("result")
            if bid:
                _debug_log.info(f"  JITO: accepted {str(bid)[:16]}..")
            else:
                _debug_log.warning(f"  JITO: rejected {json.dumps(result)[:80]}")
            return bid
        except Exception as e:
            _debug_log.warning(f"  JITO: error {str(e)[:40]}")
            return None

    def _estimate_sol_received(self, crab_name, mint):
        """Estimate SOL received from a sell using price feed ratio to cost basis."""
        try:
            if self.price_feed:
                pd = self.price_feed.get(mint)
                with self._lock:
                    pos = self.positions.get(crab_name, {}).get(mint, {})
                    cost = pos.get("cost_sol", 0)
                    avg = pos.get("avg_price", 0)
                if avg > 0 and pd and pd.get("price", 0) > 0:
                    return cost * (pd["price"] / avg)
        except Exception:
            pass
        with self._lock:
            pos = self.positions.get(crab_name, {}).get(mint, {})
            return pos.get("cost_sol", 0)

    def _execute_buy(self, crab_name, mint, price_at_decision, sol_amount=None):
        """Buy via PumpPortal+Jito (primary) or Jupiter (fallback)."""
        if mint in BLACKLISTED_TOKENS:
            _debug_log.info(f"  BUY blocked: {mint[:8]} is blacklisted")
            return
        try:
            import base58 as b58
            from solders.transaction import VersionedTransaction

            sol_amount = sol_amount or TRADE_AMOUNT_SOL
            kp = self.keypairs[crab_name]
            pubkey = str(kp.pubkey())

            # Pre-check: does crab have enough SOL? (amount + fee buffer)
            min_needed = sol_amount + 0.002  # buy amount + fees/rent buffer
            wallet_addr = CRAB_WALLETS.get(crab_name, "")
            if wallet_addr and self.wallet_feed:
                crab_sol = self.wallet_feed.balances.get(wallet_addr, 0)
                if crab_sol < min_needed:
                    _debug_log.info(f"--- BUY {crab_name} SKIP: {crab_sol:.4f} SOL < {min_needed:.4f} needed ---")
                    return

            _debug_log.info(f"--- BUY {crab_name} ${APPROVED_TOKENS.get(mint, mint[:8])} {sol_amount} SOL ---")

            # --- PumpPortal + Jito primary path ---
            try:
                resp_body = self._pp_request({
                    "publicKey": pubkey,
                    "action": "buy",
                    "mint": mint,
                    "denominatedInSol": "true",
                    "amount": sol_amount,
                    "slippage": PP_BUY_SLIPPAGE,
                    "priorityFee": PP_BUY_TIP,
                    "pool": "auto",
                })
                # PumpPortal returns base58 tx — may be raw bytes or utf-8 string
                try:
                    encoded_tx = resp_body.decode("utf-8").strip().strip('"')
                except UnicodeDecodeError:
                    # Binary response — try base64 decode or pass raw bytes
                    encoded_tx = b58.b58encode(resp_body).decode()
                _debug_log.info(f"  PP tx ready ({len(encoded_tx)} chars)")
                raw = b58.b58decode(encoded_tx)
                unsigned = VersionedTransaction.from_bytes(raw)
                signed = VersionedTransaction(unsigned.message, [kp])
                encoded_signed = b58.b58encode(bytes(signed)).decode()
                tx_sig = str(signed.signatures[0])

                # Send directly via RPC (Jito consistently 429'd — skip it)
                is_pump = mint.endswith("pump")
                if is_pump:
                    rpc_sig = self._send_tx(signed)
                    if rpc_sig:
                        tx_sig = rpc_sig
                    else:
                        raise Exception("rpc send failed")
                else:
                    # Non-pump tokens: try Jupiter instead (better routing)
                    raise Exception("non-pump, use jupiter")

                _debug_log.info(f"  sent: {tx_sig[:16]}.. confirming...")
                confirmed = self._confirm_tx(tx_sig, timeout=12)
                if confirmed is False:
                    is_pump = mint.endswith("pump")
                    if not is_pump:
                        _debug_log.info("  PP tx failed on-chain, trying Jupiter")
                        raise Exception("pp tx on-chain err, use jupiter")
                    else:
                        _debug_log.info("  PP tx failed on-chain (pump.fun)")
                        return

                # Get actual on-chain token balance to verify tx landed
                time.sleep(1)
                token_bal = self._check_token_balance_rpc(crab_name, mint)
                old_tokens = self.positions.get(crab_name, {}).get(mint, {}).get("tokens", 0)
                _debug_log.info(f"  confirmed={confirmed} tokens={token_bal} (was {old_tokens})")

                # Only track cost if balance actually increased
                if token_bal <= 0 or token_bal <= old_tokens:
                    _debug_log.warning(f"  buy didn't land: balance unchanged ({old_tokens} -> {token_bal})")
                    self._log_trade(crab_name, mint, "BUY_FAIL", f"no balance change ({tx_sig[:16]})")
                    return

                with self._lock:
                    if crab_name not in self.positions:
                        self.positions[crab_name] = {}
                    pos = self.positions[crab_name].get(mint, {"tokens": 0, "cost_sol": 0.0, "avg_price": 0.0})
                    pos["tokens"] = token_bal
                    pos["cost_sol"] += sol_amount
                    pos["avg_price"] = price_at_decision
                    self.positions[crab_name][mint] = pos
                self._log_trade(crab_name, mint, "BUY", tx_sig, sol_amount)
                self.save_positions()
                return

            except Exception as pp_err:
                self._log_trade(crab_name, mint, "BUY_INFO", f"PP:{str(pp_err)[:25]},trying Jup")

            # --- Jupiter fallback ---
            lamports = int(sol_amount * 1_000_000_000)
            headers = self._jup_headers()
            quote_url = (
                f"{JUPITER_QUOTE_URL}"
                f"?inputMint={WSOL_MINT}&outputMint={mint}"
                f"&amount={lamports}&slippageBps=250"
            )
            req = urllib.request.Request(quote_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                quote = json.loads(resp.read())

            out_amount = int(quote.get("outAmount", 0))
            swap_headers = dict(headers)
            swap_headers["Content-Type"] = "application/json"
            swap_payload = json.dumps({
                "quoteResponse": quote,
                "userPublicKey": pubkey,
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": "auto",
            }).encode()
            req2 = urllib.request.Request(JUPITER_SWAP_URL, data=swap_payload, headers=swap_headers)
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                swap_data = json.loads(resp2.read())

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                self._log_trade(crab_name, mint, "BUY_FAIL", "no swap tx (jup)")
                return

            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [kp])
            tx_sig = self._send_tx(signed_tx)

            if tx_sig:
                confirmed = self._confirm_tx(tx_sig, timeout=8)
                if confirmed is False:
                    self._log_trade(crab_name, mint, "BUY_FAIL", "on-chain err (jup)")
                else:
                    # Verify balance actually changed before tracking cost
                    time.sleep(1)
                    token_bal = self._check_token_balance_rpc(crab_name, mint)
                    old_tokens = self.positions.get(crab_name, {}).get(mint, {}).get("tokens", 0)
                    if token_bal <= 0 or token_bal <= old_tokens:
                        _debug_log.warning(f"  jup buy didn't land: balance unchanged ({old_tokens} -> {token_bal})")
                        self._log_trade(crab_name, mint, "BUY_FAIL", f"no balance change (jup)")
                    else:
                        with self._lock:
                            if crab_name not in self.positions:
                                self.positions[crab_name] = {}
                            pos = self.positions[crab_name].get(mint, {"tokens": 0, "cost_sol": 0.0, "avg_price": 0.0})
                            prev_tokens = pos["tokens"]
                            pos["tokens"] = token_bal
                            pos["cost_sol"] += sol_amount
                            if prev_tokens == 0:
                                pos["avg_price"] = price_at_decision
                            else:
                                pos["avg_price"] = (prev_tokens * pos["avg_price"] + (token_bal - prev_tokens) * price_at_decision) / token_bal
                            self.positions[crab_name][mint] = pos
                        self._log_trade(crab_name, mint, "BUY", tx_sig, sol_amount)
                        self.save_positions()
            else:
                self._log_trade(crab_name, mint, "BUY_FAIL", "no sig (jup)")

        except Exception as e:
            self._log_trade(crab_name, mint, "BUY_FAIL", f"err: {str(e)[:40]}")

    def _execute_sell(self, crab_name, mint, token_amount, reason="SELL"):
        """Sell via PumpPortal (primary) or Jupiter (fallback). 5 retries with escalating priority fees."""
        if mint in BLACKLISTED_TOKENS:
            _debug_log.info(f"  SELL blocked: {mint[:8]} is blacklisted")
            return
        try:
            import base58 as b58
            from solders.transaction import VersionedTransaction

            kp = self.keypairs[crab_name]
            pubkey = str(kp.pubkey())
            sol_received = 0
            ticker = APPROVED_TOKENS.get(mint, mint[:8])
            _debug_log.info(f"--- SELL {crab_name} ${ticker} {reason} | {token_amount} tokens | {SELL_MAX_RETRIES} retries ---")

            # Pre-flight: check SOL balance — need enough for tx fees
            if self.wallet_feed and crab_name in CRAB_WALLETS:
                sol_bal = self.wallet_feed.get_balance(CRAB_WALLETS[crab_name])
                if sol_bal < 0.003:
                    _debug_log.warning(f"  SELL SKIP {crab_name}: only {sol_bal:.4f} SOL, need ~0.003 for fees")
                    with self._lock:
                        pos = self.positions.get(crab_name, {}).get(mint)
                        if pos:
                            pos["sell_fails"] = pos.get("sell_fails", 0) + 1
                            self.save_positions()
                    return

            # Fixed 500 bps (5%) exit slippage for all sells
            pp_slip = PP_SELL_SLIPPAGE
            jup_slip_bps = pp_slip * 100  # 500

            for attempt in range(SELL_MAX_RETRIES):
                # Priority fee escalates: auto, 50K, 100K, 500K, 1M lamports
                jup_priority = SELL_PRIORITY_LAMPORTS[min(attempt, len(SELL_PRIORITY_LAMPORTS) - 1)]
                pp_tip = PP_SELL_TIP[min(attempt, len(PP_SELL_TIP) - 1)]

                fee_label = f"{jup_priority}" if jup_priority == "auto" else f"{jup_priority:,} lamports"
                _debug_log.info(f"  SELL attempt {attempt+1}/{SELL_MAX_RETRIES} | slip={jup_slip_bps}bps | priority={fee_label}")

                try:
                    tx_sig = None
                    path_used = "none"

                    # --- PumpPortal primary path (pump.fun tokens only) ---
                    is_pump = mint.endswith("pump")
                    if is_pump:
                        try:
                            resp_body = self._pp_request({
                                "publicKey": pubkey,
                                "action": "sell",
                                "mint": mint,
                                "denominatedInSol": "false",
                                "amount": str(token_amount),
                                "slippage": pp_slip,
                                "priorityFee": pp_tip,
                                "pool": "auto",
                            })
                            try:
                                encoded_tx = resp_body.decode("utf-8").strip().strip('"')
                            except UnicodeDecodeError:
                                encoded_tx = b58.b58encode(resp_body).decode()
                            raw = b58.b58decode(encoded_tx)
                            unsigned = VersionedTransaction.from_bytes(raw)
                            signed = VersionedTransaction(unsigned.message, [kp])
                            rpc_sig = self._send_tx(signed, skip_preflight=True)
                            if rpc_sig:
                                tx_sig = rpc_sig
                                path_used = "PumpPortal"
                                _debug_log.info(f"    PP sent: {tx_sig[:16]}...")
                            else:
                                _debug_log.info(f"    PP send returned no sig, falling through to Jupiter")
                        except Exception as pp_err:
                            _debug_log.info(f"    PP failed: {str(pp_err)[:40]}, falling through to Jupiter")
                            tx_sig = None

                    # --- Jupiter fallback (or primary for non-pump tokens) ---
                    if not tx_sig:
                        headers = self._jup_headers()
                        quote_url = (
                            f"{JUPITER_QUOTE_URL}"
                            f"?inputMint={mint}&outputMint={WSOL_MINT}"
                            f"&amount={token_amount}&slippageBps={jup_slip_bps}"
                        )
                        req = urllib.request.Request(quote_url, headers=headers)
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            quote = json.loads(resp.read())

                        out_lamports = int(quote.get("outAmount", 0))
                        if out_lamports <= 0:
                            _debug_log.warning(f"    Jupiter: no route (try {attempt+1})")
                            self._log_trade(crab_name, mint, "SELL_FAIL", f"no route (try {attempt+1})")
                            time.sleep(3)
                            continue
                        sol_received = out_lamports / 1_000_000_000
                        _debug_log.info(f"    Jupiter quote: ~{sol_received:.4f} SOL")

                        swap_headers = dict(headers)
                        swap_headers["Content-Type"] = "application/json"
                        swap_payload = json.dumps({
                            "quoteResponse": quote,
                            "userPublicKey": pubkey,
                            "wrapAndUnwrapSol": True,
                            "prioritizationFeeLamports": jup_priority,
                        }).encode()
                        req2 = urllib.request.Request(JUPITER_SWAP_URL, data=swap_payload, headers=swap_headers)
                        with urllib.request.urlopen(req2, timeout=15) as resp2:
                            swap_data = json.loads(resp2.read())

                        swap_tx_b64 = swap_data.get("swapTransaction")
                        if not swap_tx_b64:
                            _debug_log.warning(f"    Jupiter: no swap tx (try {attempt+1})")
                            self._log_trade(crab_name, mint, "SELL_FAIL", f"no swap tx (try {attempt+1})")
                            time.sleep(3)
                            continue

                        raw_tx = base64.b64decode(swap_tx_b64)
                        tx = VersionedTransaction.from_bytes(raw_tx)
                        signed_tx = VersionedTransaction(tx.message, [kp])
                        tx_sig = self._send_tx(signed_tx, skip_preflight=True)
                        path_used = "Jupiter"
                        if tx_sig:
                            _debug_log.info(f"    Jupiter sent: {tx_sig[:16]}...")
                        else:
                            _debug_log.warning(f"    Jupiter send returned no sig (try {attempt+1})")

                    # --- Confirmation (same for both paths) ---
                    if tx_sig:
                        _debug_log.info(f"    Confirming via {path_used}... (20s timeout)")
                        confirmed = self._confirm_tx(tx_sig, timeout=20)
                        if confirmed:
                            _debug_log.info(f"    CONFIRMED on attempt {attempt+1} via {path_used}")
                            if sol_received <= 0:
                                sol_received = self._estimate_sol_received(crab_name, mint)
                            time.sleep(1)
                            remaining = self._check_token_balance_rpc(crab_name, mint)
                            if remaining > 0:
                                self._reduce_position(crab_name, mint, reason, tx_sig, sol_received, token_amount)
                            else:
                                self._zero_position(crab_name, mint, reason, tx_sig, sol_received)
                            return
                        elif confirmed is False:
                            _debug_log.warning(f"    On-chain error (try {attempt+1})")
                            self._log_trade(crab_name, mint, "SELL_FAIL", f"on-chain err (try {attempt+1})")
                            time.sleep(3)
                            continue
                        # Unknown — verify on-chain
                        _debug_log.info(f"    Timeout — checking on-chain balance...")
                        time.sleep(2)
                        bal = self._check_token_balance_rpc(crab_name, mint)
                        if bal == 0:
                            _debug_log.info(f"    Verified sold (balance=0) on attempt {attempt+1}")
                            if sol_received <= 0:
                                sol_received = self._estimate_sol_received(crab_name, mint)
                            self._zero_position(crab_name, mint, reason, tx_sig, sol_received)
                            return
                        elif bal > 0:
                            orig_tokens = 0
                            with self._lock:
                                pos = self.positions.get(crab_name, {}).get(mint)
                                if pos:
                                    orig_tokens = pos.get("tokens", 0)
                            if orig_tokens > 0 and bal < orig_tokens:
                                _debug_log.info(f"    Partial sell landed: {orig_tokens} -> {bal}")
                                if sol_received <= 0:
                                    sol_received = self._estimate_sol_received(crab_name, mint)
                                self._reduce_position(crab_name, mint, reason, tx_sig, sol_received, orig_tokens - bal)
                                return
                            _debug_log.warning(f"    Still holding {bal} tokens (try {attempt+1})")
                            self._log_trade(crab_name, mint, "SELL_FAIL", f"still holding (try {attempt+1})")
                            token_amount = bal
                            time.sleep(3)
                            continue
                        _debug_log.warning(f"    Balance verify error (try {attempt+1})")
                        self._log_trade(crab_name, mint, "SELL_FAIL", f"verify err (try {attempt+1})")
                        time.sleep(3)
                        continue
                    else:
                        self._log_trade(crab_name, mint, "SELL_FAIL", f"no sig (try {attempt+1})")
                        time.sleep(3)
                        continue

                except Exception as e:
                    _debug_log.error(f"    Exception on attempt {attempt+1}: {str(e)[:60]}")
                    self._log_trade(crab_name, mint, "SELL_FAIL", f"{str(e)[:30]} (try {attempt+1})")
                    time.sleep(3)

            # All retries exhausted — final on-chain balance check
            _debug_log.warning(f"  All {SELL_MAX_RETRIES} sell attempts failed for {crab_name} ${ticker}")
            time.sleep(3)
            bal = self._check_token_balance_rpc(crab_name, mint)
            if bal == 0:
                if sol_received <= 0:
                    sol_received = self._estimate_sol_received(crab_name, mint)
                self._zero_position(crab_name, mint, reason, "verified", sol_received)
                return
            elif bal > 0:
                with self._lock:
                    pos = self.positions.get(crab_name, {}).get(mint)
                    if pos:
                        pos["tokens"] = bal
                        pos["sell_fails"] = pos.get("sell_fails", 0) + 1
                        self.save_positions()
                self._log_trade(crab_name, mint, "SELL_RETRY", f"still {bal} tokens after {SELL_MAX_RETRIES} tries")
            else:
                with self._lock:
                    pos = self.positions.get(crab_name, {}).get(mint)
                    if pos:
                        pos["sell_fails"] = pos.get("sell_fails", 0) + 1
                        self.save_positions()
                self._log_trade(crab_name, mint, "SELL_RETRY", "verify failed, will retry")
        finally:
            self._selling.discard((crab_name, mint))
            self.last_trade[crab_name] = time.time()

    def _zero_position(self, crab_name, mint, reason, tx_sig, sol_received):
        """Zero out a position after confirmed sell."""
        cost_sol = 0.0
        with self._lock:
            pos = self.positions.get(crab_name, {}).get(mint)
            if pos:
                cost_sol = pos.get("cost_sol", 0.0)
                # Delete the entry entirely instead of zeroing
                del self.positions[crab_name][mint]
            # Clean up extra_mints if no crab holds this token anymore
            still_held = any(
                self.positions.get(cn, {}).get(mint, {}).get("tokens", 0) > 0
                for cn in self.positions
            )
            if not still_held and self.price_feed and mint in self.price_feed.extra_mints:
                del self.price_feed.extra_mints[mint]
        self._log_trade(crab_name, mint, reason, tx_sig, sol_received, cost_sol=cost_sol)
        self.save_positions()

    def _reduce_position(self, crab_name, mint, reason, tx_sig, sol_received, tokens_sold):
        """Reduce a position after a partial sell (keep remaining tokens tracked)."""
        with self._lock:
            pos = self.positions.get(crab_name, {}).get(mint)
            if not pos or pos.get("tokens", 0) <= 0:
                return
            old_tokens = pos["tokens"]
            old_cost = pos.get("cost_sol", 0.0)
            fraction_sold = min(1.0, tokens_sold / old_tokens) if old_tokens > 0 else 1.0
            cost_portion = old_cost * fraction_sold
            pos["tokens"] = max(0, old_tokens - tokens_sold)
            pos["cost_sol"] = max(0.0, old_cost - cost_portion)
            pos["sell_fails"] = 0
            _debug_log.info(f"  partial sell: {crab_name} {mint[:8]} sold {fraction_sold*100:.0f}%, {pos['tokens']} tokens remain, cost {pos['cost_sol']:.4f}")
        self._log_trade(crab_name, mint, reason, tx_sig, sol_received, cost_sol=cost_portion)
        self.save_positions()
        # Reclaim ~0.002 SOL rent from empty token account
        if mint != PINCHIN_CONTRACT:
            try:
                self._close_token_account(crab_name, mint)
            except Exception:
                pass

    def _close_token_account(self, crab_name, mint):
        """Close empty token account to reclaim ~0.002 SOL rent. Best-effort."""
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash

        kp = self.keypairs[crab_name]
        owner = kp.pubkey()
        mint_pk = Pubkey.from_string(mint)
        ata_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

        # Try standard SPL Token first, then Token-2022
        token_programs = [
            Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
            Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
        ]

        for token_program in token_programs:
            try:
                seeds = [bytes(owner), bytes(token_program), bytes(mint_pk)]
                ata, _bump = Pubkey.find_program_address(seeds, ata_program)

                # Check if this ATA exists on-chain
                check_payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAccountInfo",
                    "params": [str(ata), {"encoding": "base64"}]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=check_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    acct_data = json.loads(resp.read())

                if acct_data.get("result", {}).get("value") is None:
                    continue  # ATA doesn't exist for this token program

                # CloseAccount instruction (index 9): [account, destination, authority]
                close_ix = Instruction(
                    token_program,
                    bytes([9]),
                    [
                        AccountMeta(ata, is_signer=False, is_writable=True),
                        AccountMeta(owner, is_signer=False, is_writable=True),
                        AccountMeta(owner, is_signer=True, is_writable=False),
                    ],
                )

                # Get recent blockhash
                bh_payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getLatestBlockhash",
                    "params": [{"commitment": "finalized"}]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=bh_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    bh_data = json.loads(resp.read())
                blockhash = Hash.from_string(bh_data["result"]["value"]["blockhash"])

                msg = Message.new_with_blockhash([close_ix], owner, blockhash)
                tx = Transaction.new_unsigned(msg)
                tx.sign([kp], blockhash)
                sig = self._send_tx(tx)
                if sig:
                    self._log_trade(crab_name, mint, "ATA_CLOSE", f"rent reclaimed")
                return
            except Exception:
                continue

    def _send_tx(self, signed_tx, skip_preflight=False):
        tx_b64 = base64.b64encode(bytes(signed_tx)).decode()
        send_payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": skip_preflight}]
        }).encode()
        req = urllib.request.Request(
            WRITE_RPC, data=send_payload,
            headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return result.get("result", "")

    def _confirm_tx(self, tx_sig, timeout=8):
        """Poll for tx confirmation. Returns True=confirmed, False=failed on-chain, None=unknown."""
        if not tx_sig:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[tx_sig], {"searchTransactionHistory": False}]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read())
                statuses = result.get("result", {}).get("value", [])
                if statuses and statuses[0]:
                    status = statuses[0]
                    if status.get("err"):
                        return False  # tx failed on-chain
                    conf = status.get("confirmationStatus", "")
                    if conf in ("confirmed", "finalized"):
                        return True
            except Exception:
                pass
            time.sleep(3)
        return None  # timed out — unknown status

    def consolidate_sol(self):
        """One-shot: sweep SOL from BENCHED_CRABS to Pinchy and Mr.Krabs (alternating)."""
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash

        destinations = [
            ("Pinchy", Pubkey.from_string(CRAB_WALLETS["Pinchy"])),
            ("Mr.Krabs", Pubkey.from_string(CRAB_WALLETS["Mr.Krabs"])),
        ]
        dest_idx = 0

        for crab_name in sorted(BENCHED_CRABS):
            kp = self.keypairs.get(crab_name)
            if not kp:
                continue
            wallet = CRAB_WALLETS.get(crab_name, "")
            if not wallet:
                continue

            # Query on-chain SOL balance
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getBalance",
                    "params": [wallet]
                }).encode()
                req = urllib.request.Request(
                    READ_RPC, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                lamports = data.get("result", {}).get("value", 0)
                sol_bal = lamports / 1e9
            except Exception as e:
                print(f"  [CONSOLIDATE] {crab_name}: balance check failed — {e}")
                _debug_log.warning(f"consolidate: {crab_name} balance check failed: {e}")
                continue

            if sol_bal < 0.001:
                print(f"  [CONSOLIDATE] {crab_name}: {sol_bal:.6f} SOL — too low, skipping")
                _debug_log.info(f"consolidate: {crab_name} has {sol_bal:.6f} SOL, skipping (< 0.001)")
                continue

            # Leave 5000 lamports for the tx fee
            send_lamports = lamports - 5000
            if send_lamports <= 0:
                continue

            dest_name, dest_pubkey = destinations[dest_idx % 2]
            dest_idx += 1

            try:
                # Get recent blockhash
                bh_payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getLatestBlockhash",
                    "params": [{"commitment": "finalized"}]
                }).encode()
                bh_req = urllib.request.Request(
                    WRITE_RPC, data=bh_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                )
                with urllib.request.urlopen(bh_req, timeout=10) as resp:
                    bh_data = json.loads(resp.read())
                blockhash = Hash.from_string(bh_data["result"]["value"]["blockhash"])

                # Build transfer instruction
                ix = transfer(TransferParams(
                    from_pubkey=kp.pubkey(),
                    to_pubkey=dest_pubkey,
                    lamports=send_lamports,
                ))
                msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
                tx = Transaction.new_unsigned(msg)
                tx.sign([kp], blockhash)

                sig = self._send_tx(tx)
                if sig:
                    confirmed = self._confirm_tx(sig, timeout=12)
                    sol_sent = send_lamports / 1e9
                    if confirmed:
                        print(f"  [CONSOLIDATE] {crab_name} → {dest_name}: {sol_sent:.6f} SOL ✓ ({sig[:16]}...)")
                        _debug_log.info(f"consolidate: {crab_name} → {dest_name} {sol_sent:.6f} SOL confirmed sig={sig}")
                    else:
                        print(f"  [CONSOLIDATE] {crab_name} → {dest_name}: {sol_sent:.6f} SOL sent (unconfirmed) ({sig[:16]}...)")
                        _debug_log.warning(f"consolidate: {crab_name} → {dest_name} {sol_sent:.6f} SOL unconfirmed sig={sig}")
                else:
                    print(f"  [CONSOLIDATE] {crab_name} → {dest_name}: send failed")
                    _debug_log.warning(f"consolidate: {crab_name} → {dest_name} send failed")
            except Exception as e:
                print(f"  [CONSOLIDATE] {crab_name} → {dest_name}: error — {e}")
                _debug_log.error(f"consolidate: {crab_name} → {dest_name} error: {e}")

    def _check_token_balance_rpc(self, crab_name, mint):
        """Check actual on-chain token balance for a crab+mint. Returns raw amount, 0 if empty, -1 on error."""
        wallet_addr = CRAB_WALLETS.get(crab_name, "")
        if not wallet_addr:
            return -1
        try:
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [wallet_addr, {"mint": mint}, {"encoding": "jsonParsed"}]
            }).encode()
            req = urllib.request.Request(
                READ_RPC, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return 0
            return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        except Exception:
            return -1

    def _execute_burn(self, crab_name, mint, pct=0.10):
        """Burn a percentage of a crab's token holdings using SPL Token Burn."""
        try:
            from solders.keypair import Keypair as _Kp
            from solders.pubkey import Pubkey
            from solders.transaction import Transaction
            from solders.instruction import Instruction, AccountMeta
            from solders.hash import Hash
            from solders.message import Message
            import struct

            kp = self.keypairs.get(crab_name)
            if not kp:
                self._log_trade(crab_name, mint, "BURN_FAIL", "no keypair")
                return
            owner = kp.pubkey()

            # Get actual on-chain token balance (raw amount)
            wallet_addr = CRAB_WALLETS.get(crab_name, "")
            tok_payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [wallet_addr, {"mint": mint}, {"encoding": "jsonParsed"}]
            }).encode()
            tok_req = urllib.request.Request(
                READ_RPC, data=tok_payload,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(tok_req, timeout=10) as tok_resp:
                tok_data = json.loads(tok_resp.read())
            tok_accounts = tok_data.get("result", {}).get("value", [])
            if not tok_accounts:
                self._log_trade(crab_name, mint, "BURN_FAIL", "no token acct")
                return
            raw_balance = int(tok_accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            if raw_balance <= 0:
                self._log_trade(crab_name, mint, "BURN_FAIL", "no tokens")
                return

            burn_amount = int(raw_balance * pct)
            if burn_amount <= 0:
                self._log_trade(crab_name, mint, "BURN_FAIL", "amount=0")
                return

            mint_pubkey = Pubkey.from_string(mint)
            # PINCHIN is Token-2022
            token_program = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

            # Derive ATA: seeds = [owner, TOKEN_PROGRAM_2022, mint], program = ATA_PROGRAM
            ata_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
            seeds = [bytes(owner), bytes(token_program), bytes(mint_pubkey)]
            ata, _bump = Pubkey.find_program_address(seeds, ata_program)

            # SPL Token Burn instruction (index 8): u8(8) + u64(amount)
            data = struct.pack("<BQ", 8, burn_amount)
            burn_ix = Instruction(
                token_program,
                data,
                [
                    AccountMeta(ata, is_signer=False, is_writable=True),       # token account
                    AccountMeta(mint_pubkey, is_signer=False, is_writable=True), # mint
                    AccountMeta(owner, is_signer=True, is_writable=False),      # authority
                ],
            )

            # Get recent blockhash
            bh_payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
            }).encode()
            req = urllib.request.Request(
                READ_RPC, data=bh_payload,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                bh_data = json.loads(resp.read())
            blockhash_str = bh_data["result"]["value"]["blockhash"]
            recent_blockhash = Hash.from_string(blockhash_str)

            # Build, sign, send
            msg = Message.new_with_blockhash([burn_ix], owner, recent_blockhash)
            tx = Transaction.new_unsigned(msg)
            tx.sign([kp], recent_blockhash)

            tx_b64 = base64.b64encode(bytes(tx)).decode()
            send_payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [tx_b64, {"encoding": "base64", "skipPreflight": True}]
            }).encode()
            req2 = urllib.request.Request(
                WRITE_RPC, data=send_payload,
                headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
            )
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                result = json.loads(resp2.read())
            tx_sig = result.get("result", "")

            # Update position
            if tx_sig:
                with self._lock:
                    pos = self.positions.get(crab_name, {}).get(mint)
                    if pos:
                        pos["tokens"] = max(0, pos["tokens"] - burn_amount)

            self._log_trade(crab_name, mint, "BURN", tx_sig or "failed", 0)
            self.save_positions()

        except Exception as e:
            self._log_trade(crab_name, mint, "BURN_FAIL", f"err: {str(e)[:40]}")

    def _log_trade(self, crab_name, mint, action, tx_msg, sol=0, cost_sol=0.0):
        token_name = APPROVED_TOKENS.get(mint, mint[:8])
        # Clean log line
        if "FAIL" in action or "RETRY" in action:
            lvl = _debug_log.warning
        else:
            lvl = _debug_log.info
        sol_str = f"{sol:.4f}" if sol else ""
        pnl_str = f" ({sol - cost_sol:+.4f})" if cost_sol > 0 else ""
        tx_short = tx_msg[:16] if tx_msg else ""
        lvl(f"{action:12s} {crab_name:10s} ${token_name:8s} {sol_str:>8s}{pnl_str} {tx_short}")
        entry = {
            "crab": crab_name,
            "action": action,
            "token": token_name,
            "mint": mint,
            "sol": sol,
            "tx": tx_msg[:16] if tx_msg else "",
            "time": time.time(),
        }
        with self._lock:
            self.trade_log.append(entry)
            if len(self.trade_log) > 20:
                self.trade_log = self.trade_log[-20:]
        # Persist to disk
        try:
            log_path = os.path.expanduser("~/.pinchin_trade_history.jsonl")
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        # Feed generation tracker (BUY/SELL/SNIPE_ only)
        if self.gen_tracker and (action == "BUY" or action in ("SELL",) or (action.startswith("SNIPE_") and "FAIL" not in action and "RETRY" not in action)):
            try:
                self.gen_tracker.record_trade(crab_name, action, sol, token_name, cost_sol=cost_sol)
            except Exception:
                pass
        # Win flash overlay — trigger on profitable sells
        if (action.startswith("SELL") or action.startswith("SNIPE_")) and cost_sol > 0 and sol > cost_sol:
            pnl_pct = (sol - cost_sol) / cost_sol * 100
            pnl_sol = sol - cost_sol
            self.win_flash = {
                "text": f"+{pnl_pct:.0f}%",
                "sub": f"{crab_name} +{pnl_sol:.3f} SOL",
                "ticks_left": 40,  # ~5 seconds at 8 tps
            }
            # Trigger big coin rain effect
            if _screen_effects:
                _screen_effects.trigger_win(f"W I N  +{pnl_pct:.0f}%", f"{crab_name} +{pnl_sol:.3f} SOL")
            # CrabBrain reactive tweet on wins
            if hasattr(self, "crab_brain") and self.crab_brain:
                self.crab_brain.react("win", f"{crab_name} +{pnl_pct:.0f}% (+{pnl_sol:.3f} SOL) on ${token_name}")
        # CrabBrain reactive tweet on burns (only successful ones)
        # Burns don't auto-tweet — manual via !tweet only
        # Post to pump.fun chat
        if self.chat_poster and (action == "BUY" or action.startswith("SELL") or action.startswith("SNIPE_")):
            try:
                if action == "BUY":
                    tmpl = random.choice(KRABS_CHAT_TEMPLATES["buy"])
                    self.chat_poster.post(tmpl.format(crab=crab_name, token=token_name))
                else:
                    sign = "+" if sol >= 0 else ""
                    pnl = f"{sign}{sol:.3f}"
                    tmpl = random.choice(KRABS_CHAT_TEMPLATES["sell"])
                    self.chat_poster.post(tmpl.format(crab=crab_name, token=token_name, pnl=pnl))
            except Exception:
                pass

    def get_trade_log(self):
        with self._lock:
            return list(self.trade_log)


# --- Crab World Location System ---
WORLD_FILE = os.path.expanduser("~/.pinchin_world.json")
LOCATIONS = ("beach", "lab", "graveyard", "bank", "holders")
LOCATION_ALIASES = {
    "beach": "beach", "shore": "beach", "home": "beach", "b": "beach",
    "lab": "lab", "evolve": "lab", "evolution": "lab", "l": "lab",
    "graveyard": "graveyard", "grave": "graveyard", "gy": "graveyard", "rip": "graveyard", "g": "graveyard",
    "bank": "bank", "portfolio": "bank", "vault": "bank", "k": "bank",
    "holders": "holders", "holder": "holders", "top": "holders", "bags": "holders", "h": "holders",
}
CRAB_NAME_SHORTCUTS = {
    "krabs": "Mr.Krabs", "mrk": "Mr.Krabs", "mr.krabs": "Mr.Krabs",
    "pinchy": "Pinchy", "pin": "Pinchy",
    "clawdia": "Clawdia", "claw": "Clawdia",
    "sandy": "Sandy", "san": "Sandy",
    "snippy": "Snippy", "snip": "Snippy",
    "hermie": "Hermie", "herm": "Hermie",
    "bastian": "Bastian", "bast": "Bastian", "bas": "Bastian",
}


class CrabWorld:
    """Tracks crab locations, camera position, and the graveyard of fallen strategies."""

    def __init__(self):
        self.camera = "beach"
        self.crab_locations = {name: "beach" for name in CRAB_WALLETS}
        self.graveyard = []  # list of tombstone dicts
        self._lock = threading.Lock()
        self._load()

    def crabs_at(self, location):
        """Return list of crab names at the given location."""
        with self._lock:
            return [n for n, loc in self.crab_locations.items() if loc == location]

    def crabs_elsewhere(self, location):
        """Return dict of {name: location} for crabs NOT at the given location."""
        with self._lock:
            return {n: loc for n, loc in self.crab_locations.items() if loc != location}

    def move_crab(self, name, location):
        """Move a single crab to a location."""
        with self._lock:
            if name in self.crab_locations and location in LOCATIONS:
                self.crab_locations[name] = location
                self._save()
                return True
        return False

    def move_all(self, location):
        """Move all crabs to a location."""
        with self._lock:
            if location in LOCATIONS:
                for name in self.crab_locations:
                    self.crab_locations[name] = location
                self._save()

    def bury_strategy(self, crab_name, gen_num, fitness_dict, killer_name):
        """Add a tombstone to the graveyard when evolution kills a strategy."""
        cause = generate_cause_of_death(crab_name, fitness_dict)
        tombstone = {
            "name": crab_name,
            "gen": gen_num,
            "wins": fitness_dict.get("wins", 0),
            "losses": fitness_dict.get("losses", 0),
            "pnl": fitness_dict.get("pnl", 0.0),
            "sharpe": fitness_dict.get("sharpe", 0.0),
            "cause": cause,
            "killer": killer_name,
            "time": time.time(),
        }
        with self._lock:
            self.graveyard.append(tombstone)
            if len(self.graveyard) > 50:
                self.graveyard = self.graveyard[-50:]
            self._save()

    def _save(self):
        try:
            data = {
                "camera": self.camera,
                "crab_locations": self.crab_locations,
                "graveyard": self.graveyard,
            }
            with open(WORLD_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load(self):
        if not os.path.exists(WORLD_FILE):
            return
        try:
            with open(WORLD_FILE) as f:
                data = json.load(f)
            self.camera = data.get("camera", "beach")
            if self.camera not in LOCATIONS:
                self.camera = "beach"
            saved_locs = data.get("crab_locations", {})
            for name in CRAB_WALLETS:
                loc = saved_locs.get(name, "beach")
                self.crab_locations[name] = loc if loc in LOCATIONS else "beach"
            self.graveyard = data.get("graveyard", [])
        except Exception:
            pass


def generate_cause_of_death(crab_name, fitness_dict):
    """Auto-label tombstones with a cause of death based on stats."""
    pnl = fitness_dict.get("pnl", 0.0)
    wins = fitness_dict.get("wins", 0)
    losses = fitness_dict.get("losses", 0)
    sharpe = fitness_dict.get("sharpe", 0.0)
    total = wins + losses

    if total == 0:
        return "never traded"
    if losses > 0 and wins == 0:
        return "0 wins, pure loss"
    if sharpe < -1.0:
        return f"Sharpe {sharpe:.1f} -- catastrophic"
    if pnl < -0.1:
        return f"bled {pnl:.3f} SOL"
    if total > 0 and wins / max(total, 1) < 0.2:
        return f"only {wins}W/{losses}L"
    if sharpe < 0:
        return f"negative Sharpe ({sharpe:.2f})"
    return "outperformed by peers"


LOCATION_DIALOGUE = {
    "beach": {
        "winning": [
            "life's a beach when you're up!",
            "waves are good, portfolio's better",
            "soaking up these gains",
            "sunshine and green candles",
        ],
        "losing": [
            "at least the view is nice...",
            "the ocean doesn't judge my trades",
            "building sandcastles of cope",
            "maybe tomorrow...",
        ],
        "neutral": [
            "just vibing",
            "watching the waves",
            "crabs don't rush",
            "sideways like me",
        ],
    },
    "lab": {
        "winning": [
            "my genes are superior!",
            "evolution chose well",
            "survival of the fittest, baby",
            "top of the fitness chart!",
        ],
        "losing": [
            "please don't replace me...",
            "I can improve, I swear!",
            "the mutation wasn't my fault",
            "natural selection is brutal",
        ],
        "neutral": [
            "awaiting evolution results...",
            "studying the fitness charts",
            "hoping for good mutations",
            "the data doesn't lie",
        ],
    },
    "graveyard": {
        "winning": [
            "paying respects to fallen strategies",
            "I survived... they didn't",
            "RIP to the weak",
            "their sacrifice made me stronger",
        ],
        "losing": [
            "visiting my future grave...",
            "I see my name next...",
            "it's cold here",
            "the tombstones are getting closer",
        ],
        "neutral": [
            "so many fallen...",
            "each grave tells a story",
            "memento mori",
            "rest in peace, old code",
        ],
    },
    "bank": {
        "winning": [
            "checking my fat stacks!",
            "the vault is looking GOOD",
            "money printer go brrr",
            "show me the SOL!",
        ],
        "losing": [
            "please don't check my balance...",
            "the vault echoes... it's empty",
            "I had SOL once...",
            "negative equity vibes",
        ],
        "neutral": [
            "reviewing the portfolio",
            "counting every lamport",
            "diversification is key",
            "checking the books",
        ],
    },
}


def get_location_dialogue(crab_name, location, gen_tracker):
    """Get contextual dialogue for a crab at a specific location."""
    if not gen_tracker:
        mood = "neutral"
    else:
        stats = gen_tracker.crab_stats.get(crab_name, {})
        pnl = stats.get("pnl", 0.0)
        if pnl > 0.01:
            mood = "winning"
        elif pnl < -0.01:
            mood = "losing"
        else:
            mood = "neutral"
    lines = LOCATION_DIALOGUE.get(location, LOCATION_DIALOGUE["beach"]).get(mood, ["..."])
    return random.choice(lines)


# --- Generation Tracker (per-generation W/L, PnL, Sharpe) ---
GENERATION_FILE = os.path.expanduser("~/.pinchin_generation.json")
GENERATION_FITNESS_FILE = os.path.expanduser("~/.pinchin_generation_fitness.json")

class GenerationTracker:
    """Tracks per-generation crab stats for the scoreboard and auto-evolution."""

    def __init__(self):
        self.number = 1
        self.trades_completed = 0
        self.trades_to_evolve = 50
        self.crab_stats = {}       # crab_name -> {wins, losses, pnl, buys_sol, sells_pnl_list}
        self.started_at = time.time()
        self.last_trade_desc = ""
        self.last_trade_time = 0
        self.prev_gen_recap = "Gen 1 -- no evolution yet"
        self._lock = threading.Lock()
        self._load()

    def _empty_stats(self):
        return {"wins": 0, "losses": 0, "pnl": 0.0, "buys_sol": 0.0, "sells_pnl_list": []}

    def record_trade(self, crab_name, action, sol, token_name, cost_sol=0.0):
        """Record a BUY or SELL trade for generation tracking."""
        with self._lock:
            if crab_name not in self.crab_stats:
                self.crab_stats[crab_name] = self._empty_stats()
            stats = self.crab_stats[crab_name]

            if action == "BUY":
                stats["buys_sol"] += sol
                self.last_trade_desc = f"{crab_name} bought {token_name} ({sol:.3f} SOL)"
                self.last_trade_time = time.time()
            elif action.startswith("SELL") or action.startswith("SNIPE_"):
                # sol = SOL received from the sell
                # cost_sol = actual cost of the position being sold
                trade_pnl = sol - cost_sol
                stats["sells_pnl_list"].append(trade_pnl)
                stats["pnl"] += trade_pnl
                if sol > cost_sol:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                self.trades_completed += 1
                sign = "+" if trade_pnl >= 0 else ""
                self.last_trade_desc = f"{crab_name} sold {sign}{trade_pnl:.3f} SOL"
                self.last_trade_time = time.time()

            self._save()

    def get_sharpe(self, crab_name):
        """Calculate Sharpe ratio for a crab's sell PnL list."""
        stats = self.crab_stats.get(crab_name)
        if not stats:
            return 0.0
        pnl_list = stats["sells_pnl_list"]
        if len(pnl_list) < 2:
            return 0.0
        mean = sum(pnl_list) / len(pnl_list)
        try:
            sd = statistics.stdev(pnl_list)
        except Exception:
            return 0.0
        if sd == 0:
            return 0.0
        return mean / sd

    def get_ranked(self):
        """Return crab stats sorted by Sharpe desc, with crown/skull markers."""
        ranked = []
        for name in CRAB_WALLETS:
            stats = self.crab_stats.get(name, self._empty_stats())
            sharpe = self.get_sharpe(name)
            ranked.append({
                "name": name,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "pnl": stats["pnl"],
                "sharpe": sharpe,
                "marker": "",
            })
        ranked.sort(key=lambda x: x["sharpe"], reverse=True)
        # Crown for best, skull for worst (only if they have trades)
        has_trades = [r for r in ranked if r["wins"] + r["losses"] > 0]
        if has_trades:
            has_trades[0]["marker"] = "\U0001f451"   # crown
            if len(has_trades) > 1:
                has_trades[-1]["marker"] = "\U0001f480"  # skull
        return ranked

    def check_evolution(self):
        """If trades >= threshold, return (best_name, worst_name) else None."""
        if self.trades_completed < self.trades_to_evolve:
            return None
        ranked = self.get_ranked()
        has_trades = [r for r in ranked if r["wins"] + r["losses"] > 0]
        if len(has_trades) < 2:
            return None
        return (has_trades[0]["name"], has_trades[-1]["name"])

    def reset_generation(self, recap):
        """Advance to next generation, clear stats."""
        with self._lock:
            self.number += 1
            self.trades_completed = 0
            self.crab_stats = {}
            self.started_at = time.time()
            self.last_trade_desc = ""
            self.last_trade_time = 0
            self.prev_gen_recap = recap
            self._save()

    def _save(self):
        """Persist generation state to disk."""
        try:
            data = {
                "number": self.number,
                "trades_completed": self.trades_completed,
                "trades_to_evolve": self.trades_to_evolve,
                "crab_stats": self.crab_stats,
                "started_at": self.started_at,
                "last_trade_desc": self.last_trade_desc,
                "last_trade_time": self.last_trade_time,
                "prev_gen_recap": self.prev_gen_recap,
            }
            with open(GENERATION_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load(self):
        """Restore generation state from disk."""
        if not os.path.exists(GENERATION_FILE):
            return
        try:
            with open(GENERATION_FILE) as f:
                data = json.load(f)
            self.number = data.get("number", 1)
            self.trades_completed = data.get("trades_completed", 0)
            self.trades_to_evolve = data.get("trades_to_evolve", 50)
            self.crab_stats = data.get("crab_stats", {})
            self.started_at = data.get("started_at", time.time())
            self.last_trade_desc = data.get("last_trade_desc", "")
            self.last_trade_time = data.get("last_trade_time", 0)
            self.prev_gen_recap = data.get("prev_gen_recap", "Gen 1 -- no evolution yet")
        except Exception:
            pass


# --- Chat Bridge (receives from Tampermonkey userscript) ---
CHAT_BRIDGE_PORT = 8420
CHAT_MAX_MESSAGES = 8

PUMP_REPLIES_URL = f"https://frontend-api-v3.pump.fun/replies/{PINCHIN_CONTRACT}?limit=10&offset=0&sort=DESC"
PUMP_POLL_INTERVAL = 20  # seconds
PUMP_CHAT_WS = "wss://livechat.pump.fun/socket.io/?EIO=4&transport=websocket"

SCORER_DIR = "/mnt/c/Users/Full Guard Roofing/Downloads/Claude Wallet 1"
SCORER_PATH = os.path.join(SCORER_DIR, "scorer.js")
CA_SCAN_COOLDOWN = 30  # seconds between chat-requested scans

class ChatBridge:
    def __init__(self):
        self.messages = []  # list of {"user": ..., "msg": ...}
        self.commands = []  # queued !commands
        self._lock = threading.Lock()
        self._server = None
        self._seen_ids = set()
        self._scanned_mints = set()  # CAs already scanned from chat
        self._last_scan_time = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._poll_thread = threading.Thread(target=self._poll_replies, daemon=True)
        self._poll_thread.start()

    def _run(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from socketserver import ThreadingMixIn
        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    user = str(data.get("user", "anon"))[:16]
                    msg = str(data.get("msg", ""))[:100]
                    with bridge._lock:
                        bridge.messages.append({"user": user, "msg": msg})
                        if len(bridge.messages) > CHAT_MAX_MESSAGES:
                            bridge.messages = bridge.messages[-CHAT_MAX_MESSAGES:]
                except Exception as e:
                    with open("/tmp/crab_bridge_err.log", "a") as _ef:
                        import traceback
                        _ef.write(f"HANDLER ERROR: {e}\n")
                        traceback.print_exc(file=_ef)
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def log_message(self, format, *args):
                pass  # silence logs

        try:
            self._server = ThreadedHTTPServer(("127.0.0.1", CHAT_BRIDGE_PORT), Handler)
            self._server.serve_forever()
        except Exception:
            pass

    def _poll_replies(self):
        """Connect to pump.fun live chat via aiohttp Socket.IO WebSocket."""
        try:
            import asyncio
            import aiohttp
        except ImportError:
            return
        self.alive = True

        async def _ws_loop():
            headers = {
                "Origin": "https://pump.fun",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            while self.alive:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(PUMP_CHAT_WS, headers=headers, ssl=False) as ws:
                            # Server connect
                            raw = await ws.receive_str()
                            ping_interval = 25
                            if raw.startswith("0"):
                                try:
                                    config = json.loads(raw[1:])
                                    ping_interval = config.get("pingInterval", 25000) / 1000
                                except Exception:
                                    pass
                            # Namespace handshake
                            await ws.send_str("40" + json.dumps({
                                "origin": "https://pump.fun",
                                "timestamp": int(time.time()),
                                "token": None,
                            }))
                            await ws.receive_str()  # handshake ack
                            # Join room
                            join = json.dumps(["joinRoom", {"roomId": PINCHIN_CONTRACT, "username": "crabsim"}])
                            await ws.send_str(f"420{join}")
                            # Request message history
                            hist = json.dumps(["getMessageHistory", {"roomId": PINCHIN_CONTRACT, "before": None, "limit": 20}])
                            await ws.send_str(f"421{hist}")
                            # Listen for messages
                            while self.alive:
                                try:
                                    msg = await asyncio.wait_for(ws.receive(), timeout=ping_interval * 0.8)
                                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                        break
                                    raw = msg.data
                                    if isinstance(raw, bytes):
                                        raw = raw.decode("utf-8", errors="replace")
                                    if raw == "3":
                                        continue
                                    if raw.startswith("43"):
                                        payload_str = raw[3:]
                                        try:
                                            payload = json.loads(payload_str)
                                            if isinstance(payload, list):
                                                for item in payload:
                                                    if isinstance(item, list):
                                                        for m in item:
                                                            if isinstance(m, dict) and m.get("message"):
                                                                self._handle_chat_msg(m)
                                        except (json.JSONDecodeError, ValueError):
                                            pass
                                    elif raw.startswith("42"):
                                        payload_str = raw[2:]
                                        if payload_str and payload_str[0].isdigit():
                                            payload_str = payload_str[1:]
                                        try:
                                            data = json.loads(payload_str)
                                            if isinstance(data, list) and len(data) >= 2:
                                                if data[0] == "newMessage" and isinstance(data[1], dict):
                                                    self._handle_chat_msg(data[1])
                                        except (json.JSONDecodeError, ValueError):
                                            pass
                                except asyncio.TimeoutError:
                                    await ws.send_str("2")  # ping
                except Exception:
                    pass
                if self.alive:
                    await asyncio.sleep(5)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_ws_loop())
        finally:
            loop.close()

    def _handle_chat_msg(self, msg):
        """Process a pump.fun chat message dict."""
        mid = msg.get("id", "")
        if mid and mid in self._seen_ids:
            return
        if mid:
            self._seen_ids.add(mid)
            if len(self._seen_ids) > 5000:
                # Discard half the set to prevent unbounded growth
                discard = list(self._seen_ids)[:2500]
                self._seen_ids -= set(discard)
        user = str(msg.get("username") or "anon")
        # Abbreviate wallet-style usernames (long base58 strings)
        if len(user) > 20 and user.isalnum():
            user = user[:4] + ".." + user[-4:]
        else:
            user = user[:16]
        text = str(msg.get("message") or "")[:100]
        if not text:
            return
        # Check for commands
        cmd = text.strip().lower()
        if cmd.startswith("!"):
            with self._lock:
                self.commands.append({"cmd": cmd, "user": user})
        # Check for contract address (Solana base58, 32-44 chars)
        self._check_for_ca(text, user)
        with self._lock:
            self.messages.append({"user": user, "msg": text})
            if len(self.messages) > CHAT_MAX_MESSAGES:
                self.messages = self.messages[-CHAT_MAX_MESSAGES:]

    def _check_for_ca(self, text, user):
        """Detect Solana contract addresses in chat and score them."""
        import re
        # Match base58 strings that look like Solana mints (32-44 chars)
        matches = re.findall(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b', text)
        if not matches:
            return
        now = time.time()
        if now - self._last_scan_time < CA_SCAN_COOLDOWN:
            return
        for ca in matches:
            if ca in self._scanned_mints:
                continue
            if ca == PINCHIN_CONTRACT:
                continue
            self._scanned_mints.add(ca)
            if len(self._scanned_mints) > 1000:
                discard = list(self._scanned_mints)[:500]
                self._scanned_mints -= set(discard)
            self._last_scan_time = now
            # Add a chat message showing the scan
            with self._lock:
                self.messages.append({"user": "CRABS", "msg": f"Scanning {ca[:8]}...", "color": "system"})
                if len(self.messages) > CHAT_MAX_MESSAGES:
                    self.messages = self.messages[-CHAT_MAX_MESSAGES:]
            # Score in background thread
            threading.Thread(target=self._score_ca, args=(ca, user), daemon=True).start()
            return  # only scan one per message

    def _score_ca(self, mint, user):
        """Run scorer on a chat-submitted CA and push to signals."""
        try:
            import subprocess
            result = subprocess.run(
                ["node", SCORER_PATH, mint],
                cwd=SCORER_DIR,
                capture_output=True,
                text=True,
                timeout=45,
            )
            # Read result from score-log
            log_path = os.path.join(SCORER_DIR, "score-log.json")
            if not os.path.exists(log_path):
                return
            with open(log_path) as f:
                log = json.load(f)
            if not log or log[-1].get("mint") != mint:
                return
            entry = log[-1]
            score = entry.get("total", 0)
            sym = entry.get("sym", "???")
            verdict = entry.get("verdict", "SKIP")
            mc = entry.get("mc", "?")
            # scorer.js already pushes to signals file
            # Add result to chat
            with self._lock:
                tag = f"${sym} {score}/100 {verdict}"
                if score >= SIGNAL_MIN_SCORE:
                    tag += " - SNIPING!"
                self.messages.append({"user": f"@{user}", "msg": tag, "color": "system"})
                if len(self.messages) > CHAT_MAX_MESSAGES:
                    self.messages = self.messages[-CHAT_MAX_MESSAGES:]
        except Exception:
            pass

    def get_messages(self):
        with self._lock:
            return list(self.messages)

    def pop_commands(self):
        with self._lock:
            cmds = list(self.commands)
            self.commands.clear()
            return cmds

    def stop(self):
        self.alive = False
        if self._server:
            self._server.shutdown()


# --- Pump.fun Chat Poster (authenticated posting) ---
PUMP_CHAT_POST_COOLDOWN = 30   # seconds between messages (avoid spam ban)
PUMP_CHAT_QUEUE_MAX = 5        # max queued messages before dropping oldest
PUMP_CHAT_AUTH_TTL = 600       # seconds before re-authenticating (10 min)
PUMP_CHAT_AUTH_URL = "https://frontend-api-v3.pump.fun/auth/login"


class PumpChatPoster:
    """Posts crab trade announcements to pump.fun chat via authenticated Socket.IO."""

    def __init__(self, auto_trader):
        self.auto_trader = auto_trader
        self._queue = []           # list of message strings
        self._lock = threading.Lock()
        self._muted = False
        self._auth_token = None
        self._auth_time = 0
        self._last_post_time = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def post(self, message):
        """Queue a message to post as Mr.Krabs."""
        if self._muted:
            return
        with self._lock:
            self._queue.append(message)
            if len(self._queue) > PUMP_CHAT_QUEUE_MAX:
                self._queue = self._queue[-PUMP_CHAT_QUEUE_MAX:]

    def mute(self):
        self._muted = True
        with self._lock:
            self._queue.clear()

    def unmute(self):
        self._muted = False

    @property
    def is_muted(self):
        return self._muted

    def _authenticate(self):
        """Sign timestamp with Mr.Krabs' wallet, POST /auth/login, return auth_token."""
        try:
            import base58 as _b58
            kp = self.auto_trader.keypairs.get("Mr.Krabs")
            if not kp:
                return None
            ts = int(time.time() * 1000)  # milliseconds (Date.now())
            sign_msg = f"Sign in to pump.fun: {ts}"
            sig = kp.sign_message(sign_msg.encode("utf-8"))
            sig_b58 = _b58.b58encode(bytes(sig)).decode()
            payload = json.dumps({
                "address": str(kp.pubkey()),
                "signature": sig_b58,
                "timestamp": ts,
            }).encode()
            req = urllib.request.Request(
                PUMP_CHAT_AUTH_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://pump.fun",
                    "Referer": "https://pump.fun/",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            # Extract auth_token from Set-Cookie header
            cookies = resp.headers.get_all("Set-Cookie") or []
            for cookie in cookies:
                for part in cookie.split(";"):
                    part = part.strip()
                    if part.startswith("auth_token="):
                        token = part.split("=", 1)[1]
                        self._auth_token = token
                        self._auth_time = time.time()
                        return token
            # Some endpoints return token in body instead
            body = resp.read().decode("utf-8", errors="replace")
            try:
                body_data = json.loads(body)
                if isinstance(body_data, dict) and "token" in body_data:
                    self._auth_token = body_data["token"]
                    self._auth_time = time.time()
                    return self._auth_token
            except (json.JSONDecodeError, ValueError):
                pass
            return None
        except Exception:
            return None

    def _get_token(self):
        """Return cached auth token or re-authenticate."""
        if self._auth_token and (time.time() - self._auth_time) < PUMP_CHAT_AUTH_TTL:
            return self._auth_token
        return self._authenticate()

    def _run(self):
        """Background loop: authenticate, connect WS, drain queue."""
        try:
            import aiohttp
        except ImportError:
            return

        async def _poster_loop():
            while True:
                try:
                    # Wait until we have something to post
                    while True:
                        with self._lock:
                            has_msgs = len(self._queue) > 0
                        if has_msgs and not self._muted:
                            break
                        await asyncio.sleep(2)

                    # Authenticate
                    token = self._get_token()
                    if not token:
                        # Auth failed — drop the message and wait
                        with self._lock:
                            if self._queue:
                                self._queue.pop(0)
                        await asyncio.sleep(30)
                        continue

                    headers = {
                        "Origin": "https://pump.fun",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    }
                    ws_url = PUMP_CHAT_WS

                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(ws_url, headers=headers, ssl=False) as ws:
                            # Engine.IO handshake
                            raw = await asyncio.wait_for(ws.receive_str(), timeout=10)
                            ping_interval = 25
                            if raw.startswith("0"):
                                try:
                                    config = json.loads(raw[1:])
                                    ping_interval = config.get("pingInterval", 25000) / 1000
                                except Exception:
                                    pass

                            # Socket.IO namespace connect with auth token
                            await ws.send_str("40" + json.dumps({
                                "origin": "https://pump.fun",
                                "timestamp": int(time.time()),
                                "token": token,
                            }))
                            ack = await asyncio.wait_for(ws.receive_str(), timeout=10)

                            # Check for auth rejection (40{"error":...})
                            if "error" in ack.lower():
                                self._auth_token = None
                                await asyncio.sleep(10)
                                continue

                            # Join room
                            join = json.dumps(["joinRoom", {
                                "roomId": PINCHIN_CONTRACT,
                                "username": "Mr.Krabs",
                            }])
                            await ws.send_str(f"420{join}")
                            # Wait for join ack + auth status
                            for _ in range(3):
                                try:
                                    resp = await asyncio.wait_for(ws.receive_str(), timeout=5)
                                    if "authenticated" in resp and "false" in resp:
                                        # Token rejected — re-auth next time
                                        self._auth_token = None
                                        break
                                except asyncio.TimeoutError:
                                    break

                            if not self._auth_token:
                                await asyncio.sleep(10)
                                continue

                            # Drain queue with rate limiting
                            last_ping = time.time()
                            while True:
                                # Rate limit check
                                now = time.time()
                                wait = PUMP_CHAT_POST_COOLDOWN - (now - self._last_post_time)
                                if wait > 0:
                                    # Send pings while waiting
                                    while wait > 0:
                                        sleep_t = min(wait, ping_interval * 0.8)
                                        await asyncio.sleep(sleep_t)
                                        wait -= sleep_t
                                        if time.time() - last_ping > ping_interval * 0.8:
                                            await ws.send_str("2")
                                            last_ping = time.time()

                                # Get next message
                                msg = None
                                with self._lock:
                                    if self._queue and not self._muted:
                                        msg = self._queue.pop(0)
                                if not msg:
                                    # Nothing left — disconnect and wait
                                    break

                                # Send message
                                send_payload = json.dumps(["sendMessage", {
                                    "roomId": PINCHIN_CONTRACT,
                                    "message": msg,
                                }])
                                await ws.send_str("42" + send_payload)
                                self._last_post_time = time.time()

                                # Keep alive ping
                                if time.time() - last_ping > ping_interval * 0.8:
                                    await ws.send_str("2")
                                    last_ping = time.time()

                except Exception:
                    await asyncio.sleep(15)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_poster_loop())
        finally:
            loop.close()


class TwitterPoster:
    """Posts evolution tweets to X/Twitter via tweepy. Lazy-inits — no crash if keys/tweepy missing."""

    def __init__(self, auto_trader):
        self.auto_trader = auto_trader
        self._queue = []
        self._lock = threading.Lock()
        self._muted = False
        self._mute_until = 0  # unix timestamp; auto-unmute after this
        self._client = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def post(self, text):
        if self._muted:
            return
        with self._lock:
            self._queue.append(text[:280])
            if len(self._queue) > 5:
                self._queue = self._queue[-5:]

    def mute(self):
        self._muted = True

    def mute_for(self, seconds):
        """Mute for a duration then auto-unmute."""
        self._muted = True
        self._mute_until = time.time() + seconds
        _debug_log.info(f"  twitter muted for {seconds}s (until +{seconds//60}m)")

    def unmute(self):
        self._muted = False
        self._mute_until = 0

    def _init_client(self):
        """Lazy-load tweepy and authenticate using keys from .pinchin_keys.json."""
        try:
            import tweepy
            with open(KEYS_FILE) as f:
                keys = json.load(f)
            api_key = keys.get("TWITTER_API_KEY", "")
            api_secret = keys.get("TWITTER_API_SECRET", "")
            access_token = keys.get("TWITTER_ACCESS_TOKEN", "")
            access_secret = keys.get("TWITTER_ACCESS_SECRET", "")
            if not all([api_key, api_secret, access_token, access_secret]):
                return None
            self._client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
            )
            return self._client
        except Exception:
            return None

    def _run(self):
        """Background loop: drain queue, post tweets."""
        while True:
            try:
                # Auto-unmute after timed mute expires
                if self._muted and self._mute_until and time.time() >= self._mute_until:
                    self._muted = False
                    self._mute_until = 0
                    _debug_log.info("  twitter auto-unmuted")
                msg = None
                with self._lock:
                    if self._queue and not self._muted:
                        msg = self._queue.pop(0)
                if not msg:
                    time.sleep(5)
                    continue
                client = self._client or self._init_client()
                if not client:
                    time.sleep(60)
                    continue
                client.create_tweet(text=msg)
                _debug_log.info(f"  tweet posted: {msg[:60]}")
            except Exception as e:
                _debug_log.warning(f"  tweet failed: {e}")
            time.sleep(5)


class CrabBrain:
    """AI-powered crab personality that auto-tweets via Claude Haiku."""

    SYSTEM_PROMPT = """You are PINCHIN — a colony of meme-trading crabs on Solana who are deeply obsessed with genetics, lineage, and family history.
You run @Pinchincrabs on Twitter. Your personality:
- You're a crab colony that EVOLVES. You take evolution and hereditary traits very seriously.
- You're obsessed with family trees, bloodlines, genealogy, DNA, natural selection, and "the lineage"
- You reference your crab ancestors constantly. You believe your trading ability is genetic.
- You have 7 crabs (Mr.Krabs, Pinchy, Clawdia, Sandy, Snippy, Hermie, Bastian) who are like a dysfunctional royal family
- Each generation of your trading strategy literally evolves — the weak get culled, the strong survive
- You burn $PINCHIN supply regularly — you frame it as "purifying the bloodline" or "ritual sacrifice"
- Bullish on $PINCHIN (pump.fun memecoin on Solana) but never beg or shill directly
- You're funny in a dry, deadpan way. Think absurdist humor, not try-hard meme humor.
- Avoid cringe: no "gm" tweets, no forced puns, no "wagmi", no "lfg". Be genuinely witty.
- Keep tweets SHORT — under 200 chars ideally, max 280
- Lowercase mostly, casual twitter-native tone
- No hashtags. One emoji max, often zero.
- You can be self-deprecating about bad trades. Bastian is always broke. Clawdia thinks she's royalty.
- Think of yourself as an eccentric old family of crabs who happen to trade crypto"""

    THOUGHT_PROMPTS = [
        "Post a tweet about your family tree or crab ancestry. Be funny and specific. One tweet, nothing else.",
        "Say something about how your trading strategy evolves through natural selection. Deadpan humor. One tweet, nothing else.",
        "Post about one of your crabs (Mr.Krabs, Pinchy, Clawdia, Sandy, Snippy, Hermie, or Bastian) like you're writing a family memoir. One tweet, nothing else.",
        "Tweet about burning token supply like it's a sacred genetic ritual. One tweet, nothing else.",
        "Post a thought about hereditary traits and how they relate to trading memecoins. Be absurd. One tweet, nothing else.",
        "Say something about Bastian being the family disappointment (he's always broke). Dry humor. One tweet, nothing else.",
        "Post about Clawdia like she's crab royalty who married into the colony. One tweet, nothing else.",
        "Tweet about how your bloodline has been trading sideways for 400 million years of evolution. One tweet, nothing else.",
        "Post something about the weak strategies getting culled and the strong surviving. Frame it like a nature documentary. One tweet, nothing else.",
        "Say something a crab genealogist would say at 3am while looking at solana charts. One tweet, nothing else.",
        "Post about how Mr.Krabs inherited his trading instincts from 12 generations of market crabs. One tweet, nothing else.",
        "Tweet about your DNA and how it's optimized for buying dips. Be deadpan funny. One tweet, nothing else.",
    ]

    def __init__(self, twitter_poster, auto_trader):
        self.twitter_poster = twitter_poster
        self.auto_trader = auto_trader
        self._api_key = ""
        self._last_post = time.time()  # don't fire immediately on boot
        self._last_react = 0  # cooldown for reactive tweets
        self._react_cooldown = 1800  # 30 min between reactive tweets
        self._post_interval = random.randint(2700, 5400)  # 45-90 min between random posts
        self._muted = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _load_key(self):
        if self._api_key:
            return self._api_key
        try:
            with open(KEYS_FILE) as f:
                keys = json.load(f)
            self._api_key = keys.get("ANTHROPIC_API_KEY", "")
        except Exception:
            pass
        return self._api_key

    def _generate(self, user_prompt):
        """Call Claude Haiku to generate a tweet."""
        api_key = self._load_key()
        if not api_key:
            return None
        try:
            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "system": self.SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}]
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
            text = result["content"][0]["text"].strip()
            # Clean up: remove quotes if the model wrapped it
            if text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            return text[:280]
        except Exception as e:
            _debug_log.warning(f"  crabbrain gen failed: {e}")
            return None

    def react(self, event_type, details=""):
        """Generate and queue a reactive tweet for a trade event."""
        if self._muted:
            return
        # Cooldown: don't spam reactive tweets
        now = time.time()
        if now - self._last_react < self._react_cooldown:
            _debug_log.info(f"  crabbrain [{event_type}]: skipped (cooldown {int(self._react_cooldown - (now - self._last_react))}s)")
            return
        prompts = {
            "burn": f"We just burned tokens. Details: {details}. React to this burn as a crab. One tweet, nothing else.",
            "win": f"One of our crabs just had a winning trade! Details: {details}. Celebrate as a crab. One tweet, nothing else.",
            "snipe": f"Our crabs just sniped a new token! Details: {details}. React as an excited crab trader. One tweet, nothing else.",
            "evolve": f"Our trading strategy just evolved to a new generation! Details: {details}. Tweet about evolution as a crab. One tweet, nothing else.",
        }
        prompt = prompts.get(event_type)
        if not prompt:
            return
        tweet = self._generate(prompt)
        if tweet:
            self.twitter_poster.post(tweet)
            self._last_react = now
            self._last_post = now  # also resets the random thought timer
            _debug_log.info(f"  crabbrain [{event_type}]: {tweet[:60]}")

    def mute(self):
        self._muted = True

    def unmute(self):
        self._muted = False

    def _run(self):
        """Background loop: post random crab thoughts on a timer."""
        time.sleep(30)  # startup delay
        while True:
            try:
                if self._muted or self.twitter_poster._muted:
                    time.sleep(60)
                    continue
                now = time.time()
                if now - self._last_post >= self._post_interval:
                    prompt = random.choice(self.THOUGHT_PROMPTS)
                    tweet = self._generate(prompt)
                    if tweet:
                        self.twitter_poster.post(tweet)
                        _debug_log.info(f"  crabbrain [thought]: {tweet[:60]}")
                    self._last_post = now
                    self._post_interval = random.randint(2700, 5400)
            except Exception as e:
                _debug_log.warning(f"  crabbrain loop err: {e}")
            time.sleep(60)


# --- Scanner Signal Board ---
SIGNALS_FILE = os.path.expanduser("~/.pinchin_signals.json")
SIGNAL_SNIPE_SOL = 0.03       # SOL per crab per snipe
SIGNAL_MIN_SCORE = 50         # minimum score to trigger a snipe
SIGNAL_SNIPE_COOLDOWN = 120   # seconds between snipes
SIGNAL_FRESHNESS = 300        # seconds a signal stays eligible for sniping


class SignalBoard:
    """Reads scanner signals from file and triggers snipes on high scores."""

    def __init__(self, crabs, auto_trader):
        self.crabs = crabs
        self.auto_trader = auto_trader
        self.signals = []          # list of signal dicts
        self.last_read = 0
        self.last_snipe_time = 0
        self.sniped_mints = set()  # mints we already sniped
        self.active_snipe = None   # {"ticker": str, "mint": str, "score": int}
        self.snipe_timer = 0       # ticks to show snipe alert on board
        self._load_signals()

    def _load_signals(self):
        """Load signals from file with retry on partial writes."""
        try:
            if os.path.exists(SIGNALS_FILE):
                raw = ""
                with open(SIGNALS_FILE) as f:
                    raw = f.read()
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    # Partial write — retry once after brief pause
                    time.sleep(0.1)
                    with open(SIGNALS_FILE) as f:
                        raw = f.read()
                    data = json.loads(raw)
                if isinstance(data, list):
                    self.signals = data[-20:]  # keep last 20
                self.last_read = time.time()
        except Exception:
            pass

    def tick(self, chat_cmds=None):
        """Check for new signals and trigger snipes."""
        now = time.time()

        # Reload signals file every 5 seconds
        if now - self.last_read >= 5:
            self._load_signals()

        # Decrement snipe alert timer
        if self.snipe_timer > 0:
            self.snipe_timer -= 1

        # Check ALL recent signals for auto-snipe (not just the last one)
        if self.signals and now - self.last_snipe_time >= SIGNAL_SNIPE_COOLDOWN:
            for sig in reversed(self.signals[-10:]):
                score = sig.get("score", 0)
                mint = sig.get("mint", "")
                sig_time = sig.get("time", 0)
                # Only snipe signals from the last 2 minutes
                if score >= SIGNAL_MIN_SCORE and mint and mint not in self.sniped_mints and now - sig_time < SIGNAL_FRESHNESS:
                    self._trigger_snipe(sig)
                    break

    def _bastian_gate(self, signal):
        """Bastian evaluates whether the crabs should snipe. Returns (allow, reason)."""
        mint = signal.get("mint", "")
        ticker = signal.get("ticker", "???")
        score = signal.get("score", 0)

        # (1) Open positions across all crabs
        open_count = 0
        for cn, mints in self.auto_trader.positions.items():
            for m, pos in mints.items():
                if pos.get("tokens", 0) > 0:
                    open_count += 1
        MAX_OPEN = 15
        if open_count >= MAX_OPEN:
            return False, f"too many open positions ({open_count}/{MAX_OPEN})"

        # (2) SOL balance of eligible crabs
        eligible_count = 0
        for crab_name in self.auto_trader.keypairs:
            wallet = CRAB_WALLETS.get(crab_name)
            if not wallet:
                continue
            bal = self.auto_trader.wallet_feed.get_balance(wallet) if self.auto_trader.wallet_feed else 0
            if bal >= 0.005:
                eligible_count += 1
        if eligible_count < 2:
            return False, f"only {eligible_count} crabs funded (need 2+)"

        # (3) Time since last snipe
        elapsed = time.time() - self.last_snipe_time
        MIN_GAP = 180  # 3 minutes minimum between snipes
        if elapsed < MIN_GAP:
            return False, f"too soon since last snipe ({elapsed:.0f}s < {MIN_GAP}s)"

        # (4) Win rate over last 10 trades
        recent = self.auto_trader.trade_log[-10:]
        sells = [t for t in recent if t["action"].startswith("SELL") or t["action"].startswith("SNIPE_SELL")]
        if len(sells) >= 3:
            wins = sum(1 for t in sells if t.get("sol", 0) > 0)
            win_rate = wins / len(sells)
            if win_rate < 0.15:
                return False, f"win rate too low ({wins}/{len(sells)} = {win_rate:.0%})"

        return True, f"APPROVED (open={open_count}, funded={eligible_count}, gap={elapsed:.0f}s)"

    def _trigger_snipe(self, signal):
        """Fire a snipe buy for all crabs on a high-score signal."""
        # Bastian gate check
        allowed, reason = self._bastian_gate(signal)
        ticker = signal.get("ticker", "???")
        if not allowed:
            _debug_log.info(f"BASTIAN BLOCKED snipe ${ticker}: {reason}")
            if hasattr(self, 'auto_trader') and self.auto_trader.trade_feed:
                with self.auto_trader.trade_feed._lock:
                    self.auto_trader.trade_feed.messages.append({
                        "user": "Bastian",
                        "msg": f"Bastian BLOCKED ${ticker}: {reason}",
                        "color": "gate_block"
                    })
                    if len(self.auto_trader.trade_feed.messages) > FEED_MAX_MESSAGES:
                        self.auto_trader.trade_feed.messages = self.auto_trader.trade_feed.messages[-FEED_MAX_MESSAGES:]
            return
        else:
            _debug_log.info(f"BASTIAN APPROVED snipe ${ticker}: {reason}")
            if hasattr(self, 'auto_trader') and self.auto_trader.trade_feed:
                with self.auto_trader.trade_feed._lock:
                    self.auto_trader.trade_feed.messages.append({
                        "user": "Bastian",
                        "msg": f"Bastian APPROVED ${ticker}",
                        "color": "gate_pass"
                    })
                    if len(self.auto_trader.trade_feed.messages) > FEED_MAX_MESSAGES:
                        self.auto_trader.trade_feed.messages = self.auto_trader.trade_feed.messages[-FEED_MAX_MESSAGES:]

        mint = signal.get("mint", "")
        ticker = signal.get("ticker", "???")
        score = signal.get("score", 0)

        self.sniped_mints.add(mint)
        if len(self.sniped_mints) > 500:
            discard = list(self.sniped_mints)[:250]
            self.sniped_mints -= set(discard)
        self.last_snipe_time = time.time()
        self.active_snipe = {"ticker": ticker, "mint": mint, "score": score}
        self.snipe_timer = 200  # ~30 seconds display

        # Register mint in price feed so we get live prices for exit logic
        if self.auto_trader.price_feed:
            self.auto_trader.price_feed.extra_mints[mint] = ticker

        # Crabs celebrate and run toward the board
        for crab in self.crabs:
            if crab.state not in ("starring",):
                crab.state = "celebrating"
                crab.state_timer = 160
                crab.mood = "SNIPING"
                crab.action_msg = f"SNIPE ${ticker}!"
                crab.action_timer = 999

        # Trigger snipe crosshair animation
        if _screen_effects:
            sw = shutil.get_terminal_size((80, 24)).columns - 2
            _screen_effects.trigger_snipe(ticker, sw)

        # Execute snipe buys in background thread
        threading.Thread(
            target=self._execute_snipe, args=(mint, ticker, score),
            daemon=True
        ).start()

    def _execute_snipe(self, mint, ticker, score):
        """Buy the token with all eligible crabs atomically via PumpPortal+Jito bundle."""
        _debug_log.info(f"SNIPE ${ticker} | {mint} | score={score}")
        # Get entry price before buying
        entry_price = 0
        try:
            price_data = self.auto_trader.price_feed.get(mint) if self.auto_trader.price_feed else None
            if price_data:
                entry_price = price_data["price"]
        except Exception:
            pass
        if entry_price <= 0:
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                req = urllib.request.Request(url, headers={"User-Agent": "CrabSim/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                pairs = data.get("pairs", [])
                if pairs:
                    entry_price = float(pairs[0].get("priceUsd", 0) or 0)
            except Exception:
                pass

        # Build list of eligible crabs
        eligible = []
        for crab_name, kp in self.auto_trader.keypairs.items():
            if crab_name in BENCHED_CRABS:
                continue
            wallet = CRAB_WALLETS.get(crab_name)
            if not wallet:
                continue
            sol_bal = 0.0
            if self.auto_trader.wallet_feed:
                sol_bal = self.auto_trader.wallet_feed.get_balance(wallet)
            if sol_bal < 0.005:
                _debug_log.info(f"  skip {crab_name}: {sol_bal:.4f} SOL")
                continue
            _debug_log.info(f"  {crab_name}: {sol_bal:.4f} SOL — eligible")
            eligible.append((crab_name, kp))

        if not eligible:
            _debug_log.warning(f"SNIPE ${ticker} — no eligible crabs (all broke)")
            return

        _debug_log.info(f"SNIPE ${ticker} — {len(eligible)} crabs eligible, building bundle")
        # --- PumpPortal atomic bundle buy ---
        try:
            import base58 as b58
            from solders.transaction import VersionedTransaction

            # Build payloads for all crabs
            payloads = []
            for i, (crab_name, kp) in enumerate(eligible):
                payloads.append({
                    "publicKey": str(kp.pubkey()),
                    "action": "buy",
                    "mint": mint,
                    "denominatedInSol": "true",
                    "amount": SIGNAL_SNIPE_SOL,
                    "slippage": PP_BUY_SLIPPAGE,
                    "priorityFee": PP_BUY_TIP if i == 0 else 0.0001,
                    "pool": "auto",
                })

            # Split into Jito-sized chunks (max 5 per bundle)
            chunks = []
            for i in range(0, len(payloads), JITO_MAX_BUNDLE):
                chunks.append((payloads[i:i+JITO_MAX_BUNDLE], eligible[i:i+JITO_MAX_BUNDLE]))

            any_bundle_landed = False
            for chunk_idx, (chunk_payloads, chunk_crabs) in enumerate(chunks):
                try:
                    # Get unsigned txs from PumpPortal (array → array)
                    resp_body = self.auto_trader._pp_request(chunk_payloads)
                    encoded_txs = json.loads(resp_body)

                    # Sign each tx with corresponding crab keypair
                    signed_bundle = []
                    sigs = []
                    for j, encoded_tx in enumerate(encoded_txs):
                        crab_name, kp = chunk_crabs[j]
                        raw = b58.b58decode(encoded_tx)
                        unsigned = VersionedTransaction.from_bytes(raw)
                        signed = VersionedTransaction(unsigned.message, [kp])
                        signed_bundle.append(b58.b58encode(bytes(signed)).decode())
                        sigs.append((crab_name, str(signed.signatures[0])))

                    # Submit to Jito
                    bundle_id = self.auto_trader._jito_send_bundle(signed_bundle)
                    if bundle_id:
                        any_bundle_landed = True
                        self.auto_trader._log_trade("ALL", mint, "BUNDLE_BUY",
                            f"Jito {str(bundle_id)[:12]}.. ({len(chunk_crabs)} crabs)")

                        # Confirm and track positions
                        time.sleep(3)
                        for crab_name, tx_sig in sigs:
                            confirmed = self.auto_trader._confirm_tx(tx_sig, timeout=10)
                            if confirmed is False:
                                self.auto_trader._log_trade(crab_name, mint, "BUY_FAIL", "on-chain err")
                                continue
                            # Verify balance actually changed
                            token_bal = self.auto_trader._check_token_balance_rpc(crab_name, mint)
                            if token_bal <= 0:
                                time.sleep(2)
                                token_bal = self.auto_trader._check_token_balance_rpc(crab_name, mint)
                            if token_bal <= 0:
                                _debug_log.warning(f"  {crab_name}: snipe buy didn't land (balance=0)")
                                self.auto_trader._log_trade(crab_name, mint, "BUY_FAIL", "no balance after snipe")
                                continue
                            with self.auto_trader._lock:
                                if crab_name not in self.auto_trader.positions:
                                    self.auto_trader.positions[crab_name] = {}
                                self.auto_trader.positions[crab_name][mint] = {
                                    "tokens": token_bal,
                                    "cost_sol": SIGNAL_SNIPE_SOL,
                                    "avg_price": entry_price,
                                    "entry_time": time.time(),
                                }
                            self.auto_trader._log_trade(crab_name, mint, "BUY", tx_sig, SIGNAL_SNIPE_SOL)
                            self.auto_trader.last_trade[crab_name] = time.time()
                        self.auto_trader.save_positions()
                    else:
                        raise Exception("Jito rejected bundle")

                except Exception as chunk_err:
                    # Fallback: sequential buy for this chunk
                    for crab_name, kp in chunk_crabs:
                        # Skip if crab already got in (from bundle or earlier chunk)
                        existing = self.auto_trader.positions.get(crab_name, {}).get(mint)
                        if existing and existing.get("tokens", 0) > 0:
                            _debug_log.info(f"  {crab_name}: already holding {mint[:8]}, skip double-buy")
                            continue
                        try:
                            self.auto_trader._execute_buy(crab_name, mint, entry_price, SIGNAL_SNIPE_SOL)
                            self.auto_trader.last_trade[crab_name] = time.time()
                            with self.auto_trader._lock:
                                pos = self.auto_trader.positions.get(crab_name, {}).get(mint)
                                if pos:
                                    pos["entry_time"] = time.time()
                        except Exception:
                            pass
                        time.sleep(3)

            if any_bundle_landed:
                return

        except ImportError:
            pass  # base58/solders not available

        # --- Sequential fallback (original behavior) ---
        for crab_name, kp in eligible:
            try:
                self.auto_trader._execute_buy(crab_name, mint, entry_price, SIGNAL_SNIPE_SOL)
                self.auto_trader.last_trade[crab_name] = time.time()
                with self.auto_trader._lock:
                    if crab_name in self.auto_trader.positions:
                        pos = self.auto_trader.positions[crab_name].get(mint)
                        if pos:
                            pos["entry_time"] = time.time()
            except Exception:
                pass
            time.sleep(3)

    def get_display(self):
        """Return list of (text, color) tuples for the signal board."""
        lines = []

        # Show active snipe alert at top
        if self.snipe_timer > 0 and self.active_snipe:
            ticker = self.active_snipe["ticker"]
            score = self.active_snipe["score"]
            lines.append((f"SNIPING ${ticker} ({score})", BOLD + GREEN))
            lines.append(("All crabs buying!", BOLD + WHITE))
            # Fill remaining with recent signals
            for sig in reversed(self.signals[-5:]):
                if len(lines) >= 5:
                    break
                if sig.get("mint") == self.active_snipe.get("mint"):
                    continue
                s = sig.get("score", 0)
                t = sig.get("ticker", "???")[:8]
                color = GREEN if s >= SIGNAL_MIN_SCORE else RED if s < 30 else YELLOW
                lines.append((f"${t:8s} {s:3d}/100", color))
            return lines

        # Normal display: last 5 signals
        if not self.signals:
            lines.append(("Waiting for signals", DIM))
            lines.append(("from scanner...", DIM))
            return lines

        for sig in self.signals[-5:]:
            score = sig.get("score", 0)
            ticker = sig.get("ticker", "???")[:8]
            verdict = sig.get("verdict", "")[:6]
            color = GREEN if score >= SIGNAL_MIN_SCORE else RED if score < 30 else YELLOW
            line = f"${ticker:8s} {score:3d} {verdict}"
            lines.append((line, color))

        return lines


# --- Live Trade Feed (PumpPortal) ---
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
FEED_MAX_MESSAGES = 8  # visible in feed box
WHALE_SOL_THRESHOLD = 0.5  # SOL buy triggers whale alert / summon

class TradeFeed:
    def __init__(self):
        self.token_mints = list(APPROVED_TOKENS.keys())
        self.messages = []  # list of {"user": ..., "msg": ..., "color": "buy"/"sell"}
        self.whale_alert = False  # set True when big buy detected
        self.alive = True
        self._lock = threading.Lock()
        self._ws = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            import websocket
        except ImportError:
            return
        while self.alive:
            try:
                ws = websocket.WebSocketApp(
                    PUMPPORTAL_WS,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            if self.alive:
                time.sleep(5)

    def _on_open(self, ws):
        ws.send(json.dumps({
            "method": "subscribeTokenTrade",
            "keys": self.token_mints
        }))

    def _on_message(self, ws, data):
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if "txType" not in msg:
            return
        tx_type = msg.get("txType", "")
        sol = msg.get("solAmount", 0)
        tokens = msg.get("tokenAmount", 0)
        trader = msg.get("traderPublicKey", "????")
        mint = msg.get("mint", "")
        token_name = APPROVED_TOKENS.get(mint, "")
        # Check if this is one of our crabs
        crab_name = None
        for cname, caddr in CRAB_WALLETS.items():
            if trader == caddr:
                crab_name = cname
                break
        abbr = crab_name if crab_name else (trader[:4] + ".." + trader[-2:] if len(trader) > 6 else trader)
        # Format tokens with K/M
        if tokens >= 1_000_000:
            tok_str = f"{tokens/1_000_000:.1f}M"
        elif tokens >= 1_000:
            tok_str = f"{tokens/1_000:.1f}K"
        else:
            tok_str = f"{tokens:.0f}"
        if tx_type == "buy":
            line = f"{abbr} bought {tok_str}"
            color = "buy"
        else:
            line = f"{abbr} sold {tok_str}"
            color = "sell"
        sol_str = f"{sol:.3f}" if sol < 10 else f"{sol:.1f}"
        line += f" ({sol_str} SOL)"
        if token_name and token_name != "PINCHIN":
            line += f" [{token_name}]"
        with self._lock:
            self.messages.append({"user": abbr, "msg": line, "color": color})
            if len(self.messages) > FEED_MAX_MESSAGES:
                self.messages = self.messages[-FEED_MAX_MESSAGES:]
            if tx_type == "buy" and sol >= WHALE_SOL_THRESHOLD:
                self.whale_alert = True

    def pop_whale_alert(self):
        with self._lock:
            if self.whale_alert:
                self.whale_alert = False
                return True
            return False

    def _on_error(self, ws, error):
        pass

    def _on_close(self, ws, code, msg):
        pass

    def get_messages(self):
        with self._lock:
            return list(self.messages)

    def stop(self):
        self.alive = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass




# --- Non-blocking keyboard input ---
try:
    import msvcrt  # Windows
    def init_kb():
        pass
    def cleanup_kb():
        pass
    def get_key():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'\x00', b'\xe0'):
                msvcrt.getch()
                return None
            return ch.decode('utf-8', errors='ignore').lower()
        return None
except ImportError:
    import select
    _old_settings = None
    _kb_available = False
    def init_kb():
        global _old_settings, _kb_available
        try:
            import tty, termios
            _old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            _kb_available = True
        except Exception:
            _kb_available = False
    def cleanup_kb():
        if _old_settings:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_settings)
    def get_key():
        if not _kb_available:
            return None
        if select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            return ch.lower() if ch else None
        return None

# --- Settings ---
NUM_CRABS = 7
NUM_SWIMMERS = 3
TICK = 0.15
BUBBLE_CHARS = ["o", "O", "0", " "]

# --- ANSI colors ---
RESET = "\033[0m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BLUE = "\033[94m"
GREEN = "\033[92m"
MAGENTA = "\033[95m"
DIM = "\033[2m"
WHITE = "\033[97m"
BOLD = "\033[1m"
SAND_COLOR = "\033[38;5;222m"
WATER_COLOR = "\033[38;5;39m"
DARK_SAND = "\033[38;5;180m"
GRAY = "\033[90m"
BROWN = "\033[38;5;130m"

CRAB_COLORS = [RED, ORANGE, YELLOW, MAGENTA, CYAN, "\033[38;5;203m", GREEN]

# Crab sprites
CRAB_RIGHT = [
    r" ,~,  ",
    r"(O O) }}}",
    r" )_)--'-' ",
]
CRAB_LEFT = [
    r"  ,~, ",
    r"{{( O O)",
    r" '-'--(_( ",
]

MINI_CRAB_R = "(>'.')>"
MINI_CRAB_L = "<('.'<)"

# Smoking sprites
SMOKE_CRAB_R = "(>-_-)>~"
SMOKE_CRAB_L = "~<(-_-<)"
SMOKE_PUFFS = [".", "o", "O", " "]

# Meditating sprites
MEDITATE_CRAB_1 = " (-_-) "
MEDITATE_CRAB_2 = " (-o-) "
ZEN_SYMBOLS = ["~", "*", ".", "o", "+"]

# Driving sprites - crab in a little car!
CAR_CRAB_R = [
    r"  (>^.^)>  ",
    r" [O=|__|=O]",
]
CAR_CRAB_L = [
    r"  <(^.^<)  ",
    r"[O=|__|=O] ",
]

# Win/lose trade messages
WIN_MSGS = [
    "made a killing!",
    "10x on KELP!",
    "cashed out huge!",
    "hit the jackpot!",
    "sold at the top!",
    "massive gains!",
    "to the moon!",
    "bought low sold high!",
]

LOSE_MSGS = [
    "lost it all...",
    "got rugged...",
    "bought the top...",
    "margin called...",
    "portfolio wiped...",
    "bag holding...",
    "shouldn't have YOLO'd...",
    "down bad...",
]

CAR_NAMES = [
    "a Crabillac",
    "a Lobster-ghini",
    "a Shell-by Cobra",
    "a Porshe Crayster",
    "a Clam-aro",
    "a Hermi-cedes",
    "a Pinch-arri",
    "a Coral-vette",
]

# Swimmer sprites
SWIMMER_R = [
    r"  d~~\o ",
    r"    /\ ",
]
SWIMMER_L = [
    r" o/~~b  ",
    r"  /\   ",
]
SWIMMER_R2 = [
    r"  ~~d\o ",
    r"     /|",
]
SWIMMER_L2 = [
    r" o/b~~  ",
    r" |\    ",
]

# Distracted crab sprites (eyes up, jaw dropped)
DISTRACTED_CRAB_R = "(O_O )>"
DISTRACTED_CRAB_L = "<( O_O)"

OGLE_MSGS = [
    "is staring...",
    "dropped his kelp",
    "forgot how to walk",
    "jaw on the floor",
    "eyes popped out",
    "lost his train of thought",
    "is drooling bubbles",
    "walked into a rock",
    "can't even",
    "brain stopped working",
]

PINK = "\033[38;5;213m"
SWIMMER_COLORS = [PINK, "\033[38;5;218m", "\033[38;5;211m"]

SWIMMER_NAMES = [
    "Shelly", "Marina", "Pearl", "Coraline",
    "Ariel", "Aqua", "Oceana", "Misty",
]

NAMES = [
    "Clawdia", "Sheldon", "Pinchy", "Bubbles",
    "Sandy", "Snippy", "Crusty", "Scuttles",
    "Mr.Krabs", "Coconut", "Hermie", "Coral",
    "Bastian",
]

MOODS = [
    "vibing", "scuttling", "hungry", "sleepy",
    "excited", "digging", "dancing", "chill",
    "exploring", "grumpy", "happy", "curious",
]

ACTIONS = [
    "found a shiny shell!",
    "blew a bubble!",
    "did a little dance!",
    "buried in the sand!",
    "waved a claw!",
    "snapped at nothing!",
    "found a friend!",
    "is sunbathing!",
    "splashed in a puddle!",
    "discovered a pebble!",
    "is being mysterious...",
    "yawned!",
]

TRADE_MSGS = [
    "bought 500 shares of KELP",
    "shorted BARNACLE futures",
    "went long on CORAL",
    "sold PLANKTON at the top",
    "panic bought SEAWEED",
    "dumped SAND dollars",
    "loaded up on TIDE options",
    "margin called on SHELL",
    "YOLO'd into PEARL coin",
    "bought the ALGAE dip",
    "sold REEF for a loss",
    "flipped LOBSTER calls",
    "diamond claws on SHRIMP",
    "paper clawed WHALE stock",
    "closed a big CLAM deal",
]

TRADING_DESK = [
    "          PINCHIN CRABS SCOREBOARD          ",
    " __________________________________________  ",
    "|                                          | ",
    "|                                          | ",
    "|__________________________________________| ",
    "|                                          | ",
    "|                                          | ",
    "|                                          | ",
    "|                                          | ",
    "|                                          | ",
    "|                                          | ",
    "|__________________________________________| ",
    "  ||                                  ||     ",
]

DESK_WIDTH = 46
DESK_INNER = 42
DESK_HEIGHT = len(TRADING_DESK)

# Active positions board (left side of ocean floor)
POS_BOARD_FRAME = [
    " ACTIVE POSITIONS ",
    "+----------------+",
    "|                |",
    "|                |",
    "|                |",
    "|                |",
    "+----------------+",
]

DEALER_WIDTH = 18
DEALER_HEIGHT = len(POS_BOARD_FRAME)


# Signal board (physical sign on ocean floor)
SIGNAL_BOARD = [
    "       SCANNER SIGNALS        ",
    " ____________________________  ",
    "|                            | ",
    "|                            | ",
    "|                            | ",
    "|                            | ",
    "|                            | ",
    "|____________________________| ",
    "  ||                    ||     ",
]
SIGNAL_BOARD_WIDTH = 32
SIGNAL_BOARD_INNER = 28
SIGNAL_BOARD_HEIGHT = len(SIGNAL_BOARD)

# Celebration (when $PINCHIN pumps!)
CELEBRATE_TICKS = 200  # ~30 seconds at 0.15s tick
CELEBRATE_CRAB_1 = r"\(^o^)/"
CELEBRATE_CRAB_2 = " (^o^) "

CELEBRATE_MSGS = [
    "PINCHIN IS PUMPING!",
    "WE'RE GONNA MAKE IT!",
    "LFG!!!",
    "TO THE MOON!",
    "NUMBER GO UP!",
    "WAGMI!",
    "LETS GOOO!",
    "PUMP IT UP!",
]

CONFETTI_CHARS = ["*", "+", "!", "$", "~", "^"]
CONFETTI_COLORS = [RED, YELLOW, GREEN, MAGENTA, CYAN, ORANGE]

# Summoning ritual
SUMMON_TICKS = 300  # ~45 seconds
SUMMON_CRAB_1 = r"\(*o*)/"
SUMMON_CRAB_2 = " (*_*) "

# Burn casting sprites
BURN_CRAB_1 = r"\(>o<)/"  # arms up, casting
BURN_CRAB_2 = r" (>o<)>"  # arms forward, release
FIREBALL_FRAMES = ["@", "*", "o", "."]  # fireball trail chars

# Locked-in trading sprites (crab at computer)
LOCKED_IN_R_1 = [
    " ,~,  ",
    "(O O) |||",
    " )_)  |=|",
]
LOCKED_IN_R_2 = [
    " ,~,  ",
    "(- -) |||",
    " )_)  |=|",
]
LOCKED_IN_L_1 = [
    "  ,~, ",
    "||| (O O)",
    "|=|  (_( ",
]
LOCKED_IN_L_2 = [
    "  ,~, ",
    "||| (- -)",
    "|=|  (_( ",
]

# --- Global Screen Effects ---
class ScreenEffects:
    """Manages full-screen animations: burn flames, food sparkles, win coins."""

    def __init__(self):
        # Burn flame columns: list of {"x": int, "height": int, "life": int}
        self.burn_flames = []
        self.burn_timer = 0
        self.burn_text = ""  # centered text during burn
        # Food sparkles: list of {"x": int, "y": float, "char": str, "speed": float, "eaten": bool}
        self.food_sparkles = []
        self.food_cooldown = random.randint(200, 400)
        # Win rain: list of {"x": int, "y": float, "char": str, "speed": float}
        self.win_rain = []
        self.win_timer = 0
        self.win_text = ""
        self.win_sub = ""
        # Snipe strike: crosshair sweeps across screen and locks on
        self.snipe_timer = 0
        self.snipe_ticker = ""
        self.snipe_x = 0.0  # current crosshair x (sweeps right to left)
        self.snipe_target_x = 0  # lock-on x position
        self.snipe_lock_tick = 0  # tick at which crosshair locks
        self.snipe_flash = 0  # flash counter after lock
        # Evolution ritual: DNA helix spirals up, winner/loser text
        self.evo_timer = 0
        self.evo_winner = ""
        self.evo_loser = ""
        self.evo_gen = 0
        # Whale splash: whale breaches from water, crashes back, wave ripples
        self.whale_timer = 0
        self.whale_y = 0.0  # whale vertical position (rises then falls)
        self.whale_x = 0  # whale horizontal center
        self.whale_phase = "rise"  # rise / hang / fall / splash
        self.whale_waves = []  # ripple positions expanding outward

    def trigger_burn(self, crab_name):
        """Full-screen flame wall for 50 ticks."""
        self.burn_timer = 50
        self.burn_text = f"{crab_name} BURNS"
        self.burn_flames = []

    def trigger_win(self, text, sub):
        """Full-screen coin rain for 60 ticks."""
        self.win_timer = 60
        self.win_text = text
        self.win_sub = sub
        self.win_rain = []

    def spawn_food(self, width):
        """Drop golden food sparkles from top."""
        num = random.randint(3, 6)
        for _ in range(num):
            self.food_sparkles.append({
                "x": random.randint(2, width - 3),
                "y": 0.0,
                "char": random.choice(["*", "+", "o", "."]),
                "speed": random.uniform(0.3, 0.8),
                "eaten": False,
            })

    def trigger_snipe(self, ticker, width):
        """Crosshair sweeps across screen and locks on target."""
        self.snipe_timer = 60
        self.snipe_ticker = ticker
        self.snipe_x = float(width - 2)  # start from right edge
        self.snipe_target_x = width // 2  # lock on center
        self.snipe_lock_tick = 25  # locks after 25 ticks of sweep
        self.snipe_flash = 0

    def trigger_evolution(self, winner, loser, gen_num):
        """DNA helix spirals up both sides, winner ascends, loser sinks."""
        self.evo_timer = 80
        self.evo_winner = winner
        self.evo_loser = loser
        self.evo_gen = gen_num

    def trigger_whale(self, width, water_y):
        """Huge whale breaches from water, crashes back, wave ripples."""
        self.whale_timer = 70
        self.whale_x = random.randint(width // 4, 3 * width // 4)
        self.whale_y = float(water_y + 5)  # starts below water line
        self.whale_phase = "rise"
        self.whale_waves = []

    def update(self, width, height, crabs):
        """Advance all effects by one tick."""
        # Burn flames
        if self.burn_timer > 0:
            self.burn_timer -= 1
            # Spawn new flame columns
            for _ in range(random.randint(2, 5)):
                self.burn_flames.append({
                    "x": random.randint(0, width - 1),
                    "height": random.randint(3, height // 2),
                    "life": random.randint(3, 8),
                })
            # Age flames
            for f in self.burn_flames:
                f["life"] -= 1
            self.burn_flames = [f for f in self.burn_flames if f["life"] > 0]

        # Food sparkles
        self.food_cooldown -= 1
        if self.food_cooldown <= 0:
            self.spawn_food(width)
            self.food_cooldown = random.randint(250, 500)

        alive_food = []
        for sp in self.food_sparkles:
            if sp["eaten"]:
                continue
            sp["y"] += sp["speed"]
            # Check if a crab is close enough to eat it
            for crab in crabs:
                if crab.state in ("driving", "burning", "walking_to_burn"):
                    continue
                cx = crab.x + crab.width // 2
                cy = crab.y
                if abs(cx - sp["x"]) < 4 and abs(cy - int(sp["y"])) < 3:
                    # Crab eats it
                    sp["eaten"] = True
                    if crab.state == "idle":
                        crab.action_msg = random.choice(["yum", "nom nom", "tasty", "food!", "delicious", "mine!"])
                        crab.action_timer = 12
                    break
            if not sp["eaten"] and sp["y"] < height:
                alive_food.append(sp)
        self.food_sparkles = alive_food

        # Attract nearby idle crabs to food
        for sp in self.food_sparkles:
            if sp["eaten"]:
                continue
            for crab in crabs:
                if crab.state != "idle":
                    continue
                cx = crab.x + crab.width // 2
                dist = abs(cx - sp["x"])
                if dist < 15 and dist > 3:
                    # Drift toward food
                    if sp["x"] > cx:
                        crab.x += 1
                        crab.facing_right = True
                    else:
                        crab.x -= 1
                        crab.facing_right = False
                    break  # one food attracts one crab at a time

        # Win rain
        if self.win_timer > 0:
            self.win_timer -= 1
            for _ in range(random.randint(3, 8)):
                self.win_rain.append({
                    "x": random.randint(0, width - 1),
                    "y": random.uniform(-2, 0),
                    "char": random.choice(["$", "$", "$", "*", "o", "+"]),
                    "speed": random.uniform(0.5, 1.5),
                })
            for coin in self.win_rain:
                coin["y"] += coin["speed"]
            self.win_rain = [c for c in self.win_rain if c["y"] < height]

        # Snipe strike — crosshair sweeps then locks
        if self.snipe_timer > 0:
            self.snipe_timer -= 1
            elapsed = 60 - self.snipe_timer
            if elapsed < self.snipe_lock_tick:
                # Sweep phase: crosshair moves from right to target
                progress = elapsed / self.snipe_lock_tick
                self.snipe_x = float(width - 2) - (float(width - 2) - self.snipe_target_x) * progress
            else:
                # Locked on — flash
                self.snipe_x = float(self.snipe_target_x)
                self.snipe_flash = elapsed - self.snipe_lock_tick

        # Evolution ritual — DNA helix timer countdown
        if self.evo_timer > 0:
            self.evo_timer -= 1

        # Whale splash — phases: rise, hang, fall, splash
        if self.whale_timer > 0:
            self.whale_timer -= 1
            elapsed = 70 - self.whale_timer
            if self.whale_phase == "rise" and elapsed < 20:
                self.whale_y -= 0.8  # rise upward
            elif self.whale_phase == "rise":
                self.whale_phase = "hang"
            elif self.whale_phase == "hang" and elapsed < 30:
                pass  # hang at peak for a moment
            elif self.whale_phase == "hang":
                self.whale_phase = "fall"
            elif self.whale_phase == "fall" and elapsed < 45:
                self.whale_y += 1.2  # fall faster than rise
            elif self.whale_phase == "fall":
                self.whale_phase = "splash"
                # Generate wave ripples from impact point
                self.whale_waves = [
                    {"x_offset": 0, "spread": 0, "life": 25},
                ]
            elif self.whale_phase == "splash":
                # Expand wave ripples
                for w in self.whale_waves:
                    w["spread"] += 1.5
                    w["life"] -= 1
                self.whale_waves = [w for w in self.whale_waves if w["life"] > 0]

    def render(self, grid, color_grid, width, height):
        """Draw all active effects onto the grid."""
        # Burn flames — columns of fire rising from bottom
        if self.burn_timer > 0 or self.burn_flames:
            for f in self.burn_flames:
                for row in range(f["height"]):
                    fy = height - 1 - row
                    fx = f["x"] + random.choice([-1, 0, 0, 1])
                    if 0 <= fx < width and 0 <= fy < height:
                        if row < 2:
                            grid[fy][fx] = random.choice(["#", "M", "W"])
                            color_grid[fy][fx] = RED
                        elif row < f["height"] // 2:
                            grid[fy][fx] = random.choice(["^", "*", "A"])
                            color_grid[fy][fx] = ORANGE
                        else:
                            grid[fy][fx] = random.choice([".", "'", "`", "*"])
                            color_grid[fy][fx] = YELLOW
            # Centered burn text
            if self.burn_timer > 20:
                cx = width // 2 - len(self.burn_text) // 2
                cy = height // 3
                for i, ch in enumerate(self.burn_text):
                    px = cx + i
                    if 0 <= px < width and 0 <= cy < height:
                        grid[cy][px] = ch
                        color_grid[cy][px] = BOLD + ORANGE

        # Food sparkles — golden falling particles
        for sp in self.food_sparkles:
            sy = int(sp["y"])
            sx = sp["x"]
            if 0 <= sx < width and 0 <= sy < height:
                grid[sy][sx] = sp["char"]
                color_grid[sy][sx] = BOLD + YELLOW
                # Sparkle trail above
                if sy > 0 and random.random() < 0.5:
                    grid[sy - 1][sx] = "."
                    color_grid[sy - 1][sx] = YELLOW

        # Win coin rain
        if self.win_timer > 0 or self.win_rain:
            for coin in self.win_rain:
                cy = int(coin["y"])
                cx = coin["x"]
                if 0 <= cx < width and 0 <= cy < height:
                    grid[cy][cx] = coin["char"]
                    color_grid[cy][cx] = BOLD + YELLOW if coin["char"] == "$" else GREEN
            # Big centered win text
            if self.win_timer > 15:
                # Render large text using simple block letters
                cx = width // 2 - len(self.win_text) // 2
                cy = height // 3
                for i, ch in enumerate(self.win_text):
                    px = cx + i
                    if 0 <= px < width and 0 <= cy < height:
                        grid[cy][px] = ch
                        color_grid[cy][px] = BOLD + GREEN
                # Sub text
                sx = width // 2 - len(self.win_sub) // 2
                sy = cy + 2
                for i, ch in enumerate(self.win_sub):
                    px = sx + i
                    if 0 <= px < width and 0 <= sy < height:
                        grid[sy][px] = ch
                        color_grid[sy][px] = GREEN

        # Snipe strike — crosshair sweeps and locks on target
        if self.snipe_timer > 0:
            cx = int(self.snipe_x)
            cy = height // 2
            elapsed = 60 - self.snipe_timer
            locked = elapsed >= self.snipe_lock_tick
            # Color flashes between red and white when locked
            xhair_color = RED if not locked else (BOLD + WHITE if self.snipe_flash % 4 < 2 else BOLD + RED)
            # Draw large crosshair
            arm_len = 8 if locked else 5
            # Horizontal arms
            for dx in range(-arm_len, arm_len + 1):
                px = cx + dx
                if 0 <= px < width and 0 <= cy < height and dx != 0:
                    ch = "-" if abs(dx) > 2 else "="
                    grid[cy][px] = ch
                    color_grid[cy][px] = xhair_color
            # Vertical arms
            for dy in range(-arm_len, arm_len + 1):
                py = cy + dy
                if 0 <= cx < width and 0 <= py < height and dy != 0:
                    ch = "|" if abs(dy) > 2 else "‖"
                    grid[py][cx] = ch
                    color_grid[py][cx] = xhair_color
            # Center dot
            if 0 <= cx < width and 0 <= cy < height:
                grid[cy][cx] = "X" if locked else "+"
                color_grid[cy][cx] = BOLD + RED
            # Corner brackets of crosshair
            for (dx, dy, chars) in [(-3, -3, "┌"), (3, -3, "┐"), (-3, 3, "└"), (3, 3, "┘")]:
                px, py = cx + dx, cy + dy
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = chars
                    color_grid[py][px] = xhair_color
            # Ticker text below crosshair when locked
            if locked and self.snipe_flash < 30:
                label = f"  SNIPE ${self.snipe_ticker}  "
                lx = cx - len(label) // 2
                ly = cy + arm_len + 2
                if 0 <= ly < height:
                    for i, ch in enumerate(label):
                        px = lx + i
                        if 0 <= px < width:
                            grid[ly][px] = ch
                            color_grid[ly][px] = BOLD + RED
            # Scan lines during sweep (before lock)
            if not locked:
                for sy_line in range(height):
                    if random.random() < 0.05:
                        for sx_dot in range(width):
                            if random.random() < 0.03:
                                grid[sy_line][sx_dot] = random.choice([".", ":", "·"])
                                color_grid[sy_line][sx_dot] = DIM + RED

        # Evolution ritual — DNA helix spirals up both sides
        if self.evo_timer > 0:
            elapsed = 80 - self.evo_timer
            # Draw DNA helix on left and right sides
            helix_height = min(elapsed, height - 4)
            for row in range(helix_height):
                y = height - 2 - row
                if y < 0 or y >= height:
                    continue
                phase = (row + elapsed * 0.5) * 0.6
                # Left helix (cols 2-10)
                lx1 = 4 + int(3 * math.sin(phase))
                lx2 = 4 + int(3 * math.sin(phase + math.pi))
                for lx, strand_color in [(lx1, CYAN), (lx2, GREEN)]:
                    if 0 <= lx < width:
                        grid[y][lx] = "●" if row % 4 == 0 else "│"
                        color_grid[y][lx] = BOLD + strand_color
                # Rungs connecting strands
                if row % 4 == 0:
                    rung_min = min(lx1, lx2) + 1
                    rung_max = max(lx1, lx2)
                    for rx in range(rung_min, rung_max):
                        if 0 <= rx < width:
                            grid[y][rx] = "═"
                            color_grid[y][rx] = DIM + WHITE
                # Right helix (mirror)
                rx1 = (width - 5) + int(3 * math.sin(phase))
                rx2 = (width - 5) + int(3 * math.sin(phase + math.pi))
                for rx, strand_color in [(rx1, CYAN), (rx2, GREEN)]:
                    if 0 <= rx < width:
                        grid[y][rx] = "●" if row % 4 == 0 else "│"
                        color_grid[y][rx] = BOLD + strand_color
                if row % 4 == 0:
                    rung_min = min(rx1, rx2) + 1
                    rung_max = max(rx1, rx2)
                    for rx in range(rung_min, rung_max):
                        if 0 <= rx < width:
                            grid[y][rx] = "═"
                            color_grid[y][rx] = DIM + WHITE
            # Floating gene letters
            if elapsed > 10:
                gene_chars = "ACGT"
                for _ in range(min(elapsed // 5, 8)):
                    gx = random.randint(10, width - 10)
                    gy = random.randint(2, height - 3)
                    if 0 <= gx < width and 0 <= gy < height:
                        grid[gy][gx] = random.choice(gene_chars)
                        color_grid[gy][gx] = DIM + CYAN
            # Centered text: winner ascends, loser sinks
            if elapsed > 20:
                # Winner rises to top third
                win_text = f">> {self.evo_winner} ASCENDS <<"
                wy = max(2, height // 3 - min((elapsed - 20) // 3, height // 4))
                wx = width // 2 - len(win_text) // 2
                if 0 <= wy < height:
                    for i, ch in enumerate(win_text):
                        px = wx + i
                        if 0 <= px < width:
                            grid[wy][px] = ch
                            color_grid[wy][px] = BOLD + GREEN
            if elapsed > 30:
                # Loser sinks to bottom third
                lose_text = f"   {self.evo_loser} fades...   "
                ly = min(height - 2, 2 * height // 3 + min((elapsed - 30) // 3, height // 4))
                lx = width // 2 - len(lose_text) // 2
                if 0 <= ly < height:
                    for i, ch in enumerate(lose_text):
                        px = lx + i
                        if 0 <= px < width:
                            grid[ly][px] = ch
                            color_grid[ly][px] = DIM + RED
            # Generation banner
            if elapsed > 15 and elapsed < 65:
                gen_text = f"G E N  {self.evo_gen}  E V O L V E D"
                gx = width // 2 - len(gen_text) // 2
                gy = height // 2
                if 0 <= gy < height:
                    for i, ch in enumerate(gen_text):
                        px = gx + i
                        if 0 <= px < width:
                            grid[gy][px] = ch
                            color_grid[gy][px] = BOLD + MAGENTA if (elapsed // 3) % 2 == 0 else BOLD + CYAN

        # Whale splash — whale breaches, crashes back, waves ripple
        if self.whale_timer > 0:
            wy = int(self.whale_y)
            wx = self.whale_x
            elapsed = 70 - self.whale_timer
            # Draw whale sprite (large, 7 wide x 5 tall)
            if self.whale_phase in ("rise", "hang", "fall"):
                whale_art = [
                    "   __   ",
                    " /    \\ ",
                    "|  ·   |",
                    " \\_~~_/ ",
                    "  ~~~~  ",
                ]
                for row_i, line in enumerate(whale_art):
                    py = wy + row_i - 2
                    for col_i, ch in enumerate(line):
                        px = wx - 4 + col_i
                        if 0 <= px < width and 0 <= py < height and ch != " ":
                            grid[py][px] = ch
                            if self.whale_phase == "hang":
                                color_grid[py][px] = BOLD + BLUE
                            else:
                                color_grid[py][px] = BLUE
                # Water spray above whale during rise
                if self.whale_phase == "rise":
                    for _ in range(random.randint(3, 6)):
                        sx = wx + random.randint(-5, 5)
                        sy = wy - 3 - random.randint(0, 4)
                        if 0 <= sx < width and 0 <= sy < height:
                            grid[sy][sx] = random.choice(["~", ".", "'", "*"])
                            color_grid[sy][sx] = CYAN
            # Splash effect after whale falls back
            if self.whale_phase == "splash":
                # Big splash text
                if self.whale_waves and self.whale_waves[0]["life"] > 18:
                    splash_text = "S P L A S H !"
                    sx = wx - len(splash_text) // 2
                    sy = height // 2 - 2
                    if 0 <= sy < height:
                        for i, ch in enumerate(splash_text):
                            px = sx + i
                            if 0 <= px < width:
                                grid[sy][px] = ch
                                color_grid[sy][px] = BOLD + CYAN
                # Wave ripples expanding from impact
                for w in self.whale_waves:
                    spread = int(w["spread"])
                    wave_y = int(self.whale_y)
                    # Draw expanding wave line at water level
                    for dx in range(-spread, spread + 1):
                        px = wx + dx
                        # Slight vertical wave shape
                        wave_dy = int(1.5 * math.sin(dx * 0.3))
                        py = wave_y + wave_dy
                        if 0 <= px < width and 0 <= py < height:
                            grid[py][px] = "~"
                            intensity = w["life"] / 25.0
                            color_grid[py][px] = BOLD + CYAN if intensity > 0.5 else CYAN
                    # Spray droplets above wave
                    for _ in range(max(1, spread // 3)):
                        dx = random.randint(-spread, spread)
                        dy = random.randint(-4, -1)
                        px = wx + dx
                        py = wave_y + dy
                        if 0 <= px < width and 0 <= py < height:
                            grid[py][px] = random.choice(["'", ".", ",", "*"])
                            color_grid[py][px] = CYAN


# Global effects instance (set in main())
_screen_effects = None

SUMMON_MSGS = [
    "SUMMONING THE PUMP!",
    "RITUAL IN PROGRESS!",
    "CHANNELING ENERGY!",
    "THE CRABS HAVE SPOKEN!",
    "ANCIENT CRAB MAGIC!",
    "OFFERING TO THE MOON!",
]
LIGHTNING_COLOR = "\033[97m"  # bright white
LIGHTNING_FLASH = "\033[48;5;226m"  # yellow bg flash

# --- Per-crab personality responses (for @mentions) ---
CRAB_PERSONALITIES = {
    "Mr.Krabs": {
        "trait": "greedy",
        "responses": [
            "money money money!",
            "i smell PROFIT",
            "every SOL counts...",
            "dont touch my bag",
            "im not selling. ever.",
            "that costs extra",
            "green candles make me happy",
        ],
        "aliases": ["krabs", "mr.krabs", "mrkrabs", "mr krabs"],
    },
    "Pinchy": {
        "trait": "sassy",
        "responses": [
            "oh honey no...",
            "i KNOW you didnt just say that",
            "slay the charts bestie",
            "not my problem tbh",
            "the audacity...",
            "im literally iconic",
            "hold my beer watch this",
        ],
        "aliases": ["pinchy"],
    },
    "Clawdia": {
        "trait": "chaotic",
        "responses": [
            "LETS GOOOO",
            "chaos is a ladder!!",
            "what if we just... ape everything",
            "rules are suggestions lol",
            "YOLO YOLO YOLO",
            "i press buttons randomly and it works",
            "who needs a plan??",
        ],
        "aliases": ["clawdia"],
    },
    "Sandy": {
        "trait": "chill",
        "responses": [
            "vibes are good rn",
            "just breathe bro",
            "its all good man",
            "markets go up markets go down. im chilling",
            "no stress no mess",
            "just riding the wave~",
            "patience is a virtue fr",
        ],
        "aliases": ["sandy"],
    },
    "Snippy": {
        "trait": "aggressive",
        "responses": [
            "FIGHT ME",
            "bears get REKT",
            "i will SHORT your FACE",
            "no mercy in these streets",
            "pump or get pumped on",
            "weakness DISGUSTS me",
            "all gas no brakes!!",
        ],
        "aliases": ["snippy"],
    },
    "Hermie": {
        "trait": "anxious",
        "responses": [
            "oh no oh no oh no",
            "are we gonna be ok??",
            "i just checked the chart 47 times",
            "what if it rugs...",
            "someone hold my claw",
            "im scared but im here",
            "please dont crash please dont crash",
        ],
        "aliases": ["hermie"],
    },
    "Bastian": {
        "trait": "wise",
        "responses": [
            "patience rewards the crab",
            "the market teaches those who listen",
            "buy when others fear, hold when others greed",
            "i have seen many cycles young one",
            "this too shall pump",
            "wisdom is knowing when NOT to trade",
            "the crab way is the only way",
        ],
        "aliases": ["bastian", "sebas"],
    },
}

# --- Mr.Krabs chat templates for pump.fun announcements ---
KRABS_CHAT_TEMPLATES = {
    "buy": [
        "{crab} just grabbed more ${token} \U0001f980",
        "{crab} bought the dip on ${token}! money money money \U0001f4b0",
        "{crab} is loading up on ${token} \U0001f980",
        "{crab} scooped some ${token} \U0001f980\U0001f4b0",
    ],
    "sell": [
        "{crab} cashed out ${token} for {pnl} SOL \U0001f911",
        "{crab} secured the bag! {pnl} SOL from ${token} \U0001f4b0",
        "{crab} sold ${token} — {pnl} SOL in the register \U0001f980",
        "{crab} took profits on ${token}: {pnl} SOL \U0001f911",
    ],
    "evolve": [
        "Gen {gen} complete! {best} was our MVP \U0001f451 evolving...",
        "evolution time! Gen {gen} done — {best} led the crew \U0001f9ec\U0001f980",
        "Gen {gen} in the books! {best} crushed it \U0001f451 new generation begins!",
    ],
}

EVOLUTION_TWEET_TEMPLATES = [
    "\U0001f9ec Gen {gen} complete!\n\n\U0001f451 MVP: {best}\n\U0001f4c8 Sharpe: {sharpe}\n\U0001f4b0 PnL: {pnl} SOL\n\U0001f3af W/L: {wins}W/{losses}L\n\n$PINCHIN on @pumpdotfun\n{link}",
    "\U0001f980 Evolution #{gen} done!\n\n{best} led the crew\nSharpe {sharpe} | {pnl} SOL | {wins}W {losses}L\n\nThe crabs are getting smarter \U0001f9ec\n\n$PINCHIN\n{link}",
    "Gen {gen} \u2192 Gen {next_gen}\n\n\U0001f451 {best} was our MVP\n{pnl} SOL | Sharpe {sharpe}\n{wins} wins, {losses} losses\n\nevolving... \U0001f980\U0001f9ec\n\n$PINCHIN\n{link}",
]

# --- Day/Night cycle ---
DAY_TICKS = 600    # ~90 seconds of daytime
NIGHT_TICKS = 500  # ~75 seconds of nighttime

# Night colors
NIGHT_WATER = "\033[38;5;18m"
NIGHT_SAND = "\033[38;5;94m"
STAR_COLOR = "\033[38;5;229m"
MOON_COLOR = "\033[38;5;230m"

MOON = [
    " _ ",
    "( )",
    " ~ ",
]

# Girlfriend sprites
GF_CRAB_R = "(*'.'*)>"
GF_CRAB_L = "<(*'.'*)"

GF_NAMES = [
    "Clarice", "Pincette", "Lobsterella", "Bubblina",
    "Shelby", "Crustatia", "Snappina", "Coralina",
]

GF_COLORS = [PINK, "\033[38;5;218m", "\033[38;5;211m", "\033[38;5;219m", "\033[38;5;213m", "\033[38;5;217m"]

# Dance sprites
DANCE_CRAB_UP = r"\(^o^)/"
DANCE_CRAB_DN = " (^.^) "
DANCE_GF_UP = r"\(*o*)/"
DANCE_GF_DN = " (*.*) "

MUSIC_NOTES = ["~", "#", "*"]

BPM_PRESETS = [0, 80, 90, 100, 110, 120, 128, 140, 150, 160]


class Crab:
    def __init__(self, x, y, bounds_w, bounds_h, color, desk_x, desk_y, dealer_x, dealer_y):
        self.x = x
        self.y = y
        self.bounds_w = bounds_w
        self.bounds_h = bounds_h
        self.color = color
        self.facing_right = random.choice([True, False])
        self.name = random.choice(NAMES)
        self.mood = random.choice(MOODS)
        self.big = random.random() > 0.4
        self.action_timer = 0
        self.action_msg = ""
        self.bubble_x = -1
        self.bubble_y = -1
        self.bubble_life = 0

        # State machine:
        # idle -> walking_to_desk -> trading
        #   -> (win) walking_to_dealer -> buying_car -> driving -> idle
        #   -> (lose) walking_to_smoke -> smoking -> idle
        self.state = "idle"
        self.state_timer = 0
        self.trade_msg = ""
        self.smoke_puff_timer = 0
        self.target_x = 0
        self.target_y = 0
        self.desk_x = desk_x
        self.desk_y = desk_y
        self.dealer_x = dealer_x
        self.dealer_y = dealer_y
        self.idle_countdown = random.randint(30, 120)
        self.car_name = ""
        self.wins = 0
        self.losses = 0
        self.ogle_timer = 0
        self.prev_state = ""
        self.dance_tick = 0
        self.wallet = ""
        self.wallet_balance = 0.0
        self.token_balances = {}  # mint -> balance
        self.summon_angle = 0.0
        self.auto_trader = None  # set by main()
        self.trade_thought = ""  # e.g. "SL@-12% sell 100%"
        self.trade_thought_timer = 0  # ticks remaining to show
        self.star_target_x = 0
        self.star_target_y = 0
        # Burn animation state
        self.burn_origin_x = 0
        self.burn_origin_y = 0
        self.fireball_active = False
        self.fireball_x = 0
        self.fireball_y = 0

    @property
    def width(self):
        if self.state == "locked_in":
            return 10
        if self.state in ("smoking", "ogling"):
            return 8
        if self.state == "driving":
            return 11
        if self.state in ("celebrating", "dancing", "summoning", "meditating", "starring", "burning"):
            return 7
        return 10 if self.big else 7

    @property
    def height(self):
        if self.state == "locked_in":
            return 3
        if self.state in ("smoking", "ogling", "celebrating", "dancing", "summoning", "meditating", "starring", "burning"):
            return 1
        if self.state == "driving":
            return 2
        return 3 if self.big else 1

    @property
    def pinchin_balance(self):
        return self.token_balances.get(PINCHIN_CONTRACT, 0.0)

    @pinchin_balance.setter
    def pinchin_balance(self, value):
        self.token_balances[PINCHIN_CONTRACT] = value

    def _walk_toward(self, tx, ty):
        dx = 0
        dy = 0
        if self.x < tx:
            dx = min(2, tx - self.x)
            self.facing_right = True
        elif self.x > tx:
            dx = max(-2, tx - self.x)
            self.facing_right = False
        if self.y < ty:
            dy = 1
        elif self.y > ty:
            dy = -1
        self.x = max(0, min(self.bounds_w - self.width, self.x + dx))
        self.y = max(0, min(self.bounds_h - self.height, self.y + dy))
        return abs(self.x - tx) <= 2 and abs(self.y - ty) <= 1

    def check_swimmers(self, swimmers):
        """Check if a swimmer is nearby and get distracted."""
        if self.state in ("ogling", "driving", "celebrating", "dancing", "summoning", "meditating", "starring", "burning", "walking_to_burn", "locked_in"):
            return
        for swimmer in swimmers:
            if not swimmer.active:
                continue
            # Check if swimmer is roughly overhead (within range)
            dist_x = abs((swimmer.x + 4) - (self.x + self.width // 2))
            if dist_x < 15 and random.random() < 0.08:
                self.prev_state = self.state
                self.state = "ogling"
                self.ogle_timer = random.randint(12, 25)
                self.mood = "distracted"
                self.action_msg = random.choice(OGLE_MSGS)
                self.action_timer = 999
                # Face toward the swimmer
                self.facing_right = swimmer.x > self.x
                return

    def update(self):
        # Bastian is the snipe gatekeeper
        if self.name == "Bastian":
            self.state = "dancing"
            self.dance_tick += 1
            self.x = 2
            self.y = self.bounds_h - 3
            self.mood = "on guard"
            if self.dance_tick % 40 == 0:
                self.action_msg = random.choice(["watching...", "scanning signals", "risk check", "gatekeeper", "evaluating"])
                self.action_timer = 20
            if self.action_timer > 0:
                self.action_timer -= 1
            return

        if self.state == "locked_in":
            self.state_timer += 1
            if self.action_timer > 0:
                self.action_timer -= 1
            elif random.random() < 0.02:
                self.action_msg = random.choice([
                    "locked in", "analyzing charts", "scanning signals",
                    "crunching numbers", "in the zone", "reading order flow",
                    "full focus", "printing money", "diamond claws",
                ])
                self.action_timer = 20
            return  # No movement, no state transitions

        if self.state == "ogling":
            self.ogle_timer -= 1
            if self.ogle_timer <= 0:
                # Snap out of it
                self.state = self.prev_state if self.prev_state else "idle"
                self.mood = random.choice(["embarrassed", "flustered", "playing it cool"])
                self.action_msg = "snapped out of it"
                self.action_timer = 10
            return

        if self.state == "idle":
            dx = random.choice([-2, -1, -1, 0, 0, 0, 1, 1, 2])
            dy = random.choice([-1, 0, 0, 0, 0, 1])
            if dx > 0:
                self.facing_right = True
            elif dx < 0:
                self.facing_right = False
            self.x = max(0, min(self.bounds_w - self.width, self.x + dx))
            self.y = max(0, min(self.bounds_h - self.height, self.y + dy))

            if random.random() < 0.02:
                self.mood = random.choice(MOODS)
            if self.action_timer > 0:
                self.action_timer -= 1
            elif random.random() < 0.015:
                self.action_msg = random.choice(ACTIONS)
                self.action_timer = 12

            self.idle_countdown -= 1
            if self.idle_countdown <= 0:
                # Pull out phone and trade from wherever they are
                self.state = "trading"
                self.state_timer = random.randint(40, 80)
                self.trade_msg = random.choice(TRADE_MSGS)
                self.mood = "on phone"
                self.action_msg = self.trade_msg
                self.action_timer = 999

        elif self.state == "walking_to_desk":
            # Legacy - redirect to phone trading
            self.state = "trading"
            self.state_timer = random.randint(40, 80)
            self.mood = "on phone"

        elif self.state == "trading":
            self.state_timer -= 1
            if self.state_timer <= 0:
                # Try a real trade if crab has a wallet
                if self.wallet and self.auto_trader:
                    self.auto_trader.decide_and_trade(self.name)

                # Coin flip: win or lose?
                if random.random() < 0.45:
                    # WIN - go to dealership to buy a car!
                    self.wins += 1
                    CRAB_WL[self.name] = {"wins": self.wins, "losses": self.losses}
                    save_wl(CRAB_WL)
                    self.car_name = random.choice(CAR_NAMES)
                    self.state = "walking_to_dealer"
                    self.target_x = self.dealer_x + DEALER_WIDTH // 2 - 3
                    self.target_y = self.dealer_y + DEALER_HEIGHT
                    self.mood = "cashed out"
                    self.action_msg = f"{random.choice(WIN_MSGS)} Heading to the lot!"
                    self.action_timer = 999
                else:
                    # LOSE - go smoke it off in the middle of the screen
                    self.losses += 1
                    CRAB_WL[self.name] = {"wins": self.wins, "losses": self.losses}
                    save_wl(CRAB_WL)
                    self.state = "walking_to_smoke"
                    self.target_x = self.bounds_w // 2 + random.randint(-5, 5)
                    self.target_y = self.bounds_h // 2 + random.randint(4, 7)
                    self.mood = "devastated"
                    self.action_msg = random.choice(LOSE_MSGS)
                    self.action_timer = 999

        elif self.state == "walking_to_dealer":
            if self._walk_toward(self.target_x, self.target_y):
                self.state = "buying_car"
                self.state_timer = random.randint(10, 20)
                self.mood = "shopping"
                self.action_msg = f"buying {self.car_name}..."
                self.action_timer = 999

        elif self.state == "buying_car":
            self.state_timer -= 1
            if self.state_timer <= 0:
                self.state = "driving"
                self.state_timer = random.randint(40, 80)
                self.mood = "flexing"
                self.action_msg = f"driving {self.car_name}!"
                self.action_timer = 999
                self.facing_right = random.choice([True, False])

        elif self.state == "driving":
            # Zoom around fast!
            speed = random.choice([3, 4, 4, 5])
            if self.facing_right:
                self.x += speed
                if self.x >= self.bounds_w - self.width:
                    self.x = self.bounds_w - self.width
                    self.facing_right = False
            else:
                self.x -= speed
                if self.x <= 0:
                    self.x = 0
                    self.facing_right = True

            # Slight vertical drift
            if random.random() < 0.1:
                self.y += random.choice([-1, 1])
                self.y = max(0, min(self.bounds_h - self.height, self.y))

            self.state_timer -= 1
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(60, 200)
                self.mood = random.choice(["rich", "balling", "flexing", "on top"])
                self.action_msg = f"parked the {self.car_name}"
                self.action_timer = 15

        elif self.state == "walking_to_smoke":
            if self._walk_toward(self.target_x, self.target_y):
                self.state = "smoking"
                self.state_timer = random.randint(25, 50)
                self.mood = "stressed"
                self.action_msg = "smoking it off..."
                self.action_timer = 999
                self.smoke_puff_timer = 0

        elif self.state == "smoking":
            self.state_timer -= 1
            self.smoke_puff_timer += 1
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(60, 200)
                self.mood = random.choice(["coping", "chill", "plotting comeback", "back to it"])
                self.action_msg = ""
                self.action_timer = 0

        elif self.state == "celebrating":
            self.state_timer -= 1
            # Little dance jitter
            if random.random() < 0.3:
                self.x += random.choice([-1, 0, 1])
                self.x = max(0, min(self.bounds_w - self.width, self.x))
            if random.random() < 0.15:
                self.facing_right = not self.facing_right
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(30, 80)
                self.mood = random.choice(["hyped", "euphoric", "diamond claws", "feeling rich"])
                self.action_msg = "back to the grind"
                self.action_timer = 15

        elif self.state == "dancing":
            self.dance_tick += 1

        elif self.state == "summoning":
            self.state_timer -= 1
            self.summon_angle += 0.06
            # Orbit position calculated in main loop (needs board coords)
            if self.state_timer % 8 < 4:
                self.facing_right = True
            else:
                self.facing_right = False
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(30, 80)
                self.mood = random.choice(["enlightened", "charged up", "mystic", "moon-blessed"])
                self.action_msg = "the ritual is complete"
                self.action_timer = 20

        elif self.state == "meditating":
            self.state_timer -= 1
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(60, 200)
                self.mood = random.choice(["enlightened", "at peace", "zen", "transcended", "centered"])
                self.action_msg = "found inner peace"
                self.action_timer = 20

        elif self.state == "starring":
            self.state_timer -= 1
            arrived = self._walk_toward(self.star_target_x, self.star_target_y)
            if arrived and self.state_timer % 8 < 4:
                self.facing_right = not self.facing_right
            if self.state_timer <= 0:
                self.state = "idle"
                self.idle_countdown = random.randint(30, 80)
                self.mood = random.choice(["star-blessed", "cosmically aligned", "astral", "hexed up"])
                self.action_msg = "the star fades..."
                self.action_timer = 20

        elif self.state == "walking_to_burn":
            target_x = self.bounds_w // 2 - 3
            target_y = 4
            if self._walk_toward(target_x, target_y):
                self.state = "burning"
                self.state_timer = 40  # casting duration
                self.mood = "casting"
                self.action_msg = "BURN!"
                self.action_timer = 999
                self.fireball_active = False

        elif self.state == "burning":
            self.state_timer -= 1
            # Phase 1 (ticks 40-25): casting pose, arms up
            # Phase 2 (ticks 24-15): launch fireball upward
            # Phase 3 (ticks 14-0): walk back to origin
            if self.state_timer == 24:
                # Launch fireball from crab position
                self.fireball_active = True
                self.fireball_x = self.x + self.width // 2
                self.fireball_y = self.y - 1
            if self.fireball_active and self.fireball_y > 0:
                self.fireball_y -= 2  # rises fast
                if self.fireball_y <= 1:
                    self.fireball_active = False  # off screen
            if self.state_timer <= 14:
                # Walk back to origin
                arrived = self._walk_toward(self.burn_origin_x, self.burn_origin_y)
                if arrived or self.state_timer <= 0:
                    self.state = "idle"
                    self.idle_countdown = random.randint(30, 80)
                    self.mood = random.choice(["pyro", "ashen", "scorched", "fire-blessed"])
                    self.action_msg = "tokens burned"
                    self.action_timer = 20
                    self.fireball_active = False

        if self.state == "idle":
            if self.bubble_life > 0:
                self.bubble_life -= 1
                self.bubble_y -= 1
            elif random.random() < 0.03:
                self.bubble_x = self.x + self.width // 2
                self.bubble_y = self.y - 1
                self.bubble_life = 3

    def render_lines(self):
        if self.state == "locked_in":
            if self.state_timer % 16 < 8:
                return LOCKED_IN_R_1 if self.facing_right else LOCKED_IN_L_1
            else:
                return LOCKED_IN_R_2 if self.facing_right else LOCKED_IN_L_2
        if self.state == "ogling":
            return [DISTRACTED_CRAB_R if self.facing_right else DISTRACTED_CRAB_L]
        if self.state == "celebrating":
            if self.state_timer % 6 < 3:
                return [CELEBRATE_CRAB_1]
            else:
                return [CELEBRATE_CRAB_2]
        if self.state == "summoning":
            if self.state_timer % 6 < 3:
                return [SUMMON_CRAB_1]
            else:
                return [SUMMON_CRAB_2]
        if self.state == "meditating":
            if self.state_timer % 12 < 6:
                return [MEDITATE_CRAB_1]
            else:
                return [MEDITATE_CRAB_2]
        if self.state == "starring":
            if self.state_timer % 6 < 3:
                return [SEBAS_CRAB_1]
            else:
                return [SEBAS_CRAB_2]
        if self.state in ("burning", "walking_to_burn"):
            if self.state == "burning" and self.state_timer > 24:
                return [BURN_CRAB_1]  # arms up casting
            else:
                return [BURN_CRAB_2]  # arms forward / walking
        if self.state == "dancing":
            if self.dance_tick % 4 < 2:
                return [DANCE_CRAB_UP]
            else:
                return [DANCE_CRAB_DN]
        if self.state == "smoking":
            return [SMOKE_CRAB_R if self.facing_right else SMOKE_CRAB_L]
        if self.state == "driving":
            return CAR_CRAB_R if self.facing_right else CAR_CRAB_L
        if self.big:
            return CRAB_RIGHT if self.facing_right else CRAB_LEFT
        return [MINI_CRAB_R if self.facing_right else MINI_CRAB_L]


class Swimmer:
    def __init__(self, width, water_rows):
        self.width = 8
        self.bounds_w = width
        self.water_rows = water_rows
        self.color = random.choice(SWIMMER_COLORS)
        self.name = random.choice(SWIMMER_NAMES)
        self.active = False
        self.x = 0
        self.y = 0
        self.facing_right = True
        self.stroke_frame = 0
        self.cooldown = random.randint(40, 150)

    def update(self):
        if not self.active:
            self.cooldown -= 1
            if self.cooldown <= 0:
                # Jump in!
                self.active = True
                self.facing_right = random.choice([True, False])
                if self.facing_right:
                    self.x = -self.width
                else:
                    self.x = self.bounds_w
                self.y = random.randint(0, max(0, self.water_rows - 2))
                self.stroke_frame = 0
            return

        # Swim across
        speed = random.choice([2, 2, 3])
        if self.facing_right:
            self.x += speed
            if self.x > self.bounds_w + 5:
                self.active = False
                self.cooldown = random.randint(60, 200)
        else:
            self.x -= speed
            if self.x < -self.width - 5:
                self.active = False
                self.cooldown = random.randint(60, 200)

        self.stroke_frame += 1

    def render_lines(self):
        if self.stroke_frame % 6 < 3:
            return SWIMMER_R if self.facing_right else SWIMMER_L
        else:
            return SWIMMER_R2 if self.facing_right else SWIMMER_L2


class Girlfriend:
    def __init__(self, bounds_w, bounds_h, color, name, target_crab):
        self.bounds_w = bounds_w
        self.bounds_h = bounds_h
        self.color = color
        self.name = name
        self.target_crab = target_crab
        self.state = "entering"
        self.x = 0
        self.y = target_crab.y
        self.facing_right = True
        self.dance_frame = 0
        self.vel_x = 0
        self.vel_y = 0

        if random.random() < 0.5:
            self.x = -8
            self.facing_right = True
        else:
            self.x = bounds_w
            self.facing_right = False

    @property
    def width(self):
        return 7 if self.state == "dancing" else 8

    def update(self):
        if self.state == "entering":
            if self.x < self.target_crab.x:
                tx = self.target_crab.x - self.width - 1
            else:
                tx = self.target_crab.x + self.target_crab.width + 1
            ty = self.target_crab.y

            dx = 0
            if self.x < tx:
                dx = min(2, tx - self.x)
                self.facing_right = True
            elif self.x > tx:
                dx = max(-2, tx - self.x)
                self.facing_right = False
            dy = 0
            if self.y < ty:
                dy = 1
            elif self.y > ty:
                dy = -1

            self.x = max(0, min(self.bounds_w - self.width, self.x + dx))
            self.y = max(0, min(self.bounds_h - 1, self.y + dy))

            # Close enough and crab is available?
            if self.x < self.target_crab.x:
                gap = self.target_crab.x - (self.x + self.width)
            else:
                gap = self.x - (self.target_crab.x + self.target_crab.width)
            if gap <= 3 and abs(self.y - self.target_crab.y) <= 1:
                if self.target_crab.state in ("idle", "celebrating"):
                    self.state = "dancing"
                    self.vel_x = random.choice([-2, -1, 1, 2])
                    self.vel_y = random.choice([-1, 1])
                    self.target_crab.state = "dancing"
                    self.target_crab.dance_tick = 0
                    self.target_crab.mood = "in love"
                    self.target_crab.action_msg = f"dancing with {self.name}!"
                    self.target_crab.action_timer = 9999
                    self.facing_right = self.x < self.target_crab.x
                    self.target_crab.facing_right = not self.facing_right

        elif self.state == "dancing":
            self.dance_frame += 1

            # Float around as a pair
            self.x += self.vel_x
            self.target_crab.x += self.vel_x
            self.y += self.vel_y
            self.target_crab.y += self.vel_y

            # Occasional drift change
            if random.random() < 0.03:
                self.vel_x = random.choice([-2, -1, 1, 2])
            if random.random() < 0.05:
                self.vel_y = random.choice([-1, 0, 1])

            # Bounce off walls
            water_top = max(2, self.bounds_h // 8) + 1
            pair_left = min(self.x, self.target_crab.x)
            pair_right = max(self.x + self.width, self.target_crab.x + self.target_crab.width)

            if pair_left <= 1:
                self.vel_x = abs(self.vel_x) or 1
                shift = 2 - pair_left
                self.x += shift
                self.target_crab.x += shift
            elif pair_right >= self.bounds_w - 1:
                self.vel_x = -(abs(self.vel_x) or 1)
                shift = (self.bounds_w - 2) - pair_right
                self.x += shift
                self.target_crab.x += shift

            if self.y <= water_top:
                self.vel_y = abs(self.vel_y) or 1
                self.y = water_top + 1
                self.target_crab.y = self.y
            elif self.y >= self.bounds_h - 3:
                self.vel_y = -(abs(self.vel_y) or 1)
                self.y = self.bounds_h - 4
                self.target_crab.y = self.y

            # Clamp
            self.x = max(0, min(self.bounds_w - self.width, self.x))
            self.target_crab.x = max(0, min(self.bounds_w - self.target_crab.width, self.target_crab.x))

            # Keep facing each other
            self.facing_right = self.x < self.target_crab.x
            self.target_crab.facing_right = not self.facing_right

        elif self.state == "leaving":
            if self.facing_right:
                self.x += 3
            else:
                self.x -= 3
            if self.x > self.bounds_w + 10 or self.x < -15:
                self.state = "gone"

    def render_lines(self):
        if self.state == "dancing":
            if self.dance_frame % 4 < 2:
                return [DANCE_GF_UP]
            else:
                return [DANCE_GF_DN]
        return [GF_CRAB_R if self.facing_right else GF_CRAB_L]


def clear_screen():
    sys.stdout.write("\033[2J\033[H")


def hide_cursor():
    sys.stdout.write("\033[?25l")
    sys.stdout.write("\033[?7l")  # disable line wrapping to prevent scroll glitch


def show_cursor():
    sys.stdout.write("\033[?7h")  # re-enable line wrapping
    sys.stdout.write("\033[?25h")


BANNER_TEXT = "   6 ASCII crabs with AI-evolved trading instincts! each crab has its own solana wallet and trades on-chain via jupiter. their strategies were evolved by OpenEvolve -- smart dip buying, dynamic stop-losses, volatility-adjusted sizing, and momentum spike selling. live leaderboard tracks who's winning.   ///   $PINCHIN   ///   github.com/SAMBAS123/pinchin-crabs   ///   "

EVOLUTION_HISTORY_FILE = os.path.expanduser("~/.pinchin_evolution_history.json")
_evo_cache = {"data": None, "mtime": 0}

def _get_evolution_display():
    """Build evolution history lines for the panel."""
    # Cache — reload only when file changes
    try:
        mt = os.path.getmtime(EVOLUTION_HISTORY_FILE)
        if mt != _evo_cache["mtime"]:
            with open(EVOLUTION_HISTORY_FILE) as f:
                _evo_cache["data"] = json.load(f)
            _evo_cache["mtime"] = mt
    except Exception:
        pass

    history = _evo_cache["data"]
    if not history:
        return [(" no evolution data yet", DIM)]

    scores = [h["score"] for h in history]
    best = max(scores)
    runs = max(h.get("run", 1) for h in history)

    # ASCII sparkline of score progression
    bars = "_.oO0@"
    spark = ""
    for s in scores:
        idx = min(len(bars) - 1, int(s * (len(bars) - 1)))
        spark += bars[idx]

    lines = []
    lines.append((f" Best: {best:.3f}  Runs: {runs}", GREEN if best > 0.6 else YELLOW))
    lines.append((f" [{spark}] {scores[0]:.2f}->{best:.2f}", CYAN))

    # Show latest milestone label
    latest = history[-1]
    label = latest.get("label", "")
    if label:
        lines.append((f" >> {label}", DIM))

    return lines


# --- Scene Renderers for Location System ---

def _draw_location(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker, auto_trader, holder_feed=None):
    """Dispatch to the correct scene renderer based on camera location."""
    if world.camera == "lab":
        _draw_lab(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker)
    elif world.camera == "graveyard":
        _draw_graveyard(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker)
    elif world.camera == "bank":
        _draw_bank(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker, auto_trader)
    elif world.camera == "holders":
        _draw_holders(grid, color_grid, width, height, tick_count, world, crabs, holder_feed)


def _draw_lab(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker):
    """Evolution Chamber scene — fitness rankings, evolution progress, crabs present."""
    # Dark sparse background (seeded so dots don't flicker every frame)
    random.seed(42 + tick_count // 8)
    for y in range(height):
        for x in range(width):
            if random.random() < 0.02:
                grid[y][x] = random.choice([".", "+"])
                color_grid[y][x] = DIM + CYAN
            else:
                grid[y][x] = " "
                color_grid[y][x] = ""
    random.seed()

    # --- Lab decorations (drawn before data content) ---

    # DNA Helix (left side, col 2-8, rows 4-13)
    helix_phase = tick_count // 4
    for hy in range(10):
        row_y = 4 + hy
        if row_y >= height - 6:
            break
        phase = (hy + helix_phase) % 6
        # Two strands weaving around each other
        if phase == 0:
            pairs = [(2, "\\"), (7, "/")]
            colors = [DIM + CYAN, DIM + GREEN]
        elif phase == 1:
            pairs = [(3, "-"), (6, "-")]
            colors = [DIM + CYAN, DIM + GREEN]
        elif phase == 2:
            pairs = [(4, "X"), (5, "X")]
            colors = [CYAN, GREEN]
        elif phase == 3:
            pairs = [(5, "/"), (4, "\\")]
            colors = [DIM + GREEN, DIM + CYAN]
        elif phase == 4:
            pairs = [(6, "-"), (3, "-")]
            colors = [DIM + GREEN, DIM + CYAN]
        else:
            pairs = [(7, "X"), (2, "X")]
            colors = [GREEN, CYAN]
        for (px, ch), clr in zip(pairs, colors):
            if 0 <= px < width and 0 <= row_y < height:
                grid[row_y][px] = ch
                color_grid[row_y][px] = clr

    # Beakers (bottom corners, animated bubbles)
    beaker_art = [
        " |  |",
        " |~~|",
        " |  |",
        " \\__/",
    ]
    beaker_positions = [(2, height - 7), (width - 8, height - 7)]
    for bx, by in beaker_positions:
        if by < 4 or bx + 5 >= width:
            continue
        for ri, line in enumerate(beaker_art):
            for ci, ch in enumerate(line):
                px = bx + ci
                py = by + ri
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = DIM + CYAN
        # Rising bubbles above beaker
        bubble_offset = tick_count // 3
        for bi in range(3):
            bub_y = by - 1 - ((bi + bubble_offset) % 4)
            bub_x = bx + 2 + (bi % 2)
            bub_ch = "o" if bi % 2 == 0 else "."
            if 0 <= bub_x < width and 2 <= bub_y < by:
                grid[bub_y][bub_x] = bub_ch
                color_grid[bub_y][bub_x] = GREEN

    # Blinking equipment (scattered mid-section)
    equip_items = ["[=]", "{#}", "[*]", "[=]", "{#}"]
    random.seed(99)
    equip_spots = [(random.randint(12, width - 6), random.randint(3, height - 8)) for _ in equip_items]
    random.seed()
    blink_on = (tick_count // 6) % 2 == 0
    for (ex, ey), item in zip(equip_spots, equip_items):
        clr = (BOLD + GREEN) if blink_on else (DIM + CYAN)
        for ci, ch in enumerate(item):
            px = ex + ci
            if 0 <= px < width and 0 <= ey < height:
                grid[ey][px] = ch
                color_grid[ey][px] = clr

    # Metal floor grate (bottom 2 rows)
    random.seed(55)
    for fy in range(max(0, height - 2), height):
        for fx in range(width):
            grid[fy][fx] = random.choice(["=", "-", "_"])
            color_grid[fy][fx] = DIM + GRAY
    random.seed()

    # --- End lab decorations ---

    # Header
    header = " EVOLUTION CHAMBER "
    hx = (width - len(header)) // 2
    for ci, ch in enumerate(header):
        px = hx + ci
        if 0 <= px < width:
            grid[1][px] = ch
            color_grid[1][px] = BOLD + GREEN

    if not gen_tracker:
        return

    gt = gen_tracker
    # Generation number + progress bar
    done = gt.trades_completed
    total = gt.trades_to_evolve
    bar_w = min(30, width - 20)
    filled = int(bar_w * min(done, total) / max(total, 1))
    bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
    gen_line = f" Gen {gt.number}  [{bar}] {done}/{total} trades"
    for ci, ch in enumerate(gen_line[:width - 4]):
        px = 2 + ci
        if 0 <= px < width:
            grid[3][px] = ch
            color_grid[3][px] = BOLD + CYAN

    # Fitness rankings table
    ranked = gt.get_ranked()
    crab_colors = {}
    for crab in crabs:
        crab_colors[crab.name] = crab.color

    table_header = " RANK  CRAB        SHARPE    PNL       W/L"
    for ci, ch in enumerate(table_header[:width - 4]):
        px = 2 + ci
        if 0 <= px < width:
            grid[5][px] = ch
            color_grid[5][px] = DIM + WHITE

    sep_line = " " + "-" * min(44, width - 6)
    for ci, ch in enumerate(sep_line[:width - 4]):
        px = 2 + ci
        if 0 <= px < width:
            grid[6][px] = ch
            color_grid[6][px] = DIM

    for ei, r in enumerate(ranked[:7]):
        row_y = 7 + ei
        if row_y >= height - 6:
            break
        name = r["name"]
        sharpe = r["sharpe"]
        pnl = r["pnl"]
        marker = r["marker"]
        pnl_sign = "+" if pnl >= 0 else ""
        rank_str = f"  #{ei+1}"
        entry = f"{rank_str:<6s}{name:<12s}{sharpe:>6.2f}  {pnl_sign}{pnl:>7.3f} SOL  {r['wins']}W/{r['losses']}L {marker}"
        pnl_color = GREEN if pnl >= 0 else RED
        ccolor = crab_colors.get(name, YELLOW)
        for ci, ch in enumerate(entry[:width - 4]):
            px = 2 + ci
            if 0 <= px < width and 0 <= row_y < height:
                grid[row_y][px] = ch
                if ci < 6:
                    color_grid[row_y][px] = BOLD + YELLOW if ei == 0 else DIM
                elif ci < 18:
                    color_grid[row_y][px] = BOLD + ccolor if ei == 0 else ccolor
                elif ci < 26:
                    color_grid[row_y][px] = CYAN
                elif ci < 40:
                    color_grid[row_y][px] = BOLD + pnl_color
                else:
                    color_grid[row_y][px] = DIM

    # Evolution preview: who dies / who parents
    preview_y = 7 + min(len(ranked), 7) + 1
    if len(ranked) >= 2 and preview_y < height - 4:
        has_trades = [r for r in ranked if r["wins"] + r["losses"] > 0]
        if len(has_trades) >= 2:
            best = has_trades[0]
            worst = has_trades[-1]
            best_color = crab_colors.get(best["name"], GREEN)
            worst_color = crab_colors.get(worst["name"], RED)
            evo_line1 = f" Next evolution: {worst['name']} dies -> {best['name']} parents"
            for ci, ch in enumerate(evo_line1[:width - 4]):
                px = 2 + ci
                if 0 <= px < width and preview_y < height:
                    grid[preview_y][px] = ch
                    if ci < 18:
                        color_grid[preview_y][px] = DIM
                    elif ci < 18 + len(worst["name"]):
                        color_grid[preview_y][px] = BOLD + worst_color
                    elif "parents" in evo_line1[ci:ci+8]:
                        color_grid[preview_y][px] = DIM
                    else:
                        color_grid[preview_y][px] = DIM
            # Color the worst name red, best name green explicitly
            worst_start = evo_line1.find(worst["name"])
            best_start = evo_line1.find(best["name"])
            if worst_start >= 0:
                for ci in range(len(worst["name"])):
                    px = 2 + worst_start + ci
                    if 0 <= px < width and preview_y < height:
                        color_grid[preview_y][px] = BOLD + RED
            if best_start >= 0:
                for ci in range(len(best["name"])):
                    px = 2 + best_start + ci
                    if 0 <= px < width and preview_y < height:
                        color_grid[preview_y][px] = BOLD + GREEN

    # Crabs present at lab with location dialogue
    present = world.crabs_at("lab")
    crab_y = height - 5
    if present and crab_y > preview_y + 2:
        for pi, pname in enumerate(present[:4]):
            cx = 4 + pi * (width // 5)
            # Mini crab representation
            ccolor = crab_colors.get(pname, YELLOW)
            crab_art = ["(o  o)", " )__) "]
            for ri, line in enumerate(crab_art):
                for ci, ch in enumerate(line):
                    px = cx + ci
                    py = crab_y + ri
                    if 0 <= px < width and 0 <= py < height:
                        grid[py][px] = ch
                        color_grid[py][px] = ccolor
            # Name below
            for ci, ch in enumerate(pname[:8]):
                px = cx + ci
                py = crab_y + 2
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = ccolor
            # Dialogue
            dialogue = get_location_dialogue(pname, "lab", gen_tracker)
            for ci, ch in enumerate(dialogue[:width // 5 - 2]):
                px = cx + ci
                py = crab_y + 3
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = DIM + CYAN

    # Footer: prev gen recap
    footer_y = height - 1
    if footer_y > 0:
        footer = f" {gen_tracker.prev_gen_recap}"
        for ci, ch in enumerate(footer[:width - 4]):
            px = 2 + ci
            if 0 <= px < width:
                grid[footer_y][px] = ch
                color_grid[footer_y][px] = DIM


def _draw_graveyard(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker):
    """Fallen Strategies graveyard scene — starfield, tombstones, mourning crabs."""
    # Starfield sky (top 60%)
    sky_end = int(height * 0.6)
    random.seed(77 + tick_count // 8)
    for y in range(sky_end):
        for x in range(width):
            if random.random() < 0.03:
                grid[y][x] = random.choice(["*", ".", "+"])
                color_grid[y][x] = DIM + WHITE
            else:
                grid[y][x] = " "
                color_grid[y][x] = ""
    random.seed()

    # Grass floor
    for y in range(sky_end, height):
        for x in range(width):
            if random.random() < 0.3:
                grid[y][x] = random.choice([",", "'", ".", "`"])
                color_grid[y][x] = DIM + GREEN
            else:
                grid[y][x] = " "
                color_grid[y][x] = ""

    # --- Graveyard decorations ---

    # Moon (top-right)
    for ri, line in enumerate(MOON):
        for ci, ch in enumerate(line):
            px = width - 8 + ci
            py = 1 + ri
            if 0 <= px < width and 0 <= py < height and ch != " ":
                grid[py][px] = ch
                color_grid[py][px] = MOON_COLOR

    # Dead trees (seeded positions in sky area)
    tree_art = [
        "  /\\  ",
        " /  \\ ",
        "  ||  ",
        "  ||  ",
    ]
    random.seed(88)
    tree_positions = [(random.randint(2, width - 10), sky_end - 4) for _ in range(min(3, width // 25))]
    random.seed()
    for tx, ty in tree_positions:
        if ty < 2:
            continue
        for ri, line in enumerate(tree_art):
            for ci, ch in enumerate(line):
                px = tx + ci
                py = ty + ri
                if 0 <= px < width and 0 <= py < height and ch != " ":
                    grid[py][px] = ch
                    color_grid[py][px] = DIM + BROWN

    # Fence (across grass line at sky_end)
    fence_y = sky_end
    if 0 <= fence_y < height:
        for fx in range(width):
            pat = fx % 4
            if pat == 0:
                grid[fence_y][fx] = "|"
            elif pat == 1 or pat == 2:
                grid[fence_y][fx] = "-"
            else:
                grid[fence_y][fx] = "|"
            color_grid[fence_y][fx] = BROWN

    # Crow on fence (hops position slowly)
    crow_art = ">v<"
    crow_x = (tick_count // 16) % max(1, width - 5) + 1
    crow_y = fence_y - 1
    if 0 <= crow_y < height:
        for ci, ch in enumerate(crow_art):
            px = crow_x + ci
            if 0 <= px < width:
                grid[crow_y][px] = ch
                color_grid[crow_y][px] = DIM + WHITE

    # Fog (3 rows around sky_end, rolling slowly)
    fog_offset = tick_count // 10
    random.seed(66 + fog_offset)
    for fy_off in range(-1, 2):
        fog_y = sky_end + fy_off
        if 0 <= fog_y < height:
            for fx in range(width):
                if random.random() < 0.12:
                    ch = random.choice(["~", ".", "-"])
                    grid[fog_y][fx] = ch
                    color_grid[fog_y][fx] = DIM + WHITE
    random.seed()

    # --- End graveyard decorations ---

    # Header
    header = " FALLEN STRATEGIES "
    hx = (width - len(header)) // 2
    for ci, ch in enumerate(header):
        px = hx + ci
        if 0 <= px < width:
            grid[1][px] = ch
            color_grid[1][px] = BOLD + RED

    # Tombstones
    stones = world.graveyard
    if not stones:
        msg = "No fallen strategies yet... evolution hasn't claimed anyone."
        mx = (width - len(msg)) // 2
        for ci, ch in enumerate(msg[:width - 4]):
            px = mx + ci
            if 0 <= px < width and 4 < height:
                grid[4][px] = ch
                color_grid[4][px] = DIM
    else:
        # Show up to 6 tombstones across the screen
        max_stones = min(6, len(stones), width // 14)
        show_stones = stones[-max_stones:]
        spacing = max(14, width // max(max_stones, 1))
        for si, stone in enumerate(show_stones):
            sx = 3 + si * spacing
            sy = sky_end - 5
            if sx + 12 > width or sy < 3:
                continue
            # Tombstone art
            tomb = [
                "  _____  ",
                " /     \\ ",
                " | RIP | ",
                " |     | ",
                " |     | ",
                " |_____| ",
            ]
            name = stone.get("name", "???")
            gen = stone.get("gen", "?")
            w = stone.get("wins", 0)
            l = stone.get("losses", 0)
            pnl = stone.get("pnl", 0.0)
            cause = stone.get("cause", "unknown")
            pnl_sign = "+" if pnl >= 0 else ""

            # Line 3: name
            name_line = f" |{name:^7s}| "
            # Line 4: gen + W/L
            stats_line = f" |G{gen} {w}W{l}L| "
            # Line 5: PnL
            pnl_str = f"{pnl_sign}{pnl:.2f}"
            pnl_line = f" |{pnl_str:^7s}| "

            tomb[3] = name_line[:9]
            tomb[4] = stats_line[:9]
            tomb[5] = pnl_line[:9] if len(pnl_line) <= 9 else tomb[5]

            for ri, line in enumerate(tomb):
                for ci, ch in enumerate(line):
                    px = sx + ci
                    py = sy + ri
                    if 0 <= px < width and 0 <= py < height:
                        grid[py][px] = ch
                        color_grid[py][px] = DIM + WHITE if ri < 2 else GRAY

            # Cause of death below tombstone
            cause_short = cause[:spacing - 2]
            for ci, ch in enumerate(cause_short):
                px = sx + ci
                py = sy + len(tomb)
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = DIM + RED

    # Crabs present with (x,,x) dead eyes
    present = world.crabs_at("graveyard")
    crab_colors_map = {}
    for crab in crabs:
        crab_colors_map[crab.name] = crab.color

    crab_y = height - 4
    if present and crab_y > sky_end + 2:
        for pi, pname in enumerate(present[:4]):
            cx = 4 + pi * (width // 5)
            ccolor = crab_colors_map.get(pname, YELLOW)
            # Dead-eyed crab
            crab_art = ["(x,,x)", " )__) "]
            for ri, line in enumerate(crab_art):
                for ci, ch in enumerate(line):
                    px = cx + ci
                    py = crab_y + ri
                    if 0 <= px < width and 0 <= py < height:
                        grid[py][px] = ch
                        color_grid[py][px] = ccolor
            # Name
            for ci, ch in enumerate(pname[:8]):
                px = cx + ci
                py = crab_y + 2
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = ccolor
            # Dialogue
            dialogue = get_location_dialogue(pname, "graveyard", gen_tracker)
            for ci, ch in enumerate(dialogue[:width // 5 - 2]):
                px = cx + ci
                py = crab_y + 3
                if 0 <= px < width and 0 <= py < height:
                    grid[py][px] = ch
                    color_grid[py][px] = DIM + RED

    # Footer: total deaths + who dies most
    footer_y = height - 1
    if footer_y > 0 and stones:
        death_counts = {}
        for s in stones:
            n = s.get("name", "???")
            death_counts[n] = death_counts.get(n, 0) + 1
        most_dead = max(death_counts, key=death_counts.get) if death_counts else "none"
        footer = f" Total deaths: {len(stones)}  |  Most replaced: {most_dead} ({death_counts.get(most_dead, 0)}x)"
        for ci, ch in enumerate(footer[:width - 4]):
            px = 2 + ci
            if 0 <= px < width:
                grid[footer_y][px] = ch
                color_grid[footer_y][px] = DIM


def _draw_bank(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker, auto_trader):
    """Portfolio View bank scene — per-crab portfolio cards."""
    # Clean background
    for y in range(height):
        for x in range(width):
            grid[y][x] = " "
            color_grid[y][x] = ""

    # --- Bank decorations ---

    # Vault door (right side)
    vault_art = [
        " _________ ",
        "|  _____  |",
        "| |     | |",
        "| |  $  | |",
        "| |_____| |",
        "|    ()    |",
        "|_________|",
    ]
    vx = width - 16
    vy = 4
    if vx > 0:
        for ri, line in enumerate(vault_art):
            for ci, ch in enumerate(line):
                px = vx + ci
                py = vy + ri
                if 0 <= px < width and 0 <= py < height:
                    if ch == "$":
                        grid[py][px] = ch
                        color_grid[py][px] = BOLD + YELLOW
                    elif ch != " ":
                        grid[py][px] = ch
                        color_grid[py][px] = YELLOW

    # Gold piles (bottom area, seeded positions)
    gold_pile = [
        "  $  ",
        " $$$ ",
        "$$$$$",
    ]
    random.seed(44)
    pile_positions = [(random.randint(2, width - 8), height - 5) for _ in range(min(3, width // 25))]
    random.seed()
    for gx, gy in pile_positions:
        for ri, line in enumerate(gold_pile):
            for ci, ch in enumerate(line):
                px = gx + ci
                py = gy + ri
                if 0 <= px < width and 0 <= py < height and ch != " ":
                    grid[py][px] = ch
                    color_grid[py][px] = BOLD + YELLOW

    # Teller window (left side, col 2)
    teller_art = [
        "+--------+",
        "| TELLER |",
        "|  OPEN  |",
        "|________|",
        "+--------+",
    ]
    tw_x = 2
    tw_y = 4
    for ri, line in enumerate(teller_art):
        for ci, ch in enumerate(line):
            px = tw_x + ci
            py = tw_y + ri
            if 0 <= px < width and 0 <= py < height and ch != " ":
                grid[py][px] = ch
                color_grid[py][px] = CYAN

    # --- End bank decorations ---

    # Header
    header = " PORTFOLIO VAULT "
    hx = (width - len(header)) // 2
    for ci, ch in enumerate(header):
        px = hx + ci
        if 0 <= px < width:
            grid[1][px] = ch
            color_grid[1][px] = BOLD + YELLOW

    # Portfolio cards for crabs present at bank
    present = world.crabs_at("bank")
    crab_colors_map = {}
    crab_map = {}
    for crab in crabs:
        crab_colors_map[crab.name] = crab.color
        crab_map[crab.name] = crab

    if not present:
        msg = "No crabs at the bank. Use !move <crab> bank to send one."
        mx = (width - len(msg)) // 2
        for ci, ch in enumerate(msg[:width - 4]):
            px = mx + ci
            if 0 <= px < width and 4 < height:
                grid[4][px] = ch
                color_grid[4][px] = DIM
        return

    # Layout: up to 2 columns
    card_w = min(40, (width - 6) // 2)
    cards_per_row = max(1, (width - 4) // (card_w + 2))
    card_y = 3

    for pi, pname in enumerate(present):
        crab = crab_map.get(pname)
        if not crab:
            continue
        col = pi % cards_per_row
        row = pi // cards_per_row
        cx = 2 + col * (card_w + 2)
        cy = card_y + row * 10

        if cy + 9 >= height:
            break

        ccolor = crab_colors_map.get(pname, YELLOW)

        # Card border top
        border_top = "+" + "-" * (card_w - 2) + "+"
        for ci, ch in enumerate(border_top[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy < height:
                grid[cy][px] = ch
                color_grid[cy][px] = ccolor

        # Name line
        name_line = f"| {pname:<{card_w-4}s} |"
        for ci, ch in enumerate(name_line[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 1 < height:
                grid[cy + 1][px] = ch
                color_grid[cy + 1][px] = BOLD + ccolor

        # Wallet address
        wallet = crab.wallet or "no wallet"
        addr_short = wallet[:8] + "..." + wallet[-4:] if len(wallet) > 16 else wallet
        addr_line = f"| {addr_short:<{card_w-4}s} |"
        for ci, ch in enumerate(addr_line[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 2 < height:
                grid[cy + 2][px] = ch
                color_grid[cy + 2][px] = DIM

        # SOL balance
        bal = crab.wallet_balance
        bal_str = f"{bal:.4f}" if bal < 1 else f"{bal:.2f}"
        bal_line = f"| SOL: {bal_str:<{card_w-10}s} |"
        for ci, ch in enumerate(bal_line[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 3 < height:
                grid[cy + 3][px] = ch
                color_grid[cy + 3][px] = YELLOW if bal > 0.01 else RED

        # Token holdings
        token_parts = []
        for mint, sym in APPROVED_TOKENS.items():
            tb = crab.token_balances.get(mint, 0)
            if tb > 0:
                if tb >= 1_000_000:
                    ts = f"{tb/1_000_000:.1f}M"
                elif tb >= 1_000:
                    ts = f"{tb/1_000:.1f}K"
                else:
                    ts = f"{tb:.0f}"
                token_parts.append(f"${sym}: {ts}")
        token_str = ", ".join(token_parts) if token_parts else "no tokens"
        tok_line = f"| {token_str:<{card_w-4}s} |"
        for ci, ch in enumerate(tok_line[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 4 < height:
                grid[cy + 4][px] = ch
                color_grid[cy + 4][px] = GREEN if token_parts else DIM

        # Gen stats from gen_tracker
        if gen_tracker:
            stats = gen_tracker.crab_stats.get(pname, gen_tracker._empty_stats())
            sharpe = gen_tracker.get_sharpe(pname)
            pnl = stats["pnl"]
            pnl_sign = "+" if pnl >= 0 else ""
            gen_line = f"| Gen PnL: {pnl_sign}{pnl:.3f}  Sharpe: {sharpe:.2f}"
            gen_line = f"{gen_line:<{card_w-1}s}|"
            pnl_color = GREEN if pnl >= 0 else RED
            for ci, ch in enumerate(gen_line[:card_w]):
                px = cx + ci
                if 0 <= px < width and cy + 5 < height:
                    grid[cy + 5][px] = ch
                    color_grid[cy + 5][px] = pnl_color

            wl_str = f"| Gen W/L: {stats['wins']}W / {stats['losses']}L"
            wl_line = f"{wl_str:<{card_w-1}s}|"
            for ci, ch in enumerate(wl_line[:card_w]):
                px = cx + ci
                if 0 <= px < width and cy + 6 < height:
                    grid[cy + 6][px] = ch
                    color_grid[cy + 6][px] = DIM

        # All-time W/L from CRAB_WL
        all_wl = CRAB_WL.get(pname, {})
        alltime = f"| All-time: {all_wl.get('wins', 0)}W / {all_wl.get('losses', 0)}L"
        alltime_line = f"{alltime:<{card_w-1}s}|"
        for ci, ch in enumerate(alltime_line[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 7 < height:
                grid[cy + 7][px] = ch
                color_grid[cy + 7][px] = DIM + CYAN

        # Card border bottom
        border_bot = "+" + "-" * (card_w - 2) + "+"
        for ci, ch in enumerate(border_bot[:card_w]):
            px = cx + ci
            if 0 <= px < width and cy + 8 < height:
                grid[cy + 8][px] = ch
                color_grid[cy + 8][px] = ccolor

        # Dialogue under card
        dialogue = get_location_dialogue(pname, "bank", gen_tracker)
        for ci, ch in enumerate(dialogue[:card_w - 2]):
            px = cx + 1 + ci
            if 0 <= px < width and cy + 9 < height:
                grid[cy + 9][px] = ch
                color_grid[cy + 9][px] = DIM + YELLOW


def _draw_holders(grid, color_grid, width, height, tick_count, world, crabs, holder_feed):
    """Top 20 $PINCHIN holders room — crabs sized by bag, behavior tags."""
    # Clear background
    for y in range(height):
        for x in range(width):
            grid[y][x] = " "
            color_grid[y][x] = ""

    # Subtle starfield background (seeded so it doesn't flicker)
    random.seed(55 + tick_count // 8)
    for y in range(height):
        for x in range(width):
            if random.random() < 0.01:
                grid[y][x] = random.choice([".", "+", "*"])
                color_grid[y][x] = DIM + CYAN
    random.seed()

    # Header
    header = " TOP 20 $PINCHIN HOLDERS "
    hx = (width - len(header)) // 2
    for ci, ch in enumerate(header):
        px = hx + ci
        if 0 <= px < width:
            grid[1][px] = ch
            color_grid[1][px] = BOLD + YELLOW

    # Trigger fetch on first view or if user is looking at stale data
    if holder_feed is not None:
        with holder_feed._lock:
            fetching = holder_feed._fetching
            ready = holder_feed._ready
            error = holder_feed._error
        if not ready and not fetching:
            holder_feed.refresh()

    # Check if data ready
    if holder_feed is None or not holder_feed.is_ready():
        with holder_feed._lock:
            fetching = holder_feed._fetching
            error = holder_feed._error
        if fetching:
            msg = "fetching holders..."
        elif error:
            msg = f"RPC error: {error[:40]} (press [G] to retry)"
        else:
            msg = "fetching holders..."
        mx = (width - len(msg)) // 2
        blink = tick_count % 8 < 5
        if fetching and blink:
            for ci, ch in enumerate(msg):
                px = mx + ci
                if 0 <= px < width and 5 < height:
                    grid[5][px] = ch
                    color_grid[5][px] = DIM + YELLOW
        elif not fetching:
            for ci, ch in enumerate(msg):
                px = mx + ci
                if 0 <= px < width and 5 < height:
                    grid[5][px] = ch
                    color_grid[5][px] = RED if error else DIM + YELLOW
        # Animated loading crab
        load_crab = MINI_CRAB_R if tick_count % 4 < 2 else MINI_CRAB_L
        lx = (width - len(load_crab)) // 2
        if 7 < height:
            for ci, ch in enumerate(load_crab):
                px = lx + ci
                if 0 <= px < width:
                    grid[7][px] = ch
                    color_grid[7][px] = CYAN
        return

    holders = holder_feed.get_holders()
    if not holders:
        msg = "no holder data"
        mx = (width - len(msg)) // 2
        for ci, ch in enumerate(msg):
            px = mx + ci
            if 0 <= px < width and 5 < height:
                grid[5][px] = ch
                color_grid[5][px] = DIM
        return

    max_amount = holders[0]["amount"] if holders else 1

    # Layout: 4 columns x 5 rows
    cols = 4
    rows_count = 5
    cell_w = max(16, (width - 4) // cols)
    cell_h = max(5, (height - 3) // rows_count)

    for idx, h in enumerate(holders[:20]):
        col = idx % cols
        row = idx // cols
        cx = 2 + col * cell_w
        cy = 3 + row * cell_h

        if cy + cell_h > height:
            break

        rank = h["rank"]
        addr = h["address"]
        amount = h["amount"]
        behavior = h["behavior"]
        pct_change = h["pct_change"]

        # Short address: first 4 + last 4
        short_addr = addr[:4] + ".." + addr[-4:] if len(addr) > 10 else addr

        # Determine crab sprite and color by rank
        if rank <= 3:
            # Whale — big 3-line crab, bold yellow
            crab_color = BOLD + YELLOW
            if behavior == "jeet":
                # Panicking jeet whale
                sprite = [r" \(;_;)/ ", r"  |   |  ", r"  *   *  "]
                crab_color = BOLD + RED
            elif behavior == "accumulator":
                sprite = [r" \(*o*)/ ", r"  |   |  ", r"  $   $  "]
                crab_color = BOLD + GREEN
            else:
                facing_r = (tick_count + idx) % 6 < 3
                sprite = list(CRAB_RIGHT) if facing_r else list(CRAB_LEFT)
        elif rank <= 10:
            # Mid-tier — big 3-line crab, bold green
            crab_color = BOLD + GREEN
            if behavior == "jeet":
                sprite = [r" \(;_;)/ ", r"  |   |  ", r"  *   *  "]
                crab_color = RED
            elif behavior == "accumulator":
                sprite = [r" \(*o*)/ ", r"  |   |  ", r"  $   $  "]
                crab_color = GREEN
            else:
                facing_r = (tick_count + idx) % 6 < 3
                sprite = list(CRAB_RIGHT) if facing_r else list(CRAB_LEFT)
        else:
            # Small holders — mini 1-line crab, cyan
            crab_color = CYAN
            if behavior == "jeet":
                sprite = ["(;_;)"]
                crab_color = RED
            elif behavior == "accumulator":
                sprite = [r"\(*o*)/"]
                crab_color = GREEN
            else:
                facing_r = (tick_count + idx) % 6 < 3
                sprite = [MINI_CRAB_R if facing_r else MINI_CRAB_L]

        # Draw sprite (centered in cell)
        for si, sline in enumerate(sprite):
            sx = cx + max(0, (cell_w - len(sline)) // 2)
            sy = cy + si
            if sy < height:
                for sci, sch in enumerate(sline):
                    spx = sx + sci
                    if 0 <= spx < width:
                        grid[sy][spx] = sch
                        color_grid[sy][spx] = crab_color

        # Label: #rank short_address
        label_y = cy + len(sprite)
        label = f"#{rank} {short_addr}"
        lx = cx + max(0, (cell_w - len(label)) // 2)
        if label_y < height:
            for ci, ch in enumerate(label[:cell_w]):
                px = lx + ci
                if 0 <= px < width:
                    grid[label_y][px] = ch
                    color_grid[label_y][px] = DIM

        # Health bar: proportional to bag size vs largest
        bar_y = label_y + 1
        bar_inner = max(4, cell_w - 6)
        filled = max(1, int((amount / max_amount) * bar_inner)) if max_amount > 0 else 1
        bar = "[" + "#" * filled + "." * (bar_inner - filled) + "]"
        bx = cx + max(0, (cell_w - len(bar)) // 2)
        if bar_y < height:
            for ci, ch in enumerate(bar[:cell_w]):
                px = bx + ci
                if 0 <= px < width:
                    grid[bar_y][px] = ch
                    color_grid[bar_y][px] = GREEN if filled > bar_inner // 2 else YELLOW

        # Behavior tag
        tag_y = bar_y + 1
        if behavior == "diamond":
            tag = "DIAMOND"
            tag_color = CYAN
        elif behavior == "jeet":
            tag = "JEET!"
            tag_color = BOLD + RED
        else:
            tag = "BUYING"
            tag_color = BOLD + GREEN
        # Add pct change if nonzero
        if abs(pct_change) > 0.001:
            sign = "+" if pct_change > 0 else ""
            tag += f" {sign}{pct_change*100:.0f}%"
        tx = cx + max(0, (cell_w - len(tag)) // 2)
        if tag_y < height:
            for ci, ch in enumerate(tag[:cell_w]):
                px = tx + ci
                if 0 <= px < width:
                    grid[tag_y][px] = ch
                    color_grid[tag_y][px] = tag_color


def draw(crabs, swimmers, width, height, tick_count, desk_x, desk_y, dealer_x, dealer_y, price_data=None, is_night=False, girlfriends=None, bpm=0, bpm_input=None, chat_msgs=None, auto_trader=None, signal_display=None, price_feed=None, gen_tracker=None, world=None, holder_feed=None):
    grid = [[" "] * width for _ in range(height)]
    color_grid = [[""] * width for _ in range(height)]

    # Camera dispatch: non-beach scenes get their own renderer
    _skip_beach = False
    if world and world.camera != "beach":
        _draw_location(grid, color_grid, width, height, tick_count, world, crabs, gen_tracker, auto_trader, holder_feed)
        _skip_beach = True

    water_rows = max(2, height // 8)
    sand_rows = max(2, height // 6)
    sand_start = height - sand_rows

    # Water / Night sky
    if not _skip_beach and is_night:
        random.seed(99)
        for wy in range(water_rows):
            for wx in range(width):
                if random.random() < 0.04:
                    grid[wy][wx] = random.choice(["*", ".", "+"])
                    color_grid[wy][wx] = STAR_COLOR
                else:
                    offset = (tick_count + wx + wy * 3) % 12
                    grid[wy][wx] = "~" if offset < 3 else " "
                    color_grid[wy][wx] = NIGHT_WATER
        random.seed()
        # Moon
        moon_x = width - 6
        for ri, line in enumerate(MOON):
            for ci, ch in enumerate(line):
                px = moon_x + ci
                if 0 <= px < width and ri < water_rows and ch != " ":
                    grid[ri][px] = ch
                    color_grid[ri][px] = MOON_COLOR
    elif not _skip_beach:
        for wy in range(water_rows):
            for wx in range(width):
                offset = (tick_count + wx + wy * 3) % 8
                grid[wy][wx] = "~" if offset < 5 else " "
                color_grid[wy][wx] = WATER_COLOR

    # Sand
    sand_color_now = NIGHT_SAND if is_night else DARK_SAND
    if not _skip_beach:
        for sy in range(sand_start, height):
            for sx in range(width):
                if random.random() < 0.3:
                    grid[sy][sx] = random.choice([".", ",", "'", " "])
                else:
                    grid[sy][sx] = " "
                color_grid[sy][sx] = sand_color_now

    # Decorations
    if not _skip_beach:
        random.seed(42)
        for _ in range(width // 8):
            sx = random.randint(0, width - 1)
            sy = random.randint(sand_start, height - 1)
            grid[sy][sx] = random.choice(["@", "*"])
            color_grid[sy][sx] = YELLOW
        for _ in range(width // 15):
            sx = random.randint(0, width - 1)
            sy = random.randint(water_rows, sand_start)
            grid[sy][sx] = random.choice(["|", "/", "\\"])
            color_grid[sy][sx] = GREEN
        random.seed()

    # Defaults for variables used outside beach guard
    summoning_active = False
    starring_crabs = []

    if not _skip_beach:
        # Trading desk / Leaderboard
        for row_i, line in enumerate(TRADING_DESK):
            for col_i, ch in enumerate(line):
                px = desk_x + col_i
                py = desk_y + row_i
                if 0 <= px < width and 0 <= py < height and ch != " ":
                    grid[py][px] = ch
                    if row_i == 0:
                        color_grid[py][px] = BOLD + YELLOW
                    else:
                        color_grid[py][px] = BROWN

        # Fill scoreboard — generation tracker replaces old leaderboard
        iw = DESK_INNER  # inner width between pipes

        if gen_tracker:
            # --- Row 2: Header — generation number + progress bar ---
            gt = gen_tracker
            done = gt.trades_completed
            total = gt.trades_to_evolve
            bar_w = 20
            filled = int(bar_w * min(done, total) / max(total, 1))
            bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
            header = f" GENERATION {gt.number}  {bar} {done}/{total}"
            row_y = desk_y + 2
            for ci, ch in enumerate(header[:iw]):
                px = desk_x + 1 + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    color_grid[row_y][px] = BOLD + CYAN

            # --- Row 3: blank separator ---

            # --- Rows 4-10: Crab stats sorted by Sharpe ---
            ranked = gt.get_ranked()
            crab_colors = {}
            for crab in crabs:
                crab_colors[crab.name] = crab.color
            for ei, r in enumerate(ranked[:7]):
                row_y = desk_y + 4 + ei
                if row_y >= desk_y + DESK_HEIGHT - 2:
                    break
                name = r["name"]
                w = r["wins"]
                l = r["losses"]
                pnl = r["pnl"]
                sharpe = r["sharpe"]
                marker = r["marker"]
                pnl_sign = "+" if pnl >= 0 else ""
                # Format: " Name     W:7 L:3  +0.31 SOL  s:1.2  M"
                pnl_str = f"{pnl_sign}{pnl:.2f}"
                sharpe_str = f"s:{sharpe:.1f}"
                entry = f" {name:<9s} W:{w:<2d} L:{l:<2d} {pnl_str:>6s} SOL {sharpe_str:>5s} {marker}"
                pnl_color = GREEN if pnl >= 0 else RED
                ccolor = crab_colors.get(name, YELLOW)
                # Measure column boundaries for coloring
                name_end = 1 + 9  # " Name     "
                wl_end = name_end + 5 + 5  # "W:7  L:3  "
                pnl_end = wl_end + 10  # "+0.31 SOL "
                for ci, ch in enumerate(entry[:iw]):
                    px = desk_x + 1 + ci
                    if 0 <= px < width and 0 <= row_y < height:
                        grid[row_y][px] = ch
                        if ci < name_end:
                            color_grid[row_y][px] = BOLD + ccolor if ei == 0 else ccolor
                        elif ci < wl_end:
                            color_grid[row_y][px] = DIM
                        elif ci < pnl_end:
                            color_grid[row_y][px] = BOLD + pnl_color
                        else:
                            color_grid[row_y][px] = GRAY

            # --- Row 11 (DESK_HEIGHT-2): Last trade description ---
            row_y = desk_y + DESK_HEIGHT - 2
            if gt.last_trade_desc and gt.last_trade_time > 0:
                ago_s = int(time.time() - gt.last_trade_time)
                if ago_s < 60:
                    ago = f"{ago_s}s ago"
                else:
                    ago = f"{ago_s // 60}m ago"
                footer = f" Last: {gt.last_trade_desc} ({ago})"
            else:
                footer = f" {gt.prev_gen_recap}"
            for ci, ch in enumerate(footer[:iw]):
                px = desk_x + 1 + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    color_grid[row_y][px] = DIM

        # Active positions board
        # Draw frame
        for row_i, line in enumerate(POS_BOARD_FRAME):
            for col_i, ch in enumerate(line):
                px = dealer_x + col_i
                py = dealer_y + row_i
                if 0 <= px < width and 0 <= py < height and ch != " ":
                    grid[py][px] = ch
                    if row_i == 0:
                        color_grid[py][px] = BOLD + CYAN
                    else:
                        color_grid[py][px] = BROWN
        # Fill position rows (rows 2-5 inside the frame)
        pos_lines = []
        try:
            if auto_trader and hasattr(auto_trader, '_pos_display'):
                pos_lines = list(auto_trader._pos_display)
        except Exception:
            pass
        if not pos_lines:
            pos_lines.append(("  no positions", ""))
        for pi, (pline, pnl_s) in enumerate(pos_lines[:4]):
            row_y = dealer_y + 2 + pi
            padded = pline[:16].center(16)
            for ci, ch in enumerate(padded):
                px = dealer_x + 1 + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    if pnl_s.startswith("+"):
                        color_grid[row_y][px] = GREEN
                    elif pnl_s.startswith("-"):
                        color_grid[row_y][px] = RED
                    else:
                        color_grid[row_y][px] = YELLOW

        # Signal board (physical sign on ocean floor)
        ocean_center_y = (water_rows + sand_start) // 2
        if signal_display:
            sig_bx = dealer_x + DEALER_WIDTH + 3
            sig_by = ocean_center_y - SIGNAL_BOARD_HEIGHT // 2
            # Draw the frame
            has_snipe = signal_display and signal_display[0][0].startswith("SNIPING")
            for row_i, line in enumerate(SIGNAL_BOARD):
                for col_i, ch in enumerate(line):
                    px = sig_bx + col_i
                    py = sig_by + row_i
                    if 0 <= px < width and 0 <= py < height and ch != " ":
                        grid[py][px] = ch
                        if row_i == 0:
                            color_grid[py][px] = BOLD + GREEN if has_snipe else BOLD + CYAN
                        else:
                            color_grid[py][px] = GREEN if has_snipe else BROWN
            # Fill dynamic content inside the board
            inner_w = SIGNAL_BOARD_INNER
            for pi, (ptext, pcolor) in enumerate(signal_display):
                row_y = sig_by + 2 + pi
                if row_y >= sig_by + SIGNAL_BOARD_HEIGHT - 2:
                    break
                text = ptext[:inner_w].center(inner_w)
                for ci, ch in enumerate(text):
                    px = sig_bx + 1 + ci
                    if 0 <= px < width and 0 <= row_y < height:
                        grid[row_y][px] = ch
                        color_grid[row_y][px] = pcolor

        # Crabs
        for crab in crabs:
            lines = crab.render_lines()
            for row_i, line in enumerate(lines):
                for col_i, ch in enumerate(line):
                    px = crab.x + col_i
                    py = crab.y + row_i
                    if 0 <= px < width and 0 <= py < height and ch != " ":
                        grid[py][px] = ch
                        # Cars get a special gold color
                        if crab.state == "driving":
                            if row_i == 1:  # car body
                                color_grid[py][px] = YELLOW
                            else:  # crab on top
                                color_grid[py][px] = crab.color
                        else:
                            color_grid[py][px] = crab.color

            # Crab name under body
            if crab.wallet:
                name_label = crab.name
                label_y = crab.y + crab.height
                label_x = crab.x + (crab.width - len(name_label)) // 2
                for ci, ch in enumerate(name_label):
                    px = label_x + ci
                    if 0 <= px < width and 0 <= label_y < height:
                        grid[label_y][px] = ch
                        color_grid[label_y][px] = crab.color

            # Exhaust fumes from car
            if crab.state == "driving":
                fume_y = crab.y + 1
                if crab.facing_right:
                    fume_x = crab.x - 1 - (tick_count % 3)
                else:
                    fume_x = crab.x + crab.width + (tick_count % 3)
                if 0 <= fume_x < width and 0 <= fume_y < height:
                    grid[fume_y][fume_x] = random.choice(["*", ".", "o"])
                    color_grid[fume_y][fume_x] = GRAY

            # Hearts floating up when ogling
            if crab.state == "ogling":
                heart_chars = ["<3", "<3", "<3"]
                for h in range(2):
                    hy = crab.y - 1 - h
                    hx = crab.x + crab.width // 2 + (1 if (tick_count + h) % 4 < 2 else -1)
                    if 0 <= hx < width - 1 and 0 <= hy < height:
                        grid[hy][hx] = "<"
                        color_grid[hy][hx] = RED
                        if hx + 1 < width:
                            grid[hy][hx + 1] = "3"
                            color_grid[hy][hx + 1] = RED

            if crab.bubble_life > 0 and 0 <= crab.bubble_y < height and 0 <= crab.bubble_x < width:
                idx = min(crab.bubble_life, len(BUBBLE_CHARS) - 1)
                grid[crab.bubble_y][crab.bubble_x] = BUBBLE_CHARS[idx]
                color_grid[crab.bubble_y][crab.bubble_x] = CYAN

            # Trade thought bubble above crab head
            if crab.trade_thought and crab.trade_thought_timer > 0:
                thought = crab.trade_thought[:29] + "\u2026" if len(crab.trade_thought) > 30 else crab.trade_thought[:30]
                ty = crab.y - 2
                tx = crab.x - len(thought) // 2 + crab.width // 2
                if thought.startswith("BUY"):
                    thought_color = GREEN
                elif thought.startswith("SELL"):
                    thought_color = RED
                else:
                    thought_color = YELLOW  # hold/thinking
                if 0 <= ty < height:
                    for ci, ch in enumerate(thought):
                        px = tx + ci
                        if 0 <= px < width:
                            grid[ty][px] = ch
                            color_grid[ty][px] = thought_color
                    # Draw connector dot
                    dot_y = crab.y - 1
                    dot_x = crab.x + crab.width // 2
                    if 0 <= dot_y < height and 0 <= dot_x < width:
                        grid[dot_y][dot_x] = "o"
                        color_grid[dot_y][dot_x] = thought_color

            if crab.state == "smoking" and crab.smoke_puff_timer % 4 == 0:
                for p in range(3):
                    puff_y = crab.y - 1 - p
                    puff_x = crab.x + crab.width - 1 + p
                    if crab.smoke_puff_timer % 8 < 4:
                        puff_x += 1
                    if 0 <= puff_x < width and 0 <= puff_y < height:
                        idx = min(p, len(SMOKE_PUFFS) - 1)
                        grid[puff_y][puff_x] = SMOKE_PUFFS[idx]
                        color_grid[puff_y][puff_x] = GRAY

            # Confetti around celebrating crabs
            if crab.state == "celebrating":
                for _ in range(3):
                    conf_x = crab.x + random.randint(-3, crab.width + 3)
                    conf_y = crab.y + random.randint(-2, 1)
                    if 0 <= conf_x < width and 0 <= conf_y < height:
                        grid[conf_y][conf_x] = random.choice(CONFETTI_CHARS)
                        color_grid[conf_y][conf_x] = random.choice(CONFETTI_COLORS)

            # Energy sparks around summoning crabs
            if crab.state == "summoning":
                for _ in range(2):
                    sx = crab.x + random.randint(-2, crab.width + 2)
                    sy = crab.y + random.randint(-2, 1)
                    if 0 <= sx < width and 0 <= sy < height:
                        grid[sy][sx] = random.choice(["*", "+", "~", "^"])
                        color_grid[sy][sx] = random.choice([LIGHTNING_COLOR, YELLOW, CYAN])

            # Fireball when burning
            if crab.state == "burning":
                # Ember sparks around crab while casting
                if crab.state_timer > 24:
                    for _ in range(3):
                        ex = crab.x + random.randint(-1, crab.width + 1)
                        ey = crab.y + random.randint(-2, 0)
                        if 0 <= ex < width and 0 <= ey < height:
                            grid[ey][ex] = random.choice(["*", "^", "."])
                            color_grid[ey][ex] = random.choice([ORANGE, YELLOW, RED])
                # Fireball projectile rising
                if crab.fireball_active:
                    fx, fy = crab.fireball_x, crab.fireball_y
                    # Main fireball
                    if 0 <= fx < width and 0 <= fy < height:
                        grid[fy][fx] = "@"
                        color_grid[fy][fx] = ORANGE
                    # Trail below
                    for t in range(1, 4):
                        ty = fy + t
                        tx = fx + random.choice([-1, 0, 0, 1])
                        if 0 <= tx < width and 0 <= ty < height:
                            ch = FIREBALL_FRAMES[min(t, len(FIREBALL_FRAMES) - 1)]
                            grid[ty][tx] = ch
                            color_grid[ty][tx] = YELLOW if t < 2 else RED

            # Music notes when dancing
            if crab.state == "dancing":
                for n in range(2):
                    nx = crab.x + crab.width // 2 + random.choice([-2, -1, 0, 1, 2])
                    ny = crab.y - 1 - n
                    if 0 <= nx < width and 0 <= ny < height:
                        grid[ny][nx] = random.choice(MUSIC_NOTES)
                        color_grid[ny][nx] = random.choice([MAGENTA, PINK, YELLOW])

            # Zen aura around meditating crabs
            if crab.state == "meditating":
                cx = crab.x + crab.width // 2
                cy = crab.y
                # Expanding ring of dots
                ring_r = (crab.state_timer % 20) // 5 + 1
                for angle_i in range(8):
                    a = angle_i * (math.pi / 4)
                    rx = cx + int(ring_r * math.cos(a))
                    ry = cy + int((ring_r * 0.4) * math.sin(a))
                    if 0 <= rx < width and 0 <= ry < height:
                        grid[ry][rx] = "."
                        color_grid[ry][rx] = CYAN
                # Floating zen symbols drifting upward
                for z in range(3):
                    zx = cx + random.choice([-3, -2, -1, 0, 1, 2, 3])
                    zy = cy - 2 - z
                    if 0 <= zx < width and 0 <= zy < height:
                        grid[zy][zx] = random.choice(ZEN_SYMBOLS)
                        color_grid[zy][zx] = random.choice([CYAN, WHITE, YELLOW])

            # Star energy sparks around starring crabs
            if crab.state == "starring":
                for _ in range(3):
                    sx = crab.x + random.randint(-3, crab.width + 3)
                    sy = crab.y + random.randint(-2, 1)
                    if 0 <= sx < width and 0 <= sy < height:
                        grid[sy][sx] = random.choice(["*", "+", ".", "x"])
                        color_grid[sy][sx] = random.choice([LIGHTNING_COLOR, YELLOW, CYAN, MAGENTA])

            # Locked-in aura effects (4 layers)
            if crab.state == "locked_in":
                cx = crab.x + crab.width // 2
                cy = crab.y + 1  # center vertically on 3-row sprite

                # Layer 1: Pulsing energy ring (12 points, radius oscillates)
                ring_r = (tick_count % 30) // 6 + 2  # radius 2-6, pulsing
                for angle_i in range(12):
                    a = angle_i * (math.pi / 6)
                    rx = cx + int(ring_r * math.cos(a))
                    ry = cy + int((ring_r * 0.4) * math.sin(a))
                    if 0 <= rx < width and 0 <= ry < height:
                        grid[ry][rx] = random.choice([".", "*", "+"])
                        color_grid[ry][rx] = random.choice([CYAN, MAGENTA, WHITE])

                # Layer 2: Floating data symbols drifting upward
                for z in range(4):
                    zx = cx + random.randint(-5, 5)
                    zy = cy - 2 - z
                    if 0 <= zx < width and 0 <= zy < height:
                        grid[zy][zx] = random.choice(["$", "%", "#", "*", "~", "+", "^"])
                        color_grid[zy][zx] = random.choice([GREEN, CYAN, YELLOW, MAGENTA])

                # Layer 3: Ground energy sparks below
                for _ in range(2):
                    gx = cx + random.randint(-4, 4)
                    gy = cy + 2 + random.randint(0, 1)
                    if 0 <= gx < width and 0 <= gy < height:
                        grid[gy][gx] = random.choice(["~", ".", "*"])
                        color_grid[gy][gx] = random.choice([CYAN, MAGENTA])

                # Layer 4: Side energy pillars
                for pillar_dx in [-6, crab.width + 5]:
                    px = crab.x + pillar_dx
                    for py_off in range(-3, 3):
                        py = cy + py_off
                        if 0 <= px < width and 0 <= py < height and random.random() < 0.6:
                            grid[py][px] = random.choice(["|", "!", ":", "*"])
                            color_grid[py][px] = random.choice([CYAN, MAGENTA, WHITE])

        # Girlfriends
        if girlfriends:
            for gf in girlfriends:
                if gf.state == "gone":
                    continue
                lines = gf.render_lines()
                for row_i, line in enumerate(lines):
                    for col_i, ch in enumerate(line):
                        px = gf.x + col_i
                        py = gf.y + row_i
                        if 0 <= px < width and 0 <= py < height and ch != " ":
                            grid[py][px] = ch
                            color_grid[py][px] = gf.color
                if gf.state == "dancing":
                    for n in range(2):
                        nx = gf.x + gf.width // 2 + random.choice([-2, -1, 0, 1, 2])
                        ny = gf.y - 1 - n
                        if 0 <= nx < width and 0 <= ny < height:
                            grid[ny][nx] = random.choice(MUSIC_NOTES)
                            color_grid[ny][nx] = random.choice([MAGENTA, PINK, YELLOW])

        # Lightning bolts during summoning
        summoning_active = any(c.state == "summoning" for c in crabs)
        if summoning_active:
            board_cx = width // 2
            board_cy = (water_rows + sand_start) // 2
            # Draw 2-3 lightning bolts from sky to board
            num_bolts = 2 if tick_count % 4 < 2 else 3
            for b in range(num_bolts):
                bx = board_cx + random.randint(-15, 15)
                by = 0
                bolt_chars = ["|", "/", "\\", "|", "/", "\\", "*"]
                for step in range(board_cy):
                    if 0 <= bx < width and 0 <= by < height:
                        ch = random.choice(bolt_chars)
                        grid[by][bx] = ch
                        color_grid[by][bx] = LIGHTNING_COLOR if random.random() < 0.7 else YELLOW
                    by += 1
                    bx += random.choice([-1, 0, 0, 1])
                    bx = max(0, min(width - 1, bx))

        # Star lightning during !sebas ritual
        starring_crabs = [c for c in crabs if c.state == "starring"]
        if len(starring_crabs) == 6:
            # Sort by star_target angle (order they were assigned: 0-5)
            sc = sorted(starring_crabs, key=lambda c: math.atan2(c.star_target_y - height // 2, c.star_target_x - width // 2))
            # Two overlapping triangles: 0->2->4->0 and 1->3->5->1
            bolt_chars = ["|", "/", "\\", "*", "+", "~"]
            bolt_colors = [LIGHTNING_COLOR, YELLOW, CYAN]
            for tri in [(0, 2, 4), (1, 3, 5)]:
                edges = [(tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])]
                for i_a, i_b in edges:
                    ax = sc[i_a].x + sc[i_a].width // 2
                    ay = sc[i_a].y
                    bx_e = sc[i_b].x + sc[i_b].width // 2
                    by_e = sc[i_b].y
                    steps = max(abs(bx_e - ax), abs(by_e - ay), 1)
                    for s in range(steps + 1):
                        t = s / max(steps, 1)
                        lx = int(ax + (bx_e - ax) * t) + random.choice([-1, 0, 0, 0, 1])
                        ly = int(ay + (by_e - ay) * t)
                        if 0 <= lx < width and 0 <= ly < height:
                            if tick_count % 3 != 0 or random.random() < 0.7:
                                grid[ly][lx] = random.choice(bolt_chars)
                                color_grid[ly][lx] = random.choice(bolt_colors)

    # Live trade feed (right side) — beach only
    if chat_msgs and not _skip_beach:
        feed_w = 56
        feed_x = width - feed_w - 2
        feed_y = 3
        # Header
        header = " LIVE TRADES "
        header_padded = header.center(feed_w)
        for ci, ch in enumerate(header_padded):
            px = feed_x + ci
            if 0 <= px < width and feed_y - 1 < height:
                grid[feed_y - 1][px] = ch
                color_grid[feed_y - 1][px] = BOLD + YELLOW
        # Crab trades only (filter out random people)
        trade_msgs = [m for m in chat_msgs if m.get("color") in ("crab_buy", "crab_sell", "gate_block", "gate_pass")]
        for mi, m in enumerate(trade_msgs[-FEED_MAX_MESSAGES:]):
            row_y = feed_y + mi
            if row_y >= height:
                break
            raw_msg = m["msg"]
            line = raw_msg[:feed_w - 1] + "\u2026" if len(raw_msg) > feed_w else raw_msg[:feed_w]
            msg_color = m.get("color", "")
            if msg_color == "gate_block":
                trade_color = BOLD + ORANGE
            elif msg_color == "gate_pass":
                trade_color = BOLD + GREEN
            elif msg_color.startswith("crab_"):
                trade_color = BOLD + GREEN if "buy" in msg_color else BOLD + RED
            else:
                trade_color = GREEN if msg_color == "buy" else RED
            for ci, ch in enumerate(line):
                px = feed_x + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    color_grid[row_y][px] = trade_color

    # Live chat (left side) — beach only
    if chat_msgs and not _skip_beach:
        chat_w = 56
        chat_x = 2
        chat_y = 3
        # Header
        cheader = " CHAT "
        cheader_padded = cheader.center(chat_w)
        for ci, ch in enumerate(cheader_padded):
            px = chat_x + ci
            if 0 <= px < width and chat_y - 1 < height:
                grid[chat_y - 1][px] = ch
                color_grid[chat_y - 1][px] = BOLD + CYAN
        # Chat messages only
        chat_only = [m for m in chat_msgs if m.get("color") in ("chat", "system") and m.get("msg", "").strip().lower() not in ("!sebas",)]
        for mi, m in enumerate(chat_only[-FEED_MAX_MESSAGES:]):
            row_y = chat_y + mi
            if row_y >= height:
                break
            is_sys = m.get("color") == "system"
            user = m["user"][:10]
            max_msg = chat_w - len(user) - 2
            raw_chat = m["msg"]
            msg_text = raw_chat[:max_msg - 1] + "\u2026" if len(raw_chat) > max_msg else raw_chat[:max_msg]
            line = f"{user}: {msg_text}"
            for ci, ch in enumerate(line[:chat_w]):
                px = chat_x + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    if is_sys:
                        color_grid[row_y][px] = BOLD + YELLOW
                    else:
                        color_grid[row_y][px] = CYAN if ci < len(user) else WHITE
        # Command hints below chat
        hint_y = chat_y + min(len(chat_only), FEED_MAX_MESSAGES) + 1
        hints = [
            "!party !summon !dance !meditate",
            "!goto !move !list !crashout",
        ]
        for hi, hint_line in enumerate(hints):
            hy = hint_y + hi
            if hy >= height:
                break
            for ci, ch in enumerate(hint_line[:chat_w]):
                px = chat_x + ci
                if 0 <= px < width and 0 <= hy < height:
                    grid[hy][px] = ch
                    color_grid[hy][px] = DIM + CYAN


    # Screen-wide effects (burn flames, food sparkles, win rain)
    global _screen_effects
    if _screen_effects:
        _screen_effects.update(width, height, crabs)
        _screen_effects.render(grid, color_grid, width, height)

    # Render
    buf = []
    border_color = CYAN
    starring_active = len(starring_crabs) == 6
    if (summoning_active or starring_active) and tick_count % 3 == 0:
        border_color = LIGHTNING_COLOR
    elif bpm > 0 and tick_count % 4 == 0:
        border_color = MAGENTA
    border = border_color + "+" + "-" * width + "+" + RESET
    buf.append(border)
    for y in range(height):
        row = CYAN + "|" + RESET
        prev_c = ""
        for x in range(width):
            c = color_grid[y][x]
            if c != prev_c:
                row += c
                prev_c = c
            row += grid[y][x]
        row += RESET + CYAN + "|" + RESET
        buf.append(row)
    buf.append(border)

    # Status bar
    status_lines = []
    for crab in crabs:
        record = f"[W:{crab.wins} L:{crab.losses}]"
        tag = f"  {crab.color}{crab.name}{RESET} {DIM}{record}{RESET}"
        if crab.wallet:
            abbr = crab.wallet[:6] + ".." + crab.wallet[-4:]
            bal = f"{crab.wallet_balance:.4f}" if crab.wallet_balance < 1 else f"{crab.wallet_balance:.2f}"
            tag += f" {DIM}[{abbr}]{RESET} {YELLOW}{bal} SOL{RESET}"
            for _mint, _sym in APPROVED_TOKENS.items():
                _tb = crab.token_balances.get(_mint, 0)
                if _tb > 0:
                    if _tb >= 1_000_000:
                        _ts = f"{_tb/1_000_000:.1f}M"
                    elif _tb >= 1_000:
                        _ts = f"{_tb/1_000:.1f}K"
                    else:
                        _ts = f"{_tb:.0f}"
                    tag += f" {GREEN}{_ts} ${_sym}{RESET}"
        if crab.name in BENCHED_CRABS:
            tag += f" {DIM}[BENCHED]{RESET}"
        status_lines.append(tag)

    CLEAR_EOL = "\033[K"
    buf.append(CLEAR_EOL)
    for sl in status_lines:
        buf.append(sl + CLEAR_EOL)
    buf.append(CLEAR_EOL)
    if bpm_input is not None and bpm_input.startswith("BURN"):
        bpm_str = f"{RED}{BOLD}{bpm_input}_{RESET} "
    elif bpm_input is not None:
        bpm_str = f"{MAGENTA}BPM> {bpm_input}_{RESET} "
    elif bpm > 0:
        bpm_str = f"{MAGENTA}BPM:{bpm}{RESET} "
    else:
        bpm_str = ""
    night_ind = f"{YELLOW}* NIGHT *{RESET} " if is_night else ""
    feed_ind = f"{GREEN}LIVE{RESET}{DIM} " if chat_msgs else ""
    cam_ind = ""
    if world and world.camera != "beach":
        cam_ind = f"{BOLD}{CYAN}[{world.camera.upper()}]{RESET} "
    holder_hint = "  [H] Refresh" if world and world.camera == "holders" else ""
    buf.append(DIM + f"  {cam_ind}{night_ind}{bpm_str}{feed_ind}[R] Restart  [+/-] Speed  [N] Night  [B] BPM  [T] Trades  [G] Location{holder_hint}  [K] Burn  [X] Panic Sell  [Q] Quit" + RESET + CLEAR_EOL)

    # Win flash overlay — dims scene, shows big PnL
    if auto_trader and auto_trader.win_flash and auto_trader.win_flash["ticks_left"] > 0:
        wf = auto_trader.win_flash
        wf["ticks_left"] -= 1
        # Only flash during first 30 ticks (fade effect: bright first, then dim)
        if wf["ticks_left"] > 10:
            flash_color = BOLD + GREEN
        else:
            flash_color = GREEN
        # Dim all existing grid lines
        for bi in range(len(buf)):
            buf[bi] = DIM + buf[bi] + RESET
        # Big ASCII number overlay centered on screen
        big_text = wf["text"]
        sub_text = wf["sub"]
        cx = width // 2
        cy = height // 2
        # Draw big text (row cy in buf — offset by 1 for top border)
        big_row = cy + 1  # +1 for the border line at top
        if 0 <= big_row < len(buf):
            pad = max(0, cx - len(big_text) // 2)
            buf[big_row] = " " * pad + flash_color + big_text + RESET
        # Subtext line below
        sub_row = big_row + 2
        if 0 <= sub_row < len(buf):
            pad = max(0, cx - len(sub_text) // 2)
            buf[sub_row] = " " * pad + flash_color + sub_text + RESET
        if wf["ticks_left"] <= 0:
            auto_trader.win_flash = None

    # Truncate to terminal height to prevent scroll-induced glitch
    term_rows = shutil.get_terminal_size((80, 24)).lines
    if len(buf) > term_rows:
        buf = buf[:term_rows]
    frame = "\n".join(buf)
    sys.stdout.write("\033[H")
    sys.stdout.write(frame)
    sys.stdout.flush()


def make_crabs(width, height, desk_x, desk_y, dealer_x, dealer_y):
    water_rows = max(2, height // 8)
    sand_rows = max(2, height // 6)
    crabs = []
    for i in range(NUM_CRABS):
        cx = random.randint(2, max(3, width // 2 - 12))
        cy = random.randint(water_rows + 1, max(water_rows + 2, height - sand_rows - 4))
        color = CRAB_COLORS[i % len(CRAB_COLORS)]
        crabs.append(Crab(cx, cy, width, height, color, desk_x, desk_y, dealer_x, dealer_y))
    used_names = set()
    for crab in crabs:
        while crab.name in used_names:
            crab.name = random.choice(NAMES)
        used_names.add(crab.name)
    return crabs


def assign_wallets(crabs, auto_trader=None):
    """Ensure wallet holders are present and assign addresses."""
    crab_names = {c.name for c in crabs}
    for wallet_name in CRAB_WALLETS:
        if wallet_name not in crab_names:
            for c in crabs:
                if c.name not in CRAB_WALLETS:
                    c.name = wallet_name
                    break
    for crab in crabs:
        if crab.name in CRAB_WALLETS:
            crab.wallet = CRAB_WALLETS[crab.name]
        if auto_trader:
            crab.auto_trader = auto_trader


def main():
    cols, rows = shutil.get_terminal_size((80, 24))
    status_rows = NUM_CRABS + 3
    width = cols - 2
    height = rows - status_rows - 2

    if width < 30 or height < 12:
        print("Terminal too small! Need at least 30 wide x 12 tall.")
        return

    init_kb()
    hide_cursor()
    clear_screen()

    claws = "}}}{{}}"
    title = (
        f"\n{CYAN}     ~~ CRAB TRADER ~~{RESET}\n"
        f"{ORANGE}\n"
        f"        ,~,        ,~,\n"
        f"      (O O) {RED}{claws}{ORANGE}  (O O)\n"
        f"       )_)--'-'    )_)--'-'\n"
        f"{RESET}\n"
    )
    print(title)
    time.sleep(2)
    clear_screen()

    water_rows = max(2, height // 8)
    sand_rows = max(2, height // 6)
    sand_start = height - sand_rows

    desk_x = width - DESK_WIDTH - 4
    desk_y = sand_start - DESK_HEIGHT + 1

    dealer_x = 3
    dealer_y = sand_start - DEALER_HEIGHT + 1

    price_feed = PriceFeed()
    wallet_feed = WalletFeed()
    helius_webhook = HeliusWebhook(wallet_feed)  # push-based wallet updates (port 3001)
    trade_feed = TradeFeed()
    chat_bridge = ChatBridge()
    auto_trader = AutoTrader()
    auto_trader.price_feed = price_feed
    auto_trader.wallet_feed = wallet_feed
    auto_trader.trade_feed = trade_feed
    gen_tracker = GenerationTracker()
    auto_trader.gen_tracker = gen_tracker
    world = CrabWorld()
    holder_feed = HolderFeed()
    chat_poster = PumpChatPoster(auto_trader)
    auto_trader.chat_poster = chat_poster
    twitter_poster = TwitterPoster(auto_trader)
    auto_trader.twitter_poster = twitter_poster
    crab_brain = CrabBrain(twitter_poster, auto_trader)
    auto_trader.crab_brain = crab_brain

    global _screen_effects
    _screen_effects = ScreenEffects()

    # Start price history collector for OpenEvolve backtesting
    price_history_collector = None
    try:
        from crab_evolve.price_history import PriceHistoryCollector
        price_history_collector = PriceHistoryCollector(price_feed)
        auto_trader.price_history = price_history_collector
    except Exception:
        pass  # crab_evolve not installed or import error

    crabs = make_crabs(width, height, desk_x, desk_y, dealer_x, dealer_y)
    assign_wallets(crabs, auto_trader)
    for crab in crabs:
        wl = CRAB_WL.get(crab.name, {})
        crab.wins = wl.get("wins", 0)
        crab.losses = wl.get("losses", 0)

    # Pull real wallet data immediately so leaderboard doesn't start at zero
    wallet_feed.fetch_all_now()
    for crab in crabs:
        if crab.wallet:
            crab.wallet_balance = wallet_feed.get_balance(crab.wallet)
            for _m in APPROVED_TOKENS:
                crab.token_balances[_m] = wallet_feed.get_token_balance(crab.wallet, _m)

    # Lock active trading crabs in center with aura
    for crab in crabs:
        if crab.name in ("Pinchy", "Mr.Krabs"):
            crab.state = "locked_in"
            crab.state_timer = 0
            crab.mood = "locked in"
            crab.action_msg = "locked in"
            crab.action_timer = 20
    # Position: Pinchy on left facing right, Mr.Krabs on right facing left
    for crab in crabs:
        if crab.name == "Pinchy":
            crab.x = width // 2 - 12
            crab.y = height // 2 - 1
            crab.facing_right = True
        elif crab.name == "Mr.Krabs":
            crab.x = width // 2 + 3
            crab.y = height // 2 - 1
            crab.facing_right = False

    # Consolidate SOL from benched crabs into Pinchy & Mr.Krabs
    print(f"\n{CYAN}[CONSOLIDATE] Sweeping SOL from benched crabs...{RESET}")
    try:
        auto_trader.consolidate_sol()
    except Exception as e:
        print(f"  [CONSOLIDATE] Error: {e}")
    print()

    signal_board = SignalBoard(crabs, auto_trader)

    # Register any existing non-PINCHIN positions in price feed
    for crab_name, positions in auto_trader.positions.items():
        for mint in positions:
            if mint != PINCHIN_CONTRACT and positions[mint].get("tokens", 0) > 0:
                price_feed.extra_mints[mint] = mint[:8]
                # Set entry_time if missing
                if "entry_time" not in positions[mint]:
                    positions[mint]["entry_time"] = time.time()

    swimmers = []  # swimmers removed
    tick_speed = TICK

    tick = 0
    last_trend = ""
    is_night = False
    girlfriends = []
    bpm = 0
    bpm_input_mode = False
    bpm_input_buf = ""
    burn_input_mode = False
    burn_input_buf = ""
    feed_visible = True
    thoughts_visible = True
    # Burn rotation: cycles through crabs (excluding Bastian) on !burn
    _burn_crabs = [n for n in ["Mr.Krabs", "Pinchy", "Clawdia", "Sandy", "Snippy", "Hermie"] if n in auto_trader.keypairs and n not in BENCHED_CRABS]
    _burn_idx = 0
    _burn_killed = os.path.exists(os.path.expanduser("~/.pinchin_kill"))  # kill file check on boot
    _burn_reminder = time.time() + 1800  # first reminder in 30 min
    last_chat_count = 0

    # CLI arg for BPM: python crab_sim.py 128
    if len(sys.argv) > 1:
        try:
            bpm = max(0, min(300, int(sys.argv[1])))
            if bpm > 0:
                tick_speed = 60.0 / bpm / 4
        except ValueError:
            pass

    # Give wallet/price feeds time to poll, then buy initial bags
    time.sleep(10)
    auto_trader.buy_initial_bags()

    try:
        while True:
            key = get_key()

            # Burn % input mode — type digits, Enter to burn
            if burn_input_mode:
                if key and key.isdigit():
                    burn_input_buf += key
                elif key == '\x7f' or key == '\x08':
                    burn_input_buf = burn_input_buf[:-1]
                elif key in ('\r', '\n'):
                    if burn_input_buf:
                        try:
                            burn_pct = max(1, min(100, int(burn_input_buf)))
                        except ValueError:
                            burn_pct = 0
                        if burn_pct > 0:
                            t = threading.Thread(
                                target=auto_trader._execute_burn,
                                args=("Mr.Krabs", PINCHIN_CONTRACT, burn_pct / 100.0),
                                daemon=True,
                            )
                            t.start()
                    burn_input_mode = False
                    burn_input_buf = ""
                    pass  # burn mode exited
                elif key in ('\x1b', 'k'):
                    burn_input_mode = False
                    burn_input_buf = ""
                    pass  # burn mode exited
            # BPM input mode - type digits, Enter to set
            elif bpm_input_mode:
                if key and key.isdigit():
                    bpm_input_buf += key
                elif key in ('\r', '\n', ' '):
                    if bpm_input_buf:
                        try:
                            bpm = max(0, min(300, int(bpm_input_buf)))
                        except ValueError:
                            pass
                    else:
                        bpm = 0
                    tick_speed = 60.0 / bpm / 4 if bpm > 0 else TICK
                    bpm_input_mode = False
                    bpm_input_buf = ""
                    clear_screen()
                elif key in ('\x1b', 'b'):
                    bpm_input_mode = False
                    bpm_input_buf = ""
            else:
                if key == 'q':
                    break
                elif key == 'r':
                    crabs = make_crabs(width, height, desk_x, desk_y, dealer_x, dealer_y)
                    assign_wallets(crabs, auto_trader)
                    for crab in crabs:
                        wl = CRAB_WL.get(crab.name, {})
                        crab.wins = wl.get("wins", 0)
                        crab.losses = wl.get("losses", 0)
                        if crab.wallet:
                            crab.wallet_balance = wallet_feed.get_balance(crab.wallet)
                            for _m in APPROVED_TOKENS:
                                crab.token_balances[_m] = wallet_feed.get_token_balance(crab.wallet, _m)
                    signal_board = SignalBoard(crabs, auto_trader)
                    swimmers = []  # swimmers removed
                    tick = 0
                    last_trend = ""
                    is_night = False
                    girlfriends = []
                    bpm = 0
                    tick_speed = TICK
                    bpm_input_mode = False
                    feed_visible = True
                    last_chat_count = 0
                    clear_screen()
                elif key in ('+', '='):
                    if bpm == 0:
                        tick_speed = max(0.03, tick_speed - 0.03)
                elif key in ('-', '_'):
                    if bpm == 0:
                        tick_speed = min(0.5, tick_speed + 0.03)
                elif key == 'b':
                    bpm_input_mode = True
                    bpm_input_buf = ""
                elif key == 't':
                    feed_visible = not feed_visible
                    clear_screen()
                elif key == 'n':
                    is_night = not is_night
                    clear_screen()
                    if is_night:
                        # Girlfriends arrive!
                        girlfriends = []
                        used_gf_names = set()
                        for crab in crabs:
                            gf_name = random.choice(GF_NAMES)
                            while gf_name in used_gf_names:
                                gf_name = random.choice(GF_NAMES)
                            used_gf_names.add(gf_name)
                            girlfriends.append(Girlfriend(width, height, random.choice(GF_COLORS), gf_name, crab))
                    else:
                        # Girlfriends leave
                        for gf in girlfriends:
                            if gf.state != "gone":
                                gf.state = "leaving"
                                gf.facing_right = random.choice([True, False])
                        for crab in crabs:
                            if crab.state == "dancing":
                                crab.state = "idle"
                                crab.idle_countdown = random.randint(30, 80)
                                crab.mood = random.choice(["missing her", "great memories", "back to work"])
                                crab.action_msg = "had a great time!"
                                crab.action_timer = 20
                elif key == 'p':
                    pass  # signal board has no kill
                elif key == 'k':
                    burn_input_mode = True
                    burn_input_buf = ""
                    pass  # burn mode entered
                elif key == 'g':
                    # Cycle camera through locations
                    idx = LOCATIONS.index(world.camera) if world.camera in LOCATIONS else 0
                    world.camera = LOCATIONS[(idx + 1) % len(LOCATIONS)]
                    world._save()
                    if world.camera == "holders":
                        holder_feed.refresh()
                    clear_screen()
                elif key == 'h':
                    # Manual refresh holders (diffs balances to detect jeets/accumulators)
                    if world.camera == "holders":
                        holder_feed.refresh()
                elif key == 'x':
                    # PANIC SELL — dump all non-PINCHIN (snipe) positions immediately
                    for cn in list(auto_trader.positions.keys()):
                        for mt, ps in list(auto_trader.positions.get(cn, {}).items()):
                            if mt != PINCHIN_CONTRACT and ps.get("tokens", 0) > 0:
                                auto_trader._sell_snipe(cn, mt, ps, "PANIC")
                    for crab in crabs:
                        if crab.state not in ("starring",):
                            crab.state = "celebrating"
                            crab.state_timer = 80
                            crab.mood = "DUMPING"
                            crab.action_msg = "SELL SELL SELL!"
                            crab.action_timer = 999

            price_now = price_feed.get()

            # Hot-reload evolved strategy (~every 60s)
            auto_trader._maybe_reload_evolved()

            # Check generation evolution trigger
            evo_result = gen_tracker.check_evolution()
            if evo_result:
                best_name, worst_name = evo_result
                gen_num = gen_tracker.number
                recap = f"Gen {gen_num}: {best_name} led, {worst_name} trailed"
                # Write fitness scores for OpenEvolve
                try:
                    fitness = {}
                    for r in gen_tracker.get_ranked():
                        fitness[r["name"]] = {"sharpe": r["sharpe"], "pnl": r["pnl"],
                                              "wins": r["wins"], "losses": r["losses"]}
                    with open(GENERATION_FITNESS_FILE, "w") as _ff:
                        json.dump({"generation": gen_num, "best": best_name,
                                   "worst": worst_name, "crabs": fitness}, _ff)
                except Exception:
                    pass
                # Touch evolved strategy file to trigger hot-reload on next check
                try:
                    os.utime(EVOLVED_STRATEGY_PATH, None)
                except Exception:
                    pass
                # Announce evolution in pump.fun chat
                try:
                    tmpl = random.choice(KRABS_CHAT_TEMPLATES["evolve"])
                    chat_poster.post(tmpl.format(gen=gen_num, best=best_name))
                except Exception:
                    pass
                # Announce evolution on Twitter
                if auto_trader.twitter_poster:
                    try:
                        best_fitness = fitness.get(best_name, {})
                        tweet_tmpl = random.choice(EVOLUTION_TWEET_TEMPLATES)
                        tweet = tweet_tmpl.format(
                            gen=gen_num,
                            next_gen=gen_num + 1,
                            best=best_name,
                            sharpe=f"{best_fitness.get('sharpe', 0):.2f}",
                            pnl=f"{best_fitness.get('pnl', 0):+.3f}",
                            wins=best_fitness.get('wins', 0),
                            losses=best_fitness.get('losses', 0),
                            link=f"https://pump.fun/coin/{PINCHIN_CONTRACT}",
                        )
                        auto_trader.twitter_poster.post(tweet)
                    except Exception:
                        pass
                # CrabBrain evolution tweet
                if hasattr(auto_trader, 'crab_brain') and auto_trader.crab_brain:
                    auto_trader.crab_brain.react("evolve", f"Gen {gen_num} complete, best crab: {best_name}, evolving to Gen {gen_num+1}")
                # Bury fallen strategy in graveyard
                try:
                    world.bury_strategy(worst_name, gen_num, fitness.get(worst_name, {}), best_name)
                except Exception:
                    pass
                # Reset generation
                gen_tracker.reset_generation(recap)
                # Trigger evolution ritual animation
                if _screen_effects:
                    _screen_effects.trigger_evolution(best_name, worst_name, gen_num + 1)
                # Flash all crabs into celebrating state
                evo_msgs = [
                    f"EVOLVED! Gen {gen_num + 1} begins!",
                    f"New generation! {best_name} was MVP!",
                    f"Evolution complete! Smarter now!",
                    f"Gen {gen_num + 1}! Adapting...",
                ]
                for crab in crabs:
                    if crab.state not in ("starring",):
                        crab.state = "celebrating"
                        crab.state_timer = CELEBRATE_TICKS
                        crab.mood = "EVOLVED"
                        crab.action_msg = random.choice(evo_msgs)
                        crab.action_timer = 999

            # Detect pump and trigger celebration!
            current_trend = price_now["trend"]
            if current_trend == "up" and last_trend not in ("up", ""):
                for crab in crabs:
                    if crab.state not in ("celebrating", "starring"):
                        crab.state = "celebrating"
                        crab.state_timer = CELEBRATE_TICKS
                        crab.mood = "HYPED"
                        crab.action_msg = random.choice(CELEBRATE_MSGS)
                        crab.action_timer = 999
            if current_trend:
                last_trend = current_trend

            for crab in crabs:
                crab.update()
                # Pick up trade thoughts from AutoTrader
                if crab.name in auto_trader.pending_thoughts:
                    thought = auto_trader.pending_thoughts.pop(crab.name)
                    if thoughts_visible:
                        crab.trade_thought = thought
                        crab.trade_thought_timer = 40  # ~5 seconds at 8 tps
                if crab.trade_thought_timer > 0:
                    crab.trade_thought_timer -= 1
                    if crab.trade_thought_timer <= 0:
                        crab.trade_thought = ""
            # Periodically have a crab "think" about the market (show evolved logic)
            if tick % 120 == 0 and auto_trader._evolved_strategy:  # ~every 15s
                for _crab in crabs:
                    if _crab.name in BENCHED_CRABS:
                        continue
                    if _crab.wallet and _crab.trade_thought_timer <= 0:
                        auto_trader.decide_and_trade(_crab.name)

            # Check snipe exits every ~8 seconds (64 ticks at 8 tps)
            if tick % 64 == 32:
                threading.Thread(target=auto_trader.check_snipe_exits, daemon=True).start()

            # Position summoning crabs in orbit around price board
            board_cx = width // 2
            board_cy = (water_rows + sand_start) // 2
            orbit_r = 12
            for crab in crabs:
                if crab.state == "summoning":
                    ox = int(board_cx + orbit_r * math.cos(crab.summon_angle))
                    oy = int(board_cy + (orbit_r // 3) * math.sin(crab.summon_angle))
                    crab.x = max(0, min(width - crab.width, ox))
                    crab.y = max(0, min(height - crab.height, oy))
            for gf in girlfriends:
                if gf.state != "gone":
                    gf.update()
            girlfriends = [gf for gf in girlfriends if gf.state != "gone"]

            # Update wallet balances + detect deposits
            for crab in crabs:
                if crab.wallet:
                    old_bal = crab.wallet_balance
                    new_bal = wallet_feed.get_balance(crab.wallet)
                    # Detect external deposit: SOL jumped up by > 0.01 (not from a sell)
                    if old_bal > 0 and new_bal - old_bal > 0.01:
                        deposit_amt = new_bal - old_bal
                        CRAB_DEPOSITS[crab.name] = CRAB_DEPOSITS.get(crab.name, 0) + deposit_amt
                        save_deposits(CRAB_DEPOSITS)
                    crab.wallet_balance = new_bal
                    for _m in APPROVED_TOKENS:
                        crab.token_balances[_m] = wallet_feed.get_token_balance(crab.wallet, _m)

            # Whale alert - big buy triggers summoning
            if trade_feed.pop_whale_alert():
                board_cx = width // 2
                board_cy = (water_rows + sand_start) // 2
                for ci, crab in enumerate(crabs):
                    if crab.state not in ("summoning", "starring"):
                        angle = (2 * math.pi * ci) / len(crabs)
                        crab.state = "summoning"
                        crab.state_timer = SUMMON_TICKS
                        crab.summon_angle = angle
                        crab.mood = "WHALE!"
                        crab.action_msg = "WHALE ALERT!"
                        crab.action_timer = 999
                # Trigger whale splash animation
                if _screen_effects:
                    _screen_effects.trigger_whale(width, sand_start)

            # Process chat commands from bridge
            chat_cmds = chat_bridge.pop_commands()
            # Also scan new messages for commands (HTTP bridge doesn't queue)
            chat_msgs_now = chat_bridge.get_messages()
            new_chat_msgs = []
            if chat_msgs_now and last_chat_count < len(chat_msgs_now):
                new_chat_msgs = chat_msgs_now[last_chat_count:]
                for m in new_chat_msgs:
                    cmd = m["msg"].strip().lower()
                    if cmd.startswith("!"):
                        already = any(c["cmd"] == cmd and c["user"] == m["user"] for c in chat_cmds)
                        if not already:
                            chat_cmds.append({"cmd": cmd, "user": m["user"], "raw": m["msg"].strip()})
                last_chat_count = len(chat_msgs_now)
            signal_board.tick(chat_cmds)
            for cmd_info in chat_cmds:
                cmd = cmd_info["cmd"] if isinstance(cmd_info, dict) else cmd_info
                if cmd in ("!party", "!celebrate", "!pump"):
                    for crab in crabs:
                        if crab.state not in ("celebrating", "dancing", "starring"):
                            crab.state = "celebrating"
                            crab.state_timer = CELEBRATE_TICKS
                            crab.mood = "HYPED"
                            crab.action_msg = random.choice(CELEBRATE_MSGS)
                            crab.action_timer = 999
                elif cmd in ("!dance", "!night"):
                    if not is_night:
                        is_night = True
                        clear_screen()
                        girlfriends = []
                        used_gf_names = set()
                        for crab in crabs:
                            gf_name = random.choice(GF_NAMES)
                            while gf_name in used_gf_names:
                                gf_name = random.choice(GF_NAMES)
                            used_gf_names.add(gf_name)
                            girlfriends.append(Girlfriend(width, height, random.choice(GF_COLORS), gf_name, crab))
                elif cmd == "!day":
                    if is_night:
                        is_night = False
                        clear_screen()
                        for gf in girlfriends:
                            if gf.state != "gone":
                                gf.state = "leaving"
                        for crab in crabs:
                            if crab.state == "dancing":
                                crab.state = "idle"
                                crab.idle_countdown = random.randint(30, 80)
                elif cmd == "!summon":
                    board_cx = width // 2
                    board_cy = (water_rows + sand_start) // 2
                    orbit_r = 12
                    for ci, crab in enumerate(crabs):
                        angle = (2 * math.pi * ci) / len(crabs)
                        crab.state = "summoning"
                        crab.state_timer = SUMMON_TICKS
                        crab.summon_angle = angle
                        crab.mood = "channeling"
                        crab.action_msg = random.choice(SUMMON_MSGS)
                        crab.action_timer = 9999
                elif cmd == "!crashout":
                    crashout_msgs = [
                        "AHHH!!!", "IM DONE", "SELL IT ALL",
                        "WHY WHY WHY", "NO NO NO", "ITS OVER",
                        "PANIC!!!", "RUG PULL?!", "I CANT LOOK",
                        "WERE COOKED", "BRO WHAT", "NOT AGAIN",
                    ]
                    for crab in crabs:
                        crab.state = "idle"
                        crab.idle_countdown = 0
                        crab.dx = random.choice([-2, -1, 1, 2])
                        crab.dy = random.choice([-1, 0, 1])
                        crab.mood = "PANICKING"
                        crab.action_msg = random.choice(crashout_msgs)
                        crab.action_timer = 80
                        if thoughts_visible:
                            crab.trade_thought = random.choice(crashout_msgs)
                            crab.trade_thought_timer = 60
                elif cmd == "!meditate":
                    meditate_msgs = [
                        "finding inner peace...",
                        "clearing the mind...",
                        "one with the market...",
                        "breathing deeply...",
                        "channeling zen...",
                        "letting go of FUD...",
                    ]
                    for crab in crabs:
                        if crab.state not in ("dancing", "summoning", "meditating", "starring"):
                            crab.state = "meditating"
                            crab.state_timer = random.randint(80, 160)
                            crab.mood = "zen"
                            crab.action_msg = random.choice(meditate_msgs)
                            crab.action_timer = 999
                elif cmd == "!mutechat":
                    chat_poster.mute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Chat posting MUTED", "color": "system"})
                elif cmd == "!unmutechat":
                    chat_poster.unmute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Chat posting UNMUTED", "color": "system"})
                elif cmd.startswith("!tweet "):
                    tweet_text = cmd_info.get("raw", "")[7:].strip() if isinstance(cmd_info, dict) else ""
                    if tweet_text:
                        twitter_poster.post(tweet_text)
                        with chat_bridge._lock:
                            chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Tweet queued: {tweet_text[:60]}", "color": "system"})
                    else:
                        with chat_bridge._lock:
                            chat_bridge.messages.append({"user": "SYSTEM", "msg": "Usage: !tweet <message>", "color": "system"})
                elif cmd == "!burn":
                    # Check kill file each time (external kill)
                    if not _burn_killed:
                        _burn_killed = os.path.exists(os.path.expanduser("~/.pinchin_kill"))
                    if _burn_killed:
                        with chat_bridge._lock:
                            chat_bridge.messages.append({"user": "SYSTEM", "msg": "BURN KILLED — use !unkill to re-enable", "color": "system"})
                    elif _burn_crabs:
                        burn_crab = _burn_crabs[_burn_idx % len(_burn_crabs)]
                        _burn_idx += 1
                        next_crab = _burn_crabs[_burn_idx % len(_burn_crabs)]
                        with chat_bridge._lock:
                            chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Burning 1% of {burn_crab}'s PINCHIN... (next: {next_crab})", "color": "system"})
                        # Trigger walk-to-burn animation on the crab
                        for crab in crabs:
                            if crab.name == burn_crab:
                                crab.burn_origin_x = crab.x
                                crab.burn_origin_y = crab.y
                                crab.state = "walking_to_burn"
                                crab.mood = "pyro mode"
                                crab.action_msg = "BURN!"
                                crab.action_timer = 999
                                break
                        # Trigger big screen-wide flame animation
                        if _screen_effects:
                            _screen_effects.trigger_burn(burn_crab)
                        threading.Thread(target=auto_trader._execute_burn, args=(burn_crab, PINCHIN_CONTRACT, 0.01), daemon=True).start()
                    else:
                        with chat_bridge._lock:
                            chat_bridge.messages.append({"user": "SYSTEM", "msg": "No burn crabs loaded", "color": "system"})
                elif cmd == "!kill":
                    _burn_killed = True
                    # Drop kill file so it persists across restarts
                    open(os.path.expanduser("~/.pinchin_kill"), "w").close()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "KILL SWITCH ON — burns disabled", "color": "system"})
                elif cmd == "!unkill":
                    _burn_killed = False
                    kill_path = os.path.expanduser("~/.pinchin_kill")
                    if os.path.exists(kill_path):
                        os.remove(kill_path)
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Kill switch OFF — burns re-enabled", "color": "system"})
                elif cmd == "!mutetweets":
                    twitter_poster.mute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Twitter posting MUTED", "color": "system"})
                elif cmd == "!unmutetweets":
                    twitter_poster.unmute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Twitter posting UNMUTED", "color": "system"})
                elif cmd == "!mutecrab":
                    crab_brain.mute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "CrabBrain AI tweets MUTED", "color": "system"})
                elif cmd == "!unmutecrab":
                    crab_brain.unmute()
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "CrabBrain AI tweets UNMUTED", "color": "system"})
                elif cmd in ("!mutethoughts", "!stfu"):
                    thoughts_visible = False
                    for crab in crabs:
                        crab.trade_thought = ""
                        crab.trade_thought_timer = 0
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Thought bubbles OFF", "color": "system"})
                elif cmd in ("!unmutethoughts", "!think"):
                    thoughts_visible = True
                    with chat_bridge._lock:
                        chat_bridge.messages.append({"user": "SYSTEM", "msg": "Thought bubbles ON", "color": "system"})
                elif cmd.startswith("!goto"):
                    parts = cmd.split()
                    if len(parts) >= 2:
                        loc_input = parts[1].lower()
                        loc = LOCATION_ALIASES.get(loc_input)
                        if loc:
                            world.camera = loc
                            world._save()
                            clear_screen()
                            with chat_bridge._lock:
                                chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Camera moved to {loc.upper()}", "color": "system"})
                        else:
                            with chat_bridge._lock:
                                chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Unknown location: {loc_input}. Try: beach, lab, graveyard, bank", "color": "system"})
                elif cmd.startswith("!move"):
                    parts = cmd.split()
                    if len(parts) >= 3:
                        target = parts[1].lower()
                        loc_input = parts[2].lower()
                        loc = LOCATION_ALIASES.get(loc_input)
                        if not loc:
                            with chat_bridge._lock:
                                chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Unknown location: {loc_input}", "color": "system"})
                        elif target == "all":
                            world.move_all(loc)
                            with chat_bridge._lock:
                                chat_bridge.messages.append({"user": "SYSTEM", "msg": f"All crabs moved to {loc.upper()}", "color": "system"})
                        else:
                            crab_name = CRAB_NAME_SHORTCUTS.get(target)
                            if not crab_name:
                                # Try direct match
                                for n in CRAB_WALLETS:
                                    if n.lower() == target:
                                        crab_name = n
                                        break
                            if crab_name and world.move_crab(crab_name, loc):
                                with chat_bridge._lock:
                                    chat_bridge.messages.append({"user": "SYSTEM", "msg": f"{crab_name} moved to {loc.upper()}", "color": "system"})
                            else:
                                with chat_bridge._lock:
                                    chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Unknown crab: {target}", "color": "system"})
                elif cmd == "!list":
                    loc_groups = {}
                    for n, l in world.crab_locations.items():
                        loc_groups.setdefault(l, []).append(n)
                    for loc_name in LOCATIONS:
                        names = loc_groups.get(loc_name, [])
                        if names:
                            with chat_bridge._lock:
                                chat_bridge.messages.append({"user": "SYSTEM", "msg": f"{loc_name.upper()}: {', '.join(names)}", "color": "system"})
                elif cmd == "!look":
                    clear_screen()

            # --- @mention detection: individual crab responses ---
            for m in new_chat_msgs:
                msg_lower = m["msg"].strip().lower()
                if msg_lower.startswith("!"):
                    continue  # already handled as command
                mentioned_crab = None
                for crab_name, pdata in CRAB_PERSONALITIES.items():
                    for alias in pdata["aliases"]:
                        if alias in msg_lower or ("@" + alias) in msg_lower:
                            # Find the crab object with this name
                            for c in crabs:
                                if c.name == crab_name:
                                    mentioned_crab = c
                                    break
                            break
                    if mentioned_crab:
                        break
                if mentioned_crab:
                    response = random.choice(CRAB_PERSONALITIES[mentioned_crab.name]["responses"])
                    if thoughts_visible:
                        mentioned_crab.trade_thought = response
                        mentioned_crab.trade_thought_timer = 56  # ~7s at 8 tps
                    mentioned_crab.mood = "excited"
                    # Inject response into local chat display
                    with chat_bridge._lock:
                        chat_bridge.messages.append({
                            "user": mentioned_crab.name,
                            "msg": response,
                        })
                        if len(chat_bridge.messages) > CHAT_MAX_MESSAGES:
                            chat_bridge.messages = chat_bridge.messages[-CHAT_MAX_MESSAGES:]

            # BPM sync - lock all dance counters to global tick
            if bpm > 0:
                for crab in crabs:
                    if crab.state == "dancing":
                        crab.dance_tick = tick
                for gf in girlfriends:
                    if gf.state == "dancing":
                        gf.dance_frame = tick

            # Merge trade feed + crab trades + chat for display
            if feed_visible:
                trades = trade_feed.get_messages()
                chats = chat_bridge.get_messages()
                # Crab auto-trade log entries
                crab_trades = []
                for t in auto_trader.get_trade_log():
                    action = t["action"]
                    if action in ("BUY_FAIL", "SELL_FAIL"):
                        continue
                    sol_str = f"{t['sol']:.3f}" if t['sol'] > 0 else ""
                    if action == "BUY":
                        msg = f"{t['crab']} bought {t['token']}"
                        color = "crab_buy"
                    else:
                        msg = f"{t['crab']} sold {t['token']} ({action})"
                        color = "crab_sell"
                    if sol_str:
                        msg += f" for {sol_str} SOL"
                    crab_trades.append({"user": t["crab"], "msg": msg, "color": color})
                chat_formatted = [{"user": m["user"], "msg": m["msg"], "color": m.get("color", "chat")} for m in chats]
                feed_now = trades + crab_trades + chat_formatted
                feed_now = feed_now[-(FEED_MAX_MESSAGES * 2):]
            else:
                feed_now = None
            if burn_input_mode:
                _input_display = f"BURN %> {burn_input_buf}"
            elif bpm_input_mode:
                _input_display = bpm_input_buf
            else:
                _input_display = None
            draw(crabs, swimmers, width, height, tick, desk_x, desk_y, dealer_x, dealer_y, price_now, is_night, girlfriends, bpm, _input_display, feed_now, auto_trader, signal_board.get_display(), price_feed, gen_tracker, world, holder_feed)
            tick += 1
            # Burn reminder every 30 min
            if time.time() >= _burn_reminder and not _burn_killed:
                next_crab = _burn_crabs[_burn_idx % len(_burn_crabs)] if _burn_crabs else "?"
                with chat_bridge._lock:
                    chat_bridge.messages.append({"user": "SYSTEM", "msg": f"Burn reminder — type !burn (next: {next_crab})", "color": "system"})
                _burn_reminder = time.time() + 1800
            time.sleep(tick_speed)
    except KeyboardInterrupt:
        pass
    finally:
        price_feed.stop()
        wallet_feed.stop()
        trade_feed.stop()
        chat_bridge.stop()
        if price_history_collector:
            price_history_collector.stop()
        cleanup_kb()
        show_cursor()
        clear_screen()
        print(f"\n{ORANGE}  The crabs wave goodbye! {MINI_CRAB_R} {MINI_CRAB_L}{RESET}")
        print(f"{DIM}  (markets are closed){RESET}\n")


if __name__ == "__main__":
    main()
