# 实号验收续跑

本轮实号账本是 [evidence/acceptance-cases.json](evidence/acceptance-cases.json)。最终配置测试提交为 `b9228ccf3fce45a25b72b0d17bb5e77b4e1ede08`，Python 完整回归和镜像源提交为 `beed898fb088209051ca2bbaa4112f8bc92d0eed`；之后只增加 Compose init 并定向验证回收，版本边界见 [验收报告](acceptance-report.md)。当前没有用户指定的专用测试账号或私密配置，隔离源码服务记录 `enabled_accounts=0`；T1–T6、I1、I2、V1 均未发送。预算已消耗为文本 0/6、图片 0/2、视频 0/1。

后续验收工具提交为 `8b2dc1c55aea2bc3c67ddf5c92984648142cfd52`。新增预算/SSE 用例 8 项、断连用例 5 项均在合成对端通过，完整媒体解码工具通过自产正负例；没有重跑旧的 64 项来重复记数，也没有新增实号通过项。当前管理页按用户要求保留运行在 <http://127.0.0.1:18611/admin>。此前外部会话验证所用 Cookie 经协调确认为现网账号，未交接或用于本轮；仍需独立测试账号。

## 先补齐测试账号

由用户明确指定本地私密配置文件路径，或在隔离测试管理页导入一个专用 Muse 账号。Cookie 至少需要有效的 `hatch_sess`，导入后先测试账号。不要搜索现网或其他项目的账号文件，不要在聊天、日志、报告或 Git 中记录 Cookie、服务 Key 或签名下载地址。

继续使用独立测试目录、并发 1、单 worker 和本机监听。`MUSE2API_HOME` 是包含 `data/` 的根目录。不要让源码进程与容器同时占用同一份数据或同一账号；先只读核对运行状态，切换实例时分别执行停止、确认退出、启动、健康检查。Docker 构建和运行结果由本轮主报告记录，本账本中的账号阻塞不代表 Docker 构建失败。

## 每次生成前先落盘

从账本挑选一个尚未发送的案例，先确定输入和预期证据，再执行以下步骤：

1. 在私密测试目录原子写入发送意图，记录 case_id、带时区时间、请求摘要/哈希、匿名账号标识和预算类别。若证据需要保留原始 operation/session/node ID，只保存在私密目录；入库报告使用匿名关联标识，不包含凭据。
2. 在账本中登记意图并预扣该次预算，再 POST 一次。文本最多 6 次、图片最多 2 次、视频最多 1 次；失败、超时、拒绝或结果未知均计入，不因失败自动补发。
3. 立即持久化收到的 request/operation/task/session/node 关联、返回状态与证据路径。客户端或进程在 POST 附近退出，且不能证明未发送时，保留该笔预算，只查原操作或标为待确认。
4. 需要续跑时先读账本与磁盘操作记录，不重复执行已有发送意图的案例。`delivery_failed` 只恢复原文件，不重新生成。

不要重复跑 `protocol_smoke.py --generate` 来补测单项。该组合每次提交文本和图片，并创建新的图片幂等键；`--generate --video` 还会提交视频。逐案调用原接口；查询、恢复和原文件下载单独执行。

### 已准备的逐案入口

`tools/live_protocol_case.py` 的私密账本必须放在仓库外，默认是本机独立测试临时根的 `live-cases/`。`status` 不发请求；`send` 只运行一个指定案例，先原子预扣预算，任何已有意图都拒绝再次发送。原始响应和关联 ID 只保存在私密目录，stdout 仅有脱敏摘要。

```powershell
.venv/Scripts/python.exe tools/live_protocol_case.py status
# 先确认专用账号健康，再逐项执行；下面不是批量执行清单。
.venv/Scripts/python.exe tools/live_protocol_case.py send --case T1 --env-file .env
.venv/Scripts/python.exe tools/live_protocol_case.py query --case I1 --env-file .env --wait 600
```

I2 还需按顺序提供两次 `--reference <PNG文件>`；两个小图必须可区分。同步格式等未分配实号预算的组合继续只用受控验证。该工具只读取服务 `MUSE2API_KEY`，不会自动将 `MUSE_COOKIE` 导入账号池；Cookie 需要先通过管理接口导入，不能把“写入变量”当作已导入。

