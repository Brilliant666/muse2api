# 本轮收尾与续接

本轮已按用户要求收尾，**不自动继续发送**。第二阶段总预算仍为 560 文本、35 图片含编辑、5 视频；累计 18/5/1，剩余 542/30/4。第一阶段原 6/2/1 独立保留。

[最终报告](phase2-resumed-report.md) · [机器结果](phase2-resumed-results.json)

## 当前状态

- 本轮测试容器、诊断连接、自有 Chrome 均已停止；停止容器和镜像保留。
- 原 18611 实例保留。18613 登录管理页已恢复，仅登录/配置，生成入口与自动保活关闭。
- 九个账号最新 Cookie/expiry 经 revision CAS 同步回登录管理 Store。未复制测试操作、任务或 quarantine；不重新导入旧 Cookie。
- 最新业务修复为 `21ed2cd7913e527de8561b358182c7ac2432c95c`；最后测试工具为 `90da5530fdb28259bd34698a18b80e38fc78b35e`。最终 SDK chat SHA 为 `ea7356e5987bdacbda75fb38253bcc76395f273a0df2920f894259d9218d21ab`。
- 最终修复标准回归为 79 Python + 6 JS，通过且无 skip；最终 SDK 新实号生成为 0，不能按回归或 build/hash 宣称实号修复通过。
- 真实 C3/C5/两小时批次、D01 断连注入均未开始。D11～D15 postcheck 仅有计划，没有意图或发送记录。十账号门槛仍 BLOCKED。

本机私密绝对路径保存在工作树内被忽略的 `.env.acceptance-private` 指针文件，只有路径，没有 Cookie 或服务 Key。不要把它、实际 `.env`、账号、raw history 或原始 ID 提交 Git。公开报告用 `<PRIVATE_ROOT>` 等占位符。

## 账本与已完成案例

`<ORIGINAL_PHASE2_ROOT>/phase2-ledger.json` 必须仍为 SHA `7b59725580ae290ad30809f6f7af827235d28f7af31e9be14db299faaf52875b`。其旁 authority 绑定本轮唯一 `<PRIVATE_ROOT>/resumed/phase2-resume-ledger.jsonl`；Docker 内原路径固定为 `/phase2-original/phase2-ledger.json` 和 `/phase2-private/resumed/`。不能换空目录、删锁、删 authority 或重建账本绕过预算。遇到残留锁先查原进程与现场，不自动重发。

20 个新意图为 L2-001～011、D02-001～D10-001。原始工具 19 PASS/1 FAIL，初次业务 18 PASS/2 FAIL：

- L2-011 原图片误判失败已通过原操作恢复媒体，新增生成 0；原 FAIL 与初始 Store/ledger 快照保留。
- D10-001 初始 API 的 completed 缓存仍是错误的 commentary。后置原 Noise history 已读到同原 turn 的正式 marker，未修改 completed 状态来触发恢复。**不要拿 completed recover 缓存作为远端读取证据。**

生成只允许在新的明确继续指示后、按未使用编号和有界计划执行。失败和超时同样占预算；unknown 只恢复原 account/session/VM/operation，不换号重新生成。

## 重新准备与检查

旧忽略目录 `.protocol/` 故意保留，包含旧摘要，已不匹配新 manifest。不要安装它覆盖当前修复。现有已校验的新 SDK 源在指针文件中的 `PREPARED_PROTOCOL_DIR`；或为重新准备选择一个不存在的新目录：

```bash
python tools/prepare_protocol.py --clone --destination <NEW_PREPARED_DIR>
python -m pip install --no-deps <NEW_PREPARED_DIR>
python tools/test_protocol.py --protocol-source <NEW_PREPARED_DIR> --report <PRIVATE_REPORT_PATH>
python -m unittest discover -s tests -p 'test_phase2_resume.py' -v
```

Windows 使用本工作树 `.venv/Scripts/python.exe`。准备器固定 Git `6309f64b44ce133a2428fadf966a395d503ce84f`，校验 patch/全部输出；新输出目录不能覆盖旧目录。

最终实际 Linux codec 为 `sha256:4bd98aee11a0d34882f809b7e0a72599ea4026a272a0563445e002e1a5bf3bab`。该镜像业务/SDK 绑定 21ed；工具只读挂 `/app/tools`，凭据/数据在外部私密挂载。不要重启共享 Docker、清理旧容器或复用生产数据。原生 WebSocket fallback 显式关闭。

## 后续实号的前置条件

先核对当前登录页有没有用户配置、登录任务或正在使用的同账号客户端；必要时按“只读核对→暂停本测试实例→确认退出→独占检查→启动→健康检查”分步处理。两进程不能持有同一 Store。登录管理 Store 与本轮测试 Store 是不同目录；恢复测试前核对哪份凭据最新，不能让旧测试快照覆盖后来的登录更新。

全局并发 3 应为三个不同账号各一条在途请求。SDK 已修复无媒体/显式 commentary 的过早完成；最终用户 message 边界仍不应被宣传为所有后台 agent 的绝对 CPU idle 证明。可选 `--session-probe-marker PHASE2_D11_001` 仅为已有连接的有界只读观察，默认关闭、未实号运行，不作为自动调度输入。

只有真实图片、编辑、视频已完整解码且新修复 postcheck 通过后再考虑混合 C3；C3R 与 C3TEXT 共用预算槽，SOAKR/SOAK9/SOAK9TEXT 共用槽，不能重复领取。原 C5/SOAKR 坚持十个真实独立身份，九账号结果只能标部分验收。检测到严重重复缺陷、unknown、数据/归属错误、双生成、封禁或明显限流即停当前批次，保留现场。

继续更新原 Draft PR #1，不新增、不合并、不部署生产。
