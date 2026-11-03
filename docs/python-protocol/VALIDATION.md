# 提交前验证记录

日期：2026-11-03。范围是把已实现的 Python 集成迁入原仓库根布局、供后续 Codex 实号验收。**本记录不代表 native curl、Docker 实际构建或真实 Muse 已通过。**

## 来源与变更范围

原应用基线：`Brilliant666/muse2api@1cd01682533dbf2c015fa879a8dc09534b50b4a3`，Git tree 为 `0c231808d982e997eb73c49e1f8a47098dbf4190`。本地基线 55 个文件的原始 Git blob 摘要均与远端一致。

协议固定提交：`its-benjamin/muse2api-http@6309f64b44ce133a2428fadf966a395d503ce84f`。5 个补丁输出文件与先前已测交付版的 SHA-256 一致。原应用核心改动沿用交付版；`app.py`、`config.py`、`store.py` 只额外去掉末尾空白行。

本次新增根布局的协议准备器、构建路径、测试入口和仓库内验收任务。协议完整源码不进入本仓库。新增 `.gitattributes` 固定补丁的 LF，避免 Windows 自动换行导致摘要不符。

## 实际执行结果

| 项目 | 结果 | 证据边界 |
| --- | --- | --- |
| 新增 Python 回归 | 61 / 61，0 skip | 原仓库根应用 + 修改后的协议源码；可控网络对端 |
| 新增管理页行为 | 6 / 6 | 实际 JS 函数，DOM/network harness |
| 原 Python 回归脚本 | 23 PASS，1 BLOCKED | 每脚本独立临时目录；缺 Chromium 的媒体选择测试未通过 |
| 原管理页错误处理 | 10 个行为通过 | 原 JS 脚本 |
| 协议准备器 | 8 项通过 | 临时本地 Git fixture commit 的实际 archive、补丁与最终摘要验证 |
| 测试迁移检查 | 61 个 test_ 方法 AST 未改变 | 仅调整目录、导入与报告写出 |
| 缺依赖负例 | BLOCKED，退出码 2 | runner 没有把缺依赖空跑记为通过 |
| Python 语法 | 44 个文件通过 | AST 解析，不等于运行验证 |
| Compose | YAML 解析及根路径检查通过 | 未执行 Docker build |
| JS 语法 | 新增管理页用例与扩展脚本通过 | Node 语法检查 |

原脚本 `tests/test_media_selection.py` 需要实际 Chromium，该项明确被环境阻塞，不计入 23 个通过脚本。外部网络在 Python 回归中被 socket/DNS guard 阻断；原测试使用的本地 HTTP/SOCKS fixture 只放行 localhost/loopback。

本地环境使用 Python 3.12.14，真实 FastAPI 0.115.12、Starlette 0.46.2、AnyIO 4.9.0 等官方版本源码作为测试依赖。它们不是框架替身，但不是本次联网 pip 安装的结果。测试依赖源码不随仓库分发。

原生 `curl_cffi` 在此测试环境中不可用，新增回归的导入/传输边界采用替身。报告明确记录 `curl_import_stub:true`、`live_native_transport_tested:false` 和 `live_muse_tested:false`。协议 wire、history、媒体及适配器的实际模块参与了离线测试。

## 准备器测试说明

8 项检查覆盖：

- 真实本地 Git archive → 原始 SHA-256 → 声明的 LF 规范化 → git apply → 输出 SHA-256。
- 脏输入工作区不改变固定 Git 对象归档，未声明规范化的文件保持原字节。
- 拒绝覆盖已有输出、路径穿越、符号链接和篡改补丁。
- 中途失败保留 `PREPARATION_INCOMPLETE`。

这些检查使用临时 fixture commit，输入文件来自此前验证的原始 Git blob。**本次没有联网克隆固定协议提交；不能把临时 Git fixture 写成线上固定提交的完整准备验收。**

## 尚需 Codex 执行

- 真实 GitHub 固定提交克隆与准备。
- 干净环境依赖安装、原生 curl I/O、实际 Docker 构建与启动。
- 真 Muse 认证、文本/SSE/Responses、图片/编辑/视频及完整解码。
- 真实 unknown/重启恢复、Cookie 轮换、VM 休眠再唤醒。
- 目标部署需要的代理、多账号、浏览器回退与资源指标。

执行 [ACCEPTANCE.md](ACCEPTANCE.md)，从当前仓库开始并把证据绑定实际测试的提交。不能仅修改 `live_validation` 字段把待验状态变成通过。

## 证据

`evidence/` 保存本次结果摘要、迁移审查、测试源摘要与提交内容摘要。操作记录、账号信息、真实 Cookie、私密配置和生成媒体不在其中。没有为本次验证发起真实生成。
