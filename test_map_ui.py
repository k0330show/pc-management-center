"""容量マップで、スキャン対象のルートから配下へたどれることを確かめる。

一時フォルダだけを使い、ごみ箱への移動は差し替える。ウィンドウは隠したまま動かす。
"""

from __future__ import annotations

import gc
import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

import app as appmod
from app import App, squarify


def pump(a: App, seconds: float = 3.0, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        a.update()
        if until is not None and until():
            return
        time.sleep(0.02)


class TestSquarify(unittest.TestCase):
    def test_面積は値に比例し_はみ出さず_重ならない(self):
        values = sorted([500, 300, 120, 50, 20, 7, 3, 1] + [1] * 40, reverse=True)
        rects = squarify(values, 10, 20, 400, 300)
        self.assertEqual(len(rects), len(values))
        total = sum(values)
        for v, (x, y, w, h) in zip(values, rects):
            self.assertAlmostEqual(w * h, 400 * 300 * v / total, delta=1e-6)
            self.assertGreaterEqual(x, 10 - 1e-6)
            self.assertGreaterEqual(y, 20 - 1e-6)
            self.assertLessEqual(x + w, 410 + 1e-6)
            self.assertLessEqual(y + h, 320 + 1e-6)
        for i, (x1, y1, w1, h1) in enumerate(rects):
            for x2, y2, w2, h2 in rects[i + 1:]:
                overlap_w = min(x1 + w1, x2 + w2) - max(x1, x2)
                overlap_h = min(y1 + h1, y2 + h2) - max(y1, y2)
                self.assertFalse(overlap_w > 1e-6 and overlap_h > 1e-6, "四角が重なっている")

    def test_帯より正方形に近い(self):
        rects = squarify([100] * 16, 0, 0, 400, 400)
        worst = max(max(w / h, h / w) for _x, _y, w, h in rects)
        self.assertLess(worst, 2.0, "細長い帯になっている（小さい項目が選べない）")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()      # 画面に出さない
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"画面が使えない環境です: {exc}")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_map_test_"))
        self._ledger_dir = tempfile.mkdtemp(prefix="pmc_ledger_")
        appmod.App.LEDGER_PATH = os.path.join(self._ledger_dir, "trash_log.json")
        # 移動は差し替えるので、ごみ箱の中を探し直しても見つからない（待たない）
        self._real_delays = appmod.recyclebin.RETRY_DELAYS
        appmod.recyclebin.RETRY_DELAYS = ()
        self._real_move = appmod.trash.move_to_trash
        self.moved = []

        def fake_move(path, size, hwnd=0):
            self.moved.append(path)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        appmod.trash.move_to_trash = fake_move
        self.a = App(hidden=True)
        self.a.withdraw()
        self.a.raise_window = lambda w: None
        self.a.current_root = self.dir
        self.a.confirm_trash = lambda spec: True
        self.a.notify = lambda *a, **k: None
        self.a.show_trash_results = lambda *a, **k: None
        # 地図の大きさ（隠したウィンドウでは 1×1 になるので、描く大きさを決めておく）
        self.a.treemap.canvas.winfo_width = lambda: 800
        self.a.treemap.canvas.winfo_height = lambda: 500

    def tearDown(self):
        appmod.trash.move_to_trash = self._real_move
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()
        appmod.App.LEDGER_PATH = None
        appmod.recyclebin.RETRY_DELAYS = self._real_delays
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self._ledger_dir, ignore_errors=True)

    def write(self, rel: str, size: int) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def scan(self):
        self.a.start_scan()
        pump(self.a, 20, until=lambda: bool(self.a.file_rows))
        self.assertTrue(self.a.file_rows)


