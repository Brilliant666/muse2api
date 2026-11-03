# Python 协议后端第二阶段验收（2026-11-03）

**结论：未达到 Linux 多账号试运行或受控预发布门槛。** 实际 Linux Docker 的 4 次实号请求中，Chat 普通、Chat SSE、Responses 普通各 1 次通过；第 4 次图片请求上游完成但没有媒体，保留 FAIL。L1 计划 20 次，实际 4 次，批次为 INCOMPLETE。用户随后明确要求“保持全部实号停发，交付当前结果”，因此不再发送剩余 L1、三账号 C3P 或其他实号请求。

本轮使用三个获授权且身份不同的账号；真实并发始终为 1。原任务的至少 5/10 账号门槛未满足，没有实测并发 3/5 或两小时稳定性。受控测试复现并修复了账号预选与租约分离造成的队头等待、握手失败连接清理、JSON Store 崩溃隔离顺序，以及显式 429 被 SDK 路径吞掉后继续执行的问题。没有依据修改账号池架构、媒体提取器、后端语言或生产参数。

[机器结果](phase2-results.json) · [环境与版本](evidence/phase2-environment.json) · [实号四案](evidence/phase2-live-results.json) · [性能与资源](evidence/phase2-resources.json) · [受控验证](evidence/phase2-controlled.json) · [历史保全与清理](evidence/phase2-preservation.json)

## 范围、账号与预算

起点为 `4c93645fe0b9cd6ca5b12ed766703f21b5f321af`，继续 `Brilliant666/muse2api` 的现有 Draft PR #1；只推送自己的 PR 分支，不合并、不部署、不修改生产配置、数据或凭据。原管理实例 `localhost:18611` 保留；本轮独立 worktree、venv、仓库外数据根、Store、协议状态及端口 18612。管理实例未复用本轮数据，单一 Store 只有一个运行服务或诊断持有者。

用户通过 IIJ 对话明确授权复用三个既有上游账号。本地测试状态隔离，但不能声称账号与外部/生产使用完全独立，未排除其他客户端活动。三个身份分别由真实登录页的 `isAuthenticated=true`、`viewerId == prelude.user.identifier` 核验，不以 Cookie、VM 或重复导入个数冒充身份数。Git 只保存本轮私密盐的 HMAC 指纹及 `test-1..3` 别名。测试前实时额度显示周额度已用 27%、2%、1%，剩余 73%、98%、99%；额外额度仅保留“实时 UI 已显示”的边界，不用旧缓存替代。身份/额度预检零生成，Cookie 正常写入本轮 Store。

| 批次 | 原定规模与上限（文本/图片含编辑/视频） | 最终状态 |
| --- | --- | --- |
| L1 Linux 功能 | 20 = 15/4/1，单 worker、并发 1 | FAIL，INCOMPLETE：发送 4，3 PASS / 1 FAIL，16 NOT_RUN |
| C3 原门槛 | 120 = 111/8/1，并发 3、至少 5 账号 | BLOCKED：仅有 3 个身份，没有开始 |
| C3P 用户授权部分验收 | 三账号并发 3，共享 C3 的 120 = 111/8/1 预算槽 | NOT_RUN：从未开始；用户要求继续停发 |
| C5 容量探索 | 120 = 111/8/1，并发 5、10 账号 | BLOCKED：身份数不足，没有开始 |
| SOAK | 最多 300 = 288/11/1，约 2 小时、并发 3、10 账号 | BLOCKED / NOT_RUN：身份不足且用户停发 |

总授权上限为 560 = **525 文本、31 图片/编辑、4 视频**，本轮实际消耗 **3/1/0**，账面剩余 **522/30/4**。失败和超时同样计费；剩余额度不是继续发送的授权。没有把媒体预算换成文本，没有用空目录、重置账本或替换批次绕过停止要求。C3P 与 C3 互斥共用一个预算槽，不能重复领取。

第一阶段原九案与 **6/2/1** 账本保持不变。本轮未重跑九案，也未改写旧报告、机器结果、RESUME 或历史证据；原账本/八个操作/三个任务的字节摘要均与本轮前快照一致。

## 实际 Linux 构建、启动与四案

重新真实构建 source 与 codec 镜像，分别用时 35.532 秒、56.156 秒，固定协议 Git 源 `its-benjamin/muse2api-http@6309f64b44ce133a2428fadf966a395d503ce84f`，实际安装原生 curl 依赖。最终镜像在 codec 层上仅覆盖已修复的 `protocol_backend.py` 并编译检查；工具只读挂载到 `/app/tools`，凭据与数据位于仓库外私密挂载目录，镜像不含账号。

