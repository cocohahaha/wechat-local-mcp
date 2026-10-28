"""Reproducible synthetic I/O benchmark. Never opens WeChat or real keys.

The fixture contains a valid empty SQLite schema page followed by encrypted
synthetic pages. It measures the page crypto/I/O path, not application queries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import tracemalloc

from Crypto.Cipher import AES
from wechat_local_mcp.crypto import PAGE_SIZE, RESERVE_SIZE, SQLITE_HEADER, sync_databases

KEY = bytes(range(32))  # Synthetic test key, not a credential.


def fixture(path: Path, mib: int) -> None:
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA page_size=4096")
    db.execute("VACUUM")
    db.close()
    first = bytearray(path.read_bytes())
    first[20] = RESERVE_SIZE
    first[105:107] = (PAGE_SIZE - RESERVE_SIZE).to_bytes(2, "big")
    iv = bytes(range(16))
    reserve = iv + b"\x00" * 64
    initial = b"0123456789abcdef" + AES.new(KEY, AES.MODE_CBC, iv).encrypt(bytes(first[16:-80])) + reserve
    page = AES.new(KEY, AES.MODE_CBC, iv).encrypt(b"\x00" * (PAGE_SIZE - RESERVE_SIZE)) + reserve
    with path.open("wb") as stream:
        stream.write(initial)
        for _ in range(mib * 1024 * 1024 // PAGE_SIZE - 1):
            stream.write(page)


def legacy_sync(source: Path, destination: Path) -> None:
    """v0.4.1 full-read, full-decrypt, full-write algorithm, without private data."""
    data = source.read_bytes()
    decoded = bytearray()
    for number in range(len(data) // PAGE_SIZE):
        page = data[number * PAGE_SIZE:(number + 1) * PAGE_SIZE]
        prefix = 16 if number == 0 else 0
        plain = AES.new(KEY, AES.MODE_CBC, page[-80:-64]).decrypt(page[prefix:-80])
        if number == 0:
            decoded += SQLITE_HEADER
        decoded += plain
        decoded += page[-80:]
    destination.write_bytes(bytes(decoded))


def measure(callback, memory=False):
    if memory:
        tracemalloc.start()
    start = time.perf_counter()
    outcome = callback()
    if isinstance(outcome, dict) and not outcome.get("complete"):
        raise RuntimeError("benchmark sync did not complete: " + json.dumps(outcome.get("failed", [])))
    result = {"seconds": round(time.perf_counter() - start, 6)}
    if isinstance(outcome, dict):
        result.update(bytes_processed=outcome["bytes_processed"], unchanged_count=outcome["unchanged_count"])
    if memory:
        result["peak_python_mib"] = round(tracemalloc.get_traced_memory()[1] / 1048576, 2)
        tracemalloc.stop()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=64)
    parser.add_argument("--memory", action="store_true", help="measure Python allocations; tracing slows timings")
    args = parser.parse_args()
    if not 1 <= args.mib <= 1024:
        parser.error("--mib must be 1..1024")
    with tempfile.TemporaryDirectory(prefix="wechat-sync-benchmark-") as directory:
        root = Path(directory)
        source = root / "source/message/message_0.db"
        fixture(source, args.mib)
        keys = {"message_0.db": KEY.hex()}
        legacy = lambda: legacy_sync(source, root / "legacy.db")
        current = lambda: sync_databases(root / "source", keys, root / "output", profile="chat")
        results = {"synthetic_mib": args.mib, "memory_tracing": args.memory,
                   "legacy_first": measure(legacy, args.memory), "legacy_repeat": measure(legacy, args.memory),
                   "new_first": measure(current, args.memory), "new_unchanged": measure(current, args.memory)}
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
