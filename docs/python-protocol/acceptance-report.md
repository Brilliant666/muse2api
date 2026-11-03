# Python 协议集成验收报告

日期：2026-11-03，Asia/Shanghai。仓库 `Brilliant666/muse2api`，分支 `codex/python-protocol-integration`，继续使用 [Draft PR #1](https://github.com/Brilliant666/muse2api/pull/1)。

**结论：本机 Windows 原生源码环境达到单账号试运行门槛，九个预算内真实案例全部 PASS；尚未达到现网替换门槛。** 真实生成恰好使用 **6/6 文本、2/2 图片、1/1 视频**。没有重复执行生成，T5 断连后只恢复原操作。预算已耗尽，不再发送新的生成请求。

## 实际测试版本与账号范围

- 九个生成功能案例最后实号测试 commit：`b08f11000930674990c2834e1a9da091fd08a668`，tree `e80f1466d49ac12e6b368d65988b9db6d1ef7c8d`。九案之后仅增加零生成 VM 诊断工具、文档和脱敏证据，没有再修改业务、协议、依赖或部署配置。
- 最早运行的测试进程启动于 `129488a39417e616b709dff2becfc5c178d0357b`；T5 重启及 T6 使用 b08f11000930674990c2834e1a9da091fd08a668 的应用/配置版本。已核验这些版本间应用/协议/配置/引擎文件没有变化。后续增加的是验收客户端工具与文档。
- 原 64 项 Python（61 + 跨进程恢复 3）、6 项 JS、原有 25 个 Python 脚本/10 项管理页行为及两种镜像的原测试版本仍分别保留在旧证据中；未把旧测试改写为当前版本全量重跑。新增预算/SSE 8 项、断连 5 项合成用例也保留各自源文件摘要。
- 原应用基线 `1cd01682533dbf2c015fa879a8dc09534b50b4a3`；协议固定源 `its-benjamin/muse2api-http@6309f64b44ce133a2428fadf966a395d503ce84f`。真实 Git 准备、补丁输出与普通安装产物均经摘要校验，原 manifest 未改变，完整协议源码未提交。
- 受测环境 Python 3.12.16、curl_cffi 0.17.0、cryptography 50.0.2、Pillow 13.0.0、ffmpeg/ffprobe 9.0.2；浏览器可执行文件元数据版本 155.0.8059.85。完整版本和安装包摘要见 [实号环境](evidence/live-environment.json)。服务未使用测试替身。
- 用户后续明确授权复用此前选中的一个启用账号，并经独立私密文件交接。本轮本地数据和浏览器 profile 独立，Muse 账号来源是获授权复用的既有账号；不宣称它与现网使用完全独立。没有修改现网服务、配置或部署。
- 使用独立 worktree 和仓库外 `MUSE2API_HOME`、并发 1、单 worker、`127.0.0.1:18611`，Chrome 使用独立 profile/CDP 19211。原开发工作树保留，未覆盖已有工作或强制回退。

## 九个真实案例

| 案例 | 结果 | 实际证据 |
| --- | --- | --- |
| T1 Chat 普通 | PASS | HTTP 200，38 字符 assistant 内容，`finish_reason=stop` |
| T2 Chat SSE | PASS | 21 个事件，38 字符增量拼接，唯一 `stop`，末尾 `[DONE]` |
| T3 Responses 普通 | PASS | HTTP 200，`status=completed`，38 字符输出 |
| T4 Responses SSE | PASS | `response.created` 开始，8 个事件，`response.completed` 终结；增量与最终输出完全一致 |
| T5 断连/重启/恢复 | PASS | 真实内容出现且原操作仍 running 时断开，落盘 unknown/隔离；新进程恢复原 3359 字符、连续 60 项；account/session/node/VM 相同，原操作集合前后均为 8，`resubmitted:false` |
| T6 浏览器回退 | PASS | 先停止协议实例并确认退出；同一数据/Key/账号在实际隔离 Chrome 中通过会话检查与一次普通 Chat，38 字符及 stop；任务与真实媒体字节保留 |
| I1 异步图片 | PASS | 原创建/查询/下载流程，1600×1600 WebP，36,342 字节，Pillow verify + 完整 load |
| I2 双参考图编辑 | PASS | 原编辑接口上传顺序可区分的两张 64×64 PNG；结果 2240×1120 WebP，34,938 字节，完整解码；结果不等于上传预览，可观察到红色在蓝色左侧 |
| V1 短视频 | PASS | 原任务创建/查询/下载，1280×720 H.264，5 秒，479,523 字节；ffprobe 检查并由 ffmpeg 全部解码 120 帧及 1 条音轨到 null |

三份原始媒体 SHA-256：

| 案例 | SHA-256 |
| --- | --- |
| I1 | `b5b1a55c5c534e69d92a12823cbec4d8aac38a03e4af7103bfeb10fde12cf97b` |
| I2 | `0f2af7036b08caf3cdc221becc4da6ba78705b3409fd16d395be93eab0879c87` |
| V1 | `77e122d7c22c48d3c39d2540054c847db2e125413a2345bf73854871bc3b8003` |

[真实案例汇总](evidence/live-functional-validation.json)记录 SHA/tree、发送意图时间、请求与结果摘要；[案例账本](evidence/acceptance-cases.json)记录实际 6/2/1 消耗。入库关联 ID 是匿名别名及稳定指纹，原始 request/operation/task/session/node/VM ID、响应和真实媒体只保存在私密测试目录。每案只发一次客户端生成 POST，共 9 次；本地协议操作共 8 个，T6 浏览器后端不新增协议操作。没有独立抓取浏览器或上游每一个 wire 提交帧，不将客户端一次 POST 夸大为普遍的上游防重发证明。

## 恢复、Cookie 与资源

T5 在非空增量到达时核对磁盘 session/node 和 running 状态，再关闭本次客户端连接。随后看到 unknown、`delivery=accepted`、账号隔离及 active_operations=0。原有维护接口在 1.719 秒内成功取得生成槽位与账号维护租约，原操作仍 unknown；这是受保护 API 的行为观测，并非直接读取锁变量。更广的 generator/队列边界仍由受控测试支持。

旧进程退出后，新进程先确认原 unknown 和隔离均保留，再恢复原操作，得到完整 3359 字符/60 项而非断连前的 2 字符。独立审计逐值核对最终 node、account/session/VM 和完整 8 个操作 ID 集合一致，隔离解除。完成记录保留历史取消 error/code，但权威状态和 delivery 为 completed，管理页按 state 显示完成；未把历史字段当作恢复失败。见 [真实断连恢复](evidence/live-disconnect-recovery.json)。

真实会话检查观察到 `hatch_sess`、`hatch_vml` 值轮换，`hatch_gw` 新签发，三个 expiry 项写入 Store；报告仅保留匿名指纹。没有在重启后重新导入旧交接 Cookie；新进程从持久 Store 完成真实认证并恢复原结果。自动续签保留手工导入版本 1，手工换凭据后的旧回调保护仍属于受控证据。见 [Cookie 生命周期](evidence/live-cookie-lifecycle.json)。

实号阶段观察到单账号 runtime 复用；重启前后 runtime 内存状态重建。进程 TCP/RSS 快照单独记录，不把 HTTP/WS 总连接数当作 gateway 连接数，也不把历史重连量当作同时连接数；未独立测 wire 复用率或给出性能提升百分比。本轮顺序执行、并发 1，没有实测现网并发 3。见 [资源与清理](evidence/live-runtime-and-cleanup.json)。

## 构建、受控验收与已修复问题

前一轮已真实克隆固定协议源、安装 Windows 原生依赖，并完成 protocol/browser 两种 Docker 镜像构建及容器内部 HTTP、鉴权、模型、账号接口检查。默认协议镜像实际无 Chromium 二进制/进程；Windows 和 Linux Docker 内的 native curl HTTP/WS 合成对端各 6 项通过。受控 OS 进程恢复、媒体超限/超时、半成品清理、原文件交付恢复和身份不符拒绝分别保留证据。

已修复：

1. Windows `core.autocrlf=true` 改变 archive 原字节和 apply 输出：两步固定 EOL 策略，保留所有原 manifest 摘要校验；真实固定源及 2 项 Git 回归通过。
2. 实际 Chromium 容器退出后 8 个 PPid=1 僵尸：Compose 增加 `init: true`；同镜像同测试使用 init 后残留 0。直接 Docker CLI 同样需要 `--init`。
3. 验收入口的无用 sniffio 预检、Windows loopback socketpair 误拦截和依赖部署默认并发的测试前提：只修正测试环境前提，没有降低业务断言或改业务并发。

本次实号轮未再复现需要修改业务协议代码的缺陷。Docker 镜像源仍是 `beed898fb088209051ca2bbaa4112f8bc92d0eed`，Compose init 配置定向测试版本为 `b9228ccf3fce45a25b72b0d17bb5e77b4e1ede08`；这些与最后实号版本间的业务/协议/Dockerfile 内容一致，后续仅增加客户端验收工具和文档。精确版本边界不混用。

Docker 本机 Windows 命名管道/端口转发异常。此前通过临时官方 Linux CLI 访问已有 WSL daemon 完成独立 tag/container/volume 的构建和启动，未重启共享 Docker/WSL 或已有容器；容器内 HTTP 通过，Windows 发布端口的上一轮检查为 BLOCKED，本次实号轮未复测；未声称本机 `docker compose up` 成功。

- protocol 镜像：`sha256:4d3cb26b0173fa37ed4f59148fe051e83c29e42a1e37dc8c1d3729531cd48649`
- browser 镜像：`sha256:129a69891043e1a6f51d072baf080cfbd2dcd0a890bab138545514370fca46a6`

详细构建命令、日志摘要与哈希见 [Docker 构建证据](evidence/acceptance-docker-builds.json)。本轮真实生成运行在 Windows 源码服务，未在 Linux 容器中发送新的实号生成。

## 未覆盖项与交付状态

| 项目 | 状态 | 边界 |
| --- | --- | --- |
| 真实 VM 休眠/失活后唤醒 | NOT_OBSERVED | 后续约 30 分钟零生成观察四次均为原 VM 的 RUNNING，未捕获休眠前态；未执行 wake/Noise/history，见补验记录 |
| Linux Docker 真实 Muse 接口/媒体 | NOT_RUN | 镜像/启动/native 合成 I/O 已过，实号仅 Windows；现有生成预算已用满 |
| 实号多账号、指定代理 | NOT_RUN | 本轮只有一个获授权复用账号，没有指定测试代理；受控隔离测试单列 |
| 真实并发 3/目标部署负载 | NOT_RUN | 测试并发 1、单 worker；现网 3 是未修改的部署参数 |
| 实号 delivery_failed 原文件恢复 | NOT_OBSERVED | 三次真实交付均成功；受控 native/跨进程原文件恢复已通过 |
| Windows Docker 发布端口 | BLOCKED（上轮） | 容器内部正常；实号轮未复测本机 Desktop 转发 |

这不阻塞当前 Windows 源码环境的单账号试运行结论，但不能给出现网替换通过。替换前仍需真实 VM 生命周期及目标环境、所需并发/代理/多账号证据；若需要新增生成，必须另定预算，不能清空本轮账本重发。

九案验收结束时，服务、启动器、独立 Chrome 进程树均已停止，18611/19211 端口释放；其他浏览器与现网服务未终止或变更。私密测试账号、响应、生成媒体和恢复记录留在仓库外，Git 中只有脱敏元数据。Docker 的独立镜像和已停止测试容器/合成数据卷保留，不清理共享资源。

结果与续跑入口：[机器结果](acceptance-results.json)、[RESUME.md](RESUME.md)。当前九案全部完成，不再缺少账号输入。本 PR 保持 Draft，不合并、不迁移或切换生产。

## 2026-11-03：零生成 VM 自然空闲补验

**结果：NOT_OBSERVED。** 本次只补验 VM 生命周期，不重跑九案；新增生成预算和消耗均为 0/0/0。窗口为北京时间 2026-11-03 15:55:43 至 2026-11-03 16:25:46，计划上限 1800 秒，末次请求回包后实耗 1801.75 秒。初始及约第 10、20、30 分钟四次真实 session_info 均返回原 VM 的 assigned/RUNNING。这是四个采样点的状态，不证明间隔内持续运行，也未排除其他客户端活动。

执行前确认原私密根、原 ledger 和原 T5 实际 ID 均在，九案完成且已消耗 6/2/1；未使用 Git 中匿名操作别名。隔离服务无活动操作、运行时连接为 0，三个任务及八个操作已完成，近期无配置写入，后台保活当前未执行。为避免其每 900 秒保活干扰，先停止并确认仅本次服务退出，再由独立 Backend 独占同一 Store；未导入 app，也未启动 SDK AccountPool。观察只调用不含 bootstrap/wake 的真实 GET /api/session?vm_state=live，期间不建 Noise 连接。

未捕获明确休眠前态，因此 **bootstrap(force=True)、wake、Noise 和原会话 chat.history 均未执行，不能计为通过**。没有使用 completed recover、本地缓存/媒体、缺失状态或本地进程停止替代远端证据，也没有修改已完成操作状态。没有停止生产、其他客户端、共享 Docker 或远端 VM。

结束后原 ledger 字节摘要不变，九个生成意图、八个实际操作 ID 及其状态/文件、三个任务及其文件均不变；四条诊断 HTTP 请求全为 session GET，POST、生成、审批及网关请求均为 0。hatch_sess、hatch_vml、hatch_gw 在本次查询中轮换，expiry 增加已写盘，手工凭据版本保持 3，未重新导入旧 Cookie。诊断进程已退出、锁释放，只关闭本次诊断连接。随后恢复原协议管理页 http://127.0.0.1:18611/admin；页面、backend、accounts 健康检查均为 200，协议 running、无浏览器、无活动操作，原一个启用账号保留。

业务源码与九案版本 b08f11000930674990c2834e1a9da091fd08a668 相同；诊断实际版本 `b2f600e8a40a94280bbdb5f787f442c0c6d70f97`，工具内容 SHA-256 `552c94f27d3e642af9e1d7b59b31007f689aca8ac977da2f79ae41cec3dd0420`。本次只做工具语法、状态排除及原消息匹配的定向检查和独立审查，没有重跑旧回归或九个功能案例。详见 [脱敏补验证据](evidence/live-vm-wakeup.json)。结论仍仅为本机 Windows 源码单账号试运行；Linux 容器实号、真实并发 3、多账号/代理和真实 VM 唤醒仍未覆盖。
