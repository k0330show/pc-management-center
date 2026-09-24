"""完全一致の重複ファイルを探す。

このモジュールは **通常の容量スキャンとは無関係**である。ユーザーが
「重複ファイルを探す」を押したときだけ動き、スキャン済みの一覧を入力に取る。

## 何をもって「重複」とするか

**中身が 1 バイトも違わないこと**だけを重複とする。名前が同じでも中身が違えば
別物だし、名前が違っても中身が同じなら重複である。名前・更新日時・作成者は
判定に使わない。

## 読む量を抑える

全ファイルを丸ごと読むと時間もディスクも食う。三段で絞る。

    1. 容量が同じもの同士だけを候補にする（違えば中身も違う。ここでは何も開かない）
    2. 先頭と末尾のひとかたまりだけ読んで、そこで違えば落とす
    3. 残ったものだけ全体を読んで要約（ハッシュ）を突き合わせる

段 2 で大半が落ちるので、全体を読むのは本当に似ているものだけになる。
組になるのは**中身全体の要約が一致したものだけ**（小さいファイルは段 2 で全体を読み切っている）。

## 時間の大半は「開く」回数

実データでは、1 ファイルを開くたびにウイルス対策ソフトなどの検査が入り、読む量より
開く回数で時間が決まる。そこで v0.8 から、1 ファイルを開くのは 1 回だけにし
（別名の確認も同じハンドルで行う）、開く・読むを複数スレッドで重ねて待ち時間を隠す。

## 触らないもの

次は候補にしない。理由は「二重に数えないため」と「勝手に取りに行かないため」。

    リンク・ジャンクション    実体は別にある。数えれば同じ中身を二度数えることになる
    ハードリンク             同じ実体を指す別名。消しても容量は空かない
    クラウドのみのファイル    読むと OneDrive などが実体を取りに行く（大量ダウンロード）
    空ファイル               中身が無いので「同じ」だが、消しても容量は空かない

読めなかったファイルは **「重複なし」に混ぜない**。件数を別に数え、呼び手が
そのまま表示できるようにする。
"""

from __future__ import annotations

from i18n import T

import hashlib
import os
import stat
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

# 段 2 で読む大きさ。先頭と末尾から同じだけ読む。
# 64KB は多くのファイル形式でヘッダ（署名・寸法・符号化）が収まる大きさで、
# 中身の違うファイルはここでほぼ落ちる。
EDGE_BYTES = 64 * 1024

# 段 3 で一度に読む大きさ。大きすぎるとメモリを食い、小さすぎると呼び出しが増える。
CHUNK_BYTES = 1024 * 1024

# Windows のファイル属性（クラウドの「実体がまだ無い」印）
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

_CLOUD_ONLY = (FILE_ATTRIBUTE_RECALL_ON_OPEN
               | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
               | FILE_ATTRIBUTE_OFFLINE)


@dataclass
class DupFile:
    """重複の候補・結果に出てくる 1 ファイル。

    `key` は呼び手（アプリ）が自分の一覧の行と結び付けるためのもので、
    このモジュールは中身を見ない。
    """

    path: str
    size: int
    modified: float
    key: object = None
    # スキャン時に得た Windows のファイル属性（None なら不明で、検出時に調べ直す）。
    # これでリンク・クラウドのみのファイルを「開く前に」外せる。
    attrs: int | None = None


@dataclass
class DupGroup:
    """中身がまったく同じファイルの組。"""

    digest: str
    size: int
    files: list[DupFile]

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        """見かけの合計。同じ中身を何度も数えた値なので、空く容量ではない。"""
        return self.size * self.count

    @property
    def reclaimable(self) -> int:
        """1 本だけ残したときに空く容量。整理の効き目はこちらで測る。"""
        return self.size * (self.count - 1)


@dataclass
class DupSkipped:
    """候補から外した・読めなかったものの数。0 件でも呼び手が出せるように持つ。"""

    unreadable: int = 0        # 開けなかった・途中で読めなくなった
    vanished: int = 0          # 検出中に消えた
    links: int = 0             # シンボリックリンク・ジャンクション
    hardlinks: int = 0         # 同じ実体を指す別名（二重に数えない）
    cloud_only: int = 0        # クラウドにしか実体がない（取りに行かせない）
    empty: int = 0             # 空ファイル（消しても容量は空かない）
    unreadable_paths: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        labels = [
            ("unreadable", T('読み取れなかったファイル')),
            ("vanished", T('検出中に消えたファイル')),
            ("links", T('リンク・ジャンクション')),
            ("hardlinks", T('同じ実体を指す別名（ハードリンク）')),
            ("cloud_only", T('クラウドにのみ実体があるファイル')),
            ("empty", T('空ファイル')),
        ]
        return [T('{0} {1:,}件', text, getattr(self, key)) for key, text in labels if getattr(self, key)]