真实环境为 Linux x86_64 / WSL2、Docker 24.0.5、Python 3.12.16、curl_cffi 0.17.0、cryptography 50.0.2、FastAPI 0.162.0、Uvicorn 0.54.0、Pillow 13.0.0、ffmpeg/ffprobe 5.1.9。通过临时官方 Linux CLI 访问已有 daemon，未重启 Docker/WSL 或共享容器；客户端在同一 Linux 容器内访问 loopback，未把 Windows 发布端口或 `docker compose up` 写成通过。受测后端为实际 protocol，browser_running=false，单 worker、并发 1。

| 案例 | 账号 | 结果 | 证据与耗时 |
| --- | --- | --- | --- |
| L1-001 Chat 普通 | test-1 | PASS | HTTP 200，13 字符，12.827 秒 |
| L1-002 Chat SSE | test-2 | PASS | HTTP 200，流式拼接与原 operation 内容一致，13 字符，9.366 秒 |
| L1-003 Responses 普通 | test-3 | PASS | HTTP 200，completed，13 字符，10.363 秒 |
| L1-004 图片 | test-1 | FAIL | 创建 HTTP 202，最终 task/operation 失败，`media_missing`，26.696 秒 |

四案分别预扣一次、各发一个生成 POST，原 prompt marker、account、operation、session、VM 与返回归属逐案一致。前三案结果与各自原 operation 的文本相同；不存在拿其他账号或历史结果替代本案的证据。实际账号分布为 2/1/1，成功数为 1/1/1；样本太少，不将它作为稳定成功率估计。

L1-004 的 operation 为 `state=failed, delivery=completed`：上游本轮回复已完成，但未产生媒体，不能将完成传输解释为成功图片交付。停止测试服务并确认退出后，以最新 Linux Store Cookie、原账号/会话/VM，由一次性 Backend 独占 Store 建立真实 Noise 连接，直接 **GET 原 chat.history**：HTTP 200，仅原 user 与 assistant 两个事件，user prompt 逐字相同，assistant 171 字符，媒体引用/文件/下载 URL 均为 0。该 operation 的 `message_id` 本来为 null，不能把摘要中的“未找到该 ID”当成错会话证据。

本案客观分类为 **UPSTREAM_COMPLETED_WITHOUT_MEDIA**。没有额度耗尽、429、订阅限制或可提取媒体的证据；不能据此推断所有账号永久不能生成图片，也没有依据修媒体提取器。应用已经附加明确的从零绘图要求，没有发现明显的 text-only 测试指令错误。诊断没有调用生成、审批或 recover，没有改完成状态，也没有用缓存/本地媒体冒充远端证据；前后 ledger、operation、task 文件摘要一致。

剩余 Responses SSE、图片、参考图编辑、视频均 NOT_RUN；没有本轮真实媒体文件，字节数、SHA-256 和完整媒体解码验收 **不能计为 PASS**。第一阶段 Windows 媒体成功和受控合成媒体仅保留各自历史/证据层级。

## 可复现缺陷及最小修复

| 问题 | 复现与修复 | 验证边界 |
| --- | --- | --- |
| 忙碌预选账号占住队列，其他空闲账号未利用 | 原四案均红；把实际选择与租约获取放在 worker 调度时原子完成，回调记录真正使用的账号。原四案及扩展六案绿 | 受控配置并发 3，实际重叠峰值 2；不等于 Muse 实号并发 3 |
| 握手创建连接后 Cookie 写盘异常造成遗留连接 | 三案中一案红；通用异常路径在原有重试前关闭已创建连接，三案绿 | stale credential 分支仍不重试；不是十账号长期泄漏验收 |
| JSON Store 终态/账号隔离跨文件落盘顺序 | 真实子进程在两次写之间 `os._exit(87)`，初六案五红；先落终态再解除隔离，启动按原操作协调隔离，最终七案及旧 Store 四案绿 | 保留其他 unknown 的隔离；不覆盖任意损坏 JSON、断电 fsync 或多进程共享 Store |
| SDK 吞掉显式 429 后继续后续路径 | 保留有类型的 upstream_status，在原连接 request/ack 边界阻断；11 项通过 | ACK 429 红版是一提交+一 history，不夸大为双生成；注册 429 由仍提交一次变为零提交；history 429 一读即停；媒体仍最多两次原文件下载，不重新生成 |
| 验收客户端可能恢复未经验证的返回 ID | 加强 prompt/operation/account/task/媒体归属门槛和 first-error stop，22 项通过 | 原 ID 不明时拒绝 recover，不把未知结果换账号重发 |
| Linux SIGTERM 丢失 metrics 尾回执 | 空 Store、无网络的实际 Linux 子进程由 exit -15/无尾改为 exit 0，`server_exit → metrics_summary` 与 writer drain 完成 | SIGKILL 不保证尾回执，未宣称强杀或负载退出通过 |

