"""重複結果の「まとめて選ぶ・まとめて移す」を、実際の App を動かして確かめる。

**一時フォルダだけ**を対象にし、ごみ箱への移動は差し替える（呼ばれたことを記録して自分で消す）。
ウィンドウは隠したまま動かし、確認画面・進み具合も差し替えるので、画面には何も出ない。
確認画面そのものを確かめるときも、作るだけで表示しない（ask() を呼ばない）。

見るのは:
  各組で最低 1 件は必ず残ること、残す側を移動前に確認・変更できること
  「すべて」が表示中だけか結果全体かを区別し、確認画面に出すこと
  2 段階の確認がそれぞれ独立に止められること（既定はキャンセル）
  低リスク候補が、誤判定しうるものを候補に入れないこと
  実行直前に変化・消失・保護を 1 件ずつ確かめ、成功・失敗・未実行を分けること
"""

from __future__ import annotations

import csv
import gc
import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

import app as appmod
from app import App
from classifier import FolderContext


def pump(a: App, seconds: float = 3.0, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        a.update()
        if until is not None and until():
            return
        time.sleep(0.02)


class FakeProgress:
    def __init__(self, cancel_after=None):
        self.cancelled = False
        self.cancel_after = cancel_after
        self.done = 0
        self.closed = False

    def step(self, done, moved, failed):
        self.done = done
        if self.cancel_after is not None and done >= self.cancel_after:
            self.cancelled = True

    def close(self):
        self.closed = True


class FakeEvent:
    def __init__(self, state=0):
        self.x = self.y = self.x_root = self.y_root = 0
        self.state = state


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
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_bulk_test_"))
        self.moved: list[str] = []
        self.notices: list[tuple[str, str, bool]] = []
        self.stage1: list[dict] = []
        self.stage2_calls = 0
        self.answer1 = True
        self.answer2 = True
        self.on_stage2 = None
        self.progress = FakeProgress()

        self._real_move = appmod.trash.move_to_trash

        def fake_move(path, size, hwnd=0):
            self.moved.append(path)
            os.remove(path)

        appmod.trash.move_to_trash = fake_move

        # ごみ箱の記録は一時フォルダへ（利用者の記録ファイルには書かない）
        self._ledger_dir = tempfile.mkdtemp(prefix="pmc_ledger_")
        appmod.App.LEDGER_PATH = os.path.join(self._ledger_dir, "trash_log.json")
        # 移動は差し替えるので、ごみ箱の中を探し直しても見つからない（待たない）
        self._real_delays = appmod.recyclebin.RETRY_DELAYS
        appmod.recyclebin.RETRY_DELAYS = ()
        a = self.a = App(hidden=True)
        a.withdraw()
        a.raise_window = lambda w: None
        a.current_root = self.dir
        a.notify = lambda title, text, error=False: self.notices.append((title, text, error))
        a.show_trash_results = lambda title, text, rows, error=False: (
            self.notices.append((title, text, error)), self.results.append(rows))
        self.results: list = []
        a.confirm_trash = lambda spec: False
        a.confirm_dup_stage1 = lambda summary: (self.stage1.append(summary), self.answer1)[1]

        def stage2():
            self.stage2_calls += 1
            if self.on_stage2:
                self.on_stage2()
            return self.answer2
        a.confirm_dup_stage2 = stage2
        a.make_progress = lambda title, total: self.progress
        a.PROGRESS_MIN = 1

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

    # --- 道具 ---
    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan_and_find(self):
        self.a.start_scan()
        pump(self.a, 20, until=lambda: bool(self.a.file_rows))
        self.assertTrue(self.a.file_rows)
        self.a.start_dup_scan()
        pump(self.a, 20, until=lambda: not self.a.dup_running)
        self.assertIsNotNone(self.a.dup_result)

    def groups(self):
        return self.a.dup_result.groups

    def file_of(self, path: Path):
        for g in self.groups():
            for f in g.files:
                if Path(f.path) == path:
                    return g, f
        self.fail(f"重複結果に {path} がない")

    def assert_each_group_keeps_one(self):
        for g in self.groups():
            unchecked = [f for f in g.files if f.path not in self.a.dup_checked]
            self.assertGreaterEqual(len(unchecked), 1, "全部にチェックが付いた組がある")
            if any(f.path in self.a.dup_checked for f in g.files):
                keep = self.a.dup_keep.get(g.digest)
                self.assertIn(keep, [f.path for f in unchecked], "チェックのある組に「残す」が無い")


class TestChecks(Base):
    def setUp(self):
        super().setUp()
        self.a1 = self.write("a/one.bin", b"A" * 5000)
        self.a2 = self.write("b/one.bin", b"A" * 5000)
        self.a3 = self.write("c/one.bin", b"A" * 5000)
        self.scan_and_find()

    def test_チェックすると残す側の案が置かれ_全部にはチェックできない(self):
        g, f1 = self.file_of(self.a1)
        _, f2 = self.file_of(self.a2)
        _, f3 = self.file_of(self.a3)
        self.assertIsNone(self.a._dup_check(g, f1, True))
        keep = self.a.dup_keep.get(g.digest)
        self.assertIsNotNone(keep, "チェックしたのに残す側が決まっていない")
        self.assertIn(g.digest, self.a.dup_keep_auto)
        self.assertNotIn(keep, self.a.dup_checked)
        # 残り 2 件にもチェックを付けようとすると、最後の 1 件は断る
        msgs = [self.a._dup_check(g, f, True) for f in (f2, f3)]
        self.assertEqual(sum(1 for m in msgs if m), 1, "組の全部にチェックが付いてしまう")
        self.assert_each_group_keeps_one()

    def test_指定した残す側にはチェックできない(self):
        g, f1 = self.file_of(self.a1)
        self.a._dup_set_keep(g, f1.path)
        msg = self.a._dup_check(g, f1, True)
        self.assertTrue(msg and "残す" in msg)
        self.assertNotIn(f1.path, self.a.dup_checked)

    def test_チェックした行を残す側にするとチェックが外れる(self):
        g, f1 = self.file_of(self.a1)
        _, f2 = self.file_of(self.a2)
        self.a._dup_check(g, f1, True)
        self.a._dup_check(g, f2, True)
        self.a._dup_set_keep(g, f2.path)
        self.assertNotIn(f2.path, self.a.dup_checked)
        self.assertEqual(self.a.dup_keep[g.digest], f2.path)
        self.assert_each_group_keeps_one()

    def test_チェック欄のクリックとShiftクリックで範囲(self):
        a = self.a
        order = [iid for iid, _g, _f in a._dup_order]
        tree = a.dup_tree
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: "#1"
        tree.identify_row = lambda y: order[0]
        self.assertEqual(a._dup_click(FakeEvent()), "break")
        self.assertEqual(len(a.dup_checked), 1)
        tree.identify_row = lambda y: order[2]
        a._dup_click(FakeEvent(state=0x0001))      # Shift
        # 3 件の組なので、範囲で全部を選んでも 1 件は残る
        self.assertEqual(len(a.dup_checked), 2)
        self.assert_each_group_keeps_one()
        self.assertIn("個別", " ".join(a.dup_scope_labels()))

    def test_チェック欄以外のクリックは通常の行選択に任せる(self):
        tree = self.a.dup_tree
        tree.identify_region = lambda x, y: "cell"
        tree.identify_column = lambda x: "#5"
        tree.identify_row = lambda y: self.a._dup_order[0][0]
        self.assertIsNone(self.a._dup_click(FakeEvent(state=0x0004)))   # Ctrl+クリック
        self.assertEqual(self.a.dup_checked, {})

    def test_離れた行を選んでSpaceでチェック(self):
        a = self.a
        rows = [a._dup_order[0][0], a._dup_order[2][0]]      # 間の行は選ばない
        a.dup_tree.selection_set(rows)
        a._dup_space()
        checked = {Path(p) for p in a.dup_checked}
        self.assertEqual(len(checked), 2)
        self.assertNotIn(Path(a._dup_order[1][2].path), checked, "選んでいない間の行にチェックが付いた")
        a._dup_space()
        self.assertEqual(a.dup_checked, {}, "もう一度押しても外れない")

    def test_右クリックで複数選択を崩さない(self):
        a = self.a
        rows = [a._dup_order[0][0], a._dup_order[2][0]]
        a.dup_tree.selection_set(rows)
        a.dup_tree.identify_row = lambda y: rows[1]
        real_menu = appmod.tk.Menu

        class FakeMenu(real_menu):
            def tk_popup(self, *args, **kw):
                pass

        appmod.tk.Menu = FakeMenu
        try:
            a._dup_context_menu(FakeEvent())
        finally:
            appmod.tk.Menu = real_menu
        self.assertEqual(set(a.dup_tree.selection()), set(rows))


class TestRunningDisplay(Base):
    """検出中・完了・中止の表示。途中結果を全体の結果や確定した節約容量として見せない。"""

    def setUp(self):
        super().setUp()
        for n in range(4):
            data = bytes([80 + n]) * (5000 + n)
            self.write(f"r/{n}.bin", data)
            self.write(f"s/{n}.bin", data)

    def test_確定した組を途中結果として順に受け取る(self):
        calls = []
        real = self.a._add_partial_groups
        self.a._add_partial_groups = lambda groups: (calls.append(len(groups)), real(groups))
        old = appmod.DupScanner.PARTIAL_INTERVAL
        appmod.DupScanner.PARTIAL_INTERVAL = 0
        try:
            self.scan_and_find()
        finally:
            appmod.DupScanner.PARTIAL_INTERVAL = old
        self.assertTrue(calls, "途中結果を受け取っていない")
        self.assertEqual(sum(calls), 4)
        self.assertEqual(len(self.groups()), 4)
        self.assertIn("完了", self.a.dup_note.get())

    def test_検出中は途中結果と明示し_チェックも移動もできない(self):
        self.scan_and_find()
        groups = list(self.groups())
        a = self.a
        a.dup_running = True
        a.dup_result = appmod.duplicates.DupResult([], appmod.duplicates.DupSkipped(), 0, 0)
        a._dup_progress = ("edges", 3, 8, 1, 0)
        a._add_partial_groups(groups[:1])
        self.assertIn("検出中（途中結果）", a.dup_note.get())
        self.assertIn("候補 3 / 8", a.dup_note.get())
        self.assertIn("全体の結果ではありません", a.dup_note_extra.get())
        self.assertIn("検出中", a.dup_page_note.get())
        a.dup_select_all("all")
        self.assertEqual(a.dup_checked, {}, "検出中にチェックできてしまう")
        a.trash_selected_dup()
        self.assertEqual(self.stage1, [], "検出中に移動の確認へ進んだ")
        a.dup_running = False

    def test_中止したときは未確認の候補があると出す(self):
        self.scan_and_find()
        r = self.a.dup_result
        stopped = appmod.duplicates.DupResult(list(r.groups[:2]), r.skipped, 4, 0, cancelled=True, unchecked=12)
        self.a._show_dup_result(stopped)
        self.assertIn("中止しました（途中までの結果）", self.a.dup_note.get())
        self.assertIn("まだ確かめていない候補 12 件", self.a.dup_note_extra.get())
        self.assertIn("全体の結果ではありません", self.a.dup_note_extra.get())


class TestSingleFromSelectAll(Base):
    def test_すべて選ぶで1件になっても2段階の確認を通す(self):
        self.write("s/a.bin", b"S" * 3000)
        self.write("t/a.bin", b"S" * 3000)
        self.scan_and_find()
        self.a.dup_select_all("all")
        self.assertEqual(len(self.a.dup_checked), 1)
        self.answer1 = False
        self.a.trash_selected_dup()
        self.assertEqual(len(self.stage1), 1, "「すべて選ぶ」なのに 1 件用の確認で済ませた")
        self.assertEqual(self.moved, [])


class TestSelectAllScopes(Base):
    def setUp(self):
        super().setUp()
        for n in range(5):
            data = bytes([65 + n]) * (3000 + n)
            self.write(f"x/{n}.bin", data)
            self.write(f"y/{n}.bin", data)
        self.a.DUP_PAGE_GROUPS = 2
        self.scan_and_find()

    def test_ページごとに描き_表示中だけと全体を区別する(self):
        a = self.a
        self.assertEqual(len(self.groups()), 5)
        self.assertEqual(len(a.dup_tree.get_children("")), 2, "1 ページ分だけを描いていない")
        a.dup_select_all("page")
        self.assertEqual(len({v[0] for v in a.dup_checked.values()}), 2, "表示中でない組まで選んだ")
        self.assertIn("表示中", a.dup_scope_labels()[0])
        a.dup_select_all("all")
        self.assertEqual(len(a.dup_checked), 5)
        self.assertTrue(any("検出結果の全 5 組" in s for s in a.dup_scope_labels()))
        self.assertIn("範囲", a.dup_check_note.get())
        self.assert_each_group_keeps_one()

    def test_1段目でキャンセルすると2段目も移動もない(self):
        self.a.dup_select_all("all")
        self.answer1 = False
        self.a.trash_selected_dup()
        self.assertEqual(len(self.stage1), 1)
        self.assertEqual(self.stage2_calls, 0, "1 段目を止めたのに 2 段目が出た")
        self.assertEqual(self.moved, [])

    def test_1段目の内容(self):
        self.a.dup_select_all("all")
        self.answer1 = False
        self.a.trash_selected_dup()
        s = self.stage1[0]
        self.assertEqual(s["count"], 5)
        self.assertEqual(s["groups"], 5)
        self.assertEqual(s["size"], sum(3000 + n for n in range(5)))
        self.assertTrue(any("検出結果の全" in x for x in s["scopes"]), "範囲が確認画面に渡っていない")
        self.assertTrue(s["locations"], "主な保存場所がない")
        self.assertEqual(s["auto_keeps"], 5)

    def test_2段目でキャンセルすると移動しない(self):
        self.a.dup_select_all("all")
        self.answer2 = False
        self.a.trash_selected_dup()
        self.assertEqual(self.stage2_calls, 1)
        self.assertEqual(self.moved, [])
        self.assertEqual(len(self.a.dup_checked), 5, "キャンセルしたのにチェックが消えた")

    def test_両方で進めると残す側以外を移し_表示と集計が揃う(self):
        a = self.a
        before_total = a.total_size
        a.dup_select_all("all")
        keeps = {a.dup_keep[g.digest] for g in self.groups()}
        a.trash_selected_dup()
        self.assertEqual(len(self.moved), 5)
        for k in keeps:
            self.assertTrue(os.path.exists(k), "残す側を移動した")
        self.assertEqual(a.dup_result.groups, [], "移動したのに重複結果に残っている")
        self.assertEqual(a.dup_checked, {})
        self.assertEqual(a.total_size, before_total - sum(3000 + n for n in range(5)))
        self.assertEqual(len(a.file_rows), 5)
        self.assertIn("移動した: 5 件", a.status.get())
        self.assertTrue(self.progress.closed)

    def test_2段目で残す側を変えると_新旧どちらも移さない(self):
        a = self.a
        a.dup_select_all("all")
        g = self.groups()[0]
        old_keep = a.dup_keep[g.digest]
        new_keep = next(f.path for f in g.files if f.path != old_keep)
        self.on_stage2 = lambda: a._dup_set_keep(g, new_keep)
        a.trash_selected_dup()
        self.assertTrue(os.path.exists(new_keep), "2 段目で残す側にしたファイルを移動した")
        self.assertTrue(os.path.exists(old_keep), "前の残す側を黙って移動対象に加えた")
        self.assertEqual(len(self.moved), 4)

    def test_一部失敗と未実行を分けて集計する(self):
        a = self.a
        a.dup_select_all("all")
        plan = a._dup_plan()
        # 1 組目: 移す側が変わる → 失敗（移動しなかった）
        changed = Path(plan[0].targets[0].path)
        changed.write_bytes(b"Z" * (plan[0].group.size + 1))
        # 2 組目: 残す側が消える → この組は未実行
        Path(plan[1].keep.path).unlink()
        a.trash_selected_dup()
        self.assertEqual(len(self.moved), 3)
        self.assertTrue(changed.exists())
        self.assertTrue(os.path.exists(plan[1].targets[0].path), "残す側が消えた組を移動した")
        title, text, _err = self.notices[-1]
        self.assertIn("一部", title)
        self.assertIn("移動した: 3 件", text)
        self.assertIn("移動しなかった", text)
        self.assertIn("未実行: 1 件", text)
        # 失敗はチェックを外して理由を残す。未実行はチェックを残す（続きから実行できる）
        self.assertNotIn(str(changed), a.dup_checked)
        self.assertIn("移動しなかった", a.dup_fail_note[str(changed)])
        self.assertIn(plan[1].targets[0].path, a.dup_checked)
        self.assertIn("未実行", a.dup_fail_note[plan[1].targets[0].path])

    def test_途中で中止したら残りは未実行(self):
        self.a.dup_select_all("all")
        self.progress = FakeProgress(cancel_after=2)
        self.a.trash_selected_dup()
        self.assertEqual(len(self.moved), 2)
        title, text, _ = self.notices[-1]
        self.assertIn("未実行: 3 件", text)
        self.assertIn("中止", text)

    def test_保護対象は計画から外し_確認中に変わったものも数える(self):
        a = self.a
        a.dup_select_all("all")
        plan = a._dup_plan()
        first = plan[0].targets[0].path
        second = plan[1].targets[0].path
        real = a.protected_reason
        # 計画の時点で守っている場所 → 計画から外れ、理由が付く
        a.protected_reason = lambda path, is_dir: "守っている場所のため" if path == first else real(path, is_dir)
        plan2 = a._dup_plan()
        self.assertTrue(any(f.path == first for p in plan2 for f, _w in p.blocked), "保護対象が計画に入っている")

        # 2 段目のあいだに守る場所が増えた → 実行前に作り直した計画で止まり、結果にも数える
        def later():
            a.protected_reason = (lambda path, is_dir: "守っている場所のため" if path in (first, second)
                                  else real(path, is_dir))
        self.on_stage2 = later
        a.trash_selected_dup()
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertEqual(len(self.moved), 3)
        _title, text, _ = self.notices[-1]
        self.assertIn("移動しなかった（確認で止めた・失敗）: 2 件", text, "止めたものが集計から漏れている")
        a.protected_reason = real

    def test_実行中にも1件ずつ保護対象を確かめる(self):
        a = self.a
        a.dup_select_all("all")
        real = a.protected_reason

        class Trip(FakeProgress):
            def step(inner, done, moved, failed):
                super().step(done, moved, failed)
                if done == 1:       # 1 件目を移したあとで、すべて守る場所になった
                    a.protected_reason = lambda path, is_dir: "守っている場所のため"
        self.progress = Trip()
        a.trash_selected_dup()
        a.protected_reason = real
        self.assertEqual(len(self.moved), 1, "実行直前の保護判定をしていない")
        _title, text, _ = self.notices[-1]
        self.assertIn("移動しなかった（確認で止めた・失敗）: 4 件", text)
        self.assertIn("守っている場所", text)

    def test_一覧の保存は全件を含む(self):
        a = self.a
        a.dup_select_all("all")
        out = self.dir / "plan.csv"
        a.export_dup_plan_csv(str(out))
        with open(out, encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))[1:]
        self.assertEqual(sum(1 for r in rows if r[1] == "ごみ箱へ移動"), 5)
        self.assertEqual(sum(1 for r in rows if r[1].startswith("残す")), 5)

    def test_チェックのある組だけ表示(self):
        a = self.a
        g = self.groups()[3]
        a._dup_set_group(g, True)
        a.dup_view_mode.current(1)
        a._dup_goto(0)
        self.assertEqual(len(a.dup_tree.get_children("")), 1)


