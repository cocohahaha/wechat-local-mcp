$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先按照 https://docs.astral.sh/uv/ 安装 uv。"
}

uv sync --extra windows --extra test --python 3.12
uv run pytest -q

Write-Host ""
Write-Host "Windows 依赖与离线测试已完成。"
Write-Host "请先登录微信并恢复主窗口，然后运行："
Write-Host "  uv run wechat-local-mcp --transport stdio"
Write-Host "首次可让 MCP 客户端调用 wechat_ui_diagnose 检查窗口和本机 OCR。"
