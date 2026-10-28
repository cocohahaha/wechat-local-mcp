#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v claude >/dev/null 2>&1; then
  echo "未找到 Claude Code。请先安装并运行 claude。" >&2
  exit 1
fi

exec bash "$script_dir/install.sh" --client claude --register "$@"
