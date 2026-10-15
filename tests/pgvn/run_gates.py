"""Explicit milestone gate entry point. Unimplemented gates never pass silently."""
from pathlib import Path
import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milestone", required=True, choices=[f"M{i}" for i in range(8)])
    args = parser.parse_args()
    test = Path(__file__).resolve().parent / ("test_" + args.milestone.lower() + ".py")
    if not test.exists():
        parser.error(f"{args.milestone} gate is not implemented yet")
    return subprocess.call([sys.executable, str(test)])


if __name__ == "__main__":
    sys.exit(main())
