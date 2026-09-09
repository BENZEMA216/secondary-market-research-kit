# Agent 调用契约

契约对应工具包 0.1.0、研究引擎 0.3.0。以本 Skill 的 `SKILL.md` 所在目录为根，统一入口为 `scripts/research.py`。以下命令均以已经解析到真实安装目录为前提；示例写作 `python3.11 scripts/research.py`。

## 运行与输出

需要 Python >= 3.11 与 IANA 时区数据；默认使用标准库、离线、provider off。调用支持全局 `--timeout SECONDS`，默认 180 秒，放在子命令前：

```bash
python3.11 scripts/research.py --timeout 300 validate
```

除 `--help` 输出人类可读帮助外，stdout 始终为一个 JSON 对象。日志不得混在 JSON 前后。标准结构固定为：

```json
{
  "schema_version": "secondary-research-cli-v1",
  "ok": true,
  "command": "describe",
  "evidence_level": "INTERFACE_METADATA",
  "data": {},
  "error": null
}
```

这是结构示意，`data` 随命令变化。失败时 `error` 是含 `code`、`message`、`hint` 的对象；保留 `data` 中可用的诊断，不把错误忽略后继续做资格判断。先检查退出码和 `ok`，再看证据层与业务状态。

| 退出码 | 含义 |
|---|---|
| `0` | 命令完成，仍须检查证据层与业务状态 |
| `2` | 参数、输入、完整性或校验失败 |
| `3` | 依赖、provider 或运行时失败，包括超时 |

例如 provider-off 的分析会保存 `BLOCKED` 状态并以非零码返回；读取同一状态的 `status` 可以正常完成，但不使该状态变成合格候选。若分析超时，数据库可能留下 `RUNNING` revision；重试前先查询，不假设事务未发生。

| 证据层 | 应如何解释 |
|---|---|
| `INTERFACE_METADATA` | 工具声明的能力与合同，未运行研究 |
| `LOCAL_ENVIRONMENT_CHECKS` | 本机运行条件检查 |
| `LOCAL_SYNTHETIC_VALIDATION` | 本地合成测试与合同校验 |
| `SYNTHETIC_FIXTURE_ONLY` | 合成样例或合成回放 |
| `USER_SUPPLIED_HISTORICAL_REPLAY` | 回放用户提供且自行声明为历史的数据；未独立认证来源 |
| `SAVED_STATE_ONLY` | 读取历史保存状态，本次无新分析 |
| `MODEL_REVIEW_ONLY` | 模型材料复核层，不是事件资格、收益或下单许可 |
| `NO_MODEL_EVIDENCE` | 未获得可用模型复核证据，例如 provider-off 阻断；不能单凭此标签判断有无调用 |
| `NONE` | 当前错误没有提供所请求的研究证据 |

脚本路径可独立于当前工作目录。例如程序调用时使用参数数组，避免通过 shell 拼接用户输入：

```python
import json
import subprocess
import sys

result = subprocess.run(
    [sys.executable, str(skill_dir / "scripts" / "research.py"), "demo", "--kind", "signal"],
    text=True,
    capture_output=True,
    check=False,
)
envelope = json.loads(result.stdout)
if result.returncode != 0 or not envelope["ok"]:
    raise RuntimeError(envelope["error"])
# 下游仍须检查 evidence_level；此例只有合成证据。
```

调用方的 `sys.executable` 也必须满足 Python 版本要求；`skill_dir` 是已解析的 Skill 根目录 `Path` 对象。CLI 的数据、数据库和输出路径相对于调用者当前工作目录解析；bundle 内的来源路径相对于 bundle 目录解析。Agent 应优先传入已解析的绝对路径。

## 能力发现、环境与本地验证

```bash
python3.11 scripts/research.py describe
python3.11 scripts/research.py doctor
python3.11 scripts/research.py validate
python3.11 scripts/research.py demo --kind signal
python3.11 scripts/research.py demo --kind paper
```

