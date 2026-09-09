# 调用合同

入口：`python3.11 /absolute/skill/scripts/equity.py [--timeout SECONDS] COMMAND`。默认总超时 300 秒；超时参数放在命令前。路径含空格需要 shell 引号。所有业务调用通过 `run TOOL --args-file JSON --output NEW_DIR`，工具名和完整参数清单以 `describe` 或 [tool-registry.json](tool-registry.json) 为准。

| 工具组 | 作用 | 默认外部能力 |
|---|---|---|
| `data.*` | FMP Stable 财报、企业价值、报价、分析师信息、同业、新闻和技术指标；Yahoo Finance 成交量 | 必须 `--allow-network` |
| `finance.*` | 历史财务整理、增长与预测、同业 EBITDA 预测 | 离线 |
| `valuation.*` | EV/EBITDA、DCF、同业及组合估值 | 离线 |
| `sensitivity.analyze` | 收入、利润率、组合敏感性 | 离线 |
| `catalyst.analyze`, `news.process` | 已提供新闻的规则分类 | 离线 |
| `news.enhanced`, `sentiment.snapshot` | FMP 新闻整合、可选 Adanos 活跃度 | 必须 `--allow-network` |
| `chart.*` | 基础图表及增强图表 | 离线 |
| `text.section` | 八类报告文字生成，强制 strict | 必须同时 `--allow-network --allow-model` |
| `text.enhanced.*` | 执行摘要、预测方法论、催化剂说明、估值说明、投资建议；章节摘要、风险排版、来源格式化 | 前五项要求双开关；后三项离线且不初始化模型客户端 |
| `text.agent-section` | 原 EquityResearchAgentManager 的八种 typed Agent 章节 | 必须双开关、显式 key/model；禁 tracing/工具/handoff/MCP |
| `report.html`, `report.pdf` | 调用者数据的原模板渲染 | 离线 |
| `pipeline.full` | 财务→预测→估值→敏感性/催化剂→图表→文字→HTML/PDF | 本地输入离线；缺财务数据/启用文字模型时需要明确开关 |
| `source.*` | 原三个主程序的受控调用 | 原 financial-analysis 要联网；文字生成另需模型开关 |

运行数据只写入明确的新目录；不会重用已有目录。完整运行成功后输出 `result.json` 及工具生成的文件，stdout 返回每个文件的相对路径、字节数与 SHA-256。临时文件和图表缓存在临时目录；失败时不留下宣称完成的正式输出。强制杀死进程可能留下临时工作目录，不能视为报告完成。

## JSON 表格

```json
{"financial_data":{"income_statement":{"$csv":"inputs/income.csv"}}}
```

`$csv` 读取本地 CSV。`$table` 接受记录数组或 pandas split 对象 `{"columns":[...],"index":[...],"data":[...]}`；输出表格使用同样的 `$table` split 合同，可保存后直接再次调用。`$json` 递归加载本地 JSON。这些路径始终相对当前调用者的 cwd，嵌套 JSON 不改变路径基准。数值 `NaN/Infinity` 输入被拒绝，结果中的 pandas 缺失值以 JSON null 表示。

不在参数 JSON 中传入 API key。配置仅走单独的 `--config-file`，不得把真实配置文件提交。输出和错误不会回显已加载的凭证；原模块 stdout/stderr 日志不作为结果透传。模型的原始异常文本被隐藏，避免 SDK 错误回显请求头或 URL 中的密钥。

## 增强文字和结构化 Agent

`text.enhanced.executive-summary/forecast-methodology/catalyst-analysis/valuation-analysis/investment-recommendation` 分别调用原增强类的五个公开方法与原提示；包装器要求私有配置中的 `openai_api_key/openai_model`，并严格检查 SDK completion：`finish_reason=stop`、无 refusal、无工具调用且有非空文本。原模块的 pending/fallback 文本不能成为成功结果。

`text.enhanced.section-summary/risk-factors/format-data-reference` 调用原类的三个纯本地方法，跳过会创建客户端的构造函数。`format-data-reference` 的 `source` 和 `metric` 必须为非空的调用者标签，不会自动补“FMP”来源，也不证明来源正确。

`text.agent-section` 参数为 `data/text_type/company_name/company_ticker`。`text_type` 为八项之一：`tagline/company_overview/investment_overview/valuation_overview/risks/competitor_analysis/major_takeaways/news_summary`。`data` 仅接受 `financial_metrics/peer_ebitda/peer_ev_ebitda` 三张表以及 `company_news` 新闻列表；至少提供一项非空、可消费材料，`news_summary` 必须有新闻。表格使用 `$csv/$table`；每条新闻至少有非空 `title` 或 `text`。原 manager 只把新闻 `title/publishedDate/text` 放入提示，`url/source` 不进入原提示，不能据此宣称引用已被抓取或验证。

SDK 路径保留原 manager 与八种输出类型；包装器使用 `openai-agents==0.3.3` 的显式 `OpenAIChatCompletionsModel`，避开原源码中不兼容的 `ModelSettings(model=...)` 环境分支。导入前设置禁 tracing，导入后再禁 tracing，运行配置仍要求禁 tracing 和敏感数据 tracing。Agent 工具、handoff、MCP 均为空，每次最多一轮；非预期输出类型、空字段或字符串 fallback 均拒绝。SDK 分支只有 mock 验证，没有真实模型调用验收。