@dataclass
class DupResult:
    groups: list[DupGroup]
    skipped: DupSkipped
    checked: int          # 中身を確かめたファイル数
    read_bytes: int       # 実際に読んだ量
    cancelled: bool = False
    timings: dict = field(default_factory=dict)   # 工程ごとの時間（計測・報告用）
    unchecked: int = 0    # 中止したときに、まだ確かめていなかった候補の数

    @property
    def duplicate_files(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def apparent_size(self) -> int:
        """重複しているファイルの見かけの合計容量。"""
        return sum(g.total_size for g in self.groups)

    @property
    def reclaimable(self) -> int:
        """各組で 1 本ずつ残したときに空く可能性がある容量。"""
        return sum(g.reclaimable for g in self.groups)


def _win_attributes(path: str) -> int | None:
    """Windows のファイル属性。取れなければ None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return None if attrs == 0xFFFFFFFF else attrs
    except Exception:
        return None


def is_cloud_only(path: str, attrs: int | None = None) -> bool:
    """クラウドにしか実体が無いか。

    読むと OneDrive などが実体を取りに行く（通信・課金・時間）。
    「重複を探しただけで数十 GB 落ちてきた」を起こさないために、触らない。
    """
    if attrs is None:
        attrs = _win_attributes(path)
    if attrs is None:
        return False
    if attrs & _CLOUD_ONLY:
        # OFFLINE だけの場合、再解析ポイント（クラウドの印）も伴うかを見る。
        if attrs & (FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            return True
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _file_identity(st) -> tuple | None:
    """同じ実体を指しているかを見分ける鍵（ボリューム + ファイル番号）。

    ハードリンクは名前が違っても同じ実体なので、消しても容量は空かない。
    重複として並べると「空く容量」を偽ることになるため、二本目以降は外す。

    読むために開いたハンドルの fstat を使う（別に lstat すると、1 ファイルを 2 回開くことになる）。
    リンク数が 1 なら別名は無いので、鍵も要らない（ほとんどのファイルがこれ）。
    """
    if getattr(st, "st_nlink", 1) <= 1:
        return None
    ino = getattr(st, "st_ino", 0)
    dev = getattr(st, "st_dev", 0)
    if not ino:
        return None
    return (dev, ino)


def _read_edges(path: str, size: int) -> bytes:
    """先頭と末尾を少しだけ読む。小さいファイルは全部読む。"""
    with open(path, "rb") as fh:
        if size <= EDGE_BYTES * 2:
            return fh.read()
        head = fh.read(EDGE_BYTES)
        fh.seek(-EDGE_BYTES, os.SEEK_END)
        return head + fh.read(EDGE_BYTES)


def _digest(path: str, cancel: threading.Event | None, on_read: Callable[[int], None] | None,
            size: int | None = None) -> str:
    """中身全体の要約。途中で中止されたら空文字を返す。容量が一覧と違えば ChangedError。"""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as fh:
        if size is not None and os.fstat(fh.fileno()).st_size != size:
            raise ChangedError(path)
        while True:
            if cancel is not None and cancel.is_set():
                return ""
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
            if on_read is not None:
                on_read(len(chunk))
    return h.hexdigest()


class ChangedError(OSError):
    """一覧を作ったあとに容量が変わっていた（中身も変わっている）。"""


# --- 1 ファイル分の下調べ（別スレッドで動く） -----------------------------------

def _probe(f: DupFile, size: int, cancel: threading.Event | None):
    """開く前の除外 → 1 回だけ開いて、別名の鍵と「先頭・末尾」の要約を取る。

    返り値: (状態, 鍵, 要約, 全体を読んだか, 読んだバイト数, かかった秒)
      状態: "ok" / "links" / "cloud_only" / "vanished" / "unreadable" / "cancelled"
    小さいファイル（先頭＋末尾で全体になる大きさ）は、ここで中身全体の要約まで作る。
    """
    t0 = time.perf_counter()
    if cancel is not None and cancel.is_set():
        return "cancelled", None, None, False, 0, 0.0
    attrs = f.attrs
    try:
        if attrs is None or attrs < 0:
            # スキャン時の属性が無い（呼び手が渡していない）ときだけ、ここで調べる
            lst = os.lstat(f.path)
            if stat.S_ISLNK(lst.st_mode):
                return "links", None, None, False, 0, time.perf_counter() - t0
            attrs = getattr(lst, "st_file_attributes", 0)
        # リンク・再解析ポイントは実体が別にある。クラウドのみのファイルは開くと取りに行く。
        # どちらも開く前に外す（開かないので、読む時間もかからない）。
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return "links", None, None, False, 0, time.perf_counter() - t0
        if is_cloud_only(f.path, attrs):
            return "cloud_only", None, None, False, 0, time.perf_counter() - t0
        with open(f.path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_size != size:
                return "vanished", None, None, False, 0, time.perf_counter() - t0   # 検出中に変わった
            ident = _file_identity(st)
            if size <= EDGE_BYTES * 2:
                data = fh.read()
                full = True
            else:
                data = fh.read(EDGE_BYTES)
                fh.seek(-EDGE_BYTES, os.SEEK_END)
                data += fh.read(EDGE_BYTES)
                full = False
        key = hashlib.blake2b(data, digest_size=32).hexdigest()
        return "ok", ident, key, full, len(data), time.perf_counter() - t0
    except FileNotFoundError:
        return "vanished", None, None, False, 0, time.perf_counter() - t0
    except OSError:
        return "unreadable", None, None, False, 0, time.perf_counter() - t0


def default_workers() -> int:
    """同時に読むファイル数。開く・読むの待ち時間（ウイルス対策の検査など）を重ねて隠す。"""
    return max(2, min(8, (os.cpu_count() or 4)))


def find_duplicates(
    files: Iterable[DupFile],
    *,
    cancel: threading.Event | None = None,
    progress: Callable[..., None] | None = None,
    min_size: int = 1,
    on_groups: Callable[[list[DupGroup]], None] | None = None,
    workers: int | None = None,
) -> DupResult:
    """中身がまったく同じファイルの組を返す。

    `progress(phase, done, total, groups, reclaimable)` が時々呼ばれる。
    `on_groups(組の一覧)` は、組が**確定するたびに**呼ばれる（途中結果の表示用）。
    確定とは「中身全体の要約が一致した」こと。先頭・末尾だけの一致では組にしない。

    速さのための工夫:
      - 容量が同じものが無いファイルは開かない（段 1）
      - リンク・クラウドのみのファイルは、スキャン時の属性で開く前に外す
      - 1 ファイルは 1 回だけ開く（別名の確認と先頭・末尾の読み込みを同じハンドルで）
      - 小さいファイルは先頭・末尾の読み込みで全体を読み切るので、そこで要約まで作る
      - 開く・読むを複数スレッドで重ねる（待ち時間が主で、CPU はほとんど使わない）
      - 効き目（容量 × 本数）の大きい候補から調べ、確定した組から順に知らせる
    """
    t_start = time.perf_counter()
    skipped = DupSkipped()
    timings = {"candidates_sec": 0.0, "probe_sec": 0.0, "hash_sec": 0.0, "wall_sec": 0.0,
               "first_group_sec": None, "workers": 0, "opened": 0}

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    # --- 段 1: 容量でまとめる（容量が違えば中身も違う。ここでは何も開かない）---
    by_size: dict[int, list[DupFile]] = {}
    for f in files:
        if stopped():
            return DupResult([], skipped, 0, 0, cancelled=True, timings=timings)
        if f.size < min_size:
            if f.size == 0:
                skipped.empty += 1
            continue
        by_size.setdefault(f.size, []).append(f)
    candidates = [(size, group) for size, group in by_size.items() if len(group) > 1]
    candidates.sort(key=lambda x: x[0] * len(x[1]), reverse=True)  # 効き目の大きい順に確かめる
    total_candidates = sum(len(g) for _, g in candidates)
    timings["candidates_sec"] = time.perf_counter() - t_start
    if stopped():
        return DupResult([], skipped, 0, 0, cancelled=True, timings=timings,
                         unchecked=total_candidates)

    n_workers = workers or default_workers()
    timings["workers"] = n_workers
    groups: list[DupGroup] = []
    reclaim = 0
    checked = 0
    read_bytes = 0
    cancelled = False

    def note_unreadable(path: str):
        skipped.unreadable += 1
        if len(skipped.unreadable_paths) < 50:
            skipped.unreadable_paths.append(path)

    def report(phase: str):
        if progress:
            progress(phase, checked, total_candidates, len(groups), reclaim)

    def confirm(new: list[DupGroup]):
        nonlocal reclaim
        if not new:
            return
        for g in new:
            g.files.sort(key=lambda f: (f.modified, f.path))
        groups.extend(new)
        reclaim += sum(g.reclaimable for g in new)
        if timings["first_group_sec"] is None:
            timings["first_group_sec"] = time.perf_counter() - t_start
        if on_groups:
            on_groups(new)

    def hash_task(f: DupFile, size: int):
        t0 = time.perf_counter()
        counter = [0]

        def on_read(n: int):
            counter[0] += n
        try:
            d = _digest(f.path, cancel, on_read, size)
            return ("ok" if d else "cancelled"), d, counter[0], time.perf_counter() - t0
        except (FileNotFoundError, ChangedError):
            return "vanished", None, counter[0], time.perf_counter() - t0
        except OSError:
            return "unreadable", None, counter[0], time.perf_counter() - t0

    pool = ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="dupscan")
    # 大きいファイルの全体読みは、同時に読む本数を絞る（ハードディスクで読み位置が飛び回らないように）
    big_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="duphash")
    pending: deque = deque()          # (容量, [DupFile], [future])
    in_flight = 0
    max_in_flight = n_workers * 32    # 先に投げておく件数（待ち時間を重ねる分だけ）
    it = iter(candidates)
    exhausted = False
    try:
        while True:
            # 先の候補を投げておく（前の組を仕上げている間も読み続けられるように）
            while not exhausted and in_flight < max_in_flight and not stopped():
                nxt = next(it, None)
                if nxt is None:
                    exhausted = True
                    break
                size, same_size = nxt
                futs = [pool.submit(_probe, f, size, cancel) for f in same_size]
                pending.append((size, same_size, futs))
                in_flight += len(same_size)
            if not pending:
                break
            if stopped():
                cancelled = True
                break

            size, same_size, futs = pending.popleft()
            in_flight -= len(same_size)
            by_key: dict[str, list[DupFile]] = {}
            full_known = False
            seen_identity: set[tuple] = set()
            for f, fut in zip(same_size, futs):
                state, ident, key, full, nread, sec = fut.result()
                timings["probe_sec"] += sec
                if state == "cancelled":
                    cancelled = True
                    continue
                if state == "links":
                    skipped.links += 1
                elif state == "cloud_only":
                    skipped.cloud_only += 1
                elif state == "vanished":
                    skipped.vanished += 1
                elif state == "unreadable":
                    note_unreadable(f.path)
                else:
                    timings["opened"] += 1
                    read_bytes += nread
                    if ident is not None:
                        if ident in seen_identity:
                            skipped.hardlinks += 1
                            checked += 1
                            continue
                        seen_identity.add(ident)
                    by_key.setdefault(key, []).append(f)
                    full_known = full
                checked += 1
                report("edges")
            if cancelled:
                break

            buckets = [b for b in by_key.values() if len(b) > 1]
            if full_known:
                # 小さいファイル: 先頭＋末尾の読み込みで全体を読んでいる。要約の一致 = 中身の一致
                confirm([DupGroup(k, size, list(b)) for k, b in by_key.items() if len(b) > 1])
            elif buckets:
                # 先頭と末尾が一致しただけでは組にしない。全体を読んで確かめる
                jobs = [(f, big_pool.submit(hash_task, f, size)) for b in buckets for f in b]
                by_digest: dict[str, list[DupFile]] = {}
                for f, fut in jobs:
                    state, digest, nread, sec = fut.result()
                    timings["hash_sec"] += sec
                    read_bytes += nread
                    if state == "cancelled":
                        cancelled = True
                    elif state == "vanished":
                        skipped.vanished += 1
                    elif state == "unreadable":
                        note_unreadable(f.path)
                    else:
                        by_digest.setdefault(digest, []).append(f)
                    report("hash")
                if cancelled:
                    break
                confirm([DupGroup(d, size, same) for d, same in by_digest.items() if len(same) > 1])
            # この容量の組を調べ終えた。候補が少ないと上の報告が少ないので、ここでも知らせる。
            report("edges")
    finally:
        if cancelled or stopped():
            cancelled = True
        pool.shutdown(wait=True, cancel_futures=True)
        big_pool.shutdown(wait=True, cancel_futures=True)

    # 効き目（空く容量）の大きい順。同じなら 1 ファイルの大きい順
    groups.sort(key=lambda g: (g.reclaimable, g.size), reverse=True)
    timings["wall_sec"] = time.perf_counter() - t_start
    return DupResult(groups, skipped, checked, read_bytes, cancelled=cancelled, timings=timings,
                     unchecked=max(0, total_candidates - checked) if cancelled else 0)
