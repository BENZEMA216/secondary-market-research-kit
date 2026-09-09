# 0.1.0 本地验收记录

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
