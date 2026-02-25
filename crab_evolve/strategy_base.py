"""Abstract base class for crab trading strategies."""


class CrabStrategyBase:
    """Base class that all evolved crab strategies must implement.

    The decide() method receives a sensor dict and returns a trading decision.

    Sensors:
        price_usd (float):              Current token price in USD
        change_5m (float):              5-minute % price change
        change_1h (float):              1-hour % price change
        trend (str):                    "up", "down", or ""
        has_position (bool):            Whether we hold tokens
        tokens (int):                   Current token balance
        avg_price (float):              Average entry price (0 if no position)
        unrealized_pnl_pct (float):     Unrealized P&L as percentage
        price_history_5m (list[float]): Last 12 prices (5-min intervals, ~1hr window)
        volatility (float):             Recent stddev/mean ratio
        sol_balance (float):            Available SOL for trading
        minutes_since_last_trade (float): Minutes since last trade
        total_trades (int):             Total trades executed so far

    Returns:
        dict with keys:
            action (str):           "buy", "sell", or "hold"
            amount_fraction (float): 0.0 to 1.0 (fraction of available balance to use)
            reason (str):           Human-readable reason for the decision
    """

    def decide(self, sensors: dict) -> dict:
        raise NotImplementedError("Subclasses must implement decide()")
