# 第三方来源与保留声明

本仓库的 `equity-research-toolkit` 与 `market-data-toolkit` 包含来自 [AI4Finance Foundation / FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) 的代码快照，并增加本仓库的研究工具适配层。它们是独立的衍生研究工具包，不是官方 FinRobot 产品，也不表示获得上游背书。

上游归属声明：**Copyright © 2024–2026 AI4Finance Foundation.**

## 随包保留的原文件

| 工具包 | 许可证文本 | 原 NOTICE | 原商标政策 | 文件来源记录 |
|---|---|---|---|---|
| `equity-research-toolkit` | [LICENSE](skills/equity-research-toolkit/runtime/LICENSE) | [NOTICE](skills/equity-research-toolkit/runtime/NOTICE) | [TRADEMARK_POLICY.md](skills/equity-research-toolkit/runtime/TRADEMARK_POLICY.md) | [SOURCE_PROVENANCE.json](skills/equity-research-toolkit/SOURCE_PROVENANCE.json) |
| `market-data-toolkit` | [LICENSE](skills/market-data-toolkit/runtime/LICENSE) | [NOTICE](skills/market-data-toolkit/runtime/NOTICE) | [TRADEMARK_POLICY.md](skills/market-data-toolkit/runtime/TRADEMARK_POLICY.md) | [SOURCE_PROVENANCE.json](skills/market-data-toolkit/SOURCE_PROVENANCE.json) |

复制的 FinRobot `LICENSE` 与 `NOTICE` 声明 **Apache License, Version 2.0**。原 `NOTICE` 还包含第三方软件及 AI 生成内容说明；其完整文本保留在上表文件中，本文不替代原文件。

来源快照中的 `setup.py` 将许可证元数据写为 MIT，与该快照的 `LICENSE` 和 `NOTICE` 存在冲突。本仓库记录这一原始元数据差异，保留 Apache-2.0 许可证文本和原 NOTICE，不将该 `setup.py` 的 MIT 字段作为本分发包的许可证声明，也不将第三方代码改标为本仓库自有代码。

`FinRobot` 与 `AI4Finance` 的商标说明保留在原文件中。本仓库仅用这些名称说明来源和兼容关系；独立工具包使用各自的 Skill 名称。

## 来源快照与修改说明

两套 FinRobot 工具包的来源记录保存基准 revision、实际分发文件及 hash，并区分原样复制、本地已有改动与分发适配。来源工作区包含未提交改动时，单独的 Git revision 不能代表全部分发字节；应同时阅读对应 `SOURCE_PROVENANCE.json` 和各级 `MANIFEST.sha256`。

适配层、补充说明和原文件的修改标记按实际文件保留。含本地已有修改的文件不应称为未经修改的上游版本；为其补充修改声明也不代表原算法行为获得了新的验证。

`scenario-router-research` 的来源另见仓库根目录 [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json) 及该 Skill 的运行时清单。FinRobot 的许可证与归属声明仅说明对应第三方部分，不替其他独立来源或本仓库新增文件选择许可证。

## 外部依赖与数据服务

| 类别 | 代表性依赖或服务 | 本仓库的处理 |
|---|---|---|
| Python 数据与绘图 | pandas、NumPy、Matplotlib、mplfinance、Backtrader | 按具体 Skill 的依赖文件与命令声明安装；不复制原用户虚拟环境 |
| 文档与报告 | ReportLab、原 SEC/Marker/RAG 管线所需解析器、向量库与模型组件 | 只有所选接口的依赖才适用；受限原管线及替代入口见 [覆盖范围](TOOL_COVERAGE.md) |
| 模型与 Agent SDK | OpenAI SDK、openai-agents、Autogen 及原 RAG 依赖 | 代码或依赖声明不包含模型账户、凭证或自动启用模型的授权 |
| 金融及社交数据 | FMP、Yahoo Finance、Finnhub、SEC 相关服务、Reddit、FinNLP 各来源、Adanos | 工具代码不提供服务订阅、API key 或数据分发授权；本包不附带用户采集的真实数据与报告 |
| 浏览器辅助 | [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite) / ego-browser | 作为外部运行时依赖记录；不内嵌浏览器二进制、登录状态或浏览数据 |

各依赖的具体版本、许可证文件与服务条件由相应项目或服务提供；本表用于识别来源，不是完整依赖许可证清单，也不把上游 NOTICE 中列出的库都称为本包实际内嵌的代码。

## 分发排除项

公开分发范围由发布清单确定。真实 API 配置、账号数据库、用户行情与归档材料、生成报告、模型会话、notebook 既有输出、截图、日志、缓存、虚拟环境和浏览器用户数据不属于此代码包。需要运行外部工具时，由接收方提供其明确选择并有权使用的配置、输入和独立输出位置。
