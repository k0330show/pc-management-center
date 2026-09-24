"""一覧の行の色（チェック・行の選択・縞模様が重なったとき）で、文字が読めることを確かめる。

ttk.Treeview では、同じ行に色を指定したタグが複数あるときや、選択の色（スタイルの map）と
タグの色が重なるときに、どちらの背景・文字色が使われるかが順番や状態で変わる。
そこで「どちらが優先されても読める」ことを確かめる:
  背景の候補 = 行のタグの背景すべて（＋選んでいれば選択の背景。タグに背景が無ければ一覧の背景）
  文字の候補 = 行のタグの文字色すべて（＋選んでいれば選択の文字色。タグに文字色が無ければ一覧の文字色）
  → すべての組み合わせでコントラスト比 4.5 以上（WCAG の本文の基準）
画面は一度も表示しない（App(hidden=True)）。描画した画像ではなく、色の値で確かめる。
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
import i18n
from app import App

MIN_CONTRAST = 4.5


def _lum(color: str) -> float:
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def _norm(tree, color) -> str | None:
    if not color:
        return None
    r, g, b = tree.winfo_rgb(str(color))
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"


def worst_row_contrast(tree, iid) -> tuple[float, str, str]:
    """その行で起こりうる、いちばん読みにくい（背景, 文字）の組み合わせ。"""
    style = appmod.ttk.Style(tree)
    base_bg = _norm(tree, style.lookup("Treeview", "background")) or "#ffffff"
    base_fg = _norm(tree, style.lookup("Treeview", "foreground")) or "#000000"
    sel_bg = _norm(tree, style.lookup("Treeview", "background", ("selected",)))
    sel_fg = _norm(tree, style.lookup("Treeview", "foreground", ("selected",)))
    bgs, fgs = set(), set()
    for tag in tree.item(iid, "tags"):
        bg = _norm(tree, tree.tag_configure(tag, "background"))
        fg = _norm(tree, tree.tag_configure(tag, "foreground"))
        if bg:
            bgs.add(bg)
        if fg:
            fgs.add(fg)
    if not bgs:
        bgs.add(base_bg)
    if not fgs:
        fgs.add(base_fg)
    if iid in tree.selection():
        bgs.add(sel_bg)
        fgs.add(sel_fg)
    return min((contrast(f, b), b, f) for b in bgs for f in fgs)


class Base(unittest.TestCase):
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
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_color_test_"))
        self.aux = Path(tempfile.mkdtemp(prefix="pmc_color_aux_"))
        appmod.App.LEDGER_PATH = str(self.aux / "trash_log.json")
        appmod.App.SETTINGS_PATH = str(self.aux / "settings.json")
        self._real_move = appmod.trash.move_to_trash
        appmod.trash.move_to_trash = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("移動しない"))
        self.a = App(hidden=True, language=self.LANG)
        self.a.raise_window = lambda w: None
        self.a.current_root = self.dir
        for i in range(60):                               # スクロールが要る行数
            self.write(f"d{i % 3}/f{i:02d}.bin", bytes([65 + i % 20]) * (1000 + (i % 7) * 100))
        self.write("d1/sub/g.bin", b"G" * 500)
        self.a.start_scan()
        end = time.monotonic() + 20
        while time.monotonic() < end and not self.a.file_rows:
            self.a.update()
            time.sleep(0.02)
        self.assertTrue(self.a.file_rows)

    def tearDown(self):
        appmod.trash.move_to_trash = self._real_move
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()
        appmod.App.LEDGER_PATH = None
        appmod.App.SETTINGS_PATH = None
        i18n.set_language("ja")
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.aux, ignore_errors=True)

    def write(self, rel, data):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def assert_readable(self, tree, where):
        bad = []
        stack = list(tree.get_children(""))
        while stack:
            iid = stack.pop()
            stack.extend(tree.get_children(iid))
            c, bg, fg = worst_row_contrast(tree, iid)
            if c < MIN_CONTRAST:
                bad.append(f"{tree.item(iid, 'tags')} sel={iid in tree.selection()} bg={bg} fg={fg} {c:.2f}")
        self.assertEqual(bad[:5], [], f"{where}: 読みにくい行がある（{len(bad)} 行）")

    def file_iids(self):
        return list(self.a.file_tree.get_children(""))


class TestFileAndFolderRows(Base):
    def test_直す前の付け方は読めない_と判定できる(self):
        """テストの物差しの確認: 縞模様とチェックの両方の色を付けると、読めない組み合わせになる。"""
        tree = self.a.file_tree
        iid = self.file_iids()[1]
        tree.item(iid, tags=("odd", "checked"))              # v0.10 の付け方
        c, _bg, _fg = worst_row_contrast(tree, iid)
        self.assertLess(c, 1.5, "直す前の付け方を読めないと判定できていない")

    def test_複数チェック_別の行をクリック_スクロール(self):
        a, tree = self.a, self.a.file_tree
        rows = self.file_iids()
        for iid in rows[:12]:                                 # 縞の行も縞でない行も含めて 12 行
            a._set_checked("file", a._file_iids[iid], True)
        a._update_check_views()
        self.assert_readable(tree, "複数チェック")
        tree.selection_set(rows[3])                           # チェックした行を選ぶ
        a.update()
        self.assert_readable(tree, "チェックした行を選ぶ")
        tree.selection_set(rows[20])                          # 別の（チェックしていない）行をクリック
        a.update()
        self.assert_readable(tree, "チェック後に別の行を選ぶ")
        self.assertEqual(tree.item(rows[3], "tags")[0], "checked", "選択を外れた行の色が戻っていない")
        tree.selection_set(rows[2:9])                         # チェックした行を含めて複数選ぶ
        a.update()
        self.assert_readable(tree, "複数選択")
        tree.yview_moveto(1.0)                                # スクロール（色はタグなので変わらない）
        a.update()
        tree.yview_moveto(0.0)
        self.assert_readable(tree, "スクロール")

    def test_絞り込み_並べ替え_タブ切り替え(self):
        a, tree = self.a, self.a.file_tree
        for f in a.file_rows[::3]:
            a._set_checked("file", f, True)
        a._set_checked("dir", self.dir / "d1", True)          # フォルダのチェック（中の行は ▣）
        a._update_check_views()
        for col in ("size", "name", "check", "modified"):
            a.sort_files(col)
            tree.selection_set(self.file_iids()[:4])
            a.update()
            self.assert_readable(tree, f"並べ替え {col}")
        a.search_text.set("f1")
        a.render_files()
        tree.selection_set(self.file_iids()[:2])
        a.update()
        self.assert_readable(tree, "絞り込み")
        a.checked_only.set(True)
        a.render_files()
        self.assert_readable(tree, "チェックしたものだけ")
        a.checked_only.set(False)
        a.search_text.set("")
        for tab in (a.folders_tab, a.dups_tab, a.files_tab, a.overview_tab, a.files_tab):
            a.tabs.select(tab)
            a.update()
        self.assert_readable(tree, "タブ切り替え")

    def test_フォルダ一覧(self):
        a, tree = self.a, self.a.folder_tree
        for d in ("d0", "d1", "d2", "d1/sub"):
            a._fill_folder_children(str(self.dir / d))
            tree.item(str(self.dir / d), open=True)
        a._set_checked("dir", self.dir / "d1", True)
        for f in a.dir_index[str(self.dir / "d0")][:5]:
            a._set_checked("file", f, True)
        a._update_check_views()
        self.assert_readable(tree, "フォルダ一覧")
        tree.selection_set([str(self.dir / "d1"), str(self.dir / "d0")])
        a.update()
        self.assert_readable(tree, "フォルダ一覧で選ぶ")
        self.assertEqual(tree.item(str(self.dir / "d1"), "tags")[0], "checked_sel")
        tree.selection_set(str(self.dir / "d2"))
        a.update()
        self.assertEqual(tree.item(str(self.dir / "d1"), "tags")[0], "checked")
        self.assert_readable(tree, "フォルダ一覧で別の行を選ぶ")

    def test_重複ファイル一覧(self):
        a = self.a
        a.start_dup_scan()
        end = time.monotonic() + 20
        while time.monotonic() < end and a.dup_running:
            a.update()
            time.sleep(0.02)
        a.dup_select_all("all")
        tree = a.dup_tree
        self.assert_readable(tree, "重複ファイル一覧")
        tree.selection_set(list(a._dup_rows)[:6])
        a.update()
        self.assert_readable(tree, "重複ファイル一覧で選ぶ")


class TestAfterLanguageSwitch(Base):
    def test_日本語と英語の切り替え後も読める(self):
        a = self.a
        for f in a.file_rows[:10]:
            a._set_checked("file", f, True)
        a._update_check_views()
        for lang in ("en", "ja", "en"):
            self.assertTrue(a.set_language(lang))
            a.tabs.select(a.files_tab)
            a.file_tree.selection_set(a.file_tree.get_children("")[:3])
            a.update()
            self.assert_readable(a.file_tree, f"切り替え後 {lang}")
            self.assert_readable(a.folder_tree, f"切り替え後 {lang}（フォルダ一覧）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
