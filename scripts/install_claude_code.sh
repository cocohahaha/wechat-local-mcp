#!/bin/zsh
set -eu

script_dir="${0:A:h}"
project_dir="${script_dir:h}"
mcp_bin="$project_dir/.venv/bin/wechat-local-mcp"

if ! command -v claude >/dev/null 2>&1; then
  echo "未找到 Claude Code。请先安装并运行 claude。" >&2
  exit 1
fi

if [[ ! -x "$mcp_bin" ]]; then
  echo "MCP 尚未安装。请先在项目目录运行：uv sync --extra ui --extra media" >&2
  exit 1
fi

claude mcp remove --scope user wechat-local-mcp >/dev/null 2>&1 || true
claude mcp add --scope user --transport stdio wechat-local-mcp -- \
  "$mcp_bin" --transport stdio

echo
echo "已注册。请完全退出并重新打开 Claude Code，然后运行 /mcp 查看状态。"