T5 先用 `reserve --case T5 --request-file <私密JSON>` 保存流式、较长纯文本任务意图，再用 `tools/live_protocol_disconnect.py` 消费这一个意图。它收到真实内容且确认原操作仍执行、磁盘 session/node/VM 一致时才断开；若已完成则 NOT_OBSERVED，不重发。停止、确认退出、启动和恢复原操作仍由操作者分步完成；辅助工具不重启服务。

`tools/decode_acceptance_media.py` 用 Pillow verify + 逐帧 load 检查图片，用 ffprobe 和 ffmpeg 全文件解码检查视频。便携工具位于本机测试临时根 `media-tools/`，不进入 Git。使用 `--ffprobe`、`--ffmpeg` 指定其完整路径；报告只含格式、尺寸、时长、字节数、SHA 和解码状态，不含媒体内容或原始解码器 stderr。自产测试文件解码通过不等于真实 Muse 媒体通过。

## 案例与保留预算

| 案例 | 原接口 | 预算 |
| --- | --- | --- |
| T1 / T2 | `POST /v1/chat/completions`，普通 / SSE | 各文本 1 |
| T3 / T4 | `POST /v1/responses`，普通 / SSE | 各文本 1 |
| T5 | 确认已提交且仍执行时断开，重启后恢复原操作 | 文本 1 |
| T6 | 隔离环境切换浏览器后端后完成真实 Chat | 文本 1 |
| I1 | `POST /v1/images/tasks`，再查询原 task | 图片 1 |
| I2 | `POST /v1/images/edits`，两张顺序可区分的小参考图 | 图片 1 |
| V1 | `POST /v1/videos`，再查询原 task | 视频 1 |

T5 若断开前已完成，只能记录未观察到实号 unknown；不得自动增加第 7 次文本。重启前保留 unknown 隔离及数据，重启后使用原操作的 `POST /admin/protocol/operations/{id}/recover`，或使用 `protocol_smoke.py --recover`；不再调用生成。明确放弃恢复时要记录 `upstream_cancelled:false`，它不表示上游生成已取消。

I1 可以在原结果交付阶段做一次受控失败并恢复同一文件。I1/I2 的原结果必须完整图像解码，记录格式、尺寸、字节数和 SHA-256；V1 需要 `ffprobe` 检查流、时长和尺寸，再用 `ffmpeg` 将全文件解码到 null 并记录字节数和 SHA-256。HTTP 200、文件签名或视频首帧不能替代完整解码。真实媒体留在私密测试目录，不提交仓库。

## 生命周期和浏览器回退

真实 Cookie 轮换只有观察到上游值/expiry 变化，并验证 Store 写回及重启后认证，才能记 PASS；只保存匿名指纹及凭据版本。没有发生则记 `NOT_OBSERVED`。真实 VM 唤醒需要先观察到休眠/失活，再观察到恢复；等待几分钟、重启本地进程和合成对端的 wake 响应不能替代。可将尚未执行的 T1–T4 安排在实际唤醒后，避免增加预算。

T6 使用带 Chromium 的集成版镜像或等价隔离环境。先保留/处理 unknown，停止当前测试实例并确认退出；再按 README 配置浏览器构建与后端，分别执行构建、启动、健康检查。确认账号、任务和媒体记录仍可读取后，才发送已登记意图的 T6。不要使用未识别隔离字段的旧源码覆盖新数据，不切换生产。

本机可控证据分别在 `acceptance-offline.json`、`acceptance-recovery.json` 和 `acceptance-native.json`，不能将它们写成真实 Muse、Cookie 轮换、VM 生命周期或实号代理/多账号通过。当前没有第二个实号或指定测试代理；如目标部署需要这些条件，另列待验，不扩大本轮预算。

续跑后更新实际测试 SHA、依赖/镜像标识、各案状态和未覆盖项，并更新已有 Draft PR #1。不创建重复 PR、不合并、不部署或切换现网。
