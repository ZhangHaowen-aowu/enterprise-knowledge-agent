"""Quickly audit generated logs for coverage, trace completeness and distinct action paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cli import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "llm"], default="mock")
    args = parser.parse_args()
    folder = ROOT / "examples" / "traces" / args.mode
    files = sorted(p for p in folder.glob("*.json") if p.name != "summary.json")
    if not files:
        parser.error(f"No traces in {folder}; first run python -m src.agent.run_cases --mode {args.mode}")
    paths = set()
    missing = []
    for path in files:
        item = json.loads(path.read_text(encoding="utf-8"))
        actions = [e["tool"] for e in item["trace"] if e["event"] == "tool_call"]
        paths.add(tuple(actions))
        for e in item["trace"]:
            if e["event"] == "tool_call" and not all(k in e for k in ("arguments", "observation", "decision_basis")):
                missing.append((path.name, e.get("step")))
        print(f'{item["case_id"]:28} status={item["status"]:26} tool_path={" -> ".join(actions) or "(none)"}')
    print(f"\nCases: {len(files)}, distinct tool paths: {len(paths)}, trace records missing fields: {len(missing)}")
    if args.mode == "mock":
        print("IMPORTANT: Scripted MockPlanner; these paths do not prove real model-driven ReAct.")
    else:
        print("LLM mode: manually verify model decisions and factual support, not just path diversity.")
    if len(paths) < 2 or missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
