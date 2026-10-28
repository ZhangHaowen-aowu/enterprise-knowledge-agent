# 02 系统设计、ReAct 动态决策和取舍

## 核心动作循环

`src/agent/engine.py` 运行一个有预算的状态机，但**不预置动作流程**。

1. 将问题、对话历史、真实工具观察（含错误）与执行预算传入决策器。
2. 决策器只返回**一个**动作：检索、读取或最终状态；真实模式下每一步都由 LLM API 生成这一动作。
3. 若选择工具，运行本地函数，把工具名称、参数、真实返回/错误写入观察，再回到第 1 步。
4. 若选择结束，验证状态和逐字引文。引用不存在、未读文档、疑似同主题版本未读或已读资料冲突时拒绝直接作答，并将简短反馈传回模型。
5. 超出最大决策步数或工具调用次数时返回 `incomplete_step_limit` / `incomplete_tool_limit`。

**为什么不是固定 if-else？** 真实模式中 Python 只负责执行模型指定的动作和校验边界；先读 D01 还是 D02、是否重检索、工具故障后是否恢复/停止，以及如何总结冲突均通过**模型本次动作**驱动。框架层的确定性安全规则仅用于防止错误声明，不替代模型选择。离线 `MockPlanner` 则是为回归测试故意编写的脚本决策器，不得当作 LLM 成绩。

## 模型输入/输出合同

模型读取 JSON state：`question`, `history`, `observations`, `validation_feedback`, `budget`。模型只需输出下列之一：

```json
{"type":"tool", "name":"search_documents", "arguments":{"query":"差旅报销期限"}, "reason":"先定位可能的制度文档"}
```

```json
{"type":"tool", "name":"read_document", "arguments":{"doc_id":"D01"}, "reason":"读取原文核对规定"}
```

```json
{"type":"final", "status":"needs_clarification", "answer":"制度存在冲突，请明确适用版本。", "citations":[{"doc_id":"D01", "quote":"员工国内差旅费用应在费用发生后30天内提交报销申请。"},{"doc_id":"D02", "quote":"员工国内差旅费用应在费用发生后15天内提交报销申请。"}], "missing_info":["适用版本或负责人确认"], "reason":"两份原文的期限相互矛盾"}
```

`reason` 是简短的**可观察操作说明**，不是对模型隐含推理链的要求。只有读过的原文可作引文。`OPENAI_BASE_URL` 可以连接兼容的供应商；`temperature=0` 用于降低不必要随机性，**不意味着结果必然可复现**。

## 冲突示例：为什么不能只取最近日期

D01（2026-01-10）规定国内差旅报销期限为 30 天；D02（2026-06-05）规定 15 天。两个文档给出同一业务范围但未明确优先级或废止关系。新的日期不自动证明旧政策失效。模型应读两份原文，向用户展示冲突并请求确认；即使模型试图直接回答“15 天”，安全校验也阻止已知证据冲突下的无条件确定答复。

## 关键工程取舍

| 决策 | 收益 | 限制 |
| --- | --- | --- |
| Python 标准库 CLI | 免安装复杂依赖、快速评审 | 还不具备 Web/权限系统 |
| JSON 决策而非框架特定 Tool Calling | 更换兼容 API 方便，Trace 可读 | 部分模型 JSON 不稳定，需校验/容错 |
| 中文 n-gram + 同义词检索 | 无需下载模型，支持改写对照 | 不能替代 embedding、reranker 等语义检索 |
| 读取全文后才能引用 | 排除只靠搜索摘要的假引用 | 逐字匹配不能证明语义蕴含 |
| 人工标注 rule_key/value | 可重现发现制度冲突 | 标注覆盖和冲突检测有限 |
| 工具故障注入与预算控制 | 复现异常路径和停止条件 | 未实现分布式重试、回退缓存 |

## 模型与工具可用性

- 真正模型模式：`--mode llm`；`src/agent/planners.py:APIPlanner` 发出独立 Chat Completions 请求。
- 离线流程模式：`--mode mock`；`MockPlanner` 根据上一条观察选择动作，仅供测试代码行为。
- 工具调用：`KnowledgeTools.search_documents/read_document`，都由本地合成资料提供。故障注入遵循 `--fail-tool search_documents:1` 等。
- 没有使用 LangGraph、ADK、AgentScope；不存在需要声明的框架版本。只用 Python 3.10+ 标准库。
