from __future__ import annotations

from pathlib import Path

from wechat_local_mcp.config import account_dir


def test_account_dir_accepts_xwechat_files_itself(tmp_path: Path) -> None:
    xwechat = tmp_path / "xwechat_files"
    account = xwechat / "wxid_example"
    (account / "db_storage").mkdir(parents=True)

    assert account_dir(xwechat) == account


def test_account_dir_accepts_xwechat_files_parent(tmp_path: Path) -> None:
    account = tmp_path / "xwechat_files" / "wxid_example"
    (account / "db_storage").mkdir(parents=True)

    assert account_dir(tmp_path) == account
