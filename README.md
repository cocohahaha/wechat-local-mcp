# wechat-local-mcp

一个只读的本地 MCP 服务，用来把你自己的 macOS 微信聊天变成可搜索、可提取待办的上下文。

它分成两层：

1. 一次性准备层：从本机微信账号目录捕获每个数据库的 32 字节密钥，并把数据库解密成独立快照。
2. MCP 查询层：只读取快照，不向微信发消息、不修改微信数据库、不联网。

这样微信升级时，只需要替换准备层；`wechat_list_chats`、`wechat_search_messages`、`wechat_find_todos` 等工具不需要跟着重写。

## 数据位置与版本兼容

`wechat_status` 会动态读取当前微信版本。聊天数据库通常位于：

`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage/`

较新的 macOS 微信可能为每个数据库使用独立密钥，旧版“一个 key 解所有库”的工具不能直接复用。微信升级后，数据库结构或密钥派生方式仍可能变化。工具不会覆盖 `/Applications/WeChat.app`，也不会对日常微信做重签名。

## 安全与隐私

- 仓库不包含真实密钥、微信数据库、聊天导出、账号目录或本机虚拟环境。
- MCP 服务自身不要求网络访问，只读取本机解密快照。
- 当 MCP 客户端把查询结果交给模型分析时，被选中的聊天内容会进入该客户端的模型上下文；请按所用客户端的隐私策略判断是否使用。
- 密钥捕获脚本只应当用于你有权访问的本机微信账号。
- `keys.json` 和明文快照默认保存在项目目录之外的用户配置目录中，并限制为仅当前系统账户可读；不要提交或分享它们。

## 安装

在本目录执行：

```bash
uv sync --extra keys --extra ui --extra test --python /opt/homebrew/bin/python3.12
```

查看状态：

```bash
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync status
```

## 一次性准备密钥

如果你已有可用的 `keys.json`，放到：

`~/.config/wechat-local-mcp/keys.json`

格式是数据库相对路径（或文件名）到 64 位十六进制密钥的映射，例如：

```json
{
  "contact/contact.db": "REPLACE_WITH_64_HEX_CHARACTERS",
  "message/message_0.db": "REPLACE_WITH_64_HEX_CHARACTERS"
}
```

如果没有 keys 文件，项目附带一个不改日常微信安装包的 Frida 辅助脚本。先安装可选依赖：

```bash
uv sync --extra keys --python /opt/homebrew/bin/python3.12
```

直接附加日常微信在部分 macOS 版本上会被系统拒绝。如果本机允许附加，可以退出并重新打开微信后运行：

```bash
uv run --python /opt/homebrew/bin/python3.12 python scripts/capture_keys.py \
  --pid "$(pgrep -x WeChat | head -1)" \
  --db-dir "$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage"
```

这只把捕获到的派生 key 合并写入 `~/.config/wechat-local-mcp/keys.json`，不会清除之前已捕获的有效 key。如果 macOS 报 `task_for_pid` 或权限错误，请使用专用副本：

```bash
zsh scripts/prepare_capture_copy.sh
```

退出日常微信后，用 `--spawn "$HOME/Library/Application Support/wechat-local-mcp/WeChatKeyCapture.app/Contents/MacOS/WeChat"` 运行捕获脚本。脚本会在微信启动时安装只读派生函数观察器；当前版本通常无需再次扫码。辅助副本放在应用支持目录中，避免被 macOS 当作第二个日常微信注册；脚本不会替你修改系统设置，也不会对日常使用的 `/Applications/WeChat.app` 重签名。

> 密钥捕获是一次性环境准备，当前微信版本或下次升级后可能需要重新做。不要把 `keys.json`、明文数据库或聊天导出文件提交到 Git。

## 解密本地快照

建议先完全退出微信，再执行：

```bash
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync sync
```

默认输出到：

`~/Library/Application Support/wechat-local-vault/decrypted/current/`

也可以通过环境变量覆盖：

```bash
export WECHAT_CONTAINER_DIR="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents"
export WECHAT_DECRYPTED_DIR="$HOME/Library/Application Support/wechat-local-vault/decrypted/current"
export WECHAT_KEYS_FILE="$HOME/.config/wechat-local-mcp/keys.json"
```

## 配置 MCP

以 stdio 方式添加到支持 MCP 的客户端：

```json
{
  "mcpServers": {
    "wechat-local": {
      "command": "/绝对路径/outputs/wechat-local-mcp/.venv/bin/wechat-local-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

或直接运行：

```bash
uv run --python /opt/homebrew/bin/python3.12 wechat-local-mcp --transport stdio
```

Codex CLI 可直接注册：

```bash
codex mcp add wechat-local -- \
  "/绝对路径/outputs/wechat-local-mcp/.venv/bin/wechat-local-mcp" \
  --transport stdio
```

## 响应格式

所有读取、搜索、汇总和待办工具默认使用 `response_format="json"`。响应同时包含：

- JSON 文本内容，兼容只读取 MCP `content` 的客户端。
- 原始对象形式的 `structuredContent`，方便支持结构化工具输出的客户端直接消费。

如需适合人工阅读的文本，可以显式传入 `response_format="markdown"`；此时 `structuredContent` 仍会保留。

## 可用工具

- `wechat_status`：检查微信版本、数据库、密钥与快照状态。
- `wechat_sync`：按 keys.json 解密数据库到独立快照目录。
- `wechat_list_chats`：列出联系人和群聊，支持分页。
- `wechat_read_chat`：按昵称、备注、群名或微信 ID 读取消息。
- `wechat_search_messages`：按关键词、聊天和时间范围搜索消息。
- `wechat_recent_messages`：汇总最近消息。
- `wechat_find_todos`：用可解释的中英文行动项启发式找待办候选。
- `wechat_chat_summary`：返回参与者统计和待办候选，供模型进一步总结。
- `wechat_ui_diagnose`：诊断屏幕录制/OCR 只读兜底。
- `wechat_list_recent_chats`：数据库快照不可用时读取当前窗口可见会话。
- `wechat_read_chat_ui`：数据库快照不可用时用本机 OCR 读取聊天。
- `wechat_find_todos_ui`：对 OCR 结果做待办候选提取。

所有查询工具都是只读的；`wechat_find_todos` 只返回候选，不会替你创建任务、发消息或修改微信。

OCR 兜底需要：

```bash
uv sync --extra ui --python /opt/homebrew/bin/python3.12
```

并在系统设置中允许运行 MCP 的终端使用“屏幕与系统音频录制”。OCR 是当前屏幕的近似读取，长期历史、精确 sender 和全文搜索仍以数据库快照后端为准。

## 验证

```bash
uv run --python /opt/homebrew/bin/python3.12 pytest
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync status
```

若没有明文快照，MCP 会返回下一步提示，不会把加密数据库误当成可读内容。

## 许可证与声明

本项目采用 [MIT License](LICENSE)。

本项目是社区开发的非官方工具，与腾讯或微信团队无隶属或背书关系。“微信”和“WeChat”及相关标识属于其各自权利人。
