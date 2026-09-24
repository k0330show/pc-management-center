"""フォルダの辿り方と、ファイル一覧の複数選択を確かめる。

一時フォルダだけを対象にし、実ファイルは移動しない。ごみ箱への移動は
呼ばれたことだけ記録して、こちらで消す。ウィンドウは隠したまま動かす。

見るのは:
  中身を開けること、今どこにいるか分かること、親へ戻れること
  別の操作で同じ場所を開いても表示が食い違わないこと
  複数選択の作法（右クリックで選択を壊さない、詳細は 1 件のみ）
  複数件の移動が、1 件ずつと同じ確かめ方を全件に対して行うこと
  中身を見られるようにしたことで、守っている場所が操作できるようになっていないこと
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
from app import App, FileItem


def pump(a: App, seconds: float = 3.0, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        a.update()
        if until is not None and until():
            return
        time.sleep(0.02)


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
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_browse_test_"))
        self.moved: list[str] = []
        self.confirmed = True
        self.refusals: list[tuple[str, str]] = []
        self.notices: list[tuple[str, str]] = []

        self._real_move = appmod.trash.move_to_trash

        def fake_move(path, size, hwnd=0):
            self.moved.append(path)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        appmod.trash.move_to_trash = fake_move

        # ごみ箱の記録は一時フォルダへ（利用者の記録ファイルには書かない）
        self._ledger_dir = tempfile.mkdtemp(prefix="pmc_ledger_")
        appmod.App.LEDGER_PATH = os.path.join(self._ledger_dir, "trash_log.json")
        # 移動は差し替えるので、ごみ箱の中を探し直しても見つからない（待たない）
        self._real_delays = appmod.recyclebin.RETRY_DELAYS
        appmod.recyclebin.RETRY_DELAYS = ()
        self.a = App(hidden=True)
        self.a.withdraw()
        self.a.raise_window = lambda w: None   # 詳細画面なども前面に出さない
        self.a.current_root = self.dir
        self.specs: list[dict] = []
        self.multi_specs: list[tuple[list, list]] = []
        self.a.confirm_trash = lambda spec: (self.specs.append(spec), self.confirmed)[1]
        self.a.confirm_trash_many = lambda mv, bl, **kw: (self.multi_specs.append((mv, bl)), self.multi_kw.append(kw), self.confirmed)[2]
        self.multi_kw: list = []
        self.a.notify = lambda title, text, error=False: self.notices.append((title, text))
        self.a.show_trash_results = lambda title, text, rows, error=False: (
            self.notices.append((title, text)), self.results.append(rows))
        self.results: list = []
        real_status = self.a.status
        self.a._refuse = lambda name, reason, detail="": (
            self.refusals.append((name, reason)), real_status.set(""))[1]

    def tearDown(self):
        appmod.trash.move_to_trash = self._real_move
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()      # 画面の部品の後始末を、このスレッドで済ませる
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self._ledger_dir, ignore_errors=True)
        appmod.App.LEDGER_PATH = None
        appmod.recyclebin.RETRY_DELAYS = self._real_delays

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self) -> None:
        self.a.start_scan()
        pump(self.a, 20, until=lambda: bool(self.a.file_rows))
        self.assertTrue(self.a.file_rows, "スキャンできていない")

    def file_iid(self, path: Path) -> str:
        for iid, item in self.a._file_iids.items():
            if Path(item.path) == path:
                return iid
        self.fail(f"ファイル一覧に {path} がない")


class TestFolderBrowse(Base):
    def setUp(self):
        super().setUp()
        self.write("親/子/深い.bin", b"D" * 4000)
        self.write("親/直下.bin", b"P" * 3000)
        self.write("別/ほか.bin", b"O" * 2000)
        self.scan()

    def test_はじめは対象フォルダの直下だけが出る(self):
        root = str(self.dir)
        self.assertTrue(self.a.folder_tree.exists(root), "対象フォルダの行がない")
        names = {os.path.basename(i) for i in self.a.folder_tree.get_children(root)}
        self.assertIn("親", names)
        self.assertIn("別", names)
        # 孫はまだ開いていない
        self.assertFalse(self.a.folder_tree.exists(str(self.dir / "親" / "子")),
                         "開いてもいない階層まで先に並べている")

    def test_行を開くと直下のフォルダとファイルが出る(self):
        oya = str(self.dir / "親")
        self.a.folder_tree.item(oya, open=True)
        self.a._fill_folder_children(oya)
        kids = self.a.folder_tree.get_children(oya)
        paths = {self.a.folder_tree.set(k, "path") for k in kids}
        self.assertIn(str(self.dir / "親" / "子"), paths, "直下のフォルダが出ていない")
        self.assertIn(str(self.dir / "親" / "直下.bin"), paths, "直下のファイルが出ていない")

    def test_今いる場所と親へ戻る道が出る(self):
        oya = str(self.dir / "親")
        self.a._fill_folder_children(oya)
        self.a.folder_tree.selection_set(oya)
        self.a._update_folder_here()
        self.assertEqual(self.a.folder_here.get(), oya, "今いる場所が出ていない")
        self.assertEqual(str(self.a.folder_up_btn["state"]), "normal", "親へ戻れない")
        self.a.open_parent_folder()
        self.assertEqual(self.a.folder_here.get(), str(self.dir))
        self.assertEqual(str(self.a.folder_up_btn["state"]), "disabled",
                         "対象フォルダより上へ行けてしまう")

    def test_中身のファイルから詳細画面へ進める(self):
        oya = str(self.dir / "親")
        self.a._fill_folder_children(oya)
        iid = "f:" + str(self.dir / "親" / "直下.bin")
        self.assertTrue(self.a.folder_tree.exists(iid))
        self.a.folder_tree.selection_set(iid)
        self.a.detail_folder_selected()
        self.assertIsNotNone(self.a.detail)
        kind, target = self.a.detail.current
        self.assertEqual(kind, "file")
        self.assertEqual(Path(target.path).name, "直下.bin")
        self.a.detail.withdraw()

    def test_別の操作で同じ場所を開いても食い違わない(self):
        deep = self.dir / "親" / "子"
        # 容量マップから開く
        self.a.show_in_map(deep)
        self.assertEqual(self.a.folder_here.get(), str(deep),
                         "マップで開いた場所と、フォルダ一覧の場所が食い違う")
        # 位置の絞り込みから開く
        self.a.folder_tree.selection_set(str(self.dir))
        self.a._update_folder_here()
        self.a.apply_filter("folder", (str(deep), False))
        self.assertEqual(self.a.folder_here.get(), str(deep),
                         "絞り込みで指した場所と、フォルダ一覧の場所が食い違う")

    def test_一覧の数字はスキャン結果と同じ(self):
        oya = self.dir / "親"
        self.a._fill_folder_children(str(oya))
        shown = self.a.folder_tree.set(str(oya), "size")
        self.assertEqual(shown, appmod.human_size(self.a.folder_sizes[oya]),
                         "フォルダ一覧の容量が、スキャン結果と違う")

    def test_中身から選んでも守っている場所は移せない(self):
        # 対象フォルダのルートは、中身を開いても操作できない
        self.a.folder_tree.selection_set(str(self.dir))
        self.a._update_folder_here()
        self.a.trash_selected_folder()
        self.assertEqual(self.moved, [], "スキャン対象のルートを移動してしまった")
        self.assertTrue(self.refusals or self.notices, "断った理由を伝えていない")


    def test_辿れるようになっても親より上へは出られない(self):
        """中身を開けるようにしたことで、スキャン対象の外が操作できるようになっていないか。"""
        root = str(self.dir)
        # 一覧に並ぶのは対象フォルダとその配下だけ
        def walk(iid):
            yield iid
            for c in self.a.folder_tree.get_children(iid):
                yield from walk(c)
        for iid in walk(root):
            if iid.endswith("|more"):
                continue
            path = iid[2:] if iid.startswith("f:") else iid
            self.assertTrue(os.path.normcase(path).startswith(os.path.normcase(root)),
                            f"対象フォルダの外が一覧に出ている: {path}")

    def test_守っている場所のファイルは中身から選んでも移せない(self):
        """保護の判定はパスで決まる。どの画面から選んでも同じに効く。"""
        real = self.a.protected_reason
        self.a.protected_reason = lambda path, is_dir: "守っている場所のため" if "直下" in path else real(path, is_dir)
        oya = str(self.dir / "親")
        self.a._fill_folder_children(oya)
        self.a.folder_tree.selection_set("f:" + str(self.dir / "親" / "直下.bin"))
        self.a.trash_selected_folder()
        self.assertEqual(self.moved, [], "守っている場所のファイルを移動した")
        self.assertTrue(self.refusals, "断った理由を伝えていない")
        self.a.protected_reason = real


class TestMultiSelect(Base):
    def setUp(self):
        super().setUp()
        self.p1 = self.write("m1.bin", b"1" * 1000)
        self.p2 = self.write("m2.bin", b"2" * 2000)
        self.p3 = self.write("m3.bin", b"3" * 3000)
        self.scan()

    def test_複数選べる(self):
        iids = [self.file_iid(p) for p in (self.p1, self.p2)]
        self.a.file_tree.selection_set(iids)
        self.a._update_file_selection_note()
        self.assertEqual(len(self.a._selected_files()), 2)
        note = self.a.file_selection_note.get()
        self.assertIn("2", note)
        self.assertIn("合計", note, "合計容量を出していない")

    def test_詳細は一件のときだけ(self):
        self.a.file_tree.selection_set([self.file_iid(self.p1), self.file_iid(self.p2)])
        self.a.detail_file_selected()
        self.assertIsNone(self.a.detail, "複数選択のまま詳細を開いている")
        self.assertIn("1 件ずつ", self.a.file_note_extra.get(), "複数選択時の扱いを伝えていない")

    def test_選択中の行を右クリックしても選択を壊さない(self):
        iids = [self.file_iid(self.p1), self.file_iid(self.p2)]
        self.a.file_tree.selection_set(iids)

        class Ev:
            x_root = y_root = 0
            y = 0

        # 選択中の行を指した状態で右クリック
        self.a.file_tree.identify_row = lambda _y: iids[0]
        popped = {}
        real_menu = appmod.tk.Menu

        class FakeMenu(real_menu):
            def tk_popup(self, *a, **kw):
                popped["yes"] = True

        appmod.tk.Menu = FakeMenu
        try:
            self.a._file_context_menu(Ev())
        finally:
            appmod.tk.Menu = real_menu
        self.assertEqual(set(self.a.file_tree.selection()), set(iids),
                         "選択中の行を右クリックしたら選択が 1 件に戻された")
        self.assertTrue(popped.get("yes"), "メニューが出ていない")

    def test_選んでいない行を右クリックするとその行に切り替わる(self):
        self.a.file_tree.selection_set([self.file_iid(self.p1)])
        other = self.file_iid(self.p3)

        class Ev:
            x_root = y_root = 0
            y = 0

        self.a.file_tree.identify_row = lambda _y: other
        real_menu = appmod.tk.Menu

        class FakeMenu(real_menu):
            def tk_popup(self, *a, **kw):
                pass

        appmod.tk.Menu = FakeMenu
        try:
            self.a._file_context_menu(Ev())
        finally:
            appmod.tk.Menu = real_menu
        self.assertEqual(self.a.file_tree.selection(), (other,),
                         "選んでいない行を右クリックしても、その行に切り替わらない")


class TestMultiTrash(Base):
    def setUp(self):
        super().setUp()
        self.p1 = self.write("t1.bin", b"1" * 1000)
        self.p2 = self.write("t2.bin", b"2" * 2000)
        self.p3 = self.write("t3.bin", b"3" * 3000)
        self.scan()

    def select(self, *paths):
        self.a.file_tree.selection_set([self.file_iid(p) for p in paths])
        self.a._update_file_selection_note()

    def test_確認画面に全件と合計が出る(self):
        self.select(self.p1, self.p2, self.p3)
        self.confirmed = False
        self.a.trash_selected_file()
        self.assertEqual(len(self.multi_specs), 1, "複数件の確認画面が出ていない")
        movable, blocked = self.multi_specs[0]
        self.assertEqual(len(movable), 3, "全件が確認の対象になっていない")
        self.assertEqual(sum(i.size for i, _ in movable), 6000)
        self.assertEqual(self.moved, [], "キャンセルしたのに移動した")

    def test_全件移動して一覧と容量が揃う(self):
        before = self.a.total_size
        self.select(self.p1, self.p2)
        self.a.trash_selected_file()
        self.assertEqual(len(self.moved), 2)
        self.assertFalse(self.p1.exists())
        self.assertFalse(self.p2.exists())
        self.assertEqual(self.a.total_size, before - 3000)
        names = {f.name for f in self.a.file_rows}
        self.assertNotIn("t1.bin", names)
        self.assertNotIn("t2.bin", names)

    def test_変わっていたものだけ移さない(self):
        self.select(self.p1, self.p2)
        self.p2.write_bytes(b"X" * 2000)
        os.utime(self.p2, (time.time(), time.time() + 5))
        self.a.trash_selected_file()
        self.assertEqual(len(self.moved), 1, "変わったファイルまで移動した")
        self.assertFalse(self.p1.exists())
        self.assertTrue(self.p2.exists(), "検出後に変わったファイルを移動してしまった")
        self.assertTrue(any("一部" in t for t, _ in self.notices),
                        "成功と失敗を分けて伝えていない")

    def test_一件だけ選んだときは従来どおり(self):
        self.select(self.p3)
        self.a.trash_selected_file()
        self.assertEqual(len(self.specs), 1, "1 件用の確認画面が出ていない")
        self.assertEqual(self.multi_specs, [], "1 件なのに複数件の画面が出ている")
        self.assertEqual(len(self.moved), 1)


class TestFolderMultiSelect(Base):
    """フォルダ一覧でも、離れた行の複数選択・右クリック・まとめての移動ができること。"""

    def setUp(self):
        super().setUp()
        self.write("f1/a.bin", b"A" * 1000)
        self.write("f2/b.bin", b"B" * 2000)
        self.write("f3/c.bin", b"C" * 4000)
        self.loose = self.write("loose.bin", b"L" * 500)
        self.scan()
        self.row = {n: str(self.dir / n) for n in ("f1", "f2", "f3")}
        self.row["loose"] = "f:" + str(self.loose)

    def test_離れた行を選ぶと件数と合計を出す(self):
        self.a.folder_tree.selection_set([self.row["f1"], self.row["f3"]])   # 間の f2 は選ばない
        self.a._update_folder_selection_note()
        note = self.a.folder_selection_note.get()
        self.assertIn("選択 2 件", note)
        self.assertIn(appmod.human_size(5000), note)

    def test_選択中の行を右クリックしても選択を壊さない(self):
        rows = [self.row["f1"], self.row["f3"]]
        self.a.folder_tree.selection_set(rows)
        self.a.folder_tree.identify_row = lambda _y: rows[1]
        real_menu = appmod.tk.Menu

        class FakeMenu(real_menu):
            def tk_popup(self, *a, **kw):
                pass

        class Ev:
            x_root = y_root = y = 0

        appmod.tk.Menu = FakeMenu
        try:
            self.a._folder_context_menu(Ev())
        finally:
            appmod.tk.Menu = real_menu
        self.assertEqual(set(self.a.folder_tree.selection()), set(rows))

    def test_詳細は一件のときだけ(self):
        self.a.folder_tree.selection_set([self.row["f1"], self.row["f3"]])
        self.a.detail_folder_selected()
        self.assertIsNone(self.a.detail)
        self.assertIn("1 件ずつ", self.a.status.get())

    def test_フォルダとファイルをまとめて確認して移す(self):
        before = self.a.total_size
        self.a.folder_tree.selection_set([self.row["f1"], self.row["f3"], self.row["loose"]])
        self.a.trash_selected_folder()
        self.assertEqual(len(self.multi_specs), 1, "まとめての確認画面が出ていない")
        movable, blocked = self.multi_specs[0]
        self.assertEqual(len(movable), 3)
        self.assertEqual(sum(i.size for i, _ in movable), 5500)
        self.assertFalse((self.dir / "f1").exists())
        self.assertFalse((self.dir / "f3").exists())
        self.assertFalse(self.loose.exists())
        self.assertTrue((self.dir / "f2").exists(), "選んでいないフォルダを移した")
        self.assertEqual(self.a.total_size, before - 5500)
        self.assertNotIn(self.dir / "f1", self.a.folder_sizes)

    def test_フォルダの中の行も選んでいたら二重に数えない(self):
        oya = str(self.dir / "f1")
        self.a._fill_folder_children(oya)
        inner = "f:" + str(self.dir / "f1" / "a.bin")
        self.a.folder_tree.selection_set([oya, inner, self.row["f2"]])
        self.confirmed = False
        self.a.trash_selected_folder()
        movable, _blocked = self.multi_specs[0]
        self.assertEqual(len(movable), 2)
        self.assertEqual(sum(i.size for i, _ in movable), 3000)

    def test_変わったフォルダと守っている場所はまとめては移さない(self):
        (self.dir / "f3" / "new.bin").write_bytes(b"N" * 10)       # スキャン後に中身が増えた
        self.a.folder_tree.selection_set([self.row["f1"], self.row["f3"], str(self.dir)])
        self.a.trash_selected_folder()
        movable, blocked = self.multi_specs[0]
        self.assertEqual([i.name for i, _ in movable], ["f1"])
        reasons = " ".join(w for _i, w in blocked)
        self.assertIn("中身が変化", reasons)
        self.assertTrue((self.dir / "f3").exists())
        self.assertTrue(self.dir.exists())
        self.assertFalse((self.dir / "f1").exists())
        self.assertTrue(any("一部" in t for t, _ in self.notices), "移さなかったものを伝えていない")

    def test_確認後に中身が変わったフォルダは移さない(self):
        def confirm(mv, bl, **kw):
            (self.dir / "f1" / "late.bin").write_bytes(b"x")      # 確認画面を開いている間に変わる
            return True
        self.a.confirm_trash_many = confirm
        self.a.folder_tree.selection_set([self.row["f1"], self.row["f2"]])
        self.a.trash_selected_folder()
        self.assertTrue((self.dir / "f1").exists(), "確認中に変わったフォルダを移した")
        self.assertFalse((self.dir / "f2").exists())
        title, text = self.notices[-1]
        self.assertIn("一部", title)
        self.assertIn("中身が変化", text)


class FakeEv:
    def __init__(self, state=0):
        self.x = self.y = self.x_root = self.y_root = 0
        self.state = state


class TestChecks(Base):
    """ファイル一覧・フォルダ一覧の「移動」チェックと、まとめての移動。"""

    def setUp(self):
        super().setUp()
        self.f1 = self.write("d1/a.bin", b"A" * 1000)
        self.f2 = self.write("d1/b.bin", b"B" * 2000)
        self.f3 = self.write("d2/c.bin", b"C" * 4000)
        self.f4 = self.write("d2/sub/d.bin", b"D" * 8000)
        self.scan()

    def click_file_check(self, path, state=0):
        tree = self.a.file_tree
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: "#1"
        iid = self.file_iid(path)
        tree.identify_row = lambda y: iid
        return self.a._file_check_click(FakeEv(state))

    def item(self, path):
        return next(f for f in self.a.file_rows if Path(f.path) == path)

    def test_チェック欄のクリックとShift範囲と合計(self):
        self.a.sort_files("name")          # a, b, c, d の順（昇順になるまで押す）
        if [self.a._file_iids[i].name for i in self.a.file_tree.get_children()][0] != "a.bin":
            self.a.sort_files("name")
        self.assertEqual(self.click_file_check(self.f1), "break")
        self.click_file_check(self.f3, state=0x0001)       # Shift: a〜c
        self.assertEqual({f.name for f in self.a.check_files.values()}, {"a.bin", "b.bin", "c.bin"})
        note = self.a.check_note.get()
        self.assertIn("チェック 3 件", note)
        self.assertIn(appmod.human_size(7000), note)
        self.assertEqual(self.a.file_tree.set(self.file_iid(self.f2), "check"), "☑")
        self.click_file_check(self.f2)                     # 個別に外す
        self.assertNotIn(id(self.item(self.f2)), self.a.check_files)

    def test_チェック欄以外のクリックは行選択に任せる(self):
        tree = self.a.file_tree
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: "#3"
        tree.identify_row = lambda y: self.file_iid(self.f1)
        self.assertIsNone(self.a._file_check_click(FakeEv(0x0004)))    # Ctrl+クリック
        self.assertEqual(self.a.check_files, {})

    def test_離れた行を選んでSpaceでチェック(self):
        self.a.file_tree.selection_set([self.file_iid(self.f1), self.file_iid(self.f4)])
        self.a._space_check("files")
        self.assertEqual({f.name for f in self.a.check_files.values()}, {"a.bin", "d.bin"})

    def test_並べ替えや絞り込みでチェックは変わらない(self):
        self.a.file_tree.selection_set([self.file_iid(self.f1), self.file_iid(self.f3)])
        self.a._space_check("files")
        before = dict(self.a.check_files)
        self.a.sort_files("size")
        self.a.search_text.set("a.bin")
        self.a.render_files()
        self.assertEqual(self.a.check_files, before, "絞り込み・並べ替えでチェックが変わった")
        self.assertIn("チェック 2 件", self.a.check_note.get())
        self.a.search_text.set("")
        self.a.checked_only.set(True)
        self.a.render_files()
        self.assertEqual({f.name for f in self.a._file_iids.values()}, {"a.bin", "c.bin"})
        self.a.checked_only.set(False)

    def test_フォルダとその中のファイルを両方チェックしても二重に処理しない(self):
        d2 = self.dir / "d2"
        self.a._set_checked("dir", d2, True)
        self.a._set_checked("file", self.item(self.f3), True)
        self.a._set_checked("file", self.item(self.f1), True)
        self.a._update_check_views()
        self.assertIn("チェックしたフォルダの中", self.a.check_note.get())
        self.a.trash_checked()
        movable, _blocked = self.multi_specs[-1]
        self.assertEqual(len(movable), 2, "フォルダの中のファイルを別に数えている")
        self.assertIn("一緒に移動", self.multi_kw[-1]["note"])
        self.assertTrue(any(isinstance(o, appmod.FolderTarget) for o, _k in movable))
        self.assertFalse(d2.exists())
        self.assertFalse(self.f1.exists())
        self.assertEqual(self.moved.count(str(self.f3)), 0, "中のファイルを二重に移した")
        self.assertEqual(self.a.check_files, {})
        self.assertEqual(self.a.check_dirs, {})
        self.assertNotIn(d2, self.a.treemap.child_dirs.get(self.dir, []), "容量マップが古いまま")
        self.assertEqual(self.a.total_size, 2000)

    def test_フォルダ一覧でもチェックでき_中の行は含まれる印になる(self):
        tree = self.a.folder_tree
        d2 = str(self.dir / "d2")
        self.a._fill_folder_children(d2)
        tree.item(d2, open=True)
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: "#1"
        tree.identify_row = lambda y: d2
        self.assertEqual(self.a._folder_check_click(FakeEv()), "break")
        self.assertIn(d2, self.a.check_dirs)
        self.assertEqual(tree.set("f:" + str(self.f3), "check"), "▣", "中の行に「含まれる」印がない")
        self.assertEqual(self.a.file_tree.set(self.file_iid(self.f3), "check"), "▣",
                         "ファイル一覧にもフォルダのチェックが反映されていない")

    def test_確認画面で個別に外せる(self):
        for p in (self.f1, self.f2, self.f3):
            self.a._set_checked("file", self.item(p), True)
        items = [(self.item(p), "x") for p in (self.f1, self.f2, self.f3)]
        d = appmod.MultiTrashConfirmDialog(self.a, items, [], on_exclude=self.a._uncheck_objs)
        try:
            row = next(i for i, o in d._row_obj.items() if o is self.item(self.f2))
            d.tree.selection_set(row)
            d.exclude_selected()
            self.assertEqual(len(items), 2, "確認画面の対象から外れていない")
            self.assertNotIn(id(self.item(self.f2)), self.a.check_files)
            self.assertIn("2 件を移動する", d.move_btn.cget("text"))
            self.assertEqual(str(d.move_btn.cget("state")), "disabled", "確認のチェックなしで押せる")
        finally:
            d.destroy()

    def test_一部失敗は対象ごとに結果を出す(self):
        for p in (self.f1, self.f2, self.f3):
            self.a._set_checked("file", self.item(p), True)

        def confirm(mv, bl, **kw):
            self.f2.write_bytes(b"changed!!")      # 確認中に変わった
            return True
        self.a.confirm_trash_many = confirm
        self.a.trash_checked()
        rows = self.results[-1]
        by_name = {r[0]: r[2] for r in rows}
        self.assertEqual(by_name["a.bin"], "移動した")
        self.assertTrue(by_name["b.bin"].startswith("移動しなかった"))
        self.assertEqual(by_name["c.bin"], "移動した")
        self.assertTrue(self.f2.exists())
        self.assertIn("一部", self.notices[-1][0])
        self.assertEqual(self.a.check_files, {}, "移動しなかったもののチェックが残っている")


class TestCheckedGroupOps(Base):
    """チェックした項目を、右クリック・ボタン・Delete キーでひとまとまりとして扱う。"""

    def setUp(self):
        super().setUp()
        self.f1 = self.write("d1/a.bin", b"A" * 1000)
        self.f2 = self.write("d1/b.bin", b"B" * 2000)
        self.f3 = self.write("d2/c.bin", b"C" * 4000)
        self.f4 = self.write("d2/sub/d.bin", b"D" * 8000)
        self.scan()
        self.confirmed = False           # 既定では確認画面で止める（移動しない）

    def item(self, path):
        return next(f for f in self.a.file_rows if Path(f.path) == path)

    def check(self, *paths):
        for p in paths:
            self.a._set_checked("file", self.item(p), True)
        self.a._update_check_views()

    def menu_on(self, tree_name, iid):
        """右クリックのメニューを作らせ、項目（add_command の引数）の一覧を返す（メニューは表示しない）。"""
        entries = []
        real_menu = appmod.tk.Menu

        class RecMenu(real_menu):
            def add_command(self, **kw):
                entries.append(kw)
                super().add_command(**kw)

            def tk_popup(self, *a, **kw):
                pass
        tree = getattr(self.a, tree_name)
        tree.identify_row = lambda _y: iid
        appmod.tk.Menu = RecMenu
        try:
            if tree_name == "file_tree":
                self.a._file_context_menu(FakeEv())
            else:
                self.a._folder_context_menu(FakeEv())
        finally:
            appmod.tk.Menu = real_menu
        return entries

    def run_entry(self, entries, prefix):
        e = next((e for e in entries if str(e.get("label", "")).startswith(prefix)), None)
        self.assertIsNotNone(e, f"メニューに「{prefix}…」がない: {[x.get('label') for x in entries]}")
        e["command"]()

    def test_チェックした行は濃い青になり_外すと戻る(self):
        iid = self.file_iid(self.f2)
        before = self.a.file_tree.item(iid, "tags")
        self.check(self.f2)
        self.assertIn("checked", self.a.file_tree.item(iid, "tags"))
        self.assertEqual(str(self.a.file_tree.tag_configure("checked", "background")), appmod.P["check_row"])
        self.assertEqual(str(self.a.file_tree.tag_configure("checked", "foreground")), appmod.P["check_row_text"])
        self.assertNotEqual(appmod.P["check_row"], appmod.P["select"], "行の選択と同じ色では見分けられない")
        self.a._set_checked("file", self.item(self.f2), False)
        self.a._update_check_views()
        self.assertEqual(tuple(self.a.file_tree.item(iid, "tags")), tuple(before), "外しても色が戻らない")

    def test_フォルダをチェックすると中の行は含まれる色になる(self):
        d2 = str(self.dir / "d2")
        self.a._fill_folder_children(d2)
        self.a._set_checked("dir", self.dir / "d2", True)
        self.a._update_check_views()
        self.assertIn("checked", self.a.folder_tree.item(d2, "tags"))
        self.assertIn("covered", self.a.folder_tree.item("f:" + str(self.f3), "tags"))
        self.assertIn("covered", self.a.file_tree.item(self.file_iid(self.f3), "tags"))

    def test_並べ替え_絞り込みのあとも色とチェックが保たれる(self):
        self.check(self.f1, self.f3)
        self.a.sort_files("size")
        self.a.sort_files("name")
        for p in (self.f1, self.f3):
            self.assertIn("checked", self.a.file_tree.item(self.file_iid(p), "tags"))
        self.assertNotIn("checked", self.a.file_tree.item(self.file_iid(self.f2), "tags"))
        self.a.search_text.set("c.bin")
        self.a.render_files()
        self.assertIn("checked", self.a.file_tree.item(self.file_iid(self.f3), "tags"))
        self.assertEqual(len(self.a.check_files), 2)

    def test_チェック済みの行を右クリックするとチェック全件が対象(self):
        self.check(self.f1, self.f2, self.f3)
        self.a.file_tree.selection_set(self.file_iid(self.f4))   # 選択はチェックと関係ない行
        entries = self.menu_on("file_tree", self.file_iid(self.f2))
        labels = [str(e.get("label")) for e in entries]
        self.assertIn("チェックした 3 件をごみ箱へ移動…", labels)
        self.assertFalse(any(label.startswith("この 1 件") for label in labels))
        self.run_entry(entries, "チェックした 3 件")
        movable, _bl = self.multi_specs[-1]
        self.assertEqual({o.name for o, _k in movable}, {"a.bin", "b.bin", "c.bin"})
        self.assertEqual(sum(o.size for o, _k in movable), 7000)
        self.assertEqual(self.moved, [], "確認でキャンセルしたのに移動した")

    def test_チェックのない行を右クリックするとその1件だけで_チェックは外さない(self):
        self.check(self.f1, self.f2)
        entries = self.menu_on("file_tree", self.file_iid(self.f3))
        labels = [str(e.get("label")) for e in entries]
        self.assertIn("この 1 件（c.bin）だけをごみ箱へ移動…", labels)
        self.assertIn("チェックした 2 件をごみ箱へ移動…（この行は含みません）", labels)
        self.confirmed = True
        self.run_entry(entries, "この 1 件")
        self.assertEqual(self.moved, [str(self.f3)], "右クリックした 1 件以外も移動した")
        self.assertEqual({f.name for f in self.a.check_files.values()}, {"a.bin", "b.bin"},
                         "ほかの行のチェックが外れた")
        self.assertTrue(self.f1.exists() and self.f2.exists())

    def test_ボタンとDeleteはチェックがあればチェック全件(self):
        self.check(self.f1, self.f3)
        self.a.file_tree.selection_set(self.file_iid(self.f2))    # 選んでいるのは別の行
        self.a.trash_selected_file()                                # ボタン・Delete キーと同じ処理
        movable, _bl = self.multi_specs[-1]
        self.assertEqual({o.name for o, _k in movable}, {"a.bin", "c.bin"}, "選択の行を対象にした")
        self.a.folder_tree.selection_set(str(self.dir / "d1"))
        self.a.trash_selected_folder()
        movable, _bl = self.multi_specs[-1]
        self.assertEqual({o.name for o, _k in movable}, {"a.bin", "c.bin"})

    def test_チェックがなければ従来どおり選択が対象(self):
        self.a.file_tree.selection_set([self.file_iid(self.f1), self.file_iid(self.f2)])
        self.a.trash_selected_file()
        movable, _bl = self.multi_specs[-1]
        self.assertEqual({o.name for o, _k in movable}, {"a.bin", "b.bin"})

    def test_絞り込みで隠れたチェックの件数を確認画面に出す(self):
        self.check(self.f1, self.f2, self.f3)
        self.a.search_text.set("a.bin")
        self.a.render_files()
        entries = self.menu_on("file_tree", self.file_iid(self.f1))
        self.assertTrue(any("この一覧に表示されていません" in str(e.get("label")) for e in entries))
        self.run_entry(entries, "チェックした 3 件")
        warn = self.multi_kw[-1].get("warn") or ""
        self.assertIn("表示されていない 2 件", warn)
        movable, _bl = self.multi_specs[-1]
        self.assertEqual(len(movable), 3, "見えている行だけを対象にした")

    def test_確認画面に見えていない件数を目立たせて出す(self):
        items = [(self.item(p), "x") for p in (self.f1, self.f2)]
        d = appmod.MultiTrashConfirmDialog(self.a, items, [], warn="今の一覧に表示されていない 1 件も対象に含まれます")
        try:
            warn_labels = [w for w in d.winfo_children()[0].winfo_children()
                           if isinstance(w, appmod.ttk.Label) and str(w.cget("style")) == "Warn.TLabel"]
            self.assertTrue(any("表示されていない 1 件" in str(w.cget("text")) for w in warn_labels))
        finally:
            d.destroy()

    def test_フォルダ一覧でも同じ操作(self):
        d2 = self.dir / "d2"
        self.a._set_checked("dir", d2, True)
        self.a._set_checked("file", self.item(self.f1), True)   # ファイル一覧で付けたチェックも共通
        self.a._update_check_views()
        entries = self.menu_on("folder_tree", str(d2))
        self.run_entry(entries, "チェックした 2 件")
        movable, _bl = self.multi_specs[-1]
        self.assertEqual(len(movable), 2)
        warn = self.multi_kw[-1].get("warn") or ""
        self.assertIn("表示されていない 1 件", warn, "折りたたまれて見えていないファイルの件数がない")
        # チェックのないフォルダを右クリック → その 1 件だけ
        entries = self.menu_on("folder_tree", str(self.dir / "d1"))
        self.assertTrue(any(str(e.get("label")).startswith("この 1 件（d1）") for e in entries))

    def test_フォルダと中身を両方チェックしても二重にしない_移動後は表示も更新(self):
        self.a._set_checked("dir", self.dir / "d2", True)
        self.check(self.f3, self.f1)
        self.confirmed = True
        self.a.trash_selected_file()
        movable, _bl = self.multi_specs[-1]
        self.assertEqual(len(movable), 2)
        self.assertIn("一緒に移動", self.multi_kw[-1]["note"])
        self.assertFalse((self.dir / "d2").exists())
        self.assertFalse(self.f1.exists())
        self.assertEqual(self.a.check_files, {})
        self.assertEqual(self.a.check_dirs, {})
        self.assertIn("チェックなし", self.a.check_note.get())
        for iid in self.a.file_tree.get_children():
            self.assertFalse({"checked", "covered"} & set(self.a.file_tree.item(iid, "tags")))
        self.assertNotIn(self.dir / "d2", self.a.treemap.child_dirs.get(self.dir, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