`demo` 的 `--kind` 默认是 `signal`。`doctor` 检查 Python、时区数据与 Skill/运行时完整性。`validate` 在临时副本中执行 `validate.py` 和 `validate_runtime.py`，报告位于 `data.reports`，随后删除临时副本；报告里的临时路径不能作为持久附件链接。

环境检查不等于完整测试；`validate` 的通过也只提供本地合成证据。原生运行时的历史 parity 数据未包含在包内，应保留对应 `SKIPPED` 和 `PASS_SYNTHETIC_ONLY`，不能改写成历史验证通过。

缺少 `America/New_York` IANA 时区数据时，按照 `doctor` 返回的建议处理。本工具不会自动下载行情、安装依赖或开启模型 provider。

## 历史回放

先读 [BACKTEST.md](../runtime/BACKTEST.md)；其输入、成交与输出限制仍然有效。包装层保留原 `backtest_cli.py` 的参数：

| 参数 | 要求或默认值 |
|---|---|
| `--data` | 必填；外部数据目录 |
| `--output` | 必填；新输出目录或空目录，拒绝覆盖已有结果 |
| `--start`、`--end` | 必填；交易日 `YYYY-MM-DD` |
| `--data-mode` | 必填；`historical_point_in_time` 或 `synthetic_fixture` |
| `--code-revision` | 必填；历史模式要求 40 或 64 位十六进制不可变 revision |
| `--event-variant` | `E2A`（默认）或 `E2B` |
| `--reversal-variant` | `M2`、`M3` 或 `M4`（默认） |
| `--event-mode` | `strict_primary`（默认）或 `agent_assisted_secondary` |
| `--coverage-mode` | `retrospective_audit`（默认）或 `point_in_time` |
| `--initial-cash` | 默认 `100000` |
| `--slippage-bps` | 默认 `0,10,25,50`；每边额外滑点，叠加在 bid/ask 之外 |
| `--commission-per-share` | 默认 `0` |
| `--minimum-commission` | 默认 `0` |

下面是需替换路径、日期与代码 revision 的模板，不是随包可运行的真实数据实验：

```bash
python3.11 scripts/research.py backtest \
  --data /path/to/your/dataset \
  --output /path/to/new/results \
  --start 2025-01-02 --end 2025-12-31 \
  --event-variant E2A --reversal-variant M4 \
  --event-mode strict_primary \
  --coverage-mode retrospective_audit \
  --data-mode historical_point_in_time \
  --slippage-bps 0,10,25,50 \
  --commission-per-share 0.005 --minimum-commission 1 \
  --code-revision YOUR_ACTUAL_IMMUTABLE_REVISION
```

九个输入文件必须齐全：`calendar.csv`、`universe.csv`、`daily_bars.csv`、`intraday_bars.csv`、`quotes.csv`、`feed_manifest.json`、`articles.jsonl`、`events.jsonl`、`reference_snapshots.jsonl`。JSONL 允许空记录，但文件必须存在。需满足完整交易日历、稳定证券身份、同一价格基准、五分钟完整网格、日内/日线一致性、warm-up、边界报价和点时事件合同。事件 feed 的 provider 标识必须先规范化为 `news_feed`。

包装层在临时目录完成运行，成功后发布到请求的输出目录；失败不发布半成品目录。已有结果需另选新目录，不删除或覆盖旧结果。每个成本情景独立重建状态、资本和风险预算。输出包括 manifest、候选/订单/成交/交易账本、每日净值、指标、组合快照与成本对照；检查具体文件路径和哈希后再引用结果。

必须保留以下解释：

- `historical_point_in_time` 是调用者标签，程序不能认证供应商、dataset vintage 或原始数据权利。`--code-revision` 同样是被记录的声明。
- `point_in_time` 当前只支持单交易日盘前覆盖判断，不支持 M4；多日因果新闻覆盖还需要覆盖修订账本。
- `retrospective_audit` 使用事后覆盖完整性断言，不是实时一致性验证。
- E2A/E2B、M2/M3/M4 分别运行，不合并资本后声称独立对照。
- 额外滑点已包含在成交价中，不应再次从 P&L 扣除；佣金是单独现金扣项。
- 回放成功不证明盈利、OOS、源数据完整、真实成交容量或实盘可用。详细执行限制必须随研究结论保留。

