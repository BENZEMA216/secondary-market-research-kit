# 金融工具调用合同

工具包使用 Python >= 3.11。脚本、注册表和本地文本工具只需标准库；各数据工具按需导入对应依赖。`tool_registry.json` 是固定白名单，包含每项工具的原始 callable、描述、JSON 参数 schema、依赖、环境变量名称、network/model/filesystem 权限、包装状态和确切阻断原因。

## 接口

```text
python3.11 scripts/tools.py [--timeout SECONDS] list
python3.11 scripts/tools.py describe [TOOL]
python3.11 scripts/tools.py doctor [TOOL]
python3.11 scripts/tools.py [--timeout SECONDS] run TOOL --args-file FILE --output NEWDIR [--allow-network] [--allow-model]
```

全局超时默认为 180 秒，最大 3600 秒，放在命令之前。`run` 的输出目录必须不存在，不覆盖空目录或旧结果。`--args-file` 为最多 2 MB 的严格 UTF-8 JSON：拒绝重复 key、NaN/Infinity、未知字段、错误类型、无效日期和未允许的文件/URL。输入路径相对调用者 cwd 解析，Agent 应优先使用绝对路径；本地材料只读。

所有命令，包括 `--help`，stdout 是一个 JSON 对象：

```json
{
  "schema_version": "market-data-cli-v1",
  "ok": true,
  "command": "list",
  "evidence_level": "INTERFACE_METADATA",
  "data": {},
  "error": null
}
```

失败的 `error` 包含 `code`、`message`、`hint`。退出码 `0` 表示调度完成，`2` 表示参数/权限/输出冲突/完整性错误，`3` 表示依赖、凭证、上游或超时错误。上游明确的 no-data sentinel 和空表作为 `UPSTREAM_NO_DATA` 返回，不当作有效财务数据。

证据层为：

| `evidence_level` | 含义 |
|---|---|
| `INTERFACE_METADATA` | 静态接口能力，无数据源调用 |
| `LOCAL_ENVIRONMENT_CHECKS` | 依赖发现、凭证存在性与文件完整性检查 |
| `LOCAL_DOCUMENT_PROCESSING` | 本地文本或材料处理；不能证明材料真实 |
| `EXTERNAL_PROVIDER_OUTPUT_UNVERIFIED` | 获得上游输出，未独立认证市场来源与点时属性 |
| `NONE` | 当前请求失败，没有取得所请求的研究证据 |

`doctor` 的 `ok` 表示检查本身执行完毕。还须查看 `dependencies[].available`、`environment[].present`、`runtime_integrity.ok` 与 `skill_integrity.ok`，不能把总 `ok` 当成全部依赖已满足。

## 可调用范围

| 组别 | `wrapped` 数量 | 说明 |
|---|---:|---|
| YFinance | 8 | 股价、当前公司信息、分红、财务报表和分析师建议 |
| FMP legacy | 6 | 目标价、SEC 链接、市值、BVPS、财务指标与竞争者指标 |
| Finnhub | 4 | 公司简介、新闻、财务历史与当前指标 |
| SEC-API | 4 | 10-K 元数据、HTML、PDF 与章节 |
| Reddit | 1 | wallstreetbets、stocks、investing 搜索；不是历史社区全集 |
| FinNLP | 7 | CNBC、第一财经、InvestorPlace、新浪、Finnhub、雪球、Stocktwits |
| ReportAnalysis | 10 | 三张报表/分部/风险/竞争/业务/公司描述/关键数据与写作材料 |
| Chart | 3 | mplfinance 股价图、相对标普表现、PE/EPS 图 |
| Backtrader | 1 | 固定 SMA 交叉策略，无任意类加载 |
| ReportLab | 1 | 汇编明确给出的文本与本地图片 |
| Text | 1 | 沿用上游 `text.split()` 词数检查，非中文字符计数 |
| 安全材料与检索适配器 | 3 | 电话会、SEC Archives、本地词项检索 |
| 合计 | 49 | 存在可调用包装；外部服务未做线上认证 |

