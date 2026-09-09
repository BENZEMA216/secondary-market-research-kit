# 工具覆盖范围

本仓库将策略研究、公司研报和市场数据工具整理为三套 Skill。此文记录原工具的去向、接口边界和验证含义；具体命令、参数、依赖及受限项目以本次发布的工具注册表和 `describe` 输出为准，不使用固定工具数量代表覆盖完成。

在仓库根目录查看接口：

```bash
python3.11 scripts/toolkit.py list
python3.11 scripts/toolkit.py describe scenario-router-research
python3.11 scripts/toolkit.py describe equity-research-toolkit
python3.11 scripts/toolkit.py describe market-data-toolkit
```

`list` 和 `describe` 展示接口元数据。`doctor` 检查其实现声明的本机条件，不能代替数据供应商请求、模型调用或真实历史研究。收到 JSON 后，应同时检查退出码、`ok`、证据层级、业务状态及缺失项；被列入注册表的受限工具不代表可以直接执行。

## 三套 Skill 的职责

| Skill | 来源与覆盖能力 | 必须保留的边界 |
|---|---|---|
| [scenario-router-research](skills/scenario-router-research/SKILL.md) | Scenario Router 的场景路由、事件与反转规则、组合风控、本地 paper 模拟、历史回放、公司材料复核 | 合成样例不能作为真实行情证据；回放需要调用者选择和提供数据。模型 provider 默认关闭，无券商下单接口 |
| [equity-research-toolkit](skills/equity-research-toolkit/SKILL.md) | FinRobot equity core 的财务数据、预测、同业比较、估值、敏感性、催化剂、新闻、零售情绪、图表、HTML/PDF 和文字生成接口 | 数据请求需要相应服务配置；模型生成需要单独启用。确定性计算仍依赖输入和预测假设，生成报告不代表结论已经独立核实 |
| [market-data-toolkit](skills/market-data-toolkit/SKILL.md) | FinRobot legacy 的行情、财报、新闻、SEC、社交数据、研究材料、图表、文本检查和固定策略回测接口 | 部分 legacy 路径需要额外依赖或被显式阻断；替代入口与原实现应分别标明，不把离线封装检查写成供应商当前可用 |

## 原金融工具的归属

| 原能力组 | 本仓库接口归属 | 覆盖解释 |
|---|---|---|
| 完整公司研报流水线 | `equity-research-toolkit` | 财务分析、HTML 报告、可选 PDF 生成；文字生成与网络请求按命令合同启用 |
| FMP Stable 财务与行情 | `equity-research-toolkit` | 三张财务报表、比率与关键指标、企业价值、公司与同业数据、报价、分析师、技术指标和新闻 |
| 财务加工与预测 | `equity-research-toolkit` | 历史指标提取、数值清洗、增长与预测、同业 EBITDA 预测；输入、年份和假设以接口声明为准 |
| 估值与敏感性 | `equity-research-toolkit` | EV/EBITDA、同业和 DCF 估值，估值区间与综合输出，收入和利润率敏感性。计算输出不是经市场验证的目标价格 |
| 新闻、催化剂与零售情绪 | `equity-research-toolkit` | 抓取与本地加工是不同步骤；新闻分类、影响分级和催化剂识别属于实现中的规则，模型文字另行标识 |
| 财务、价格、估值与高级图表 | `equity-research-toolkit` | 对调用方提供或明确请求的数据绘图；图表生成成功不证明其数据完整或及时 |
| HTML/PDF 渲染与报告结构 | `equity-research-toolkit` | 文本、表格、图像、模板和报告文件输出；缺失内容与降级必须保留在结果中 |
| 普通文字生成与独立 Agent 文字扩展 | `equity-research-toolkit` | `text.section`、八个 `text.enhanced.*` 方法和 `text.agent-section` 分别提供普通章节、增强文字/本地格式处理和原 Agent Manager 的结构化章节入口；模型接口需显式启用。材料辅助生成不代表引用已核验，也不是默认已运行的 Agent 团队 |
| Yahoo Finance / `YFinanceUtils` | `market-data-toolkit` | 股价、公司信息、股息、三张报表和分析师建议 |
| Finnhub / `FinnHubUtils` | `market-data-toolkit` | 公司介绍、新闻、历史及当前基础财务数据 |
| FMP legacy / `FMPUtils` | `market-data-toolkit` | 目标价、SEC 报告位置、历史市值与每股账面价值、财务和同业指标；旧端点与 Stable 接口不混为一个已验证来源 |
| SEC / `SECUtils` 与替代下载入口 | `market-data-toolkit` | 报告元数据、HTML/PDF、章节提取；原实现和替代接口的认证、数据源及参数差异以注册表为准 |
| Reddit / `RedditUtils` | `market-data-toolkit` | 按查询与日期筛选帖子，需要相应服务配置 |
| FinNLP 新闻和社交抓取 | `market-data-toolkit` | CNBC、Yicai、InvestorPlace、Sina Finance、Finnhub、Xueqiu、Stocktwits 的原工具入口；依赖和外部服务可用性分别检查 |
| `ReportAnalysisUtils` | `market-data-toolkit` | 财报、分部、风险、同业、业务及公司描述工具主要抓取材料并准备提示词文件；不把“材料已保存”写成“模型分析已完成”。关键数据聚合另行返回 |
| `MplFinanceUtils` / `ReportChartUtils` | `market-data-toolkit` | 价格图、股价表现与 PE/EPS 图；联网取数和本地绘图均应按命令标明 |
| `BackTraderUtils.back_test` | `market-data-toolkit` | 固定策略的本地模拟入口；原工具允许自定义模块的扩展能力不自动获得执行权限 |
| `ReportLabUtils` / `TextUtils` | `market-data-toolkit` | 年度报告组装及文本长度检查；PDF 组装可能还需数据和图像，不应一概标为无网络操作 |
| `finance_data.get_data`、RAG 与原 SEC/Marker 管线 | `market-data-toolkit` 注册表中的受限项和替代说明 | 原路径涉及文档抓取、解析、embedding、向量库、模型权重或外部二进制；被阻断时必须返回原因和实际提供的替代接口，不能虚报原管线已跑通 |