业务修复集中在 app.py、protocol_backend.py、store.py。没有重写账号池、迁移 Store、改变后端语言、增加未经证明必要的 idle TTL 或改生产并发。429 修复只识别明确状态；不能保证所有上游错误格式均已分类。原生媒体下载的原文件只读重试与生成重新提交明确分开。

## 受控故障与原生传输证据

最终业务源上，原协议套件 **64 Python + 6 JS** 全通过、无 skip；其中 curl import 未替换，但网络对端仍是合成，不能写成真实 Muse 或原生网络验收。最终组合回归 **16/16**：scheduler 6、fault isolation 6、connection cleanup 3、media truncation 1，外部 socket/DNS 阻断。SDK status 11、批次安全 22、身份解析 8、采集契约 6 也分别保留各自结果与文件摘要；不同轮次有重叠，不相加伪称独立覆盖数。

| 故障 | 已完成证据 | Linux 真实 Muse 本轮 |
| --- | --- | --- |
| 发送前失败 | 受控 A 提交 0、健康 B 完成 1 | NOT_RUN |
| 401/403/429/额度不足 | 受控错误与 A 隔离，B 完成；已接受后未知操作不重发 | NOT_RUN，未主动耗尽真实额度 |
| 代理失效 | Windows、Linux 各 1 项真实 curl + 本地 HTTP 对端；坏代理 A 不触达目标，健康 B session GET/Cookie 回写 | 真实外部代理 NOT_RUN；本地原生测试无 Noise/生成 |
| Cookie 换新/旧回调 | 受控 credential revision CAS；本轮 Linux 三账号串行真实 Cookie 变化并持久化 | 串行轮换 PASS，并发覆盖/人工换 Cookie 后竞态 NOT_RUN |
| 发送后断连、取消、进程重启 | 原 64 套件及新 Store 进程崩溃协调，保留原操作 | 新 Linux 实号故障 NOT_RUN；第一阶段 Windows T5 是历史证据 |
| VM 不匹配 | 旧离线恢复套件拒绝错 VM | NOT_RUN |
| 媒体截断与原文件恢复 | 安装的 SDK 真正写入临时文件 36/73 字节，受控网关中断；无半成品公开/残留，健康 B 完成；恢复原 73 字节文件且新增生成 0 | 原生媒体截断、真实媒体恢复 NOT_RUN |
| 真实 VM 休眠后 wake | 第一阶段自然空闲观察维持 NOT_OBSERVED | 本轮未新观察或唤醒 |

截断测试使用受控迭代器与实际 SDK 文件 I/O，不是 native socket/Noise/Muse 媒体传输。原生代理 Windows/Linux 测试执行于 e878 等价业务快照、codec 镜像（显式状态修复 39 之前），未在最终 39 重跑，不借用最终业务 SHA。原生代理测试使用新合成 Cookie、隔离临时 Store、Linux `--network none`（仅 loopback），不是生产代理证明。初次 mount 前置失败（exit 125）与后续实际测试记录分别保留，未将前置失败抹掉或计入真实生成预算。

## 性能、资源与连接生命周期

完整 L1-r2 metrics 共 456 行（455 条事件加最终 summary），SHA-256 `0ee7a2d1d64eb89ed61e8313ed826c5bc33ae8b117adadad72effd94f7adc155`；dropped、writer_errors、hook_errors 均为 0。窗口约 666.86 秒，包含请求前后空闲采样，只有 59.425 秒的四案串行批次，**不是两小时稳定性测试**。

- 三次真实 Noise 握手；三个 Runtime/活动连接/receiver 峰值均为 3，test-1 的同一连接用于两案。采样 active operation 峰值与客户端在途峰值均为 1。原 history 诊断另建一次连接，发生于服务停止后，不混入服务三次握手计数。
- RSS 从 74,739,712 到 89,821,184 字节（峰值同末值）；FD 从 19 到 31，峰值 32；进程累计 CPU 由 0.43 到 3.40 秒。TCP PID 所有连接峰值 8，包含 HTTP/控制/网关，不能把它当网关连接数。三账号短窗口的缓存增长不证明十账号泄漏或无泄漏。
- 成功文本延迟 P50 10.363 秒，P95（nearest-rank）12.827 秒，n=3；四案含失败的 P50 11.595 秒、P95 26.696 秒，n=4。仅描述本样本，不作容量结论。
- 实际 capacity 获取 4 次，约 0.0061–0.0087 ms；account lease 获取约 10.002–14.730 ms。客户端记录的 0.015/12.882/22.288/32.694 秒是从批次开始的排程延迟，不能当服务端队列等待。
- Store 实际写入 40 次，无错误，P50 9.780 ms、P95 12.608 ms，范围 7.454–13.111 ms；这是实际 JSON 写/replace 延迟，受宿主挂载影响，不是高负载或断电持久性证明。
- 仪表记录 4 次生成写入开始、4 次本地 send/flush 完成，逐原 operation 各一次，没有 unclassified POST。它证明本地提交路径计数，**不等于独立抓获 provider 精确接受次数**。

