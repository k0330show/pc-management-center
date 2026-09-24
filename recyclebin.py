"""このアプリがごみ箱へ移した項目の記録と、元の場所への復元。

Windows のごみ箱全体は扱わない。空にする・完全に削除する機能も持たない。

## ごみ箱の中の項目をどう特定するか

NTFS のドライブでは、ごみ箱へ移した項目は `<ドライブ>\\$Recycle.Bin\\<ユーザーの SID>\\` に
2 つ 1 組で置かれる（Windows Vista 以降の仕組み）。

    $R<6 文字><拡張子>   実体（元のファイル・フォルダを名前だけ変えたもの）
    $I<6 文字><拡張子>   記録（形式の版・元の容量・移した日時〔100 ナノ秒単位〕・元のパス）

6 文字は項目ごとにランダムに決まる。このアプリは、移した直後にごみ箱の `$I` を読み、
「元のパスが一致」「移す直前以降に作られた」「まだ他の記録に使っていない」ものが
**ちょうど 1 つ**のときだけ、その組（`$R` の名前と `$I` の中身）を記録する。
候補が無い・2 つ以上あるときは推測せず「特定できない」として記録する。

名前と元のパスだけで同じものとは決めない。確認のたびに、`$I` の中身（元のパス・
100 ナノ秒単位の日時・容量）と、`$R` の有無・種類を照らし合わせる。`$R` の容量・更新日時は
照合に使わない（同じパスのファイルを入れ替えた直後は、前のファイルの値が返ることがあるため）。
Windows 側で復元・削除されると `$R` / `$I` が無くなるので「見つからない」になる。

## 復元

`$R` を元のパスへ名前を変えて戻し（同じドライブ内の移動）、`$I` を消す。
Windows の「元に戻す」と同じ結果になる。

- 元の場所に同じ名前のものがあれば戻さない（上書きしない。名前の変更も失敗する）
- 元のフォルダが無ければ戻さない（別の場所へは戻さない。フォルダを勝手に作らない）

## 制約

- 特定できるのは、このアプリ（v0.8 以降）が移した項目だけ
- `$I` の形式は版 1（Vista〜8.1）と版 2（Windows 10 以降）だけを読む。知らない版なら特定しない
- ネットワーク上の場所・ごみ箱の無いドライブは、そもそも移動しない（trash.py）
"""

from __future__ import annotations

from i18n import DATA, N_, T

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field

_EPOCH_AS_FILETIME = 116444736000000000


