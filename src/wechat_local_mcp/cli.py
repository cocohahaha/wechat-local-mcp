from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import account_dir, decrypted_dir, keys_file, load_keys, paths, status
from .crypto import sync_databases


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a local WeChat decrypted snapshot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sync = sub.add_parser("sync")
    sync.add_argument("--keys", default=str(keys_file()))
    sync.add_argument("--out", default=str(decrypted_dir()))
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    p = paths()
    account = account_dir(p.container)
    if account is None:
        raise SystemExit(f"微信账号目录不存在：{p.container / 'xwechat_files'}")
    result = sync_databases(account / "db_storage", load_keys(Path(args.keys)), Path(args.out))
    print(json.dumps(result, ensure_ascii=False, indent=2))