## 其他原组件的处理

| 组件 | 分类与分发边界 | 不应作出的完成声明 |
|---|---|---|
| 原 FinRobot WebUI | 上游交互应用。其调用的研报引擎由 `equity-research-toolkit` 承接；原认证、账号库、管理后台、服务部署和界面不属于这次 CLI 迁移的验收范围 | 不能用研报命令成功推定原 WebUI、跨用户权限或部署已经迁移并验证 |
| ego-browser | [总目录](catalog.json) 中的外部浏览器工具依赖。继续使用其官方 Skill 和用户另行安装的 Ego Lite runtime；本包不分发浏览器二进制、用户 profile、登录状态或浏览数据 | 目录中列出该依赖不证明本机已连接、登录有效或某个网站可访问 |
| `CodingUtils.list_dir/see_file/modify_code/create_file_with_code` | 非金融通用文件工具，未默认暴露到金融调用接口。原实现的任意路径读写不能因封装任务而自动启用 | 不能将这组执行能力计为已开放的金融工具；也不能从原工具去向记录中省略 |
| `IPythonUtils.exec_python/display_image` | 依赖活动 IPython 的通用执行与展示工具，未默认暴露。执行 Python 的权限由宿主与用户任务另外决定 | 不声称已经提供安全的任意代码沙箱或 Notebook 运行环境 |
| Backtrader 自定义 strategy/sizer/indicator | 属于代码扩展能力；与固定策略模拟区分。是否支持扩展、允许哪些类，以注册表及参数检查为准 | 原函数能够动态 import 不等于包装层授权执行任意模块 |
| 实验 Agent 组合、Agent builder 和 notebooks | 演示或实验来源。已有 notebook 输出、截图、会话与运行数据不纳入公开包；金融能力按上表归属，不直接启动实验脚本 | 源码存在不代表多 Agent 已接入默认流水线，更不代表模型或代码执行已通过实际验证 |
| 内部 helper、数据类、回调、格式化函数与 PDF page builder | 随其父能力归类，部分随必要运行时保留；无需把每个函数都制造为独立 CLI。HTTP pipeline 服务辅助也不等于独立金融工具 | 不以内部函数数量扩大“工具已覆盖”数量；复制源文件也不等于每条内部路径都完成测试 |

## 验证与授权

工具覆盖至少需要分别回答：接口是否存在、依赖是否具备、离线行为是否验证、真实外部调用是否验证。发布时以实际测试结果和具体产物为准，本文件不预先声明所有接口测试通过。

| 证据 | 能说明什么 | 不能据此推定什么 |
|---|---|---|
| 注册表、`list`、`describe` | 本次发布声明的接口、参数和边界 | 依赖已安装或外部服务可用 |
| 文件 manifest 与来源记录 | 文件内容与记录一致、分发范围可追溯 | 代码正确、数据真实或供应商许可 |
| `doctor` | 其明确检查的本机条件 | 账户额度、订阅权限、模型或 API 实际成功 |
| 合成样例、mock 和离线测试 | 被执行到的接口与实现行为 | 真实市场数据覆盖、历史盈利或实盘就绪 |
| 明确标识的真实数据/API/模型运行 | 该次调用及其实际输出 | 其他 provider、其他日期、完整覆盖或持续可用 |

整理、封装、安装或分享当前工具的授权，不自动授权启动模型、调用真实数据 API、发送公司材料或执行通用代码。运行时遵循所选命令的授权要求，保留明确选择的输入、参数、数据来源和输出位置；已有文件不静默覆盖。

第三方来源与保留声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
