"""Market simulator for backtesting evolved crab strategies.

Replays price history, simulates buy/sell execution with slippage,
builds sensor dicts at each step, and tracks equity/PnL metrics.
"""

import math
import statistics


SLIPPAGE_PCT = 0.01  # 1% slippage on each trade
STEP_INTERVAL_SEC = 15  # seconds between price snapshots


class MarketSimulator:
    """Backtests a strategy against recorded price history."""

    def __init__(self, price_snapshots, initial_sol=0.1):
        """
        Args:
            price_snapshots: list of {"ts": float, "price": float, ...}
            initial_sol: starting SOL balance
        """
        self.snapshots = price_snapshots
        self.initial_sol = initial_sol

    def run_episode(self, strategy, start_idx=0, end_idx=None):
        """Run a single backtest episode.

        Args:
            strategy: object with decide(sensors) -> dict
            start_idx: starting index in snapshots
            end_idx: ending index (None = end of data)

        Returns:
            dict with episode results: equity_curve, trades, final metrics
        """
        if end_idx is None:
            end_idx = len(self.snapshots)

        prices = self.snapshots[start_idx:end_idx]
        if len(prices) < 20:
            return self._empty_result()

        sol_balance = self.initial_sol
        tokens = 0
        avg_price = 0.0
        total_trades = 0
        wins = 0
        losses = 0
        last_trade_step = -9999
        equity_curve = []
        trades = []

        for step, snap in enumerate(prices):
            price = snap["price"]
            if price <= 0:
                continue

            # Build sensors
            sensors = self._build_sensors(
                step, prices, price, sol_balance, tokens, avg_price,
                last_trade_step, total_trades
            )

            # Get strategy decision
            try:
                decision = strategy.decide(sensors)
            except Exception:
                decision = {"action": "hold", "amount_fraction": 0.0, "reason": "error"}

            action = decision.get("action", "hold")
            fraction = max(0.0, min(1.0, decision.get("amount_fraction", 0.0)))

            # Execute trade
            if action == "buy" and fraction > 0 and sol_balance > 0.001:
                sol_to_spend = sol_balance * fraction
                effective_price = price * (1 + SLIPPAGE_PCT)  # slippage makes buy more expensive
                tokens_bought = (sol_to_spend / effective_price) * 1e9  # token units
                tokens_bought = int(tokens_bought)

                if tokens_bought > 0:
                    # Update average price
                    if tokens > 0 and avg_price > 0:
                        total_value = (tokens * avg_price) + (tokens_bought * price)
                        avg_price = total_value / (tokens + tokens_bought)
                    else:
                        avg_price = price
                    tokens += tokens_bought
                    sol_balance -= sol_to_spend
                    total_trades += 1
                    last_trade_step = step
                    trades.append({
                        "step": step, "action": "buy", "price": price,
                        "tokens": tokens_bought, "sol": sol_to_spend
                    })

            elif action == "sell" and fraction > 0 and tokens > 0:
                tokens_to_sell = int(tokens * fraction)
                if tokens_to_sell > 0:
                    effective_price = price * (1 - SLIPPAGE_PCT)  # slippage makes sell cheaper
                    sol_received = (tokens_to_sell * effective_price) / 1e9
                    sol_balance += sol_received
                    tokens -= tokens_to_sell
                    total_trades += 1
                    last_trade_step = step

                    # Track win/loss
                    if avg_price > 0:
                        if price > avg_price:
                            wins += 1
                        else:
                            losses += 1

                    trades.append({
                        "step": step, "action": "sell", "price": price,
                        "tokens": tokens_to_sell, "sol": sol_received
                    })

                    if tokens == 0:
                        avg_price = 0.0

            # Track equity (SOL + token value in SOL)
            token_value_sol = (tokens * price) / 1e9 if tokens > 0 else 0
            equity = sol_balance + token_value_sol
            equity_curve.append(equity)

        # Calculate final metrics
        final_equity = equity_curve[-1] if equity_curve else self.initial_sol
        pnl = final_equity - self.initial_sol
        pnl_pct = (pnl / self.initial_sol) * 100 if self.initial_sol > 0 else 0

        return {
            "equity_curve": equity_curve,
            "trades": trades,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / max(1, wins + losses),
            "final_equity": final_equity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "sharpe": self._calc_sharpe(equity_curve),
            "max_drawdown": self._calc_max_drawdown(equity_curve),
        }

    def run_episodes(self, strategy, num_episodes=5, window_size=None):
        """Run multiple episodes with rolling windows.

        Args:
            strategy: object with decide(sensors)
            num_episodes: number of episodes
            window_size: snapshots per episode (None = split evenly)

        Returns:
            list of episode result dicts
        """
        n = len(self.snapshots)
        if window_size is None:
            window_size = max(240, n // num_episodes)  # at least 1 hour worth

        results = []
        for i in range(num_episodes):
            # Rolling window: spread episodes across the data
            if num_episodes > 1:
                start = int(i * (n - window_size) / max(1, num_episodes - 1))
            else:
                start = 0
            start = max(0, min(start, n - window_size))
            end = min(start + window_size, n)

            if end - start < 20:
                continue

            result = self.run_episode(strategy, start_idx=start, end_idx=end)
            results.append(result)

        return results

    def _build_sensors(self, step, prices, current_price, sol_balance, tokens,
                       avg_price, last_trade_step, total_trades):
        """Build the sensor dict that the strategy sees."""
        # Calculate 5-min and 1-hr changes
        # At 15s intervals: 5min = 20 steps back, 1hr = 240 steps back
        change_5m = 0.0
        change_1h = 0.0
        if step >= 20 and prices[step - 20]["price"] > 0:
            old = prices[step - 20]["price"]
            change_5m = ((current_price - old) / old) * 100
        if step >= 240 and prices[step - 240]["price"] > 0:
            old = prices[step - 240]["price"]
            change_1h = ((current_price - old) / old) * 100

        # Trend from last 4 snapshots (~1 minute)
        trend = ""
        if step >= 4:
            recent = [prices[step - j]["price"] for j in range(4, -1, -1)]
            if all(p > 0 for p in recent):
                if recent[-1] > recent[0]:
                    trend = "up"
                elif recent[-1] < recent[0]:
                    trend = "down"

        # Price history: 12 prices at ~5 min intervals
        price_history_5m = []
        for k in range(12):
            idx = step - (11 - k) * 20
            if 0 <= idx < len(prices):
                price_history_5m.append(prices[idx]["price"])

        # Volatility: stddev/mean of last 20 prices
        volatility = 0.0
        if step >= 20:
            recent_prices = [prices[step - j]["price"] for j in range(20) if prices[step - j]["price"] > 0]
            if len(recent_prices) >= 2:
                mean = statistics.mean(recent_prices)
                if mean > 0:
                    volatility = statistics.stdev(recent_prices) / mean

        # Unrealized PnL
        unrealized_pnl_pct = 0.0
        if tokens > 0 and avg_price > 0:
            unrealized_pnl_pct = ((current_price - avg_price) / avg_price) * 100

        # Minutes since last trade
        steps_since = step - last_trade_step
        minutes_since = (steps_since * STEP_INTERVAL_SEC) / 60

        return {
            "price_usd": current_price,
            "change_5m": change_5m,
            "change_1h": change_1h,
            "trend": trend,
            "has_position": tokens > 0,
            "tokens": tokens,
            "avg_price": avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "price_history_5m": price_history_5m,
            "volatility": volatility,
            "sol_balance": sol_balance,
            "minutes_since_last_trade": minutes_since,
            "total_trades": total_trades,
        }

    def _calc_sharpe(self, equity_curve, risk_free=0.0):
        """Calculate annualized Sharpe ratio from equity curve."""
        if len(equity_curve) < 2:
            return 0.0
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                r = (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                returns.append(r)
        if not returns or len(returns) < 2:
            return 0.0
        mean_ret = statistics.mean(returns)
        std_ret = statistics.stdev(returns)
        if std_ret == 0:
            return 0.0
        # Annualize: each step is 15s, so ~2.1M steps/year
        steps_per_year = 365.25 * 24 * 3600 / STEP_INTERVAL_SEC
        sharpe = ((mean_ret - risk_free) / std_ret) * math.sqrt(steps_per_year)
        return sharpe

    def _calc_max_drawdown(self, equity_curve):
        """Calculate maximum drawdown as a fraction (0 to 1)."""
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def _empty_result(self):
        return {
            "equity_curve": [],
            "trades": [],
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "final_equity": self.initial_sol,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
