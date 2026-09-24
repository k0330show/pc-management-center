"""ファイル・フォルダを Windows のごみ箱へ移す（完全削除はしない）。

外部ライブラリは使わず、Windows 標準の shell32 の SHFileOperationW を ctypes で呼ぶ。
Windows は次の場合、ごみ箱へ移す指定をしても「完全削除」してしまうため、事前に確認して実行しない:
  - ごみ箱のないドライブ（ネットワーク・一部のリムーバブルなど）
  - ごみ箱を使わない設定（NukeOnDelete）
  - ごみ箱の容量上限を超える大きさのファイル
念のため FOF_WANTNUKEWARNING も指定し、それでも完全削除になる場合は Windows が警告する。
"""

from __future__ import annotations

from i18n import N_, T

import os
import sys


class TrashError(Exception):
    """ごみ箱へ移せなかった理由（利用者に見せる文言）。"""


# SHFileOperation の結果コード（主なもの）
_ERRORS = {
    0x2: N_('ファイルが見つかりません（スキャン後に移動・削除された可能性があります）'),
    0x3: N_('フォルダが見つかりません'),
    0x5: N_('アクセスが拒否されました（権限がないか、読み取り専用の場所です）'),
    0x20: N_('ほかのアプリがファイルを使用中です。アプリを閉じてからやり直してください'),
    0x21: N_('ほかのアプリがファイルの一部をロックしています'),
    0x78: N_('アクセスが拒否されました（権限がありません）'),
    0x7C: N_('パスが正しくありません'),
    0x4C7: N_('操作がキャンセルされました'),
    0x10000: N_('移動中にエラーが発生しました'),
}

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 削除をごみ箱への移動にする
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000    # ごみ箱に入らず完全削除になる場合は警告させる

DRIVE_FIXED = 3


def _drive_root(path: str) -> str | None:
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if not drive or drive.startswith("\\\\"):
        return None  # UNC（ネットワーク）パス
    return drive + "\\"


def _recycle_settings(root: str) -> tuple[bool | None, int | None]:
    """(ごみ箱を使わない設定か, 容量上限バイト)。分からなければ None。"""
    import ctypes
    import winreg

    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return None, None
    guid = buf.value.rstrip("\\").rsplit("\\", 1)[-1].replace("Volume", "", 1)  # {GUID}
    key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
    nuke = cap = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            try:
                nuke = bool(winreg.QueryValueEx(k, "NukeOnDelete")[0])
            except OSError:
                pass
            try:
                cap = int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
            except OSError:
                pass
    except OSError:
        pass
    return nuke, cap


def check_can_recycle(path: str, size: int) -> None:
    """ごみ箱へ移せる（完全削除にならない）ことを確かめる。移せなければ TrashError。"""
    if not sys.platform.startswith("win"):
        raise TrashError(T('この環境（Windows 以外）ではごみ箱への移動に対応していません'))
    import ctypes

    root = _drive_root(path)
    if root is None:
        raise TrashError(T('ネットワーク上の場所はごみ箱に対応していないため移動しません（完全削除になるため）'))
    if ctypes.windll.kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
        raise TrashError(T('{0} はごみ箱に対応しない可能性のあるドライブのため移動しません（完全削除になるおそれがあるため）', root))
    nuke, cap = _recycle_settings(root)
    if nuke:
        raise TrashError(T('{0} はごみ箱を使わない設定（削除すると即時に完全削除）になっているため移動しません', root))
    if cap is not None and size > cap:
        raise TrashError(T('対象がごみ箱の容量上限（{0:,} MB）より大きく、ごみ箱に入らず完全削除になるため移動しません', cap // (1024 * 1024)))


# --- 操作対象にしない重要な場所 -------------------------------------------------

# ドライブ直下にあるシステム用のフォルダ（これ自身と配下すべて）
_SYSTEM_TOP_DIRS = {"$recycle.bin", "system volume information", "recovery", "boot", "$winreagent",
                    "$sysreset", "config.msi", "documents and settings", "$windows.~bt", "$windows.~ws"}
# ドライブ直下にある Windows のシステムファイル
_SYSTEM_ROOT_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "bootnxt", "dumpstack.log",
                      "dumpstack.log.tmp"}
# ユーザーフォルダ直下の標準フォルダ（フォルダそのものは対象外。中のファイル・フォルダは可）
_USER_SHELL_DIRS = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music", "Favorites",
                    "Contacts", "Links", "Saved Games", "Searches", "OneDrive", "AppData",
                    r"AppData\Local", r"AppData\Roaming", r"AppData\LocalLow", r"AppData\Local\Temp",
                    r"AppData\Local\Programs", r"AppData\Roaming\Microsoft\Windows\Start Menu"]


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("\\/") + os.sep)


_ENV_KEYS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "USERPROFILE",
             "OneDrive", "OneDriveConsumer", "OneDriveCommercial", "PUBLIC", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
_locations_cache: dict[tuple, tuple] = {}


