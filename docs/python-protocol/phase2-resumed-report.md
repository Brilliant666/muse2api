# 第二阶段续测：多账号、完成判定与 Linux（2026-11-03～04）

**结论：本轮已按用户“尽快收尾”结束，未达到 Linux 多账号试运行或受控预发布门槛。** 九账号实际 Linux、单 worker、全局并发 1 的新功能请求共 20 次，发现并修复两类提前完成缺陷；最终 commentary 修复尚未进行新实号生成。没有启动 C3、C5、断连注入或两小时批次，不把剩余预算当作继续发送的授权。

第一阶段、首次第二阶段及邮箱登录报告保持原样。本轮起点为 `d99ba0618f6ee94aee424e0910e531fdaf43d5b9`，继续现有 Draft PR #1，不合并、不操作生产。

[机器结果](phase2-resumed-results.json) · [案例](evidence/phase2-resumed-cases.json) · [资源](evidence/phase2-resumed-resources.json) · [构建环境](evidence/phase2-resumed-environment.json) · [历史保全](evidence/phase2-resumed-preservation.json) · [续接说明](phase2-resumed-resume.md)

## 范围与账本

用户恢复有界实号验收并授权增加预算。本轮第二阶段总硬上限为 **600 = 560 文本、35 图片含编辑、5 视频**，包含旧第二阶段已消耗的 **3/1/0**；第一阶段 **6/2/1** 独立保留。失败和超时也计数，未知操作只恢复原操作，不换账号重发。

旧第二阶段 ledger SHA-256 为 `7b59725580ae290ad30809f6f7af827235d28f7af31e9be14db299faaf52875b`。新账本追加、hash-chain、发送前 fsync，并用旧账本旁的单一 authority 绑定唯一续测目录；缺失、截断、换空目录或重复案例均拒绝继续发送。

九个测试账号来自此前获授权的邮箱登录池，真实 Windows 页面双 principal 与 Linux 原生 curl HTML 均核对为九个独立身份。初始实时 Windows UI 周额度均显示已用 0%；该额度来源不能写成 Linux 协议额度查询。新 worktree、venv、Store、协议状态、端口和 Linux 容器均隔离。原 18611 实例保留；18613 登录实例在确认无任务、配置活动后暂停，测试期间单一 Store 只由一个服务或诊断进程持有。

**全局并发 3 表示三个不同账号各一条在途请求，每账号上限 1。** IIJ 无法确认第十个独占身份，因此原十账号 C5/两小时 SOAK 门槛仍 BLOCKED，九账号长期测试只能单列部分验收。

## 已观察的实号结果

修复前 L2 全局并发为 1，共发送 11 次：10 工具 PASS、1 图片 FAIL，剩余 9 项未发送。失败图片 L2-011 初次 `media_missing` 保留 FAIL。随后在媒体修复版本补测原未发送的九项，用新编号 D02～D10，不重新打开 L2 槽；九项接口/归属/解码工具均 PASS，但 D10 后置语义核验发现返回了进度消息，必须单独记为初次业务失败。

因此这 20 个新功能请求不能简单写成全部通过：**工具结果 19 PASS / 1 FAIL；初次业务结果 18 PASS / 2 FAIL**。本轮新增消耗 15/4/1，加上旧第二阶段为 **18/5/1**，合计 24 次，剩余 **542/30/4**。没有重置预算。最终 Store 的 20 个 completed 操作包含 D10 的旧错误缓存，不能据此改写初次业务结果。

| 案例 | 证据 |
| --- | --- |
| L2-004 图片 | 53,868 字节 WEBP，1920×1280，完整解码；SHA `5ec0ddc8c552ef0deb3175edee2a7fe96cd8edd9db0f7b284d638a1126642d6c` |
| L2-007 双参考图编辑 | 4,818 字节 PNG，完整解码，目视确认红方块在左、蓝圆在右；SHA `a35d4efa8148b5aeb30dc85a97a8b20ab7487e0a236ca43ab95233105d694178` |
| L2-011 初次图片失败 | 后置真实原 history 出现原 turn 的媒体；初始 FAIL 不重标。之后恢复同一原操作并下载 39,704 字节，完整解码，新增生成 0；SHA `65f235385b4395c5079d9896e2e7faef420cf14dbad90661cc7822faeacd870e` |
| D04 编辑 | 40,042 字节，完整解码；SHA `62cdce47393d0d8b7377632425105d786484b7a6b9769644ee4f5329ad73ebfc` |
| D07 视频 | 375,996 字节、5 秒、H.264 1280×720/24fps，完整解码 120 帧及 1 音轨；SHA `c96f1a75840ef0d37a977b45d46fb4b00672fde81559df7f8c348b037a63ce74` |
| D10 Responses | 原 API 111 字为显式 commentary；后置原 history 的另一条正式 message 才是 14 字测试 marker。保留初始工具 PASS 与语义失败，不改 completed 状态来强行恢复，也不把 completed recover 缓存当远端证据。 |

