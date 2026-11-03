# 验收结果与后续查询

本轮真实测试已完成，SHA `b08f11000930674990c2834e1a9da091fd08a668`，九案全部 PASS。预算 **6/6 文本、2/2 图片、1/1 视频** 已用满。测试服务与独立 Chrome 均已停止，私密账本和媒体保留在仓库外；不再等待账号导入。

[验收报告](acceptance-report.md) · [机器结果](acceptance-results.json) · [案例账本](evidence/acceptance-cases.json)

## 本轮规则

每个生成意图在 POST 前原子持久化并预扣预算，失败/超时/unknown 也占用。`tools/live_protocol_case.py` 会拒绝已有案例再次发送；不得为了重发清空账本或换空状态目录绕过本轮上限。现有 `protocol_smoke.py --generate` 会打包发送新文本和图片，不能用它补测本轮。

本轮账号是用户后续明确授权复用的既有账号，通过独立私密文件交接并导入隔离 Store。不要在聊天、报告或 Git 记录 Cookie、服务 Key、签名下载 URL 或原始关联 ID。`MUSE2API_HOME` 是包含 data/ 的根目录，不能让两个实例同时占用同一数据。当前不需要新的账号输入来完成这轮。

## 已准备的工具

- `tools/live_protocol_case.py`：单案例 send/reserve，查询原 task、恢复原 operation、下载原文件；只读取服务 Key，原始响应与关联 ID 留私密目录，stdout 仅摘要。默认私密目录是本机测试临时根的 live-cases/。
- `tools/live_protocol_disconnect.py`：仅消费预扣的 T5，实际内容出现且原操作仍执行时才断开；已完成则 NOT_OBSERVED，不重发。它不停止或重启服务；本轮 T5 已恢复，不能再 reserve/send。
- `tools/decode_acceptance_media.py`：Pillow verify+逐帧 load；ffprobe 与 ffmpeg 全文件视频/音轨解码。ffmpeg/ffprobe 9.0.2 位于本机私密测试根 media-tools/，使用参数指定完整路径，不进入 Git。自产样例和真实 Muse 媒体的证明层级分开。

以下 status 命令只读私密账本，不请求服务、不生成：

```powershell
.venv/Scripts/python.exe tools/live_protocol_case.py status
```

如果以后只需要原操作查询/恢复/下载，先核对私密账本；按“只读核对→停止→确认退出→启动→健康检查”分步操作隔离服务。`recover` 只查询原会话或取回原媒体，不重新生成。使用原 Store 和原关联 ID，不能导入旧 Cookie 覆盖真实续签结果。明确放弃恢复返回 `upstream_cancelled:false`，不代表取消上游。

当前保留初次准备失败的忽略目录 .protocol/；实际安装用的是新的已校验输出，路径记在 evidence/acceptance-offline.json。源码服务使用 venv 普通安装产物，没有注入该不完整目录。需要重建时选择新输出或按准备器说明保留旧目录，不覆盖现存输出；生成协议源码、venv、私密配置、data 和媒体均不提交 Git。

## 后续部署验收边界

当前结论仅适用于本机 Windows 源码、并发 1、单 worker、单账号。Linux Docker 已真实构建/启动并完成 native 合成 I/O，但未用本轮实号预算在容器中生成；Windows Desktop 发布端口转发上轮存在环境阻塞，实号轮未复测。

真实 VM 休眠/失活→RUNNING 未观察到，不能用本地重启或 wake HTTP 成功替代。实号多账号/代理、真实并发 3、真实 delivery_failed 恢复也没有本轮实号证据；相关受控验证单列。现网 3 是未修改的部署参数。

如目标替换需要这些条件，应先确定独立测试安排及新增生成预算。任何新生成都不属于本轮剩余额度。本轮继续使用现有 Draft PR #1，不创建重复 PR，不合并、不迁移或切换生产。
