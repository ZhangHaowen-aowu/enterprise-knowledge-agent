"""Execute fixed synthetic evaluation cases and preserve ACTUAL run logs, not invented traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cli import ROOT, build_agent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "llm"], default="mock")
    parser.add_argument("--cases", default=str(ROOT / "examples/cases.json"))
    parser.add_argument("--output", help="Defaults to examples/traces/<mode>")
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    output = Path(args.output or ROOT / "examples/traces" / args.mode)
    output.mkdir(parents=True, exist_ok=True)
    summary = []
    for case in cases:
        agent = build_agent(args.mode, fail_tools=case.get("fail_tools", {}), max_steps=args.max_steps)
        result = agent.run(case["question"], case.get("history", [])).as_dict()
        artifact = {"case_id": case["id"], "question": case["question"], "history": case.get("history", []),
                    "failure_injection": case.get("fail_tools", {}), "expected_status": case["expected_status"], **result}
        (output / (case["id"] + ".json")).write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record = {"case": case["id"], "expected": case["expected_status"], "actual": result["status"],
                  "passed": result["status"] == case["expected_status"], "tool_calls": result["tool_calls"],
                  "steps": result["steps"], "tool_path": [e["tool"] for e in result["trace"] if e["event"] == "tool_call"]}
        summary.append(record)
        print(f'{case["id"]:28} {result["status"]:28} expected={case["expected_status"]}')
    overall = {"mode": args.mode, "note": "Mock is scripted, not evidence of real LLM decisions." if args.mode == "mock" else "Actual external model outputs; inspect for quality and factual support.",
               "passed": sum(int(c["passed"]) for c in summary), "total": len(summary), "cases": summary}
    (output / "summary.json").write_text(json.dumps(overall, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f'\nWrote {len(cases)} real execution traces to: {output}')
    if overall["passed"] != overall["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
