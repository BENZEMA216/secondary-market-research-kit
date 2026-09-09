# 二级市场研究工具包

把当前「二级」项目中的策略研究、公司研报与市场数据工具，整理成三个可独立安装的 Skill 和统一 JSON 调用入口。工具包版本 **0.2.0**；Scenario Router 引擎版本仍为 **0.3.0**。

| Skill | 解决的问题 | 入口 |
|---|---|---|
| `scenario-router-research` | 反转/事件策略、组合风控、paper执行、历史回放、材料复核 | `scripts/research.py` |
| `equity-research-toolkit` | FMP数据、财务预测、同业、估值/敏感性、新闻/催化剂、图表与HTML/PDF研报 | `scripts/equity.py` |
| `market-data-toolkit` | Yahoo Finance、FMP legacy、Finnhub、SEC、Reddit、FinNLP、材料获取/检索与固定策略回测 | `scripts/tools.py` |

后两组使用 FinRobot 来源代码与独立适配层，保留其许可证和归属。默认离线；外部数据与模型调用由命令显式开启。源码与 Skill 中不包含用户凭证、账号库、已有研报或浏览器登录态。完整范围、原入口对照与未适配的 legacy 扩展见 [工具覆盖说明](TOOL_COVERAGE.md)。

## 第一次运行

需要 Python **3.11 或更新版本**。下面使用 `python3.11`；若已确认 `python3 --version` 至少为 3.11，也可替换为 `python3`。统一入口、能力清单和依赖检查使用标准库；财务、图表等工具按需使用各 Skill 声明的依赖。Scenario Router 需要 IANA 时区数据；缺少时由 `doctor` 提示，脚本不会自动安装。从仓库根目录运行：

财务研报的锁定依赖使用 Python **3.11** 验收，建议按该版本创建独立环境，不把其他 Python 版本的目录检查通过当作全部金融依赖已经可用。

```bash
python3.11 scripts/toolkit.py list
python3.11 scripts/toolkit.py describe equity-research-toolkit
python3.11 scripts/toolkit.py describe market-data-toolkit
python3.11 skills/scenario-router-research/scripts/research.py describe
python3.11 skills/scenario-router-research/scripts/research.py doctor
python3.11 scripts/share.py verify
python3.11 skills/scenario-router-research/scripts/research.py demo --kind signal
python3.11 skills/scenario-router-research/scripts/research.py demo --kind paper
python3.11 skills/scenario-router-research/scripts/research.py validate
```

除人类可读的 `--help` 外，每条脚本命令向标准输出写入一个 JSON 对象，方便 Agent 或其他程序解析。`describe` 返回能力与参数说明；`doctor` 检查本地运行条件；`share.py verify` 检查发布文件完整性；`demo` 运行合成样例；`validate` 运行本地校验。各层结果需分别阅读：完整性通过不等于测试通过，合成测试通过不等于真实历史策略有效。

## 安装为项目 Skill

