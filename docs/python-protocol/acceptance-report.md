# Python 协议集成验收报告

日期：2026-11-03，Asia/Shanghai。仓库 `Brilliant666/muse2api`，分支 `codex/python-protocol-integration`，继续使用 [Draft PR #1](https://github.com/Brilliant666/muse2api/pull/1)。

**结论：达到“构建与离线验收通过”这一层级；尚不能判定可进入单账号试运行或现网替换。** 专用测试账号未提供，实号服务账号池为空，T1–T6、I1、I2、V1 全部 BLOCKED，真实生成消耗为文本 **0/6**、图片 **0/2**、视频 **0/1**。没有用合成对端、导入成功、文件头或 HTTP 200 替代真实验收。

## 版本与隔离

- 初始远端与 PR head 均为 `52d5acd74ec54757fe228a4f3daed6099046739a`。保留现有开发目录，另建 `C:/Users/brilliant/Documents/ChatGPT/muse2api-protocol-acceptance` worktree；没有覆盖未提交工作或回退后续提交。
- 最终配置测试 SHA：`b9228ccf3fce45a25b72b0d17bb5e77b4e1ede08`，tree `d2b6925109de9095d6bfd389c8abaa6eb9728360`。
- Python 完整回归及两种镜像源 SHA：`beed898fb088209051ca2bbaa4112f8bc92d0eed`。此后只有 `docker-compose.yml` 增加 init；最终 SHA 做了 Compose 解析、实际 init、同卷记录与 Chromium 回收定向验收。Python、应用、协议补丁和 Dockerfile 的 Git blob 均未改变。后续报告提交只包含文档和证据。
- 协议固定源仍为 `its-benjamin/muse2api-http@6309f64b44ce133a2428fadf966a395d503ce84f`。真实联网克隆后归档、补丁和五文件摘要均验证；没有改 manifest 摘要或 vendor 完整协议源码。
- 干净 Python 3.12.16 venv 实际 pip 安装：`curl_cffi 0.17.0`、`cryptography 50.0.2`、`muse2api-http 0.2.0`、FastAPI 0.162.0、Uvicorn 0.54.0。完整版本见 [依赖记录](evidence/acceptance-dependencies.json)。服务使用普通安装产物，没有测试替身或临时依赖目录注入。
- 源码服务使用新建私密配置和全新应用数据根目录，监听 `127.0.0.1:18611`，并发 1、单 worker。容器使用独立命名卷 `muse2api-acceptance-20261103-data`；切换前分别停止并确认退出。没有读取生产账号，也未切换生产；IIJ 的并发 3 未修改。

## 实际结果

| 检查 | 状态 | 证据层级与边界 |
| --- | --- | --- |
| 固定协议 Git 准备、输出校验与安装 | PASS | 真实 GitHub clone / Git 对象 / pip；另有 2 项真实 Git 换行回归 |
| 协议离线回归 | PASS | 64 Python（历史 61 + 新增跨进程恢复 3），6 JS；0 fail/error/skip；真实框架、合成对端 |
| 原有相关回归 | PASS | 25 Python 脚本（含准备器新测试），原管理页 JS 10 行为；实际隔离 headless Edge |
| 原生 curl HTTP / WebSocket | PASS | Windows 6 项、Linux Docker 6 项；实际已安装协议与 native I/O；对端合成，WS 使用 IdentityCipher，不证明真实 Noise/TLS/Muse 认证 |
| 默认协议镜像构建、启动与元数据 | PASS | 实际 Docker Engine；容器内部 HTTP、鉴权、管理页、模型别名、空账号接口通过；实际检查无 Chromium 二进制/浏览器进程 |
| Chromium 回退镜像 | PASS（限定范围） | 实际构建、启动和本地 DOM/双参考上传选择测试；相同 Key、卷中合成任务和媒体字节保留。没有实号文本回退 |
| 受控断连、unknown、重启与交付恢复 | PASS | 实际 Store/Backend/ChatService/SessionStore + 合成网关；强杀真实 OS 进程后新解释器恢复；每案生成提交计数均保持 1；VM 不符拒绝，媒体恢复只重下原文件 |
| Chromium 子进程回收 | PASS（修复后） | 原配置实际残留 8 个 PPid=1 的僵尸进程；Compose init / `docker create --init` 后同测试残留 0，单 Uvicorn worker |
| 真实 Chat / Responses 普通与 SSE、T5、T6 | BLOCKED | 未指定测试账号；没有发送生成请求 |
| 真实图片、双参考图编辑、视频及完整解码 | BLOCKED | 未生成实号媒体；没有实际格式/尺寸/时长/完整解码结果可报告 |
| 真实 Cookie 轮换、VM 休眠/唤醒 | BLOCKED / NOT_OBSERVED | 没有实号观察；合成 Set-Cookie 顺序/expiry/删除/Store 重载、401/403/503、合成 wake 分开记录 |
| 实号多账号/代理、活跃连接与资源 | BLOCKED | 无第二账号/指定代理；仅完成合成隔离与空闲内存快照，不宣称性能提升 |
| Windows → Docker 发布端口 | BLOCKED | `127.0.0.1:18612` 拒绝连接；容器内 HTTP 正常。本机 Docker Desktop 的命名管道/端口转发有环境故障 |

逐项机器可读结论见 [acceptance-results.json](acceptance-results.json)，9 个实号案例和预算见 [案例账本](evidence/acceptance-cases.json)。合成媒体字节或浏览器 DOM 中的示例 data URL 不属于真实生成媒体完整解码证据。

## 修复与复现

1. **Windows 固定 Git 准备失败**：全局 `core.autocrlf=true` 先改变 `git archive` 中 LF 文件的原字节，随后又改变 `git apply` 输出。固定源 blob 与原 manifest 本来一致。准备器对这两步显式固定 `core.autocrlf=false`、`core.eol=lf`，仍先验原始摘要、再按清单规范化和打补丁。真实克隆与 2 项真实 Git 回归通过；未降低校验。
2. **容器孤儿进程未回收**：实际 Chromium 测试退出后留下 8 个已退出但未回收的子进程，父进程为 PID 1 Uvicorn。Compose 增加 `init: true`；用相同镜像、相同测试和 `--init` 验证，残留 Chromium/僵尸均为 0，任务与媒体仍可读取。直接 Docker CLI 运行同样需要 `--init`。
3. **验收入口兼容问题**：干净安装的新 AnyIO 不再带未使用的 sniffio，旧预检因此错误 BLOCKED；移除该无用预检。Windows asyncio 的 socketpair 使用 loopback TCP，合成测试原先连本机也拦截；仅放行 loopback，继续拦截外网。并发测试显式创建两槽锁，避免依赖部署参数默认 2。未改业务并发或通过删除业务断言掩盖失败。

原有脚本首次只因上述并发测试前提失败；修复后只重跑该脚本，保留其他已通过结果。协议完整回归在接入新增 3 项恢复后执行为 64/64。浏览器回收只因实际 8 个残留这一证据触发定向复测。未发现可在当前受控条件下复现的 Python 业务协议层缺陷。

## Docker 的实际执行方式

Windows Docker CLI 的两个 context 均在读取 version 时超时，最初 build 也超时。只读诊断发现现有 WSL daemon 本身健康；从 Docker 官方固定 URL 下载独立 Linux CLI 到测试临时目录，经其已有 Unix socket 完成构建、create、start、exec、stop，没有重启 Docker Desktop、WSL、daemon 或已有容器。

两种镜像 ID：

- protocol：`sha256:4d3cb26b0173fa37ed4f59148fe051e83c29e42a1e37dc8c1d3729531cd48649`
- browser：`sha256:129a69891043e1a6f51d072baf080cfbd2dcd0a890bab138545514370fca46a6`

准确构建命令、时间、日志摘要与哈希见 [Docker 构建记录](evidence/acceptance-docker-builds.json)。实际使用独立 tag/container/volume 和 loopback 发布端口；Compose 做过实际配置解析，容器通过等价 Docker CLI 参数启动，不宣称 `docker compose up` 在这个故障 Windows endpoint 上成功。

源码元数据检查、协议容器内部 HTTP、native Linux I/O、浏览器镜像、init 修复分别见 `evidence/acceptance-metadata.json`、`acceptance-docker-runtime.json`、`acceptance-native-docker.json`、`acceptance-browser-fallback.json`、`acceptance-init-runtime.json` 与 `acceptance-init-cleanup.json`。原始缺 init 的 8 个僵尸证据保留在 `acceptance-browser-chromium.json`，不能单看其中 DOM PASS 误解为回收通过。

测试已结束：源码服务和本轮三个测试容器均已停止，未留下实际浏览器进程。init 容器停止时日志确认 application shutdown complete；退出码 143 如实保留，不改写为 0。独立镜像、已停止容器与仅含合成数据的卷保留供续跑，不清理共享 Docker 资源。

## 复跑与剩余条件

在新 checkout 和私密测试配置中准备依赖后，可按顺序执行：

```powershell
python tools/prepare_protocol.py --clone
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt ./.protocol
.venv/Scripts/python.exe tools/test_protocol.py --report offline.json
.venv/Scripts/python.exe tools/test_original.py --chromium '<独立 Chrome/Edge 可执行文件>' --report original.json
.venv/Scripts/python.exe tools/native_protocol_check.py --report native.json
```

本轮本地保留初次失败的 `.protocol/`；实际安装和测试使用显式的新验证输出目录，详见 `acceptance-offline.json`。在该 worktree 续跑时使用其 `--protocol-source` 路径，或按准备器规则选择新输出，不能覆盖已有目录。统一 runner 已包括跨进程恢复测试，无需为重测它再次运行整套生成。

真实验收最少还需要用户明确指定一个专用有效 Muse 测试账号的本地私密配置路径，或在隔离管理页导入。不要在聊天粘贴 Cookie 或 PAT。随后按 [RESUME.md](RESUME.md) 逐案先落盘意图、预扣预算、只发送一次；失败和超时也计数，unknown 只查原操作，delivery_failed 只取回原媒体。恢复 Windows Docker 的正常对外端口访问、准备完整图像解码器及 ffprobe/ffmpeg 后，才能补齐对应环境与媒体项。当前机器未在 PATH 中发现 ffmpeg/ffprobe。

真实 Cookie 轮换和 VM 休眠若未发生，应保持 NOT_OBSERVED；实际多账号/代理若属于目标部署要求，仍需相应账号和代理证据。所有这些未执行项均不算通过。本轮不合并 PR，不迁移或切换现网。
