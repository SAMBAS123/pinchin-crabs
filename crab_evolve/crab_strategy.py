"""Evolvable crab trading strategy.

OpenEvolve will modify ONLY the code between EVOLVE-BLOCK markers.
The decide() function receives sensor data and returns a trading decision.
"""

import sys
import os

try:
    from crab_evolve.strategy_base import CrabStrategyBase
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from strategy_base import CrabStrategyBase


class EvolvedCrabStrategy(CrabStrategyBase):
    """Trading strategy that OpenEvolve will optimize."""

    def decide(self, sensors: dict) -> dict:
        # EVOLVE-BLOCK-START

        price = sensors["price_usd"]
        change_5m = sensors["change_5m"]
        change_1h = sensors["change_1h"]
        trend = sensors["trend"]
        has_position = sensors["has_position"]
        tokens = sensors["tokens"]
        avg_price = sensors["avg_price"]
        unrealized_pnl_pct = sensors["unrealized_pnl_pct"]
        history = sensors["price_history_5m"]
        volatility = sensors["volatility"]
        sol_balance = sensors["sol_balance"]
        minutes_since_last_trade = sensors["minutes_since_last_trade"]
        total_trades = sensors["total_trades"]

        # --- Take profit: sell half at 2x ---
        if has_position and avg_price > 0:
            gain_ratio = price / avg_price
            if gain_ratio >= 2.0:
                return {
                    "action": "sell",
                    "amount_fraction": 0.5,
                    "reason": f"TP@{gain_ratio:.1f}x",
                }

        # --- Buy the dip ---
        if change_5m <= -3.0 or (change_5m <= -1.5 and trend == "down"):
            return {
                "action": "buy",
                "amount_fraction": 0.3,
                "reason": f"dip buy 5m={change_5m:.1f}%",
            }

        # --- Sell the rip ---
        if change_5m >= 5.0 or (change_5m >= 3.0 and trend == "up"):
            if has_position and tokens > 0:
                return {
                    "action": "sell",
                    "amount_fraction": 0.5,
                    "reason": f"rip sell 5m={change_5m:.1f}%",
                }

        # --- Big hourly dip accumulation ---
        if change_1h <= -10.0 and not has_position:
            return {
                "action": "buy",
                "amount_fraction": 0.4,
                "reason": f"big dip 1h={change_1h:.1f}%",
            }

        # --- Slow accumulate when flat ---
        if change_5m <= 0 and not has_position:
            return {
                "action": "buy",
                "amount_fraction": 0.2,
                "reason": "nibble flat market",
            }

        return {
            "action": "hold",
            "amount_fraction": 0.0,
            "reason": "no signal",
        }

        # EVOLVE-BLOCK-END
