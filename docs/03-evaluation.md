# 03 效果验证：预期、实际、失败和限制

## 运行方式

在项目根目录：

```bash
python -m unittest discover -s tests -v
python -m src.agent.run_cases --mode mock
```

下表记录 **2026-10-28 在离线 Mock 模式下真实执行** 的结果。各 JSON 由程序生成，存于 `examples/traces/mock/`。此处的“通过”只验证脚本决策器及基础设施，**不证明任何大模型的 ReAct 决策能力**。

| 用例 | 预期状态 | Mock 实际状态 | 结果文件 |
| --- | --- | --- | --- |
| 正常答案 | `answered` | `answered` | `01-clear.json` |
| 文档冲突 | `needs_clarification` | `needs_clarification` | `02-conflict.json` |
| 资料外问题 | `insufficient_evidence` | `insufficient_evidence` | `03-out-of-knowledge.json` |
| 近义改写 | `answered` | `answered` | `04-paraphrase.json` |
| 上下文追问 | `answered` | `answered` | `05-followup.json` |
| 一次检索故障 | `answered` | `answered` | `06-transient-tool-failure.json` |
| 两次检索故障 | `tool_failure` | `tool_failure` | `07-persistent-tool-failure.json` |

合计 **7/7** Mock 测试场景达预期；原始版本 **16/16** 单元及验收测试通过；本次修复后 **19/19**（额外增加 API 重试与连续空检索的回归测试）（详见 `tests/test_agent.py`）。还包含修改 D04 原文后再次运行，答案确实随资料变化；以及伪造引用、未读引用、未处理冲突、预先结束、预算超限等防护用例。

## 两条实测离线路径举例

- 正常 D04：`search_documents` → `read_document(D04)` → `final(answered)`。
- 冲突 D01/D02：`search_documents` → `read_document(D01)` → `read_document(D02)` → `final(needs_clarification)`。
- 工具故障注入：首次 `search_documents` 失败 → 再次 `search_documents` 成功 → `read_document(D04)` → `final(answered)`。

逐步动作、参数、工具结果及 `decision_basis` 见相应 JSON。路径确实由**模拟决策器**根据不同工具返回生成，不能把模拟路径冒充真实模型路径。

## 真实模型评估：初轮与针对性复测（2026-10-28）

模型提供方：SiliconFlow International
模型：Qwen/Qwen3.5-35B-A3B
调用方式：OpenAI-compatible Chat Completions API

### 初轮批量运行

命令：`python -m src.agent.run_cases --mode llm`

| 用例 | 预期状态 | 初轮实际状态 | 结果 |
| --- | --- | --- | --- |
| `01-clear` | `answered` | `answered` | 通过 |
| `02-conflict` | `needs_clarification` | `model_failure` | 未通过：首次模型调用 `TimeoutError`，工具调用 0 次 |
| `03-out-of-knowledge` | `insufficient_evidence` | `incomplete_tool_limit` | 未通过：检索过度，工具调用 7 次后被预算终止 |
| `04-paraphrase` | `answered` | `answered` | 通过 |
| `05-followup` | `answered` | `answered` | 通过 |
| `06-transient-tool-failure` | `answered` | `answered` | 通过 |
| `07-persistent-tool-failure` | `tool_failure` | `tool_failure` | 通过 |

初轮合计 **5/7 与预期状态一致**。原始执行证据在 `examples/traces/llm/`；首次失败记录必须保留，不应被改写为成功。

### V2 修复与针对性复测

修复方案包含：API 超时和短暂 HTTP 故障的有限重试，以及两次连续空检索后的预算护栏（禁止继续无效检索，并要求模型选择合适的最终状态）。护栏不会直接把执行状态改成成功；模型仍需返回合法 `final` 动作。它确实限制了模型后续工具动作，因此不能表述为“纯粹无约束地自主停止”。

复测命令：

```bash
python -m src.agent.run_cases --mode llm --cases examples/cases-retest.json --output examples/traces/retest
```

| 用例 | 复测结果 | 实际工具和决策轨迹 | 证据文件 |
| --- | --- | --- | --- |
| `02-conflict` | `needs_clarification`（通过） | `search_documents` → `read_document(D01)` → `read_document(D02)` → `final`；共 3 次工具调用、4 步；输出同时列出 30 天和 15 天并要求用户确认 | `examples/traces/retest/02-conflict.json` |
| `03-out-of-knowledge` | `insufficient_evidence`（通过） | 两次 `search_documents` 无结果 → `final`；共 2 次工具调用、3 步；没有编造火星殖民旅行补贴政策 | `examples/traces/retest/03-out-of-knowledge.json` |

