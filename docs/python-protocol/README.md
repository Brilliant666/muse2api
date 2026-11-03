# Python 协议后端试用版

本分支保留原 muse2api 的 API、账号池、Store、管理页与媒体目录，通过同进程 Python 协议模块连接 Muse。浏览器后端仍可选择。本分支是供 Codex 接手真实验收的集成版本；已完成的离线检查和未执行的项目见 [VALIDATION.md](VALIDATION.md)。

## 从仓库开始

应用继续位于仓库根目录，无需下载先前的交付 ZIP。Python 协议依赖来源于：

- 仓库：<https://github.com/its-benjamin/muse2api-http>
- 固定提交：`6309f64b44ce133a2428fadf966a395d503ce84f`
- 本地改动：`protocol-dependency/` 中的 manifest 与补丁。

准备器从固定 Git 提交读取源码，校验需要修改的文件原始字节，应用补丁，再核对输出。生成目录 `.protocol/` 不进入版本控制。不要安装未经本分支补丁修改的同名协议包覆盖它。

### 本地运行

需要 Python 3.11+，建议 Python 3.12，以及 Git。以下命令均在本仓库根目录执行：

```bash
python tools/prepare_protocol.py --clone
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt ./.protocol
python tools/init_test_env.py
.venv/bin/python -m uvicorn app:app --env-file .env --host 127.0.0.1 --port 18610 --workers 1
```

Windows 将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。已有协议仓库 Git checkout 时，可用 `python tools/prepare_protocol.py --checkout /path/to/muse2api-http`，不需要再次联网克隆。准备器拒绝覆盖已存在的输出；保留旧目录用于排查，再用 `--destination /path/to/new-output` 指定新目录，并将安装命令的 `./.protocol` 换为该目录。

`init_test_env.py` 只在 `.env` 不存在时创建测试配置，设置协议后端、并发 1，并把随机服务 API Key 写入文件，不输出密钥。已有 `.env` 不会被覆盖，使用旧配置时请明确设置 `MUSE_BACKEND=protocol`。

管理页默认地址为 <http://127.0.0.1:18610>。从原管理页导入一个测试账号，首期需要包含有效 `hatch_sess` 的 Cookie；没有新增密码或邮箱验证码登录。

### Docker

```bash
python tools/init_test_env.py
docker compose --env-file .env up --build -d
```

构建会读取固定协议提交并安装修改后的包；构建环境需要能访问 GitHub 和依赖源。默认协议镜像不安装 Chromium。Compose 要求明确设置 `MUSE2API_KEY`，默认只绑定本机，运行单个 worker。

默认数据位于仓库 `data/`，容器内为 `/app/data`。源码运行时，`MUSE2API_HOME` 指定的是包含 `data/` 的根目录，并非直接指定 `data/` 自身。账号、任务、操作记录和协议运行时数据均不应提交 Git。

不要让多个 worker、容器或新旧实例同时使用同一份数据目录及账号。当前账号调度是进程内状态，Store 为 JSON 文件。

## 测试与 Codex 接手

完成依赖准备和安装后，安装 Node.js 以运行 6 项管理页 JS 行为用例，再执行：

```bash
.venv/bin/python tools/test_protocol.py
.venv/bin/python tools/protocol_smoke.py --env-file .env --report metadata-check.json
```

第一条运行新增离线回归，第二条只检查本地服务元数据，默认不生成内容。报告中的可控网络测试不等于 native curl / 真实 Muse 已通过。

交给 Codex 的完整任务位于 [ACCEPTANCE.md](ACCEPTANCE.md)。它从当前仓库直接开始，包含原生构建、真实接口、媒体解码、断连恢复、Cookie 与会话生命周期、回退和结果分级。

现有 smoke 脚本提供可选生成模式，但有固定调用组合：

- `--generate` 提交一次文本和一次图片。
- `--generate --video` 提交一次文本、一次图片和一次视频。
- 每次运行都会创建新的图片幂等键。不要通过重复运行整套脚本来补测视频。
- `--recover OPERATION_ID` 只查询恢复；恢复后的媒体完整下载与解码仍需验收。

## 运行行为

原应用继续负责鉴权、模型别名、OpenAI 响应格式、账号选择、异步任务与管理页。候选协议仓库的独立 server / AccountPool 不启动。一个后台 asyncio 线程持有按账号隔离的运行时；同一账号任务、恢复和下载绑定原会话。

Store 是 Cookie 的唯一持久化来源。凭据版本比较避免旧连接回调覆盖人工重新导入的 Cookie，处理 Cookie 删除、expiry 与会话 Cookie 更新。

协议模式下，服务端不会自动换号或重发生成请求，包括明确未发送的失败。错误携带发送状态与操作标识。超时后上游可能仍在执行，本地取消只释放本地资源。

异步任务新增以下状态，客户端需要处理：

| 状态 | 含义 | 后续动作 |
| --- | --- | --- |
| `unknown` | 上游结果未确认 | 查询原操作；对应账号暂时隔离 |
| `delivery_failed` | 上游已有结果，但本地媒体交付失败 | 取回原文件；不重新生成 |
| `completed` | 本地交付完成 | 使用原结果 |
| `failed` | 明确失败 | 按错误元数据处理 |

管理接口：

- `GET /admin/backend`：当前后端及能力。
- `GET /admin/protocol/operations`：操作记录。
- `POST /admin/protocol/operations/{id}/recover`，例如 `{"timeout":60}`：查询原会话或取回媒体。
- `POST /admin/protocol/operations/{id}/abandon`，请求 `{"confirm":true}`：明确放弃本地恢复并解除隔离，响应 `upstream_cancelled:false`。

恢复不会调用第二次生成；这不保证外部客户端任意重复调用的全局幂等性。流式错误发出失败事件，不伪造正常完成。临时媒体文件不会提前公开；当前产品的媒体检查基于签名，验收另做完整解码。

## 已知限制

- 真实 Muse、native curl 传输、实际 Docker 构建及性能数据需后续验收，见验证记录。
- 普通文本缺少明确 activity 状态时仍使用安静窗口推断，需用真实 history 验证结束判断。
- 实时额度查询未实现，管理页明确显示不支持并保留已有缓存。
- 原单仓库的一键覆盖更新已禁用，应用和协议补丁需配套更新。
- 默认协议镜像不提供浏览器操作；回退镜像需显式安装 Chromium。
- 目标环境的代理、多账号隔离和生命周期需要对应实号证据，单账号通过不代表全部部署条件通过。

## 浏览器回退

直接源码启动未设置 `MUSE_BACKEND` 时仍默认 `browser`；测试配置和 Docker 默认 `protocol`。在隔离测试环境中，先处理或保留 unknown 操作、停止当前实例，再执行：

```bash
MUSE_INCLUDE_BROWSER=1 MUSE_BACKEND=browser docker compose --env-file .env up --build -d
```

PowerShell 可先设置两个进程环境变量，再运行 Compose。保持原服务 Key 和正确数据目录。集成版浏览器后端识别账号隔离；未经集成的旧源码不认识新增隔离字段，不能直接覆盖尚有 unknown 的新数据。

本分支不自动迁移或切换现网。真实验收后再安排停止旧实例、备份数据、迁移及回退。

## 第三方来源

原应用基线为 `1cd01682533dbf2c015fa879a8dc09534b50b4a3`，保留原 LICENSE。协议固定提交未发现 LICENSE；本仓库记录来源和修改补丁，不为其代码补授许可证，也不提交整份协议源码。公开分发完整衍生源码或镜像前，需要确认该依赖的授权来源。
