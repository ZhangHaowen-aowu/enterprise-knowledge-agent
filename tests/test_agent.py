"""Offline regression tests; no external LLM API required."""
from __future__ import annotations

import io
import json
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

from src.agent.cli import ROOT, build_agent
from src.agent.engine import KnowledgeAgent, _validate_final
from src.agent.planners import APIPlanner, MockPlanner
from src.agent.tools import DEFAULT_DATA, KnowledgeTools


class AgentAcceptanceTests(unittest.TestCase):
    def run_case(self, question, *, history=None, fail_tools=None):
        return build_agent("mock", fail_tools=fail_tools).run(question, history)

    def test_clear_answer_uses_exact_quote_and_read_tool(self):
        result = self.run_case("员工忘记登录密码时应该怎么办？")
        self.assertEqual(result.status, "answered")
        self.assertIn("D04", [c["doc_id"] for c in result.citations])
        self.assertTrue(any(t.get("tool") == "read_document" for t in result.trace))

    def test_conflict_cites_both_and_never_picks_newest_automatically(self):
        result = self.run_case("国内差旅报销期限是几天？")
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual({c["doc_id"] for c in result.citations}, {"D01", "D02"})
        self.assertIn("30天", result.answer)
        self.assertIn("15天", result.answer)

    def test_out_of_knowledge_refuses_to_guess(self):
        result = self.run_case("公司火星殖民旅行的补贴标准是多少？")
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertEqual(result.citations, [])

    def test_paraphrased_question(self):
        result = self.run_case("员工口令忘了，怎么找回账号？")
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.citations[0]["doc_id"], "D04")

    def test_context_followup(self):
        history = [{"role": "user", "content": "公司请假应该怎么申请？"},
                   {"role": "assistant", "content": "请假需通过系统提交申请。"}]
        result = self.run_case("那还需要主管同意吗？", history=history)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.citations[0]["doc_id"], "D03")

    def test_transient_tool_failure_recovery_is_logged(self):
        result = self.run_case("忘记登录密码怎么办？", fail_tools={"search_documents": 1})
        self.assertEqual(result.status, "answered")
        self.assertTrue(any(e.get("observation", {}).get("ok") is False for e in result.trace))
        self.assertEqual(sum(e.get("tool") == "search_documents" for e in result.trace), 2)

    def test_persistent_tool_failure_is_not_success(self):
        result = self.run_case("忘记登录密码怎么办？", fail_tools={"search_documents": 2})
        self.assertEqual(result.status, "tool_failure")
        self.assertNotEqual(result.status, "answered")

    def test_document_edit_changes_answer_not_a_hardcoded_decision_log(self):
        data = json.loads(DEFAULT_DATA.read_text(encoding="utf-8"))
        for doc in data["documents"]:
            if doc["id"] == "D04":
                doc["content"] = "忘记登录密码时，应联系人工客服完成身份核验，不再使用自助重置功能。"
                doc["rule_value"] = "人工客服"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "docs.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = build_agent("mock", data=path).run("员工忘记登录密码时应该怎么办？")
        self.assertEqual(result.status, "answered")
        self.assertIn("人工客服", result.answer)
        self.assertNotIn("内部身份平台", result.answer)


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.tools = KnowledgeTools()
        self.d1 = self.tools.read_document("D01")
        self.d2 = self.tools.read_document("D02")
        self.observations = [
            {"tool": "read_document", "arguments": {"doc_id": "D01"}, "ok": True, "result": self.d1},
            {"tool": "read_document", "arguments": {"doc_id": "D02"}, "ok": True, "result": self.d2},
        ]

    def test_fake_citation_rejected(self):
        proposed = {"status": "answered", "answer": "30天", "citations": [{"doc_id": "D01", "quote": "100天"}]}
        self.assertIn("exact substring", _validate_final(proposed, self.observations))

    def test_citation_without_read_rejected(self):
        proposed = {"status": "answered", "answer": "30天", "citations": [{"doc_id": "D01", "quote": self.d1["content"]}]}
        self.assertIn("not read", _validate_final(proposed, []))

    def test_conflicting_evidence_blocks_answer(self):
        proposed = {"status": "answered", "answer": "30天", "citations": [{"doc_id": "D01", "quote": self.d1["content"]}]}
        self.assertIn("Contradictory", _validate_final(proposed, self.observations))

    def test_conflict_request_requires_both_citations(self):
        proposed = {"status": "needs_clarification", "answer": "有冲突", "citations": [{"doc_id": "D01", "quote": self.d1["content"]}]}
        self.assertIn("both", _validate_final(proposed, self.observations))

    def test_retrieved_unread_version_blocks_premature_answer(self):
        observation = {"tool": "search_documents", "arguments": {"query": "差旅报销"}, "ok": True,
                       "result": {"hits": [{"doc_id": "D01", "rule_key": "domestic_travel_expense_deadline"},
                                           {"doc_id": "D02", "rule_key": "domestic_travel_expense_deadline"}]}}
        read = self.observations[0]
        proposed = {"status": "answered", "answer": "30天", "citations": [{"doc_id": "D01", "quote": self.d1["content"]}]}
        self.assertIn("still need reading", _validate_final(proposed, [observation, read]))

    def test_step_limit_explicitly_incomplete(self):
        result = build_agent("mock", max_steps=1).run("员工忘记登录密码时怎么办？")
        self.assertEqual(result.status, "incomplete_step_limit")

    def test_tool_limit_explicitly_incomplete(self):
        result = build_agent("mock", max_tool_calls=1).run("员工忘记登录密码时怎么办？")
        self.assertEqual(result.status, "incomplete_tool_limit")

    def test_search_requires_real_input(self):
        self.assertEqual(self.tools.search_documents("火星移民专用矿石")['hits'], [])