`list` 返回完整 53 项，包括以下 4 项已明确阻断的旧接口。安全替代实现用于同类材料研究，但不冒充原 embedding/GPU 管道已经验证。

| 旧接口 | 具体阻断原因 | 可执行替代 |
|---|---|---|
| `documents.get_data` | 原入口立即导入多个重依赖管道，使用固定输出目录，并依赖带内置 auth 占位的电话会文件 | `documents.earnings_transcript` 或 `documents.sec_archive` / `sec.download_10k_filing` |
| `rag.get_rag_function` | 接受任意 AutoGen retrieve_config，返回 Python closure/agent，并可能隐式下载 embedding | 下载指定材料后运行 `rag.retrieve_local` |
| `rag.rag_database_earnings_call` | 使用原 auth 占位下载器、隐式 all-MiniLM 获取、固定 Chroma 路径并返回闭包 | `documents.earnings_transcript` → `rag.retrieve_local` |
| `rag.rag_database_sec` | 原抓取管道硬编码上游机构联系身份；Markdown 分支还存在 `emb_fn` 定义前引用；固定数据库路径与闭包输出 | 明确提供自己的 `SEC_USER_AGENT`，运行 `documents.sec_archive` → `rag.retrieve_local` |

原源树中的 `CodingUtils`、`IPythonUtils`、装饰器、内部函数及 Backtrader 生命周期方法不是金融调用接口，不开放任意代码执行。Blocked 状态不是缺少 API key 的同义词；补齐依赖也不能绕过已声明的入口限制。

## 依赖与凭证

安装命令仅供使用者在选定环境中按需执行；本脚本不安装依赖。requirements 文件是按功能拆分的依赖清单，**不是经过线上联调的锁定环境**。不要把所有重框架视为简单行情调用的必需项。

| 工具 | 可选依赖文件 | 仅检查存在性的环境变量 |
|---|---|---|
| `yfinance.*` | `requirements/yfinance.txt` | 无 |
| `fmp.*` | `requirements/fmp.txt` | `FMP_API_KEY` |
| `finnhub.*` | `requirements/finnhub.txt` | `FINNHUB_API_KEY` |
| `sec.*` | `requirements/sec.txt` | `SEC_API_KEY`；章节未提供 report_address 时另需 `FMP_API_KEY` |
| `reddit.*` | `requirements/reddit.txt` | `REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET` |
| `finnlp.*` | `requirements/finnlp.txt` | Finnhub 分支需要 `FINNHUB_API_KEY`；其余以所提供 FinNLP 版本为准 |
| `chart.*` | `requirements/charts.txt` | 无 |
| `analysis.*`、`report.*` | `requirements/reports.txt` | 看每项 describe；主要为 FMP/SEC key |
| `backtrader.back_test` | `requirements/backtrader.txt` | 无 |
| `documents.earnings_transcript` | 标准库 | `DCF_USERNAME`、`DCF_PASSWORD` |
| `documents.sec_archive` | 标准库 | `SEC_USER_AGENT`，包含使用者自己的组织/联系邮箱 |
| `rag.retrieve_local`、`text.check_text_length` | 标准库 | 无 |

例如仅选择行情工具时，可在使用者确认的虚拟环境中运行 `python3.11 -m pip install -r requirements/yfinance.txt`。FinNLP 的模块来源未在原项目锁定：其 requirements 只列基础依赖，需要使用者另外提供经过审核的 `finnlp` 版本，不自动安装同名来源不明的软件包。

wrapper 通过明确的 namespace shim 绕开原包 eager import，然后仅加载选中源模块。原源码字节未改。SEC section cache、Matplotlib 和可配置的 Yahoo cache 被定向到临时运行目录；缓存不进入交付结果。这是受限接口与缓存管理，不是操作系统沙箱或第三方包完整安全认证。

## 材料获取与本地检索

`documents.earnings_transcript` 的 JSON 参数为 `ticker`、整数 `year` 和 `quarter`（Q1–Q4）。它只访问固定 DiscountingCashFlows endpoint，认证来自环境。保存提供者原始日期与请求日期，不沿用原版自动改年行为；数据空缺或格式错误返回非零退出码。接口可达性和订阅权限需要另行实测。

