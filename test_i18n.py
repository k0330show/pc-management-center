"""日本語 / English の切り替え。

- 画面に出る日本語の文字列が、すべて T() / N_() / L() を通り、英訳があること（抜けがあると英語の画面に日本語が残る）
- 英語で主要な画面・確認画面・メニュー・結果を開いたとき、表示に日本語が残らないこと
  （ファイル名・パスは訳さない。テストのデータは英字の名前だけにして、日本語が出たら訳し漏れと分かるようにする）
- 言語の設定: 初回の選択・「次回から表示しない」・壊れた設定ファイル・途中の切り替え
ウィンドウは一度も表示しない（App(hidden=True)）。ごみ箱への移動は差し替え、利用者のファイルは使わない。
"""

from __future__ import annotations

import ast
import gc
import os
import re
import shutil
import string
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

import i18n
from i18n_en import EN

HERE = Path(__file__).parent
CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]")
DATA_KEYS = {"(setup|install|installer|インストール)"}   # 表示しない照合用の文字列


def docstring_ids(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                out.add(id(n.body[0].value))
    return out


def fields(s):
    return sorted((f, spec) for _, f, spec, _ in string.Formatter().parse(s) if f is not None)


class TestCoverage(unittest.TestCase):
    """コードの上で、訳し漏れが無いことを確かめる。"""

    MODULES = ("app.py", "trash.py", "recyclebin.py", "duplicates.py", "dupselect.py")

    def test_日本語の文字列はすべて翻訳を通る(self):
        leaks = []
        for mod in self.MODULES:
            src = (HERE / mod).read_text(encoding="utf-8")
            tree = ast.parse(src)
            docs = docstring_ids(tree)
            wrapped = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("T", "N_", "DATA") and n.args:
                    wrapped.add(id(n.args[0]))
            for n in ast.walk(tree):
                if id(n) in docs or id(n) in wrapped:
                    continue
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and CJK.search(n.value):
                    leaks.append(f"{mod}:{n.lineno}: {n.value[:40]}")
                if isinstance(n, ast.JoinedStr) and CJK.search(ast.get_source_segment(src, n) or ""):
                    leaks.append(f"{mod}:{n.lineno}: f文字列")
        # 言語を選ぶ画面は、まだ言語が決まっていないので両方の言語で書く（例外）
        leaks = [x for x in leaks if "表示する言語を選んでください" not in x and "次回から表示しない" not in x
                 and "Language / 言語" not in x
                 and "あとから「設定 → 言語」" not in x and "日本語" not in x.split(": ", 1)[1][:3]]
        self.assertEqual(leaks, [], "翻訳を通らない日本語の文字列がある")

    def test_すべての文言に英訳があり_値の入る所が一致する(self):
        missing, mismatch = [], []
        for mod in self.MODULES:
            tree = ast.parse((HERE / mod).read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("T", "N_") and n.args:
                    a = n.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and CJK.search(a.value):
                        if a.value not in EN:
                            missing.append(f"{mod}:{n.lineno}: {a.value[:40]}")
                        elif fields(a.value) != fields(EN[a.value]):
                            mismatch.append(a.value[:40])
        self.assertEqual(missing, [], "英訳が無い")
        self.assertEqual(mismatch, [], "値の入る所（{0} など）が日本語と英語で違う")

    def test_分類処理の文の型もすべて英訳がある(self):
        from i18n_classifier import TEMPLATES
        src = (HERE / "classifier.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        docs = docstring_ids(tree)
        known = {t for t, _f in TEMPLATES}
        inner = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                for m in ast.walk(n):
                    if m is not n:
                        inner.add(id(m))
        not_registered = []
        for n in ast.walk(tree):
            if id(n) in docs or id(n) in inner:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and CJK.search(n.value):
                if n.value not in known:
                    not_registered.append(n.value[:40])
        self.assertEqual(not_registered, [], "classifier.py の文が型の一覧（i18n_classifier.py）に無い。gen_i18n_keys を実行し直す")
        self.assertEqual([t for t in known if t not in EN], [], "分類処理の型に英訳が無い")
        for t in known:
            if "{}" in t:
                self.assertEqual(t.count("{}"), EN[t].count("{}"), t)

    def test_英訳に日本語が残っていない(self):
        left = [k for k, v in EN.items() if CJK.search(v) and k not in DATA_KEYS]
        self.assertEqual(left, [])


class TestClassifierInEnglish(unittest.TestCase):
    """いろいろな場所・種類で分類し、英語の表示に日本語が残らないことを確かめる。"""

    def setUp(self):
        i18n.set_language("en")

    def tearDown(self):
        i18n.set_language("ja")

    def test_分類の結果はすべて英語になる(self):
        from classifier import KINDS, _EXT, _KNOWN_NAMES, classify_file, folder_cautions, folder_context, folder_kind
        folders = [
            r"C:\Users\u\Downloads", r"C:\Users\u\Documents\proj", r"C:\Users\u\Documents\proj\node_modules\x",
            r"C:\Users\u\Documents\proj\.git\objects", r"C:\Users\u\AppData\Roaming\Vendor\App\Cache",
            r"C:\Users\u\AppData\Local\Packages\Some.App_8wekyb\LocalState", r"D:\SteamLibrary\steamapps\common\Game\Data",
            r"D:\SteamLibrary\steamapps\shadercache\123", r"D:\SteamLibrary\steamapps\workshop\content\42\9",
            r"C:\Program Files\Epic Games\Fort\bin", r"C:\Users\u\Documents\My Games\Game\saves",
            r"C:\Users\u\Saved Games\Game", r"C:\Program Files\Vendor\Tool", r"C:\ProgramData\Vendor\Tool",
            r"C:\Windows\System32", r"C:\Users\u\.cargo\registry", r"C:\Users\u\OneDrive\Pictures\logs",
            r"C:\Users\u\Documents\backup", r"C:\Users\u\Pictures\tmp", r"C:\Users\u\Music\crashdumps",
            r"C:\Users\u\Documents\unity\Library", r"C:\Users\u\Documents\py\venv",
        ]
        siblings_by = {
            r"C:\Users\u\Documents\proj": (["node_modules", ".git", "src"], ["package.json", "tsconfig.json"]),
            r"D:\SteamLibrary\steamapps\common\Game\Data": ([], ["UnityPlayer.dll", "steam_api64.dll"]),
            r"C:\Users\u\Documents\unity": (["Assets", "ProjectSettings", "Library"], []),
            r"C:\Users\u\Documents\py": (["venv"], ["pyproject.toml"]),
        }
        names = ["a" + e for e in list(_EXT)[:400]] + list(_KNOWN_NAMES) + [
            "data.dat", "noext", "save1.dat", "setup.exe", "tool.exe", "clip.ts", "disk.bin", "model.obj", "x.db"]
        leaks = set()

        def check(text, where):
            t = i18n.L(text) if text else ""
            if CJK.search(t):
                leaks.add(f"{where}: {text} -> {t}")

        for folder in folders:
            # 親から順に文脈を作る（アプリと同じ）
            parts = folder.split("\\")
            ctx = None
            for i in range(1, len(parts) + 1):
                p = "\\".join(parts[:i]) + ("\\" if i == 1 else "")
                dirs, files = siblings_by.get(p, ([], []))
                ctx = folder_context(p, dirs, files, ctx)
            sib = {"a.cue", "a.mtl", "disk.cue", "model.mtl", "package.json"}
            for name in names:
                c = classify_file(name, 8 * 1024 * 1024 if name.endswith(".ts") else 1000, ctx, sib)
                for field_ in ("kind_display", "possibility", "purpose", "format", "location", "group_label"):
                    check(getattr(c, field_), f"{folder}\\{name}.{field_}")
                for r in c.reasons + c.cautions:
                    check(r, f"{folder}\\{name}.reason")
            for dominant in [None] + list(KINDS):
                k, label, _c, reasons = folder_kind(ctx, folder, dominant)
                check(label, f"{folder}.folder_kind")
                for r in reasons:
                    check(r, f"{folder}.folder_reason")
                for r in folder_cautions(ctx, dominant):
                    check(r, f"{folder}.folder_caution")
            for text in (ctx.group_label, ctx.container_reason, ctx.location) + tuple(ctx.group_reasons):
                check(text, f"{folder}.ctx")
        self.assertEqual(sorted(leaks)[:20], [], f"英語の表示に日本語が残る（{len(leaks)} 件）")


def collect_texts(root) -> list[str]:
    """画面の部品に出ている文字を集める（ラベル・ボタン・一覧の見出しと行・メニュー・ウィンドウの題名）。"""
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, (tk.Tk, tk.Toplevel)):
            out.append(w.title())
        for opt in ("text", "label"):
            try:
                out.append(str(w.cget(opt)))
            except (tk.TclError, AttributeError):
                pass
        try:
            var = str(w.cget("textvariable"))
            if var:
                out.append(str(w.getvar(var)))
        except (tk.TclError, AttributeError):
            pass
        try:
            out.extend(str(v) for v in w.cget("values") or ())
        except (tk.TclError, AttributeError):
            pass
        if isinstance(w, tk.ttk.Treeview):
            for col in ("#0",) + tuple(w.cget("columns")):
                out.append(str(w.heading(col, "text")))
            items = list(w.get_children(""))
            while items:
                i = items.pop()
                items.extend(w.get_children(i))
                out.append(str(w.item(i, "text")))
                out.extend(str(v) for v in w.item(i, "values"))
        if isinstance(w, tk.Menu):
            end = w.index("end")
            for i in range(0 if end is None else end + 1):
                try:
                    out.append(str(w.entrycget(i, "label")))
                except tk.TclError:
                    pass
    return out


class AppBase(unittest.TestCase):
    LANG = "en"

    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"画面が使えない環境です: {exc}")

    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self.dir = Path(tempfile.mkdtemp(prefix="pmc_i18n_test_"))
        self.aux = Path(tempfile.mkdtemp(prefix="pmc_i18n_aux_"))
        appmod.App.LEDGER_PATH = str(self.aux / "trash_log.json")
        appmod.App.SETTINGS_PATH = str(self.aux / "settings.json")
        self._real_delays = appmod.recyclebin.RETRY_DELAYS
        appmod.recyclebin.RETRY_DELAYS = ()
        self._real_move = appmod.trash.move_to_trash
        self.moved = []

        def fake_move(path, size, hwnd=0):
            self.moved.append(path)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        appmod.trash.move_to_trash = fake_move
        self.a = appmod.App(hidden=True, language=self.LANG)
        self.a.raise_window = lambda w: None
        self.a.current_root = self.dir
        self.notices, self.results, self.specs, self.multi = [], [], [], []
        self.a.notify = lambda title, text, error=False: self.notices.append((title, text))
        self.a.show_trash_results = lambda title, text, rows, error=False: self.results.append((title, text, rows))
        self.a.confirm_trash = lambda spec: (self.specs.append(spec), False)[1]
        self.a.confirm_trash_many = lambda mv, bl, **kw: (self.multi.append((mv, bl, kw)), False)[1]
        self.a.treemap.canvas.winfo_width = lambda: 800
        self.a.treemap.canvas.winfo_height = lambda: 500

    def tearDown(self):
        self.appmod.trash.move_to_trash = self._real_move
        self.appmod.recyclebin.RETRY_DELAYS = self._real_delays
        try:
            self.a.destroy()
        except tk.TclError:
            pass
        gc.collect()
        self.appmod.App.LEDGER_PATH = None
        self.appmod.App.SETTINGS_PATH = None
        i18n.set_language("ja")
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.aux, ignore_errors=True)

    def write(self, rel, data: bytes):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def pump(self, until, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            self.a.update()
            time.sleep(0.02)

    def make_data(self):
        # 名前は英字だけ（日本語が出たら訳し漏れと分かるように）
        self.write("Downloads/photo.jpg", b"J" * 5000)
        self.write("Downloads/photo (1).jpg", b"J" * 5000)
        self.write("Downloads/setup.exe", b"E" * 3000)
        self.write("proj/package.json", b"{}")
        self.write("proj/node_modules/lib/index.js", b"L" * 700)
        self.write("proj/.git/objects/ab", b"G" * 600)
        self.write("proj/src/app.ts", b"T" * 900)
        self.write("proj/src/copy.ts", b"T" * 900)
        self.write("steamapps/common/Game/data.pak", b"P" * 4000)
        self.write("steamapps/common/Game/UnityPlayer.dll", b"U" * 100)
        self.write("stuff/Cache/blob.bin", b"C" * 800)
        self.write("stuff/logs/run.log", b"R" * 300)
        self.write("stuff/backup/old.bak", b"B" * 400)
        self.write("stuff/unknown.dat", b"D" * 200)

    def scan(self):
        self.a.start_scan()
        self.pump(lambda: bool(self.a.file_rows))
        self.assertTrue(self.a.file_rows)

    def assert_no_japanese(self, texts, where):
        bad = sorted({t for t in texts if CJK.search(t) and "日本語" not in t})
        self.assertEqual(bad[:15], [], f"{where}: 英語の画面に日本語が残る（{len(bad)} 件）")


class TestEnglishScreens(AppBase):
    def menus(self, fn, tree, iid):
        entries = []
        real = self.appmod.tk.Menu

        class Rec(real):
            def add_command(self, **kw):
                entries.append(str(kw.get("label", "")))
                super().add_command(**kw)

            def tk_popup(self, *a, **kw):
                pass

        class Ev:
            x = y = x_root = y_root = state = 0
        tree.identify_row = lambda _y: iid
        self.appmod.tk.Menu = Rec
        try:
            fn(Ev())
        finally:
            self.appmod.tk.Menu = real
        return entries

    def test_主要な画面と確認操作が英語になる(self):
        a, am = self.a, self.appmod
        self.make_data()
        texts = collect_texts(a)                              # スキャン前
        self.scan()
        texts += collect_texts(a)                             # 容量マップ・一覧
        # 容量マップをたどる・右クリック
        a.treemap.navigate(self.dir / "proj")
        a.treemap.redraw()
        texts += collect_texts(a.treemap)
        texts += self.menus(lambda e: a.treemap._menu_for(self.dir / "proj" / "src", e), a.treemap.list, "")
        # フォルダ一覧を開く
        for d in ("proj", "steamapps", "stuff"):
            a._fill_folder_children(str(self.dir / d))
        texts += collect_texts(a.folder_tree)
        # 詳細画面（ファイル・フォルダ）
        for f in a.file_rows:
            a.show_file_detail(f)
            texts += collect_texts(a.detail)
        for d in (self.dir / "proj", self.dir / "steamapps" / "common" / "Game", self.dir / "stuff" / "Cache"):
            a.show_folder_detail(d)
            texts += collect_texts(a.detail)
        # チェックと右クリック・まとめての移動の確認
        item = next(f for f in a.file_rows if f.name == "setup.exe")
        a._set_checked("file", item, True)
        a._set_checked("dir", self.dir / "stuff", True)
        a._update_check_views()
        texts += collect_texts(a)
        iid = next(i for i, f in a._file_iids.items() if f is item)
        texts += self.menus(a._file_context_menu, a.file_tree, iid)
        other = next(i for i, f in a._file_iids.items() if f.name == "photo.jpg")
        texts += self.menus(a._file_context_menu, a.file_tree, other)
        a.trash_selected_file()
        mv, bl, kw = self.multi[-1]
        d = am.MultiTrashConfirmDialog(a, mv, bl, note=kw.get("note"), warn=kw.get("warn"), on_exclude=lambda o: None)
        texts += collect_texts(d)
        d.destroy()
        # 1 件の確認画面
        a._uncheck_objs([item])
        a.clear_checks()
        a.trash_file(item)
        dlg = am.TrashConfirmDialog(a, self.specs[-1])
        dlg.withdraw()
        texts += collect_texts(dlg)
        dlg.destroy()
        # 移動して、結果（一部失敗）を出す
        a.confirm_trash_many = lambda mv, bl, **kw: True
        f1 = next(f for f in a.file_rows if f.name == "unknown.dat")
        f2 = next(f for f in a.file_rows if f.name == "run.log")
        (self.dir / "stuff" / "logs" / "run.log").write_bytes(b"changed!")
        a.trash_files([f1, f2])
        title, text, rows = self.results[-1]
        texts += [title, text] + [c for r in rows for c in r]
        res = am.TrashResultDialog(a, title, text, rows)
        texts += collect_texts(res)
        res.destroy()
        texts += [a.status.get()]
        self.assert_no_japanese(texts, "スキャン・一覧・詳細・移動")

    def test_重複ファイルと2段階の確認が英語になる(self):
        a, am = self.a, self.appmod
        self.make_data()
        self.scan()
        a.start_dup_scan()
        texts = collect_texts(a.dups_tab)                     # 検出中
        self.pump(lambda: not a.dup_running)
        texts += collect_texts(a.dups_tab)
        a.confirm_low_risk = lambda info: (texts.extend(collect_texts(am.LowRiskDialog(a, info))), True)[1]
        a.dup_select_low_risk()
        texts += collect_texts(a.dups_tab)
        a._fill_dup_all_menu()
        texts += collect_texts(a._dup_all_menu)
        a.dup_select_all("all")
        texts += collect_texts(a.dups_tab)
        summary = a._dup_bulk_summary(a._dup_plan())
        w1 = am.DupBulkWarningDialog(a, summary)
        texts += collect_texts(w1)
        w1.destroy()
        w2 = am.DupBulkReviewDialog(a)
        texts += collect_texts(w2)
        w2.destroy()
        # 進み具合の画面は作ると画面を押さえる（grab）ので作らない。文言はコードの検査で確かめている
        # 中止したときの表示
        r = a.dup_result
        a._show_dup_result(am.duplicates.DupResult(list(r.groups), r.skipped, 1, 0, cancelled=True, unchecked=3))
        texts += [a.dup_note.get(), a.dup_note_extra.get()]
        self.assert_no_japanese(texts, "重複ファイル")

    def test_ごみ箱から復元の画面が英語になる(self):
        a, am = self.a, self.appmod
        e1 = am.recyclebin.Entry("gone.txt", str(self.dir / "gone.txt"), False, 10, time.time(), state="in_bin",
                                 bin_dir=str(self.aux), r_name="$RAAAAAA.txt", i_name="$IAAAAAA.txt", deleted_ft=1)
        e2 = am.recyclebin.Entry("lost.txt", str(self.dir / "x" / "lost.txt"), False, 20, time.time(),
                                 note=am.recyclebin.N_('ごみ箱の中に対応する項目が見つかりませんでした'))
        e3 = am.recyclebin.Entry("back.txt", str(self.dir / "back.txt"), False, 5, time.time(), state="restored",
                                 note=am.recyclebin.N_('このアプリで元の場所へ戻しました'))
        a.ledger.entries = [e1, e2, e3]
        a._apply_bin_check(a._compute_bin_check(a.ledger.entries))
        texts = collect_texts(a.bin_tab)
        for iid in a.bin_tree.get_children():
            a.bin_tree.selection_set(iid)
            a._bin_show_selection()
            texts.append(a.bin_detail.get())
        a.bin_tree.selection_set(a.bin_tree.get_children())
        rows = a._restore_plan([e1, e2, e3])
        dlg = am.RestoreConfirmDialog(a, rows)
        texts += collect_texts(dlg)
        dlg.destroy()
        a.confirm_restore = lambda rows: True
        a.restore_selected()
        title, text, rows = self.results[-1]
        texts += [title, text] + [c for r in rows for c in r]
        yn = am.YesNoDialog(a, am.T('記録から外す'), am.T('選んだ {0:,} 件の記録を、この一覧から外します。ごみ箱の中身は何も変えません。', 2))
        texts += collect_texts(yn)
        yn.destroy()
        self.assert_no_japanese(texts, "ごみ箱から復元")

    def test_メニューは英語でも同じ場所にある(self):
        labels = collect_texts(self.a._menubar)
        self.assertIn("Settings", labels)
        sub = self.a._menubar.nametowidget(self.a._menubar.entrycget(0, "menu"))
        self.assertIn("Language", collect_texts(sub))


class TestSwitchAndSettings(AppBase):
    LANG = "ja"

    def test_途中で切り替えてもデータと場所は残り_設定に保存される(self):
        a = self.a
        self.make_data()
        self.scan()
        n_files = len(a.file_rows)
        item = a.file_rows[0]
        a._set_checked("file", item, True)
        a.treemap.navigate(self.dir / "proj")
        a.tabs.select(a.files_tab)
        self.assertTrue(a.set_language("en"))
        self.assertEqual(i18n.LANG, "en")
        self.assertEqual(len(a.file_rows), n_files, "切り替えでスキャン結果が消えた")
        self.assertIn(id(item), a.check_files, "切り替えでチェックが消えた")
        self.assertEqual(a.treemap.current_root, self.dir / "proj", "切り替えで容量マップの場所が変わった")
        self.assertEqual(a.tabs.select(), str(a.files_tab))
        self.assert_no_japanese(collect_texts(a), "切り替え後")
        st = i18n.load_settings(a.settings_path())
        self.assertEqual((st["language"], st["ask"]), ("en", False), "次回の起動に反映されない")
        a.set_language("ja")
        self.assertTrue(any(CJK.search(t) for t in collect_texts(a)), "日本語に戻らない")

    def test_スキャン中は切り替えない(self):
        self.a.dup_running = True
        self.assertFalse(self.a.set_language("en"))
        self.assertEqual(i18n.LANG, "ja")
        self.a.dup_running = False

    def test_初回の選択と次回から表示しない(self):
        a, am = self.a, self.appmod
        calls = []
        real = am.LanguageDialog.ask
        a.deiconify = lambda: None                         # 画面を出さない
        a.withdraw = lambda: None
        path = a.settings_path()
        try:
            am.LanguageDialog.ask = lambda self_: (calls.append(1), ("en", False))[1]
            a._choose_language(hidden=False, language=None)  # 未設定 → 選ぶ画面を出す
            self.assertEqual((len(calls), i18n.LANG), (1, "en"))
            self.assertFalse(os.path.exists(path), "「次回から表示しない」なしで保存した")
            am.LanguageDialog.ask = lambda self_: (calls.append(1), ("en", True))[1]
            a._choose_language(hidden=False, language=None)
            self.assertEqual(i18n.load_settings(path)["language"], "en")
            i18n.set_language("ja")
            a._choose_language(hidden=False, language=None)  # 保存済み・表示しない → 出さない
            self.assertEqual((len(calls), i18n.LANG), (2, "en"))
            a._choose_language(hidden=True, language=None)   # 画面を出さない起動では、そもそも出さない
            self.assertEqual(len(calls), 2)
        finally:
            am.LanguageDialog.ask = real

    def test_壊れた設定ファイルでも起動できる(self):
        path = Path(self.a.settings_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        for broken in (b"{not json", b"[1, 2]", b'{"language": "xx"}', b"\xff\xfe"):
            path.write_bytes(broken)
            st = i18n.load_settings(str(path))
            self.assertEqual((st["language"], st["ask"]), (None, True))
            a2 = self.appmod.App(hidden=True)                # 例外にならずに起動する
            a2.destroy()
            gc.collect()
        i18n.save_settings(str(path), "en", True)          # 壊れていても上書きで直る
        self.assertEqual(i18n.load_settings(str(path))["language"], "en")

    def test_言語の設定はごみ箱の記録と別のファイル(self):
        self.assertNotEqual(os.path.abspath(self.a.settings_path()), os.path.abspath(self.a.ledger.path))
        self.assertTrue(i18n.default_settings_path().endswith("settings.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