class TestDialogs(Base):
    """確認画面そのもの。作るだけで表示しない。"""

    def setUp(self):
        super().setUp()
        for n in range(3):
            data = bytes([70 + n]) * (4000 + n)
            self.write(f"p/{n}.bin", data)
            self.write(f"q/{n}.bin", data)
        self.scan_and_find()
        self.a.dup_select_all("all")

    def texts(self, widget) -> str:
        out = []
        for w in widget.winfo_children():
            try:
                out.append(str(w.cget("text")))
            except tk.TclError:
                pass
            out.append(self.texts(w))
        return "\n".join(out)

    def test_1段目は既定がキャンセルで_起こりうることを書く(self):
        a = self.a
        d = appmod.DupBulkWarningDialog(a, a._dup_bulk_summary(a._dup_plan()))
        try:
            self.assertIs(d.first_focus, d.cancel_btn, "既定のボタンがキャンセルではない")
            text = self.texts(d)
            self.assertIn("検出結果の全 3 組", text)
            self.assertIn("動かなくなる可能性", text)
            self.assertIn("別のバージョンのプロジェクト", text)
            self.assertIn("代わりになるとは限りません", text)
            self.assertNotIn("安全", text, "「残るから安全」のように読める")
            self.assertTrue(d.next_btn.cget("text").startswith("次へ"), "1 段目から移動できてしまう")
            self.assertNotIn("ごみ箱へ移動", d.next_btn.cget("text"))
            d._on_return()                    # フォーカスが「次へ」に無いときの Enter
            self.assertFalse(d.result)
        finally:
            if d.winfo_exists():
                d.destroy()

    def test_2段目は全件をページでたどれ_確認のチェックまで押せない(self):
        a = self.a
        appmod.DupBulkReviewDialog.PAGE_GROUPS = 2
        d = appmod.DupBulkReviewDialog(a)
        try:
            seen = set()
            for page in range(3):
                d.goto(page)
                seen.update(f.path for _p, f, role in d._rows.values() if role == "target")
            self.assertEqual(len(seen), 3, "全件をたどれない")
            self.assertEqual(str(d.move_btn.cget("state")), "disabled", "確認なしで移動を押せる")
            d.confirmed.set(True)
            d._sync()
            self.assertEqual(str(d.move_btn.cget("state")), "normal")
            self.assertIn("3 件をごみ箱へ移動", d.move_btn.cget("text"))
            # 移動から外すと件数が変わり、確認もやり直しになる
            d.goto(0)
            target_iid = next(i for i, (_p, _f, role) in d._rows.items() if role == "target")
            d.tree.selection_set(target_iid)
            d.exclude_selected()
            self.assertEqual(d.count, 2)
            self.assertFalse(d.confirmed.get(), "対象が変わったのに確認が残っている")
            self.assertEqual(len(a.dup_checked), 2, "本体のチェックに反映されていない")
            # 残す側を変える
            keep_iid = next(i for i, (_p, _f, role) in d._rows.items() if role == "target")
            p, f, _ = d._rows[keep_iid]
            d.tree.selection_set(keep_iid)
            d.make_keep()
            self.assertEqual(a.dup_keep[p.group.digest], f.path)
            self.assertEqual(d.count, 1)
        finally:
            appmod.DupBulkReviewDialog.PAGE_GROUPS = 100
            if d.winfo_exists():
                d.destroy()

    def test_低リスクの画面は問題ないと言わない(self):
        info = {"count": 1, "size": 10, "groups": 1, "kept_ok": 0, "excluded_count": 2,
                "excluded_size": 20, "excluded": [("開発プロジェクトの中", 2)], "current_checked": 3}
        d = appmod.LowRiskDialog(self.a, info)
        try:
            text = self.texts(d)
            self.assertIn("「削除しても問題ない」ものではありません", text)
            self.assertIn("この判定の限界", text)
            self.assertIn("開発プロジェクトの中: 2 件", text)
            self.assertIn("今付いているチェック（3 件）は外し", text)
            self.assertIs(d.first_focus, d.cancel_btn)
        finally:
            d.destroy()


