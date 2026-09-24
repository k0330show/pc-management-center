"""ファイル・フォルダの「正体」を推定する分類ロジック。

ディスクには触れない純粋関数だけで構成する。スキャン時に一度だけ全ファイルへ
適用し、詳細画面では同じ関数を再実行して判断理由を取り出す（再スキャンしない）。

判定の材料:
  - 拡張子・既知のファイル名
  - パス（Program Files / AppData / steamapps など）
  - 親フォルダ名（node_modules / Cache / saves など）
  - 同じフォルダにあるファイル名（package.json / UnityPlayer.dll など）
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# --- 種類の定義 ---------------------------------------------------------------

# key: (表示名, 一般的な用途, 削除前の注意)
KINDS: dict[str, tuple[str, str, str]] = {
    "video": ("動画", "動画ファイルです。録画・ダウンロードした動画や編集素材などが考えられます。",
              "個人の録画や思い出の可能性があります。削除前に再生して中身を確認してください。"),
    "image": ("画像", "写真・スクリーンショット・イラスト・素材などの画像です。",
              "写真などは他に保存先がないこともあります。削除前に中身を確認してください。"),
    "audio": ("音声", "音楽・録音・効果音などの音声ファイルです。",
              "録音など一点ものの可能性があります。中身を確認してください。"),
    "document": ("文書", "文書・表計算・プレゼン・テキストなどの書類です。",
                 "作成した書類の可能性があります。バックアップの有無を確認してください。"),
    "archive": ("圧縮ファイル", "複数のファイルをまとめて圧縮したものです。ダウンロードした配布物や送付用のまとめなどが考えられます。",
                "展開済みの中身が別にあるなら重複の可能性がありますが、元データがこれしかない場合もあります。"),
    "installer": ("インストーラー", "アプリをインストールするためのファイルです。",
                  "インストール後は使わないことが多いですが、再インストールに必要な場合があります。再入手できるか確認してください。"),
    "disk_image": ("ディスクイメージ", "CD/DVD や USB などの中身を丸ごと 1 ファイルにしたものです。OS やソフトの配布によく使われます。",
                   "再入手できない場合は元に戻せません。"),
    "vm": ("仮想ディスク", "仮想マシンや WSL（Linux 環境）の中身全体を格納したファイルです。",
           "削除すると仮想環境内のデータがすべて失われます。仮想化ソフトから操作するのが安全です。"),
    "executable": ("プログラム", "アプリ本体や、その部品（DLL など）です。",
                   "アプリの一部である可能性があります。単体で削除するとアプリが動かなくなることがあります。"),
    "game_data": ("ゲーム関連", "ゲーム本体を構成するデータ（プログラム・グラフィック・音声など）です。",
                  "削除するとゲームが起動しなくなる可能性があります。不要ならランチャー（Steam など）からアンインストールする方が安全です。"),
    "save": ("セーブデータ（推定）", "ゲームなどの進行状況を保存したデータと考えられます。",
             "削除すると進行状況が失われる可能性があります。クラウド同期されているかどうかはこのアプリでは確認できません。"),
    "code": ("ソースコード", "プログラムのソースコードやスクリプトです。",
             "開発中のコードかもしれません。バージョン管理（Git など）やバックアップの有無を確認してください。"),
    "dev_deps": ("開発: 依存パッケージ・生成物", "開発ツールが自動でダウンロード・生成したデータ（ライブラリ、ビルド結果など）です。",
                 "多くはツールで再取得・再生成できますが、プロジェクトによっては再現できない変更が含まれる場合があります。"),
    "dev_vcs": ("開発: 変更履歴（Git など）", "プロジェクトの変更履歴を記録しているバージョン管理のデータです。",
                "削除するとプロジェクトの変更履歴が失われます。"),
    "config": ("設定ファイル", "アプリやツールの設定、構成情報を記録したファイルです。",
               "削除するとアプリの設定が初期化されたり、正しく動かなくなる可能性があります。"),
    "log": ("ログ・診断データ", "動作記録やクラッシュ時の診断情報です。",
            "多くは記録用ですが、不具合調査に使われることがあります。"),
    "cache": ("キャッシュ・一時ファイル", "処理を速くするため、または作業途中に一時的に作られるデータです。",
              "一般に再作成されるデータですが、アプリの動作中に消すと問題が起きることがあります。アプリ自身の削除機能を使う方が安全です。"),
    "database": ("データベース", "アプリが履歴・設定・保存内容などを記録しているデータベースです。",
                 "アプリの大事なデータ（履歴・メール・保存内容など）が入っている可能性があります。"),
    "font": ("フォント", "文字の書体データです。",
             "インストール済みのフォントの場合、削除すると表示に影響することがあります。"),
    "model3d": ("3D・デザインデータ", "3D モデルやデザインツールのデータです。",
                "作成途中の作品の可能性があります。"),
    "backup": ("バックアップ", "何かのバックアップ、または古い版を残したものと考えられます。",
               "元のデータが無事かどうか確認してから判断してください。"),
    "system": ("システムファイル", "Windows の動作に関わるファイルです。",
               "Windows の動作に必要な可能性が高いファイルです。このアプリからは削除を勧めません。"),
    "unknown": ("不明", "中身の種類を特定できませんでした。",
                "中身が特定できないため、削除した場合の影響は判断できません。"),
}

KIND_ORDER = list(KINDS.keys())


def kind_label(kind: str) -> str:
    return KINDS.get(kind, KINDS["unknown"])[0]


# 拡張子 -> (種類, 形式の説明)
_EXT: dict[str, tuple[str, str]] = {}


def _reg(kind: str, desc: str, *exts: str):
    for e in exts:
        _EXT[e] = (kind, desc)


_reg("video", "動画ファイル", ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
     ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".vob")
_reg("image", "画像ファイル", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
     ".tif", ".tiff", ".svg", ".ico", ".avif")
_reg("image", "カメラの RAW 画像", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2")
_reg("image", "Photoshop 画像", ".psd")
_reg("audio", "音声ファイル", ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma",
     ".aiff", ".aif", ".mid", ".midi")
_reg("document", "PDF 文書", ".pdf")
_reg("document", "Word 文書", ".doc", ".docx")
_reg("document", "Excel ブック", ".xls", ".xlsx", ".xlsm")
_reg("document", "PowerPoint プレゼンテーション", ".ppt", ".pptx")
_reg("document", "OpenDocument 文書", ".odt", ".ods", ".odp")
_reg("document", "テキスト・文書", ".txt", ".md", ".rtf", ".csv", ".tsv", ".epub", ".jtd", ".html", ".htm")
_reg("archive", "圧縮ファイル", ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
     ".lzh", ".cab")
_reg("installer", "Windows インストーラー", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle",
     ".msp", ".msu")
_reg("installer", "他 OS 用のインストーラー", ".dmg", ".pkg", ".deb", ".rpm", ".apk")
_reg("disk_image", "ディスクイメージ", ".iso", ".cue", ".nrg", ".mds")
_reg("vm", "仮想ディスク", ".vhd", ".vhdx", ".vmdk", ".vdi", ".qcow2", ".avhdx")
_reg("executable", "Windows の実行ファイル", ".exe", ".com", ".scr")
_reg("executable", "プログラムの部品（ライブラリ）", ".dll", ".ocx", ".so", ".dylib")
_reg("game_data", "ゲームのデータ格納ファイル", ".pak", ".assets", ".unity3d", ".vpk", ".bsa",
     ".ba2", ".esm", ".esp", ".wad", ".rpf", ".forge", ".upk", ".ucas", ".utoc", ".ress")
_reg("game_data", "Unreal Engine のアセット", ".uasset", ".umap")
_reg("game_data", "ゲーム用の動画形式", ".bk2", ".bik", ".usm")
_reg("game_data", "Unity のアセット（シーン・プレハブ・素材など）", ".unity", ".prefab", ".asset", ".mat",
     ".anim", ".controller", ".overridecontroller", ".physicmaterial", ".scenetemplate", ".inputactions",
     ".spriteatlas", ".terrainlayer", ".mixer", ".playable", ".signal", ".lighting")
_reg("game_data", "Godot のシーン・リソース", ".tscn", ".tres")
_reg("config", "Unity のアセット管理情報（.meta）", ".meta")
_reg("config", "Unity のアセンブリ定義", ".asmdef", ".asmref")
_reg("config", "Visual Studio のプロジェクト定義", ".csproj", ".sln", ".slnx", ".vcxproj", ".props", ".targets")
_reg("config", "JSON Lines（データ記録）", ".jsonl", ".ndjson")
_reg("cache", "TypeScript のビルドキャッシュ", ".tsbuildinfo")
_reg("log", "パフォーマンス計測の記録", ".cpuprofile", ".heapsnapshot")
_reg("save", "セーブデータ形式", ".sav", ".save", ".sl2", ".ess")
_reg("code", "ソースコード", ".py", ".js", ".mjs", ".cjs", ".jsx", ".tsx", ".java", ".c", ".cpp",
     ".cc", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".lua",
     ".vue", ".css", ".scss", ".sql", ".ipynb", ".gd", ".dart", ".shader", ".hlsl", ".cginc",
     ".compute", ".glsl", ".uss", ".uxml")
_reg("code", "スクリプト", ".bat", ".cmd", ".ps1", ".sh", ".vbs")
_reg("config", "設定ファイル", ".ini", ".cfg", ".conf", ".toml", ".reg", ".properties", ".env", ".plist")
_reg("config", "JSON / YAML / XML（設定やデータの記録に使われる形式）", ".json", ".yaml", ".yml", ".xml")
_reg("log", "ログ", ".log", ".etl", ".evtx")
_reg("log", "クラッシュダンプ（異常終了時の記録）", ".dmp", ".mdmp", ".hdmp")
_reg("database", "SQLite などのデータベース", ".sqlite", ".sqlite3", ".db", ".db3")
_reg("database", "Access データベース", ".mdb", ".accdb")
_reg("database", "Outlook のメールデータ", ".pst", ".ost")
_reg("database", "SQL Server のデータベース", ".mdf", ".ldf")
_reg("font", "フォント", ".ttf", ".otf", ".ttc", ".woff", ".woff2", ".fon")
_reg("model3d", "3D・デザインデータ", ".blend", ".fbx", ".stl", ".max", ".ma", ".mb", ".3ds",
     ".glb", ".gltf", ".ai", ".fig", ".sketch", ".clip")
_reg("backup", "バックアップ・旧版", ".bak", ".old", ".orig", ".backup")
_reg("cache", "一時ファイル", ".tmp", ".temp", ".cache")
_reg("cache", "ダウンロード途中のファイル", ".crdownload", ".part", ".partial", ".download")
_reg("system", "Windows のシステム関連ファイル", ".sys", ".mui", ".cat", ".efi", ".drv")

# 拡張子だけでは中身を特定できないもの
AMBIGUOUS_EXTS = {".dat", ".bin", ".data", ".blob", ".idx", ".pack", ".chunk", ".raw", ".img",
                  ".lock", ".res", ".arc", ".bundle", ".obj", ".ts", ""}

# 拡張子より優先する既知のファイル名 -> (種類, 説明)
_KNOWN_NAMES: dict[str, tuple[str, str]] = {
    "pagefile.sys": ("system", "Windows の仮想メモリ（ページファイル）"),
    "hiberfil.sys": ("system", "Windows の休止状態・高速スタートアップ用データ"),
    "swapfile.sys": ("system", "Windows のストアアプリ用スワップファイル"),
    "ntuser.dat": ("system", "ユーザーごとの Windows 設定（レジストリ）"),
    "usrclass.dat": ("system", "ユーザーごとの Windows 設定（レジストリ）"),
    "desktop.ini": ("system", "フォルダの表示設定（Windows が自動作成）"),
    "thumbs.db": ("cache", "画像のサムネイル キャッシュ（Windows が自動作成）"),
    "iconcache.db": ("cache", "アイコンのキャッシュ（Windows が自動作成）"),
    ".ds_store": ("config", "macOS のフォルダ表示設定"),
    "memory.dmp": ("log", "Windows のクラッシュダンプ"),
    "package-lock.json": ("config", "Node.js の依存関係ロックファイル"),
    "yarn.lock": ("config", "Yarn の依存関係ロックファイル"),
    "pnpm-lock.yaml": ("config", "pnpm の依存関係ロックファイル"),
    "poetry.lock": ("config", "Poetry の依存関係ロックファイル"),
    "cargo.lock": ("config", "Rust の依存関係ロックファイル"),
    "dockerfile": ("config", "Docker のコンテナ定義"),
    "makefile": ("code", "ビルド手順の定義（Makefile）"),
    "license": ("document", "ライセンス条項"),
    "readme": ("document", "説明書き"),
}

_INSTALLER_NAME = re.compile(r"(setup|install|installer|インストール)", re.IGNORECASE)
_SAVE_NAME = re.compile(r"(^|[^a-z])(save|savedata|savegame|slot\d*|profile)([^a-z]|$)", re.IGNORECASE)

# --- フォルダ名による「領域」の判定 -------------------------------------------

_CONTAINERS: dict[str, tuple[str, str]] = {
    "node_modules": ("deps", "Node.js の依存パッケージを置く「node_modules」フォルダ"),
    "bower_components": ("deps", "Bower の依存パッケージフォルダ"),
    "site-packages": ("deps", "Python のライブラリを置く「site-packages」フォルダ"),
    ".venv": ("deps", "Python の仮想環境「.venv」フォルダ"),
    "__pycache__": ("deps", "Python が自動生成するキャッシュ「__pycache__」フォルダ"),
    ".pytest_cache": ("deps", "pytest が自動生成するキャッシュフォルダ"),
    ".mypy_cache": ("deps", "mypy が自動生成するキャッシュフォルダ"),
    ".next": ("deps", "Next.js のビルド生成物フォルダ"),
    ".nuxt": ("deps", "Nuxt のビルド生成物フォルダ"),
    ".gradle": ("deps", "Gradle のキャッシュフォルダ"),
    ".parcel-cache": ("deps", "Parcel のキャッシュフォルダ"),
    ".turbo": ("deps", "Turborepo のキャッシュフォルダ"),
    "library": ("deps", None),  # Unity プロジェクト内でのみ（下で判定）
    ".git": ("vcs", "Git の変更履歴を保存する「.git」フォルダ"),
    ".svn": ("vcs", "Subversion の管理フォルダ"),
    ".hg": ("vcs", "Mercurial の管理フォルダ"),
    "cache": ("cache", None), "caches": ("cache", None), ".cache": ("cache", None),
    "cache_data": ("cache", None), "gpucache": ("cache", None), "code cache": ("cache", None),
    "shadercache": ("cache", None), "dxcache": ("cache", None), "glcache": ("cache", None),
    "inetcache": ("cache", None), "cachestorage": ("cache", None), "webcache": ("cache", None),
    "temp": ("cache", None), "tmp": ("cache", None),
    "logs": ("logs", None), "log": ("logs", None), "crashdumps": ("logs", None),
    "crashpad": ("logs", None), "crashes": ("logs", None), "crash reports": ("logs", None),
    "save": ("saves", None), "saves": ("saves", None), "savedata": ("saves", None),
    "savegames": ("saves", None), "savegame": ("saves", None), "saved games": ("saves", None),
    "backup": ("backup", None), "backups": ("backup", None),
}

# プロジェクト内でだけビルド生成物とみなすフォルダ名（汎用的な名前なので）
_PROJECT_BUILD_DIRS = {"build", "dist", "target", "out", "obj", "bin", "vendor", "venv", "env",
                       "coverage", ".output", "cmake-build-debug", "cmake-build-release"}

_CONTAINER_TO_KIND = {"deps": "dev_deps", "vcs": "dev_vcs", "cache": "cache", "logs": "log",
                      "saves": "save", "backup": "backup"}

_CONTAINER_PHRASE = {"cache": "キャッシュ・一時データ", "logs": "ログ・診断データ",
                     "saves": "セーブデータ", "backup": "バックアップ"}

# --- パスによる「関連するアプリ」の判定 ---------------------------------------

_VENDORS = {"google", "microsoft", "mozilla", "adobe", "jetbrains", "nvidia corporation", "nvidia",
            "apple", "intel", "amd", "realtek", "oracle", "autodesk", "unity", "epic games",
            "valve", "blizzard entertainment", "electronic arts", "ubisoft", "packages"}

_LOCATIONS = [
    ("downloads", "ダウンロード フォルダ"), ("desktop", "デスクトップ"),
    ("documents", "ドキュメント"), ("pictures", "ピクチャ"), ("videos", "ビデオ"),
    ("music", "ミュージック"),
]


@dataclass(slots=True)
class FolderContext:
    """フォルダごとの判定材料。子フォルダへ引き継がれる。"""
    group_key: str | None = None        # 関連グループの識別子（根元フォルダのパス）
    group_label: str | None = None      # 表示名 例: "開発プロジェクト: my-app（Node.js）"
    group_type: str | None = None       # game / project / app / system
    group_reasons: tuple[str, ...] = ()
    container: str | None = None        # deps / vcs / cache / logs / saves / backup
    container_root: str | None = None
    container_reason: str | None = None
    location: str | None = None         # "ダウンロード フォルダ" など
    cloud: str | None = None            # OneDrive など同期フォルダ


@dataclass
class Classification:
    kind: str
    certainty: str                      # sure / likely / unknown
    format: str
    group_key: str | None
    group_label: str | None
    purpose: str
    possibility: str | None = None
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    location: str | None = None

    @property
    def kind_label(self) -> str:
        return kind_label(self.kind)

    @property
    def kind_display(self) -> str:
        if self.kind == "unknown":
            return "種類は不明"
        if self.certainty == "likely":
            label = self.kind_label
            return label if "推定" in label else f"{label}（推定）"
        return self.kind_label


def _lower_parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path) if p]


def _path_group(parts: list[str], lparts: list[str]):
    """パスの形だけで分かる関連グループ。(key, label, type, reason, container, container_reason)"""
    n = len(lparts)
    joined_prefix = lambda i: os.sep.join(parts[: i + 1]) if i > 0 else parts[0] + os.sep

    if n >= 2 and lparts[1] == "windows" and re.match(r"^[a-z]:$", lparts[0]):
        return (joined_prefix(1), "Windows システム", "system",
                "Windows フォルダ（C:\\Windows）の中にあるため", None, None)

    for i, p in enumerate(lparts):
        nxt = lparts[i + 1] if i + 1 < n else None
        # Steam
        if p == "steamapps" and nxt == "common" and i + 2 < n:
            return (joined_prefix(i + 2), f"ゲーム: {parts[i + 2]}（Steam）", "game",
                    f"Steam のゲームのインストール先（steamapps\\common\\{parts[i + 2]}）にあるため", None, None)
        if p == "steamapps" and nxt == "workshop" and i + 3 < n and lparts[i + 2] == "content":
            return (joined_prefix(i + 3), f"Steam ワークショップ（アプリ ID {parts[i + 3]}）", "game",
                    "Steam ワークショップ（MOD など）の保存先にあるため", None, None)
        if p == "steamapps" and nxt in ("shadercache", "downloading", "temp"):
            return (joined_prefix(i), "アプリ: Steam", "app", "Steam のデータフォルダにあるため",
                    "cache", f"Steam の {parts[i + 1]} フォルダ（一時データやキャッシュ）")
        # 各種ランチャー
        if p == "epic games" and nxt and nxt not in ("launcher", "epic online services"):
            return (joined_prefix(i + 1), f"ゲーム: {parts[i + 1]}（Epic Games）", "game",
                    "Epic Games のインストール先にあるため", None, None)
        if p in ("gog games", "riot games", "xboxgames") and nxt:
            store = {"gog games": "GOG", "riot games": "Riot Games", "xboxgames": "Xbox"}[p]
            if nxt != "riot client":
                return (joined_prefix(i + 1), f"ゲーム: {parts[i + 1]}（{store}）", "game",
                        f"{store} のゲームのインストール先にあるため", None, None)
        if p in ("gog galaxy", "ubisoft game launcher") and nxt == "games" and i + 2 < n:
            return (joined_prefix(i + 2), f"ゲーム: {parts[i + 2]}", "game",
                    f"{parts[i]} のゲームのインストール先にあるため", None, None)
        if p == "my games" and nxt:
            return (joined_prefix(i + 1), f"ゲーム: {parts[i + 1]}", "game",
                    "「ドキュメント\\My Games」はゲームがセーブデータや設定を置く場所のため",
                    "saves", "「My Games」配下（セーブデータや設定がよく置かれる場所）")
        if p == "saved games" and nxt:
            return (joined_prefix(i + 1), f"ゲーム: {parts[i + 1]}", "game",
                    "「Saved Games」はゲームがセーブデータを置く場所のため",
                    "saves", "「Saved Games」配下（セーブデータの保存場所）")

    # アプリのデータ・インストール先
    for i, p in enumerate(lparts):
        base = None
        if p == "appdata" and i + 2 < n and lparts[i + 1] in ("roaming", "local", "locallow"):
            j = i + 2
            if lparts[j] == "temp":
                return None
            if lparts[j] == "programs" and j + 1 < n:
                j += 1
            if lparts[j] == "packages" and j + 1 < n:
                name = parts[j + 1].split("_")[0]
                return (joined_prefix(j + 1), f"アプリ: {name}（ストアアプリ）", "app",
                        "ストアアプリのデータ保存先（AppData\\Local\\Packages）にあるため", None, None)
            base = (j, f"アプリのデータ保存先（AppData\\{parts[i + 1]}）にあるため")
        elif p in ("program files", "program files (x86)") and i + 1 < n and i <= 1:
            base = (i + 1, f"アプリのインストール先（{parts[i]}）にあるため")
        elif p == "programdata" and i + 1 < n and i <= 1:
            base = (i + 1, "全ユーザー共通のアプリデータ（ProgramData）にあるため")
        elif (i >= 2 and lparts[i - 2] == "users" and p.startswith(".") and len(p) > 1
              and p not in (".git",)):
            return (joined_prefix(i), f"ツール: {parts[i]}", "app",
                    f"ユーザーフォルダ直下の「{parts[i]}」は開発ツールなどが設定やキャッシュを置く場所のため",
                    None, None)
        if base:
            j, reason = base
            name = parts[j]
            end = j
            if lparts[j] in _VENDORS and j + 1 < n:
                name = f"{parts[j]} {parts[j + 1]}"
                end = j + 1
            return (joined_prefix(end), f"アプリ: {name}", "app", reason, None, None)
    return None


def _location(lparts: list[str]):
    loc = None
    cloud = None
    for i, p in enumerate(lparts):
        if i >= 2 and lparts[i - 2] == "users":
            for key, label in _LOCATIONS:
                if p == key:
                    loc = label
            if p.startswith("onedrive"):
                cloud = "OneDrive"
            elif p == "dropbox":
                cloud = "Dropbox"
            elif p in ("google drive", "googledrive"):
                cloud = "Google ドライブ"
            elif p == "appdata":
                loc = "AppData（アプリのデータ領域）"
    if len(lparts) >= 2 and lparts[1] in ("program files", "program files (x86)"):
        loc = "Program Files（インストール済みアプリ）"
    return loc, cloud


_PROJECT_MARKERS = [
    ("package.json", "Node.js"), ("pyproject.toml", "Python"), ("setup.py", "Python"),
    ("requirements.txt", "Python"), ("pipfile", "Python"), ("cargo.toml", "Rust"),
    ("go.mod", "Go"), ("pom.xml", "Java"), ("build.gradle", "Java/Kotlin"),
    ("build.gradle.kts", "Kotlin"), ("cmakelists.txt", "C/C++"), ("composer.json", "PHP"),
    ("gemfile", "Ruby"), ("pubspec.yaml", "Flutter"), ("project.godot", "Godot"),
    ("vite.config.js", "Vite"), ("vite.config.ts", "Vite"), ("tsconfig.json", "TypeScript"),
]


def _project_markers(dirnames_l: set[str], filenames_l: set[str]):
    kinds = []
    found = []
    for marker, label in _PROJECT_MARKERS:
        if marker in filenames_l:
            found.append(marker)
            if label not in kinds:
                kinds.append(label)
    for fn in filenames_l:
        if fn.endswith(".sln") or fn.endswith(".csproj"):
            found.append(fn)
            if ".NET" not in kinds:
                kinds.append(".NET")
        elif fn.endswith(".uproject"):
            found.append(fn)
            kinds.append("Unreal Engine")
    if "assets" in dirnames_l and "projectsettings" in dirnames_l:
        found.append("Assets + ProjectSettings フォルダ")
        kinds.append("Unity")
    if ".git" in dirnames_l:
        found.append(".git フォルダ")
        if not kinds:
            kinds.append("Git 管理")
    return kinds, found


def _game_markers(dirnames_l: set[str], filenames_l: set[str]):
    if "unityplayer.dll" in filenames_l:
        return "UnityPlayer.dll（Unity 製ゲームの実行部品）"
    for n in ("steam_api64.dll", "steam_api.dll"):
        if n in filenames_l:
            return f"{n}（Steam 連携の部品）"
    if "engine" in dirnames_l and any(f.endswith(".exe") for f in filenames_l):
        return "Engine フォルダと .exe（Unreal Engine 製ゲームによくある構成）"
    return None


def folder_context(path: str, dirnames: list[str], filenames: list[str],
                   parent: FolderContext | None) -> FolderContext:
    """フォルダの判定材料を作る。親の結果を引き継ぎつつ、このフォルダで分かることを足す。"""
    parts = _lower_parts(path)
    lparts = [p.lower() for p in parts]
    name_l = lparts[-1] if lparts else ""
    dirnames_l = {d.lower() for d in dirnames}
    filenames_l = {f.lower() for f in filenames}
    parent = parent or FolderContext()
    ctx = FolderContext()
    ctx.location, ctx.cloud = _location(lparts)

    # 1) 関連グループ
    pg = _path_group(parts, lparts)
    path_container = None
    if pg and pg[2] in ("system", "game"):
        ctx.group_key, ctx.group_label, ctx.group_type = pg[0], pg[1], pg[2]
        ctx.group_reasons = (pg[3],)
        if pg[4]:
            path_container = (pg[4], pg[0], pg[5])
    elif parent.group_type in ("game", "project"):
        ctx.group_key, ctx.group_label = parent.group_key, parent.group_label
        ctx.group_type, ctx.group_reasons = parent.group_type, parent.group_reasons
    else:
        in_app_area = pg is not None or ctx.location == "Program Files（インストール済みアプリ）"
        game_marker = _game_markers(dirnames_l, filenames_l)
        kinds, found = ([], [])
        if not in_app_area and parent.container not in ("deps", "vcs"):
            kinds, found = _project_markers(dirnames_l, filenames_l)
        if game_marker and not kinds:
            ctx.group_key, ctx.group_type = path, "game"
            ctx.group_label = f"ゲーム: {parts[-1]}（推定）"
            ctx.group_reasons = (f"フォルダ「{parts[-1]}」に {game_marker} があるため、ゲームの可能性",)
        elif kinds:
            ctx.group_key, ctx.group_type = path, "project"
            ctx.group_label = f"開発プロジェクト: {parts[-1]}（{'・'.join(kinds)}）"
            ctx.group_reasons = (
                f"フォルダ「{parts[-1]}」に {', '.join(found[:4])} があるため、{'・'.join(kinds)} の開発プロジェクトと判断",)
        elif pg:
            ctx.group_key, ctx.group_label, ctx.group_type = pg[0], pg[1], pg[2]
            ctx.group_reasons = (pg[3],)
            if pg[4]:
                path_container = (pg[4], pg[0], pg[5])

    # 2) 領域（node_modules / Cache など）
    if parent.container in ("deps", "vcs"):
        ctx.container, ctx.container_root, ctx.container_reason = (
            parent.container, parent.container_root, parent.container_reason)
    else:
        found_c = None
        spec = _CONTAINERS.get(name_l)
        if spec:
            ctype, reason = spec
            if name_l == "library":
                if ctx.group_type == "project" and "Unity" in (ctx.group_label or "") and \
                        os.path.dirname(path) == ctx.group_key:
                    found_c = ("deps", "Unity が自動生成する「Library」フォルダ（再生成可能なキャッシュ）")
            else:
                found_c = (ctype, reason or f"「{parts[-1]}」フォルダ（一般に{_CONTAINER_PHRASE.get(ctype, ctype)}を置く名前）")
        elif name_l in _PROJECT_BUILD_DIRS and ctx.group_type == "project" and path != ctx.group_key:
            if name_l in ("venv", "env"):
                if "pyvenv.cfg" in filenames_l:
                    found_c = ("deps", f"Python の仮想環境（「{parts[-1]}」に pyvenv.cfg がある）")
            else:
                found_c = ("deps", f"開発プロジェクト内の「{parts[-1]}」フォルダ（ビルド結果や外部ライブラリがよく置かれる名前）")
        elif "pyvenv.cfg" in filenames_l:
            found_c = ("deps", f"Python の仮想環境（「{parts[-1]}」に pyvenv.cfg がある）")
        if found_c:
            ctx.container, ctx.container_root, ctx.container_reason = found_c[0], path, found_c[1]
        elif path_container:
            ctx.container, ctx.container_root, ctx.container_reason = path_container
        elif parent.container:
            ctx.container, ctx.container_root, ctx.container_reason = (
                parent.container, parent.container_root, parent.container_reason)
    return ctx


def _split_ext(name: str) -> str:
    base, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext and ext[1:].isdigit():
        return ext  # .001 などの分割ファイル
    return ext


def classify_file(name: str, size: int, ctx: FolderContext,
                  siblings_l: set[str] | frozenset = frozenset()) -> Classification:
    """1 ファイルを分類する。siblings_l は同じフォルダのファイル名（小文字）。"""
    name_l = name.lower()
    ext = _split_ext(name)
    stem_l = name_l[: len(name_l) - len(ext)] if ext else name_l
    reasons: list[str] = []
    possibility = None
    kind = "unknown"
    certainty = "unknown"
    fmt = f"拡張子 {ext}" if ext else "拡張子なし"

    known = _KNOWN_NAMES.get(name_l)
    if name_l.startswith("ntuser.dat"):
        known = _KNOWN_NAMES["ntuser.dat"]
    ext_info = _EXT.get(ext)
    container = ctx.container

    if container in ("deps", "vcs"):
        kind = _CONTAINER_TO_KIND[container]
        certainty = "sure" if container == "vcs" or "node_modules" in (ctx.container_reason or "") else "likely"
        if ext_info:
            fmt = f"{ext_info[1]}（{ext}）"
        reasons.append(f"{ctx.container_reason}の中にあるため")
    elif known:
        kind, fmt = known
        certainty = "sure"
        reasons.append(f"ファイル名「{name}」は{fmt}として知られているため")
    else:
        if ext == ".db":
            kind, certainty, fmt = "database", "likely", "データベース（.db）"
            reasons.append("拡張子 .db は多くの場合データベースだが、形式はアプリによって異なるため推定")
        elif ext_info and ext not in AMBIGUOUS_EXTS:
            kind, desc = ext_info
            fmt = f"{desc}（{ext}）"
            certainty = "sure"
            reasons.append(f"拡張子 {ext} は一般に{desc}に使われるため")
        elif ext == ".ts":
            if ctx.group_type == "project" or "package.json" in siblings_l or "tsconfig.json" in siblings_l:
                kind, certainty, fmt = "code", "likely", "TypeScript ソースコード（.ts）"
                reasons.append("拡張子 .ts は TypeScript と動画（MPEG-TS）の両方に使われるが、開発プロジェクト内にあるためソースコードと判断")
            elif size >= 5 * 1024 * 1024:
                kind, certainty, fmt = "video", "likely", "MPEG-TS 動画（.ts）"
                reasons.append("拡張子 .ts は TypeScript と動画（MPEG-TS）の両方に使われるが、5MB 以上と大きく開発プロジェクト外のため動画と推定")
            else:
                reasons.append("拡張子 .ts は TypeScript のコードと MPEG-TS 動画の両方に使われ、区別できないため")
        elif ext in (".bin", ".img") and (stem_l + ".cue") in siblings_l:
            kind, certainty, fmt = "disk_image", "likely", f"ディスクイメージ（{ext} + .cue）"
            reasons.append(f"同じフォルダに同名の「{stem_l}.cue」があり、CD/DVD イメージの組み合わせと考えられるため")
        elif ext == ".obj" and (stem_l + ".mtl") in siblings_l:
            kind, certainty, fmt = "model3d", "likely", "3D モデル（.obj + .mtl）"
            reasons.append(f"同じフォルダに同名の「{stem_l}.mtl」（材質定義）があるため 3D モデルと推定")
        else:
            if ext in AMBIGUOUS_EXTS or not ext:
                what = "拡張子がない" if not ext else f"拡張子 {ext} はさまざまなアプリが独自の形式に使う汎用的な名前の"
                reasons.append(f"{what}ため、拡張子からは中身を特定できない")
            else:
                reasons.append(f"拡張子 {ext} は登録済みの分類に当てはまらないため")

        # 保存場所による補正
        if kind == "executable" and ext in (".exe", ".msi"):
            if _INSTALLER_NAME.search(name):
                kind, certainty = "installer", "likely"
                reasons.append("ファイル名に「setup / install」などを含むため、インストーラーと推定")
            elif ctx.location == "ダウンロード フォルダ" and ctx.group_type is None:
                reasons.append("ダウンロード フォルダにある実行ファイルのため、ダウンロードしたインストーラーや単体アプリの可能性（どちらかは判断できない）")

        if container in ("cache", "logs", "saves", "backup"):
            ckind = _CONTAINER_TO_KIND[container]
            if kind == "unknown":
                possibility = f"保存場所から{kind_label(ckind).replace('（推定）', '')}の可能性"
                reasons.append(f"{ctx.container_reason}の中にあるため")
            elif kind not in ("system",) and not (container == "saves" and kind in ("image", "video")):
                if kind != ckind:
                    reasons.append(f"ファイル形式は{kind_label(kind)}だが、{ctx.container_reason}の中にあるため{kind_label(ckind).replace('（推定）', '')}として扱う")
                kind, certainty = ckind, "likely"
        elif ctx.group_type == "system":
            if kind == "unknown":
                possibility = "保存場所から Windows システム関連の可能性"
            elif kind in ("executable", "config", "database", "log", "font"):
                reasons.append("Windows フォルダ内のため、システムの一部と判断")
                kind, certainty = "system", "likely" if certainty != "sure" else "sure"
        elif ctx.group_type == "game":
            if kind == "unknown":
                if _SAVE_NAME.search(stem_l):
                    possibility = "ファイル名と保存場所からセーブデータの可能性"
                else:
                    possibility = f"保存場所から「{ctx.group_label}」のゲームデータの可能性"
            elif kind in ("executable", "image", "audio", "video", "config", "database", "archive", "font"):
                reasons.append(f"ファイル形式は{kind_label(kind)}だが、ゲームのフォルダ内にあるためゲームの構成データとして扱う")
                kind, certainty = "game_data", "likely"
        elif ctx.group_type == "app":
            if kind == "unknown":
                possibility = f"保存場所から「{ctx.group_label}」が使うデータの可能性"
        elif ctx.group_type == "project":
            if kind == "unknown":
                possibility = f"保存場所から「{ctx.group_label}」の一部の可能性"

        if kind == "unknown" and possibility is None and _SAVE_NAME.search(stem_l) and ext in (".dat", ".bin", ""):
            possibility = "ファイル名から、セーブデータや設定の可能性"

    if ctx.group_label and ctx.group_reasons:
        reasons.append(f"関連: {ctx.group_reasons[0]}")
    if ctx.location:
        reasons.append(f"保存場所: {ctx.location}")

    purpose = _purpose(kind, ctx, fmt)
    cautions = _cautions(kind, ctx)
    return Classification(kind=kind, certainty=certainty, format=fmt,
                          group_key=ctx.group_key, group_label=ctx.group_label,
                          purpose=purpose, possibility=possibility, reasons=reasons,
                          cautions=cautions, location=ctx.location)


def _purpose(kind: str, ctx: FolderContext, fmt: str) -> str:
    base = KINDS.get(kind, KINDS["unknown"])[1]
    if kind == "unknown":
        return "中身の種類を特定できませんでした。アプリが独自の形式で保存したデータの可能性があります。"
    if ctx.group_label and kind in ("game_data", "dev_deps", "dev_vcs", "config", "cache", "database", "log", "save", "executable"):
        return f"{base}（{ctx.group_label}）"
    return base


def _cautions(kind: str, ctx: FolderContext) -> list[str]:
    out = [KINDS.get(kind, KINDS["unknown"])[2]]
    if ctx.group_type == "app" and "AppData" in (ctx.location or ""):
        out.append("アプリのデータ領域です。アプリの使用中は削除しないでください。")
    if ctx.location == "Program Files（インストール済みアプリ）":
        out.append("インストールされたアプリの一部です。不要ならアプリのアンインストール機能を使うのが安全です。")
    if ctx.cloud:
        out.append(f"{ctx.cloud} と同期しているフォルダの可能性があります。削除が他の端末やクラウドにも反映されることがあります。")
    return out


def folder_kind(ctx: FolderContext, path: str, dominant_kind: str | None):
    """フォルダ自身の種類ラベルと理由。(kind_for_filter, label, certainty, reasons)"""
    reasons = []
    if ctx.group_key == path and ctx.group_type:
        reasons.extend(ctx.group_reasons)
        label = {"game": "ゲームのフォルダ", "project": "開発プロジェクトのフォルダ",
                 "app": "アプリのフォルダ", "system": "Windows のシステムフォルダ"}[ctx.group_type]
        certainty = "likely" if "推定" in (ctx.group_label or "") or "可能性" in ctx.group_reasons[0] else "sure"
        return dominant_kind, label, certainty, reasons
    if ctx.container and ctx.container_root == path:
        reasons.append(f"{ctx.container_reason}")
        k = _CONTAINER_TO_KIND[ctx.container]
        return k, f"{kind_label(k)}のフォルダ", "sure" if ctx.container in ("deps", "vcs") else "likely", reasons
    if ctx.container:
        k = _CONTAINER_TO_KIND[ctx.container]
        reasons.append(f"{ctx.container_reason}の中にあるため")
        return k, f"{kind_label(k)}のフォルダ", "likely", reasons
    if dominant_kind:
        return dominant_kind, f"フォルダ（主な中身: {kind_label(dominant_kind)}）", "likely", reasons
    return None, "フォルダ", "unknown", reasons


def folder_cautions(ctx: FolderContext, dominant_kind: str | None) -> list[str]:
    out = []
    if ctx.group_type == "system":
        out.append("Windows の動作に関わるフォルダです。このアプリからは削除を勧めません。")
    elif ctx.container:
        out.append(KINDS[_CONTAINER_TO_KIND[ctx.container]][2])
    elif ctx.group_type == "game":
        out.append(KINDS["game_data"][2])
    elif ctx.group_type == "project":
        out.append("開発プロジェクトです。ソースコードや未保存の作業が含まれる可能性があります。バージョン管理やバックアップを確認してください。")
    elif dominant_kind:
        out.append(KINDS[dominant_kind][2])
    out.extend(c for c in _cautions("unknown", ctx)[1:])
    out.append("フォルダには種類の異なるファイルが混在していることがあります。内訳を確認してから判断してください。")
    return out
