#!/usr/bin/env python3
"""A cute little random crab simulation!"""

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


# --- Live price feed for $PINCHIN ---
PINCHIN_CONTRACT = "5xQibgLSix2ptJ4mvvcPPYmnBxEhbtR4DB2YxVs1pump"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Approved tokens crabs can buy (mint -> name)
APPROVED_TOKENS = {
    PINCHIN_CONTRACT: "PINCHIN",
}

# Trading config
TRADE_AMOUNT_SOL = 0.001  # SOL per trade
TRADE_COOLDOWN = 120  # seconds between trades per crab
INITIAL_BAG_SOL = 0.01  # SOL each crab spends on startup to get a bag
STARTING_SOL = 0.1  # each crab started with this much SOL
KEYS_FILE = os.path.expanduser("~/.pinchin_keys.json")
POSITIONS_FILE = os.path.expanduser("~/.pinchin_positions.json")
DEXSCREENER_URL = f"https://api.dexscreener.com/latest/dex/tokens/{PINCHIN_CONTRACT}"

class PriceFeed:
    def __init__(self):
        self.price_usd = 0.0
        self.price_native = 0.0  # price in SOL
        self.price_change_5m = 0.0
        self.price_change_1h = 0.0
        self.market_cap = 0.0
        self.symbol = "PINCHIN"
        self.last_price = 0.0
        self.trend = ""  # "up", "down", ""
        self.alive = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self):
        while self.alive:
            try:
                req = urllib.request.Request(DEXSCREENER_URL, headers={"User-Agent": "CrabSim/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                pairs = data.get("pairs", [])
                if pairs:
                    p = pairs[0]
                    with self._lock:
                        self.last_price = self.price_usd
                        self.price_usd = float(p.get("priceUsd", 0))
                        self.price_native = float(p.get("priceNative", 0) or 0)
                        self.market_cap = float(p.get("marketCap", 0) or 0)
                        changes = p.get("priceChange", {})
                        self.price_change_5m = float(changes.get("m5", 0) or 0)
                        self.price_change_1h = float(changes.get("h1", 0) or 0)
                        if self.last_price > 0:
                            self.trend = "up" if self.price_usd > self.last_price else "down" if self.price_usd < self.last_price else self.trend
            except Exception:
                pass
            time.sleep(15)  # poll every 15 seconds

    def get(self):
        with self._lock:
            return {
                "price": self.price_usd,
                "price_sol": self.price_native,
                "mc": self.market_cap,
                "change_5m": self.price_change_5m,
                "change_1h": self.price_change_1h,
                "trend": self.trend,
            }

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
}

SOLANA_RPC = "https://api.mainnet-beta.solana.com"


class WalletFeed:
    def __init__(self):
        self.balances = {}  # wallet_addr -> SOL balance
        self.token_balances = {}  # wallet_addr -> PINCHIN balance
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
                        SOLANA_RPC, data=payload,
                        headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read())
                    lamports = data.get("result", {}).get("value", 0)
                    with self._lock:
                        self.balances[addr] = lamports / 1_000_000_000
                except Exception:
                    pass

                time.sleep(2)  # avoid rate limits

                # $PINCHIN token balance
                try:
                    payload = json.dumps({
                        "jsonrpc": "2.0", "id": 2,
                        "method": "getTokenAccountsByOwner",
                        "params": [addr,
                            {"mint": PINCHIN_CONTRACT},
                            {"encoding": "jsonParsed"}]
                    }).encode()
                    req = urllib.request.Request(
                        SOLANA_RPC, data=payload,
                        headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read())
                    accounts = data.get("result", {}).get("value", [])
                    if accounts:
                        info = accounts[0]["account"]["data"]["parsed"]["info"]
                        ui_amount = float(info["tokenAmount"]["uiAmount"])
                        with self._lock:
                            self.token_balances[addr] = ui_amount
                except Exception:
                    pass

                time.sleep(2)  # avoid rate limits
            time.sleep(15)

    def get_balance(self, addr):
        with self._lock:
            return self.balances.get(addr, 0.0)

    def get_token_balance(self, addr):
        with self._lock:
            return self.token_balances.get(addr, 0.0)

    def stop(self):
        self.alive = False


# --- Auto Trader (Jupiter swap via api.jup.ag) ---
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

EVOLVED_STRATEGY_PATH = os.path.expanduser("~/.pinchin_evolved_strategy.py")
EVOLVED_RELOAD_INTERVAL = 60  # seconds between hot-reload checks

