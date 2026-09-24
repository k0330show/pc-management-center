"""レイアウトの確認（ウィンドウは一度も表示しない）。

Tk が計算する「部品が必要とする大きさ」(winfo_reqwidth / winfo_reqheight) を使い、
最小サイズのウィンドウでも操作バーや行が切れないか、一覧を表示する高さが残るか、
確認画面が画面に収まるかを確かめる。画面を撮影して見る確認ではない。
"""

from __future__ import annotations

import gc
import os
import shutil
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

import app as appmod
from app import App, px


class Layout(unittest.TestCase):
    LANG = "ja"

    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"画面が使えない環境です: {exc}")

    def setUp(self):
        self._ledger_dir = tempfile.mkdtemp(prefix="pmc_ledger_")
        appmod.App.LEDGER_PATH = os.path.join(self._ledger_dir, "trash_log.json")
        self.a = App(hidden=True, language=self.LANG)
        self.a.raise_window = lambda w: None
        self.a.update_idletasks()
        self.min_w, self.min_h = self.a.minsize()

    def tearDown(self):
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()
        appmod.App.LEDGER_PATH = None
        appmod.i18n.set_language("ja")
        shutil.rmtree(self._ledger_dir, ignore_errors=True)

    def tabs(self):
        a = self.a
        return {"容量マップ": a.overview_tab, "フォルダ一覧": a.folders_tab, "ファイル一覧": a.files_tab,
                "重複ファイル": a.dups_tab, "ごみ箱から復元": a.bin_tab}

    def test_最小幅でも各タブの行が切れない(self):
        # タブの中身の幅 = ウィンドウ幅 − 左右の余白（ノートブックの余白 + タブの内側の余白）
        usable = self.min_w - 2 * px(16) - 2 * px(10) - px(4)
        too_wide = []
        for name, tab in self.tabs().items():
            for row in tab.winfo_children():
                if row.winfo_manager() != "pack":
                    continue
                w = row.winfo_reqwidth()
                if any(isinstance(c, appmod.ttk.Scrollbar) and str(c.cget("orient")) == "horizontal"
                       for c in row.winfo_children()):
                    continue       # 横にスクロールできる一覧は、はみ出しても切れない
                if w > usable:
                    too_wide.append(f"{name}: {row.winfo_class()} {w}px > {usable}px 中身=" + ", ".join(f"{c.winfo_class()}:{c.winfo_reqwidth()}" for c in row.winfo_children()))
        self.assertEqual(too_wide, [], "最小幅で切れる行がある")

    def test_最小サイズが小さめの画面に収まる(self):
        # 幅 1366 の画面（表示倍率 100%）でも、ウィンドウの最小サイズが画面に収まること
        self.assertLessEqual(self.min_w, px(1300), f"最小幅 {self.min_w}px が広すぎる")
        self.assertLessEqual(self.min_h, px(700))

    def test_上部の操作欄も最小幅に収まる(self):
        header = self.a._toolbars[0]
        self.assertLessEqual(header.winfo_reqwidth(), self.min_w)

    def test_最小の高さでも一覧を表示する高さが残る(self):
        # ヘッダー・下の状態表示・タブの見出しを除いた高さから、各タブの固定部分を引いて一覧に残る高さ
        a = self.a
        chrome = a._toolbars[0].master.winfo_reqheight() if False else 0
        for w in a.winfo_children():
            if w is not a.tabs and w.winfo_manager() == "pack" and isinstance(w, appmod.ttk.Frame):
                chrome += w.winfo_reqheight()
        chrome += px(40)                          # タブの見出し
        short = []
        for name, tab in self.tabs().items():
            fixed = px(20)                        # タブの内側の余白
            for row in tab.winfo_children():
                if row.winfo_manager() != "pack":
                    continue
                if row.pack_info().get("expand") in (1, "1", True):
                    continue                      # 一覧（伸び縮みする部分）
                fixed += row.winfo_reqheight()
            left = self.min_h - chrome - fixed
            if left < px(120):                    # 一覧に最低 4 行ほど
                short.append(f"{name}: 一覧に残る高さ {left}px")
        self.assertEqual(short, [], "最小の高さで一覧がほとんど見えないタブがある")

    def test_確認画面と結果画面が画面に収まる(self):
        a = self.a
        sw, sh = a.winfo_screenwidth(), a.winfo_screenheight()
        d = Path(tempfile.mkdtemp(prefix="pmc_layout_"))
        try:
            items = [(appmod.FileItem(f"とても長い名前のファイル_{i:03d}.bin", str(d / "深い" / "場所"), 1000 + i,
                                      0.0, "unknown", None), "種類は不明") for i in range(40)]
            dialogs = [
                appmod.MultiTrashConfirmDialog(a, items, items[:3], note="注記" * 30, on_exclude=lambda o: None),
                appmod.TrashResultDialog(a, "一部だけ移動しました", "移動した: 1 件\n" * 3,
                                         [(i.name, i.dir, "移動しなかった: 理由" * 5) for i, _k in items]),
                appmod.LowRiskDialog(a, {"count": 1, "size": 1, "groups": 1, "kept_ok": 0, "excluded_count": 5,
                                         "excluded_size": 5, "excluded": [("理由", 5)] * 6, "current_checked": 2}),
            ]
            for dlg in dialogs:
                dlg.withdraw()
                dlg.update_idletasks()
                self.assertLessEqual(dlg.winfo_reqwidth(), sw, f"{dlg.title()} が画面より広い")
                self.assertLessEqual(dlg.winfo_reqheight(), sh, f"{dlg.title()} が画面より高い")
                dlg.destroy()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class LayoutEnglish(Layout):
    """英語の表示でも、最小サイズで行・操作欄が切れないこと（英語の文言は長くなりやすい）。"""
    LANG = "en"


if __name__ == "__main__":
    unittest.main(verbosity=2)
