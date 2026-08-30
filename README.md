# Notify Me

Grok 卡住或遇到严重风险时，往 iPhone 上的 [Bark](https://bark.day.app) 推一条短通知。日常只靠全局 `AGENTS.md` 里的几句话，加上一个只负责投递的 MCP。

当前只支持 **Grok**（macOS）。仓库里的 `notify_me/` 是旧的 Codex 实现，装 Grok 请只用 `plugins/notify-me`。

## 会通知什么

- **任务阻塞**：必须等你给信息、授权、做选择或完成外部操作，而且你不回主线就走不下去。
- **严重风险**：继续执行可能造成灾难性或大范围不可逆影响。

普通问答不会推。进度、正常收工不会推。子 Agent 不应推。标题是「任务阻塞」或「严重风险」。只有返回 `ok=true` 且 `status=accepted` 才能说已经推送。

## 安装

需要：Grok、本机 `python3`、iPhone 上的 Bark。本仓库是私有仓库，克隆和 `grok plugin install` 都要用有权限的 GitHub 账号。

**Bark 推送 URL 只能在你自己的终端里粘贴，不要发到 Grok 对话、命令参数或日志里。**

### 1. 安装插件

任选一种。

从 GitHub（插件在子目录里，`#` 后面的路径不要漏）：

```bash
grok plugin install jattchen/notify_me#plugins/notify-me --trust
grok plugin enable notify-me
```

或先克隆再装本地目录：

```bash
git clone git@github.com:jattchen/notify_me.git
cd notify_me
grok plugin install ./plugins/notify-me --trust
grok plugin enable notify-me
```

若本机已经有旧的 `notify-me` 插件或 `~/.grok/config.toml` 里已有 `[mcp_servers.notify_me]`，先停用或删掉，避免两个同名 MCP。

### 2. 确认 MCP 出现

新开一局 Grok，应能看到工具 `notify_me__notify_me`。若没有：

```bash
PLUGIN="$HOME/.grok/plugins/notify-me"
grok mcp add notify_me -- python3 -u "$PLUGIN/scripts/mcp_server.py"
```

把 `PLUGIN` 换成实际安装路径后再执行。然后再开一局。

### 3. 绑定 Bark

打开 Bark，复制该设备的推送 URL（形如 `https://api.day.app/<device_key>`）。在**你自己的终端**运行：

```bash
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py setup
```

输入不可见。成功时 JSON 为 `ok=true`、`status=bound`，只有 `host`，没有密钥。

### 4. 测试推送

```bash
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py test --dry-run
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py test
```

第二条应返回 `status=accepted`，并且手机出现「Grok Notify Me」。没看到手机通知就不要进行下一步。

### 5. 写入全局规则

先看将要写入 `~/.grok/AGENTS.md` 的原文：

```bash
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py agents-rule plan
```

确认 `block` 无误后再写入（当前 Grok 窗口不会热加载，需要再开一局）：

```bash
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py agents-rule commit --authorize
```

装好后不要用 `/notify-me` 发日常通知。卡住时主 Agent 应直接调用 MCP。只有安装、绑定、测试、诊断时才输入 `/notify-me`。

## 诊断

```bash
python3 ~/.grok/plugins/notify-me/scripts/notify_me.py doctor
```

## 卸载

```bash
grok plugin disable notify-me
grok plugin uninstall notify-me --confirm
grok mcp remove notify_me
```

然后编辑 `~/.grok/AGENTS.md`，删掉 `<!-- notify-me:managed:start -->` 到 `<!-- notify-me:managed:end -->` 那一块。Bark 绑定文件在 `~/Library/Application Support/grok-notify-me/binding.json`，不需要可自行删除。
