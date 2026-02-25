"""Fitness evaluator for OpenEvolve.

Loads an evolved strategy, runs backtest episodes, and returns
weighted fitness metrics. This is the file OpenEvolve calls.
"""

import importlib.util
import json
import math
import os
import sys
import statistics
import tempfile
from pathlib import Path

# Add parent dir so we can import crab_evolve modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sim import MarketSimulator
from price_history import HISTORY_FILE

# Fitness weights
W_PNL = 0.30
W_SHARPE = 0.25
W_DRAWDOWN = 0.20
W_WINRATE = 0.15
W_ACTIVITY = 0.10

NUM_EPISODES = 5


def _load_price_history():
    """Load saved price history from disk."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [s for s in data if s.get("price", 0) > 0]
    except Exception:
        pass
    return []


def _load_strategy(program_code):
    """Load an evolved strategy from code string."""
    # Write to temp file and import
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="crab_strat_",
        dir=os.path.dirname(os.path.abspath(__file__)),
        delete=False
    )
    try:
        tmp.write(program_code)
        tmp.flush()
        tmp.close()

        spec = importlib.util.spec_from_file_location("evolved_strategy", tmp.name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find the strategy class
        strategy = module.EvolvedCrabStrategy()
        return strategy
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _normalize_pnl(pnl_pct):
    """Normalize PnL % to 0-1 score. 0% -> 0.5, positive -> higher, negative -> lower."""
    # sigmoid-like mapping: pnl of +50% -> ~0.88, -50% -> ~0.12
    return 1.0 / (1.0 + math.exp(-pnl_pct / 25.0))


def _normalize_sharpe(sharpe):
    """Normalize Sharpe ratio to 0-1. Sharpe of 2 -> ~0.73."""
    # Clamp to reasonable range
    sharpe = max(-5, min(10, sharpe))
    return 1.0 / (1.0 + math.exp(-sharpe / 2.0))


def _normalize_drawdown(max_dd):
    """Normalize drawdown to 0-1 score (lower drawdown = higher score)."""
    # max_dd is 0-1 (fraction), invert so less drawdown = better
    return max(0.0, 1.0 - max_dd)


def _normalize_winrate(win_rate):
    """Win rate is already 0-1."""
    return win_rate


def _activity_score(avg_trades_per_episode, target_min=3, target_max=50):
    """Score for trading activity. Penalizes both too few and too many trades.

    Strategies that always hold (0 trades) get 0.
    Target sweet spot is 3-50 trades per episode.
    """
    if avg_trades_per_episode < 1:
        return 0.0
    if target_min <= avg_trades_per_episode <= target_max:
        return 1.0
    if avg_trades_per_episode < target_min:
        return avg_trades_per_episode / target_min
    # Too many trades
    return max(0.1, 1.0 - (avg_trades_per_episode - target_max) / (target_max * 2))


def evaluate(program_path_or_code: str) -> dict:
    """OpenEvolve entry point: evaluate an evolved strategy.

    OpenEvolve passes a file path to the temp file containing evolved code.
    We read it and run backtests.

    Args:
        program_path_or_code: path to the evolved strategy file, or raw code string

    Returns:
        dict of metric_name -> float score
    """
    # OpenEvolve passes a file path; read the code from it
    if os.path.isfile(program_path_or_code):
        with open(program_path_or_code) as f:
            program_code = f.read()
    else:
        program_code = program_path_or_code

    # Load price history
    snapshots = _load_price_history()
    if len(snapshots) < 100:
        # Not enough data - return neutral scores
        return {
            "pnl_score": 0.5,
            "sharpe_score": 0.5,
            "drawdown_score": 0.5,
            "winrate_score": 0.5,
            "activity_score": 0.0,
            "combined_score": 0.25,
        }

    # Load strategy from evolved code
    try:
        strategy = _load_strategy(program_code)
    except Exception as e:
        # Strategy failed to load - return zero
        import logging
        logging.getLogger(__name__).warning(f"Strategy load failed: {e}")
        return {
            "pnl_score": 0.0,
            "sharpe_score": 0.0,
            "drawdown_score": 0.0,
            "winrate_score": 0.0,
            "activity_score": 0.0,
            "combined_score": 0.0,
        }

    # Run backtest episodes
    sim = MarketSimulator(snapshots, initial_sol=0.1)
    try:
        results = sim.run_episodes(strategy, num_episodes=NUM_EPISODES)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Backtest failed: {e}")
        return {
            "pnl_score": 0.0,
            "sharpe_score": 0.0,
            "drawdown_score": 0.0,
            "winrate_score": 0.0,
            "activity_score": 0.0,
            "combined_score": 0.0,
        }

    if not results:
        return {
            "pnl_score": 0.5,
            "sharpe_score": 0.5,
            "drawdown_score": 0.5,
            "winrate_score": 0.5,
            "activity_score": 0.0,
            "combined_score": 0.25,
        }

    # Aggregate across episodes
    pnl_pcts = [r["pnl_pct"] for r in results]
    sharpes = [r["sharpe"] for r in results]
    drawdowns = [r["max_drawdown"] for r in results]
    win_rates = [r["win_rate"] for r in results]
    trade_counts = [r["total_trades"] for r in results]

    avg_pnl = statistics.mean(pnl_pcts)
    avg_sharpe = statistics.mean(sharpes)
    avg_drawdown = statistics.mean(drawdowns)
    avg_winrate = statistics.mean(win_rates)
    avg_trades = statistics.mean(trade_counts)

    # Normalize to 0-1 scores
    pnl_score = _normalize_pnl(avg_pnl)
    sharpe_score = _normalize_sharpe(avg_sharpe)
    drawdown_score = _normalize_drawdown(avg_drawdown)
    winrate_score = _normalize_winrate(avg_winrate)
    act_score = _activity_score(avg_trades)

    # Weighted combined score
    combined = (
        W_PNL * pnl_score
        + W_SHARPE * sharpe_score
        + W_DRAWDOWN * drawdown_score
        + W_WINRATE * winrate_score
        + W_ACTIVITY * act_score
    )

    return {
        "pnl_score": round(pnl_score, 4),
        "sharpe_score": round(sharpe_score, 4),
        "drawdown_score": round(drawdown_score, 4),
        "winrate_score": round(winrate_score, 4),
        "activity_score": round(act_score, 4),
        "combined_score": round(combined, 4),
    }
