"""Deploy best evolved strategy for live use by crab_sim.

Copies the best evolved strategy to ~/.pinchin_evolved_strategy.py
so that crab_sim.py can hot-reload it during runtime.
"""

import json
import os
import shutil
import sys
from pathlib import Path

DEPLOY_PATH = os.path.expanduser("~/.pinchin_evolved_strategy.py")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openevolve_output")


def find_best_program(output_dir=None):
    """Find the best evolved program from OpenEvolve output."""
    output_dir = output_dir or OUTPUT_DIR
    best_path = os.path.join(output_dir, "best", "best_program.py")
    if os.path.exists(best_path):
        return best_path
    # Fallback: look for latest checkpoint
    checkpoints = sorted(Path(output_dir).glob("checkpoint_*/best_program.py"))
    if checkpoints:
        return str(checkpoints[-1])
    return None


def deploy(source_path=None, deploy_path=None):
    """Copy best evolved strategy to deployment location.

    Args:
        source_path: path to evolved strategy file (None = auto-detect)
        deploy_path: where to deploy (None = default ~/.pinchin_evolved_strategy.py)
    """
    deploy_path = deploy_path or DEPLOY_PATH
    if source_path is None:
        source_path = find_best_program()

    if not source_path or not os.path.exists(source_path):
        print(f"No evolved strategy found to deploy.")
        return False

    try:
        shutil.copy2(source_path, deploy_path)
        print(f"Deployed evolved strategy:")
        print(f"  From: {source_path}")
        print(f"  To:   {deploy_path}")

        # Also save deployment metadata
        meta_path = deploy_path + ".meta.json"
        meta = {
            "source": source_path,
            "deployed_at": __import__("time").time(),
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        return True
    except Exception as e:
        print(f"Deploy failed: {e}")
        return False


if __name__ == "__main__":
    deploy()