## 可复现缺陷与修复

1. **无媒体时把助手消息结束当整轮结束。** SDK 的无媒体分支会基于 assistant completed、文本稳定两秒或弱拒绝措辞提前返回。L2-011 后置原会话存在媒体，presentation 事件记录晚于本地失败；展示更新时间不是文件创建时刻，失败瞬间没有完整 history 快照，因此不臆断当时命中了哪一个弱分支。旧实际 SDK 的七项受控回归有三项失败，修复后七项通过：没有媒体且没有明确 turn 终态就继续原 deadline，超时保持 accepted/unknown，不重新生成。
2. **显式 commentary 被当作最终文本、SSE 和完成依据。** D10 前导消息的 `payload.metadata.message_kind=commentary`、`recipient=commentary`、`public_commentary=true` 与后置正式 `message_kind=message` 属同一 root execution。SDK 忽略这些字段而提前返回。修复只使用明确协议字段，保留 awaiting-reply；commentary 不进入文本、消息 ID 或 on_delta，原始事件和媒体仍保留。无正文关键词规则、不增加固定等待，也不强制要求每次出现未观察到的 `turn.completed`。八项新受控测试及七项媒体测试通过。
3. **验收工具的假阳性。** 原工具仅检查非空文本及与自身 operation 相等，D10 的进度也被标 PASS。现在先保存、验证原绑定，再要求 marker-only 请求的实际文本去除首尾空白后严格相等。多话只表示未满足本测试期望，不一律定性为后端缺陷。只读绑定与 D01 长文本诊断不受影响。

用户提供的单账号单在途限制保留为硬约束。本地 pool-02 前一次文本释放到图片获取租约相隔约 176.264 秒，没有本地租约重叠；这不足以证明远端先前整轮已经结束。修复准确的消息完成判定比单纯延长本地锁更关键。

## 零生成原 VM 与会话读取

本轮对 L2-011、D10 的独占诊断均读取真实 `session_info`，捕获原 VM 的 SUSPENDED，执行 `bootstrap(force=True)`，实际 wake 一次、真实后态 RUNNING，再用真实 Noise 直接 GET 原 session 的 chat.history，完整原 prompt 匹配。探针不生成、不审批、不调用 recover；前后 journal、原 ledger、operations、tasks 及关联 ID 集合完全一致，Cookie 通过原 CAS 回写。

这构成本轮新测试 VM 的零生成 wake/read 证据，**不改写第一阶段 NOT_OBSERVED 历史**。L2-011 的后续恢复是另一个明确动作，不与该只读探针混算。

另一次 scoped `sessions.get` 返回会话级 `status=completed` 与 `can_send_*`，但没有当前 execution/active/idle/inflight 字段，也没有忙碌时对照，不能把它当成已验证的 agent 空闲信号。第一次尝试在 RPC 前失败，第二次成功一条 gateway GET；两次分别保留，不写成一次无失败。

## 环境、版本与检查点

实际 Linux Docker/WSL2 x86_64，单 worker。Python 3.12.16、curl_cffi 0.17.0、cryptography 50.0.2、FastAPI 0.162.0、Uvicorn 0.54.0、Pillow 13.0.0、ffmpeg/ffprobe 5.1.9。镜像内无 Chromium，显式关闭 WebSocket fallback，凭据与原始响应均留仓库外。没有把 Windows 源码成功或合成对端当作 Linux 实号成功。

| 提交 | 用途 |
| --- | --- |
| `f54b21436d39a219b5ce8241a452ed758ad06f73` | 新有界续测控制器与身份预检；L2 工具版本 |
| `7eb327c86c4ce2097472b5deab00fd5e75fd8e69` | 断连、原 history 零生成诊断 |
| `c750f2fc23b9c9d5be95d91034a1ac9151438694` | 无媒体早完成修复；D02～D10 的 SDK 源码 |
| `edbb85ee297aaea4e39bb7a1b2dc5704d51e06cd` | 单例诊断完成与账号池准入分别判定 |
| `21ed2cd7913e527de8561b358182c7ac2432c95c` | commentary 修复、严格 marker 验收；新实号补验 NOT_RUN |
| `90da553` | 最终测试工具：标准验证入口纳入 15 项新回归、默认关闭的有界 session 观察器；只读观察器仅 fake 验证 |

固定协议 Git 仍为 `6309f64b44ce133a2428fadf966a395d503ce84f`。旧、媒体修复、commentary 修复三组实际 source/codec 构建与正常安装均保留；最终候选 codec 为 `sha256:4bd98aee11a0d34882f809b7e0a72599ea4026a272a0563445e002e1a5bf3bab`，安装的 chat.py 为 `ea7356e5987bdacbda75fb38253bcc76395f273a0df2920f894259d9218d21ab`。其余四个 manifest 输出保持原样。