class AutoTrader:
    """Buy the dip, sell the rip. Take 50% profit at 2x."""
    def __init__(self):
        self.keypairs = {}  # crab_name -> Keypair
        self.last_trade = {}  # crab_name -> timestamp
        self.trade_log = []
        self.jup_api_key = ""
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
        price_data = self.price_feed.get() if self.price_feed else None
        if not price_data or price_data["price"] <= 0:
            return None

        pos = self.positions.get(crab_name, {}).get(mint)
        tokens = pos["tokens"] if pos else 0
        avg_price = pos["avg_price"] if pos else 0.0
        has_position = tokens > 0

        unrealized_pnl_pct = 0.0
        if has_position and avg_price > 0:
            unrealized_pnl_pct = ((price_data["price"] - avg_price) / avg_price) * 100

        # Price history from collector
        price_history_5m = []
        if self.price_history:
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

        if action == "buy" and fraction > 0:
            sol_amount = trade_sol * fraction
            self.last_trade[crab_name] = time.time()
            t = threading.Thread(
                target=self._execute_buy,
                args=(crab_name, mint, current_price, sol_amount),
                daemon=True
            )
            t.start()
            return True
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
            for name, privkey_str in keys.items():
                if name == "JUP_API_KEY":
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

    def decide_and_trade(self, crab_name, token_mint=None):
        """Decide whether to buy or sell using evolved strategy."""
        if self.community_controlled and self.community_controlled == crab_name:
            return False  # chat controls this crab, skip AI
        if not self.can_trade(crab_name):
            return False
        mint = token_mint or PINCHIN_CONTRACT
        if mint not in APPROVED_TOKENS:
            return False

        price_data = self.price_feed.get() if self.price_feed else None
        if not price_data or price_data["price"] <= 0:
            return False

        if not self._evolved_strategy:
            return False

        pos = self.positions.get(crab_name, {}).get(mint)
        trade_sol = TRADE_AMOUNT_SOL
        mod = self.crab_modifiers.get(crab_name)
        if mod and time.time() < mod["expires"]:
            trade_sol *= mod["trade_mult"]
        current_price = price_data["price"]
        try:
            return self._try_evolved_strategy(crab_name, mint, current_price, trade_sol, pos)
        except Exception:
            return False

    def buy_initial_bags(self):
        """On startup, each crab buys an initial bag of $PINCHIN if they don't hold much on-chain."""
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

    def _execute_buy(self, crab_name, mint, price_at_decision, sol_amount=None):
        try:
            from solders.transaction import VersionedTransaction

            sol_amount = sol_amount or TRADE_AMOUNT_SOL
            kp = self.keypairs[crab_name]
            pubkey = str(kp.pubkey())
            lamports = int(sol_amount * 1_000_000_000)
            headers = self._jup_headers()

            # 1. Quote
            quote_url = (
                f"{JUPITER_QUOTE_URL}"
                f"?inputMint={WSOL_MINT}&outputMint={mint}"
                f"&amount={lamports}&slippageBps=100"
            )
            req = urllib.request.Request(quote_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                quote = json.loads(resp.read())

            out_amount = int(quote.get("outAmount", 0))

            # 2. Swap tx
            swap_headers = dict(headers)
            swap_headers["Content-Type"] = "application/json"
            swap_payload = json.dumps({
                "quoteResponse": quote,
                "userPublicKey": pubkey,
                "wrapAndUnwrapSol": True,
            }).encode()
            req2 = urllib.request.Request(JUPITER_SWAP_URL, data=swap_payload, headers=swap_headers)
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                swap_data = json.loads(resp2.read())

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                self._log_trade(crab_name, mint, "BUY_FAIL", "no swap tx")
                return

            # 3. Sign & send
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [kp])
            tx_sig = self._send_tx(signed_tx)

            # 4. Track position
            if tx_sig:
                with self._lock:
                    if crab_name not in self.positions:
                        self.positions[crab_name] = {}
                    pos = self.positions[crab_name].get(mint, {"tokens": 0, "cost_sol": 0.0, "avg_price": 0.0})
                    pos["tokens"] += out_amount
                    pos["cost_sol"] += sol_amount
                    pos["avg_price"] = price_at_decision if pos["avg_price"] == 0 else (pos["avg_price"] + price_at_decision) / 2
                    self.positions[crab_name][mint] = pos

            self._log_trade(crab_name, mint, "BUY", tx_sig or "failed", sol_amount)
            self.save_positions()

        except Exception as e:
            self._log_trade(crab_name, mint, "BUY_FAIL", f"err: {str(e)[:40]}")

    def _execute_sell(self, crab_name, mint, token_amount, reason="SELL"):
        try:
            from solders.transaction import VersionedTransaction

            kp = self.keypairs[crab_name]
            pubkey = str(kp.pubkey())
            headers = self._jup_headers()

            # 1. Quote (selling tokens for SOL)
            quote_url = (
                f"{JUPITER_QUOTE_URL}"
                f"?inputMint={mint}&outputMint={WSOL_MINT}"
                f"&amount={token_amount}&slippageBps=100"
            )
            req = urllib.request.Request(quote_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                quote = json.loads(resp.read())

            out_lamports = int(quote.get("outAmount", 0))
            sol_received = out_lamports / 1_000_000_000

            # 2. Swap tx
            swap_headers = dict(headers)
            swap_headers["Content-Type"] = "application/json"
            swap_payload = json.dumps({
                "quoteResponse": quote,
                "userPublicKey": pubkey,
                "wrapAndUnwrapSol": True,
            }).encode()
            req2 = urllib.request.Request(JUPITER_SWAP_URL, data=swap_payload, headers=swap_headers)
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                swap_data = json.loads(resp2.read())

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                self._log_trade(crab_name, mint, "SELL_FAIL", "no swap tx")
                return

            # 3. Sign & send
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [kp])
            tx_sig = self._send_tx(signed_tx)

            # 4. Update position
            if tx_sig:
                with self._lock:
                    pos = self.positions.get(crab_name, {}).get(mint)
                    if pos:
                        pos["tokens"] = max(0, pos["tokens"] - token_amount)
                        pos["cost_sol"] = max(0, pos["cost_sol"] - (pos["cost_sol"] * token_amount / (pos["tokens"] + token_amount)))

            self._log_trade(crab_name, mint, reason, tx_sig or "failed", sol_received)
            self.save_positions()

        except Exception as e:
            self._log_trade(crab_name, mint, "SELL_FAIL", f"err: {str(e)[:40]}")

    def _send_tx(self, signed_tx):
        tx_b64 = base64.b64encode(bytes(signed_tx)).decode()
        send_payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": True}]
        }).encode()
        req = urllib.request.Request(
            SOLANA_RPC, data=send_payload,
            headers={"Content-Type": "application/json", "User-Agent": "CrabSim/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return result.get("result", "")

    def _log_trade(self, crab_name, mint, action, tx_msg, sol=0):
        token_name = APPROVED_TOKENS.get(mint, mint[:8])
        with self._lock:
            self.trade_log.append({
                "crab": crab_name,
                "action": action,
                "token": token_name,
                "sol": sol,
                "tx": tx_msg[:16] if tx_msg else "",
            })
            if len(self.trade_log) > 20:
                self.trade_log = self.trade_log[-20:]

    def get_trade_log(self):
        with self._lock:
            return list(self.trade_log)


# --- Chat Bridge (receives from Tampermonkey userscript) ---
CHAT_BRIDGE_PORT = 8420
CHAT_MAX_MESSAGES = 8

PUMP_REPLIES_URL = f"https://frontend-api-v3.pump.fun/replies/{PINCHIN_CONTRACT}?limit=10&offset=0&sort=DESC"
PUMP_POLL_INTERVAL = 20  # seconds
PUMP_CHAT_WS = "wss://livechat.pump.fun/socket.io/?EIO=4&transport=websocket"

class ChatBridge:
    def __init__(self):
        self.messages = []  # list of {"user": ..., "msg": ...}
        self.commands = []  # queued !commands
        self._lock = threading.Lock()
        self._server = None
        self._seen_ids = set()
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
        with self._lock:
            self.messages.append({"user": user, "msg": text})
            if len(self.messages) > CHAT_MAX_MESSAGES:
                self.messages = self.messages[-CHAT_MAX_MESSAGES:]

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


# --- Community Poll / Voting System ---
POLL_DURATION = 3600      # 1 hour
POLL_COOLDOWN = 30        # seconds before next poll after one ends
POLL_RESULT_TICKS = 100   # ~15 seconds to show result
POLL_MIN_VOTES = 2        # minimum votes to apply takeover


class PollManager:
    def __init__(self, crabs, auto_trader):
        self.crabs = crabs
        self.auto_trader = auto_trader
        self.active_poll = None       # {"crab_name": str, "question": str} or None
        self.votes = {}               # username -> "yes"/"no"
        self.poll_start = 0
        self.controlled_crab = None   # crab name under community control
        self.result_msg = ""
        self.result_timer = 0
        self.cooldown_until = time.time() + 10  # first poll starts after 10s
        self.buy_count = 0            # total !buy commands during takeover
        self.sell_count = 0           # total !meditate commands during takeover

    def tick(self, chat_cmds):
        now = time.time()

        # Process commands
        for cmd_info in chat_cmds:
            if not isinstance(cmd_info, dict):
                continue
            cmd = cmd_info["cmd"]
            user = cmd_info["user"]

            # Votes during active poll
            if self.active_poll and not self.controlled_crab:
                if cmd == "!voteyes":
                    self._process_vote(user, "yes")
                elif cmd == "!voteno":
                    self._process_vote(user, "no")

            # Community control commands
            if self.controlled_crab:
                if cmd == "!buy":
                    self._community_buy()
                elif cmd == "!meditate":
                    self._community_sell()

        # Voting phase: check if time expired
        if self.active_poll and not self.controlled_crab:
            if now - self.poll_start >= 3600:  # 1 hour voting window
                self._tally_votes()
                return

        # Takeover phase: check if duration expired
        if self.controlled_crab:
            if now - self.poll_start >= POLL_DURATION:
                self._end_takeover()
                return

        # Result display countdown
        if self.result_timer > 0:
            self.result_timer -= 1
            return

        # Start new poll if cooldown expired
        if not self.active_poll and not self.controlled_crab and now >= self.cooldown_until:
            self._start_poll()

    def kill(self):
        """Manually end the current poll/takeover."""
        if self.controlled_crab:
            self._end_takeover()
        elif self.active_poll:
            self.active_poll = None
            self.result_msg = "Poll cancelled."
            self.result_timer = POLL_RESULT_TICKS
            self.cooldown_until = time.time() + POLL_COOLDOWN

    def _process_vote(self, username, vote):
        if username not in self.votes:
            self.votes[username] = vote

    def _start_poll(self):
        crab_names = [c.name for c in self.crabs if c.wallet]
        name = random.choice(crab_names) if crab_names else "Sandy"
        self.active_poll = {
            "crab_name": name,
            "question": "Take over a crab strat?",
        }
        self.votes = {}
        self.poll_start = time.time()
        self.buy_count = 0
        self.sell_count = 0

    def _tally_votes(self):
        """After 1hr voting, check result and activate takeover or reject."""
        yes = sum(1 for v in self.votes.values() if v == "yes")
        no = sum(1 for v in self.votes.values() if v == "no")
        total = yes + no

        if total >= POLL_MIN_VOTES and yes > no:
            # Activate community control
            self.controlled_crab = self.active_poll["crab_name"]
            self.auto_trader.community_controlled = self.controlled_crab
            self.poll_start = time.time()  # reset timer for the takeover duration
        elif total >= POLL_MIN_VOTES and no > yes:
            self.result_msg = f"NO wins ({no}-{yes})! {self.active_poll['crab_name']} stays free."
            self.active_poll = None
            self.result_timer = POLL_RESULT_TICKS
            self.cooldown_until = time.time() + POLL_COOLDOWN
        else:
            self.result_msg = f"Not enough votes ({total}/{POLL_MIN_VOTES})"
            self.active_poll = None
            self.result_timer = POLL_RESULT_TICKS
            self.cooldown_until = time.time() + POLL_COOLDOWN

    def _end_takeover(self):
        name = self.controlled_crab
        self.result_msg = f"{name} is free! (B:{self.buy_count} S:{self.sell_count})"
        self.controlled_crab = None
        self.auto_trader.community_controlled = None
        self.active_poll = None
        self.result_timer = POLL_RESULT_TICKS
        self.cooldown_until = time.time() + POLL_COOLDOWN

    def _community_buy(self):
        name = self.controlled_crab
        if not name or name not in self.auto_trader.keypairs:
            return
        price = 0
        if self.auto_trader.price_feed:
            pd = self.auto_trader.price_feed.get()
            price = pd.get("price", 0)
        if price <= 0:
            return
        # Cooldown: at least 10s between community trades
        last = self.auto_trader.last_trade.get(name, 0)
        if time.time() - last < 10:
            return
        self.auto_trader.last_trade[name] = time.time()
        self.buy_count += 1
        t = threading.Thread(
            target=self.auto_trader._execute_buy,
            args=(name, PINCHIN_CONTRACT, price, TRADE_AMOUNT_SOL),
            daemon=True,
        )
        t.start()

    def _community_sell(self):
        name = self.controlled_crab
        if not name or name not in self.auto_trader.keypairs:
            return
        pos = self.auto_trader.positions.get(name, {}).get(PINCHIN_CONTRACT)
        if not pos or pos["tokens"] <= 0:
            return
        last = self.auto_trader.last_trade.get(name, 0)
        if time.time() - last < 10:
            return
        sell_amount = int(pos["tokens"] * 0.25)  # sell 25% per command
        if sell_amount <= 0:
            return
        self.auto_trader.last_trade[name] = time.time()
        self.sell_count += 1
        t = threading.Thread(
            target=self.auto_trader._execute_sell,
            args=(name, PINCHIN_CONTRACT, sell_amount, "CHAT_SELL"),
            daemon=True,
        )
        t.start()

    def get_display(self):
        """Return list of (text, color) tuples for rendering on poll board."""
        lines = []
        if self.controlled_crab:
            # Takeover active
            remaining = max(0, POLL_DURATION - (time.time() - self.poll_start))
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            lines.append((f"{self.controlled_crab} TAKEN OVER", BOLD + MAGENTA))
            lines.append(("CHAT CONTROLS!", BOLD + WHITE))
            lines.append(("!buy / !meditate", BOLD + CYAN))
            lines.append((f"B:{self.buy_count} S:{self.sell_count}", WHITE))
            lines.append((f"{mins}:{secs:02d} left", DIM))
        elif self.active_poll:
            # Voting phase (90s)
            remaining = max(0, 3600 - (time.time() - self.poll_start))
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            yes = sum(1 for v in self.votes.values() if v == "yes")
            no = sum(1 for v in self.votes.values() if v == "no")
            total = yes + no
            yes_bars = int(6 * yes / total) if total > 0 else 0
            no_bars = int(6 * no / total) if total > 0 else 0
            yes_bar = "#" * yes_bars + "-" * (6 - yes_bars)
            no_bar = "#" * no_bars + "-" * (6 - no_bars)

            lines.append((self.active_poll["question"], BOLD + WHITE))
            lines.append(("!voteyes / !voteno", DIM + CYAN))
            lines.append((f"Y:{yes_bar}{yes} N:{no_bar}{no}", WHITE))
            lines.append((f"{mins}:{secs:02d} to vote", DIM))
        elif self.result_timer > 0:
            lines.append(("POLL RESULT", BOLD + YELLOW))
            lines.append((self.result_msg, WHITE))
        else:
            lines.append(("Next poll soon...", DIM))
        return lines


# --- Live Trade Feed (PumpPortal) ---
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
FEED_MAX_MESSAGES = 8  # visible in feed box
WHALE_SOL_THRESHOLD = 0.5  # SOL buy triggers whale alert / summon

class TradeFeed:
    def __init__(self, token_mint=None):
        self.token_mint = token_mint or PINCHIN_CONTRACT
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
            "keys": [self.token_mint]
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
NUM_CRABS = 6
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

CRAB_COLORS = [RED, ORANGE, YELLOW, MAGENTA, GREEN, "\033[38;5;203m"]

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
    "    LEADERBOARD      ",
    " ___________________  ",
    "|                   | ",
    "|                   | ",
    "|                   | ",
    "|                   | ",
    "|                   | ",
    "|                   | ",
    "|___________________| ",
    "  ||           ||     ",
]

DESK_WIDTH = 23
DESK_HEIGHT = len(TRADING_DESK)

# Car dealership ASCII art (left side of ocean floor)
CAR_DEALER = [
    " CRAB MOTORS LLC ",
    " _______________  ",
    "| [O=|__|=O]   | ",
    "|   NEW CRABS!  | ",
    "|  0% APR FIN.  | ",
    "|_______________|  ",
    "  ||         ||   ",
]

DEALER_WIDTH = 19
DEALER_HEIGHT = len(CAR_DEALER)

# Price ticker board (center of ocean floor)
PRICE_BOARD = [
    "  $PINCHIN LIVE  ",
    " _______________  ",
    "|               | ",
    "|               | ",
    "|_______________| ",
    "  ||         ||   ",
]
BOARD_WIDTH = 19
BOARD_HEIGHT = len(PRICE_BOARD)

# Poll board (physical sign on ocean floor)
POLL_BOARD = [
    "    COMMUNITY POLL    ",
    " ____________________  ",
    "|                    | ",
    "|                    | ",
    "|                    | ",
    "|                    | ",
    "|                    | ",
    "|____________________| ",
    "  ||            ||     ",
]
POLL_BOARD_WIDTH = 24
POLL_BOARD_INNER = 20
POLL_BOARD_HEIGHT = len(POLL_BOARD)

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
        self.pinchin_balance = 0.0
        self.summon_angle = 0.0
        self.auto_trader = None  # set by main()
        self.trade_thought = ""  # e.g. "SL@-12% sell 100%"
        self.trade_thought_timer = 0  # ticks remaining to show

    @property
    def width(self):
        if self.state in ("smoking", "ogling"):
            return 8
        if self.state == "driving":
            return 11
        if self.state in ("celebrating", "dancing", "summoning", "meditating"):
            return 7
        return 10 if self.big else 7

    @property
    def height(self):
        if self.state in ("smoking", "ogling", "celebrating", "dancing", "summoning", "meditating"):
            return 1
        if self.state == "driving":
            return 2
        return 3 if self.big else 1

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
        if self.state in ("ogling", "driving", "celebrating", "dancing", "summoning", "meditating"):
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
                    token = random.choice(list(APPROVED_TOKENS.keys()))
                    self.auto_trader.decide_and_trade(self.name, token)

                # Coin flip: win or lose?
                if random.random() < 0.45:
                    # WIN - go to dealership to buy a car!
                    self.wins += 1
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

        if self.state == "idle":
            if self.bubble_life > 0:
                self.bubble_life -= 1
                self.bubble_y -= 1
            elif random.random() < 0.03:
                self.bubble_x = self.x + self.width // 2
                self.bubble_y = self.y - 1
                self.bubble_life = 3

    def render_lines(self):
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


def show_cursor():
    sys.stdout.write("\033[?25h")


BANNER_TEXT = "   6 ASCII crabs with AI-evolved trading instincts! each crab has its own solana wallet and trades on-chain via jupiter. their strategies were evolved by OpenEvolve -- smart dip buying, dynamic stop-losses, volatility-adjusted sizing, and momentum spike selling. live leaderboard tracks who's winning.   ///   $PINCHIN   ///   github.com/SAMBAS123/pinchin-crabs   ///   "

def draw(crabs, swimmers, width, height, tick_count, desk_x, desk_y, dealer_x, dealer_y, price_data=None, is_night=False, girlfriends=None, bpm=0, bpm_input=None, chat_msgs=None, auto_trader=None, poll_display=None):
    grid = [[" "] * width for _ in range(height)]
    color_grid = [[""] * width for _ in range(height)]

    water_rows = max(2, height // 8)
    sand_rows = max(2, height // 6)
    sand_start = height - sand_rows

    # Water / Night sky
    if is_night:
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
    else:
        for wy in range(water_rows):
            for wx in range(width):
                offset = (tick_count + wx + wy * 3) % 8
                grid[wy][wx] = "~" if offset < 5 else " "
                color_grid[wy][wx] = WATER_COLOR

    # Swimmers in the water (not at night)
    if not is_night:
        for swimmer in swimmers:
            if not swimmer.active:
                continue
            lines = swimmer.render_lines()
            for row_i, line in enumerate(lines):
                for col_i, ch in enumerate(line):
                    px = swimmer.x + col_i
                    py = swimmer.y + row_i
                    if 0 <= px < width and 0 <= py < height and ch != " ":
                        grid[py][px] = ch
                        color_grid[py][px] = swimmer.color

    # Scrolling banner (top row)
    banner_len = len(BANNER_TEXT)
    scroll_offset = (tick_count // 4) % banner_len
    for bx in range(width):
        ci = (scroll_offset + bx) % banner_len
        ch = BANNER_TEXT[ci]
        grid[0][bx] = ch
        color_grid[0][bx] = BOLD + YELLOW

    # Sand
    sand_color_now = NIGHT_SAND if is_night else DARK_SAND
    for sy in range(sand_start, height):
        for sx in range(width):
            if random.random() < 0.3:
                grid[sy][sx] = random.choice([".", ",", "'", " "])
            else:
                grid[sy][sx] = " "
            color_grid[sy][sx] = sand_color_now

    # Decorations
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

    # Fill leaderboard with crab PnL rankings (real balance vs starting SOL)
    if price_data and price_data.get("price", 0) > 0:
        price_per_token_sol = price_data.get("price_sol", 0)  # current market price in SOL
        lb_entries = []
        for crab in crabs:
            if not crab.wallet:
                continue
            sol_bal = crab.wallet_balance
            # Value PINCHIN at current market price (pinchin_balance is uiAmount)
            pinchin_value_sol = crab.pinchin_balance * price_per_token_sol
            total_value = sol_bal + pinchin_value_sol
            pnl_sol = total_value - STARTING_SOL
            pnl_pct = (pnl_sol / STARTING_SOL) * 100 if STARTING_SOL > 0 else 0
            lb_entries.append((crab.name, pnl_sol, pnl_pct, crab.color))
        lb_entries.sort(key=lambda x: x[1], reverse=True)
        medals = ["1.", "2.", "3.", "4.", "5.", "6."]
        for ei, (name, pnl, pnl_pct, ccolor) in enumerate(lb_entries[:6]):
            row_y = desk_y + 2 + ei
            if row_y >= desk_y + DESK_HEIGHT - 2:
                break
            sign = "+" if pnl_pct >= 0 else ""
            pct_str = f"{sign}{pnl_pct:.0f}%"
            entry = f" {medals[ei]}{name:<8}{pct_str:>8} "
            pnl_color = GREEN if pnl >= 0 else RED
            for ci, ch in enumerate(entry[:19]):
                px = desk_x + 1 + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    if ci < 3:
                        # Rank number - gold/silver/bronze for top 3
                        if ei == 0:
                            color_grid[row_y][px] = BOLD + YELLOW
                        elif ei == 1:
                            color_grid[row_y][px] = GRAY
                        elif ei == 2:
                            color_grid[row_y][px] = ORANGE
                        else:
                            color_grid[row_y][px] = DIM
                    elif ci < 11:
                        color_grid[row_y][px] = ccolor
                    else:
                        color_grid[row_y][px] = BOLD + pnl_color

    # Car dealership
    for row_i, line in enumerate(CAR_DEALER):
        for col_i, ch in enumerate(line):
            px = dealer_x + col_i
            py = dealer_y + row_i
            if 0 <= px < width and 0 <= py < height and ch != " ":
                grid[py][px] = ch
                if row_i == 0:
                    color_grid[py][px] = BOLD + CYAN
                elif ch in "O=|_[]":
                    color_grid[py][px] = YELLOW if row_i == 2 else BROWN
                elif ch == "!" or ch == "%" or ch == "0":
                    color_grid[py][px] = GREEN
                else:
                    color_grid[py][px] = BROWN

    # Price ticker board (center of ocean floor)
    board_x = width // 2 - BOARD_WIDTH // 2
    board_center_y = (water_rows + sand_start) // 2
    board_y = board_center_y - BOARD_HEIGHT // 2

    for row_i, line in enumerate(PRICE_BOARD):
        for col_i, ch in enumerate(line):
            px = board_x + col_i
            py = board_y + row_i
            if 0 <= px < width and 0 <= py < height and ch != " ":
                grid[py][px] = ch
                if row_i == 0:
                    color_grid[py][px] = BOLD + YELLOW
                else:
                    color_grid[py][px] = BROWN

    # Dynamic price data on the board
    if price_data and price_data["price"] > 0:
        p = price_data["price"]
        if p < 0.0001:
            price_str = f"${p:.8f}"
        elif p < 1:
            price_str = f"${p:.6f}"
        else:
            price_str = f"${p:.4f}"

        c5m = price_data["change_5m"]
        c5m_sign = "+" if c5m >= 0 else ""
        trend = price_data["trend"]
        trend_arrow = "^" if trend == "up" else "v" if trend == "down" else " "
        change_str = f"{trend_arrow} {c5m_sign}{c5m:.1f}% 5m"

        price_color = GREEN if trend == "up" else RED if trend == "down" else YELLOW
        change_color = GREEN if c5m >= 0 else RED

        price_padded = price_str.center(15)
        for ci, ch in enumerate(price_padded):
            px = board_x + 1 + ci
            py = board_y + 2
            if 0 <= px < width and 0 <= py < height:
                grid[py][px] = ch
                color_grid[py][px] = price_color

        change_padded = change_str.center(15)
        for ci, ch in enumerate(change_padded):
            px = board_x + 1 + ci
            py = board_y + 3
            if 0 <= px < width and 0 <= py < height:
                grid[py][px] = ch
                color_grid[py][px] = change_color
    else:
        loading = "loading...".center(15)
        for ci, ch in enumerate(loading):
            px = board_x + 1 + ci
            py = board_y + 2
            if 0 <= px < width and 0 <= py < height:
                grid[py][px] = ch
                color_grid[py][px] = YELLOW

    # Poll board (physical sign, between dealer and price board)
    if poll_display:
        poll_bx = dealer_x + DEALER_WIDTH + 3
        poll_by = board_y  # same vertical as price board
        # Draw the frame
        for row_i, line in enumerate(POLL_BOARD):
            for col_i, ch in enumerate(line):
                px = poll_bx + col_i
                py = poll_by + row_i
                if 0 <= px < width and 0 <= py < height and ch != " ":
                    grid[py][px] = ch
                    if row_i == 0:
                        color_grid[py][px] = BOLD + YELLOW
                    else:
                        color_grid[py][px] = BROWN
        # Fill dynamic content inside the board (rows 2-6, 20 chars inner)
        inner_w = POLL_BOARD_INNER
        for pi, (ptext, pcolor) in enumerate(poll_display):
            row_y = poll_by + 2 + pi
            if row_y >= poll_by + POLL_BOARD_HEIGHT - 2:
                break
            text = ptext[:inner_w].center(inner_w)
            for ci, ch in enumerate(text):
                px = poll_bx + 1 + ci
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
            thought = crab.trade_thought[:30]  # cap length
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

    # Live trade feed (right side)
    if chat_msgs:
        feed_w = 28
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
        # Trade messages only
        trade_msgs = [m for m in chat_msgs if m.get("color") in ("buy", "sell", "crab_buy", "crab_sell")]
        for mi, m in enumerate(trade_msgs[-FEED_MAX_MESSAGES:]):
            row_y = feed_y + mi
            if row_y >= height:
                break
            line = m["msg"][:feed_w]
            is_crab = m.get("color", "").startswith("crab_")
            if is_crab:
                trade_color = BOLD + GREEN if "buy" in m["color"] else BOLD + RED
            else:
                trade_color = GREEN if m["color"] == "buy" else RED
            for ci, ch in enumerate(line):
                px = feed_x + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    color_grid[row_y][px] = trade_color

    # Live chat (left side)
    if chat_msgs:
        chat_w = 56
        chat_x = 2
        chat_y = 3
        # Header
        cheader = " PUMP.FUN CHAT "
        cheader_padded = cheader.center(chat_w)
        for ci, ch in enumerate(cheader_padded):
            px = chat_x + ci
            if 0 <= px < width and chat_y - 1 < height:
                grid[chat_y - 1][px] = ch
                color_grid[chat_y - 1][px] = BOLD + CYAN
        # Chat messages only
        chat_only = [m for m in chat_msgs if m.get("color") == "chat"]
        for mi, m in enumerate(chat_only[-FEED_MAX_MESSAGES:]):
            row_y = chat_y + mi
            if row_y >= height:
                break
            user = m["user"][:10]
            msg_text = m["msg"][:chat_w - len(user) - 2]
            line = f"{user}: {msg_text}"
            for ci, ch in enumerate(line[:chat_w]):
                px = chat_x + ci
                if 0 <= px < width and 0 <= row_y < height:
                    grid[row_y][px] = ch
                    color_grid[row_y][px] = CYAN if ci < len(user) else WHITE
        # Command hints below chat
        hint_y = chat_y + min(len(chat_only), FEED_MAX_MESSAGES) + 1
        hints = [
            "!party !summon !dance !crashout",
            "!voteyes !voteno !buy !meditate",
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


    # Render
    buf = []
    border_color = CYAN
    if summoning_active and tick_count % 3 == 0:
        border_color = LIGHTNING_COLOR
    elif bpm > 0 and tick_count % 4 == 0:
        border_color = MAGENTA
    border = border_color + "+" + "-" * width + "+" + RESET
    buf.append(border)
    for y in range(height):
        row = CYAN + "|" + RESET
        for x in range(width):
            c = color_grid[y][x]
            row += c + grid[y][x]
        row += RESET + CYAN + "|" + RESET
        buf.append(row)
    buf.append(border)

    # Status bar
    status_lines = []
    for crab in crabs:
        record = f"[W:{crab.wins} L:{crab.losses}]"
        tag = f"  {crab.color}{crab.name}{RESET} {DIM}{record}{RESET}"
        if crab.wallet:
            abbr = crab.wallet
            bal = f"{crab.wallet_balance:.4f}" if crab.wallet_balance < 1 else f"{crab.wallet_balance:.2f}"
            pb = crab.pinchin_balance
            if pb >= 1_000_000:
                pinchin_str = f"{pb/1_000_000:.1f}M"
            elif pb >= 1_000:
                pinchin_str = f"{pb/1_000:.1f}K"
            elif pb > 0:
                pinchin_str = f"{pb:.0f}"
            else:
                pinchin_str = "0"
            tag += f" {DIM}[{abbr}]{RESET} {YELLOW}{bal} SOL{RESET} {GREEN}{pinchin_str} $PINCHIN{RESET}"
        tag += f" {crab.mood}"
        if crab.action_timer > 0:
            tag += f" -- {crab.action_msg}"
        status_lines.append(tag)

    CLEAR_EOL = "\033[K"
    buf.append(CLEAR_EOL)
    for sl in status_lines:
        buf.append(sl + CLEAR_EOL)
    buf.append(CLEAR_EOL)
    if bpm_input is not None:
        bpm_str = f"{MAGENTA}BPM> {bpm_input}_{RESET} "
    elif bpm > 0:
        bpm_str = f"{MAGENTA}BPM:{bpm}{RESET} "
    else:
        bpm_str = ""
    night_ind = f"{YELLOW}* NIGHT *{RESET} " if is_night else ""
    feed_ind = f"{GREEN}LIVE{RESET}{DIM} " if chat_msgs else ""
    buf.append(DIM + f"  {night_ind}{bpm_str}{feed_ind}[R] Restart  [+/-] Speed  [N] Night  [B] BPM  [T] Trades  [Q] Quit" + RESET + CLEAR_EOL)

    sys.stdout.write("\033[H")
    sys.stdout.write("\n".join(buf))
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
        print("Terminal too small! Need at least 32x20.")
        return

    init_kb()
    hide_cursor()
    clear_screen()

    claws = "}}}{{}}"
    evolved_active = os.path.isfile(os.path.expanduser("~/.pinchin_evolved_strategy.py"))
    if evolved_active:
        strategy_line = f"{GREEN}   [EVOLVED] AI-evolved trading strategies active{RESET}"
        subtitle = f"{DIM}   Crabs trade with OpenEvolve-optimized instincts!{RESET}"
    else:
        strategy_line = f"{YELLOW}   [HARDCODED] Using default trading logic{RESET}"
        subtitle = f"{DIM}   Run 'python run_evolution.py' to evolve strategies!{RESET}"
    title = (
        f"\n{CYAN}     ~~ CRAB SIMULATION ~~{RESET}\n"
        f"{ORANGE}\n"
        f"        ,~,        ,~,\n"
        f"      (O O) {RED}{claws}{ORANGE}  (O O)\n"
        f"       )_)--'-'    )_)--'-'\n"
        f"{RESET}\n"
        f"{strategy_line}\n"
        f"{subtitle}\n"
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
    trade_feed = TradeFeed()
    chat_bridge = ChatBridge()
    auto_trader = AutoTrader()
    auto_trader.price_feed = price_feed
    auto_trader.wallet_feed = wallet_feed

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
    poll_manager = PollManager(crabs, auto_trader)
    swimmers = [Swimmer(width, water_rows) for _ in range(NUM_SWIMMERS)]
    # Give them unique names
    used_swim_names = set()
    for s in swimmers:
        while s.name in used_swim_names:
            s.name = random.choice(SWIMMER_NAMES)
        used_swim_names.add(s.name)
    tick_speed = TICK

    tick = 0
    last_trend = ""
    is_night = False
    girlfriends = []
    bpm = 0
    bpm_input_mode = False
    bpm_input_buf = ""
    feed_visible = True
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

            # BPM input mode - type digits, Enter to set
            if bpm_input_mode:
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
                    poll_manager = PollManager(crabs, auto_trader)
                    swimmers = [Swimmer(width, water_rows) for _ in range(NUM_SWIMMERS)]
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
                    poll_manager.kill()

            price_now = price_feed.get()

            # Hot-reload evolved strategy (~every 60s)
            auto_trader._maybe_reload_evolved()

            # Detect pump and trigger celebration!
            current_trend = price_now["trend"]
            if current_trend == "up" and last_trend not in ("up", ""):
                for crab in crabs:
                    if crab.state != "celebrating":
                        crab.state = "celebrating"
                        crab.state_timer = CELEBRATE_TICKS
                        crab.mood = "HYPED"
                        crab.action_msg = random.choice(CELEBRATE_MSGS)
                        crab.action_timer = 999
            if current_trend:
                last_trend = current_trend

            for crab in crabs:
                crab.check_swimmers(swimmers)
                crab.update()
                # Pick up trade thoughts from AutoTrader
                if crab.name in auto_trader.pending_thoughts:
                    crab.trade_thought = auto_trader.pending_thoughts.pop(crab.name)
                    crab.trade_thought_timer = 40  # ~5 seconds at 8 tps
                if crab.trade_thought_timer > 0:
                    crab.trade_thought_timer -= 1
                    if crab.trade_thought_timer <= 0:
                        crab.trade_thought = ""
            # Periodically have a crab "think" about the market (show evolved logic)
            if tick % 120 == 0 and auto_trader._evolved_strategy:  # ~every 15s
                thinking_crab = random.choice(crabs)
                if thinking_crab.wallet and thinking_crab.trade_thought_timer <= 0:
                    auto_trader.decide_and_trade(thinking_crab.name)

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
            if not is_night:
                for swimmer in swimmers:
                    swimmer.update()
            for gf in girlfriends:
                if gf.state != "gone":
                    gf.update()
            girlfriends = [gf for gf in girlfriends if gf.state != "gone"]

            # Update wallet balances
            for crab in crabs:
                if crab.wallet:
                    crab.wallet_balance = wallet_feed.get_balance(crab.wallet)
                    crab.pinchin_balance = wallet_feed.get_token_balance(crab.wallet)

            # Whale alert - big buy triggers summoning
            if trade_feed.pop_whale_alert():
                board_cx = width // 2
                board_cy = (water_rows + sand_start) // 2
                for ci, crab in enumerate(crabs):
                    if crab.state not in ("summoning",):
                        angle = (2 * math.pi * ci) / len(crabs)
                        crab.state = "summoning"
                        crab.state_timer = SUMMON_TICKS
                        crab.summon_angle = angle
                        crab.mood = "WHALE!"
                        crab.action_msg = "WHALE ALERT!"
                        crab.action_timer = 999

            # Process chat commands from bridge
            chat_cmds = chat_bridge.pop_commands()
            # Also scan new messages for commands (HTTP bridge doesn't queue)
            chat_msgs_now = chat_bridge.get_messages()
            if chat_msgs_now and last_chat_count < len(chat_msgs_now):
                for m in chat_msgs_now[last_chat_count:]:
                    cmd = m["msg"].strip().lower()
                    if cmd.startswith("!"):
                        already = any(c["cmd"] == cmd and c["user"] == m["user"] for c in chat_cmds)
                        if not already:
                            chat_cmds.append({"cmd": cmd, "user": m["user"]})
                last_chat_count = len(chat_msgs_now)
            poll_manager.tick(chat_cmds)
            for cmd_info in chat_cmds:
                cmd = cmd_info["cmd"] if isinstance(cmd_info, dict) else cmd_info
                if cmd in ("!party", "!celebrate", "!pump"):
                    for crab in crabs:
                        if crab.state not in ("celebrating", "dancing"):
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
                        if crab.state not in ("dancing", "summoning", "meditating"):
                            crab.state = "meditating"
                            crab.state_timer = random.randint(80, 160)
                            crab.mood = "zen"
                            crab.action_msg = random.choice(meditate_msgs)
                            crab.action_timer = 999

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
                chat_formatted = [{"user": m["user"], "msg": m["msg"], "color": "chat"} for m in chats]
                feed_now = trades + crab_trades + chat_formatted
                feed_now = feed_now[-(FEED_MAX_MESSAGES * 2):]
            else:
                feed_now = None
            draw(crabs, swimmers, width, height, tick, desk_x, desk_y, dealer_x, dealer_y, price_now, is_night, girlfriends, bpm, bpm_input_buf if bpm_input_mode else None, feed_now, auto_trader, poll_manager.get_display())
            tick += 1
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
