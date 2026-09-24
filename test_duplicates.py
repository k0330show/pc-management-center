"""重複検出の検査。

使うのはこのテストが作った一時フォルダだけで、利用者の実ファイルには触れない。

見るのは「重複を見つけられるか」だけではない。**見つけてはいけないものを
見つけていないか**——名前が同じだけのもの、リンク、クラウドにしか無いもの——
そして、読めなかったものを黙って「重複なし」に混ぜていないか。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from duplicates import DupFile, find_duplicates, EDGE_BYTES


def item(path: Path) -> DupFile:
    st = path.stat()
    return DupFile(str(path), st.st_size, st.st_mtime, key=str(path))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_dup_test_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self, *paths: Path, **kw):
        return find_duplicates([item(p) for p in paths], **kw)


class TestSameContent(Base):
    def test_同じ中身なら名前が違っても重複(self):
        a = self.write("写真.jpg", b"A" * 5000)
        b = self.write("コピー - 写真.jpg", b"A" * 5000)
        c = self.write("別物.jpg", b"B" * 5000)
        r = self.scan(a, b, c)
        self.assertEqual(len(r.groups), 1, "同じ中身の組が見つかっていない")
        found = {Path(f.path).name for f in r.groups[0].files}
        self.assertEqual(found, {a.name, b.name})

    def test_名前と容量が同じでも中身が違えば重複ではない(self):
        d1 = self.dir / "one"
        d2 = self.dir / "two"
        a = self.write("one/同じ名前.dat", b"X" * 4096)
        b = self.write("two/同じ名前.dat", b"Y" * 4096)
        self.assertEqual(a.stat().st_size, b.stat().st_size, "容量まで同じ状況を作れていない")
        r = self.scan(a, b)
        self.assertEqual(r.groups, [], "名前と容量が同じというだけで重複としている")
        self.assertEqual(r.checked, 2, "中身を確かめていない")
        del d1, d2

    def test_先頭だけ同じで末尾が違うファイルは重複ではない(self):
        body = b"H" * (EDGE_BYTES * 2 + 1000)
        a = self.write("a.bin", body + b"1")
        b = self.write("b.bin", body + b"2")
        self.assertEqual(a.stat().st_size, b.stat().st_size)
        r = self.scan(a, b)
        self.assertEqual(r.groups, [], "末尾の違いを見落としている")

    def test_大きいファイルも中身で判じる(self):
        body = os.urandom(EDGE_BYTES * 2 + 5000)
        a = self.write("big1.bin", body)
        b = self.write("big2.bin", body)
        r = self.scan(a, b)
        self.assertEqual(len(r.groups), 1)
        self.assertGreater(r.read_bytes, EDGE_BYTES, "全体を読まずに判じている")

    def test_三つ以上でも一つの組になる(self):
        ps = [self.write(f"same{i}.txt", b"Z" * 3000) for i in range(4)]
        r = self.scan(*ps)
        self.assertEqual(len(r.groups), 1)
        self.assertEqual(r.groups[0].count, 4)


class TestSizes(Base):
    def test_見かけの合計と空く容量を混同しない(self):
        ps = [self.write(f"c{i}.bin", b"Q" * 10_000) for i in range(3)]
        r = self.scan(*ps)
        g = r.groups[0]
        self.assertEqual(g.total_size, 30_000, "見かけの合計が違う")
        self.assertEqual(g.reclaimable, 20_000, "空く容量は『1 本残す』ぶんを引いた値であるべき")
        self.assertEqual(r.apparent_size, 30_000)
        self.assertEqual(r.reclaimable, 20_000)

    def test_効き目の大きい組が先に来る(self):
        small = [self.write(f"s{i}.bin", b"s" * 1000) for i in range(2)]
        big = [self.write(f"b{i}.bin", b"b" * 50_000) for i in range(2)]
        r = self.scan(*small, *big)
        self.assertEqual(len(r.groups), 2)
        self.assertGreater(r.groups[0].reclaimable, r.groups[1].reclaimable,
                           "空く容量の大きい組が先に来ていない")


class TestEmptyAndSingles(Base):
    def test_空ファイルは重複として並べない(self):
        a = self.write("empty1", b"")
        b = self.write("empty2", b"")
        r = self.scan(a, b)
        self.assertEqual(r.groups, [], "空ファイルを重複として並べている（消しても容量は空かない）")
        self.assertEqual(r.skipped.empty, 2, "空ファイルの件数を数えていない")

    def test_一つしかないものは読まない(self):
        a = self.write("only.bin", b"A" * 9000)
        r = self.scan(a)
        self.assertEqual(r.groups, [])
        self.assertEqual(r.checked, 0, "候補ですらないファイルを読んでいる")
        self.assertEqual(r.read_bytes, 0)


class TestUnreadable(Base):
    def test_読めなかったものを重複なしに混ぜない(self):
        a = self.write("ok1.bin", b"K" * 4000)
        b = self.write("ok2.bin", b"K" * 4000)
        gone = self.dir / "消える.bin"
        gone.write_bytes(b"K" * 4000)
        items = [item(a), item(b), item(gone)]
        gone.unlink()                      # 一覧を作ったあとに消える
        r = find_duplicates(items)
        self.assertEqual(len(r.groups), 1, "残ったファイルの組は見つかるはず")
        self.assertEqual(r.groups[0].count, 2)
        self.assertEqual(r.skipped.vanished, 1, "消えたファイルを数えていない")

    def test_開けないファイルは件数に残る(self):
        a = self.write("locked1.bin", b"L" * 4000)
        b = self.write("locked2.bin", b"L" * 4000)
        items = [item(a), item(b)]
        if sys.platform != "win32":
            self.skipTest("Windows 以外では排他ロックを作らない")
        # 排他で開いたままにすると、別プロセス（このテスト）からは読めない
        handle = open(a, "rb+")
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            r = find_duplicates(items)
            if r.skipped.unreadable == 0 and r.groups:
                self.skipTest("この環境ではロック中でも読めた")
            self.assertGreaterEqual(r.skipped.unreadable, 1)
            self.assertEqual(r.groups, [], "読めなかったものを重複として並べている")
        finally:
            try:
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            handle.close()


class TestLinks(Base):
    def _can_symlink(self) -> bool:
        probe = self.dir / "_probe"
        target = self.write("_probe_target", b"t")
        try:
            os.symlink(target, probe)
            probe.unlink()
            return True
        except (OSError, NotImplementedError):
            return False

    def test_シンボリックリンクは重複に数えない(self):
        if not self._can_symlink():
            self.skipTest("この環境ではシンボリックリンクを作れない（開発者モード/管理者権限）")
        a = self.write("real.bin", b"R" * 8000)
        link = self.dir / "link.bin"
        os.symlink(a, link)
        r = find_duplicates([item(a), DupFile(str(link), a.stat().st_size, a.stat().st_mtime)])
        self.assertEqual(r.groups, [], "リンクを重複として並べている（実体は一つ）")
        self.assertEqual(r.skipped.links, 1)

    def test_ハードリンクは二重に数えない(self):
        a = self.write("hard1.bin", b"H" * 7000)
        hard = self.dir / "hard2.bin"
        try:
            os.link(a, hard)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("この環境ではハードリンクを作れない")
        r = find_duplicates([item(a), item(hard)])
        self.assertEqual(r.groups, [], "ハードリンクを重複として並べている（消しても容量は空かない）")
        self.assertEqual(r.skipped.hardlinks, 1)


class TestCancel(Base):
    def test_中止すると途中で止まり_その旨を返す(self):
        for i in range(40):
            self.write(f"c{i}.bin", os.urandom(EDGE_BYTES * 2 + 100))
        for i in range(40):
            shutil.copyfile(self.dir / f"c{i}.bin", self.dir / f"d{i}.bin")
        items = [item(p) for p in sorted(self.dir.iterdir())]
        cancel = threading.Event()
        seen = {"n": 0}

        def progress(_phase, done, _total, _groups=0, _reclaim=0):
            seen["n"] = done
            if done >= 4:
                cancel.set()

        r = find_duplicates(items, cancel=cancel, progress=progress)
        self.assertTrue(r.cancelled, "中止したのに、そう返していない")
        self.assertLess(len(r.groups), 40, "中止したのに全部調べている")

    def test_はじめから中止されていれば何も読まない(self):
        a = self.write("x1.bin", b"X" * 5000)
        b = self.write("x2.bin", b"X" * 5000)
        cancel = threading.Event()
        cancel.set()
        r = find_duplicates([item(a), item(b)], cancel=cancel)
        self.assertTrue(r.cancelled)
        self.assertEqual(r.read_bytes, 0)


class TestChangedDuringScan(Base):
    def test_検出中に中身が変わったファイルは組にならない(self):
        a = self.write("m1.bin", b"M" * 6000)
        b = self.write("m2.bin", b"M" * 6000)
        items = [item(a), item(b)]
        b.write_bytes(b"N" * 6000)    # 一覧を作ったあとに中身が変わる（容量は同じ）
        r = find_duplicates(items)
        self.assertEqual(r.groups, [], "変わったあとの中身を確かめずに組にしている")

    def test_結果は検出時点の容量と更新日時を覚えている(self):
        a = self.write("k1.bin", b"K" * 4000)
        b = self.write("k2.bin", b"K" * 4000)
        r = self.scan(a, b)
        f = r.groups[0].files[0]
        self.assertEqual(f.size, 4000)
        self.assertGreater(f.modified, 0)
        self.assertTrue(f.key, "呼び手が自分の一覧と結び付ける鍵が無い")


class TestProgress(Base):
    def test_進捗は段階と件数を伝える(self):
        ps = [self.write(f"p{i}.bin", b"P" * 5000) for i in range(6)]
        phases = []
        find_duplicates([item(p) for p in ps],
                        progress=lambda phase, done, total, *_: phases.append((phase, done, total)))
        self.assertTrue(phases, "進捗が一度も呼ばれていない")
        self.assertTrue(all(t >= d for _p, d, t in phases), "件数が全体を超えている")

    def test_進捗は見つかった組と空く見込みも伝える(self):
        ps = [self.write(f"q{i}.bin", b"Q" * 7000) for i in range(4)]
        seen = []
        find_duplicates([item(p) for p in ps],
                        progress=lambda phase, done, total, groups=0, reclaim=0:
                        seen.append((groups, reclaim)))
        self.assertTrue(seen, "進捗が呼ばれていない")
        self.assertTrue(any(g > 0 for g, _r in seen),
                        "途中で見つかった組の数を伝えていない（切り上げる判断ができない）")


if __name__ == "__main__":
    # 実ファイルには触れないことを、走る前に一度書いておく
    print("一時フォルダだけを使います:", tempfile.gettempdir())
    unittest.main(verbosity=2)
    del subprocess