class TestLowRisk(Base):
    """保存場所の判定は差し替える（一時フォルダは AppData の中にあるため）。"""

    def setUp(self):
        super().setUp()
        img = b"\xff\xd8\xff" + b"J" * 6000
        self.dl_a = self.write("dl/photo.jpg", img)
        self.dl_b = self.write("dl/photo (1).jpg", img)
        img2 = b"\xff\xd8\xff" + b"K" * 6000
        self.proj = self.write("proj/assets/logo.jpg", img2)
        self.dl_c = self.write("dl/logo.jpg", img2)
        img3 = b"\xff\xd8\xff" + b"L" * 6000
        self.other1 = self.write("elsewhere/a.jpg", img3)
        self.other2 = self.write("elsewhere2/a.jpg", img3)
        img4 = b"\xff\xd8\xff" + b"M" * 6000
        self.ver = self.write("dl/v2/pic.jpg", img4)
        self.ver_plain = self.write("dl/pic.jpg", img4)
        txt = b"T" * 6000
        self.t1 = self.write("dl/notes.txt", txt)
        self.t2 = self.write("dl/notes2.txt", txt)
        self.scan_and_find()

        dl = str(self.dir / "dl")
        proj = str(self.dir / "proj")
        real_ctx = self.a.ctx_for

        def fake_ctx(folder: str):
            if folder == dl:
                return FolderContext(location="ダウンロード フォルダ")
            if folder.startswith(dl + os.sep):
                return FolderContext(location="ダウンロード フォルダ")
            if folder.startswith(proj):
                return FolderContext(location="ドキュメント", group_type="project", group_key=proj,
                                     group_label="開発プロジェクト: proj")
            return real_ctx(folder)
        self.a.ctx_for = fake_ctx
        # 版の判定は Users\<名前>\<標準フォルダ> より下を見る。一時フォルダを Downloads に見立てる。
        real_below = appmod.dupselect.folders_below_user_location
        base = str(self.dir / "dl")
        appmod.dupselect.folders_below_user_location = (
            lambda folder: [p for p in folder[len(base):].split(os.sep) if p] if folder.startswith(base)
            else real_below(folder))
        self.addCleanup(setattr, appmod.dupselect, "folders_below_user_location", real_below)
        self.info = None
        self.accept_low = True

        def confirm(info):
            self.info = info
            return self.accept_low
        self.a.confirm_low_risk = confirm

    def test_候補と除外の理由(self):
        a = self.a
        a.dup_select_low_risk()
        checked = {Path(p) for p in a.dup_checked}
        # ダウンロードの写真 2 枚: コピー名の方を移し、元の名前を残す
        self.assertIn(self.dl_b, checked)
        self.assertNotIn(self.dl_a, checked)
        # プロジェクト内と同じ中身: ダウンロード側だけを移し、プロジェクト側は残す
        self.assertIn(self.dl_c, checked)
        self.assertNotIn(self.proj, checked)
        # 用途を判定できない場所どうし: 選ばない
        self.assertNotIn(self.other1, checked)
        self.assertNotIn(self.other2, checked)
        # 版違いの可能性があるフォルダの方は選ばない（残る）
        self.assertNotIn(self.ver, checked)
        # テキストは種類で外す
        self.assertNotIn(self.t1, checked)
        self.assertNotIn(self.t2, checked)
        self.assertEqual(self.info["count"], len(checked))
        reasons = dict(self.info["excluded"])
        self.assertIn("開発プロジェクトの中", reasons)
        self.assertTrue(any(k.startswith("版違い") for k in reasons))
        self.assertTrue(any(k.startswith("種類") for k in reasons))
        self.assertTrue(any(k.startswith("アプリ") or k.startswith("用途") for k in reasons))
        self.assertEqual(a.dup_scope_labels(), ["低リスク候補"])
        self.assert_each_group_keeps_one()
        # 判定欄に理由が出る
        g, f = self.file_of(self.proj)
        self.assertTrue(a._dup_assess_label(f.path).startswith("個別確認"))

    def test_キャンセルならチェックは変えない(self):
        a = self.a
        g, f = self.file_of(self.other1)
        a._dup_check(g, f, True)
        before = dict(a.dup_checked)
        self.accept_low = False
        a.dup_select_low_risk()
        self.assertEqual(a.dup_checked, before)
        self.assertTrue(a.dup_assess, "判定の結果を見られない")

    def test_今のチェックは置き換える(self):
        a = self.a
        g, f = self.file_of(self.other1)
        a._dup_check(g, f, True)
        a.dup_select_low_risk()
        self.assertEqual(self.info["current_checked"], 1)
        self.assertNotIn(f.path, a.dup_checked, "低リスクでないものにチェックが残った")

    def test_指定した残す側は尊重する(self):
        a = self.a
        g, f = self.file_of(self.dl_b)           # コピー名の方を、利用者が「残す」に指定
        a._dup_set_keep(g, f.path)
        a.dup_select_low_risk()
        self.assertEqual(a.dup_keep[g.digest], f.path)
        self.assertIn(str(self.dl_a), a.dup_checked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
