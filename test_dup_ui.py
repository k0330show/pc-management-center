"""重複タブの動きを、実際の App を動かして確かめる。

**一時フォルダだけ**を対象にし、利用者の実ファイルには触れない。
ウィンドウは `withdraw()` で隠したまま動かすので、画面には出ない。

見るのは:
  検出してから整理するまでの一連が、途中で嘘をつかないこと
  残す側を決めずに移せないこと
  検出後に変わったファイルを移さないこと
  移したあとに、一覧・容量・重複結果が食い違わないこと
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
    """Tk の処理を回しながら待つ（画面は出さない）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        a.update()
        if until is not None and until():
            return
        time.sleep(0.02)


class DupUIBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()      # 画面に出さない
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"画面が使えない環境です: {exc}")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_ui_test_"))
        self.moved: list[tuple[str, int]] = []
        self.confirmed = True
        self.refusals: list[tuple[str, str]] = []

        # ごみ箱へは実際に動かさない。呼ばれたことだけ記録し、ファイルは自分で消す。
        self._real_move = appmod.trash.move_to_trash

        def fake_move(path, size, hwnd=0):
            self.moved.append((path, size))
            os.remove(path)

        appmod.trash.move_to_trash = fake_move

        # ごみ箱の記録は一時フォルダへ（利用者の記録ファイルには書かない）
        self._ledger_dir = tempfile.mkdtemp(prefix="pmc_ledger_")
        appmod.App.LEDGER_PATH = os.path.join(self._ledger_dir, "trash_log.json")
        # 移動は差し替えるので、ごみ箱の中を探し直しても見つからない（待たない）
        self._real_delays = appmod.recyclebin.RETRY_DELAYS
        appmod.recyclebin.RETRY_DELAYS = ()
        self.a = App(hidden=True)
        self.a.withdraw()                 # 画面に出さない
        self.a.raise_window = lambda w: None   # 詳細画面なども前面に出さない
        self.a.current_root = self.dir
        # 確認ダイアログとお知らせは出さない（テスト中に前面へ出させない）
        self.a.confirm_trash = lambda spec: self._on_confirm(spec)
        self.a.notify = lambda *args, **kw: None
        self.a.show_trash_results = lambda *args, **kw: None
        self.specs: list[dict] = []
        real_refuse = self.a._refuse
        self.a._refuse = lambda name, reason, detail="": (
            self.refusals.append((name, reason)), real_refuse.__self__.status.set(""))

    def _on_confirm(self, spec: dict) -> bool:
        self.specs.append(spec)
        return self.confirmed

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

    def scan(self) -> None:
        self.a.start_scan()
        pump(self.a, 20, until=lambda: bool(self.a.file_rows))
        self.assertTrue(self.a.file_rows, "スキャンできていない")

    def find_dups(self) -> None:
        self.a.start_dup_scan()
        pump(self.a, 20, until=lambda: not self.a.dup_running)
        self.assertIsNotNone(self.a.dup_result, "重複検出が終わっていない")

    def row_for(self, path: Path) -> str:
        for iid, entry in self.a._dup_rows.items():
            if entry[0] == "file" and Path(entry[2].path) == path:
                return iid
        self.fail(f"重複結果に {path} の行がない")


class TestFindAndShow(DupUIBase):
    def test_同じ中身の組が並び_見かけと空く容量を分けて出す(self):
        self.write("a.bin", b"D" * 20_000)
        self.write("sub/b.bin", b"D" * 20_000)
        self.write("c.bin", b"E" * 20_000)
        self.scan()
        self.find_dups()
        r = self.a.dup_result
        self.assertEqual(len(r.groups), 1)
        self.assertEqual(r.apparent_size, 40_000)
        self.assertEqual(r.reclaimable, 20_000)
        note = self.a.dup_note.get()
        self.assertIn("見かけの合計", note)
        self.assertIn("空く容量", note)
        extra = self.a.dup_note_extra.get()
        self.assertIn("これだけ空くわけではありません", extra,
                      "見かけの合計と空く容量の違いを断っていない")

    def test_通常スキャンは重複を探さない(self):
        self.write("x.bin", b"F" * 9000)
        self.write("y.bin", b"F" * 9000)
        self.scan()
        self.assertIsNone(self.a.dup_result, "スキャンしただけで重複検出が走っている")

    def test_読めなかった件数を画面に出す(self):
        a = self.write("v1.bin", b"G" * 6000)
        b = self.write("v2.bin", b"G" * 6000)
        self.write("v3.bin", b"G" * 6000)
        self.scan()
        (self.dir / "v3.bin").unlink()        # スキャン後・検出前に消える
        self.find_dups()
        self.assertEqual(self.a.dup_result.skipped.vanished, 1)
        self.assertIn("消えた", self.a.dup_note_extra.get())
        self.assertIn("「重複なし」とは判断していません", self.a.dup_note_extra.get())
        del a, b


