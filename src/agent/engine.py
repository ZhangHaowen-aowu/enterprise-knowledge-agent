"""Action-observation Agent loop with citation, conflict and budget guardrails."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .planners import PlannerError
from .tools import KnowledgeTools, ToolError

VALID_STATUSES = {"answered", "needs_clarification", "insufficient_evidence", "tool_failure"}
VALID_TOOLS = {"search_documents", "read_document"}


@dataclass
class AgentResult:
    status: str
    answer: str
    citations: list[dict]
    missing_info: list[str]
    trace: list[dict]
    tool_calls: int
    steps: int
    mode: str

    def as_dict(self) -> dict:
        return {
            "status": self.status, "answer": self.answer, "citations": self.citations,
            "missing_info": self.missing_info, "tool_calls": self.tool_calls,
            "steps": self.steps, "mode": self.mode, "trace": self.trace,
        }


def _validate_final(action: dict, observations: list[dict]) -> str | None:
    if action.get("status") not in VALID_STATUSES:
        return "Invalid final status"
    if not isinstance(action.get("answer"), str):
        return "Missing answer string"
    if not isinstance(action.get("citations"), list) or not isinstance(action.get("missing_info", []), list):
        return "Citations and missing_info must be arrays"
    read_docs = {o["result"]["id"]: o["result"] for o in observations if o["tool"] == "read_document" and o["ok"]}
    referenced = set()
    for cite in action["citations"]:
        if not isinstance(cite, dict):
            return "Citation must be an object"
        doc_id, quote = cite.get("doc_id"), cite.get("quote")
        if not isinstance(doc_id, str) or doc_id not in read_docs:
            return "Citation uses a document that was not read successfully"
        if not isinstance(quote, str) or not quote.strip() or quote not in read_docs[doc_id]["content"]:
            return "Citation quote is not an exact substring of the read source"
        referenced.add(doc_id)
    if action["status"] == "answered":
        if not referenced:
            return "An answered response requires at least one exact source quotation"
        search_ids = {
            hit["doc_id"]: hit for o in observations if o["tool"] == "search_documents" and o["ok"]
            for hit in o["result"]["hits"]
        }
        cited_keys = {read_docs[d]["rule_key"] for d in referenced}
        unresolved = [doc_id for doc_id, hit in search_ids.items() if hit["rule_key"] in cited_keys and doc_id not in read_docs]
        if unresolved:
            return f"Related retrieved documents still need reading: {', '.join(sorted(unresolved))}"
    grouped = {}
    for doc in read_docs.values():
        grouped.setdefault((doc["rule_key"], doc["scope"]), []).append(doc)
    conflicting = [g for g in grouped.values() if len({d["rule_value"] for d in g}) > 1]
    if action["status"] == "answered" and conflicting:
        return "Contradictory read documents: cannot claim a definitive answer"
    if action["status"] == "needs_clarification" and conflicting:
        relevant = {d["id"] for g in conflicting for d in g}
        if not relevant.issubset(referenced):
            return "When citing a detected conflict, cite exact text from both conflicting documents"
    return None


class KnowledgeAgent:
    def __init__(self, tools: KnowledgeTools, planner: Any, mode: str, max_steps: int = 10, max_tool_calls: int = 7):
        if max_steps < 1 or max_tool_calls < 1:
            raise ValueError("Execution budgets must be positive")
        self.tools, self.planner, self.mode = tools, planner, mode
        self.max_steps, self.max_tool_calls = max_steps, max_tool_calls

    def run(self, question: str, history: list[dict] | None = None) -> AgentResult:
        history = history or []
        observations: list[dict] = []
        trace: list[dict] = []
        feedback = ""
        tool_calls = 0
        def finish(status: str, answer: str, citations: list[dict] | None = None, missing: list[str] | None = None) -> AgentResult:
            return AgentResult(status, answer, citations or [], missing or [], trace, tool_calls, len(trace), self.mode)

        for step in range(1, self.max_steps + 1):
            state = {
                "question": question, "history": history, "observations": observations,
                "validation_feedback": feedback,
                "budget": {"step": step, "max_steps": self.max_steps,
                           "tool_calls_used": tool_calls, "tool_calls_allowed": self.max_tool_calls},
            }
            try:
                action = self.planner.next_action(state)
            except (PlannerError, ValueError) as exc:
                trace.append({"step": step, "event": "model_error", "error": str(exc)})
                return finish("model_failure", "模型决策接口失败，无法完成该任务。", missing=[str(exc)])
            if not isinstance(action, dict):
                feedback = "Action must be a JSON object"
                trace.append({"step": step, "event": "invalid_action", "feedback": feedback})
                continue
            kind = action.get("type")
            reason = str(action.get("reason", "未提供简要决策依据"))[:200]
            if kind == "tool":
                name = action.get("name")
                args = action.get("arguments")
                if name not in VALID_TOOLS or not isinstance(args, dict):
                    feedback = "Unknown tool or invalid arguments; choose a listed tool and an object"
                    trace.append({"step": step, "event": "invalid_action", "action": action, "feedback": feedback})
                    continue
                # Allow at most one retry after a tool failure.
                consecutive_failures = 0
                for obs in reversed(observations):
                    if obs["tool"] == name and not obs["ok"]:
                        consecutive_failures += 1
                    else:
                        break

                if consecutive_failures >= 2:
                    feedback = (
                        f"{name} has failed twice consecutively. "
                        "Retry budget exhausted. Do not call this tool again. "
                        "Return final tool_failure if no verified evidence is available."
                    )
                    trace.append({
                        "step": step,
                        "event": "tool_action_rejected",
                        "tool": name,
                        "reason": reason,
                        "feedback": feedback,
                    })
                    continue
                # A budget guardrail, not a hardcoded answer: the LLM still
                # chooses the final status/wording after seeing this feedback.
                empty_searches = sum(
                    1 for obs in observations
                    if obs["tool"] == "search_documents" and obs["ok"] and not obs["result"]["hits"]
                )
                any_hits = any(
                    obs["tool"] == "search_documents" and obs["ok"] and obs["result"]["hits"]
                    for obs in observations
                )
                if name == "search_documents" and empty_searches >= 2 and not any_hits:
                    feedback = ("Two searches produced no relevant documents. "
                                "No further searches allowed; choose final insufficient_evidence "
                                "without citations, or explain missing information.")
                    trace.append({"step": step, "event": "tool_action_rejected", "tool": name,
                                  "reason": reason, "feedback": feedback})
                    continue
                if tool_calls >= self.max_tool_calls:
                    trace.append({"step": step, "event": "tool_call_limit", "limit": self.max_tool_calls})
                    return finish("incomplete_tool_limit", "工具调用次数达到上限，任务尚未完成。", missing=["额外的检索/阅读预算"])
                tool_calls += 1
                record = {"step": step, "event": "tool_call", "tool": name, "arguments": args, "decision_basis": reason}
                if action.get("_api_attempts", 1) > 1:
                    record["model_api_attempts"] = action["_api_attempts"]
                try:
                    result = self.tools.execute(name, args)
                    observation = {"tool": name, "arguments": args, "ok": True, "result": result}
                    record["observation"] = {"ok": True, "result": result}
                except (ToolError, ValueError, TypeError) as exc:
                    observation = {"tool": name, "arguments": args, "ok": False, "error": str(exc)}
                    record["observation"] = {"ok": False, "error": str(exc)}
                observations.append(observation)
                trace.append(record)
                feedback = ""
            elif kind == "final":
                violation = _validate_final(action, observations)
                if violation:
                    feedback = violation
                    trace.append({"step": step, "event": "final_rejected", "reason": reason, "feedback": violation})
                    continue
                record = {"step": step, "event": "final", "status": action["status"], "decision_basis": reason}
                if action.get("_api_attempts", 1) > 1:
                    record["model_api_attempts"] = action["_api_attempts"]
                trace.append(record)
                return finish(action["status"], action["answer"], action["citations"], action.get("missing_info", []))
            else:
                feedback = "Action type must be 'tool' or 'final'"
                trace.append({"step": step, "event": "invalid_action", "feedback": feedback})
        return finish("incomplete_step_limit", "执行步数达到上限，任务尚未完成。", missing=["额外执行步数或人工确认"])
