# 邮箱验证码批量登录

管理页的「邮箱验证码批量登录」使用单个独立浏览器，串行处理至多 10 个待完成任务。它只负责登录和认证入池，不改变 `MUSE_BACKEND`，不主动 bootstrap、唤醒 VM 或提交生成。

在账号池页每行填写一个已获授权的 Muse 邮箱，点击「加入登录队列」。当前邮箱到达「等待验证码」后，从自己的邮箱读取验证码并手动填写六位数字。页面不接受第三方收件箱 URL；服务不访问收件箱。原 Cookie 导入仍然保留。

成功表示本轮 OTP challenge 邮箱匹配、认证页面的两个独立主体来源一致，且凭据已入池；**不表示生成能力、额度或实际模型权限已通过测试**。当前实现依赖真实网页浏览器，尚未证明或提供纯 HTTP OTP 登录。

## 运行条件

运行环境需要可用 Chrome/Chromium，以及既有 Python 依赖。可通过 `MUSE2API_CHROMIUM` 指定浏览器。服务启动和查询登录队列只检查本机可执行文件，不启动浏览器；第一个实际登录任务才创建专用浏览器。协议后端的默认 Docker 镜像不安装 Chrome，会显示「登录助手不可用」，创建登录任务返回 HTTP 503，原协议 API 和 Cookie 导入继续可用。不会为登录自动切换后端或安装浏览器。

建议将 `MUSE2API_HOME` 指向仓库外、仅服务用户可读写的私密目录；Docker 使用独立数据卷，并保留已有数据。账号 Cookie 位于该目录的 `data/accounts.json`，稳定身份指纹使用同目录 `email-login-identity.key` 的随机私盐进行 HMAC-SHA-256。请将这个密钥和账号文件一起备份；丢失或替换它会破坏身份去重。不要把运行数据、浏览器 profile、Cookie 或验证码提交到 Git 或放入构建上下文。

登录浏览器使用独立 CDP 端口、专用临时 profile；每个邮箱独立上下文。不会连接或清理生成后端、共享浏览器、生产 VM。任务结束/取消会关闭当前登录上下文，服务退出关闭本实例的登录助手。

## 队列与恢复

- 状态依次为 `QUEUED`、`STARTING`、`WAITING_FOR_CODE`、`VERIFYING`、`SAVING`，最后为 `AUTHENTICATED` 或明确失败状态。
- 验证码等待上限 600 秒。错误验证码最多尝试 3 次；不重新发送验证码。
- 验证码只在当前输入框及进程内存使用，提交前清空输入框，不写入 localStorage、队列 JSON、账号 JSON 或日志；页面轮询保留尚未提交的输入。
- `FAILED`、`BLOCKED`、`UNKNOWN`、`CANCELLED`、`EXPIRED`、`INTERRUPTED` 均不会自动重发。重启将未完成任务标为 `INTERRUPTED`，不继续旧验证码或登录提交。检查原任务后，由管理员明确创建新任务。
- `SAVING` 已进入账号写入边界，不能取消；其他运行阶段取消后，迟到的登录结果不得入池。
- `data/email-login-jobs.json` 只保存用户提供的邮箱、状态、时间、计数和固定错误代码，不保存 Cookie、OTP、原始上游身份、HTML 或任意上游错误正文。

## 入池和覆盖保护

本轮 challenge 回显邮箱必须与任务邮箱一致，认证上下文中的 viewer/principal 与已认证用户标识必须一致。原始上游身份只在内存中使用，账号文件只保留加盐身份指纹及 `login_email`。

同一指纹再次登录更新原账号，不创建重复账号。已有同邮箱对应不同指纹、同指纹对应不同已登记邮箱或多个重复指纹时拒绝覆盖。仅通过旧 Cookie 导入、没有认证指纹的账号无法离线判断归属；不会根据 Cookie 或 VM ID 猜测并合并它们。

账号写入与账号池共用租约锁：繁忙账号、有结果未知隔离标志、正在准备/执行/恢复的协议操作或未完成任务时拒绝更新，不清除隔离或操作记录。成功更新递增 `credential_revision`，旧 Runtime 的 Cookie 回写 CAS 被拒绝。禁用状态、代理配置及历史任务保留，认证入池不自动将生成检测状态标为成功。

## 管理 API

所有接口沿用服务的 `Authorization: Bearer <MUSE2API_KEY>` 管理鉴权：

| 方法 | 路径 | 请求 |
| --- | --- | --- |
| GET | `/admin/email-login/jobs` | 查询脱敏队列和 `available` |
| POST | `/admin/email-login/jobs` | `{"emails":["authorized@example.com"]}`，每次 1–10 个 |
| POST | `/admin/email-login/jobs/{id}/code` | `{"code":"123456"}` |
| POST | `/admin/email-login/jobs/{id}/cancel` | 无请求体 |

响应只包含队列快照，不返回验证码、Cookie 或原始身份。提交动作由单一后台工作线程执行，API 不等待浏览器完成。没有接受任意 principal/Cookie 的邮箱认证导入接口。旧 Cookie 管理接口保持原行为。

## 已执行的受控验证

在全新临时 Store、内存 ASGI 请求和 fake driver 上执行：

```powershell
python tests/test_email_login_app.py
python tests/test_email_login.py
python tests/test_email_login_browser.py
```

`test_email_login_app.py` 包含 API 鉴权/参数和脱敏、无浏览器能力降级、保留 Cookie 导入、同身份去重与 CAS、冲突/繁忙/隔离保护、真实 Manager 配 fake driver 的串行入池、启动不创建浏览器，以及离线 Node DOM 的文本注入防护、轮询保留输入、提交即清空测试。Node 缺失时 DOM 测试明确跳过。

这些受控验证不代表真实 Muse 发码、实号认证或生成验收。实际登录必须单独记录运行环境、数量、结果和未覆盖项；既有协议验收历史证据不因此改变。