**V2 针对性复测 2/2 通过**。合并初轮已通过的其他 5 个用例，七种场景均有至少一次符合预期的真实模型执行记录。

### 证据使用及结果解释

- `examples/traces/llm/`：初轮真实模型运行记录，其中包含两个失败用例，供缺陷诊断与复盘。
- `examples/traces/retest/`：V2 真实模型定向复测记录，保留初轮结果而不覆盖。
- `examples/traces/live/conflict.json`：单次真实 LLM 文档冲突问答记录，作为独立运行证据。
- `examples/traces/mock/`：模拟规划器结果，仅证明工程流程，不证明真实模型决策质量。
- Trace 中 `decision_basis` 是简短的外部动作理由，不是模型内部思维链。

验收覆盖：明确答案、文档冲突、资料外问题、相似改写、上下文追问、工具失败；同时包含持续失败以及动态调整的执行路径。进一步的可选验证包括 V2 全量重新运行、不同查询改写的鲁棒性、针对冲突制度的人工审查和模型温度/随机种子配置记录。


## 文档替换的真实 LLM 对照实验（2026-10-28）

为直接验证题目中的“替换文档内容，核对答案与动作是否改变”，提交者基于原始知识库生成只供对照的 `examples/kb-no-conflict.json`，把 D02 的 `rule_value` 从 `15天` 改为 `30天`，并把对应 `content` 改为“员工国内差旅费用应在费用发生后30天内提交报销申请。”；D01 保持 30 天，原始 `data/knowledge_base.json` 不变。

运行命令（真实 API）：

```bash
python -m src.agent.cli --mode llm --data examples/kb-no-conflict.json --question "国内差旅报销期限是几天？" --print-trace --trace-out examples/traces/live/no-conflict.json
```

| 指标 | 原始冲突知识库 | 消除冲突的知识库 |
| --- | --- | --- |
| D01 / D02 | 30 天 / 15 天 | 30 天 / 30 天 |
| 最终状态 | `needs_clarification` | `answered` |
| 输出 | 列出矛盾并请用户确认适用版本 | 回答“国内差旅报销期限是30天” |
| 工具路径 | 搜索 → 读 D01 → 读 D02 → 结束 | 搜索 → 读 D01 → 读 D02 → 结束 |
| 工具调用 / 总步骤 | 3 / 4 | 3 / 4 |
| 证据 | `examples/traces/retest/02-conflict.json` | `examples/traces/live/no-conflict.json` |

**解释**：模型真实读取两个版本，并在证据变化后改变最终决定（请求澄清 → 明确回答）；两次使用相同工具顺序，**并未证明检索/读取工具路径发生变化**。这个可复现对照直接证明最终答案和决策状态对知识库内容敏感。修改后的知识库是人为构建的合成样例，未涵盖真实生效日期或权限管理。

## V3 真实 LLM 完整验收（2026-10-29）

针对 V2 全量复测中临时工具故障提前终止的问题，
V3 调整了工具异常信息、模型恢复指令，并增加
连续工具失败后的重试预算校验。

完整执行 7 个真实 LLM 验收场景，7/7 达到预期状态。

- 正常回答、相似表达及上下文追问：正确回答并引用原文。
- 文档冲突：读取 D01/D02，发现冲突并请求澄清。
- 知识库外问题：两次无结果检索后拒答。
- 临时工具故障：模型主动重试并恢复。
- 持续工具故障：两次失败后执行器阻止继续重试，
  模型返回 tool_failure。

完整记录见 examples/traces/llm-v3/。

上述 7/7 表示本次单轮验收的状态一致性，
不代表在任意查询和随机运行下均能保证成功。

## 已知限制与风险

1. **事实蕴含不完全校验**：精确引文属于形式校验，仍有“引用真、答案误读”的风险。生产系统应增加 entailment/reranker 或人工抽检。
2. **冲突检测依赖标注**：只有 `rule_key` 和适用范围一致、`rule_value` 冲突时才可确定冲突；不同范围、隐含例外、时间生效关系未建模。
3. **检索能力较弱**：字符 n-gram/同义词适合小型可复现实验，未覆盖复杂多跳、跨语言、语义召回。
4. **回答模式**：本实例不进行复杂问答生成质量评估；模拟决策器常直接输出文档原句。
5. **网络兼容性**：真实 LLM 可能出现不合法 JSON、限流、超时、接口不兼容或模型选择错误，此时返回明确失败状态，但不能保证所有供应商行为一致。
6. **安全/权限**：没有身份权限隔离、隐私分级、恶意 Prompt Injection 的全面防御，不应连接生产知识库。

## 下一步

后续将重点进行 V2 版本的完整批量复测与多轮稳定性评估，并进一步改进检索召回质量、答案语义支持校验和文档版本管理。