十账号资源生命周期、长期饥饿/公平性、并发 Cookie 覆盖、持续吞吐及资源回落仍无实号证据。没有仅因 Runtime 留存就增加 idle 回收；已修复的是有红绿证据的异常连接清理。

## 版本、测试 SHA 与重现入口

| 版本 | 用途 |
| --- | --- |
| `4c93645fe0b9cd6ca5b12ed766703f21b5f321af` | 本阶段起点，第一阶段证据锚点 |
| `e878e5ce94fc47e1242b15800e08322a327cb72e` | 调度、连接异常清理、Store 崩溃协调修复 |
| `bbc098c28cc109cfb65c1b75ef8bd4809834ee18` | 初版有界 Linux 多账号验收工具与定向回归 |
| `39e1489a6561878f727d961a5cc6e07f53b9cbb3` | **最终业务源码测试 SHA**：显式状态/429 修复，最终 64+6、16 组合与 Linux 实号核心 |
| `a5551b2dbb3d0a7c016764b28fdd86123cd2881d` | **最终实号验收工具 SHA**，tree `9ed8182cee216a42de30cb52774ff932be8a2c7d`：22 项安全门槛、SIGTERM 回执；L1 四案使用此版本工具 |

后续报告交付提交只增加本阶段文档和脱敏证据，不再改业务或重跑请求。最初 source/codec 构建取自当时尚未提交的三修复快照，base 标签为 4c，后与 e878 的业务文件等价；不是最初就构建于 39。受测镜像源并不单靠最初 base 标签认定：保存实际源码/工具 SHA-256 与镜像运行时逐字节比对，Git 的 CRLF 工作树与 LF blob 分别记录；EOL 归一后与上述提交一致。早期红测运行于未提交快照，按当时文件摘要/日志描述；未留存的初六案 Store harness 摘要明确为 null，不借用最终七案摘要伪造早期版本。

镜像标识：

- source：`sha256:495bb650faa1b4651a297daa1b6f4bcd90b59a41e6e7bb83d7c52ac59b4a82ef`
- codec：`sha256:4e3f7216a59f0b28432261154490350bb5bd35a9cc6e4fd4371e493bf9983cd0`
- 最终实号镜像：`sha256:9d5ed8a6018c78582e1eb5e8a2c64d84dfe11e3f04deb42a673301634d61dd5f`

下列是本轮配置和命令结构记录，**不是继续发送许可**。`PRIVATE_ROOT` 必须指原第二阶段私密根，不能指18611、生产目录或新建空账本；Cookie、API Key、真实关联 ID 和原始响应不写入命令或 Git。

```dotenv
MUSE_BACKEND=protocol
MUSE2API_HOME=/phase2-private/linux-home
MUSE2API_CONCURRENCY=1
MUSE2API_HOST=127.0.0.1
MUSE2API_PORT=18612
PYTHONPATH=/app:/app/tools
# MUSE2API_KEY 仅在仓库外 linux.env；账号仅在独立 Store
```

```sh
# 历史构建结构；本轮最终镜像另以 codec 为父层覆盖最终 protocol_backend.py
# 构建、安装、服务启动及验收分别执行，未重启共享 daemon。
docker build --no-cache -t muse2api-phase2:20261103-source -f Dockerfile .
docker build --no-cache --build-arg PHASE2_SOURCE_IMAGE=muse2api-phase2:20261103-source -t muse2api-phase2:20261103-protocol -f deploy/phase2/Dockerfile .
# 独立服务启动结构：不发布 Windows 端口；挂载只读工具和本轮私密数据。
docker run --init --name muse2api-phase2-linux-20261103-L1-r2 --env-file "$PRIVATE_ROOT/linux.env" --mount "type=bind,src=$PRIVATE_ROOT,dst=/phase2-private" --mount "type=bind,src=$WORKTREE/tools,dst=/app/tools,readonly" muse2api-phase2:20261103-rate-limit python /app/tools/phase2_server.py --host 127.0.0.1 --port 18612 --workers 1 --sample-seconds 5 --metrics /phase2-private/L1-r2-metrics.jsonl
# 以下 run 是已经执行的 L1，不得再运行；账本也会拒绝同批重复开始。
docker exec muse2api-phase2-linux-20261103-L1-r2 python /app/tools/phase2_batch.py run --private-root /phase2-private --round L1 --base http://127.0.0.1:18612 --preflight /phase2-private/preflight.json --operations-file /phase2-private/linux-home/data/protocol_operations.json --ffprobe /usr/bin/ffprobe --ffmpeg /usr/bin/ffmpeg
```

