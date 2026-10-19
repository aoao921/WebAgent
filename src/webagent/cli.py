import argparse
import asyncio
import json
import sys

from .agent import WebAgent
from .config import Settings
from .demo import run_demo
from .store import CheckpointStore


def main():
    parser = argparse.ArgumentParser(prog="webagent", description="A lightweight agent for browser tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a task using your configured model")
    run.add_argument("--url", required=True)
    run.add_argument("--goal", required=True)
    run.add_argument("--allow-origin", action="append", default=[])
    run.add_argument("--coding", action="store_true", help="Expose offline Docker Python execution")
    run.add_argument("--sandbox-image", default="python:3.12-slim")
    run.add_argument("--max-turns", type=int, default=32)
    run.add_argument("--max-tool-calls", type=int, default=64)
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--mcp-url")
    demo = commands.add_parser("demo", help="Real local browser demo using a scripted model; no API key")
    demo.add_argument("--mcp-url")
    inspect = commands.add_parser("inspect", help="Read a private local run record (may contain task data)")
    inspect.add_argument("run_id")
    settings = Settings.from_env()
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            if not args.run_id.isalnum():
                raise ValueError("Run ID must be an alphanumeric identifier")
            path = settings.data_dir / args.run_id / "state.sqlite3"
            if not path.is_file():
                raise ValueError("Unknown run ID")
            value = CheckpointStore(path).get_agent_run(args.run_id, args.run_id)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return
        if args.command == "demo":
            result = asyncio.run(run_demo(args.mcp_url or settings.mcp_url, settings.data_dir))
        else:
            if args.mcp_url:
                from dataclasses import replace
                settings = replace(settings, mcp_url=args.mcp_url)
            result = asyncio.run(WebAgent(settings).run(url=args.url, goal=args.goal, allowed_origins=args.allow_origin,
                coding=args.coding, sandbox_image=args.sandbox_image, max_turns=args.max_turns,
                max_tool_calls=args.max_tool_calls, timeout=args.timeout))
        print(json.dumps({"run_id": result.run_id, "status": result.status.value, "output": result.output,
                          "error": result.error, "audit_directory": str(settings.data_dir / result.run_id)}, ensure_ascii=False, indent=2))
        if not result.success:
            sys.exit(1)
    except KeyboardInterrupt:
        print("Task interrupted; inspect local audit records for its last known state.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"WebAgent: {exc}", file=sys.stderr)
        sys.exit(1)
