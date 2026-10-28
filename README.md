# Enterprise Knowledge QA Agent

> **答题范围**：小影科技 2027 校招 AI Agent 开发岗位「企业知识问答 Agent」。本仓库使用**合成制度和产品文档**，与任何真实企业内部知识库无关。禁止向仓库提交真实 API Key 或隐私资料。

## 评审入口：四条命令

环境：Python **3.10+**，运行依赖仅标准库。以下均在**项目根目录**执行。

```bash
python -m unittest discover -s tests -v
python -m src.agent.run_cases --mode mock
python -m src.agent.audit --mode mock
python -m src.agent.cli --mode mock --question "国内差旅报销期限是几天？" --print-trace
```

第二条命令把**实际运行生成的** 7 个案例 Trace 写入 `examples/traces/mock/`，并写出 `summary.json`。这个 offline mock 是明确标记的**脚本式替身**，只用于核验工具、控制流、边界检查；**不证明真实模型会作出正确的动态决策**。

## 真实 LLM 自主决策（重要）

1. 在本地复制 `.env.example` 为 `.env`（Windows PowerShell：`Copy-Item .env.example .env`；macOS/Linux：`cp .env.example .env`；`.env` 已在 `.gitignore` 中忽略），写入你自己有权限使用的 OpenAI-compatible API 地址、密钥、模型名。
2. 运行：

```bash
python -m src.agent.cli --mode llm --question "国内差旅报销期限是几天？" --print-trace --trace-out examples/traces/live/conflict.json
python -m src.agent.run_cases --mode llm
```

已知真实 API 批量初测（2026-10-28，使用者提供日志）为 5/7 符合预期：冲突用例遇到 `TimeoutError`，资料外用例出现重复检索导致 `incomplete_tool_limit`。本版本新增瞬时 API 故障有限重试、两次无结果检索后的防重复护栏，需要重新运行模型才能确认改进效果。可以只复测这两项：

```bash
python -m src.agent.run_cases --mode llm --cases examples/cases-retest.json --output examples/traces/retest
```

如需配置超时、重试：在本地 `.env` 中可加入 `LLM_TIMEOUT_SECONDS=90`、`LLM_MAX_RETRIES=1`（最多请求两次）、`LLM_RETRY_DELAY_SECONDS=1`。这些选项有默认值，无须修改已有 API Key。**不要提交 `.env`。**

`--mode llm` **每一个 Agent 步骤调用一次真实模型**，模型基于可见的检索结果自主返回下一步 JSON 动作（`search_documents` / `read_document` / `final`）。没有调用真实 API 就不应声称已验证大模型 ReAct。模型若返回非法决策、无效引用或直接绕过冲突，会被校验拒绝，下一轮看到校验反馈；如果超出工具/步骤预算则返回明确未完成状态。实际模型效果取决于所用模型、供应商的兼容性与运行结果。

有些兼容服务不支持 `response_format: {"type":"json_object"}`：可以在 `.env` 中设 `LLM_JSON_MODE=0`，但仍必须返回可解析的 JSON。工具均为本地函数，**不依赖在线向量数据库**；无须前端、部署或 Multi-Agent。

## 自定义问题及工具故障

```bash
# 单个问题 + 日志文件
python -m src.agent.cli --mode mock --question "员工口令忘了，怎么找回账号？" --trace-out examples/custom.json

# 搜索工具前一次报错；决策器观察到错误后选择是否重试
python -m src.agent.cli --mode mock --question "忘记登录密码怎么办？" --fail-tool search_documents:1 --print-trace

# 测试“追问”上下文
python -m src.agent.cli --mode mock --question "那还需要主管同意吗？" --history examples/history_leave.json
```

## 实现内容与决策示意

```text
用户问题 / 可选历史
        |
    模型决策器 ──► search_documents(query) ──► 候选文档 ID / 版本 / 范围
        ▲                                        |
        └────── 观察结果 ◄────────────────────────┘
        |
    模型决策器 ──► read_document(doc_id) ───────► 原始全文
        ▲                                        |
        └────── 观察结果 ◄────────────────────────┘
        |
    模型决策器 ──► 再检索 / 读取其他版本 / 最终回答 / 追问 / 停止
        |
    引用、冲突、步数与工具预算校验
        |
     结果 + Trace
```

每次工具调用记录：步骤、动作、参数、工具返回（或故障）和简短可对外解释的决策依据。不会要求或存储模型的隐藏思维链。

- **检索**：轻量中文 n-gram 与同义词词表；检索只返回候选元数据，不构成事实依据。
- **读取**：必须调用 `read_document` 得到原文后才能引用；逐字校验引用文本是否真的出现在该文档中。
- **冲突保护**：如果多个同主题/适用范围的文档表达矛盾的规定，则拒绝给出未经澄清的确定答案；不会假设新版自动覆盖旧版。
- **拒答**：资料外问题可返回 `insufficient_evidence`，可注明资料缺口。
- **工具失败**：失败作为观察返回给模型，由模型决定是否重试、换查或停止。
- **预算**：默认最多 10 次决策、7 次工具调用，超限明确标记 `incomplete_*`；两次无结果检索后拒绝第三次相同性质的检索，并请模型选择终止动作。

**注意**：完全精确引用不能自动保证完整语义蕴含，`rule_key` / `rule_value` 的冲突检测依赖合成文档标注；这是 demo 工程护栏，并非企业级事实验证算法。更多限制见 `docs/03-evaluation.md`。

## GitHub 交付清单

```text
README.md
src/agent/             # CLI、模型接口、工具和决策循环
tests/                 # 可运行单元 / 验收测试
examples/cases.json    # 输入与预期状态
examples/traces/mock/  # 实际离线运行结果和步骤记录
examples/traces/llm/   # 批量真实 API 运行后生成（未提供密钥则无此目录）
examples/cases-retest.json  # 仅复测两个初轮失败案例
data/knowledge_base.json
 docs/01-requirements.md
 docs/02-solution.md
 docs/03-evaluation.md
 docs/04-ai-usage.md
 docs/05-retrospective.md
.env.example
requirements.txt
```

最终提交需在考试页面填写**题号 3、GitHub URL、最终 commit SHA**。本地 ZIP 不等于线上已提交；上传 GitHub 后使用 `git rev-parse HEAD` 获取 SHA，并核验远端仓库内容。
