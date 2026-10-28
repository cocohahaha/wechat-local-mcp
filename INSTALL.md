# Agent 安装指南 / Agent installation guide

目标：从这个 GitHub 链接完成**当前用户电脑**的安装，验证 MCP 连接，再单独确认历史消息准备状态。不要照抄维护者的路径，不要把“connected”解释为“全部聊天已可读”。

## 1. 最小安装

需要 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和一个支持本地 stdio MCP 的客户端。推荐 Python 3.12，uv 可自动安装它。必须在微信数据所在的电脑上运行，云端 Agent 不能直接读取用户电脑的微信。

```sh
git clone https://github.com/cocohahaha/wechat-local-mcp.git
cd wechat-local-mcp
```

macOS/Linux 在克隆后运行 `bash scripts/install.sh`；Windows 运行 `powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1`。脚本一次完成基础安装、握手和配置生成，不默认注册。Apple 芯片的新环境明确选择 arm64 Python，避免误用已安装的 Intel/Rosetta Python。已有虚拟环境不会自动换架构。若已有 Rosetta 环境安装失败，请保留原目录，先在新的克隆目录用脚本建立原生环境，再更新客户端配置。

Intel macOS 的上游 `cryptography` 49 已移除 x86_64 支持和预编译包（[官方变更记录](https://cryptography.io/en/49.0.0/changelog/)），可能需要源码编译且不保证成功。本项目不为省安装时间降级加密依赖；轻量安装已验证于 Apple Silicon，Intel Mac 请勿视为开箱即用。

不要默认安装 `keys` / `ui` / `windows` / `media` / `test`。`--inexact` 会保留已有可选依赖，首次只安装基础包。模型下载不属于解密或 MCP 启动流程。

升级已有安装时不要强制更换 Python：用 `uv sync --locked --inexact --python .venv/bin/python`（Windows 用 `.venv\Scripts\python.exe`）。安装脚本会自动保留已有解释器。换解释器可能导致 uv 重建虚拟环境，此时需要重新选择原有的可选依赖。

macOS/Linux 验证并生成配置：

```sh
.venv/bin/python -m wechat_local_mcp.setup bootstrap
```

Windows PowerShell：

```powershell
.venv\Scripts\python.exe -m wechat_local_mcp.setup bootstrap
```

向导返回纯 JSON：`mcp.connected`、工具数量、`history.snapshot_available`、`history.keys_file_exists`，以及 `mcpServers` 配置。握手从临时目录启动真实 MCP，测试不依赖当前工作目录；不读取聊天正文、不输出密钥值。

## 2. 注册客户端（用户已要求接入时）

macOS 一步安装到 Claude Code：

```sh
bash scripts/install_claude_code.sh
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1 -Client claude -Register
```

也可在完成第 1 步后注册。以下 `PYTHON` 代表上面该操作系统的虚拟环境 Python 路径：

```text
PYTHON -m wechat_local_mcp.setup bootstrap --client claude --register
PYTHON -m wechat_local_mcp.setup bootstrap --client codex --register
```

只运行所需客户端的那条。未传 `--register` 时绝不修改客户端。若发现不同的同名配置，向导保留它并报告 `registration_exists`；先检查 `claude mcp get wechat-local-mcp` 或 `codex mcp get wechat-local-mcp`，用户确实需要替换时再移除该条后重试。不要清空整个配置文件。相同配置重复执行是幂等的。

其他客户端使用向导输出的 `mcpServers` 对象；有些客户端字段不同，请适配到它的本地 stdio 配置。启动命令必须是**当前虚拟环境 Python 的绝对路径**，参数为 `-m wechat_local_mcp.server --transport stdio`。不要解析 Python 的符号链接到系统解释器；不要用 `uv run` / `uvx` 作为每次 MCP 握手启动器。

自定义数据目录时，在 bootstrap 上设置 `--container-dir`、`--decrypted-dir`、`--keys-file`，这些路径会写入生成配置的 `env`。已设定的 `WECHAT_*` 路径变量也会带入。不要把密钥内容填到命令行、聊天或 Git。

Claude Code 注册遵循[官方 MCP 文档](https://code.claude.com/docs/en/mcp)。注册后重启客户端或重新连接，检查 `/mcp` 与 `wechat_status`。旧版项目 `.mcp.json` 可能覆盖用户配置，请检查重复条目。移动安装目录后必须更新配置。

## 3. 选择读取路线

| 用户已有的条件 | 下一步 | 能做到什么 |
| --- | --- | --- |
| 已解密快照 | 将 `--decrypted-dir` 指向含 `contact/`、`message/` 的快照目录 | 精确历史查询；不必重新解密 |
| 当前账号有效 keys.json | 完全退出微信，按第 4 步同步 | 精确历史查询 |
| 没有密钥 | 按需安装 `ui`（macOS）或 `windows`（Windows），调用 `wechat_ui_diagnose` | 当前可见窗口 OCR，不等于完整历史 |
| 需要首次获取密钥 | 读 README 的一次性密钥准备；macOS 才有附带捕获辅助脚本 | 受微信版本和本机权限限制，不保证一次成功 |

不可自动关闭微信、修改微信签名、关闭系统保护或清理权限来“修复安装”。任何需要退出应用/登录/系统授权的步骤都应先解释并交由用户确认。密钥捕获只用于用户有权访问的本机账号；不要改日常 `/Applications/WeChat.app`。

Windows 4.x 数据路径可能是自定义目录或重定向的“文档”目录；自动发现失败时从微信设置确认位置，再设置 `--container-dir` 为 `xwechat_files` 或其父目录。**Windows 不附带自动捕获密钥工具**。Linux 可查询已有快照，但不提供微信 UI 后端。只保证当前适配的数据库格式，不保证任意微信版本。

## 4. 快速、可恢复的解密

在虚拟环境下执行（Windows 用对应的 Python）：

```sh
.venv/bin/python -m wechat_local_mcp.cli sync --dry-run
.venv/bin/python -m wechat_local_mcp.cli sync
```

- 默认 `--profile chat`：联系人 + `message_<数字>.db`，足够聊天查询。
- `--profile media`：chat + 语音/表情库，用于完整媒体理解。
- `--profile all`：包括当前查询不使用的索引及其他库，通常不必运行。
- `--force`：忽略增量缓存重建；不是日常推荐选项。
- `--db-dir /账号/db_storage --out /独立输出目录`：多账号时明确选择；不同账号不要共用快照目录。MCP 默认使用自动发现的最近修改账号，因此多账号历史查询优先指定已准备好的 `--decrypted-dir`，同步用 CLI 明确选账号。

检查 JSON：`complete=true`，并查看 `decrypted`、`unchanged`、`skipped_no_key`、`failed`、`elapsed_seconds`、`bytes_processed`。`count=0` 且 `unchanged_count>0` 是正常的快速同步。失败返回退出码 2；`dry_run` 只是计划，不代表快照已创建。

检测到非空 WAL/journal 会报告 `source_busy`。先完全退出微信，让微信正常落盘，再重试；**不要删除 WAL 文件**，它可能包含尚未保存的新消息。已有快照会保留，但不能声称它已更新。若正常退出后仍有日志，保留现场排查，不绕过检查。

首次仍需读取每个选中库；后续只处理变化的文件。文件正在解密时中断，该文件下次重做，已经完成的文件会跳过。更换密钥、源文件变化、输出丢失/变化、缓存损坏均会触发重建。详见 [PERFORMANCE.md](PERFORMANCE.md)。

## 5. 可选能力

```sh
uv sync --locked --inexact --extra ui --python .venv/bin/python       # macOS 窗口 OCR
uv sync --locked --inexact --extra keys --python .venv/bin/python     # macOS 密钥捕获辅助依赖
uv sync --locked --inexact --extra media --python .venv/bin/python    # 本机语音/图片处理
```

Windows 使用 `uv sync --locked --inexact --extra windows --python .venv\Scripts\python.exe` 安装窗口 OCR；媒体依赖将 `--extra windows` 换成 `--extra media`。这些命令沿用已安装环境的解释器，不重建其他 Python 版本的环境。

媒体依赖、模型与微信缓存可能很大，按需求安装。语音模型第一次使用可能联网下载；语音/图片内容在本机处理。普通读取结果交给云模型后仍受客户端隐私策略约束。不要将 `.db`、keys 文件、聊天导出、用户绝对路径或诊断输出提交到公共仓库。

## 6. Agent 的完成标准

1. bootstrap 返回 `mcp.connected=true`；客户端也确认连接正常。
2. 说明历史快照是否可用；没有密钥/权限时明确列出所缺条件。
3. 用户允许读取时，调用 `wechat_status` 和一次有限数量查询验证，不导出整个历史。
4. 报告真实结果，不把配置生成、注册成功、MCP 连接、历史可读混为一谈。Windows 脚本和代码路径有测试，但发布者未在每个用户的 Windows/微信版本上实测。
