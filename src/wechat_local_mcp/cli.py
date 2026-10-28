from __future__ import annotations

import argparse
import json
import sys
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
    sync.add_argument("--db-dir", type=Path, help="explicit account db_storage directory (useful with multiple accounts)")
    sync.add_argument("--profile", choices=("chat", "media", "all"), default="chat")
    sync.add_argument("--force", action="store_true", help="rebuild even unchanged databases")
    sync.add_argument("--dry-run", action="store_true", help="report work without decrypting or writing files")
    sync.add_argument("--quiet", action="store_true", help="hide stderr progress; stdout always remains JSON")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    p = paths()
    account = account_dir(p.container)
    db_root = args.db_dir or (account / "db_storage" if account else None)
    if db_root is None:
        print(json.dumps({"complete": False, "error": "account_not_found", "next_step": "Set --db-dir to your account's db_storage directory."}))
        raise SystemExit(2)
    try:
        key_path = Path(args.keys).expanduser()
        keys = load_keys(key_path) if key_path.exists() else {}
        result = sync_databases(
            db_root, keys, Path(args.out), profile=args.profile, force=args.force, dry_run=args.dry_run,
            progress=None if args.quiet else lambda event: print(
                f"Decrypting {event['file']} ({event['bytes'] / 1048576:.1f} MiB)", file=sys.stderr, flush=True),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"complete": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
