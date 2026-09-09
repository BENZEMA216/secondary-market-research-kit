---
name: scenario-router-research
description: 使用 Scenario Router 进行美股二级市场策略研究、场景路由解释、合成信号与 paper 模拟、外部 CSV/JSONL 历史回放、公司指引材料复核。用户提到 Scenario Router、MACD 三段背离、五分钟四线确认、E2A/E2B、M2/M3/M4、事件延续、该策略的回测或数据接入时使用。需要 Python 3.11+；默认离线且模型 provider 关闭；不用于实盘下单或承诺收益。
---

# Scenario Router 研究 Skill

此 Skill 内含独立 Python 运行时。它将无观察文章的反转研究与正向事件延续研究按场景互斥路由，再应用确定性组合风控和本地模拟成交。所在工具包发行版为 0.2.0，接口沿用 0.1.0，引擎版本为 0.3.0。

## 定位与启动

以当前加载的 `SKILL.md` 所在目录为 **Skill 根目录**，不是用户当前工作目录。统一脚本是与本文件同级目录下的 `scripts/research.py`。解析为实际绝对路径后调用；无需进入 `runtime/` 或设置 `PYTHONPATH`。默认使用 Python 标准库，另需本机 IANA 时区数据。

```bash
python3.11 /actual/path/to/scenario-router-research/scripts/research.py describe
python3.11 /actual/path/to/scenario-router-research/scripts/research.py doctor
```

示例中的路径必须替换为本 Skill 的真实安装路径。优先从 `describe` 读取当前能力和参数，需要具体语法时使用对应子命令的 `--help`。Python 版本最低为 3.11；若已确认 `python3` 满足要求可替代 `python3.11`。若本机缺少时区数据，按 `doctor` 的提示处理 `tzdata`，不自动安装。

## 按任务执行

| 用户任务 | 操作与先读材料 |
|---|---|
| 理解策略、分层与权限 | 读取 [三层策略说明](runtime/STRATEGY_THREE_LAYER_RESEARCH.md)，按事件层、交易层、AI 层解释 |
| 验证能否运行 | `doctor`，再 `validate`；分别报告环境和校验结果 |
| 看信号或模拟成交示例 | `demo --kind signal` 或 `demo --kind paper`；明确样例为 synthetic |
| 接入数据或做回测 | 先读 [历史回放合同](runtime/BACKTEST.md)，核对九个文件和实验参数，再执行 `backtest` |
| 复核两份归档公司材料 | 先读 [Agent 契约](references/agent-contract.md) 的材料复核部分，再执行 `evidence analyze` 或 `evidence correct` |
| 查询之前的复核 | `evidence status`；解释这是保存状态，不是重新调用模型 |
| 检查底层实现 | 按需读 [运行时索引](runtime/README.md)，再定位有关模块或测试 |

只加载当前任务所需参考文档。接口参数、输出解释与故障处理详见 [Agent 契约](references/agent-contract.md)。

## 运行约定

1. 先确定用户要求的证据层级。未提供历史数据时可以运行明确标注的合成示例，不能冒充历史实验。
2. `backtest` 要求明确的数据目录、日期区间、独立实验、成本和不可变代码 revision；输入只读，使用新的输出位置。数据格式和时间约束缺失时报告具体缺口，不降低门槛。
3. 默认 `--provider off`。仅在用户明确选择模型复核且提供材料时启用 `--provider codex`；该路径调用本地 Codex CLI 并可能发送材料到其配置的模型服务。
4. 除人类可读的 `--help` 外，每个统一脚本命令的 stdout 是一个 JSON envelope。先检查进程退出码与 `ok`，再读取 `evidence_level`、`data` 和业务状态。不要靠搜索输出里的 `PASS` 决定成功。
5. 报告实际运行的命令、关键输出路径、数据模式、覆盖模式、实验与限制。错误保留原始类型和原因；生成了文件也不能覆盖失败结论。

## 必须保留的解释边界

- 样例与内置测试是合成证据。`PASS_SYNTHETIC_ONLY` 只证明本地规则和合同校验，不证明真实历史收益。
- `historical_point_in_time` 与 `--code-revision` 是调用者声明。校验形状、时间、身份和 hash 不能独立认证数据供应商、数据版本或来源权利。
- 单份 coverage manifest 的 `point_in_time` 只支持一个交易日，且拒绝 M4；`retrospective_audit` 是事后覆盖审计，不能说成实时一致性。
- E2A/E2B 与 M2/M3/M4 是不同实验；不得混合资本后声称独立实验。保留成交假设、成本语义与回放限制。
- 默认 provider-off 的 `BLOCKED` 是未运行模型的阻断状态。状态查询不产生新分析；两次模型抽取一致也不等于事实真实。
- AI 仅可给已经合格的 E2B 候选增加否决，不能自动晋升 verified 事件、改变仓位和风控、解除锁存或下单。此包没有券商连接。
- 不将回放、指标、测试通过或文件完整性包装成盈利、OOS 稳健性、数据认证或生产就绪证明。
