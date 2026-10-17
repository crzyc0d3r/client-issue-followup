"""Command-line entry point; all external write actions remain local proposals."""

import argparse
import json
import os
from pathlib import Path

from .harness import TrueForgeReviewer
from .pipeline import run, save
from .services import JevDecider, Replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, default=Path("data/requests.json"))
    parser.add_argument("--issues", type=Path, default=Path("data/issues.json"))
    parser.add_argument("--replay", type=Path, help="Use synthetic recorded decisions and reviews; makes no service calls")
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    requests = json.loads(args.requests.read_text(encoding="utf-8"))
    issues = json.loads(args.issues.read_text(encoding="utf-8"))
    if args.replay:
        decider = reviewer = Replay(json.loads(args.replay.read_text(encoding="utf-8")))
    else:
        decider = JevDecider(os.getenv("TYPESAFE_API_KEY"), os.getenv("JEV_MODEL", "jev-latest"))
        reviewer = TrueForgeReviewer(os.getenv("TRUEFORGE_BASE_URL", "http://localhost:8790"),
                                     os.getenv("TRUEFORGE_REVIEW_MODEL"), os.getenv("TRUEFORGE_TOKEN"),
                                     os.getenv("TRUEFORGE_SANDBOX_ENVIRONMENT"))
    result = run(requests, issues, decider, reviewer, args.threshold, "replay" if args.replay else "live")
    path = save(result, args.output)
    print(json.dumps({"output": str(path), **result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