class LiveIntegrationRegressionTests(unittest.TestCase):
    def test_rejects_third_empty_search_but_lets_model_choose_stop(self):
        class UnhelpfulPlanner:
            def next_action(self, state):
                if state["validation_feedback"]:
                    return {"type": "final", "status": "insufficient_evidence",
                            "answer": "检索两次后仍无支持证据。", "citations": [], "missing_info": ["相关制度"],
                            "reason": "观察空检索和预算护栏后决定停止"}
                return {"type": "tool", "name": "search_documents",
                        "arguments": {"query": "火星殖民旅行补贴"}, "reason": "检索文档"}
        result = KnowledgeAgent(KnowledgeTools(), UnhelpfulPlanner(), "test").run("火星殖民旅行补贴是多少")
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertEqual(result.tool_calls, 2)
        self.assertTrue(any(e["event"] == "tool_action_rejected" for e in result.trace))

    def test_api_timeout_retries_once_then_succeeds(self):
        reply = {"choices": [{"message": {"content": json.dumps({
            "type": "final", "status": "insufficient_evidence", "answer": "无资料",
            "citations": [], "missing_info": [], "reason": "未检索到资料"})}}]}
        with patch.dict("os.environ", {"LLM_MAX_RETRIES": "1", "LLM_RETRY_DELAY_SECONDS": "0"}):
            with patch("src.agent.planners.urllib.request.urlopen", side_effect=[
                TimeoutError("simulated timeout"), io.BytesIO(json.dumps(reply).encode("utf-8"))
            ]) as http:
                result = APIPlanner(model="test", api_key="test").next_action({"question": "test"})
        self.assertEqual(http.call_count, 2)
        self.assertEqual(result["_api_attempts"], 2)
        self.assertEqual(result["type"], "final")

    def test_api_timeout_after_retry_reports_failure(self):
        from src.agent.planners import PlannerError
        with patch.dict("os.environ", {"LLM_MAX_RETRIES": "1", "LLM_RETRY_DELAY_SECONDS": "0"}):
            with patch("src.agent.planners.urllib.request.urlopen", side_effect=TimeoutError("simulated")):
                with self.assertRaisesRegex(PlannerError, "after 2 attempt"):
                    APIPlanner(model="test", api_key="test").next_action({"question": "test"})


if __name__ == "__main__":
    unittest.main()
