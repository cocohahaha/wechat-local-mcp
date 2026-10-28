#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo "Please install uv: https://docs.astral.sh/uv/" >&2
  exit 1
fi
cd -- "$project_dir"
python_request="3.12"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  # Preserve the user's existing interpreter and optional dependencies.
  python_request="$project_dir/.venv/bin/python"
elif [[ "$(uname -s)" == "Darwin" && "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" == "1" ]]; then
  # A plain 3.12 request may select a pre-existing Rosetta Python on Apple Silicon.
  python_request="cpython-3.12-macos-aarch64-none"
fi
uv sync --locked --inexact --python "$python_request" >&2
exec "$project_dir/.venv/bin/python" -m wechat_local_mcp.setup bootstrap "$@"