class RestoreError(Exception):
    """復元できなかった理由。code は画面の案内を選ぶのに使う。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class InfoRecord:
    version: int
    size: int
    deleted_ft: int          # 移した日時（FILETIME、100 ナノ秒単位）
    orig_path: str

    @property
    def deleted(self) -> float:
        return (self.deleted_ft - _EPOCH_AS_FILETIME) / 1e7


def read_info(path: str) -> InfoRecord:
    """`$I` ファイルを読む。知らない形式なら ValueError。"""
    with open(path, "rb") as fh:
        data = fh.read(4096)
    if len(data) < 24:
        raise ValueError(T('短すぎる'))
    version = int.from_bytes(data[0:8], "little")
    size = int.from_bytes(data[8:16], "little")
    ft = int.from_bytes(data[16:24], "little")
    if version == 2:
        n = int.from_bytes(data[24:28], "little")
        raw = data[28:28 + 2 * n]
        if len(raw) != 2 * n:
            raise ValueError(T('元のパスが途中で切れている'))
        orig = raw.decode("utf-16-le").rstrip("\0")
    elif version == 1:
        orig = data[24:24 + 520].decode("utf-16-le").split("\0", 1)[0]
    else:
        raise ValueError(T('知らない形式（版 {0}）', version))
    return InfoRecord(version, size, ft, orig)


_sid_cache: list = []


def user_sid() -> str | None:
    """今のユーザーの SID（ごみ箱のフォルダ名）。取れなければ None。"""
    if _sid_cache:
        return _sid_cache[0]
    sid = None
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
            token = wintypes.HANDLE()
            if advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                try:
                    need = wintypes.DWORD()
                    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(need))   # TokenUser
                    buf = ctypes.create_string_buffer(need.value)
                    if advapi32.GetTokenInformation(token, 1, buf, need, ctypes.byref(need)):
                        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                        text = wintypes.LPWSTR()
                        if advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
                            sid = text.value
                            kernel32.LocalFree(text)
                finally:
                    kernel32.CloseHandle(token)
        except Exception:
            sid = None
    _sid_cache.append(sid)
    return sid


def bin_dir_for(path: str) -> str | None:
    """その場所のドライブの、今のユーザーのごみ箱フォルダ。分からなければ None。"""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    sid = user_sid()
    if not drive or drive.startswith("\\\\") or not sid:
        return None
    return os.path.join(drive + "\\", "$Recycle.Bin", sid)


@dataclass
class Entry:
    """このアプリがごみ箱へ移した 1 項目の記録。"""
    name: str
    orig_path: str
    is_dir: bool
    size: int
    moved_at: float                  # このアプリが移した時刻
    state: str = "unresolved"        # in_bin / unresolved / restored / gone
    bin_dir: str | None = None
    r_name: str | None = None        # 実体（$R…）
    i_name: str | None = None        # 記録（$I…）
    deleted_ft: int | None = None    # $I の日時（照合に使う）
    r_size: int | None = None        # ファイルの実体の容量・更新日時（照合に使う）
    r_mtime: float | None = None
    note: str = ""
    restored_at: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """記録の識別子。ごみ箱の中の名前（ランダム）で、名前や元のパスでは決めない。"""
        return f"{self.bin_dir}|{self.r_name}" if self.r_name else f"unresolved|{self.orig_path}|{self.moved_at}"

    @property
    def r_path(self) -> str | None:
        return os.path.join(self.bin_dir, self.r_name) if self.bin_dir and self.r_name else None

    @property
    def i_path(self) -> str | None:
        return os.path.join(self.bin_dir, self.i_name) if self.bin_dir and self.i_name else None


def exact_stat(path: str) -> os.stat_result:
    """開いたハンドルから容量・更新日時を取る。

    Python 3.12 以降の Windows の os.stat は、ファイルを開かずにフォルダの一覧の情報を返す
    近道を使うことがあり、書き換えた直後だと古い容量・日時が返る（実測で確認）。
    照合に使う値は、必ず開いて取る。
    """
    with open(path, "rb") as fh:
        return os.fstat(fh.fileno())


RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)   # 見つからないときに探し直すまでの待ち（秒）


def _list_infos(bdir: str, since: float) -> dict[str, list[tuple[str, InfoRecord]]] | None:
    """ごみ箱の $I を読む（since 以降に作られたものだけ）。フォルダを読めなければ None。"""
    infos: dict[str, list[tuple[str, InfoRecord]]] = {}
    try:
        with os.scandir(bdir) as it:
            for e in it:
                if not e.name.startswith("$I"):
                    continue
                try:
                    if e.stat().st_mtime < since:
                        continue
                    rec = read_info(e.path)
                except (OSError, ValueError):
                    continue          # 書き込み中・検査中で読めないことがある（探し直しで拾う）
                infos.setdefault(os.path.normcase(rec.orig_path), []).append((e.name, rec))
    except OSError:
        return None
    return infos


def locate(moved: list[tuple[str, bool, int, float]], claimed: set[str]) -> list[Entry]:
    """移した直後に、ごみ箱の中の組を探して記録を作る。

    moved: (元のパス, フォルダか, 容量, 移す直前の時刻)。claimed: すでに記録に使った「$R の場所」。
    移した直後は $I がまだ読めないことがある（書き込み中・ウイルス対策ソフトの検査中）。
    見つからない項目だけ、少し待って何度か探し直す。それでも 1 つに決まらなければ推測しない。
    """
    entries = [Entry(os.path.basename(m[0]) or m[0], m[0], m[1], m[2], time.time()) for m in moved]
    pending = list(zip(moved, entries))
    delays = list(RETRY_DELAYS)
    while True:
        by_bin: dict[str | None, list] = {}
        for m, entry in pending:
            by_bin.setdefault(bin_dir_for(m[0]), []).append((m, entry))
        still = []
        for bdir, items in by_bin.items():
            infos = _list_infos(bdir, min(m[3] for m, _e in items) - 2.0) if bdir else None
            for (orig, is_dir, size, t0), entry in items:
                if infos is None:
                    entry.note = N_('ごみ箱のフォルダを読めなかったため、ごみ箱の中の項目を特定できませんでした')
                    still.append(((orig, is_dir, size, t0), entry))
                    continue
                cands = []
                for i_name, rec in infos.get(os.path.normcase(orig), []):
                    r_name = "$R" + i_name[2:]
                    r_path = os.path.join(bdir, r_name)
                    if r_path in claimed or rec.deleted < t0 - 2.0:
                        continue
                    if not os.path.lexists(r_path) or os.path.isdir(r_path) != is_dir:
                        continue
                    cands.append((i_name, r_name, rec))
                if len(cands) != 1:
                    entry.note = (N_('ごみ箱の中に対応する項目が見つかりませんでした') if not cands else
                                  N_('同じ場所から移した項目が複数あり、どれかを決められませんでした'))
                    still.append(((orig, is_dir, size, t0), entry))
                    continue
                i_name, r_name, rec = cands[0]
                entry.state, entry.note = "in_bin", ""
                entry.bin_dir, entry.i_name, entry.r_name = bdir, i_name, r_name
                entry.deleted_ft = rec.deleted_ft
                entry.size = rec.size or size
                # 照合には $I の中身（Windows が書いた容量）を使う。$R の容量・日時は使わない:
                # 同じパスのファイルを入れ替えた直後は、開いて取っても前のファイルの値が返ることがある（実測）。
                entry.r_size = rec.size
                claimed.add(r_path)
        # 複数あって決められないものは、待っても決まらない。見つからないものだけ探し直す。
        pending = [(m, e) for m, e in still if e.note.startswith(DATA('ごみ箱の中に対応する項目が見つかりません'))]
        if not pending or not delays:
            break
        time.sleep(delays.pop(0))
    return entries


def check(entry: Entry) -> tuple[str, str]:
    """記録と実体を照らし合わせる。(状態, 説明)。状態は in_bin / gone / unresolved / restored。"""
    if entry.state == "restored":
        return "restored", T(entry.note or N_('このアプリで元の場所へ戻しました'))
    if not entry.r_name:
        return "unresolved", T(entry.note or N_('ごみ箱の中の項目を特定できていません'))
    try:
        rec = read_info(entry.i_path)
    except FileNotFoundError:
        return "gone", T('ごみ箱に見つかりません（Windows で元に戻した・ごみ箱から削除した可能性があります）')
    except (OSError, ValueError) as exc:
        return "gone", T('ごみ箱の記録を読めません（{0}）', exc)
    if (os.path.normcase(rec.orig_path) != os.path.normcase(entry.orig_path) or rec.deleted_ft != entry.deleted_ft
            or (entry.r_size is not None and rec.size != entry.r_size)):
        return "gone", T('ごみ箱の記録が、このアプリが移したときと一致しません（別の項目の可能性）')
    rp = entry.r_path
    if not os.path.lexists(rp):
        return "gone", T('ごみ箱に実体が見つかりません（Windows で元に戻した・削除した可能性があります）')
    if os.path.isdir(rp) != entry.is_dir:
        return "gone", T('ごみ箱の中の実体の種類が、記録と一致しません')
    return "in_bin", T('ごみ箱にあります')


def restore_problem(entry: Entry) -> RestoreError | None:
    """復元の前に分かる問題。無ければ None（実行時にもう一度確かめる）。"""
    state, why = check(entry)
    if state != "in_bin":
        code = {"restored": "restored", "unresolved": "unresolved"}.get(state, "gone")
        return RestoreError(code, why)
    if os.path.lexists(entry.orig_path):
        return RestoreError("exists", T('元の場所に同じ名前の項目があります（上書きはしません）'))
    parent = os.path.dirname(entry.orig_path)
    if not os.path.isdir(parent):
        return RestoreError("no_parent", T('元のフォルダ「{0}」がありません（別の場所へは戻しません）', parent))
    return None


def restore(entry: Entry) -> str:
    """元の場所へ戻す。戻せなければ RestoreError。戻せたら補足（あれば）を返す。"""
    problem = restore_problem(entry)
    if problem is not None:
        raise problem
    try:
        os.rename(entry.r_path, entry.orig_path)     # 同じ名前があれば失敗する（上書きしない）
    except FileExistsError:
        raise RestoreError("exists", T('元の場所に同じ名前の項目があります（上書きはしません）'))
    except FileNotFoundError:
        raise RestoreError("gone", T('戻す直前に、ごみ箱の実体か元のフォルダが見つからなくなりました'))
    except OSError as exc:
        raise RestoreError("error", T('戻せませんでした（{0}）', exc.strerror or exc))
    extra = ""
    try:
        os.remove(entry.i_path)                      # ごみ箱の一覧から消える（中身はもう戻してある）
    except OSError as exc:
        extra = T('ごみ箱の記録（{0}）を消せませんでした。ごみ箱に古い表示が残る場合があります（{1}）', entry.i_name, exc.strerror or exc)
    _notify_shell(entry)
    entry.state = "restored"
    entry.restored_at = time.time()
    entry.note = N_('このアプリで元の場所へ戻しました')
    return extra


def _notify_shell(entry: Entry):
    """エクスプローラーのごみ箱・元のフォルダの表示を更新させる（失敗しても復元には影響しない）。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        SHCNE_UPDATEDIR, SHCNF_PATHW = 0x00001000, 0x0005
        for d in (entry.bin_dir, os.path.dirname(entry.orig_path)):
            ctypes.windll.shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, ctypes.c_wchar_p(d), None)
    except Exception:
        pass


def default_ledger_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PCManagementCenter", "trash_log.json")


class Ledger:
    """記録の保存先。アプリを閉じても残る（%LOCALAPPDATA%\\PCManagementCenter\\trash_log.json）。"""

    def __init__(self, path: str):
        self.path = path
        self.entries: list[Entry] = []
        self.load_error = ""
        self.load()

    def load(self):
        self.entries = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.load_error = T('記録を読めませんでした（{0}）', exc)
            return
        for d in data.get("entries", []):
            try:
                self.entries.append(Entry(**d))
            except TypeError:
                continue

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": [asdict(e) for e in self.entries]}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)       # 書きかけで壊れないよう、書き終えてから置き換える

    def claimed(self) -> set[str]:
        return {e.r_path for e in self.entries if e.r_path and e.state != "restored"}

    def add(self, new: list[Entry]):
        if new:
            self.entries.extend(new)
            self.save()

    def remove(self, entries: list[Entry]):
        gone = {id(e) for e in entries}
        self.entries = [e for e in self.entries if id(e) not in gone]
        self.save()