class TestMinSize(DupUIBase):
    def test_既定ではすべて調べる(self):
        self.write("s1.bin", b"S" * 500)
        self.write("s2.bin", b"S" * 500)
        self.scan()
        self.assertEqual(self.a.dup_min_size.get(), "すべて", "既定で小さいファイルを省いている")
        self.find_dups()
        self.assertEqual(len(self.a.dup_result.groups), 1,
                         "既定なのに小さいファイルの重複を見つけていない")

    def test_狭めたときは省いたことを画面に出す(self):
        self.write("s1.bin", b"S" * 500)
        self.write("s2.bin", b"S" * 500)
        self.scan()
        self.a.dup_min_size.set("1 MB 以上")
        self.find_dups()
        self.assertEqual(self.a.dup_result.groups, [], "省いたはずの小さいファイルを調べている")
        self.assertIn("調べていません", self.a.dup_note_extra.get(),
                      "省いたことを画面に出していない（重複なしと取り違える）")


class TestKeepAndTrash(DupUIBase):
    def setUp(self):
        super().setUp()
        self.p1 = self.write("keep/one.bin", b"K" * 30_000)
        self.p2 = self.write("other/two.bin", b"K" * 30_000)
        self.scan()
        self.find_dups()

    def test_残す側を決めていなければ案を示し_確認画面に出す(self):
        # 残す側を決めずに 1 行を移そうとすると、残す側の「案」を置いて確認画面に出す。
        # 予告なく決めない: 画面の「残す」欄にも確認画面にも、アプリの案だと明記する。
        self.a.dup_tree.selection_set(self.row_for(self.p2))
        self.confirmed = False
        self.a.trash_selected_dup()
        self.assertEqual(self.moved, [], "キャンセルしたのに移動した")
        self.assertTrue(self.p2.exists())
        g = self.a.dup_result.groups[0]
        self.assertEqual(self.a.dup_keep.get(g.digest), str(self.p1), "残す側の案が置かれていない")
        self.assertIn(g.digest, self.a.dup_keep_auto, "案と指定の区別がない")
        facts = dict(self.specs[0]["facts"])
        self.assertIn(self.p1.name, facts["残すファイル"])
        self.assertIn("アプリが選んだ案", facts["残すファイル"], "案であることを確認画面で伝えていない")
        self.assertTrue(any("問題がないという意味ではありません" in c for c in self.specs[0]["cautions"]),
                        "1 件残ることを安全の根拠のように見せている")

    def test_残すと決めた側は移せない(self):
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.trash_selected_dup()
        self.assertEqual(self.moved, [], "残すと決めたファイルを移動した")
        self.assertTrue(self.p1.exists())

    def test_確認画面に残す側と移す側の両方を出す(self):
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.a.dup_tree.selection_set(self.row_for(self.p2))
        self.confirmed = False                 # 押さずにやめる
        self.a.trash_selected_dup()
        self.assertEqual(len(self.specs), 1, "確認画面が出ていない")
        spec = self.specs[0]
        self.assertEqual(spec["name"], self.p2.name)
        facts = dict((k, v) for k, v in spec["facts"])
        self.assertIn("残すファイル", facts, "残す側が確認画面に出ていない")
        self.assertIn(self.p1.name, facts["残すファイル"])
        self.assertTrue(any("場所によって用途が違う" in c for c in spec["cautions"]),
                        "場所で用途が違いうることを伝えていない")
        self.assertEqual(self.moved, [], "キャンセルしたのに移動した")
        self.assertTrue(self.p2.exists())

    def test_移したあとは一覧も容量も重複結果も揃う(self):
        before_total = self.a.total_size
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.a.dup_tree.selection_set(self.row_for(self.p2))
        self.a.trash_selected_dup()
        self.assertEqual(len(self.moved), 1, "移動していない")
        self.assertFalse(self.p2.exists())
        # 重複結果から消える（組が 1 本だけになったので組ごと消える）
        self.assertEqual(self.a.dup_result.groups, [], "移動したのに重複結果に残っている")
        # ファイル一覧からも消える
        self.assertNotIn(str(self.p2), [str(f.path) for f in self.a.file_rows])
        # 合計容量が減る
        self.assertEqual(self.a.total_size, before_total - 30_000)
        self.assertIn("ファイル", self.a.summary_vars["files"].get())

    def test_検出後に中身が変わったら移さない(self):
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.p2.write_bytes(b"Z" * 30_000)     # 容量は同じまま中身だけ変わる
        os.utime(self.p2, (time.time(), time.time() + 5))
        self.a.dup_tree.selection_set(self.row_for(self.p2))
        self.a.trash_selected_dup()
        self.assertEqual(self.moved, [], "検出後に変わったファイルを移動した")
        self.assertTrue(self.p2.exists())
        self.assertTrue(self.refusals, "変わったことを伝えていない")

    def test_残す側が検出後に消えたら移さない(self):
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.p1.unlink()                        # 残すはずの側が消える
        self.a.dup_tree.selection_set(self.row_for(self.p2))
        self.a.trash_selected_dup()
        self.assertEqual(self.moved, [], "残す側が消えているのに移動した")
        self.assertTrue(self.p2.exists(), "残す側が無いまま、もう一方まで消してしまった")

    def test_もう一度押すと残す印を外せる(self):
        row = self.row_for(self.p1)
        self.a.dup_tree.selection_set(row)
        self.a.keep_selected_dup()
        self.assertTrue(self.a.dup_keep)
        self.a.dup_tree.selection_set(self.row_for(self.p1))
        self.a.keep_selected_dup()
        self.assertFalse(self.a.dup_keep, "もう一度押しても印が外れない")