class TestNavigate(Base):
    def setUp(self):
        super().setUp()
        self.write("Downloads/a/b/c/deep.bin", 40_000)
        self.write("Downloads/a/b/other.bin", 5_000)
        self.write("Downloads/top.bin", 3_000)
        for i in range(320):                       # 地図では小さすぎる項目がたくさん
            self.write(f"Downloads/many/f{i:03d}/t.bin", 10)
        self.write("Downloads/many/big/big.bin", 30_000)
        self.scan()
        self.tm = self.a.treemap
        self.dl = self.dir / "Downloads"

    def crumbs(self):
        out = []
        for w in self.tm.crumbs.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
        return out

    def test_ルートから深い階層まで順にたどれる(self):
        tm = self.tm
        self.assertEqual(tm.current_root, self.dir)
        for step in (self.dl, self.dl / "a", self.dl / "a" / "b", self.dl / "a" / "b" / "c"):
            tm.navigate(step)
            self.assertEqual(tm.current_root, step)
        texts = self.crumbs()
        for name in ("Downloads", "a", "b", "c"):
            self.assertIn(name, texts, "階層表示に途中の場所がない")
        self.assertIn("表示中: c", tm.info.get())
        self.assertIn("親フォルダ「b」", tm.info.get())
        self.assertIn("スキャン対象全体の", tm.info.get())

    def test_四角をクリックすると中へ入る(self):
        tm = self.tm
        tm.navigate(self.dl)
        tm.redraw()
        rid = tm._rect_by_target.get(self.dl / "a")
        self.assertIsNotNone(rid, "a の四角が描かれていない")
        x0, y0, x1, y1 = tm.canvas.coords(rid)

        class Ev:
            x = int((x0 + x1) / 2)
            y = int((y0 + y1) / 2)
        tm._click(Ev())
        self.assertEqual(tm.current_root, self.dl / "a")

    def test_戻ると親へ(self):
        tm = self.tm
        tm.navigate(self.dl)
        tm.navigate(self.dl / "a" / "b" / "c")       # 階層表示などから一気に深くへ
        tm.go_up()
        self.assertEqual(tm.current_root, self.dl / "a" / "b", "親フォルダへ上がれない")
        tm.go_back()
        self.assertEqual(tm.current_root, self.dl / "a" / "b" / "c", "「戻る」で直前の場所へ戻れない")
        tm.go_back()
        self.assertEqual(tm.current_root, self.dl)
        # 階層表示のボタンでルートへ
        root_btn = next(w for w in tm.crumbs.winfo_children()
                        if isinstance(w, appmod.ttk.Button) and str(w.cget("text")) == str(self.dir))
        root_btn.invoke()
        self.assertEqual(tm.current_root, self.dir)
        self.assertEqual(str(tm.up_btn["state"]), "disabled", "ルートより上へ行けてしまう")

    def test_小さくて地図で選べない項目も一覧から開ける(self):
        tm = self.tm
        tm.navigate(self.dl / "many")
        tm.redraw()
        names = [tm.list.item(i, "text") for i in tm.list.get_children()]
        self.assertEqual(len(names), 321, "一覧に全項目が出ていない")
        # 一覧の最後（いちばん小さい側）は、地図では「ほか」にまとめられて描かれない
        tiny = list(tm._list_targets.values())[-1]
        self.assertNotIn(tiny, tm._rect_by_target, "（前提）小さい項目まで地図に描いている")
        self.assertIn("more", tm._rect_by_target, "地図に描けない残りをまとめていない")
        row = next(i for i, t in tm._list_targets.items() if t == tiny)
        tm.list.selection_set(row)
        tm._open_list_row()
        self.assertEqual(tm.current_root, tiny)

    def test_右クリックから詳細_一覧_ごみ箱へ(self):
        tm = self.tm
        tm.navigate(self.dl)
        labels = []
        real_menu = appmod.tk.Menu

        class FakeMenu(real_menu):
            def add_command(self, **kw):
                labels.append(kw.get("label"))
                super().add_command(**kw)

            def tk_popup(self, *a, **kw):
                pass

        class Ev:
            x_root = y_root = 0
        appmod.tk.Menu = FakeMenu
        try:
            tm._menu_for(self.dl / "a", Ev())
        finally:
            appmod.tk.Menu = real_menu
        for want in ("中へ移動", "詳細を表示", "フォルダ一覧で表示", "ファイル一覧でこの場所を見る", "ごみ箱へ移動…"):
            self.assertIn(want, labels)
        self.a.reveal_in_folder_list(self.dl / "a")
        self.assertEqual(self.a.folder_here.get(), str(self.dl / "a"))

    def test_表示中のフォルダを移すと親へ移り_数字も更新される(self):
        tm = self.tm
        tm.navigate(self.dl)
        tm.navigate(self.dl / "a")
        before = self.a.folder_sizes[self.dl]
        self.assertTrue(self.a.trash_folder(self.dl / "a"))
        self.assertEqual(tm.current_root, self.dl)
        self.assertNotIn(self.dl / "a", tm.child_dirs.get(self.dl, []))
        names = [tm.list.item(i, "text") for i in tm.list.get_children()]
        self.assertFalse(any(n.endswith(" a") for n in names), "移したフォルダが一覧に残っている")
        self.assertEqual(self.a.folder_sizes[self.dl], before - 45_000)
        self.assertIn(appmod.human_size(before - 45_000), tm.info.get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
