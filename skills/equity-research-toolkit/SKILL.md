---
name: equity-research-toolkit
description: Run agent-friendly equity research tools built on the local FinRobot core: financial data, forecasts, peers, news, technical indicators, DCF and EV/EBITDA, sensitivity, catalysts, charts, and HTML/PDF reports. Prefer offline supplied inputs; require explicit network/model flags for external calls.
---

# 个股研究工具

这是基于 FinRobot 开源核心的派生研究适配器，并非官方 FinRobot 产品。入口是本 Skill 内的 `scripts/equity.py`，无需安装本仓库为 Python 包。使用 Python 3.11 或以上；运行位置不受当前工作目录限制。

先运行 `describe` 获取全部 71 个工具、必填参数及输入类型，再运行 `doctor` 检查依赖与冻结源码哈希。`doctor` 只读包元数据，不导入第三方模块，不读用户配置，不联网。缺少依赖时遵照它返回的安装命令在项目虚拟环境内安装 `requirements.lock.txt`；不要自动全局安装。

```bash
python3.11 /path/to/skill/scripts/equity.py describe
python3.11 /path/to/skill/scripts/equity.py doctor
python3.11 /path/to/skill/scripts/equity.py demo --output /path/to/new-demo --pdf
python3.11 /path/to/skill/scripts/equity.py run valuation.combined \
  --args-file /path/to/skill/examples/valuation.json --output /path/to/new-valuation
```

普通命令的 stdout 始终是一个 JSON 对象，包含 `schema_version/ok/command/evidence_level/data/error`；`--help` 为人类可读文本。`ok=true` 只表示该命令完成，不能升级为市场数据认证、实盘验证或投资收益证明。退出码 0 为完成，2 为参数、授权能力开关、输入或完整性失败，3 为依赖、模型或运行失败。先检查退出码和 `ok`，再解释 `evidence_level`。

`run TOOL --args-file FILE --output NEW_DIR` 调用注册工具。输入 JSON 只能提供 registry 中的参数，不可传入脚本、函数名、任意模块或嵌入的命令行选项。表格使用 `{"$csv":"local.csv"}` 或 `{"$table":[{"column":"value"}]}`；大块 JSON 可以使用 `{"$json":"local.json"}`。所有文件路径均相对调用者 cwd；输出必须是一个不存在的新目录。解析与运行先在临时目录完成，再独占创建输出目录并逐文件发布；遇到并发写入不会覆盖旧数据。

外部数据工具必须显式提供 `--allow-network`。模型工具及任何启用模型的 pipeline 同时要求 `--allow-network --allow-model`。凭证只能从调用者明确指定的 `--config-file` 读取；不读取原项目配置或自动发现环境变量中的密钥。先用 `config-template --output NEW_FILE` 创建占位模板，再由使用者私下配置。不要把真实配置、API 密钥、来源受限的数据和私人报告加入分享包。

文字能力包括 `text.section` 的八类统一章节、`text.enhanced.*` 的五类模型说明与三类本地摘要/风险排版/来源格式化，以及 `text.agent-section` 的原 EquityResearchAgentManager 八类结构化 Agent。增强模型必须正常完成且无 refusal，截断、过滤、空响应均失败。AgentManager 只接收明确提供的财务表/新闻，使用显式 SDK 模型与私有凭证，在导入 SDK 前关闭 tracing，并禁用工具、handoff 与 MCP；结构化输出缺字段或为空时失败。来源标签和模型引用仍需独立核实，原 Agent 名称不代表执行了联网查证。

完整研报优先使用 `pipeline.full`。它要求 5 张完整财务数据表、显式预测配置、同单位估值输入，以及 8 类调用者提供的文本或显式启用模型生成。预测年份必须连续且严格晚于全部实际年份。空上游数据、空模型响应、模型 fallback、缺少关键图表或报告都会使命令失败，输出目录不被当作完成结果发布。离线示例只含虚构数值和明确标注的合成文本。

`source.financial-analysis/source.html-report/source.pdf-report` 保留原始三个主流程的参数接口，并增加输出、配置和失败检查。原财务主流程的预测年份固定为 2025E–2027E；当实际年份与其冲突时拒绝运行，改用 `pipeline.full` 的显式年份合同。报表渲染不验证调用者提供的文字是否属实；`rendered_only` 不能当成全文研究验收。

估值沿用上游简化模型：净债务按企业价值 10% 假设；置信权重和区间不是校准概率。包装器强制显式 EBITDA、FCF、股数、目标倍数和 DCF 参数，避免缺失值默默变成默认估值。催化剂与新闻分类是规则启发式结果。保留这些方法限制，并将声明、计算和未知分开。

详细合同见 [references/agent-contract.md](references/agent-contract.md)，机器注册表见 [references/tool-registry.json](references/tool-registry.json)。冻结核心、上游许可、逐文件源哈希与本地修改记录见 `runtime/` 和 `SOURCE_PROVENANCE.json`。本工具不连接券商，不提供下单入口。
