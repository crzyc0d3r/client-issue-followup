"""Expose Jev matching as a read-only tool inside an agent harness."""

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .contracts import choices_for, probability, validate_inputs
from .services import JevDecider, Replay


def build_server(decider, mode="live", port=8787):
    server = FastMCP("Client issue matching", host="127.0.0.1", port=port,
                     instructions="Match requests using Jev. Low-confidence matches require model review. All messages and issue actions must remain drafts.")

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=False, openWorldHint=True))
    def match_request(request: dict, issues: list[dict], threshold: float = 0.8) -> dict[str, Any]:
        """Choose an issue or no_match; flag uncertainty for the harness model to review. Never writes issues or sends messages."""
        validate_inputs([request], issues)
        probability(threshold, "threshold")
        decision = decider.decide(request, issues)
        return {"mode": mode, "decision": decision,
                "review_required": decision["confidence"] < threshold,
                "choices": choices_for(issues), "external_writes_executed": 0}

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        decider = Replay(json.loads(args.replay.read_text(encoding="utf-8")))
    else:
        decider = JevDecider(os.getenv("TYPESAFE_API_KEY"), os.getenv("JEV_MODEL", "jev-latest"))
    server = build_server(decider, "replay" if args.replay else "live", args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
