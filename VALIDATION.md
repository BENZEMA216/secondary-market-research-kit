# 本地验收记录

## 0.2.0

日期：2026-09-09。本地环境：macOS，Python 3.11.14，具备 America/New_York 时区数据；金融与报告依赖在项目独立虚拟环境中安装。

| 验证层 | 结果与范围 |
|---|---|
| 分发完整性 | 仓库、三个 Skill 和各自 runtime 的清单一致；冻结运行时分别为 Scenario Router 49 文件、equity 36 文件、market 21 文件 |
| 原策略引擎 | 193 项测试通过，0 failure / 0 error；`PASS_SYNTHETIC_ONLY` 和 `PASS_LOCAL_RUNTIME_CHECKS` |
| Agent 与安装接口 | 70 项封装及分享安装测试通过；涵盖参数、权限、错误、依赖、脱敏、完整性、目录防覆盖与统一派发 |
| 金融工具目录 | equity 71 项；market 49 项可派发、4 项受限；接口数量不等于真实外部调用验收数量 |
| 新增模型路径 | 原增强文字五个模型方法与 Agent Manager 八种结构化章节由 mock 覆盖；截断、拒绝、空输出、无材料、凭证和 tracing 边界通过测试，没有真实模型调用 |
| 离线研报流水线 | 合成财务输入生成 CSV、估值/敏感性结果、PNG 图表、HTML 和 PDF，共 20 文件；`SYNTHETIC_FIXTURE_ONLY`，网络和模型关闭 |
| 项目安装 | 三个 Skill 可一次安装到新项目；Codex/Claude 项目目录、中文和空格路径、独立包执行及冲突前置检查由测试覆盖 |
| 受限来源能力 | 原 SEC/Marker/RAG 复合路径在注册表明确阻断并提供替代入口；没有把注册记录当作原管线已跑通 |

金融依赖锁定文件只按 Python 3.11 验收，`pip check` 通过。没有相应依赖的标准库环境会明确跳过 12 项重依赖测试；因此这类运行不能代替装齐依赖后的完整测试。

从源码仓库根目录复现：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r skills/equity-research-toolkit/requirements.lock.txt
.venv/bin/python -m pip check
.venv/bin/python scripts/share.py verify
.venv/bin/python scripts/toolkit.py doctor equity-research-toolkit
.venv/bin/python scripts/toolkit.py run scenario-router-research -- validate
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/toolkit.py run equity-research-toolkit -- demo --output /path/to/new-demo-output --pdf
.venv/bin/python scripts/share.py build --output dist/0.2.0
```

将演示输出路径换成尚不存在的新目录。此记录证明源文件封装、接口行为和被执行的离线流程；不包含真实供应商 API、模型服务、真实行情回测、PIT 完整性、收益/OOS、原 WebUI、客户端 UI 自动发现或券商下单验收。外部浏览器依赖没有复制进安装包。

GitHub Actions 包含 Linux Python 3.11/3.12/3.13 基础校验，以及 Python 3.11 完整金融依赖测试和合成 PDF 演示。远端结论以具体提交的 Actions 记录为准；本地成功不预先代表远端成功。

## 0.1.0 历史记录

日期：2026-09-09。环境：macOS，Python 3.11.14，具备 America/New_York 时区数据。

| 验证层 | 结果 |
|---|---|
| 原引擎来源快照 | 原清单 49 个文件的 SHA-256 全部一致，未修改原引擎 |
| 原引擎测试 | 193 项通过，0 failure / 0 error，`PASS_SYNTHETIC_ONLY` |
| 原运行时检查 | `PASS_LOCAL_RUNTIME_CHECKS`，包含本地 paper 与 provider-off 路径 |
| Agent CLI 集成测试 | 14 项通过 |
| 分享与项目安装测试 | 8 项通过 |
| 可搬移性 | 从含中文/空格的路径、无关 cwd 调用；独立 ZIP 解压后及新项目安装后 doctor / signal demo 通过 |
| 历史回放接口 | 九文件合成夹具、两组滑点参数回放成功，输出重名时保留原字节 |
| 边界错误 | JSON 参数错误、超时、旧 revision、缺失数据库、provider-off、篡改清单、软链接、已有输出均按合同处理 |

复现命令，从源码仓库根目录运行：

```bash
python3.11 scripts/share.py verify
python3.11 skills/scenario-router-research/scripts/research.py doctor
python3.11 skills/scenario-router-research/scripts/research.py validate
python3.11 -m unittest discover -s tests -v
python3.11 scripts/share.py build --output dist
```

validate 在临时副本内运行原校验，返回报告 JSON 后清理副本。报告内临时绝对路径仅为诊断上下文，不是持久下载地址；本记录不包含本机路径、原始材料、模型响应或运行数据库。

上述证明覆盖代码、离线运行及分享安装，不包含真实行情回测、供应商或 PIT 完整性认证、真实模型服务调用、收益/OOS验证、券商下单或客户端 UI 自动发现 Skill 的实测。GitHub Actions 配置覆盖 Linux Python 3.11/3.12/3.13；具体远端结果以对应提交的 Actions 记录为准。
