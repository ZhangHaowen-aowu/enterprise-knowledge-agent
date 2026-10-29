"""True API-driven model planner and a transparently scripted offline mock planner."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request


SYSTEM_PROMPT = """You are a tool-using enterprise knowledge assistant. Your NEXT ACTION must be exactly one JSON object, no markdown.
Actions:
1. {"type":"tool","name":"search_documents","arguments":{"query":"..."},"reason":"brief user-visible reason"}
2. {"type":"tool","name":"read_document","arguments":{"doc_id":"D01"},"reason":"brief user-visible reason"}
3. {"type":"final","status":"answered|needs_clarification|insufficient_evidence|tool_failure","answer":"...", "citations":[{"doc_id":"D01","quote":"exact substring copied from document content"}],"missing_info":[],"reason":"brief user-visible reason"}
You decide one action at a time from previous tool observations; NEVER write a predetermined multi-step plan as if executed. Search first; reading is required to cite evidence; retrieved metadata alone is insufficient. Adapt to failed or empty searches. If the first search is empty, try ONE meaningful query reformulation preserving the specific topic. If two searches have returned no relevant hits, STOP searching and emit final insufficient_evidence with no citations. Do not generate endless synonyms or use broad terms that lose the topic; unrelated retrieved documents are not evidence. If a retrieved and read document is unrelated to the question, return insufficient_evidence rather than pretending it answers. If validation_feedback tells you further searches are blocked, choose a final action. Reserve a step to finalize before tool budget is exhausted. For ambiguous follow-ups, incorporate the relevant conversation context into the search query. When two documents conflict and authority / applicable scope is unknown, READ both, cite exact snippets from both, mark needs_clarification, and ask the user to confirm the applicable policy; do NOT assume the newer version automatically overrides the other. Out-of-knowledge queries must return insufficient_evidence. If a tool call fails, consider retrying that tool once before giving up. If the retry fails again, return tool_failure. A failed tool invocation is different from a successful search with no results. Do not invent citations, source text, facts, precedence rules or tool outcomes. Avoid unsupported claims. Before answering check relevant conflicting search hits, and read candidate source documents. All 'reason' fields are SHORT observable action summaries, not private chain-of-thought. Never expose secrets. JSON only."""


class PlannerError(RuntimeError):
    pass


class APIPlanner:
    """One independent model call per decision, using OpenAI-compatible chat completions."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("OPENAI_MODEL", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if not self.model or not self.api_key:
            raise PlannerError("LLM mode needs OPENAI_MODEL and OPENAI_API_KEY (see .env.example)")

    def next_action(self, state: dict) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
            ],
        }
        if os.getenv("LLM_JSON_MODE", "1") != "0":
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
            method="POST",
        )
        # Limited retries apply only to transient transport/provider failures.
        # These model decision calls have no side-effecting business tools.
        timeout = max(1, int(os.getenv("LLM_TIMEOUT_SECONDS", "90")))
        retries = max(0, min(3, int(os.getenv("LLM_MAX_RETRIES", "1"))))
        delay = max(0, float(os.getenv("LLM_RETRY_DELAY_SECONDS", "1")))
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    result = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                    raise PlannerError(f"LLM HTTP error {exc.code} after {attempt + 1} attempt(s)") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == retries:
                    raise PlannerError(f"LLM connection failure: {type(exc).__name__} after {attempt + 1} attempt(s)") from exc
            except (ValueError, UnicodeDecodeError) as exc:
                raise PlannerError(f"Invalid API response: {type(exc).__name__}") from exc
            if delay:
                time.sleep(delay * (attempt + 1))
        try:
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("Model returned non-text content")
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            action = json.loads(text)
            if not isinstance(action, dict):
                raise ValueError("Action must be a JSON object")
            if attempt > 0:
                action["_api_attempts"] = attempt + 1
            return action
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerError(f"Unparseable model action: {type(exc).__name__}") from exc


class MockPlanner:
    """Rule-scripted OFFLINE TEST DOUBLE, not evidence of LLM decision quality."""

    @staticmethod
    def _query(state: dict) -> str:
        query = state["question"]
        if any(x in query for x in ("它", "那", "还", "这个", "这种", "同意")):
            users = [x.get("content", "") for x in state.get("history", []) if x.get("role") == "user"]
            if users:
                query = users[-1] + " " + query
        return query

    @staticmethod
    def final(status: str, answer: str, cites: list[dict] | None = None, missing: list[str] | None = None, reason: str = "") -> dict:
        return {"type": "final", "status": status, "answer": answer, "citations": cites or [], "missing_info": missing or [], "reason": reason}

    def next_action(self, state: dict) -> dict:
        history = state["observations"]
        query = self._query(state)
        failures = [o for o in history if not o["ok"]]
        if history and not history[-1]["ok"]:
            latest = history[-1]
            if sum(1 for o in failures if o["tool"] == latest["tool"]) >= 2:
                return self.final("tool_failure", "工具连续失败，无法可靠完成回答。", missing=[latest["tool"]], reason="两次工具失败后停止")
            return {"type": "tool", "name": latest["tool"], "arguments": latest["arguments"], "reason": "上一次调用失败，尝试一次恢复"}

        searches = [o for o in history if o["tool"] == "search_documents" and o["ok"]]
        reads = [o["result"] for o in history if o["tool"] == "read_document" and o["ok"]]
        read_ids = {doc["id"] for doc in reads}
        if not searches:
            return {"type": "tool", "name": "search_documents", "arguments": {"query": query}, "reason": "先检索候选制度或产品文档"}
        hits = searches[-1]["result"]["hits"]
        if not hits and len(searches) < 2:
            # A second query is selected AFTER observing a failed search.
            reformulated = re.sub(r"请问|怎么|如何|是否|需要|可以|公司", "", query)
            return {"type": "tool", "name": "search_documents", "arguments": {"query": reformulated}, "reason": "没有候选结果，改写检索查询"}
        if not hits:
            return self.final("insufficient_evidence", "现有知识库未找到支持该问题的资料。", missing=["相关制度或产品原文"], reason="两次检索均无匹配")
        for hit in hits:
            if hit["doc_id"] not in read_ids:
                return {"type": "tool", "name": "read_document", "arguments": {"doc_id": hit["doc_id"]}, "reason": "读取检索到的原文，检查依据或版本冲突"}

        # Only in this explicitly labeled mock do we implement a scripted response policy.
        grouped = {}
        for doc in reads:
            grouped.setdefault((doc["rule_key"], doc["scope"]), []).append(doc)
        for group in grouped.values():
            if len({d["rule_value"] for d in group}) > 1:
                cites = [{"doc_id": doc["id"], "quote": doc["content"]} for doc in group]
                return self.final(
                    "needs_clarification",
                    "发现同一适用范围内相互矛盾的规定：" + "；".join(f'{d["id"]}（{d["version_date"]}）：{d["content"]}' for d in group)
                    + " 请确认目前适用的制度版本或负责部门。",
                    cites, ["适用制度版本及其优先级"], "已读原文互相冲突，不能单方面择一",
                )
        doc = reads[0]
        return self.final("answered", doc["content"], [{"doc_id": doc["id"], "quote": doc["content"]}], reason="答案可由读取的原文直接支持")
