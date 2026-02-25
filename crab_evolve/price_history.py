"""Collects and persists price history for backtesting.

Runs as a background thread alongside PriceFeed, snapshotting prices
at regular intervals and saving to disk.
"""

import json
import os
import threading
import time

HISTORY_FILE = os.path.expanduser("~/.pinchin_price_history.json")
SNAPSHOT_INTERVAL = 15  # seconds between snapshots


class PriceHistoryCollector:
    """Background collector that records price snapshots from PriceFeed."""

    def __init__(self, price_feed, history_file=None):
        self.price_feed = price_feed
        self.history_file = history_file or HISTORY_FILE
        self.snapshots = []  # list of {"ts": float, "price": float, "change_5m": float, "change_1h": float}
        self.alive = True
        self._lock = threading.Lock()
        self._load()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _load(self):
        """Load existing history from disk."""
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file) as f:
                data = json.load(f)
            if isinstance(data, list):
                self.snapshots = data
        except Exception:
            pass

    def _save(self):
        """Persist history to disk."""
        try:
            with open(self.history_file, "w") as f:
                json.dump(self.snapshots, f)
        except Exception:
            pass

    def _poll(self):
        """Snapshot loop - runs in background thread."""
        save_counter = 0
        while self.alive:
            try:
                price_data = self.price_feed.get()
                price = price_data.get("price", 0)
                if price > 0:
                    snapshot = {
                        "ts": time.time(),
                        "price": price,
                        "change_5m": price_data.get("change_5m", 0),
                        "change_1h": price_data.get("change_1h", 0),
                    }
                    with self._lock:
                        self.snapshots.append(snapshot)
                        # Keep last 30 days max (~172800 snapshots at 15s intervals)
                        max_snapshots = 172800
                        if len(self.snapshots) > max_snapshots:
                            self.snapshots = self.snapshots[-max_snapshots:]

                    save_counter += 1
                    if save_counter >= 20:  # save every ~5 minutes
                        self._save()
                        save_counter = 0
            except Exception:
                pass
            time.sleep(SNAPSHOT_INTERVAL)

    def get_prices(self, count=None):
        """Get price history as list of floats (most recent last)."""
        with self._lock:
            snaps = list(self.snapshots)
        prices = [s["price"] for s in snaps if s.get("price", 0) > 0]
        if count:
            prices = prices[-count:]
        return prices

    def get_snapshots(self, count=None):
        """Get full snapshot dicts."""
        with self._lock:
            snaps = list(self.snapshots)
        if count:
            snaps = snaps[-count:]
        return snaps

    def get_5m_prices(self, count=12):
        """Get prices sampled at ~5min intervals (for sensor building).

        Returns list of `count` prices, each ~5 minutes apart.
        """
        with self._lock:
            snaps = list(self.snapshots)
        if not snaps:
            return []

        # 5 min = 300s, at 15s intervals = every 20 snapshots
        step = max(1, 300 // SNAPSHOT_INTERVAL)
        sampled = []
        for i in range(len(snaps) - 1, -1, -step):
            sampled.append(snaps[i]["price"])
            if len(sampled) >= count:
                break
        sampled.reverse()
        return sampled

    def has_enough_history(self, min_snapshots=240):
        """Check if we have enough data for backtesting (~1hr at 15s intervals)."""
        with self._lock:
            return len(self.snapshots) >= min_snapshots

    def stop(self):
        self.alive = False
        self._save()