`documents.sec_archive` 的参数为明确的 `url`，只接受 `https://www.sec.gov/Archives/...` 或 `https://sec.gov/Archives/...`，拒绝 URL 凭证、查询串和跨 origin 跳转。使用本人配置的 `SEC_USER_AGENT`，不借用上游机构联系身份。下载最多 20 MB。HTML/TXT 提取为 `artifacts/documents.json`；PDF 只归档，不声称已经识别文字。

`rag.retrieve_local` 的参数：

```json
{
  "paths": ["/path/to/download-run/artifacts/documents.json"],
  "query": "revenue cash flow 风险",
  "max_results": 5,
  "chunk_chars": 1200
}
```

接受最多 20 个、每个不超过 5 MB 的 UTF-8 TXT/Markdown/JSON/JSONL。JSON 文档为 `[{"page_content":"...","metadata":{}}]`，也接受含 `documents` 数组的对象。返回片段、词项匹配分数、文件 SHA-256、document_index、字符起止位置与原 metadata。分数是确定性词项匹配，不是置信度，也不是事实核验。

## 输出和权限

数据源调用必须显式传 `--allow-network`。当前 wrapped 工具均不调用生成式模型；analysis 只是组装材料，local retrieval 不下载 embedding。保留 `--allow-model` 作为明确权限字段，但它不能启用被阻断的工厂。

`run` 在独立临时目录完成后才发布输出。结果包含 `result.json`、`receipt.json`、上游生成的 `artifacts/` 文件；stdout 返回结果、具体产物路径与哈希、脱敏日志末尾和警告。出错时不发布请求的结果目录；上游可能已经消耗网络/API配额，不能把本地未发布理解为远端请求未发生。

原 `save_path`、`save_folder`、`save_fig` 不由用户任意传入，改为 wrapper 注入的输出位置。ReportLab 输入图片必须为明确给出的本地 PNG/JPEG，文本先转义，避免把用户文字当报告 markup。Backtrader 仅接受 `SMA_CrossOver`、有界 fast/slow 参数、正整数 sizer 与现金，不暴露自定义 module、indicator 或可执行字符串。

环境 key 只检查是否非空，不显示值。标准输出、错误和日志先脱敏；文本产物同样处理，发现二进制产物包含已配置凭证时拒绝发布。缓存和复制的输入图片不进入输出包。不要把私有材料或运行目录加入发布仓库。

## 研究限制

FMP legacy `/api/v3`、`/api/v4` 端点可能需要既有权限；新版 stable 客户端属于另一个 `equity-research-toolkit` Skill。这里保留上游行为并明确标识，不把端点改名当成验证成功。

上游 FMP 指标存在历史 growth/FCF 公式问题，目标价宽窗口与 BVPS 最近日期选择可能取到未来观察；`analysis.get_key_data` 混用当前信息，且上游高低价公式颠倒。这些结果只能作为待复核材料，相关字段必须独立重算后才能用于结论。

Finnhub 新闻可能随机抽样；Reddit 返回搜索结果而不是历史全集。Yahoo 回放不提供点时股票池、退市证券、真实 NBBO 或执行成本认证。生成图表、PDF、候选指令、回放统计或本地测试通过，均不构成收益证明或实盘验收。

## 来源与授权

`SOURCE_PROVENANCE.json` 记录原 FinRobot 完整 Git SHA、复制文件逐项 hash 与 `runtime/MANIFEST.sha256` 的冻结 hash。运行时保留原始 `LICENSE`、`NOTICE`、`TRADEMARK_POLICY.md` 字节；上游 LICENSE 声明 Apache-2.0。未复制存在冲突许可元数据的 setup.py。

没有复制原配置/凭证、notebook及其输出、生成报告、通用代码执行器、带硬编码 auth 占位的电话会下载器或借用上游机构身份的 SEC 抓取管道。此独立工具不代表上游项目背书；产品名称与源码包 `finrobot` 的归属说明分开。
