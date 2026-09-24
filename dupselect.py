"""重複結果を「まとめて選ぶ」ときの判定。画面を持たず、ディスクも読まない。

## 低リスク候補

「削除しても問題ない」ものを選ぶのではない。**用途を推測できる材料が揃っていて、
しかも推測の外れにくいものだけ**を機械的に拾う。材料が足りない・外れやすいものは、
対象を広げずに「個別確認」へ回す。

候補にするのは、次をすべて満たすファイルだけ。

    保存場所が利用者の標準フォルダ（ダウンロード・デスクトップ・ドキュメント・
      ピクチャ・ビデオ・ミュージック）の中で、クラウド同期フォルダではない
    開発プロジェクト・依存パッケージ・変更履歴・アプリ・ゲーム・システム・
      キャッシュ・ログ・セーブデータ・バックアップの領域ではない
    途中のフォルダ名に版の違いを思わせる言葉（v2・1.0・old・旧・最新 など）がない
    拡張子が、写真・動画・音楽・書類・配布物のどれかとして確定できる（下の一覧）
    名前が . で始まらない（設定・隠しファイルの慣習）

これで選べないものは、たとえ実際には不要でも候補にしない。

## 限界（画面にもそのまま出す）

判定材料はフォルダ名・拡張子・保存場所・フォルダ内の目印（package.json など）だけで、
**ファイルを開いて中身を見てはいない**。目印のないプロジェクトや、アプリが決まった
場所を読みに行くファイルは見分けられない。
"""

from __future__ import annotations

from i18n import DATA, N_

import os
import re

# 候補にできる拡張子。拡張子だけで中身がほぼ決まり、アプリの部品として使われにくいもの。
# .svg .ico .html .txt .csv .md .json などは Web サイトやプロジェクトの部品になりやすいので入れない。
LOW_RISK_EXTS = frozenset({
    # 写真・画像
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".psd",
    # 動画
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp",
    # 音声
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aiff", ".aif",
    # 書類
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".epub", ".rtf",
    # 配布物（ダウンロードしたもの）
    ".zip", ".rar", ".7z", ".msi", ".iso",
})
LOW_RISK_KINDS = frozenset({"image", "video", "audio", "document", "archive", "installer", "disk_image"})

# 利用者の標準フォルダ（classifier の location 表記）
USER_LOCATIONS = frozenset({N_('ダウンロード フォルダ'), N_('デスクトップ'), N_('ドキュメント'), N_('ピクチャ'), N_('ビデオ'), N_('ミュージック')})
DOWNLOADS = N_('ダウンロード フォルダ')

# 除外の理由。画面にはこの順で件数を出す。
REASONS = {
    "missing": N_('スキャン結果に見つからない（移動済みの可能性）'),
    "protected": N_('保護している場所'),
    "system": N_('Windows・システム関連'),
    "deps": N_('開発の依存パッケージ・生成物'),
    "vcs": N_('開発の変更履歴（.git など）'),
    "project": N_('開発プロジェクトの中'),
    "app": N_('アプリ・ゲームのインストール先やデータ'),
    "managed": N_('キャッシュ・ログ・セーブデータの場所'),
    "backup": N_('バックアップ'),
    "version": N_('版違いの可能性がある名前のフォルダ（v2・1.0・old・旧 など）'),
    "cloud": N_('クラウド同期フォルダ（移動が他の端末にも反映される）'),
    "location": N_('用途を判定できない場所（標準フォルダ以外・AppData など）'),
    "hidden": N_('名前が . で始まる（設定・隠しファイルの慣習）'),
    "kind": N_('種類を確定できない、または候補にしない種類'),
}

# 版の違いを思わせるフォルダ名。英数字は語単位で、日本語は部分一致で見る。
_VERSION_TOKEN = re.compile(r"^(v\d+[\w.]*|ver\d*[\w.]*|version\d*|rev\d*|r\d+|\d+(\.\d+)+[a-z]*)$", re.IGNORECASE)
_VERSION_WORDS = frozenset({"old", "older", "new", "newer", "latest", "final", "release", "releases",
                            "version", "versions", "prev", "previous", "legacy", "archive", "archived",
                            "backup", "backups", "bak", "draft", "stable", "beta", "alpha"})
