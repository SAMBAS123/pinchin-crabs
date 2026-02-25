#!/usr/bin/env python3
"""Top-level script to run OpenEvolve on crab trading strategies.

Usage:
    python run_evolution.py              # Run with default config (100 iterations)
    python run_evolution.py 10           # Quick test with 10 iterations
    python run_evolution.py --deploy     # Deploy best strategy after evolution
    python run_evolution.py --check      # Just check if price history is ready
"""

import asyncio
import json
import os
import sys
import importlib

# Ensure the installed openevolve package is found, not the local git checkout
# The local openevolve/ dir shadows the installed package when running from project root
def _import_openevolve():
    """Import the installed openevolve package, bypassing local directory shadow."""
    import importlib.util
    # Try the normal import first
    spec = importlib.util.find_spec("openevolve")
    if spec and spec.origin and "site-packages" in spec.origin:
        return importlib.import_module("openevolve")
    # If shadowed, temporarily remove project dir from sys.path
    saved = [p for p in sys.path if os.path.realpath(p) == os.path.realpath(os.path.dirname(os.path.abspath(__file__)))]
    for p in saved:
        sys.path.remove(p)
    # Also remove any cached openevolve module
    for key in list(sys.modules.keys()):
        if key == "openevolve" or key.startswith("openevolve."):
            del sys.modules[key]
    mod = importlib.import_module("openevolve")
    # Restore paths
    for p in saved:
        sys.path.append(p)
    return mod

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CRAB_EVOLVE_DIR = os.path.join(SCRIPT_DIR, "crab_evolve")
INITIAL_PROGRAM = os.path.join(CRAB_EVOLVE_DIR, "crab_strategy.py")
EVALUATOR_FILE = os.path.join(CRAB_EVOLVE_DIR, "evaluator.py")
CONFIG_FILE = os.path.join(CRAB_EVOLVE_DIR, "crab_evolve_config.yaml")
OUTPUT_DIR = os.path.join(CRAB_EVOLVE_DIR, "openevolve_output")
HISTORY_FILE = os.path.expanduser("~/.pinchin_price_history.json")

MIN_SNAPSHOTS = 240  # ~1 hour at 15s intervals


def check_price_history():
    """Verify we have enough price history for backtesting."""
    if not os.path.exists(HISTORY_FILE):
        print(f"No price history found at {HISTORY_FILE}")
        print("Run crab_sim.py for at least 30 minutes to collect data first.")
        return False

    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        count = len([s for s in data if s.get("price", 0) > 0])
        hours = (count * 15) / 3600
        print(f"Price history: {count} snapshots (~{hours:.1f} hours)")
        if count < MIN_SNAPSHOTS:
            print(f"Need at least {MIN_SNAPSHOTS} snapshots (~{MIN_SNAPSHOTS * 15 / 3600:.1f} hours).")
            print("Run crab_sim.py longer to collect more data.")
            return False
        print("Price history OK!")
        return True
    except Exception as e:
        print(f"Error reading price history: {e}")
        return False


async def run_evolution(iterations=None):
    """Run OpenEvolve to evolve crab trading strategies."""
    oe = _import_openevolve()
    OpenEvolve = oe.OpenEvolve

    print("=" * 60)
    print("  CRAB STRATEGY EVOLUTION")
    print("  Powered by OpenEvolve")
    print("=" * 60)
    print()

    # Check prerequisites
    if not check_price_history():
        return False

    for path, label in [(INITIAL_PROGRAM, "seed strategy"), (EVALUATOR_FILE, "evaluator"), (CONFIG_FILE, "config")]:
        if not os.path.exists(path):
            print(f"Missing {label}: {path}")
            return False

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print()
    print(f"Seed strategy: {INITIAL_PROGRAM}")
    print(f"Evaluator:     {EVALUATOR_FILE}")
    print(f"Config:        {CONFIG_FILE}")
    print(f"Output:        {OUTPUT_DIR}")
    if iterations:
        print(f"Iterations:    {iterations}")
    print()

    # Create and run OpenEvolve
    evolve = OpenEvolve(
        initial_program_path=INITIAL_PROGRAM,
        evaluation_file=EVALUATOR_FILE,
        config_path=CONFIG_FILE,
        output_dir=OUTPUT_DIR,
    )

    kwargs = {}
    if iterations:
        kwargs["iterations"] = iterations

    print("Starting evolution...")
    print("(This will take a while. Check openevolve_output/ for progress.)")
    print()

    best_program = await evolve.run(**kwargs)

    print()
    print("=" * 60)
    print("  EVOLUTION COMPLETE!")
    print("=" * 60)

    best_path = os.path.join(OUTPUT_DIR, "best", "best_program.py")
    if os.path.exists(best_path):
        print(f"Best strategy saved to: {best_path}")
    else:
        print("Best program generated (check output dir for details).")

    return True


def deploy_best():
    """Deploy the best evolved strategy."""
    sys.path.insert(0, CRAB_EVOLVE_DIR)
    from deploy import deploy
    return deploy()


def main():
    args = sys.argv[1:]

    if "--check" in args:
        check_price_history()
        return

    if "--deploy" in args:
        deploy_best()
        return

    # Parse iteration count
    iterations = None
    for arg in args:
        if arg.isdigit():
            iterations = int(arg)
            break

    # Run evolution
    success = asyncio.run(run_evolution(iterations))

    # Auto-deploy if successful
    if success:
        print()
        print("Auto-deploying best strategy...")
        deploy_best()


if __name__ == "__main__":
    main()
