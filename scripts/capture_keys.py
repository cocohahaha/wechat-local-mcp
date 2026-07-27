#!/usr/bin/env python3
"""Capture SQLCipher keys from the user's own running WeChat process.

This is an optional, one-time preparation helper. It never writes to the
WeChat app bundle and only writes the derived keys to the configured local
keys file. macOS may require Full Disk Access/Developer Tools permission.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


HOOK = r"""
function hex(buf) {
  return Array.from(new Uint8Array(buf)).map(function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
}
var fn = (typeof Module.findGlobalExportByName === 'function')
  ? Module.findGlobalExportByName('CCKeyDerivationPBKDF')
  : Module.findExportByName(null, 'CCKeyDerivationPBKDF');
if (fn === null) {
  send({kind: 'error', message: 'CCKeyDerivationPBKDF export not found'});
} else {
  send({kind: 'ready'});
  Interceptor.attach(fn, {
    onEnter: function (args) {
      this.salt = args[3];
      this.saltLen = args[4].toInt32();
      this.out = args[7];
      this.outLen = args[8].toInt32();
    },
    onLeave: function (retval) {
      if (retval.toInt32() !== 0 || this.salt.isNull() || this.out.isNull() || this.saltLen !== 16 || this.outLen !== 32) return;
      try {
        send({kind: 'derived', salt: hex(this.salt.readByteArray(this.saltLen)), key: hex(this.out.readByteArray(this.outLen))});
      } catch (e) {}
    }
  });
}
"""


def salt_map(db_root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(db_root.rglob("*.db")):
        try:
            data = path.read_bytes()[:16]
        except OSError:
            continue
        if len(data) == 16 and data[:16] != b"SQLite format 3\x00":
            out[data.hex()] = str(path.relative_to(db_root))
    return out


def running_pid() -> int | None:
    result = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def existing_keys(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): str(key).lower()
        for name, key in payload.items()
        if isinstance(name, str)
        and isinstance(key, str)
        and len(key) == 64
        and all(char in "0123456789abcdefABCDEF" for char in key)
    }


def save_keys(path: Path, keys: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(keys, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="capture WeChat database keys through a one-time local Frida hook")
    parser.add_argument("--pid", type=int, help="attach to a running WeChat process")
    parser.add_argument("--spawn", help="spawn a WeChat executable before installing the hook")
    parser.add_argument("--db-dir", required=True, help="the account's db_storage directory")
    parser.add_argument("--out", default=str(Path.home() / ".config/wechat-local-mcp/keys.json"))
    args = parser.parse_args()
    if bool(args.pid) == bool(args.spawn):
        parser.error("choose exactly one of --pid or --spawn")
    try:
        import frida
    except ImportError:
        print("缺少 frida：请先在项目环境执行 uv sync --extra keys", file=sys.stderr)
        return 2
    salts = salt_map(Path(args.db_dir).expanduser().resolve())
    if not salts:
        print("没有找到加密数据库盐值；请确认 --db-dir 指向 db_storage", file=sys.stderr)
        return 2
    device = frida.get_local_device()
    pid = device.spawn([args.spawn]) if args.spawn else args.pid
    try:
        session = device.attach(pid)
    except Exception as exc:
        message = str(exc)
        if "Permission" in type(exc).__name__ or "access process" in message:
            print(
                "无法附加微信进程。macOS 拒绝了 task_for_pid；请用 sudo 重试，"
                "或对一个专用的微信副本授予 get-task-allow 后再用 --spawn。"
                "不要对你日常使用的 /Applications/WeChat.app 做重签名。",
                file=sys.stderr,
            )
        else:
            print(f"Frida attach failed: {exc}", file=sys.stderr)
        return 3
    script = session.create_script(HOOK)
    output_path = Path(args.out).expanduser()
    found = existing_keys(output_path)

    def on_message(message, _data):
        if message.get("type") == "error":
            print(
                "Frida script error: "
                + str(message.get("description") or message.get("stack") or message),
                file=sys.stderr,
            )
            return
        payload = message.get("payload") if message.get("type") == "send" else None
        if not isinstance(payload, dict):
            return
        if payload.get("kind") == "error":
            print(payload.get("message"), file=sys.stderr)
        elif payload.get("kind") == "ready":
            print("hook ready; keep WeChat open and log in/open chats to trigger database key derivation", file=sys.stderr)
        elif payload.get("kind") == "derived":
            rel = salts.get(str(payload.get("salt")))
            key = str(payload.get("key", ""))
            if rel and len(key) == 64:
                found[rel] = key
                print(f"captured {rel}", file=sys.stderr)

    script.on("message", on_message)
    script.load()
    if args.spawn:
        device.resume(pid)
    print("按 Ctrl-C 停止并保存已捕获的密钥。", file=sys.stderr)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        save_keys(output_path, found)
        script.unload()
        session.detach()
    print(json.dumps({"captured": len(found), "out": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
