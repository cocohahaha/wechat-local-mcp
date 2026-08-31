# wechat-local-mcp

一个只读的本地 MCP 服务，用来把你自己的 macOS 或 Windows 微信聊天变成可搜索、可提取待办的上下文。

它分成四层：

1. 精确历史层：把已获得密钥的本地数据库解密成独立快照，再进行全文搜索和待办提取。
2. 跨版本 UI 层：在数据库适配失效或尚无密钥时，用 macOS Vision 或 Windows.Media.Ocr 读取当前微信窗口。
3. 媒体理解层：图片/截图使用本机 OCR；表情包读取消息说明和本地表情库；语音从 `media_0.db` 读取微信 SILK 并在本机转写；合并转发消息解压为分层 JSON。
4. MCP 查询层：默认返回 JSON，不向微信发消息、不修改微信数据库。

这样微信升级时，只需要替换准备层；`wechat_list_chats`、`wechat_search_messages`、`wechat_find_todos` 等工具不需要跟着重写。

## 数据位置与版本兼容

`wechat_status` 会动态读取当前微信版本。聊天数据库通常位于：

`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage/`

较新的 macOS 微信可能为每个数据库使用独立密钥，旧版“一个 key 解所有库”的工具不能直接复用。微信升级后，数据库结构或密钥派生方式仍可能变化。工具不会覆盖 `/Applications/WeChat.app`，也不会对日常微信做重签名。

Windows 微信 4.x 的账号数据通常位于“文档”目录下的 `xwechat_files`。如微信设置了自定义存储位置，可用 `WECHAT_CONTAINER_DIR` 指向 `xwechat_files` 本身或它的父目录。Windows 版优先提供不依赖数据库密钥的 UI/OCR 读取；已有明文快照仍可使用全部精确查询工具。

## 安全与隐私

- 仓库不包含真实密钥、微信数据库、聊天导出、账号目录或本机虚拟环境。
- MCP 服务自身不要求网络访问，只读取本机解密快照。
- 图片、表情和语音内容不会上传；语音模型在本机运行。首次使用某个 Whisper 模型时会从 Hugging Face 下载模型文件。
- 图片/截图 OCR、表情识别与语音转写会缓存为派生 JSON，默认位于 `~/Library/Application Support/wechat-local-vault/media-text/`。
- 当 MCP 客户端把查询结果交给模型分析时，被选中的聊天内容会进入该客户端的模型上下文；请按所用客户端的隐私策略判断是否使用。
- 密钥捕获脚本只应当用于你有权访问的本机微信账号。
- `keys.json` 和明文快照默认保存在项目目录之外的用户配置目录中，并限制为仅当前系统账户可读；不要提交或分享它们。

## 安装

### macOS

在本目录执行：

```bash
uv sync --extra keys --extra ui --extra media --extra test --python /opt/homebrew/bin/python3.12
```

查看状态：

```bash
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync status
```

### Windows 10/11