Windows/WSL 的 client 路径与 daemon bind 路径不同，实际通过官方 Linux CLI 使用 daemon 可见的 `/run/desktop/mnt/host/...` 挂载；早期 `/proc/.../mnt/host/...` mount 前置错误留在私密记录。原 root Dockerfile 和 .dockerignore 未变，验收扩展配置为 `deploy/phase2/Dockerfile`。

本轮离线回归入口与环境结构如下，各项对应结果见机器证据，不要求收尾重复运行：

```sh
python tools/test_protocol.py --protocol-source .protocol --report "$PRIVATE_ROOT/controlled-tests/protocol-final.json"
# 16 组合等定向回归使用 venv 普通安装的 SDK，PYTHONPATH 只含仓库、tests、tests/protocol，不能加入 .protocol：
python -m unittest test_phase2_scheduler test_phase2_fault_isolation test_protocol_transport_cleanup test_media_truncation_recovery
python -m unittest test_store_crash_consistency test_phase2_error_status test_phase2_batch test_phase2_identity test_phase2_server
# 原生 loopback/OS 测试必须另在隔离环境，Linux shutdown 必须真实 Linux，空 Store、无实号凭据：
python tests/test_phase2_native_proxy.py
python tests/test_phase2_server_shutdown.py
```

预检驱动为 `tools/phase2_account_preflight.py WORKTREE PRIVATE_ROOT`，要求本轮独立 CDP 19212、显式 browser env、account-map 与私密 HMAC 盐。该工具不是通用账号导入器，当前不再执行。原 history 诊断是一次性只读驱动：原 case 取实际 ID → 同一 Backend 最新 Store Cookie → ensure 原 runtime/真实 Noise → `GET chat.history` → 核对原 prompt/归属 → 关闭连接；没有额外封装生成/审批或 completed recover。

## 保全、最终判断与后续边界

L1-r2 实例已正常退出，exit 0，完整尾回执保留。早期零生成 L1 服务 exit 143、尾回执不完整的历史仍保留，未回填或冒充完整实号 metrics。一次性原 history 诊断已退出并关闭本轮连接；独立 Chrome profile 进程树已停止。原 `localhost:18611/admin` 收尾 HTTP 200，未停止或重配。独立停止容器、镜像、私密账本和响应保留；没有清理共享资源或重新导入旧 Cookie。

三个账号 Linux 最新 Store 的 Cookie 均有三项值发生变化，credential revision 保持，expiry 已持久化。初始 Windows 预检 Store 只用于本轮首次 seed，Linux 开始后没有拿旧 Cookie 覆盖它。原图片诊断前后生成意图/操作/任务集合与文件摘要均未变；本轮只有四个意图、四个操作（3 completed / 1 failed）、一个失败图片任务，没有 unknown 操作待自动重发。

1. **Python 同进程能否支撑目标并发 3？尚未建立实证。** 本次证明了真实 Linux 原生链路的三账号串行基本文本，受控调度并发通过，但不能据此回答真实目标负载稳定性。
2. **账号池调度和连接生命周期是否需要修正？需要，已完成有复现依据的最小修复。** 调度等待、异常连接与崩溃隔离已修；十账号公平性、长期空闲资源和并发 Cookie 仍未验收，没有证据支持全面重写。
3. **是否迁移 JSON Store？当前保留。** 修复写入顺序及启动协调后，无测量证据要求立即迁移。继续限定单 worker、独占 Store；大负载写入竞争、任意损坏及断电持久性仍未证明。
4. **是否达到 Linux 多账号试运行/受控预发布门槛？未达到。** 真实媒体 FAIL，20 请求功能批次未完成，并发 3、原 5/10 账号门槛与两小时稳定性均未完成。

本阶段结论不会追溯覆盖第一阶段 Windows 的历史通过项。当前继续保持全部实号停发、现有 PR Draft；任何未来续验须先获得新的用户指示，沿用原账本、原操作 ID 与最新 Cookie，保留当前失败，不重新 reserve/send 已有意图。