最终标准入口已纳入新回归，**79 Python + 6 JS 全通过，0 skip**；其中包含原 64 项和媒体/commentary 的 15 项，不重复相加。控制器 19 项、可选 session probe 5 项、原 metrics 契约 6 项也通过。较早工具另有身份预检 15、Linux 预检 14、断连 6、原 history 8 项，保留各自受测 SHA。这些是受控测试；最终镜像仅构建、实际安装摘要及零生成 native GET 已验证，不能写成最终 SDK 新实号生成通过。

## 性能与资源边界

两份闭合指标分别为 L2 的 843 个事件加 footer、媒体修复 C1 的 1017 个事件加 footer，dropped、writer、hook、Store 错误均为 0。新生成写入/本地 flush 分别 11/11、9/9，每原 operation 各一次，未分类 POST 为 0；它们不是独立 provider 计费或精确接受次数证明。

实际采样 active operation 与真实租约峰值均为 **1**，Runtime 峰值 9。L2 有两条连接各复用两次生成；媒体修复 C1 的十次握手包含零生成恢复，不能冒充十账号。指标窗口为 485.827/1171.128 秒，实际客户端负载窗口仅 213.814/187.041 秒，均不是两小时负载。

两窗 RSS 分别 80,814,080→106,438,656 和 75,182,080→106,676,224 字节；FD 峰值均 56。进程 CPU 增量分别 3.15/5.90 秒。Store 写入 median/P95 分别 12.418/17.520 ms、11.404/14.465 ms；median 与 nearest-rank P95 的方法、每类延迟和分布见机器资源证据。短期缓存增长不证明泄漏，也不证明十账号长期稳定。没有无依据增加 idle TTL。

## 故障、未执行项与四项判断

| 项目 | 本轮结果 |
| --- | --- |
| 固定 Git、安装、Linux 镜像构建与启动 | PASS，真实流程；最终 SDK 生成与启动旧版本的证据分别记录 |
| Linux 图片、编辑、视频完整媒体 | 个案 PASS；保留 L2-011 初次 FAIL 和零生成恢复补充，不把失败抹掉 |
| 原 VM 休眠唤醒、原会话读取 | 本轮新 VM PASS；旧第一阶段仍 NOT_OBSERVED |
| Cookie 真实轮换与写回 | 九账号 hatch_sess/hatch_vml 值均变化，CAS 回写最新 Cookie；未重新导入旧 Cookie |
| 新 SDK 五项 postcheck、真实 C3/九账号 SOAK | NOT_RUN：用户要求尽快收尾，未创建这些发送意图 |
| 十账号 C5、原十账号 SOAK | BLOCKED：只有九个已授权独立身份 |
| Linux 实号断连、取消、在途重启/unknown 恢复、截断交付故障 | NOT_RUN；D01 未使用；第一阶段 Windows 与旧受控测试不冒充本轮新证据 |
| 401/403/429、额度、坏代理、人工凭据更新竞态、VM 不匹配 | 新实号定向注入 NOT_RUN；本轮未观察到这些真实上游错误。旧受控证据保持原版本 |
| 最终修复 Linux 生成、并发 Cookie 覆盖、长期公平性及资源回落 | NOT_RUN，不能被回归或 build/hash 成功替代 |

1. **Python 同进程是否足够支撑目标并发 3：尚未证实。** 本轮只有实测并发 1，未发现必须迁移语言的性能证据。
2. **调度和生命周期是否需修正：准确完成判定必须修正，已完成有红绿证据的 SDK 修复。** 旧原子租约修复保留，每账号上限 1；真实 C3 公平性、长期连接回收和最终修复仍待实号验收，不重写账号池。
3. **JSON Store 是否迁移：暂时保留。** 单 worker、独占持有，当前写入延迟没有迁移依据；跨进程共享、断电持久性及更大负载没有本轮保证。
4. **Linux 多账号试运行或受控预发布门槛：未达到。** 最终修复实号、并发/长期/故障项未执行，原十身份门槛也不满足。

## 收尾状态

本轮生成服务、诊断连接和自有 Chrome 已停止，停止容器/镜像/私密数据及账本保留。原 18611 HTTP 200。九账号最新 Cookie 和 expiry 已通过 revision CAS 同步回原独立登录管理 Store，未复制测试 operation、task 或 quarantine，也未改账号归属与登录元数据。

登录管理页恢复为 `http://127.0.0.1:18613/admin`，仅登录/配置、关闭生成入口与自动保活，Runtime/活动操作为 0。仍保持 Draft PR #1，不合并、不部署生产。剩余预算不自动继续，后续需要新的继续指示并沿原 authority/账本续接。
