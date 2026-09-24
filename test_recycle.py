"""ごみ箱への移動と、このアプリからの復元を、本物の Windows のごみ箱で確かめる。

**このテストが作った一時フォルダの中のファイル・フォルダだけ**を移す。利用者のファイルは移さない。
後片付けでは、このテストの一時フォルダから来た項目だけを、ごみ箱の中から探して取り除く
（記録から特定したもの ＋ 念のため、元のパスがこの一時フォルダの中を指す新しい `$I` を全部）。
ウィンドウは隠したまま動かし、確認画面・結果画面は差し替えるので、画面には何も出ない。
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

import recyclebin
import trash

LEFTOVERS: list[str] = []      # 片付けたごみ箱の項目（最後に報告する）


def bin_items_from(folder: Path, since: float) -> list[tuple[str, str]]:
    """ごみ箱の中で、元のパスが folder の中を指す項目 ($I, $R) を探す。"""
    bdir = recyclebin.bin_dir_for(str(folder))
    out = []
    if not bdir or not os.path.isdir(bdir):
        return out
    pre = os.path.normcase(str(folder)) + os.sep
    with os.scandir(bdir) as it:
        for e in it:
            if not e.name.startswith("$I"):
                continue
            try:
                if e.stat().st_mtime < since - 5:
                    continue
                rec = recyclebin.read_info(e.path)
            except (OSError, ValueError):
                continue
            if os.path.normcase(rec.orig_path).startswith(pre) or os.path.normcase(rec.orig_path) == pre[:-1]:
                out.append((e.path, os.path.join(bdir, "$R" + e.name[2:])))
    return out


def purge(pairs):
    for ipath, rpath in pairs:
        if os.path.isdir(rpath):
            shutil.rmtree(rpath, ignore_errors=True)
        elif os.path.lexists(rpath):
            os.remove(rpath)
        if os.path.lexists(ipath):
            os.remove(ipath)
        LEFTOVERS.append(os.path.basename(rpath))


class RealBinBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith("win"):
            raise unittest.SkipTest("Windows のごみ箱でだけ確かめる")
        probe_dir = Path(tempfile.mkdtemp(prefix="pmc_rb_probe_"))
        try:
            trash.check_can_recycle(str(probe_dir / "x.bin"), 10)
        except trash.TrashError as exc:
            raise unittest.SkipTest(f"この環境では一時フォルダをごみ箱へ移せない: {exc}")
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        if not recyclebin.user_sid():
            raise unittest.SkipTest("ごみ箱のフォルダ（SID）が分からない")

    def setUp(self):
        self.t_start = time.time()
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_rb_test_"))
        self.ledger_dir = Path(tempfile.mkdtemp(prefix="pmc_rb_ledger_"))
        self.ledger = recyclebin.Ledger(str(self.ledger_dir / "trash_log.json"))

    def tearDown(self):
        # 1. 記録から特定できたもの（まだごみ箱にあるもの）を取り除く
        pairs = []
        for e in self.ledger.entries:
            if e.state != "restored" and e.r_name and os.path.lexists(e.i_path):
                rec = recyclebin.read_info(e.i_path)
                if os.path.normcase(rec.orig_path).startswith(os.path.normcase(str(self.dir))):
                    pairs.append((e.i_path, e.r_path))
        purge(pairs)
        # 2. 念のため、この一時フォルダから来た項目がごみ箱に残っていないか探して取り除く
        purge(bin_items_from(self.dir, self.t_start))
        self.assertEqual(bin_items_from(self.dir, self.t_start), [], "ごみ箱にテストの項目が残っている")
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.ledger_dir, ignore_errors=True)

    def write(self, rel: str, data: bytes) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def move(self, path: Path, is_dir: bool = False) -> recyclebin.Entry:
        size = trash.measure_folder(str(path)).size if is_dir else path.stat().st_size
        t0 = time.time()
        trash.move_to_trash(str(path), size)
        entries = recyclebin.locate([(str(path), is_dir, size, t0)], self.ledger.claimed())
        self.ledger.add(entries)
        self.assertEqual(len(entries), 1)
        return entries[0]


class TestRecycleBin(RealBinBase):
    def test_ファイルを移して特定し_元に戻せる(self):
        p = self.write("a/写真 1.jpg", b"PHOTO" * 1000)
        mtime = p.stat().st_mtime
        e = self.move(p)
        self.assertFalse(p.exists())
        self.assertEqual(e.state, "in_bin", e.note)
        self.assertTrue(e.r_name.startswith("$R") and e.i_name.startswith("$I"))
        self.assertEqual(recyclebin.check(e)[0], "in_bin")
        recyclebin.restore(e)
        self.assertEqual(p.read_bytes(), b"PHOTO" * 1000)
        self.assertAlmostEqual(p.stat().st_mtime, mtime, places=3)
        self.assertFalse(os.path.lexists(e.r_path))
        self.assertFalse(os.path.lexists(e.i_path), "ごみ箱の記録（$I）が残っている")
        self.assertEqual(e.state, "restored")

    def test_フォルダも中身ごと戻せる(self):
        self.write("box/sub/x.bin", b"x" * 3000)
        self.write("box/y.bin", b"y" * 2000)
        e = self.move(self.dir / "box", is_dir=True)
        self.assertEqual(e.state, "in_bin")
        self.assertTrue(e.is_dir)
        recyclebin.restore(e)
        self.assertEqual((self.dir / "box" / "sub" / "x.bin").read_bytes(), b"x" * 3000)
        self.assertEqual((self.dir / "box" / "y.bin").read_bytes(), b"y" * 2000)

    def test_同じ名前と場所でも別の項目を取り違えない(self):
        p = self.write("same.txt", b"first")
        e1 = self.move(p)
        p.write_bytes(b"second!")
        e2 = self.move(p)
        self.assertNotEqual(e1.r_name, e2.r_name, "別の項目を同じものとして記録している")
        recyclebin.restore(e1)
        self.assertEqual(p.read_bytes(), b"first", "違う方を戻した")
        with self.assertRaises(recyclebin.RestoreError) as cm:
            recyclebin.restore(e2)
        self.assertEqual(cm.exception.code, "exists")
        self.assertEqual(p.read_bytes(), b"first", "上書きした")
        self.assertEqual(recyclebin.check(e2)[0], "in_bin", "戻せなかった方がごみ箱から消えた")

    def test_同名があれば上書きしない(self):
        p = self.write("doc.txt", b"old")
        e = self.move(p)
        p.write_bytes(b"new one")                 # 元の場所に同じ名前のものができた
        err = recyclebin.restore_problem(e)
        self.assertEqual(err.code, "exists")
        with self.assertRaises(recyclebin.RestoreError):
            recyclebin.restore(e)
        self.assertEqual(p.read_bytes(), b"new one")
        self.assertEqual(recyclebin.check(e)[0], "in_bin")
        p.unlink()                                 # 今ある方をどけたら戻せる
        recyclebin.restore(e)
        self.assertEqual(p.read_bytes(), b"old")

    def test_元のフォルダが無ければ別の場所へは戻さない(self):
        p = self.write("gone/child.txt", b"child")
        e = self.move(p)
        shutil.rmtree(self.dir / "gone")
        err = recyclebin.restore_problem(e)
        self.assertEqual(err.code, "no_parent")
        with self.assertRaises(recyclebin.RestoreError):
            recyclebin.restore(e)
        self.assertFalse((self.dir / "gone").exists(), "元のフォルダを勝手に作った")
        (self.dir / "gone").mkdir()
        recyclebin.restore(e)
        self.assertEqual(p.read_bytes(), b"child")

    def test_Windows側で削除されたものは見つからないと出す(self):
        p = self.write("del.txt", b"bye")
        e = self.move(p)
        purge([(e.i_path, e.r_path)])             # ごみ箱から削除された状態
        self.assertEqual(recyclebin.check(e)[0], "gone")
        with self.assertRaises(recyclebin.RestoreError) as cm:
            recyclebin.restore(e)
        self.assertEqual(cm.exception.code, "gone")

    def test_Windows側で元に戻されたものも見つからないと出す(self):
        p = self.write("back.txt", b"back")
        e = self.move(p)
        os.rename(e.r_path, str(p))               # Windows のごみ箱で「元に戻す」をした状態
        os.remove(e.i_path)
        state, why = recyclebin.check(e)
        self.assertEqual(state, "gone")
        self.assertIn("元に戻した", why)
        with self.assertRaises(recyclebin.RestoreError):
            recyclebin.restore(e)
        self.assertEqual(p.read_bytes(), b"back")

    def test_記録はアプリを閉じても残る(self):
        p = self.write("keep.txt", b"k")
        e = self.move(p)
        again = recyclebin.Ledger(self.ledger.path)
        self.assertEqual([x.key for x in again.entries], [e.key])
        self.assertEqual(recyclebin.check(again.entries[0])[0], "in_bin")
        recyclebin.restore(again.entries[0])
        self.assertTrue(p.exists())


class TestRestoreFromApp(RealBinBase):
    """アプリの操作（移動 → 「ごみ箱から復元」タブ）を通して、本物のごみ箱で確かめる。"""

    def setUp(self):
        super().setUp()
        try:
            probe = tk.Tk()
            probe.withdraw()      # 画面に出さない
            probe.destroy()
        except tk.TclError as exc:
            self.skipTest(f"画面が使えない環境です: {exc}")
        import app as appmod
        self.appmod = appmod
        appmod.App.LEDGER_PATH = self.ledger.path
        self.a = appmod.App(hidden=True)
        self.a.withdraw()
        self.a.raise_window = lambda w: None
        self.a.current_root = self.dir
        self.a.confirm_trash = lambda spec: True
        self.a.confirm_trash_many = lambda mv, bl, **kw: True
        self.a.notify = lambda *a, **k: None
        self.results = []
        self.a.show_trash_results = lambda title, text, rows, error=False: self.results.append((title, text, rows))
        self.restore_rows = []
        self.a.confirm_restore = lambda rows: (self.restore_rows.append(rows), True)[1]

    def tearDown(self):
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()
        self.appmod.App.LEDGER_PATH = None
        self.ledger.load()           # アプリが書いた記録で片付ける
        super().tearDown()

    def scan(self):
        self.a.start_scan()
        end = time.monotonic() + 20
        while time.monotonic() < end and not self.a.file_rows:
            self.a.update()
            time.sleep(0.02)
        self.assertTrue(self.a.file_rows)

    def select_bin(self, *names):
        self.a.render_bin()
        rows = [i for i, e in self.a._bin_rows.items() if e.name in names]
        self.a.bin_tree.selection_set(rows)
        return rows

    def test_移して_タブから元に戻す(self):
        p1 = self.write("one.txt", b"1" * 100)
        p2 = self.write("two.txt", b"2" * 200)
        self.write("stay.txt", b"s")
        self.scan()
        items = [f for f in self.a.file_rows if f.name in ("one.txt", "two.txt")]
        self.a.trash_many([("file", i) for i in items])
        self.assertFalse(p1.exists() or p2.exists())
        entries = self.a.ledger.entries
        self.assertEqual(sorted(e.name for e in entries), ["one.txt", "two.txt"])
        self.assertTrue(all(e.state == "in_bin" for e in entries), [e.note for e in entries])
        # 記録はファイルに残っている
        self.assertEqual(len(recyclebin.Ledger(self.ledger.path).entries), 2)
        # 状態の確認（画面を止めないよう別スレッドで行う処理を、ここでは直接呼ぶ）
        self.a._apply_bin_check(self.a._compute_bin_check(entries))
        self.assertIn("ごみ箱にあります", [self.a.bin_tree.set(i, "state") for i in self.a.bin_tree.get_children()])
        self.select_bin("one.txt", "two.txt")
        self.a.restore_selected()
        self.assertEqual(p1.read_bytes(), b"1" * 100)
        self.assertEqual(p2.read_bytes(), b"2" * 200)
        title, text, rows = self.results[-1]
        self.assertTrue(all(r[2].startswith("元に戻した") for r in rows), rows)
        self.assertIn("スキャン", self.a.bin_stale.get(), "スキャン結果が古くなったことを伝えていない")
        self.assertTrue(all(e.state == "restored" for e in self.a.ledger.entries))

    def test_同名があるときは戻さず_案内を出す(self):
        p = self.write("dup.txt", b"old")
        self.scan()
        item = next(f for f in self.a.file_rows if f.name == "dup.txt")
        self.a.trash_file(item)
        self.assertFalse(p.exists())
        p.write_bytes(b"new")
        self.select_bin("dup.txt")
        self.a.restore_selected()
        (entry, problem, _note), = self.restore_rows[-1]
        self.assertEqual(problem.code, "exists")
        self.assertIn("上書き", str(problem))
        self.assertEqual(p.read_bytes(), b"new", "上書きした")
        self.assertTrue(self.results[-1][2][0][2].startswith("戻さなかった"))

    def test_親フォルダも一緒に選べば親から順に戻す(self):
        self.write("pp/inner.txt", b"i")
        self.write("pp/other.txt", b"o")
        self.scan()
        inner = next(f for f in self.a.file_rows if f.name == "inner.txt")
        self.a.trash_file(inner)                       # 先に中のファイルを移す
        self.a.trash_folder(self.dir / "pp")           # 次にフォルダごと移す
        self.assertFalse((self.dir / "pp").exists())
        # 中のファイルだけ選ぶと、親フォルダがこの一覧にあることを案内する
        self.select_bin("inner.txt")
        self.a.restore_selected()
        (_e, problem, _n), = self.restore_rows[-1]
        self.assertEqual(problem.code, "no_parent")
        self.assertIn("先にそれを復元", str(problem))
        # 両方選べば、フォルダ → 中のファイルの順に戻る
        self.select_bin("inner.txt", "pp")
        self.a.restore_selected()
        self.assertEqual((self.dir / "pp" / "inner.txt").read_bytes(), b"i")
        self.assertEqual((self.dir / "pp" / "other.txt").read_bytes(), b"o")


def tearDownModule():
    if LEFTOVERS:
        print(f"\n[test_recycle] ごみ箱から片付けたテスト項目: {len(LEFTOVERS)} 件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