_VERSION_JA = (DATA('旧'), DATA('最新'), DATA('最終'), DATA('版'), DATA('改訂'), DATA('バージョン'), DATA('過去'), DATA('古い'), DATA('バックアップ'))
_TOKEN_SPLIT = re.compile(r"[\s_\-()\[\]{}+,]+")

# コピーを作ったときに付く名前（残す側を選ぶときに、付いていない方を優先する）
_COPY_NAME = re.compile(DATA('(\\s\\(\\d+\\)$|\\s-\\s?コピー|のコピー|\\s-\\s?copy(\\s\\(\\d+\\))?$|^copy of\\s|[\\s_]copy\\d*$)'),
                        re.IGNORECASE)


def version_like(name: str) -> bool:
    """フォルダ名が、版の違い（v2・1.0・old・旧 など）を思わせるか。"""
    if any(w in name for w in _VERSION_JA):
        return True
    for tok in _TOKEN_SPLIT.split(name):
        if not tok:
            continue
        if tok.lower() in _VERSION_WORDS or _VERSION_TOKEN.match(tok):
            return True
    return False


def folders_below_user_location(folder: str) -> list[str]:
    """Users\\<名前>\\<標準フォルダ> より下のフォルダ名。標準フォルダの外なら空。"""
    parts = [p for p in re.split(r"[\\/]+", folder) if p]
    low = [p.lower() for p in parts]
    for i, p in enumerate(low):
        if p == "users" and i + 2 < len(parts):
            return parts[i + 3:]
    return []


def assess(name: str, folder: str, kind: str, certainty: str, ctx, protected: str | None) -> tuple[bool, str]:
    """低リスク候補にできるか。(候補か, 理由コード)。理由コードは REASONS のキー（候補なら "ok"）。

    ctx は classifier.FolderContext。判定の順番は「外れたら困るもの」から。
    """
    if protected:
        return False, "protected"
    if kind == "system" or ctx.group_type == "system":
        return False, "system"
    if ctx.container == "deps" or kind == "dev_deps":
        return False, "deps"
    if ctx.container == "vcs" or kind == "dev_vcs":
        return False, "vcs"
    if ctx.group_type == "project":
        return False, "project"
    loc = ctx.location or ""
    if ctx.group_type in ("app", "game") or loc.startswith(("Program Files", "AppData")):
        return False, "app"
    if ctx.container in ("cache", "logs", "saves") or kind in ("cache", "log", "save"):
        return False, "managed"
    if ctx.container == "backup" or kind == "backup":
        return False, "backup"
    if any(version_like(p) for p in folders_below_user_location(folder)):
        return False, "version"
    if ctx.cloud:
        return False, "cloud"
    if loc not in USER_LOCATIONS:
        return False, "location"
    if name.startswith("."):
        return False, "hidden"
    ext = os.path.splitext(name)[1].lower()
    if kind not in LOW_RISK_KINDS or certainty != "sure" or ext not in LOW_RISK_EXTS:
        return False, "kind"
    return True, "ok"


def keep_preference(path: str, modified: float, location: str | None):
    """残す側の選びやすさ（小さいほど残す側に向く）。

    コピー名（「(1)」「- コピー」など）でないもの → ダウンロード以外 → 古いもの → パスが短いもの。
    **どれを残しても安全、という意味ではない。** 利用者が確認・変更する前の「案」に使う。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    return (bool(_COPY_NAME.search(stem)), location == DOWNLOADS, modified, len(path), path.lower())


def top_locations(paths_sizes, root: str | None, depth: int = 2, limit: int = 6):
    """主な保存場所。スキャン対象から depth 段目までのフォルダでまとめ、容量の大きい順。

    返り値: ([(場所, 件数, 容量)], 残りの場所の数)
    """
    buckets: dict[str, list[int]] = {}
    root_n = os.path.normcase(root.rstrip("\\/")) if root else None
    for path, size in paths_sizes:
        folder = os.path.dirname(path)
        key = folder
        if root_n and os.path.normcase(folder).startswith(root_n):
            rest = [p for p in re.split(r"[\\/]+", folder[len(root_n):]) if p]
            key = os.path.join(root.rstrip("\\/"), *rest[:depth]) if rest else root
        b = buckets.setdefault(key, [0, 0])
        b[0] += 1
        b[1] += size
    ranked = sorted(buckets.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)
    return [(k, v[0], v[1]) for k, v in ranked[:limit]], max(0, len(ranked) - limit)