## 完整 pipeline

参考 [../examples/offline-pipeline.json](../examples/offline-pipeline.json)。必需字段：`ticker`、`company_name`、`forecast_config`、`valuation_inputs`。离线时还需 `financial_data`，包括非空 `income_statement/balance_sheet/cash_flow/ratios/key_metrics` 表。源财务表的 `date/year` 和金额/比率字段合同来自冻结模块；年度数据不能混入季度行。

`forecast_config` 示例：

```json
{
  "revenue_base_year":"2024A",
  "revenue_growth_assumptions":{"2025E":0.05,"2026E":0.06,"2027E":0.04},
  "margin_improvement":{"Contribution Margin":0.01,"EBITDA Margin":0.01},
  "sga_margin_change":-0.005
}
```

基准必须为最新实际年度，预测年必须从下一年起连续。该示例仅适用于内置合成数据；不能直接把过去年份当当前预测。

`valuation_inputs` 与 `valuation.combined` 的参数相同：

```json
{
  "financial_data":{"ebitda":36000000,"free_cash_flow":21600000,"shares_outstanding":10000000,"current_price":30},
  "target_multiple":12,
  "assumptions":{"growth_rate_1_5":0.1,"growth_rate_6_10":0.05,"terminal_growth":0.025,"wacc":0.1,"projection_years":10}
}
```

金额与股数必须使用一致单位；股数为实际股数时金额应为货币原单位。DCF 的 5 项参数必须全部提供；WACC 必须大于终值增长率和 0。上游净债务按企业价值 10% 假设始终披露。不要把上游 `confidence` 权重当概率。

`text_sections` 需要 `tagline/company_overview/investment_overview/valuation_overview/risks/competitor_analysis/major_takeaways/news_summary` 八项非空文字。选择 `generate_text:true` 时必须同时设置两个外部能力开关和私有模型配置。任何模型缺失、空响应或失败都拒绝完成；不会将 fallback 文本当真分析。`pdf:true` 增加 PDF，HTML 始终生成。

`report.html/report.pdf` 接受 `data` 对象，必须包含 `analysis_df` 表、公司标识及上述核心文字。HTML 保留模板内嵌 CSS，但移除外部 CDN 脚本和字体加载。PDF 图表需要 `data:image/...;base64,...` 值；完整 pipeline 会自动提供。调用者提供的文字不经过模型或事实核验。

## 证据与错误

`schema_version` 为 `equity-research-cli-v1`。六个固定顶层字段：`schema_version, ok, command, evidence_level, data, error`。成功 `error=null`；失败 `error={code,message,hint}`。`--help` 是纯 JSON 的唯一正常例外。

| evidence_level | 含义 |
|---|---|
| `INTERFACE_METADATA` | 接口说明 |
| `LOCAL_ENVIRONMENT_CHECKS` | 本机依赖和冻结源码哈希 |
| `CONFIG_TEMPLATE_ONLY` | 只创建占位模板 |
| `SYNTHETIC_FIXTURE_ONLY` | 虚构数据与供给的合成文字，没有模型/市场请求 |
| `LOCAL_INPUT_CALCULATIONS` | 使用调用者本地输入执行；输入来源未认证 |
| `EXTERNAL_DATA_UNVERIFIED` | 显式允许外部数据调用；没有独立认证数据 |
| `MODEL_ASSISTED_RESEARCH` | 显式启用模型的研究输出；不是因果收益证据 |
| `NONE` | 未形成可用结果 |

退出码：0 完成；2 参数/输入/能力开关/完整性失败；3 依赖/SDK/模型/超时失败。重要错误包括 `NETWORK_DISABLED`、`MODEL_DISABLED`、`CONFIG_REQUIRED`、`EMPTY_UPSTREAM_DATA`、`INVALID_FORECAST`、`INVALID_VALUATION`、`OUTPUT_EXISTS`、`INCOMPLETE_MODEL_OUTPUT`、`SOURCE_PIPELINE_FAILED`、`DEPENDENCY_UNAVAILABLE`。

## 来源与依赖

冻结源来自源项目提交 `d221910096de87579b02f8f0674652bf1a175f51` 的当前工作树，包含 6 个已有本地修改/新增 Python 文件。副本为这些文件增加了修改声明；原项目未修改。逐文件源哈希、分发哈希及冻结 manifest 摘要在 `SOURCE_PROVENANCE.json`。依照根 LICENSE/NOTICE 的 Apache-2.0，保留许可证和商标政策；未采用源 setup.py 中冲突的 MIT 元数据。

`requirements.lock.txt` 固定本次 Python 3.11 验证环境的全部已解析依赖，平台需提供对应包版本。包含 `openai-agents==0.3.3` 及其传递依赖；安装 SDK 的 MCP/服务器库依赖不会启动服务器或启用工具。未部署原 web app、Notebook 或服务器流程。没有真实 API/model 请求验收，没有用户报告或秘密配置随包分发。