## 公司材料复核

`evidence` 包装现有本地 AI 工作流。所有模式必填 `--database PATH --conversation ID`，支持 `--question TEXT`、`--output PATH`。`--output` 是可选的完整结果文件，已有文件拒绝覆盖。

| 子命令 | 额外参数与行为 |
|---|---|
| `analyze` | 必填 `--bundle PATH`；可选 `--expected-revision N` 与 `--provider off|codex`，默认 off；写入新 revision |
| `correct` | 必填 `--bundle PATH --expected-revision N`；可选 provider，默认 off；检查预期 revision 后追加修订 |
| `status` | 只读取已存在 SQLite 的已保存状态；不接受 provider 或 bundle，不创建数据库，不调用模型 |

查询示例：

```bash
python3.11 scripts/research.py evidence status \
  --database /path/to/existing/jobs.sqlite \
  --conversation research-case-001
```

模型默认关闭。用户明确选择模型复核后，才可用 `--provider codex` 调用本地 Codex CLI；材料可能发送到该 CLI 配置的模型服务。即使 provider 关闭，`analyze`/`correct` 也可能创建数据库并记录阻断 revision，因此环境自检应使用 `doctor`，不要拿真实研究数据库做连通性试验。

`bundle.json` 的顶层字段必须恰好为 `bundle_id`、`security_id`、`ticker`、`fiscal_period`、`lane`、`sources`。`fiscal_period` 格式是 `FY2026`；`lane` 可为 `historical_reconstruction`、`forward_capture` 或 `synthetic`。

`sources` 必须恰好包含一份 `current` 和一份 `prior`。每份字段必须恰好为：

| 字段 | 合同 |
|---|---|
| `source_id` | 唯一来源 ID |
| `role` | `current` 或 `prior` |
| `path` | 相对 bundle 目录的本地 UTF-8 文本路径，不能越过 bundle 目录 |
| `sha256` | 原始文件字节哈希，格式 `sha256:<64位hex>` |
| `url` | 材料来源定位符，记录来源；不是自动抓取入口 |
| `published_at` | 带时区发布时间 |
| `captured_at` | 带时区捕获时间，不早于发布时间，也不能晚于运行时刻 |

每份文本须非空且不超过 300000 字节；prior 的发布时间须早于 current。运行时对文本做确定性的空白折叠，同时保留原始文件 hash 和模型输入文本 hash。模型抽取必须引用实际传入文本，不能从记忆补齐。

工作流可能返回 `CHECKED_CANDIDATE`、`ABSTAIN`、`CONFLICT`、`INCOMPLETE`、`BLOCKED` 或 `SUPERSEDED`。`CHECKED_CANDIDATE` 仅表示抽取与代码检查达到候选状态，仍须检查 `e2b_screen`；不等于事件 verified 或买入许可。`status` 返回的是之前保存的状态，本次没有新的模型分析。

`provider_call_attempts` 记录 provider 调用尝试，也包括 `DisabledProvider`，不能作为网络请求次数。`returned_model_responses` 记录返回响应数；真实调用的供应商和模型身份仍应查阅原始结果中的调用 metadata，缺失信息写为 unavailable。`NO_MODEL_EVIDENCE` 表示未获得通过验证的模型复核证据，也可能出现在有响应但冲突或弃权的运行中。

修订失败不得恢复旧候选资格；旧请求晚到不得覆盖新 revision。若 `--expected-revision` 已过期，先查询最新状态，再按实际材料与用户意图决定新修订，不自动绕过版本检查。

## 给用户的结果报告

保留命令、解释器版本、数据与输出路径、证据层、业务状态、实验/成本/覆盖模式、关键错误和已知限制。对真实回放另报实际输入哈希、代码 revision 与输出 manifest；说明哪些字段只是调用者声明。对模型复核另报真实调用是否发生、revision、材料 hash 与业务状态。

文件完整性、环境可运行、本地测试、历史回放、数据来源认证、真实模型调用与实盘是不同的验收层。只报告实际取得的一层；没有的证据明确保留为空缺。