安装 Python 3.11 或 3.12 与 [uv](https://docs.astral.sh/uv/)，然后在 PowerShell 中运行：

```powershell
uv sync --extra windows --extra media --extra test --python 3.12
uv run wechat-local-mcp --transport stdio
```

也可以直接运行 `powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1` 完成依赖安装和离线测试。

Windows UI 读取使用系统自带的 `Windows.Media.Ocr`，截图只存在于进程的临时目录，识别结束后自动删除，不调用云端 OCR。若诊断提示没有中文 OCR，请在“设置 → 时间和语言 → 语言和区域”中安装简体中文语言功能。

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

### 截图快捷键或录屏权限异常

如果准备密钥后，微信的截图快捷键失效，或者系统设置中的“录屏与系统音频录制”权限反复丢失，常见原因是旧版脚本把辅助副本放在 `~/Applications`：辅助副本与正式微信使用同一个 Bundle ID，但签名不同，macOS 的 LaunchServices/TCC 可能把两者误认为同一个应用，导致快捷键打开错误副本，或把录屏权限绑定到辅助副本的签名。

修复步骤：

1. 完全退出正式微信和辅助副本。
2. 把旧的 `~/Applications/WeChatKeyCapture.app` 移到 `$HOME/Library/Application Support/wechat-local-mcp/WeChatKeyCapture.app`，或重新运行 `scripts/prepare_capture_copy.sh`。
3. 取消辅助副本的 LaunchServices 注册：

   ```bash
   LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
   "$LSREGISTER" -u "$HOME/Library/Application Support/wechat-local-mcp/WeChatKeyCapture.app"
   ```

4. 重置正式微信的录屏授权，然后重新打开正式微信：

   ```bash
   tccutil reset ScreenCapture com.tencent.xinWeChat
   open -a /Applications/WeChat.app
   ```

5. 在系统设置的“隐私与安全性 → 录屏与系统音频录制”中重新允许微信，再测试微信截图快捷键。

新版 `prepare_capture_copy.sh` 会默认把辅助副本放在应用支持目录，并在创建后主动取消其 LaunchServices 注册，避免再次抢占正式微信。辅助副本仍可通过其可执行文件的绝对路径启动，不影响密钥捕获。

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

Windows PowerShell 示例：

```powershell
codex mcp add wechat-local -- `
  "$PWD\.venv\Scripts\wechat-local-mcp.exe" `
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
- `wechat_read_chat`：按昵称、备注、群名或微信 ID 读取消息，并自动理解图片、截图、表情包、语音及合并转发消息。
- `wechat_search_messages`：按关键词、聊天和时间范围搜索普通消息及已自动转换的媒体文字。
- `wechat_recent_messages`：汇总最近消息，并自动转换其中的图片、截图、表情包和语音。
- `wechat_find_todos`：用可解释的中英文行动项启发式找待办候选。
- `wechat_chat_summary`：自动转换媒体后返回参与者统计、待办候选和消息。
- `wechat_media_status`：检查图片/表情缓存、本机 OCR、SILK 解码和离线 Whisper 是否就绪。
- `wechat_ui_diagnose`：诊断屏幕录制/OCR 只读兜底。
- `wechat_list_recent_chats`：数据库快照不可用时读取当前窗口可见会话。
- `wechat_read_chat_ui`：数据库快照不可用时用本机 OCR 读取聊天。
- `wechat_find_todos_ui`：对 OCR 结果做待办候选提取。

所有微信访问都是只读的；普通消息读取工具只在独立目录自动写入派生文字缓存。`wechat_find_todos` 只返回候选，不会替你创建任务、发消息或修改微信。

### 图片、截图、表情包、合并转发消息和语音

先准备最新明文快照并安装媒体依赖：

```bash
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync sync
uv sync --extra ui --extra media --python /opt/homebrew/bin/python3.12
```

直接调用原来的消息读取工具，无需先调用单独的媒体工具：

```json
{
  "chat": "聊天名称",
  "limit": 50,
  "response_format": "json"
}
```

`wechat_read_chat`、`wechat_recent_messages` 和 `wechat_chat_summary` 遇到图片、截图、表情包或语音时会自动处理。识别成功后：

- 消息的 `content` 直接变成图片/截图 OCR、表情说明或语音转写正文；
- `original_content` 保留原来的 `[图片]`、`[表情包]`、`[语音]` 占位信息；
- `content_source` 标识 `image_ocr`、`screenshot_ocr`、`sticker_recognition` 或 `voice_transcript`；
- `message_kind` 会区分 `image`、启发式的 `screenshot_candidate`、`sticker` 和 `voice`；
- `media` 保留引擎、模型、置信信息、分段和 `cached` 状态；
- 无法识别时的具体原因，例如“图片未在本机缓存”。

`wechat_search_messages` 会同时搜索普通文本和这些自动生成的媒体文字缓存，不需要 Agent 选择另一套搜索工具。

截图在微信数据库里仍属于普通图片，因此 `screenshot_candidate` 是根据 OCR 文字密度和长宽比给出的可解释启发式分类，并非微信官方字段。表情包优先读取消息里的说明，其次查询本地 `emoticon.db`；若缓存文件是微信专有加密格式且没有说明，会返回 `unavailable`，不会联网下载或猜测含义。

类型 19 的合并转发/压缩聊天会返回 `forwarded.items` 数组。每个子项带 `data_type`、`kind`、`sender`、`time`、`content`，以及可用的媒体摘要；支持文本、图片、语音、视频、文件、链接、位置、嵌套聊天、表情包、小程序和视频号等类型。最多展开 200 条，超出时设置 `truncated=true`。

图片优先读取微信本机的高清/中图缓存，找不到时使用缩略图。若结果提示图片未缓存，请先在微信中打开一次原图。Apple Silicon macOS 默认使用 MLX Whisper；Windows、Linux 和 Intel macOS 使用 faster-whisper。可设置：

```bash
export WECHAT_WHISPER_MODEL=mlx-community/whisper-small-mlx
export WECHAT_FASTER_WHISPER_MODEL=small
export WECHAT_MEDIA_TEXT_DIR="$HOME/Library/Application Support/wechat-local-vault/media-text"
```

语音转写属于模型推断，可能出现漏字或误字，因此返回值固定带 `review_required=true`；重要内容仍应对照原语音确认。

macOS OCR 兜底需要：

```bash
uv sync --extra ui --python /opt/homebrew/bin/python3.12
```

并在系统设置中允许运行 MCP 的终端使用“屏幕与系统音频录制”。OCR 是当前屏幕的近似读取，长期历史、精确 sender 和全文搜索仍以数据库快照后端为准。

Windows OCR 兜底需要：

```powershell
uv sync --extra windows --python 3.12
```

支持新版 `Weixin.exe` 和旧版 `WeChat.exe`。调用读取工具时应保持微信已登录、主窗口未最小化；工具可能把微信切到前台、打开指定聊天并向上滚动，但不会在输入框发送内容。OCR 是当前窗口的近似读取，长期历史、精确 sender 和全文搜索仍以数据库快照后端为准。

## 验证

```bash
uv run --python /opt/homebrew/bin/python3.12 pytest
uv run --python /opt/homebrew/bin/python3.12 wechat-local-sync status
```

Windows：

```powershell
uv run pytest
uv run wechat-local-sync status
```

若没有明文快照，MCP 会返回下一步提示，不会把加密数据库误当成可读内容。

## 许可证与声明

本项目采用 [MIT License](LICENSE)。

本项目是社区开发的非官方工具，与腾讯或微信团队无隶属或背书关系。“微信”和“WeChat”及相关标识属于其各自权利人。