class TestDetailBridge(DupUIBase):
    def test_重複結果から詳細画面へ進める(self):
        p1 = self.write("d1.bin", b"M" * 12_000)
        self.write("d2.bin", b"M" * 12_000)
        self.scan()
        self.find_dups()
        self.a.dup_tree.selection_set(self.row_for(p1))
        self.a.detail_dup_selected()
        self.assertIsNotNone(self.a.detail, "詳細画面が開いていない")
        kind, target = self.a.detail.current
        self.assertEqual(kind, "file")
        self.assertEqual(Path(target.path), p1)
        self.a.detail.withdraw()

    def test_組の見出しを選んでも詳細は出さない(self):
        self.write("e1.bin", b"N" * 12_000)
        self.write("e2.bin", b"N" * 12_000)
        self.scan()
        self.find_dups()
        group_iid = next(i for i, e in self.a._dup_rows.items() if e[0] == "group")
        self.a.dup_tree.selection_set(group_iid)
        self.a.detail_dup_selected()
        self.assertIn("組の中のファイル", self.a.dup_note_extra.get())


class TestRescan(DupUIBase):
    def test_もう一度スキャンすると古い重複結果は消える(self):
        self.write("r1.bin", b"R" * 15_000)
        self.write("r2.bin", b"R" * 15_000)
        self.scan()
        self.find_dups()
        self.assertTrue(self.a.dup_result.groups)
        self.a.start_scan()
        self.assertIsNone(self.a.dup_result, "古い重複結果が残っている")
        self.assertEqual(self.a.dup_tree.get_children(""), ())
        pump(self.a, 20, until=lambda: bool(self.a.file_rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
