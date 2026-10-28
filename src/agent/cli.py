"""Run one real-model or offline-mock knowledge agent task from the terminal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .engine import KnowledgeAgent
from .planners import APIPlanner, MockPlanner, PlannerError
from .tools import DEFAULT_DATA, KnowledgeTools

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path = ROOT / ".env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_agent(mode: str = "mock", data: str | Path = DEFAULT_DATA, fail_tools: dict[str, int] | None = None,
                max_steps: int = 10, max_tool_calls: int = 7) -> KnowledgeAgent:
    load_env()
    planner = MockPlanner() if mode == "mock" else APIPlanner()
    return KnowledgeAgent(KnowledgeTools(data, fail_tools), planner, mode, max_steps, max_tool_calls)


def parse_fail_tools(items: list[str]) -> dict[str, int]:
    result = {}
    for item in items:
        tool, sep, count = item.partition(":")
        if not sep or tool not in ("search_documents", "read_document") or not count.isdigit():
            raise ValueError("--fail-tool must look like search_documents:1")
        result[tool] = int(count)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Enterprise Knowledge QA Agent (synthetic data)")
    parser.add_argument("--question", required=True)
    parser.add_argument("--mode", choices=["mock", "llm"], default="mock")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--history", help="Path to JSON array of {role,content} messages")
    parser.add_argument("--trace-out", help="Save complete answer + observable execution trace as UTF-8 JSON")
    parser.add_argument("--fail-tool", action="append", default=[], help="Example: search_documents:1")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-tool-calls", type=int, default=7)
    parser.add_argument("--print-trace", action="store_true")
    args = parser.parse_args()
    history = json.loads(Path(args.history).read_text(encoding="utf-8")) if args.history else []
    try:
        agent = build_agent(args.mode, args.data, parse_fail_tools(args.fail_tool), args.max_steps, args.max_tool_calls)
    except (ValueError, PlannerError) as exc:
        parser.error(str(exc))
    result = agent.run(args.question, history).as_dict()
    if args.trace_out:
        path = Path(args.trace_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    brief = result if args.print_trace else {k: v for k, v in result.items() if k != "trace"}
    print(json.dumps(brief, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
