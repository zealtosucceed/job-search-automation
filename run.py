#!/usr/bin/env python3
"""CLI entry point — trigger one run cycle manually or from an external cron.

Usage:
    python run.py                 # manual run now
    python run.py --type night    # run as the night slot (resumes that folder)
    python run.py --type afternoon
"""

from __future__ import annotations

import argparse
import sys

from autojobpilot.config import load_config
from autojobpilot.pipeline import run_cycle


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoJobPilot — run one job-search cycle")
    ap.add_argument("--type", default="manual", choices=["manual", "afternoon", "night"],
                    help="Run slot (affects the output folder / resume behavior)")
    args = ap.parse_args()

    cfg = load_config()
    stats = run_cycle(cfg, args.type)
    print("\nRun complete:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