仓库：[BENZEMA216/secondary-market-research-kit](https://github.com/BENZEMA216/secondary-market-research-kit)。当前为公开仓库，可以直接克隆或从 Releases 下载分享包。

```bash
git clone https://github.com/BENZEMA216/secondary-market-research-kit.git
cd secondary-market-research-kit
```

将 Skill 安装到指定项目：

```bash
python3.11 scripts/share.py install --project /path/to/your-project --skill all
```

一次复制三个独立 Skill 到该项目的 `.agents/skills/`。只安装一个时，将 `all` 换为表中的 Skill 名称。不传 `--skill` 时保留旧版行为，仅安装 Scenario Router。Claude Code 使用：

```bash
python3.11 scripts/share.py install --project /path/to/your-project --client claude --skill all
```

对应目录是 `.claude/skills/`。安装会复制包含运行时的完整 Skill，不依赖原仓库路径；任何所选目标目录已存在时，会在写入前拒绝整个安装。更新时先保存需要保留的内容，再使用新的目标项目或明确处理旧版本。安装不改变全局 Skill 配置。第三方 Python 依赖仍需按对应 Skill 的说明安装到自己的环境。

也可解压独立 Skill 包，把整个 Skill 目录放进项目的 `.agents/skills/` 或 `.claude/skills/`；不要只复制 `SKILL.md`。重新打开项目或刷新客户端的 Skill 发现后，可对 Agent 说：

> 使用 scenario-router-research，先检查环境和完整性，再运行信号与 paper 合成示例。按证据层级解释结果。

> 使用 scenario-router-research，检查我提供的数据是否满足历史回放合同。先说明缺失的数据与时点约束，再运行指定实验。

> 使用 equity-research-toolkit，先检查环境，再用合成示例生成财务分析、估值和 HTML/PDF 研报。

> 使用 market-data-toolkit，查看可用数据来源和所需配置，再按我指定的公司、日期与材料范围调用。

Skill 的实际发现方式取决于所用客户端；直接调用 Python 脚本始终可用。

## Agent 调用入口

统一入口是 [scripts/toolkit.py](scripts/toolkit.py)：

```bash
python3.11 scripts/toolkit.py list
python3.11 scripts/toolkit.py describe equity-research-toolkit
python3.11 scripts/toolkit.py doctor market-data-toolkit
python3.11 scripts/toolkit.py run scenario-router-research -- demo --kind paper
```

公司研报的完整离线样例需要安装其锁定依赖：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r skills/equity-research-toolkit/requirements.lock.txt
.venv/bin/python scripts/toolkit.py run equity-research-toolkit -- demo --output dist/equity-demo --pdf
```

市场工具的文本样例只需标准库：

```bash
python3.11 scripts/toolkit.py run market-data-toolkit -- run text.check_text_length \
  --args-file skills/market-data-toolkit/examples/text-args.json --output dist/text-check
```

样例输出目录必须尚不存在；重复运行时更换目录。上述样例不请求真实行情或模型。

`describe` 返回对应工具组的完整命令、参数、依赖与副作用清单；`run TOOLKIT -- ...` 保留子工具的 JSON 与退出码。各 Skill 可脱离仓库直接运行自己的脚本。Scenario Router 的接口如下，完整契约见 [Agent 契约](skills/scenario-router-research/references/agent-contract.md)。另外两组工具以自己的 `describe` 与 `SKILL.md` 为准。

| 命令 | 用途 |
|---|---|
| `describe` | 能力、默认值、数据要求与边界 |
| `doctor` | 本地环境检查 |
| `validate` | 本地测试和合同校验 |
| `demo --kind signal` | 合成信号示例 |
| `demo --kind paper` | 合成本地组合与模拟成交示例 |
| `backtest` | 使用外部 CSV/JSONL 数据做历史回放 |
| `evidence analyze` | 读取两份归档公司材料并记录复核状态 |
| `evidence correct` | 基于预期 revision 提交修订复核 |
| `evidence status` | 查看已保存状态，不重新分析 |

业务结果保存在 JSON 的 `data` 中。调用方先检查退出码与 `ok`，再检查 `evidence_level` 和业务 `state`，不得仅凭进程成功就断言材料已核验或策略有效。具体退出码和字段见 Agent 契约。

历史回放要求使用者准备交易日历、点时股票池、日线、五分钟线、边界 NBBO 和四类事件账本，共九个文件。输入、参数、输出和成交假设见 [历史回放合同](skills/scenario-router-research/runtime/BACKTEST.md)。现有单份 coverage manifest 的 `point_in_time` 模式只支持一个交易日并拒绝 M4；多日 `retrospective_audit` 不能称为实时一致性证明。

## 生成分享包

从仓库根目录运行：

```bash
python3.11 scripts/share.py verify
python3.11 scripts/share.py build --output dist/0.2.0
```

`build` 输出三个独立 Skill ZIP、完整仓库源码 ZIP 和 `SHA256SUMS`，具体路径由 JSON 返回。分享独立 Skill ZIP 可让对方直接安装；分享源码 ZIP 可让对方检查代码并继续开发。根 `MANIFEST.sha256` 记录仓库发布文件哈希，每个 Skill 自带完整性清单，并保留冻结运行时的 `runtime/MANIFEST.sha256`。Scenario Router 来源记录位于仓库根目录；另两个 Skill 自带来源记录。已发布的 v0.1.0 保持原样。

哈希验证证明文件与记录一致，不能证明文件来源可信，也不会运行测试。接收者应先核对分享者通过可信渠道提供的校验值，再运行环境检查与校验。构建脚本仅打包清单中的文件，运行产物、个人数据、密钥和 Git 元数据不属于交付内容。

## 阅读顺序与证据边界

1. [Skill 入口](skills/scenario-router-research/SKILL.md)：Agent 何时使用、如何执行。
2. [三层策略说明](skills/scenario-router-research/runtime/STRATEGY_THREE_LAYER_RESEARCH.md)：事件路由、交易规则、AI 权限与后续研究。
3. [Agent 契约](skills/scenario-router-research/references/agent-contract.md)：机器接口与结果解释。
4. [历史回放合同](skills/scenario-router-research/runtime/BACKTEST.md)：外部数据与执行假设。
5. [引擎 README](skills/scenario-router-research/runtime/README.md)：底层 Python 模块和原生入口。

示例与内置测试使用合成数据。`historical_point_in_time` 和 `--code-revision` 都是调用者声明；即使格式、时间、身份和哈希校验通过，也没有认证供应商、数据版本或点时完整性。历史回放尚不模拟完整市场冲击、部分成交、停牌与所有公司行动，也未提供盈利、OOS 稳健性或实盘可用性证明。

AI 复核默认 `--provider off`，只可为已合格的 E2B 候选增加否决。启用 `--provider codex` 需要调用者明确选择并具备可用的本地 Codex CLI；这可能向配置的模型服务发送所提供的材料。AI 无权自动晋升 verified 事件、改变仓位与风控、解除锁存或下单。

## 仓库范围与来源

本版覆盖当前目录的 Scenario Router 与 FinRobot 金融工具。根 [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json) 记录 Scenario Router 来源，另两个 Skill 各有来源文件与修改说明。原项目保持不变；第三方派生代码与包装层分别记录。项目中的 Ego Browser 是通用浏览器依赖，继续使用其官方 Skill 与本机运行时，不复制浏览器应用或用户数据。

本地验证记录见 [VALIDATION.md](VALIDATION.md)。远端各次提交的检查结果见仓库 Actions，按具体提交查看。

本仓库公开可读。FinRobot 来源部分按其保留的 Apache-2.0 LICENSE/NOTICE 分发，详情见 [第三方归属](THIRD_PARTY_NOTICES.md)。自有包装层尚未另行授予开源许可证；仓库公开不改变各部分的许可归属。