def important_locations() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(配下も含めて対象外の場所, その場所自体だけ対象外の場所)。どちらも (正規化パス, 説明)。

    環境変数から作るだけなので、同じ値の間は作り直さない（まとめて数万件を判定するため）。
    """
    key = tuple(os.environ.get(k) for k in _ENV_KEYS) + (getattr(sys, "frozen", False), sys.executable)
    cached = _locations_cache.get(key)
    if cached is None:
        cached = _locations_cache[key] = _important_locations()
    return cached


def _important_locations() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    env = os.environ
    whole: list[tuple[str, str]] = []
    exact: list[tuple[str, str]] = []
    whole.append((_norm(env.get("SystemRoot") or r"C:\Windows"), N_('Windows のシステムフォルダ')))
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if env.get(key):
            whole.append((_norm(env[key]), N_('アプリのインストール先（Program Files）')))
    if env.get("ProgramData"):
        whole.append((_norm(env["ProgramData"]), N_('全ユーザー共通のアプリデータ（ProgramData）')))
    if getattr(sys, "frozen", False):
        whole.append((_norm(os.path.dirname(sys.executable)), N_('このアプリ自身のインストール先')))
    home = env.get("USERPROFILE")
    if home:
        exact.append((_norm(home), N_('ユーザーフォルダ')))
        exact.append((_norm(os.path.dirname(home)), N_('全ユーザーのフォルダ（Users）')))
        for sub in _USER_SHELL_DIRS:
            exact.append((_norm(os.path.join(home, sub)), (N_('Windows の標準フォルダ（{0}）'), sub)))
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if env.get(key):
            exact.append((_norm(env[key]), N_('OneDrive の同期フォルダ')))
            for sub in ("Desktop", "Documents", "Pictures"):
                exact.append((_norm(os.path.join(env[key], sub)), (N_('OneDrive の標準フォルダ（{0}）'), sub)))
    for key in ("PUBLIC", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        if env.get(key):
            exact.append((_norm(env[key]), (N_('Windows の標準フォルダ（{0}）'), key)))
    return whole, exact


def _label(label) -> str:
    """場所の説明を今の言語で（使い回す一覧には日本語の鍵で持つ）。"""
    return T(*label) if isinstance(label, tuple) else T(label)


def protected_reason(path: str, is_dir: bool) -> str | None:
    """ごみ箱へ移す対象にしない重要な場所なら、その理由を返す。"""
    p = _norm(path)
    drive, rest = os.path.splitdrive(p)
    parts = [x for x in rest.split(os.sep) if x]
    if not parts:
        return T('ドライブのルートのため')
    if parts[0].lower() in _SYSTEM_TOP_DIRS:
        return T('Windows のシステム用フォルダ（{0}）の中にあるため', parts[0])
    if not is_dir and len(parts) == 1 and parts[0].lower() in _SYSTEM_ROOT_FILES:
        return T('Windows のシステムファイルのため')
    whole, exact = important_locations()
    for base, label in whole:
        if _is_within(p, base):
            return T('{0}のため', _label(label)) if p == base else T('{0}の中にあるため', _label(label))
        if is_dir and _is_within(base, p):
            return T('{0}を含むフォルダのため', _label(label))
    if is_dir:
        for base, label in exact:
            if p == base:
                return T('{0}のため', _label(label))
            if _is_within(base, p):
                return T('{0}を含むフォルダのため', _label(label))
    return None


# --- フォルダの現在の中身（確認前の再集計） -------------------------------------

EXCLUDED_DIR_NAMES = {"$RECYCLE.BIN", "System Volume Information"}  # スキャンと同じ除外


class FolderMeasure:
    """フォルダの現在の中身。スキャンと同じ数え方（リンク・ジャンクションは辿らない）。"""

    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.size = 0
        self.errors = 0   # 読み取れなかったフォルダ・ファイル
        self.links = 0

    def key(self):
        return (self.files, self.dirs, self.size, self.errors, self.links)


def measure_folder(path: str) -> FolderMeasure:
    """フォルダ配下を数え直す。フォルダ自体が無ければ FileNotFoundError。"""
    import stat as _stat

    m = FolderMeasure()
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            m.errors += 1
            continue
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if e.name in EXCLUDED_DIR_NAMES:
                    continue
                try:
                    link = e.is_symlink() or e.is_junction()
                except OSError:
                    link = False
                if link:
                    m.links += 1
                else:
                    m.dirs += 1
                    stack.append(e.path)
                continue
            try:
                st = e.stat()
            except OSError:
                m.errors += 1
                continue
            if _stat.S_ISREG(st.st_mode):
                m.files += 1
                m.size += st.st_size
    return m


def is_link_or_junction(path: str) -> bool:
    try:
        return os.path.islink(path) or os.path.isjunction(path)
    except OSError:
        return False


def _shfileop_struct():
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]
    return SHFILEOPSTRUCTW


def move_to_trash(path: str, size: int, hwnd: int = 0) -> None:
    """ファイルまたはフォルダ（中身ごと）をごみ箱へ移す。size は移す総容量。失敗・中止は理由付きの TrashError。"""
    path = os.path.abspath(path)
    check_can_recycle(path, size)
    import ctypes

    from ctypes import wintypes

    # pFrom は NUL を 2 つ続けて終端する必要がある（create_unicode_buffer が末尾に 1 つ足す）
    source = ctypes.create_unicode_buffer(path + "\0")
    op = _shfileop_struct()()
    op.hwnd = hwnd or None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise TrashError(T('Windows の確認で中止されました（ファイルは移動していません）'))
    if code != 0:
        raise TrashError(T(_ERRORS[code]) if code in _ERRORS else T('Windows がエラーを返しました（コード {0:#x}）', code))
    if os.path.lexists(path):
        raise TrashError(T('移動を実行しましたが、元の場所に残っています'))
