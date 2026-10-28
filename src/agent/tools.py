"""Deterministic tools: search metadata, then read exact original document text."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "knowledge_base.json"

# Lightweight lexical expansion, not a learned embedding or production search engine.
SYNONYM_GROUPS = (
    ("密码", "口令", "凭据"),
    ("忘记", "找回", "重置", "恢复"),
    ("账户", "账号", "登录"),
    ("请假", "休假", "假期"),
    ("报销", "差旅", "费用"),
    ("主管", "审批", "批准", "同意"),
    ("字幕", "文字", "配字"),
    ("技术支持", "IT", "工单"),
)
STOP = {"怎么", "如何", "什么", "多少", "公司", "请问", "请", "时候", "需要", "可以", "是否", "有没有", "员工", "还是", "以及", "现在", "一下", "它们", "这个", "那个"}


def query_terms(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text).lower()
    words = set(re.findall(r"[a-z][a-z0-9]*", text))
    chinese_parts = re.findall(r"[\u4e00-\u9fff]+", text)
    for part in chinese_parts:
        words.update(part[i : i + 2] for i in range(len(part) - 1))
        words.update(part[i : i + 3] for i in range(len(part) - 2))
    for group in SYNONYM_GROUPS:
        if any(term.lower() in text for term in group):
            words.update(t.lower() for t in group)
    return {w for w in words if w not in STOP}


class ToolError(RuntimeError):
    pass


class KnowledgeTools:
    def __init__(self, data_path: str | Path = DEFAULT_DATA, fail_tools: dict[str, int] | None = None):
        raw = json.loads(Path(data_path).read_text(encoding="utf-8"))
        self.docs = {doc["id"]: doc for doc in raw["documents"]}
        if len(self.docs) != len(raw["documents"]):
            raise ValueError("Duplicate document IDs")
        self.fail_tools = dict(fail_tools or {})

    def _maybe_fail(self, tool: str) -> None:
        if self.fail_tools.get(tool, 0) > 0:
            self.fail_tools[tool] -= 1
            raise ToolError(f"Injected {tool} outage (remaining failures: {self.fail_tools[tool]})")

    def search_documents(self, query: str, top_k: int = 4) -> dict:
        self._maybe_fail("search_documents")
        if not query.strip():
            return {"hits": [], "query": query, "message": "Empty query"}
        tokens = query_terms(query)
        scored = []
        for doc in self.docs.values():
            title_terms = query_terms(doc["title"])
            tag_terms = query_terms(" ".join(doc["tags"]))
            body_terms = query_terms(doc["content"])
            score = 4 * len(tokens & title_terms) + 3 * len(tokens & tag_terms) + len(tokens & body_terms)
            # With distinct lexical tokens, a score >= 5 needs substantive overlap.
            if score >= 5:
                scored.append((score, doc))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        if not scored:
            return {"hits": [], "query": query, "message": "No relevant documents found"}
        strongest = scored[0][0]
        # Avoid reading loosely related documents solely due to common vocabulary.
        chosen = [(score, doc) for score, doc in scored if score >= max(5, strongest * 0.6)][:top_k]
        return {
            "query": query,
            "hits": [
                {"doc_id": d["id"], "title": d["title"], "version_date": d["version_date"],
                 "scope": d["scope"], "rule_key": d["rule_key"], "score": score}
                for score, d in chosen
            ],
            "message": f"Found {len(chosen)} candidates. Search hits are not evidence until read_document is called."
        }

    def read_document(self, doc_id: str) -> dict:
        self._maybe_fail("read_document")
        if doc_id not in self.docs:
            raise ToolError(f"Unknown document ID: {doc_id}")
        return copy.deepcopy(self.docs[doc_id])

    def execute(self, name: str, args: dict) -> dict:
        if name == "search_documents":
            query = args.get("query")
            if not isinstance(query, str):
                raise ToolError("search_documents requires query:string")
            return self.search_documents(query)
        if name == "read_document":
            doc_id = args.get("doc_id")
            if not isinstance(doc_id, str):
                raise ToolError("read_document requires doc_id:string")
            return self.read_document(doc_id)
        raise ToolError(f"Unknown tool: {name}")
