"""低リスク判定・残す側の案・保存場所のまとめ（画面なし・ディスクを読まない）。

見るのは: 誤判定しうるものを候補に入れないこと。候補を広げる方向に倒れないこと。
"""

from __future__ import annotations

import unittest

import dupselect
from classifier import FolderContext

HOME = r"C:\Users\someone"


def ctx(**kw) -> FolderContext:
    return FolderContext(**kw)


DL = ctx(location="ダウンロード フォルダ")


class TestAssess(unittest.TestCase):
    def check(self, name, folder, kind, c, certainty="sure", protected=None):
        return dupselect.assess(name, folder, kind, certainty, c, protected)

    def test_標準フォルダの写真は候補(self):
        self.assertEqual(self.check("a.jpg", HOME + r"\Downloads", "image", DL), (True, "ok"))

    def test_用途を判定できないものは外す(self):
        cases = [
            ("保護", ("a.jpg", HOME + r"\Downloads", "image", DL, "sure", "守っている場所のため"), "protected"),
            ("システム", ("a.jpg", r"C:\Windows\x", "image", ctx(group_type="system")), "system"),
            ("依存", ("a.png", HOME + r"\Downloads\p\node_modules\x", "dev_deps",
                      ctx(location="ダウンロード フォルダ", container="deps")), "deps"),
            ("変更履歴", ("a.png", HOME + r"\Documents\p\.git", "dev_vcs",
                          ctx(location="ドキュメント", container="vcs")), "vcs"),
            ("プロジェクト", ("logo.png", HOME + r"\Documents\app", "image",
                              ctx(location="ドキュメント", group_type="project")), "project"),
            ("アプリ", ("a.png", HOME + r"\AppData\Roaming\X", "image",
                        ctx(location="AppData（アプリのデータ領域）", group_type="app")), "app"),
            ("AppData", ("a.png", HOME + r"\AppData\Local\Temp", "image",
                         ctx(location="AppData（アプリのデータ領域）")), "app"),
            ("ゲーム", ("a.png", r"D:\SteamLibrary\steamapps\common\G", "image", ctx(group_type="game")), "app"),
            ("キャッシュ", ("a.png", HOME + r"\Downloads\cache", "image",
                            ctx(location="ダウンロード フォルダ", container="cache")), "managed"),
            ("バックアップ", ("a.png", HOME + r"\Documents\backup", "image",
                              ctx(location="ドキュメント", container="backup")), "backup"),
            ("版違い", ("a.png", HOME + r"\Documents\site_v2\img", "image", ctx(location="ドキュメント")), "version"),
            ("クラウド", ("a.png", HOME + r"\OneDrive\Pictures", "image",
                          ctx(location="ピクチャ", cloud="OneDrive")), "cloud"),
            ("場所不明", ("a.png", r"D:\data", "image", ctx()), "location"),
            ("隠し", (".a.png", HOME + r"\Downloads", "image", DL), "hidden"),
            ("テキスト", ("readme.txt", HOME + r"\Downloads", "document", DL), "kind"),
            ("svg", ("icon.svg", HOME + r"\Downloads", "image", DL), "kind"),
            ("推定どまり", ("a.jpg", HOME + r"\Downloads", "image", DL, "likely"), "kind"),
            ("種類不明", ("data.bin", HOME + r"\Downloads", "unknown", DL, "unknown"), "kind"),
        ]
        for label, args, code in cases:
            with self.subTest(label):
                ok, got = self.check(*args)
                self.assertFalse(ok, f"{label} を候補にしている")
                self.assertEqual(got, code)

    def test_理由にはすべて表示用の文がある(self):
        for code in ("missing", "protected", "system", "deps", "vcs", "project", "app", "managed",
                     "backup", "version", "cloud", "location", "hidden", "kind"):
            self.assertIn(code, dupselect.REASONS)


class TestVersionLike(unittest.TestCase):
    def test_版らしい名前(self):
        for name in ("v2", "V1.3", "app-1.2.3", "release", "old", "Old Files", "旧版", "最新", "project_v10",
                     "ver2", "rev3", "2.0", "backup", "archive"):
            with self.subTest(name):
                self.assertTrue(dupselect.version_like(name))

    def test_版らしくない名前(self):
        for name in ("photos", "2024 旅行", "新しいフォルダー", "vacation", "invoice", "images (2)", "movies"):
            with self.subTest(name):
                self.assertFalse(dupselect.version_like(name))

    def test_標準フォルダより下だけを見る(self):
        # ユーザー名や標準フォルダの名前そのものは、版の判定に使わない
        self.assertEqual(dupselect.folders_below_user_location(r"C:\Users\v2\Downloads\a\b"), ["a", "b"])
        self.assertEqual(dupselect.folders_below_user_location(r"D:\stuff\a"), [])


class TestKeepPreference(unittest.TestCase):
    def test_コピー名より元の名前を残す(self):
        orig = dupselect.keep_preference(r"C:\x\photo.jpg", 200.0, None)
        copy = dupselect.keep_preference(r"C:\x\photo (1).jpg", 100.0, None)
        copy2 = dupselect.keep_preference(r"C:\x\photo - コピー.jpg", 100.0, None)
        self.assertLess(orig, copy)
        self.assertLess(orig, copy2)

    def test_ダウンロードより他を残し_次に古いもの(self):
        dl = dupselect.keep_preference(r"C:\u\Downloads\a.jpg", 100.0, "ダウンロード フォルダ")
        pics = dupselect.keep_preference(r"C:\u\Pictures\a.jpg", 200.0, "ピクチャ")
        self.assertLess(pics, dl)
        old = dupselect.keep_preference(r"C:\u\Pictures\a.jpg", 100.0, "ピクチャ")
        new = dupselect.keep_preference(r"C:\u\Pictures\b\a.jpg", 200.0, "ピクチャ")
        self.assertLess(old, new)


class TestTopLocations(unittest.TestCase):
    def test_スキャン対象から2段目でまとめて容量順(self):
        root = r"C:\scan"
        items = [(r"C:\scan\a\x\1.bin", 10), (r"C:\scan\a\x\y\2.bin", 10), (r"C:\scan\b\3.bin", 50),
                 (r"C:\scan\4.bin", 1)]
        locs, more = dupselect.top_locations(items, root, depth=2, limit=2)
        self.assertEqual(locs[0], (r"C:\scan\b", 1, 50))
        self.assertEqual(locs[1], (r"C:\scan\a\x", 2, 20))
        self.assertEqual(more, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
