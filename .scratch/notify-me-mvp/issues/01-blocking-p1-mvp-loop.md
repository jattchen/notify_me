# 01 — 跑通 Blocking/P1 最小真机闭环

**What to build:** 让用户能够从本地安装 Notify Me 开始，通过渐进式激活流程私密绑定 Bark、自动初始化最小状态库并确认测试通知；经明确授权写入实际生效的全局 AGENTS 托管规则后，新顶层任务遇到任务阻塞时，主 Agent 按需读取 Notify Me，并让用户手机收到固定 P1 通知。此 Ticket 是整个 MVP 的 tracer bullet，只实现完成该闭环所需的最薄 Activation、Notification Runtime 与 Bark Transport，不实现用户订阅或 Hook。

**Blocked by:** None — can start immediately.

**Status:** resolved

- [ ] Notify Me 可以作为本地插件安装，并从只读检查进入显式初始化；初始化自动创建最小 SQLite 状态库，不要求数据库服务、Docker 或手工 SQL。
- [ ] 首次激活会指导用户在 Bark App 中找到完整推送地址，并只通过不回显的终端私密输入完成绑定；Bark 地址和设备密钥不进入聊天、命令参数、终端回显、日志或 SQLite。
- [ ] Bark 地址经过规范化和安全校验；生产地址只允许 HTTPS，loopback 测试地址可使用 HTTP，并拒绝凭证随重定向转发。
- [ ] 用户能够发送固定 P1 测试通知；本地 fake Bark 验证 payload，真实 Bark 只报告“服务已接受”，随后由用户确认手机实际出现通知。
- [ ] 激活流程解析当前 `CODEX_HOME` 中实际生效的全局 AGENTS 文件，展示精确托管块及影响，并且只有在用户明确授权后才原子写入。
- [ ] 写入规则后明确进入需要新任务验证的状态，不宣称当前任务热加载成功；新顶层任务能够验证托管规则已经生效。
- [ ] 新任务中的普通问答不会加载或调用 Notify Me；构造明确的任务阻塞后，主 Agent 才按需读取 Skill，并发送固定 Blocking/P1 通知。
- [ ] Subagent 或 Ticket Worker 不会自行发送该通知；遇到已知 worker 标识时返回抑制结果。
- [ ] Ticket 的安装物不声明、不安装、不运行任何 Hook，也不存在用户订阅创建或恢复入口。
- [ ] 自动化测试覆盖私密绑定、固定 P1 payload、Bark 成功与安全失败分类、AGENTS 托管块写入，以及普通问答与 worker 的负向场景。

## Answer

已集成提交：`5f5ba29fa94e5484d202021e80eb1cf2e135e7b4`。

协调器在集成后的 `main` 上复跑 `python3 -m unittest discover -s tests -v`，21/21 通过；Python 编译检查和 `git diff --check` 通过。真实 Bark 服务与手机可见性由人工验收阶段确认。

2026-08-03 真机验收完成：用户在新顶层任务中构造“缺少准确四位确认码且不可猜测”的真实 Blocking 场景；主 Agent 在索取信息前调用 Notify Me，返回 `ok=true`、`status=accepted`、`attempts=1`，用户确认手机实际收到固定 P1 Bark 通知。全局规则验活状态为 `active`，该测试未修改项目内 AGENTS 文件。
