"""通常スキャンと重複検出の所要時間を測る。

    python bench.py [対象フォルダ]

画面は出さない。**読むだけ**で、何も移動しない。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import duplicates
from app import Scanner


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads"
    if not target.exists():
        print(f"見つかりません: {target}")
        return
    print(f"対象: {target}")

    # --- 通常の容量スキャン ---
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()
    t0 = time.monotonic()
    s = Scanner(target, q, cancel)
    s.start()
    result = None
    while result is None:
        kind, payload = q.get()
        if kind == "done":
            result = payload
        elif kind == "cancelled":
            print("中止された")
            return
    scan_sec = time.monotonic() - t0
    files = result["files"]
    total = result["total_size"]
    print(f"通常スキャン: {scan_sec:.2f} 秒 ・ {len(files):,} ファイル ・ {total / 1024 / 1024:.0f} MB")

    # --- 重複検出（別処理。押したときだけ走るもの）---
    # 「調べる大きさ」の設定ごとに、時間と取りこぼしを見比べる。
    items = [duplicates.DupFile(str(f.path), f.size, f.modified, key=f) for f in files]
    floors = [("すべて", 1), ("1 MB 以上", 1024 ** 2), ("10 MB 以上", 10 * 1024 ** 2)]
    if len(sys.argv) > 2:
        floors = [x for x in floors if x[0] == sys.argv[2]] or floors
    for label, floor in floors:
        t0 = time.monotonic()
        dup = duplicates.find_duplicates(items, min_size=floor)
        sec = time.monotonic() - t0
        print(f"重複検出[{label}]: {sec:.1f} 秒 ・ 確かめた {dup.checked:,} 件"
              f" ・ 読んだ量 {dup.read_bytes / 1024 / 1024:.0f} MB"
              f" ・ 組 {len(dup.groups):,}"
              f" ・ 空く可能性 {dup.reclaimable / 1024 / 1024:.0f} MB")
        lines = dup.skipped.as_lines()
        print("   調べなかった・読めなかったもの: " + ("・".join(lines) if lines else "なし"))


if __name__ == "__main__":
    main()
