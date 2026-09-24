import gc
import heapq
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import i18n
import trash
import duplicates
import dupselect
import recyclebin
from classifier import (
    KINDS,
    FolderContext,
    classify_file,
    folder_context,
)
from classifier import folder_cautions as _folder_cautions_ja
from classifier import folder_kind as _folder_kind_ja
from classifier import kind_label as _kind_label_ja
from i18n import L, N_, T


# 分類処理（classifier.py）の文は日本語のまま持ち、表示するときに今の言語へ（i18n.L）
def kind_label(kind: str) -> str:
    return L(_kind_label_ja(kind))


def folder_kind(ctx, path, dominant):
    k, label, certainty, reasons = _folder_kind_ja(ctx, path, dominant)
    return k, L(label), certainty, [L(r) for r in reasons]


def folder_cautions(ctx, dominant):
    return [L(c) for c in _folder_cautions_ja(ctx, dominant)]

APP_NAME = "PC Management Center"
APP_VERSION = "0.11"
MIN_RECT_PIXELS = 3
# 一覧に一度に描画する最大行数。絞り込み・並べ替えは全件に対して行い、表示だけを上位に限定する。
FILE_DISPLAY_LIMIT = 2000
FOLDER_DISPLAY_LIMIT = 3000

CERTAINTY_TEXT = {
    "sure": N_('確度: 高 — 拡張子・ファイル名・保存場所に明確な手がかりがあります'),
    "likely": N_('確度: 推定 — 手がかりからの推測で、断定はできません'),
    "unknown": N_('確度: 不明 — 中身を特定できる手がかりがありません'),
}


@dataclass(slots=True)
class FileItem:
    name: str
    dir: str
    size: int
    modified: float
    kind: str
    group: str | None
    attrs: int = -1     # スキャン時の Windows のファイル属性（-1 は不明）。重複検出で開く前の除外に使う

    @property
    def path(self) -> Path:
        return Path(self.dir, self.name)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# --- 見た目の共通設定 -----------------------------------------------------------
# 落ち着いた配色。文字は背景とのコントラストを優先する。
P = {
    "bg": "#f4f5f7", "surface": "#ffffff", "border": "#d6dae0", "text": "#1f2328",
    "muted": "#5c6672", "faint": "#8a939e", "accent": "#2d6aa8", "accent_dark": "#23578c",
    "select": "#d5e3f3", "stripe": "#f6f8fa", "heading": "#eceff3", "hover": "#e7ecf2",
    "chip": "#e6eef7", "chip_border": "#c5d4e6",
    "map_a": "#dde7f3", "map_b": "#e8eef6", "map_files": "#eceef1",
    "checked": "#fbf0db", "warn": "#9a3412",
    # 「移動」チェックを付けた行（濃い青・白い文字）と、チェックしたフォルダの中の行（薄い青）。
    # 行の選択（明るい水色・黒い文字）と見分けられる色にする。
    "check_row": "#1f4e8c", "check_row_text": "#ffffff", "covered_row": "#c9d9ee",
    # チェックした行を選んでいるとき（選択の色とどちらが優先されても、濃い文字で読める明るさ）
    "check_row_selected": "#b7cdea",
}
UI_SCALE = 1.0  # 96dpi 基準の倍率。App の初期化時に実際の DPI から決まる。


def px(n: float) -> int:
    """96dpi 基準のピクセル値を現在の表示倍率に合わせる。"""
    return int(round(n * UI_SCALE))


def enable_dpi_awareness():
    """表示倍率を上げた環境でぼやけないよう、Windows に DPI 対応を宣言する。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def setup_style(root: tk.Tk):
    global UI_SCALE
    UI_SCALE = max(1.0, root.winfo_fpixels("1i") / 96)

    families = set(tkfont.families(root))
    family = next((f for f in ("Yu Gothic UI", "Meiryo UI", "Segoe UI") if f in families), None)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
        f = tkfont.nametofont(name)
        if family:
            f.configure(family=family)
        f.configure(size=10, weight="normal")
    base = tkfont.nametofont("TkDefaultFont").actual()
    fonts = {
        "bold": tkfont.Font(root, family=base["family"], size=10, weight="bold"),
        "title": tkfont.Font(root, family=base["family"], size=13, weight="bold"),
        "section": tkfont.Font(root, family=base["family"], size=11, weight="bold"),
        "large": tkfont.Font(root, family=base["family"], size=14, weight="bold"),
        "small": tkfont.Font(root, family=base["family"], size=9),
    }
    root.fonts = fonts

    root.configure(background=P["bg"])
    root.option_add("*Toplevel.background", P["bg"])
    root.option_add("*TCombobox*Listbox.background", P["surface"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["text"])
    root.option_add("*Menu.background", P["surface"])
    root.option_add("*Menu.activeBackground", P["select"])
    root.option_add("*Menu.activeForeground", P["text"])

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=P["bg"], foreground=P["text"], bordercolor=P["border"],
                lightcolor=P["bg"], darkcolor=P["bg"], troughcolor=P["heading"], focuscolor=P["accent"])
    s.configure("TFrame", background=P["bg"])
    s.configure("Card.TFrame", background=P["surface"])
    s.configure("TLabel", background=P["bg"], foreground=P["text"])
    s.configure("Muted.TLabel", foreground=P["muted"])
    s.configure("Title.TLabel", font=fonts["title"])
    s.configure("Strong.TLabel", font=fonts["bold"])
    s.configure("Section.TLabel", font=fonts["section"])
    s.configure("Large.TLabel", font=fonts["large"])
    s.configure("TButton", padding=(px(12), px(4)), background=P["surface"], bordercolor=P["border"],
                lightcolor=P["surface"], darkcolor=P["surface"])
    s.map("TButton",
          background=[("disabled", P["bg"]), ("pressed", P["heading"]), ("active", P["hover"])],
          foreground=[("disabled", P["faint"])],
          lightcolor=[("active", P["hover"])], darkcolor=[("active", P["hover"])])
    s.configure("Accent.TButton", background=P["accent"], foreground="#ffffff", bordercolor=P["accent"],
                lightcolor=P["accent"], darkcolor=P["accent"], font=fonts["bold"])
    s.map("Accent.TButton",
          background=[("disabled", "#a9bfd8"), ("pressed", P["accent_dark"]), ("active", P["accent_dark"])],
          foreground=[("disabled", "#ffffff")],
          lightcolor=[("active", P["accent_dark"])], darkcolor=[("active", P["accent_dark"])],
          bordercolor=[("disabled", "#a9bfd8")])
    s.configure("Chip.TButton", padding=(px(8), px(1)), background=P["chip"], bordercolor=P["chip_border"],
                lightcolor=P["chip"], darkcolor=P["chip"])
    s.map("Chip.TButton", background=[("active", P["select"])])
    s.configure("TMenubutton", padding=(px(12), px(4)), background=P["surface"], bordercolor=P["border"],
                lightcolor=P["surface"], darkcolor=P["surface"], arrowcolor=P["muted"])
    s.map("TMenubutton", background=[("active", P["hover"])])
    s.configure("Warn.TLabel", foreground=P["warn"], font=fonts["bold"])
    s.configure("TCheckbutton", background=P["bg"])
    s.map("TCheckbutton", background=[("active", P["bg"])])
    for w in ("TEntry", "TCombobox"):
        s.configure(w, fieldbackground=P["surface"], bordercolor=P["border"], lightcolor=P["surface"],
                    darkcolor=P["surface"], padding=(px(6), px(3)), arrowcolor=P["muted"])
    s.map("TCombobox", fieldbackground=[("readonly", P["surface"])],
          selectbackground=[("readonly", P["surface"])], selectforeground=[("readonly", P["text"])])
    s.configure("Treeview", background=P["surface"], fieldbackground=P["surface"], foreground=P["text"],
                rowheight=px(26), bordercolor=P["border"], lightcolor=P["border"], darkcolor=P["border"])
    s.map("Treeview", background=[("selected", P["select"])], foreground=[("selected", P["text"])])
    s.configure("Treeview.Heading", background=P["heading"], foreground=P["text"], font=fonts["bold"],
                padding=(px(6), px(4)), relief="flat", bordercolor=P["border"],
                lightcolor=P["heading"], darkcolor=P["heading"])
    s.map("Treeview.Heading", background=[("active", P["hover"])])
    s.configure("TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", padding=(px(18), px(6)), background=P["bg"], foreground=P["muted"],
                bordercolor=P["border"], lightcolor=P["bg"])
    s.map("TNotebook.Tab", background=[("selected", P["surface"]), ("active", P["hover"])],
          foreground=[("selected", P["text"])], font=[("selected", fonts["bold"])],
          lightcolor=[("selected", P["surface"])])
    s.configure("Horizontal.TProgressbar", troughcolor=P["heading"], background=P["accent"],
                bordercolor=P["heading"], lightcolor=P["accent"], darkcolor=P["accent"], thickness=px(6))
    s.configure("TScrollbar", background="#d3d8de", troughcolor=P["bg"], bordercolor=P["bg"],
                arrowcolor=P["muted"], lightcolor="#d3d8de", darkcolor="#d3d8de")
    s.configure("TSeparator", background=P["border"])
    return fonts


WRAP_START = 700   # 折り返す説明文の最初の幅（最小サイズのウィンドウに収まる幅。96dpi 基準）


def fit_wrap(root):
    """折り返す説明文を、置かれた場所の幅に合わせて折り返すようにする。

    折り返し幅を固定すると、ウィンドウを狭めたときに右端が切れる（最小サイズでも切れないように）。
    """
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if not isinstance(w, ttk.Label):
            continue
        try:
            wrap = int(str(w.cget("wraplength")) or 0)
        except (tk.TclError, ValueError):
            continue
        if wrap <= px(WRAP_START):
            continue
        w.configure(wraplength=px(WRAP_START))
        if w.winfo_manager() == "pack":
            w.pack_configure(fill="x")
        w.bind("<Configure>", lambda e, lbl=w: lbl.configure(wraplength=max(px(200), e.width - px(4))), add="+")


def elide_middle(text: str, font: tkfont.Font, width: int) -> str:
    """幅に収まらない文字列を中央で省略する（パスの先頭と末尾のファイル名を残す）。"""
    if width <= 0 or font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        head = mid // 2
        cand = text[:head] + "…" + text[len(text) - (mid - head):]
        if font.measure(cand) <= width:
            lo = mid
        else:
            hi = mid - 1
    head = lo // 2
    return text[:head] + "…" + (text[len(text) - (lo - head):] if lo - head else "")


class ElidedLabel(ttk.Label):
    """長いパスを表示幅に合わせて中央省略するラベル。幅が変わると表示し直す。"""

    def __init__(self, master, text: str = "", font=None, **kw):
        super().__init__(master, width=1, font=font, **kw)
        self._full = text
        self._font = font if isinstance(font, tkfont.Font) else tkfont.nametofont("TkDefaultFont")
        self.bind("<Configure>", lambda _e: self._refit())
        self._refit()

    def set(self, text: str):
        self._full = text
        self._refit()

    def get(self) -> str:
        return self._full

    def _refit(self):
        self.configure(text=elide_middle(self._full, self._font, self.winfo_width() - 4))


def open_in_file_manager(path: Path):
    try:
        target = str(path if path.is_dir() else path.parent)
        if sys.platform.startswith("win"):
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as exc:
        messagebox.showerror(APP_NAME, T('フォルダを開けませんでした。\n{0}', exc))


def subtree_prefix(folder: str) -> str:
    return folder if folder.endswith(os.sep) else folder + os.sep


def ancestor_context(root: Path) -> FolderContext | None:
    """スキャン対象より上のフォルダの判定材料（プロジェクト内の一部だけを選んだ場合など）。"""
    ctx = None
    for p in reversed(root.parents):
        dirs, files = [], []
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        (dirs if e.is_dir() else files).append(e.name)
                    except OSError:
                        pass
        except OSError:
            pass
        ctx = folder_context(str(p), dirs, files, ctx)
    return ctx


EXCLUDED_DIR_NAMES = {"$RECYCLE.BIN", "System Volume Information"}


class Scanner(threading.Thread):
    """選択フォルダ以下を走査し、ファイル一覧・フォルダ集計・分類を作る。

    速度のため次の 2 点に注意している（計測: 19.4 万ファイルで約 28 秒 → 下記で短縮）。
    - ファイルの容量・更新日時は os.scandir が列挙時に取得した値（DirEntry.stat）を使い、
      ファイルごとに開き直さない。
    - フォルダ容量は各フォルダ直下の合計だけを集め、走査後に一度だけ親へ積み上げる。
    辿り方は従来の os.walk(followlinks=False) と同じ。加えてジャンクションも辿らない（二重計上・循環の防止）。
    """

    PROGRESS_INTERVAL = 0.15  # 進捗を送る間隔（秒）

    def __init__(self, root: Path, out_q: queue.Queue, cancel_event: threading.Event):
        super().__init__(daemon=True)
        self.root = root
        self.out_q = out_q
        self.cancel_event = cancel_event

    def run(self):
        files: list[FileItem] = []
        direct_size: dict[str, int] = {}
        direct_count: dict[str, int] = {}
        folder_ctx: dict[str, FolderContext] = {}
        walked: list[str] = []          # 走査した順（親が必ず子より先）
        listed_only: list[str] = []     # 一覧には出るが中へは入らないフォルダ（リンク・読めない）
        skipped = {"file_errors": 0, "dir_errors": 0, "links": 0, "excluded": 0}
        total_size = 0
        cancel = self.cancel_event
        next_progress = time.monotonic() + self.PROGRESS_INTERVAL

        root_s = str(self.root)
        root_parent_ctx = ancestor_context(self.root)
        stack: list[tuple[str, FolderContext | None]] = [(root_s, root_parent_ctx)]

        while stack:
            if cancel.is_set():
                self.out_q.put(("cancelled", None))
                return
            current, parent_ctx = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                # 権限がない・途中で消えた等。フォルダ自体は親の一覧に残る（容量 0）。
                skipped["dir_errors"] += 1
                if current != root_s:
                    listed_only.append(current)
                continue

            dir_entries = []
            file_entries = []
            for e in entries:
                try:
                    is_dir = e.is_dir()
                except OSError:
                    is_dir = False
                if not is_dir:
                    file_entries.append(e)
                elif e.name in EXCLUDED_DIR_NAMES:
                    skipped["excluded"] += 1
                else:
                    dir_entries.append(e)

            names = [e.name for e in file_entries]
            ctx = folder_context(current, [e.name for e in dir_entries], names, parent_ctx)
            folder_ctx[current] = ctx
            walked.append(current)
            siblings = {n.lower() for n in names}

            size_sum = 0
            count = 0
            for i, e in enumerate(file_entries):
                if i % 2000 == 1999 and cancel.is_set():
                    self.out_q.put(("cancelled", None))
                    return
                try:
                    st = e.stat()  # シンボリックリンクはリンク先（従来の os.stat と同じ）
                    if not stat.S_ISREG(st.st_mode):
                        continue
                except OSError:
                    skipped["file_errors"] += 1
                    continue
                size = st.st_size
                c = classify_file(e.name, size, ctx, siblings)
                # 属性は列挙時に取れている値をそのまま持つ（追加の読み取りはしない）。
                # リンクはリンク先の属性になるので、印を足しておく。
                attrs = getattr(st, "st_file_attributes", -1)
                if attrs >= 0 and e.is_symlink():
                    attrs |= 0x400
                files.append(FileItem(e.name, current, size, st.st_mtime, c.kind, c.group_key, attrs))
                size_sum += size
                count += 1
            direct_size[current] = size_sum
            direct_count[current] = count
            total_size += size_sum

            # os.walk と同じ順で辿るため逆順に積む。リンク・ジャンクションのフォルダは中へ入らない
            # （二重計上や、親を指すジャンクションでの際限ない走査を防ぐ）。
            for e in reversed(dir_entries):
                path = os.path.join(current, e.name)
                try:
                    is_link = e.is_symlink() or e.is_junction()
                except OSError:
                    is_link = False
                if is_link:
                    skipped["links"] += 1
                    listed_only.append(path)
                else:
                    stack.append((path, ctx))

            now = time.monotonic()
            if now >= next_progress:
                next_progress = now + self.PROGRESS_INTERVAL
                self.out_q.put(("progress", (len(files), total_size, current)))

        # 直下の合計を、子から親へ一度だけ積み上げる（走査順の逆 = 子が先）
        agg_size = dict(direct_size)
        agg_count = dict(direct_count)
        for d in reversed(walked):
            if d == root_s:
                continue
            parent = os.path.dirname(d)
            agg_size[parent] = agg_size.get(parent, 0) + agg_size[d]
            agg_count[parent] = agg_count.get(parent, 0) + agg_count[d]

        folder_sizes: dict[Path, int] = {}
        folder_file_counts: dict[Path, int] = {}
        direct_file_sizes: dict[Path, int] = {}
        for d in walked + listed_only:
            p = Path(d)
            folder_sizes[p] = agg_size.get(d, 0)
            folder_file_counts[p] = agg_count.get(d, 0)
            direct_file_sizes[p] = direct_size.get(d, 0)

        result = {
            "files": files,
            "folder_sizes": folder_sizes,
            "folder_file_counts": folder_file_counts,
            "direct_file_sizes": direct_file_sizes,
            "folder_ctx": folder_ctx,
            "root_parent_ctx": root_parent_ctx,
            "total_size": total_size,
            "errors": skipped["file_errors"],
            "skipped": skipped,
        }
        self.out_q.put(("done", result))


class DupScanner(threading.Thread):
    """重複検出を別スレッドで回す。

    **通常の容量スキャンとは別物**で、押されたときだけ走る。
    スキャン済みの一覧（ディスクは読み直さない）を入力に取り、中身だけを確かめる。
    """

    PROGRESS_INTERVAL = 0.2
    PARTIAL_INTERVAL = 0.5   # 途中結果を画面へ渡す間隔（秒）

    def __init__(self, items: list[FileItem], out_q: queue.Queue, cancel_event: threading.Event,
                 min_size: int = 1):
        super().__init__(daemon=True)
        self.items = items
        self.out_q = out_q
        self.cancel_event = cancel_event
        self.min_size = min_size

    def run(self):
        next_progress = time.monotonic() + self.PROGRESS_INTERVAL
        batch: list = []
        next_flush = time.monotonic() + self.PARTIAL_INTERVAL

        def progress(phase, done, total, groups=0, reclaimable=0):
            nonlocal next_progress
            now = time.monotonic()
            if now >= next_progress:
                next_progress = now + self.PROGRESS_INTERVAL
                self.out_q.put(("dup_progress", (phase, done, total, groups, reclaimable)))

        def on_groups(new):
            # 確定した組を少しずつまとめて渡す（1 組ずつ送ると画面の更新が追いつかない）
            nonlocal next_flush
            batch.extend(new)
            now = time.monotonic()
            if now >= next_flush:
                next_flush = now + self.PARTIAL_INTERVAL
                self.out_q.put(("dup_partial", list(batch)))
                batch.clear()

        # FileItem をそのまま鍵に持たせる。結果から元の行へ戻れるようにするため。
        # スキャン時の属性も渡し、リンク・クラウドのみのファイルを開く前に外す。
        payload = [duplicates.DupFile(str(i.path), i.size, i.modified, key=i,
                                      attrs=i.attrs if i.attrs >= 0 else None) for i in self.items]
        result = duplicates.find_duplicates(payload, cancel=self.cancel_event, progress=progress,
                                            min_size=self.min_size, on_groups=on_groups)
        if batch:
            self.out_q.put(("dup_partial", list(batch)))
        self.out_q.put(("dup_cancelled" if result.cancelled else "dup_done", result))


def squarify(values: list[float], x: float, y: float, w: float, h: float) -> list[tuple[float, float, float, float]]:
    """面積が値に比例する四角を、なるべく正方形に近くなるように敷き詰める（squarified treemap）。

    values は大きい順に並べて渡す。返り値は同じ順の (x, y, 幅, 高さ)。
    帯を一方向に並べるだけだと、項目が多いと細い線になって選べなくなるため。
    """
    total = sum(values)
    if total <= 0 or w <= 0 or h <= 0:
        return [(x, y, 0.0, 0.0) for _ in values]
    scale = w * h / total
    areas = [v * scale for v in values]
    rects: list[tuple[float, float, float, float]] = []

    def worst(row_sum: float, row_max: float, row_min: float, side: float) -> float:
        if row_sum <= 0 or row_min <= 0:
            return float("inf")
        s2, side2 = row_sum * row_sum, side * side
        return max(side2 * row_max / s2, s2 / (side2 * row_min))

    i = 0
    n = len(areas)
    while i < n:
        side = min(w, h)
        row_sum, row_max, row_min = areas[i], areas[i], areas[i]
        j = i + 1
        current = worst(row_sum, row_max, row_min, side)
        while j < n:
            a = areas[j]
            cand = worst(row_sum + a, max(row_max, a), min(row_min, a), side)
            if cand > current:
                break
            row_sum, row_max, row_min, current = row_sum + a, max(row_max, a), min(row_min, a), cand
            j += 1
        if w >= h:        # 左端に縦 1 列
            cw = row_sum / h if h else 0.0
            cy = y
            for a in areas[i:j]:
                rh = a / cw if cw else 0.0
                rects.append((x, cy, cw, rh))
                cy += rh
            x += cw
            w -= cw
        else:             # 上端に横 1 行
            rh = row_sum / w if w else 0.0
            cx = x
            for a in areas[i:j]:
                rw = a / rh if rh else 0.0
                rects.append((cx, y, rw, rh))
                cx += rw
            y += rh
            h -= rh
        i = j
    return rects


class Treemap(ttk.Frame):
    """容量マップ。スキャン対象のルートから、配下のフォルダへ順にたどる。

    - 四角をクリックでそのフォルダへ。「← 戻る」「↑ 親フォルダ」と階層表示（クリックで移動）で戻れる
    - 右の一覧には直下の全項目を容量順に出す（地図では小さすぎて選べない項目もここから開ける）
    - スキャン済みの索引だけを使う（たどるたびにディスクを読み直さない）
    """

    MAX_RECTS = 300      # 地図に描く四角の上限。残りは「ほか N 件」にまとめ、一覧から開く
    LIST_LIMIT = 3000    # 一覧に出す上限

    def __init__(self, master, on_open_folder, on_detail=None, on_direct_files=None, describe=None,
                 on_trash=None, on_reveal=None, on_files=None, on_navigate=None):
        super().__init__(master)
        self.on_trash = on_trash
        self.on_open_folder = on_open_folder
        self.on_detail = on_detail
        self.on_direct_files = on_direct_files
        self.on_reveal = on_reveal          # フォルダ一覧でその場所を開く
        self.on_files = on_files            # ファイル一覧をその場所（配下すべて）で絞る
        self.on_navigate = on_navigate      # 場所が変わったことを知らせる
        self.describe = describe
        fonts = self.winfo_toplevel().fonts

        # 1 行目: 戻る・親へ・階層表示（どの階層もクリックで移動）
        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(0, px(4)))
        self.back_btn = ttk.Button(nav, text=T('← 戻る'), command=self.go_back, state="disabled")
        self.back_btn.pack(side="left")
        self.up_btn = ttk.Button(nav, text=T('↑ 親フォルダ'), command=self.go_up, state="disabled")
        self.up_btn.pack(side="left", padx=(px(6), 0))
        self.detail_btn = ttk.Button(nav, text=T('詳細を表示'), state="disabled",
                                     command=lambda: self.on_detail and self.on_detail(self.current_root))
        self.detail_btn.pack(side="right")
        self.crumbs = ttk.Frame(nav)
        self.crumbs.pack(side="left", fill="x", expand=True, padx=(px(10), 0))

        # 2 行目: 今どこの容量を見ているか、親・全体との関係
        self.info = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.info, style="Strong.TLabel").pack(anchor="w", pady=(0, px(6)))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        self.canvas = tk.Canvas(left, highlightthickness=1, highlightbackground=P["border"],
                                background=P["surface"], borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        ttk.Label(left, text=T('クリックで中へ ・ 右クリックでメニュー ・ Backspace で戻る'),
                  style="Muted.TLabel").pack(anchor="w", pady=(px(4), 0))
        body.add(left, weight=3)

        right = ttk.Frame(body)
        ttk.Label(right, text=T('この場所の中身（容量順）'), style="Strong.TLabel").pack(anchor="w")
        lw = ttk.Frame(right)
        lw.pack(fill="both", expand=True, pady=(px(4), 0))
        self.list = ttk.Treeview(lw, columns=("size", "share"), show="tree headings", selectmode="browse")
        self.list.heading("#0", text=T('名前'), anchor="w")
        self.list.heading("size", text=T('容量'), anchor="e")
        self.list.heading("share", text=T('割合'), anchor="e")
        self.list.column("#0", width=px(170), stretch=True)
        self.list.column("size", width=px(80), anchor="e", stretch=False)
        self.list.column("share", width=px(56), anchor="e", stretch=False)
        sy = ttk.Scrollbar(lw, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sy.set)
        self.list.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.list_note = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.list_note, style="Muted.TLabel", wraplength=px(300),
                  justify="left").pack(anchor="w", pady=(px(4), 0))
        body.add(right, weight=1)

        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Button-3>", self._context_menu)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        self.list.bind("<Double-1>", lambda _e: self._open_list_row())
        self.list.bind("<Return>", lambda _e: self._open_list_row())
        self.list.bind("<Button-3>", self._list_menu)
        self.list.bind("<<TreeviewSelect>>", lambda _e: self._highlight_list_row())
        for w in (self.canvas, self.list):
            w.bind("<BackSpace>", lambda _e: self.go_back())
            w.bind("<Alt-Up>", lambda _e: self.go_up())
        self.name_font = fonts["bold"]
        self.info_font = tkfont.nametofont("TkDefaultFont")

        self.folder_sizes: dict[Path, int] = {}
        self.folder_file_counts: dict[Path, int] = {}
        self.direct_file_sizes: dict[Path, int] = {}
        self.child_dirs: dict[Path, list[Path]] = {}
        self.scan_root: Path | None = None
        self.current_root: Path | None = None
        self.history: list[Path] = []
        self.rect_targets: dict[int, object] = {}   # 描いた図形 -> Path / None（直下のファイル）/ "more"
        self._rect_of: dict[int, int] = {}
        self._rect_by_target: dict[object, int] = {}
        self._list_targets: dict[str, object] = {}
        self._hover_rect: int | None = None
        self._selected_rect: int | None = None

    # --- データ・移動 ---
    def set_data(self, scan_root: Path, folder_sizes, direct_file_sizes, folder_file_counts=None):
        self.scan_root = scan_root
        self.current_root = scan_root
        self.history = []
        self.folder_sizes = folder_sizes
        self.direct_file_sizes = direct_file_sizes
        self.folder_file_counts = folder_file_counts if folder_file_counts is not None else {}
        # 子フォルダの索引を一度だけ作る（描画のたびに全フォルダを走査しない）。
        children = defaultdict(list)
        for p in folder_sizes.keys():
            if p != scan_root:
                children[p.parent].append(p)
        self.child_dirs = children
        self._update_nav()
        self.redraw()

    def navigate(self, folder: Path, remember: bool = True):
        """その場所を表示する（スキャン済みの範囲内ならどこへでも。読み直しはしない）。"""
        if folder not in self.folder_sizes or folder == self.current_root:
            return
        if remember and self.current_root is not None:
            self.history.append(self.current_root)
            del self.history[:-50]
        self.current_root = folder
        self._update_nav()
        self.redraw()
        if self.on_navigate:
            self.on_navigate(folder)

    def go_back(self):
        while self.history:
            prev = self.history.pop()
            if prev in self.folder_sizes:     # 消えた場所（ごみ箱へ移した等）は飛ばす
                self.navigate(prev, remember=False)
                return
        self._update_nav()

    def go_up(self):
        if not self.current_root or not self.scan_root or self.current_root == self.scan_root:
            return
        parent = self.current_root.parent
        self.navigate(parent if parent in self.folder_sizes else self.scan_root)

    def _update_nav(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        if not self.current_root:
            self.info.set("")
            for b in (self.back_btn, self.up_btn, self.detail_btn):
                b.config(state="disabled")
            self._fill_list()
            return
        self.back_btn.config(state="normal" if any(p in self.folder_sizes for p in self.history) else "disabled")
        self.up_btn.config(state="normal" if self.current_root != self.scan_root else "disabled")
        self.detail_btn.config(state="normal")

        # 階層表示: スキャン対象のルートから今の場所まで（長いときは途中を … にまとめる）
        chain = [self.current_root]
        cur = self.current_root
        while cur != self.scan_root and cur.parent != cur:
            cur = cur.parent
            chain.append(cur)
        chain.reverse()
        shown = chain if len(chain) <= 6 else chain[:1] + [None] + chain[-4:]
        hidden = chain[1:-4] if len(chain) > 6 else []
        for i, p in enumerate(shown):
            if i:
                ttk.Label(self.crumbs, text="›", style="Muted.TLabel").pack(side="left", padx=px(2))
            if p is None:
                mb = ttk.Menubutton(self.crumbs, text="…")
                m = tk.Menu(mb, tearoff=0)
                for h in hidden:
                    m.add_command(label=h.name or str(h), command=lambda q=h: self.navigate(q))
                mb.configure(menu=m)
                mb.pack(side="left")
                continue
            label = (p.name or str(p)) if p != self.scan_root else str(p)
            if p == self.current_root:
                ttk.Label(self.crumbs, text=label, style="Strong.TLabel").pack(side="left")
            else:
                ttk.Button(self.crumbs, text=label, style="Chip.TButton",
                           command=lambda q=p: self.navigate(q)).pack(side="left")

        # 容量の関係: この場所 / 親フォルダ / スキャン対象全体
        size = self.folder_sizes.get(self.current_root, 0)
        count = self.folder_file_counts.get(self.current_root)
        parts = [T('表示中: {0} ・ {1}', self.current_root.name or self.current_root, human_size(size))
                 + (T('（ファイル {0:,} 件）', count) if count is not None else "")]
        if self.current_root != self.scan_root:
            parent = self.current_root.parent
            psize = self.folder_sizes.get(parent, 0)
            if psize:
                parts.append(T('親フォルダ「{0}」（{1}）の {2:.0%}', parent.name or parent, human_size(psize), size / psize))
            total = self.folder_sizes.get(self.scan_root, 0)
            if total:
                parts.append(T('スキャン対象全体の {0:.1%}', size / total))
        else:
            parts.append(T('スキャン対象のルート'))
        self.info.set(T('  ／  ').join(parts))
        self._fill_list()

    # --- 中身 ---
    def _children(self):
        """(表示名, 容量, 対象)。対象は Path（フォルダ）か None（この場所の直下のファイル）。"""
        if not self.current_root:
            return []
        items = [(p.name, self.folder_sizes.get(p, 0), p) for p in self.child_dirs.get(self.current_root, [])]
        direct = self.direct_file_sizes.get(self.current_root, 0)
        if direct > 0:
            items.append((T('このフォルダ直下のファイル'), direct, None))
        items.sort(key=lambda x: x[1], reverse=True)
        return items

    def _fill_list(self):
        self.list.delete(*self.list.get_children())
        self._list_targets.clear()
        items = self._children()
        total = self.folder_sizes.get(self.current_root, 0) if self.current_root else 0
        for n, (label, size, target) in enumerate(items[:self.LIST_LIMIT]):
            iid = f"i{n}"
            text = ("📁 " if target is not None else "📄 ") + label
            share = f"{size / total:.0%}" if total else ""
            self.list.insert("", "end", iid=iid, text=text, values=(human_size(size), share))
            self._list_targets[iid] = target
        extra = []
        if len(items) > self.LIST_LIMIT:
            extra.append(T('容量の大きい {0:,} 件を表示（全 {1:,} 件）。', self.LIST_LIMIT, len(items)))
        if len(items) > self.MAX_RECTS:
            extra.append(T('地図には大きい {0} 件だけを描き、残りは「ほか」にまとめています。', self.MAX_RECTS))
        extra.append(T('ダブルクリック・Enter で中へ（直下のファイルはファイル一覧で表示）。'))
        self.list_note.set(" ".join(extra) if items else T('中身はありません。'))

    def _open_list_row(self):
        sel = self.list.selection()
        if not sel:
            return
        target = self._list_targets.get(sel[0])
        if target is None:
            if self.on_direct_files and self.current_root is not None:
                self.on_direct_files(self.current_root)
        else:
            self.navigate(target)

    def _highlight_list_row(self):
        sel = self.list.selection()
        target = self._list_targets.get(sel[0]) if sel else "none"
        rid = self._rect_by_target.get(target)
        if self._selected_rect is not None and self._selected_rect != self._hover_rect:
            self.canvas.itemconfigure(self._selected_rect, outline=P["surface"], width=2)
        self._selected_rect = rid
        if rid is not None:
            self.canvas.itemconfigure(rid, outline=P["accent_dark"], width=3)
            self.canvas.tag_raise(rid)
            for tid, r in self._rect_of.items():
                if r == rid:
                    self.canvas.tag_raise(tid)

    # --- 描画 ---
    def redraw(self):
        self.canvas.delete("all")
        self.rect_targets.clear()
        self._rect_of.clear()
        self._rect_by_target.clear()
        self._hover_rect = None
        self._selected_rect = None
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        if not self.current_root:
            self.canvas.create_text(w / 2, h / 2, text=T('スキャンすると、フォルダごとの容量がここに表示されます'),
                                    fill=P["muted"], anchor="center")
            return
        pad = px(6)
        x0, y0, x1, y1 = pad, pad, w - pad, h - pad
        if x1 <= x0 or y1 <= y0:
            return
        items = [it for it in self._children() if it[1] > 0]
        total = sum(size for _, size, _ in items)
        if total <= 0:
            self.canvas.create_text(w / 2, h / 2, text=T('表示できるファイルがありません'),
                                    fill=P["muted"], anchor="center")
            return
        if len(items) > self.MAX_RECTS:
            rest = items[self.MAX_RECTS - 1:]
            items = items[:self.MAX_RECTS - 1] + [(T('ほか {0:,} 件（右の一覧から選べます）', len(rest)),
                                                   sum(s for _, s, _ in rest), "more")]
        rects = squarify([s for _, s, _ in items], x0, y0, x1 - x0, y1 - y0)
        line = self.info_font.metrics("linespace")
        hidden = 0
        for idx, ((label, size, target), (rx, ry, rw, rh)) in enumerate(zip(items, rects)):
            if rw < MIN_RECT_PIXELS or rh < MIN_RECT_PIXELS:
                hidden += 1
                continue
            if target is None:
                fill = P["map_files"]
            elif target == "more":
                fill = P["heading"]
            else:
                fill = P["map_a"] if idx % 2 == 0 else P["map_b"]
            rid = self.canvas.create_rectangle(rx, ry, rx + rw, ry + rh, fill=fill, outline=P["surface"], width=2)
            self.rect_targets[rid] = target
            self._rect_by_target[target] = rid
            inner = rw - px(16)
            if rw > px(70) and rh > line * 2 + px(10):
                share = size / total
                ids = [self.canvas.create_text(rx + px(8), ry + px(5), anchor="nw", font=self.name_font,
                                               fill=P["text"], text=elide_middle(label, self.name_font, inner)),
                       self.canvas.create_text(rx + px(8), ry + px(5) + line, anchor="nw", font=self.info_font,
                                               fill=P["muted"], text=T('{0} ・ {1:.0%}', human_size(size), share))]
                note = self.describe(target) if (self.describe and isinstance(target, Path)) else None
                if note and rh > line * 3 + px(10):
                    ids.append(self.canvas.create_text(rx + px(8), ry + px(5) + line * 2, anchor="nw",
                                                       font=self.info_font, fill=P["muted"],
                                                       text=elide_middle(note, self.info_font, inner)))
                for tid in ids:
                    self.rect_targets[tid] = target
                    self._rect_of[tid] = rid
        if hidden:
            self.list_note.set(self.list_note.get() + T(' 小さすぎて地図に描けない {0:,} 件も一覧から開けます。', hidden))

    # --- 操作 ---
    def _hit(self, event):
        ids = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
        for iid in reversed(ids):
            if iid in self.rect_targets:
                return True, self.rect_targets[iid]
        return False, None

    def _hover(self, event):
        rid = None
        for iid in reversed(self.canvas.find_overlapping(event.x, event.y, event.x, event.y)):
            if iid in self.rect_targets:
                rid = self._rect_of.get(iid, iid)
                break
        self._set_hover(rid)

    def _set_hover(self, rid):
        if rid == self._hover_rect:
            return
        if self._hover_rect is not None and self._hover_rect != self._selected_rect:
            self.canvas.itemconfigure(self._hover_rect, outline=P["surface"], width=2)
        self._hover_rect = rid
        if rid is not None:
            self.canvas.itemconfigure(rid, outline=P["accent"], width=2)
            self.canvas.tag_raise(rid)
            for tid, r in self._rect_of.items():
                if r == rid:
                    self.canvas.tag_raise(tid)
        target = self.rect_targets.get(rid) if rid is not None else None
        self.canvas.configure(cursor="hand2" if isinstance(target, Path) else "")

    def _click(self, event):
        self.canvas.focus_set()
        hit, target = self._hit(event)
        if not hit:
            return
        if isinstance(target, Path) and target in self.folder_sizes:
            self.navigate(target)
        elif target == "more":
            # まとめた残りは、一覧の最初の該当行を選んで見せる
            rows = list(self._list_targets)
            if len(rows) >= self.MAX_RECTS:
                row = rows[self.MAX_RECTS - 1]
                self.list.selection_set(row)
                self.list.see(row)
                self.list.focus_set()
        elif target is None:
            # 直下のファイルは、一覧の行を選んで見せる（ダブルクリックでファイル一覧へ）
            for iid, t in self._list_targets.items():
                if t is None:
                    self.list.selection_set(iid)
                    self.list.see(iid)

    def _menu_for(self, target, event):
        menu = tk.Menu(self, tearoff=0)
        if target is None:
            folder = self.current_root
            menu.add_command(label=T('直下のファイルをファイル一覧で表示'),
                             command=lambda: self.on_direct_files and self.on_direct_files(folder))
            menu.add_command(label=T('この場所の詳細を表示'),
                             command=lambda: self.on_detail and self.on_detail(folder))
        elif isinstance(target, Path):
            menu.add_command(label=T('中へ移動'), command=lambda: self.navigate(target))
            menu.add_command(label=T('詳細を表示'), command=lambda: self.on_detail and self.on_detail(target))
            if self.on_reveal:
                menu.add_command(label=T('フォルダ一覧で表示'), command=lambda: self.on_reveal(target))
            if self.on_files:
                menu.add_command(label=T('ファイル一覧でこの場所を見る'), command=lambda: self.on_files(target))
            menu.add_command(label=T('エクスプローラーで開く'), command=lambda: self.on_open_folder(target))
            if self.on_trash:
                menu.add_separator()
                menu.add_command(label=T('ごみ箱へ移動…'), command=lambda: self.on_trash(target))
        else:
            return
        menu.tk_popup(event.x_root, event.y_root)

    def _context_menu(self, event):
        hit, target = self._hit(event)
        if hit:
            self._menu_for(target, event)

    def _list_menu(self, event):
        row = self.list.identify_row(event.y)
        if not row:
            return
        self.list.selection_set(row)
        self._menu_for(self._list_targets.get(row), event)


class DetailWindow(tk.Toplevel):
    """ファイル・フォルダの詳細。スキャン済みの索引だけを使い、ディスクは読まない。

    読む順番: これは何か → なぜそう判断したか → 整理する際の注意。
    """

    def __init__(self, app: "App"):
        super().__init__(app)
        self.withdraw()          # 表示は _present() から（前面に出す操作を 1 か所にまとめる）
        self.app = app
        fonts = app.fonts
        self.title(T('詳細 — ') + APP_NAME)
        width = min(px(720), self.winfo_screenwidth() - px(40))
        height = min(px(760), self.winfo_screenheight() - px(100))
        self.geometry(f"{width}x{height}")
        self.minsize(px(560), px(420))
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self._wrapped: list[ttk.Label] = []

        # 操作ボタンは常に見えるよう下部に固定し、情報部分だけをスクロールさせる。
        footer = ttk.Frame(self, padding=(px(20), px(10), px(20), px(14)))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Label(footer, text=T('関連するデータを一覧で表示'), style="Muted.TLabel").pack(anchor="w", pady=(0, px(4)))
        self.btn_kind = ttk.Button(footer)
        self.btn_group = ttk.Button(footer)
        self.btn_folder = ttk.Button(footer)
        for b in (self.btn_kind, self.btn_group, self.btn_folder):
            b.pack(fill="x", pady=px(2))
        bottom = ttk.Frame(footer)
        bottom.pack(fill="x", pady=(px(10), 0))
        self.btn_map = ttk.Button(bottom, text=T('容量マップで表示'))
        self.btn_map.pack(side="left")
        self.btn_explorer = ttk.Button(bottom, text=T('エクスプローラーで開く'))
        self.btn_explorer.pack(side="left", padx=(px(8), 0))
        ttk.Button(bottom, text=T('閉じる'), command=self.withdraw).pack(side="right")
        self.btn_trash = ttk.Button(bottom, text=T('ごみ箱へ移動'))
        self.current: tuple[str, object] | None = None  # ("file", FileItem) / ("folder", Path) / ("removed", FileItem)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0, background=P["bg"], borderwidth=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(self.canvas, padding=(px(20), px(16), px(20), px(12)))
        self._outer_id = self.canvas.create_window(0, 0, window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        # 見出し: 名前と、容量・日時などの要点を 1 行で
        self.title_var = tk.StringVar()
        self.subtitle_var = tk.StringVar()
        self._wrap(ttk.Label(outer, textvariable=self.title_var, style="Large.TLabel")).pack(anchor="w", fill="x")
        self._wrap(ttk.Label(outer, textvariable=self.subtitle_var, style="Muted.TLabel")).pack(anchor="w", fill="x", pady=(px(2), px(6)))
        self.path_entry = ttk.Entry(outer)
        self.path_entry.pack(fill="x")

        # 1. これは何か
        self._section(outer, T('これは何か'))
        self.kind_var = tk.StringVar()
        self.certainty_var = tk.StringVar()
        self.possibility_var = tk.StringVar()
        self.purpose_var = tk.StringVar()
        self._wrap(ttk.Label(outer, textvariable=self.kind_var, style="Title.TLabel")).pack(anchor="w", fill="x")
        self._wrap(ttk.Label(outer, textvariable=self.certainty_var, style="Muted.TLabel")).pack(anchor="w", fill="x")
        self.possibility_box = ttk.Frame(outer)
        self.possibility_box.pack(fill="x")
        self.possibility_label = self._wrap(ttk.Label(self.possibility_box, textvariable=self.possibility_var))
        self._wrap(ttk.Label(outer, textvariable=self.purpose_var)).pack(anchor="w", fill="x", pady=(px(8), px(8)))
        self.identity = ttk.Frame(outer)
        self.identity.pack(fill="x")
        self.identity.columnconfigure(1, weight=1)

        # フォルダのみ: 中身の内訳
        self.breakdown_title = ttk.Label(outer, text=T('中身の内訳（容量順）'), style="Strong.TLabel")
        self.breakdown = ttk.Frame(outer)
        self.breakdown.columnconfigure(0, weight=1)

        # 2. なぜそう判断したか
        self.reason_head = self._section(outer, T('なぜそう判断したか'))
        self.reason_var = tk.StringVar()
        self._wrap(ttk.Label(outer, textvariable=self.reason_var, justify="left")).pack(anchor="w", fill="x")

        # 3. 整理する際の注意
        self._section(outer, T('整理する際の注意'))
        self.caution_var = tk.StringVar()
        self._wrap(ttk.Label(outer, textvariable=self.caution_var, justify="left")).pack(anchor="w", fill="x")
        self._wrap(ttk.Label(outer, style="Muted.TLabel", justify="left",
                             text=T('この画面は判断の材料です。安全に削除できるかどうかは断定できません。このアプリは削除を行いません。'))).pack(anchor="w", fill="x", pady=(px(8), 0))

    # --- 表示ヘルパー ---
    def _section(self, parent, text):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(px(18), px(6)))
        ttk.Label(frame, text=text, style="Section.TLabel").pack(side="left")
        ttk.Separator(frame, orient="horizontal").pack(side="left", fill="x", expand=True, padx=(px(10), 0))
        return frame

    def _wrap(self, label):
        self._wrapped.append(label)
        return label

    def _on_resize(self, event):
        self.canvas.itemconfigure(self._outer_id, width=event.width)
        wrap = max(px(200), event.width - px(48))
        for lbl in self._wrapped:
            lbl.configure(wraplength=wrap)
        for lbl in self.identity.grid_slaves(column=1):
            lbl.configure(wraplength=max(px(160), wrap - px(140)))

    def _fill_facts(self, rows):
        for w in self.identity.winfo_children():
            w.destroy()
        wrap = max(px(160), self.canvas.winfo_width() - px(190))
        for r, (label, value) in enumerate(rows):
            ttk.Label(self.identity, text=label, style="Muted.TLabel").grid(
                row=r, column=0, sticky="nw", padx=(0, px(16)), pady=px(2))
            ttk.Label(self.identity, text=value, justify="left", wraplength=wrap).grid(
                row=r, column=1, sticky="w", pady=px(2))

    def _set_path(self, text):
        self.path_entry.configure(state="normal")
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, text)
        self.path_entry.configure(state="readonly")

    def _set_reasons(self, reasons):
        self.reason_var.set("\n".join(T('・{0}', r) for r in reasons) or T('・特記事項なし'))

    def _set_possibility(self, text):
        self.possibility_var.set(f"※ {text}" if text else "")
        if text:
            self.possibility_label.pack(anchor="w", fill="x", pady=(px(4), 0))
        else:
            self.possibility_label.pack_forget()

    def _set_button(self, btn, text, command):
        btn.configure(text=text, command=command or (lambda: None), state="normal" if command else "disabled")

    def _present(self):
        self.canvas.yview_moveto(0)
        self.app.raise_window(self)

    # --- ファイル ---
    def show_file(self, item: FileItem, present: bool = True):
        app = self.app
        self.current = ("file", item)
        c = classify_file(item.name, item.size, app.ctx_for(item.dir), app.sibling_names(item.dir))
        self.title_var.set(item.name)
        self.subtitle_var.set(T('ファイル  ·  {0}（{1:,} バイト）  ·  更新 {2}  ·  拡張子 {3}', human_size(item.size), item.size, fmt_date(item.modified), item.ext or T('なし')))
        self._set_path(str(item.path))

        self.kind_var.set(L(c.kind_display))
        self.certainty_var.set(T(CERTAINTY_TEXT[c.certainty]))
        self._set_possibility(L(c.possibility) if c.possibility else None)
        self.purpose_var.set(L(c.purpose))
        self._fill_facts([
            (T('関連'), L(c.group_label) if c.group_label else T('特定できません')),
            (T('形式'), L(c.format)),
            (T('保存場所'), L(c.location) if c.location else "—"),
        ])
        self.breakdown_title.pack_forget()
        self.breakdown.pack_forget()
        self._set_reasons([L(r) for r in c.reasons])
        self.caution_var.set(self._caution_text([L(x) for x in c.cautions]))

        kl = T('種類不明') if c.kind == "unknown" else kind_label(c.kind)
        self._set_button(self.btn_kind, T('同じ種類を表示（{0}）', kl),
                         lambda: app.apply_filter("kind", c.kind))
        if c.group_key:
            self._set_button(self.btn_group, T('同じアプリ・プロジェクトを表示（{0}）', L(c.group_label)),
                             lambda: app.apply_filter("group", c.group_key))
        else:
            self._set_button(self.btn_group, T('同じアプリ・プロジェクトを表示（特定できません）'), None)
        self._set_button(self.btn_folder, T('同じフォルダを表示（{0}・直下）', os.path.basename(item.dir) or item.dir),
                         lambda: app.apply_filter("folder", (item.dir, False)))
        folder = Path(item.dir)
        self.btn_map.configure(command=lambda: app.show_in_map(folder),
                               state="normal" if folder in app.folder_sizes else "disabled")
        self.btn_explorer.configure(command=lambda: open_in_file_manager(item.path), state="normal")
        self._set_trash_button(app.protected_reason(str(item.path), False), lambda: app.trash_file(item))
        if present:
            self._present()

    def _set_trash_button(self, protected: str | None, command):
        self.btn_trash.pack(side="right", padx=(0, px(8)))
        if protected:
            self.btn_trash.configure(state="disabled", command=lambda: None)
            self.caution_var.set(self.caution_var.get() +
                                 T('\n・この場所は{0}ため、このアプリからはごみ箱へ移動できません。', protected.removesuffix(T('ため'))))
        else:
            self.btn_trash.configure(state="normal", command=command)

    def show_removed(self, target):
        """ごみ箱へ移したファイル・フォルダ（またはその中にあったもの）。存在するものとして表示し続けない。"""
        self.current = ("removed", target)
        is_folder = isinstance(target, Path)
        path = str(target) if is_folder else str(target.path)
        self.title_var.set(target.name or path)
        self.subtitle_var.set(T('ごみ箱へ移動済み'))
        self._set_path(path)
        self.kind_var.set(T('このフォルダ（中身を含む）はごみ箱へ移動しました') if is_folder
                          else T('このファイルはごみ箱へ移動しました（またはごみ箱へ移したフォルダの中にありました）'))
        self.certainty_var.set("")
        self._set_possibility(None)
        self.purpose_var.set(T('元の場所にはもうありません。Windows のごみ箱から元に戻せます。一覧・件数・容量マップからは除きました。'))
        self._fill_facts([])
        self.breakdown_title.pack_forget()
        self.breakdown.pack_forget()
        self._set_reasons([])
        self.reason_var.set("")
        self.caution_var.set("")
        for b in (self.btn_kind, self.btn_group, self.btn_folder):
            self._set_button(b, b.cget("text"), None)
        for b in (self.btn_map, self.btn_explorer, self.btn_trash):
            b.configure(state="disabled")

    def refresh(self):
        """一覧が変わったとき、表示中の内容を最新にする（前面には出さない）。"""
        if not self.current or not self.winfo_exists():
            return
        kind, target = self.current
        if kind == "file":
            self.show_file(target, present=False)
        elif kind == "folder":
            self.show_folder(target, present=False)

    # --- フォルダ ---
    def show_folder(self, folder: Path, present: bool = True):
        app = self.app
        self.current = ("folder", folder)
        fs = str(folder)
        ctx = app.ctx_for(fs)
        stats = app.folder_breakdown(fs)
        total = sum(s for s, _ in stats.values())
        dominant = max(stats, key=lambda k: stats[k][0]) if stats else None
        if dominant and total and stats[dominant][0] / total < 0.5:
            dominant_for_label = None
        else:
            dominant_for_label = dominant
        fkind, flabel, certainty, reasons = folder_kind(ctx, fs, dominant_for_label)
        latest = app.folder_latest(fs)

        subdirs = len(app.treemap.child_dirs.get(folder, []))
        self.title_var.set(folder.name or fs)
        meta = [T('フォルダ'), f"{human_size(app.folder_sizes.get(folder, 0))}",
                T('{0:,} ファイル', app.folder_file_counts.get(folder, 0)), T('サブフォルダ {0:,}', subdirs)]
        if latest:
            meta.append(T('最終更新 {0}', fmt_date(latest)))
        self.subtitle_var.set("  ·  ".join(meta))
        self._set_path(fs)

        if dominant and total:
            share = stats[dominant][0] / total * 100
            reasons = list(reasons) + [T('配下のファイル容量のうち {0:.0f}% が「{1}」', share, kind_label(dominant))]
        if ctx.group_label and ctx.group_key != fs and ctx.group_reasons:
            reasons.append(T('関連: {0}', L(ctx.group_reasons[0])))
        if ctx.location:
            reasons.append(T('保存場所: {0}', L(ctx.location)))

        purpose = {
            "game": T('ゲームのインストール先、またはゲームが使うデータの保存先と考えられます。'),
            "project": T('ソフトウェア開発のプロジェクト（ソースコード・設定・ツールのデータ）です。'),
            "app": T('アプリの本体、またはアプリが設定・キャッシュ・履歴などを保存する場所と考えられます。'),
            "system": T('Windows が動作のために使う場所です。'),
        }.get(ctx.group_type or "", T('特定のアプリに属する手がかりは見つかりませんでした。中身の内訳を参考にしてください。'))
        if ctx.container:
            purpose = L(KINDS[fkind][1]) if fkind in KINDS else purpose

        self.kind_var.set(flabel)
        self.certainty_var.set(T(CERTAINTY_TEXT[certainty]))
        self._set_possibility(None)
        self.purpose_var.set(purpose)
        self._fill_facts([
            (T('関連'), L(ctx.group_label) if ctx.group_label else T('特定できません')),
            (T('保存場所'), L(ctx.location) if ctx.location else "—"),
        ])

        for w in self.breakdown.winfo_children():
            w.destroy()
        if stats:
            ordered = sorted(stats.items(), key=lambda kv: kv[1][0], reverse=True)[:8]
            for r, (k, (size, count)) in enumerate(ordered):
                pct = size / total * 100 if total else 0
                ttk.Label(self.breakdown, text=kind_label(k)).grid(row=r, column=0, sticky="w", pady=px(1))
                ttk.Label(self.breakdown, text=human_size(size)).grid(row=r, column=1, sticky="e", padx=(px(12), 0))
                ttk.Label(self.breakdown, text=f"{pct:.0f}%", style="Muted.TLabel", width=5, anchor="e").grid(
                    row=r, column=2, sticky="e", padx=(px(8), 0))
                ttk.Label(self.breakdown, text=T('{0:,} 件', count), style="Muted.TLabel").grid(
                    row=r, column=3, sticky="e", padx=(px(12), px(12)))
                ttk.Button(self.breakdown, text=T('表示'),
                           command=lambda kk=k: app.apply_filter_many({"kind": kk, "folder": (fs, True)})
                           ).grid(row=r, column=4, sticky="e", pady=px(1))
        else:
            ttk.Label(self.breakdown, text=T('ファイルがありません'), style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.breakdown_title.pack(anchor="w", pady=(px(12), px(4)), before=self.reason_head)
        self.breakdown.pack(fill="x", before=self.reason_head)

        self._set_reasons(reasons)
        self.caution_var.set(self._caution_text(folder_cautions(ctx, dominant_for_label)))

        if fkind:
            self._set_button(self.btn_kind, T('同じ種類を表示（{0}・スキャン範囲全体）', kind_label(fkind)),
                             lambda: app.apply_filter("kind", fkind))
        else:
            self._set_button(self.btn_kind, T('同じ種類を表示（中身が混在しているため特定できません）'), None)
        if ctx.group_key:
            self._set_button(self.btn_group, T('同じアプリ・プロジェクトを表示（{0}）', L(ctx.group_label)),
                             lambda: app.apply_filter("group", ctx.group_key))
        else:
            self._set_button(self.btn_group, T('同じアプリ・プロジェクトを表示（特定できません）'), None)
        self._set_button(self.btn_folder, T('同じフォルダを表示（{0}・配下すべて）', folder.name or fs),
                         lambda: app.apply_filter("folder", (fs, True)))
        self.btn_map.configure(command=lambda: app.show_in_map(folder),
                               state="normal" if folder in app.folder_sizes else "disabled")
        self.btn_explorer.configure(command=lambda: open_in_file_manager(folder), state="normal")
        self._set_trash_button(app.protected_reason(fs, True), lambda: app.trash_folder(folder))
        if present:
            self._present()

    @staticmethod
    def _caution_text(cautions):
        return "\n".join(T('・{0}', c) for c in dict.fromkeys(cautions))


class TrashConfirmDialog(tk.Toplevel):
    """「ごみ箱へ移動」の確認。「移動する」を押したときだけ True を返す。

    spec: name / path / is_folder / facts [(見出し, 値)] / cautions / warnings / risky
    既定のボタン（Enter）はキャンセル。注意を強める場合や、スキャン後の変化がある場合は、
    確認のチェックを入れるまで「移動する」を押せない。
    """

    def __init__(self, app: "App", spec: dict):
        super().__init__(app)
        self.result = False
        is_folder = spec["is_folder"]
        self.title(T('ごみ箱へ移動 — ') + APP_NAME)
        self.transient(app)
        self.resizable(True, False)
        self.minsize(px(520), 1)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        wrap = px(560)

        frame = ttk.Frame(self, padding=(px(20), px(16)))
        frame.pack(fill="both", expand=True)
        head = (T('次のフォルダを、中身ごと Windows のごみ箱へ移動します') if is_folder
                else T('次のファイルを Windows のごみ箱へ移動します'))
        ttk.Label(frame, text=head, style="Section.TLabel").pack(anchor="w")
        if is_folder:
            ttk.Label(frame, style="Strong.TLabel", wraplength=wrap, justify="left",
                      text=T('フォルダ内のすべてのファイルとサブフォルダも一緒に移動します（フォルダ全体の移動）。')
                      ).pack(anchor="w", pady=(px(4), 0))
        ttk.Label(frame, text=T('ごみ箱からいつでも元に戻せます。完全には削除しません。'),
                  style="Muted.TLabel").pack(anchor="w", pady=(px(2), px(12)))

        self.facts = ttk.Frame(frame)
        self.facts.pack(fill="x")
        self.facts.columnconfigure(1, weight=1)
        rows = [(T('フォルダ名') if is_folder else T('ファイル名'), spec["name"]), (T('フルパス'), None)] + list(spec["facts"])
        for r, (label, value) in enumerate(rows):
            ttk.Label(self.facts, text=label, style="Muted.TLabel").grid(
                row=r, column=0, sticky="nw", padx=(0, px(16)), pady=px(2))
            if value is None:
                e = ttk.Entry(self.facts)
                e.insert(0, spec["path"])
                e.configure(state="readonly")
                e.grid(row=r, column=1, sticky="ew", pady=px(2))
            else:
                ttk.Label(self.facts, text=value, wraplength=wrap, justify="left",
                          style="Strong.TLabel" if r == 0 else "TLabel").grid(row=r, column=1, sticky="w", pady=px(2))

        warnings = list(spec.get("warnings") or [])
        if warnings:
            ttk.Label(frame, text="\n".join(T('・{0}', w) for w in warnings), style="Strong.TLabel",
                      wraplength=wrap + px(80), justify="left").pack(anchor="w", fill="x", pady=(px(10), 0))

        ttk.Label(frame, text=T('移動する前に'), style="Strong.TLabel").pack(anchor="w", pady=(px(14), px(4)))
        lines = list(dict.fromkeys(spec["cautions"]))
        lines.append(T('分類は推測です。この画面の情報だけでは、移動しても問題ないとは断定できません。'))
        ttk.Label(frame, text="\n".join(T('・{0}', c) for c in lines), wraplength=wrap + px(80),
                  justify="left").pack(anchor="w", fill="x")

        need_check = spec["risky"] or bool(warnings)
        self.confirmed = tk.BooleanVar(value=not need_check)
        if need_check:
            if spec["risky"]:
                ttk.Label(frame, wraplength=wrap + px(80), justify="left", style="Strong.TLabel",
                          text=T('Windows やアプリの動作に関わる可能性がある場所・種類です。移動するとアプリが正しく動かなくなることがあります。')).pack(anchor="w", pady=(px(10), px(4)))
            ttk.Checkbutton(frame, text=T('内容を確認し、移動して問題ないと自分で判断しました'),
                            variable=self.confirmed, command=self._sync).pack(anchor="w", pady=(px(6), 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(px(16), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.move_btn = ttk.Button(buttons, text=T('移動する'), command=self.accept)
        self.move_btn.pack(side="right", padx=(0, px(8)))
        self._sync()
        self.bind("<Escape>", lambda _e: self.cancel())
        self.bind("<Return>", lambda _e: self.cancel() if self.focus_get() is not self.move_btn else self.accept())

    def _sync(self):
        self.move_btn.configure(state="normal" if self.confirmed.get() else "disabled")

    def accept(self):
        if not self.confirmed.get():
            return
        self.result = True
        self.destroy()

    def cancel(self):
        self.result = False
        self.destroy()

    def ask(self) -> bool:
        self.cancel_btn.focus_set()
        self.grab_set()
        self.wait_window()
        return self.result


class MultiTrashConfirmDialog(tk.Toplevel):
    """複数のファイル・フォルダをごみ箱へ移す前の確認。

    1 件用と分けているのは、**全件を目で確かめられること**が要るからである。
    件数と合計容量だけ出して「はい」を押させると、何を消すのか分からないまま消せてしまう。
    一覧はスクロールでき、移せないもの（保護対象・スキャン後に変わったもの）は
    理由つきで分けて出す。on_exclude を渡すと、行を選んで「移動から外す」ができる
    （movable の一覧そのものから取り除き、呼び手にも知らせる）。
    """

    def __init__(self, app: "App", movable: list[tuple[object, str]], blocked: list[tuple[object, str]],
                 note: str | None = None, on_exclude=None, warn: str | None = None):
        """movable / blocked は (FileItem または FolderTarget, 種類・理由)。"""
        super().__init__(app)
        self.withdraw()        # ask() まで出さない
        self.result = False
        self.app = app
        self.movable = movable
        self.on_exclude = on_exclude
        self.title(T('ごみ箱へ移動 — ') + APP_NAME)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.minsize(px(600), px(420))
        wrap = px(620)

        frame = ttk.Frame(self, padding=(px(20), px(16)))
        frame.pack(fill="both", expand=True)
        self.head = tk.StringVar()
        self.dirs_note = tk.StringVar()
        self.total_note = tk.StringVar()
        ttk.Label(frame, textvariable=self.head, style="Section.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=self.dirs_note, style="Strong.TLabel", wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(px(4), 0))
        ttk.Label(frame, textvariable=self.total_note, style="Strong.TLabel").pack(anchor="w", pady=(px(2), 0))
        if warn:
            ttk.Label(frame, text=warn, style="Warn.TLabel", wraplength=wrap + px(80),
                      justify="left").pack(anchor="w", pady=(px(4), 0))
        if note:
            ttk.Label(frame, text=note, style="Muted.TLabel", wraplength=wrap + px(80),
                      justify="left").pack(anchor="w", pady=(px(2), 0))
        ttk.Label(frame, text=T('ごみ箱から元に戻せます（このアプリの「ごみ箱から復元」タブからも）。完全には削除しません。'),
                  style="Muted.TLabel").pack(anchor="w", pady=(px(2), px(10)))

        # 移すもの（全件。スクロールして確かめられる）
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=T('移動するもの'), style="Strong.TLabel").pack(side="left")
        if on_exclude is not None:
            ttk.Button(top, text=T('選んだ行を移動から外す'), command=self.exclude_selected).pack(side="right")
            ttk.Label(top, text=T('Ctrl+クリックで離れた行、Shift+クリックで範囲を選べます'),
                      style="Muted.TLabel").pack(side="right", padx=px(8))
        list_wrap = ttk.Frame(frame)
        list_wrap.pack(fill="both", expand=True, pady=(px(4), px(8)))
        cols = ("size", "kind", "path")
        self.tree = tree = ttk.Treeview(list_wrap, columns=cols, show="tree headings", height=8,
                                        selectmode="extended" if on_exclude is not None else "none")
        tree.heading("#0", text=T('名前'), anchor="w")
        tree.column("#0", width=px(220), stretch=False)
        for col, title, width, anchor2 in (("size", T('容量'), 90, "e"), ("kind", T('種類'), 200, "w"),
                                           ("path", T('場所'), 320, "w")):
            tree.heading(col, text=title, anchor=anchor2)
            tree.column(col, width=px(width), anchor=anchor2, stretch=(col == "path"))
        sy = ttk.Scrollbar(list_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sy.set)
        tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self._row_obj: dict[str, object] = {}
        for i, (item, kind_text) in enumerate(movable):
            iid = tree.insert("", "end", text=("📁 " if isinstance(item, FolderTarget) else "") + item.name,
                              tags=("odd",) if i % 2 else (),
                              values=(human_size(item.size), kind_text, item.dir))
            self._row_obj[iid] = item
        tree.tag_configure("odd", background=P["stripe"])
        self.note = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.note, style="Muted.TLabel").pack(anchor="w")

        if blocked:
            ttk.Label(frame, text=T('移動しないもの（{0:,} 件）', len(blocked)), style="Strong.TLabel"
                      ).pack(anchor="w", pady=(px(6), px(2)))
            lines = [T('・{0} — {1}', item.name, why) for item, why in blocked[:8]]
            if len(blocked) > 8:
                lines.append(T('・ほか {0:,} 件', len(blocked) - 8))
            ttk.Label(frame, text="\n".join(lines), wraplength=wrap + px(80), justify="left",
                      style="Muted.TLabel").pack(anchor="w")

        ttk.Label(frame, text=T('移動する前に'), style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(4)))
        self.risky = any(app.item_risky(item) for item, _ in movable)
        cautions = [T('分類は推測です。この画面の情報だけでは、移動しても問題ないとは断定できません。'),
                    T('同じ中身でも、置かれている場所によって用途が違うことがあります。')]
        if self.risky:
            cautions.insert(0, T('Windows やアプリの動作に関わる可能性がある場所・種類が含まれています。'))
        ttk.Label(frame, text="\n".join(T('・{0}', c) for c in cautions), wraplength=wrap + px(80),
                  justify="left").pack(anchor="w", fill="x")

        # 複数件はまとめて動くので、常に自分の目での確認を求める
        self.confirmed = tk.BooleanVar(value=False)
        self.confirm_chk = ttk.Checkbutton(frame, variable=self.confirmed, command=self._sync)
        self.confirm_chk.pack(anchor="w", pady=(px(10), 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(px(14), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.move_btn = ttk.Button(buttons, command=self.accept)
        self.move_btn.pack(side="right", padx=(0, px(8)))
        self._refresh_counts()
        self.bind("<Escape>", lambda _e: self.cancel())

    def _refresh_counts(self):
        mv = self.movable
        total = sum(i.size for i, _ in mv)
        n_dirs = sum(1 for i, _ in mv if isinstance(i, FolderTarget))
        self.head.set(T('次の {0:,} 件を Windows のごみ箱へ移動します', len(mv)))
        self.dirs_note.set(T('うち {0:,} 件はフォルダです。フォルダは中のファイル・サブフォルダごと移動します。', n_dirs)
                           if n_dirs else "")
        self.total_note.set(T('合計 {0}（{1:,} バイト）', human_size(total), total))
        self.confirm_chk.configure(text=T('上の {0:,} 件を確認し、移動して問題ないと自分で判断しました', len(mv)))
        self.move_btn.configure(text=T('{0:,} 件を移動する', len(mv)))
        self.confirmed.set(False)          # 対象が変わったら確認もやり直す
        self._sync()

    def exclude_selected(self):
        sel = [i for i in self.tree.selection() if i in self._row_obj]
        if not sel:
            self.note.set(T('移動から外す行を選んでください。'))
            return
        objs = [self._row_obj.pop(i) for i in sel]
        gone = {id(o) for o in objs}
        self.movable[:] = [(o, k) for o, k in self.movable if id(o) not in gone]
        self.tree.delete(*sel)
        if self.on_exclude is not None:
            self.on_exclude(objs)
        self.note.set(T('{0:,} 件を移動から外しました（チェックも外れています）。', len(objs)))
        self._refresh_counts()

    def _sync(self):
        ok = self.confirmed.get() and bool(self.movable)
        self.move_btn.configure(state="normal" if ok else "disabled")

    def accept(self):
        if not (self.confirmed.get() and self.movable):
            return
        self.result = True
        self.destroy()

    def cancel(self):
        self.result = False
        self.destroy()

    def ask(self) -> bool:
        self.deiconify()
        self.cancel_btn.focus_set()
        self.grab_set()
        self.wait_window()
        return self.result


class TrashResultDialog(tk.Toplevel):
    """まとめて移した結果を、対象ごとに出す（移動した／移動しなかった／未実行）。"""

    def __init__(self, app: "App", title: str, summary: str, rows: list[tuple[str, str, str]]):
        super().__init__(app)
        self.withdraw()
        self.title(f"{title} — {APP_NAME}")
        self.transient(app)
        self.minsize(px(640), px(360))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        f = ttk.Frame(self, padding=(px(20), px(16)))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=title, style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, text=summary, justify="left", wraplength=px(640)).pack(anchor="w", pady=(px(4), px(8)))
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("result", "place"), show="tree headings", height=12)
        self.tree.heading("#0", text=T('名前'), anchor="w")
        self.tree.heading("result", text=T('結果'), anchor="w")
        self.tree.heading("place", text=T('場所'), anchor="w")
        self.tree.column("#0", width=px(200), stretch=False)
        self.tree.column("result", width=px(300), stretch=False)
        self.tree.column("place", width=px(300), stretch=True)
        sy = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.tree.tag_configure("ng", foreground=P["warn"])
        # 移動しなかったもの・未実行のものを先に出す（見落とさないように）
        def ok(r):
            return r.startswith((T('移動した'), T('元に戻した')))
        order = sorted(rows, key=lambda r: 1 if ok(r[2]) else 0)
        for name, place, result in order:
            self.tree.insert("", "end", text=name, values=(result, place), tags=() if ok(result) else ("ng",))
        ttk.Button(f, text=T('閉じる'), command=self.destroy).pack(anchor="e", pady=(px(10), 0))

    def show(self):
        self.deiconify()
        self.grab_set()
        self.wait_window()


@dataclass
class FolderTarget:
    """複数選択でまとめて移すフォルダ。確認画面の一覧に FileItem と並べて出す。"""
    path: Path
    size: int                        # 数え直した現在の容量
    measure: "trash.FolderMeasure"   # 確認前に数え直した中身（直前にもう一度数えて比べる）
    kind_text: str
    risky: bool

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    @property
    def dir(self) -> str:
        return str(self.path.parent)


class _ModalDialog(tk.Toplevel):
    """確認用の小窓の共通部分。ask() を呼ぶまで画面に出さない（テストでは出さずに中身を確かめる）。"""

    def __init__(self, app: "App", title: str):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self.result = False
        self.title(f"{title} — {APP_NAME}")
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())
        self.frame = ttk.Frame(self, padding=(px(20), px(16)))
        self.frame.pack(fill="both", expand=True)
        self.first_focus = None

    def lines(self, parent, items, style="TLabel", wrap=None, pady=(0, 0)):
        lbl = ttk.Label(parent, text="\n".join(T('・{0}', t) for t in items), style=style,
                        wraplength=wrap or px(640), justify="left")
        lbl.pack(anchor="w", fill="x", pady=pady)
        return lbl

    def accept(self):
        self.result = True
        self.destroy()

    def cancel(self):
        self.result = False
        self.destroy()

    def ask(self) -> bool:
        self.deiconify()
        if self.first_focus is not None:
            self.first_focus.focus_set()
        self.grab_set()
        self.wait_window()
        return self.result


class RestoreConfirmDialog(_ModalDialog):
    """復元の前に、対象・復元先・戻せない理由と対処を見せる。既定はキャンセル。"""

    GUIDE = {
        "exists": N_('元の場所に同じ名前の項目があります。上書きはしません。今ある方の名前を変えるか別の場所へ移してから、もう一度復元してください。'),
        "no_parent": N_('元のフォルダがありません。別の場所へは戻しません。元のフォルダを作り直す（そのフォルダもこの一覧にあれば先に復元する）と戻せます。'),
        "gone": N_('ごみ箱に見つかりません。Windows のごみ箱で元に戻した・削除した可能性があります。元の場所か Windows のごみ箱を確かめてください。'),
        "unresolved": N_('移したときにごみ箱の中の項目を特定できなかったため、このアプリからは戻せません。Windows のごみ箱から「元に戻す」を使ってください。'),
        "restored": N_('すでに元の場所へ戻しています。'),
        "error": N_('戻せませんでした。'),
    }

    def __init__(self, app: "App", rows: list[tuple]):
        """rows: (記録, 問題 RestoreError | None, 補足)。"""
        super().__init__(app, T('ごみ箱から復元'))
        self.geometry(f"{min(px(900), self.winfo_screenwidth() - px(40))}x{min(px(560), self.winfo_screenheight() - px(80))}")
        self.minsize(px(640), px(400))
        f = self.frame
        ok = [r for r in rows if r[1] is None]
        ng = [r for r in rows if r[1] is not None]
        ttk.Label(f, text=T('次の {0:,} 件を元の場所へ戻します', len(ok)), style="Section.TLabel").pack(anchor="w")
        if ng:
            ttk.Label(f, text=T('戻せない {0:,} 件は戻しません（理由と対処を下に表示）', len(ng)), style="Strong.TLabel"
                      ).pack(anchor="w", pady=(px(2), 0))
        ttk.Label(f, style="Muted.TLabel", wraplength=px(840), justify="left",
                  text=T('元の場所に同じ名前のものがあるときは戻しません（上書きしません）。元のフォルダが無いときも、別の場所へは戻しません。戻したあとは、スキャン結果が古くなります。')).pack(anchor="w", pady=(px(4), px(8)))

        buttons = ttk.Frame(f)
        buttons.pack(side="bottom", fill="x", pady=(px(12), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.ok_btn = ttk.Button(buttons, text=T('{0:,} 件を元の場所へ戻す', len(ok)), command=self.accept,
                                 state="normal" if ok else "disabled")
        self.ok_btn.pack(side="right", padx=(0, px(8)))
        codes = sorted({r[1].code for r in ng})
        if codes:
            self.lines(f, [T(self.GUIDE.get(c, "")) for c in codes], style="Muted.TLabel", wrap=px(840),
                       pady=(px(6), 0)).pack_configure(side="bottom")

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=("check", "dest"), show="tree headings", selectmode="none")
        tree.heading("#0", text=T('名前'), anchor="w")
        tree.heading("check", text=T('確認の結果'), anchor="w")
        tree.heading("dest", text=T('復元先（元の場所）'), anchor="w")
        tree.column("#0", width=px(200), stretch=False)
        tree.column("check", width=px(300), stretch=False)
        tree.column("dest", width=px(320), stretch=True)
        sy = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sy.set)
        tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        tree.tag_configure("ng", foreground=P["warn"])
        for entry, problem, note in ok + ng:
            text = (T('戻せます') + (T('（{0}）', note) if note else "")) if problem is None else T('戻せません: {0}', problem)
            tree.insert("", "end", text=("📁 " if entry.is_dir else "") + entry.name,
                        values=(text, entry.orig_path), tags=() if problem is None else ("ng",))
        self.first_focus = self.cancel_btn


class LanguageDialog(tk.Toplevel):
    """初回起動時に言語を選ぶ小さな画面。両方の言語で書く（まだどちらか分からないため）。"""

    def __init__(self, app: "App", current: str = "ja"):
        super().__init__(app)
        self.withdraw()
        self.title("Language / 言語 — " + APP_NAME)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.accept)
        self.lang = tk.StringVar(value=current)
        self.dont_ask = tk.BooleanVar(value=False)
        f = ttk.Frame(self, padding=(px(24), px(18)))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="表示する言語を選んでください\nChoose the display language",
                  style="Section.TLabel", justify="left").pack(anchor="w", pady=(0, px(10)))
        for code, label in (("ja", "日本語"), ("en", "English")):
            ttk.Radiobutton(f, text=label, value=code, variable=self.lang).pack(anchor="w", pady=px(2))
        ttk.Checkbutton(f, text="次回から表示しない / Don't show this again",
                        variable=self.dont_ask).pack(anchor="w", pady=(px(12), 0))
        ttk.Label(f, text="あとから「設定 → 言語」で変更できます。\nYou can change it later in Settings → Language.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(px(8), 0))
        self.ok_btn = ttk.Button(f, text="OK", style="Accent.TButton", command=self.accept)
        self.ok_btn.pack(anchor="e", pady=(px(14), 0))
        self.bind("<Return>", lambda _e: self.accept())

    def accept(self):
        self.destroy()

    def ask(self) -> tuple[str, bool]:
        self.deiconify()
        self.ok_btn.focus_set()
        self.grab_set()
        self.wait_window()
        return self.lang.get(), self.dont_ask.get()


class YesNoDialog(_ModalDialog):
    """はい／いいえの確認（Windows 標準の画面は OS の言語で出るため、アプリの言語でそろえる）。既定は「いいえ」。"""

    def __init__(self, app: "App", title: str, text: str):
        super().__init__(app, title)
        f = self.frame
        ttk.Label(f, text=text, wraplength=px(520), justify="left").pack(anchor="w")
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(px(16), 0))
        self.no_btn = ttk.Button(buttons, text=T('いいえ'), command=self.cancel)
        self.no_btn.pack(side="right")
        ttk.Button(buttons, text=T('はい'), command=self.accept).pack(side="right", padx=(0, px(8)))
        self.first_focus = self.no_btn


class LowRiskDialog(_ModalDialog):
    """「低リスク候補を選ぶ」の結果を見せる。ここではチェックを付けるだけで、移動はしない。"""

    CRITERIA = [
        N_('保存場所が ダウンロード・デスクトップ・ドキュメント・ピクチャ・ビデオ・ミュージック の中（クラウド同期フォルダを除く）'),
        N_('開発プロジェクト・依存パッケージ・変更履歴・アプリ・ゲーム・システム・キャッシュ・ログ・セーブデータ・バックアップの領域ではない'),
        N_('途中のフォルダ名に版の違いを思わせる言葉（v2・1.0・old・旧・最新 など）がない'),
        N_('拡張子から 写真・動画・音楽・書類（PDF・Office など）・圧縮ファイル・インストーラー と確定できる'),
        N_('各組で最低 1 件は残す（条件に合わないファイルがある組では、そちらを残す）'),
    ]
    LIMITS = [
        N_('判定材料はフォルダ名・拡張子・保存場所・フォルダ内の目印（package.json など）だけで、ファイルの中身や、アプリがどこを読みに行くかは調べていません。'),
        N_('目印のないプロジェクトの素材や、アプリ・スクリプトが決まった場所で使っているファイルは見分けられません。'),
        N_('削除しても問題ないという判定ではありません。移動前の一覧で、場所と残す側を確かめてください。'),
    ]

    def __init__(self, app: "App", info: dict):
        super().__init__(app, T('低リスク候補を選ぶ'))
        self.minsize(px(600), 1)
        f = self.frame
        wrap = px(660)
        ttk.Label(f, text=T('低リスク候補を選ぶ'), style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, style="Strong.TLabel", wraplength=wrap, justify="left",
                  text=T('「削除しても問題ない」ものではありません。用途を推測できる材料がそろい、推測が外れにくいものだけを機械的に選びます。')).pack(anchor="w", pady=(px(4), px(10)))

        ttk.Label(f, text=T('結果'), style="Strong.TLabel").pack(anchor="w")
        res = [T('候補（チェックを付けるもの）: {0:,} 件 ・ 移動予定 {1}（{2:,} 組）', info['count'], human_size(info['size']), info['groups'])]
        if info.get("kept_ok"):
            res.append(T('条件に合うが、組に 1 件は残すため残す側にしたもの: {0:,} 件', info['kept_ok']))
        res.append(T('除外して「個別確認」に回すもの: {0:,} 件 ・ {1}', info['excluded_count'], human_size(info['excluded_size'])))
        self.lines(f, res, wrap=wrap, pady=(px(2), px(4)))
        if info["excluded"]:
            ttk.Label(f, text=T('除外の理由（件数の多い順）'), style="Muted.TLabel").pack(anchor="w", pady=(px(4), 0))
            self.lines(f, [T('{0}: {1:,} 件', label, n) for label, n in info["excluded"]], style="Muted.TLabel",
                       wrap=wrap)

        ttk.Label(f, text=T('選ぶ条件（すべて満たすものだけ）'), style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(2)))
        self.lines(f, [T(x) for x in self.CRITERIA], wrap=wrap)
        ttk.Label(f, text=T('この判定の限界'), style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(2)))
        self.lines(f, [T(x) for x in self.LIMITS], wrap=wrap)

        after = T('まだ何も移動しません。チェックを付けたあと、一覧で確かめてから「チェックしたものをごみ箱へ」で移動します。')
        if info.get("current_checked"):
            after = T('今付いているチェック（{0:,} 件）は外し、候補だけにチェックを付けます。', info['current_checked']) + after
        ttk.Label(f, text=after, wraplength=wrap, justify="left").pack(anchor="w", pady=(px(12), 0))

        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(px(14), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.ok_btn = ttk.Button(buttons, text=T('候補 {0:,} 件にチェックを付ける', info['count']), command=self.accept,
                                 state="normal" if info["count"] else "disabled")
        self.ok_btn.pack(side="right", padx=(0, px(8)))
        self.first_focus = self.cancel_btn


class DupBulkWarningDialog(_ModalDialog):
    """一括移動の 1 段目。規模・場所・起こりうることを伝える。既定はキャンセル。

    ここでは移動しない。「次へ」で 2 段目（移動するものの一覧）へ進むだけ。
    """

    def __init__(self, app: "App", summary: dict):
        super().__init__(app, T('重複ファイルの一括移動 1/2'))
        self.minsize(px(620), 1)
        f = self.frame
        wrap = px(680)
        ttk.Label(f, text=T('確認 1/2 — まとめてごみ箱へ移動する前に'), style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, style="Large.TLabel",
                  text=T('{0:,} 件 ・ {1}（{2:,} 組）', summary['count'], human_size(summary['size']), summary['groups'])
                  ).pack(anchor="w", pady=(px(6), 0))
        ttk.Label(f, text=T('選んだ範囲: ') + T(' ＋ ').join(summary["scopes"]), style="Strong.TLabel",
                  wraplength=wrap, justify="left").pack(anchor="w", pady=(px(2), 0))
        if summary.get("blocked"):
            ttk.Label(f, text=T('このほか {0:,} 件は保護対象などのため移動しません（次の画面に理由を出します）。', summary['blocked']),
                      style="Muted.TLabel", wraplength=wrap, justify="left").pack(anchor="w")

        ttk.Label(f, text=T('主な保存場所'), style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(2)))
        locs = [T('{0}  —  {1:,} 件 ・ {2}', path, n, human_size(size)) for path, n, size in summary["locations"]]
        if summary.get("more_locations"):
            locs.append(T('ほか {0:,} か所', summary['more_locations']))
        self.lines(f, locs, wrap=wrap)

        if summary.get("categories"):
            ttk.Label(f, text=T('対象に含まれるもの（保存場所・種類からの推測）'), style="Strong.TLabel"
                      ).pack(anchor="w", pady=(px(12), px(2)))
            self.lines(f, [T('{0}: {1:,} 件', label, n) for label, n in summary["categories"]], wrap=wrap)

        ttk.Label(f, text=T('起こりうること'), style="Warn.TLabel").pack(anchor="w", pady=(px(12), px(2)))
        self.lines(f, [
            T('各組で 1 件は残しますが、残した 1 件で代わりになるとは限りません。内容が同じでも、別のバージョンのプロジェクトや別のアプリが、それぞれの場所にあるファイルを必要としていることがあります。'),
            T('移動したファイルを使っていたプロジェクトがビルドできなくなったり、アプリやゲームが起動しない・正しく動かなくなる可能性があります。'),
            T('ごみ箱からは元の場所へ戻せます（ごみ箱を空にするまで）。'),
        ], wrap=wrap)
        if summary.get("auto_keeps"):
            ttk.Label(f, wraplength=wrap, justify="left", style="Muted.TLabel",
                      text=T('{0:,} 組の「残す」側はアプリが選んだ案です（コピー名でない・ダウンロード以外・古い順）。次の画面で確認・変更できます。', summary['auto_keeps'])
                      ).pack(anchor="w", pady=(px(8), 0))

        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(px(16), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.next_btn = ttk.Button(buttons, text=T('次へ: 移動するファイルの一覧を確認する'), command=self.accept)
        self.next_btn.pack(side="right", padx=(0, px(8)))
        self.first_focus = self.cancel_btn      # 既定はキャンセル
        self.bind("<Return>", lambda _e: self._on_return())

    def _on_return(self):
        """Enter はフォーカスのあるボタン。既定（キャンセル）のままなら何もせず閉じる。"""
        if self.focus_get() is self.next_btn:
            self.accept()
        else:
            self.cancel()


class DupBulkReviewDialog(_ModalDialog):
    """一括移動の 2 段目。移動するファイルを全件、組ごとに確かめる。

    件数が多くても全件をたどれるよう、組単位でページに分ける（一覧は保存もできる）。
    残す側の変更・移動から外す操作はここでもでき、変更はそのまま本体の選択に反映する。
    移動するのは、確認のチェックを入れて「◯件をごみ箱へ移動」を押したときだけ。
    """

    PAGE_GROUPS = 100

    def __init__(self, app: "App"):
        super().__init__(app, T('重複ファイルの一括移動 2/2'))
        self.geometry(f"{min(px(980), self.winfo_screenwidth() - px(40))}x"
                      f"{min(px(680), self.winfo_screenheight() - px(80))}")
        self.minsize(px(720), px(480))
        f = self.frame
        self.page = 0
        self.count = 0
        self._rows: dict[str, tuple] = {}

        ttk.Label(f, text=T('確認 2/2 — 移動するファイルの一覧（最終確認）'), style="Section.TLabel").pack(anchor="w")
        self.head = tk.StringVar()
        ttk.Label(f, textvariable=self.head, style="Large.TLabel").pack(anchor="w", pady=(px(4), 0))
        self.scope = tk.StringVar()
        ttk.Label(f, textvariable=self.scope, style="Strong.TLabel", wraplength=px(900), justify="left"
                  ).pack(anchor="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=px(900), justify="left",
                  text=T('「残す」の行は移動しません。残す側を変えるには行を選んで「残す側にする」、移動をやめるには行を選んで「移動から外す」（Ctrl+クリックで離れた行を追加、Shift+クリックで範囲）。')
                  ).pack(anchor="w", pady=(px(4), px(6)))

        # 下から積む（一覧が伸びても操作が隠れないように）
        footer = ttk.Frame(f)
        footer.pack(side="bottom", fill="x", pady=(px(10), 0))
        self.confirmed = tk.BooleanVar(value=False)
        self.confirm_chk = ttk.Checkbutton(footer, variable=self.confirmed, command=self._sync)
        self.confirm_chk.pack(side="left")
        self.cancel_btn = ttk.Button(footer, text=T('キャンセル'), command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.move_btn = ttk.Button(footer, command=self.accept)
        self.move_btn.pack(side="right", padx=(0, px(8)))

        tools = ttk.Frame(f)
        tools.pack(side="bottom", fill="x", pady=(px(6), 0))
        ttk.Button(tools, text=T('残す側にする'), command=self.make_keep).pack(side="left")
        ttk.Button(tools, text=T('移動から外す'), command=self.exclude_selected).pack(side="left", padx=(px(8), 0))
        ttk.Button(tools, text=T('一覧を保存（CSV）…'), command=self.save_csv).pack(side="left", padx=(px(8), 0))
        self.next_btn = ttk.Button(tools, text=T('次の組 ▶'), command=lambda: self.goto(self.page + 1))
        self.next_btn.pack(side="right")
        self.page_var = tk.StringVar()
        ttk.Label(tools, textvariable=self.page_var, style="Muted.TLabel").pack(side="right", padx=px(8))
        self.prev_btn = ttk.Button(tools, text=T('◀ 前の組'), command=lambda: self.goto(self.page - 1))
        self.prev_btn.pack(side="right")
        self.note = tk.StringVar()
        ttk.Label(f, textvariable=self.note, style="Muted.TLabel").pack(side="bottom", anchor="w")

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        cols = (("action", T('操作'), 150, "w"), ("size", T('容量'), 90, "e"), ("modified", T('更新日時'), 130, "w"),
                ("path", T('場所'), 420, "w"))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="tree headings", selectmode="extended")
        self.tree.heading("#0", text=T('組 / ファイル'), anchor="w")
        self.tree.column("#0", width=px(260), stretch=False)
        for col, title, width, anchor in cols:
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=(col == "path"))
        sy = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.tree.tag_configure("keep", foreground=P["accent"])
        self.tree.tag_configure("blocked", foreground=P["faint"])
        self.first_focus = self.cancel_btn
        fit_wrap(self)
        self.refresh()

    # --- 表示 ---
    def refresh(self):
        self.plan = self.app._dup_plan()
        count = sum(len(p.targets) for p in self.plan)
        size = sum(f.size for p in self.plan for f in p.targets)
        groups = sum(1 for p in self.plan if p.targets)
        blocked = sum(len(p.blocked) for p in self.plan)
        self.count = count
        self.head.set(T('次の {0:,} 件（{1:,} 組）・合計 {2} を Windows のごみ箱へ移動します', count, groups, human_size(size)))
        self.scope.set(T('選んだ範囲: ') + T(' ＋ ').join(self.app.dup_scope_labels())
                       + (T('  ／  移動しないもの {0:,} 件（理由つきで一覧に表示）', blocked) if blocked else ""))
        self.confirm_chk.configure(text=T('一覧を確認し、上の {0:,} 件を移動すると自分で判断しました', count))
        self.move_btn.configure(text=T('{0:,} 件をごみ箱へ移動', count))
        self.confirmed.set(False)        # 対象が変わったら確認もやり直す
        self._sync()
        self.goto(self.page)

    def goto(self, page: int):
        pages = max(1, -(-len(self.plan) // self.PAGE_GROUPS))
        self.page = max(0, min(page, pages - 1))
        start = self.page * self.PAGE_GROUPS
        shown = self.plan[start:start + self.PAGE_GROUPS]
        self.tree.delete(*self.tree.get_children(""))
        self._rows.clear()
        for n, p in enumerate(shown, start=start + 1):
            g = p.group
            gid = f"p{n}"
            self.tree.insert("", "end", iid=gid, open=True,
                             text=T('組 {0:,} ・ {1} 個が同じ中身', n, g.count),
                             values=(T('移動 {0} ・ 残す {1}', len(p.targets), g.count - len(p.targets)),
                                     f"{human_size(g.size)} × {g.count}", "", ""))
            self._rows[gid] = (p, None, "group")
            if p.keep is not None:
                iid = f"{gid}k"
                self.tree.insert(gid, "end", iid=iid, text=os.path.basename(p.keep.path), tags=("keep",),
                                 values=(T('◎ 残す（アプリの案）') if p.keep_auto else T('◉ 残す（指定）'),
                                         human_size(p.keep.size), fmt_date(p.keep.modified),
                                         os.path.dirname(p.keep.path)))
                self._rows[iid] = (p, p.keep, "keep")
            for j, t in enumerate(p.targets):
                iid = f"{gid}t{j}"
                self.tree.insert(gid, "end", iid=iid, text=os.path.basename(t.path),
                                 values=(T('→ ごみ箱へ移動'), human_size(t.size), fmt_date(t.modified),
                                         os.path.dirname(t.path)))
                self._rows[iid] = (p, t, "target")
            for j, (b, why) in enumerate(p.blocked):
                iid = f"{gid}b{j}"
                self.tree.insert(gid, "end", iid=iid, text=os.path.basename(b.path), tags=("blocked",),
                                 values=(T('移動しない: {0}', why), human_size(b.size), fmt_date(b.modified),
                                         os.path.dirname(b.path)))
                self._rows[iid] = (p, b, "blocked")
        if self.plan:
            last = min(start + self.PAGE_GROUPS, len(self.plan))
            self.page_var.set(T('組 {0:,}–{1:,} / {2:,}', start + 1, last, len(self.plan)))
        else:
            self.page_var.set(T('移動するものはありません'))
        self.prev_btn.configure(state="normal" if self.page > 0 else "disabled")
        self.next_btn.configure(state="normal" if self.page < pages - 1 else "disabled")

    def _sync(self):
        ok = self.confirmed.get() and self.count > 0
        self.move_btn.configure(state="normal" if ok else "disabled")

    # --- 操作 ---
    def make_keep(self):
        """選んだファイルを、その組の「残す」にする。前の「残す」は移動対象には加えない。"""
        per_group: dict[str, list] = {}
        for iid in self.tree.selection():
            p, f, _role = self._rows.get(iid, (None, None, None))
            if f is not None:
                per_group.setdefault(p.group.digest, []).append((p, f))
        if not per_group:
            self.note.set(T('残す側にするファイルの行を選んでください。'))
            return
        skipped = 0
        for items in per_group.values():
            if len(items) != 1:
                skipped += 1        # 同じ組で 2 行以上選ばれていたら、どちらにするか決められない
                continue
            p, f = items[0]
            self.app._dup_set_keep(p.group, f.path)
        self.refresh()
        self.note.set(T('同じ組で複数の行を選んでいた {0} 組は変更していません（残す側は 1 組に 1 件）。', skipped)
                      if skipped else T('残す側を変更しました。前に「残す」だったファイルも移動しません。'))

    def exclude_selected(self):
        paths = []
        for iid in self.tree.selection():
            p, f, role = self._rows.get(iid, (None, None, None))
            if role == "target":
                paths.append(f.path)
            elif role == "group":
                paths.extend(t.path for t in p.targets)
        if not paths:
            self.note.set(T('移動から外す行（「→ ごみ箱へ移動」の行、または組の行）を選んでください。'))
            return
        self.app._dup_uncheck_paths(paths)
        self.refresh()
        self.note.set(T('{0:,} 件を移動から外しました（本体のチェックも外れています）。', len(paths)))

    def save_csv(self):
        path = filedialog.asksaveasfilename(parent=self, title=T('移動するファイルの一覧を保存'),
                                            defaultextension=".csv", initialfile=T('移動予定の一覧.csv'),
                                            filetypes=[("CSV", "*.csv")])
        if path:
            try:
                self.app.export_dup_plan_csv(path)
                self.note.set(T('保存しました: {0}', path))
            except OSError as exc:
                self.note.set(T('保存できませんでした: {0}', exc))

    def accept(self):
        if not (self.confirmed.get() and self.count > 0):
            return
        super().accept()


class BulkProgress(tk.Toplevel):
    """まとめて移動している間の進み具合。「中止」を押すと、残りは実行しない（未実行として数える）。"""

    def __init__(self, app: "App", title: str, total: int):
        super().__init__(app)
        self.title(f"{title} — {APP_NAME}")
        self.transient(app)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.cancelled = False
        self.total = total
        self._next = 0.0
        f = ttk.Frame(self, padding=(px(20), px(16)))
        f.pack(fill="both", expand=True)
        self.text = tk.StringVar(value=T('0 / {0:,} 件', total))
        ttk.Label(f, textvariable=self.text, style="Strong.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(f, length=px(420), maximum=max(1, total))
        self.bar.pack(fill="x", pady=(px(8), px(8)))
        ttk.Label(f, text=T('中止すると、まだ移動していないものはそのまま残ります（未実行として数えます）。'),
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Button(f, text=T('中止'), command=self.cancel).pack(anchor="e", pady=(px(10), 0))
        self.grab_set()

    def cancel(self):
        self.cancelled = True
        self.text.set(T('中止しています…（今の 1 件が終わりしだい止めます）'))

    def step(self, done: int, moved: int, failed: int):
        now = time.monotonic()
        if now < self._next and done < self.total:
            return
        self._next = now + 0.1
        if not self.cancelled:
            self.text.set(T('{0:,} / {1:,} 件  （移動 {2:,} ・ 移動しなかった {3:,}）', done, self.total, moved, failed))
        self.bar.configure(value=done)
        self.update()

    def close(self):
        try:
            self.grab_release()
            self.destroy()
        except tk.TclError:
            pass


@dataclass
class DupPlanGroup:
    """一括移動の計画のうち 1 組分。残す 1 件・移す分・移せない分（理由つき）。"""
    group: duplicates.DupGroup
    keep: duplicates.DupFile | None
    keep_auto: bool
    targets: list
    blocked: list


@dataclass
class TrashStep:
    """まとめて移すときの 1 件。run() は移動まで行い、None（移動した）か ("failed" | "skipped", 理由) を返す。"""
    obj: object
    name: str
    size: int
    run: Callable[[], "tuple[str, str] | None"]


# ごみ箱へ移す前に注意を強める種類（Windows・アプリの動作やデータに関わりうる）
TRASH_RISKY_KINDS = {"system", "executable", "config", "database", "save", "game_data", "dev_vcs", "dev_deps"}


# 未展開のフォルダに置く仮の子要素（| は Windows のパスに使えない文字）
PLACEHOLDER_SUFFIX = "|placeholder"


class LocationPicker(tk.Toplevel):
    """スキャン済みのフォルダ階層から「位置」を選ぶ。子フォルダは開いたときに読み込む。"""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title(T('位置を選ぶ — ') + APP_NAME)
        self.geometry(f"{min(px(680), self.winfo_screenwidth() - px(40))}x"
                      f"{min(px(540), self.winfo_screenheight() - px(100))}")
        self.minsize(px(420), px(320))
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        frame = ttk.Frame(self, padding=(px(16), px(14)))
        frame.pack(fill="both", expand=True)
        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=(px(10), 0))
        self.direct = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text=T('直下のみ'), variable=self.direct).pack(side="left")
        ttk.Button(bottom, text=T('キャンセル'), command=self.withdraw).pack(side="right")
        ttk.Button(bottom, text=T('この位置で絞り込む'), style="Accent.TButton",
                   command=self.choose).pack(side="right", padx=(0, px(8)))

        ttk.Label(frame, text=T('絞り込むフォルダを選んでください（ダブルクリックでも決定）'),
                  style="Muted.TLabel").pack(anchor="w", pady=(0, px(8)))
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("size", "count"), selectmode="browse")
        self.tree.heading("#0", text=T('フォルダ'), anchor="w")
        self.tree.heading("size", text=T('容量'), anchor="e")
        self.tree.heading("count", text=T('ファイル数'), anchor="e")
        self.tree.column("#0", width=px(380), stretch=True)
        self.tree.column("size", width=px(100), anchor="e", stretch=False)
        self.tree.column("count", width=px(100), anchor="e", stretch=False)
        sy = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", lambda _e: self._load_children(self.tree.focus()))
        self.tree.bind("<Double-1>", lambda _e: self.choose())
        self.tree.bind("<Return>", lambda _e: self.choose())
        self._loaded: set[str] = set()

    def _insert(self, parent_iid: str, folder: Path):
        app = self.app
        iid = str(folder)
        self.tree.insert(parent_iid, "end", iid=iid, text=folder.name or iid,
                         values=(human_size(app.folder_sizes.get(folder, 0)),
                                 f"{app.folder_file_counts.get(folder, 0):,}"))
        if app.treemap.child_dirs.get(folder):
            self.tree.insert(iid, "end", iid=iid + PLACEHOLDER_SUFFIX, text="…")

    def _load_children(self, iid: str):
        if not iid or iid in self._loaded:
            return
        self._loaded.add(iid)
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        app = self.app
        children = sorted(app.treemap.child_dirs.get(Path(iid), []),
                          key=lambda p: app.folder_sizes.get(p, 0), reverse=True)
        for child in children:
            self._insert(iid, child)

    def reset(self):
        self.tree.delete(*self.tree.get_children())
        self._loaded.clear()
        if self.app.scan_root is not None:
            self._insert("", self.app.scan_root)

    def show(self, folder: Path | None):
        if not self.tree.get_children() or self.tree.get_children()[0] != str(self.app.scan_root):
            self.reset()
        current = self.app.filters.get("folder")
        self.direct.set(bool(current) and not current[1])
        # 選択中の位置まで階層を開く
        root = self.app.scan_root
        if folder is not None and root is not None:
            chain = [folder] + [p for p in folder.parents]
            chain = [p for p in reversed(chain) if p == root or root in p.parents]
            for p in chain[:-1]:
                self._load_children(str(p))
                self.tree.item(str(p), open=True)
            if self.tree.exists(str(folder)):
                self.tree.selection_set(str(folder))
                self.tree.focus(str(folder))
                self.tree.see(str(folder))
        self.app.raise_window(self)

    def choose(self):
        sel = self.tree.selection()
        if not sel or sel[0].endswith(PLACEHOLDER_SUFFIX):
            return
        self.app.set_filter("folder", (sel[0], not self.direct.get()))
        self.withdraw()


class App(tk.Tk):
    LEDGER_PATH: str | None = None   # ごみ箱の記録の保存先（None なら %LOCALAPPDATA%。テストでは一時ファイル）
    SETTINGS_PATH: str | None = None  # 言語の設定の保存先（ごみ箱の記録とは別のファイル）

    def __init__(self, hidden: bool = False, language: str | None = None):
        """hidden=True なら、一度も画面に出さずに作る（テスト・計測用）。

        作ってから withdraw() しても、下の update_idletasks() の時点で一瞬表示されてしまう。
        そのため、非表示にするなら最初に（何かを描く前に）隠す。
        """
        super().__init__()
        if hidden:
            self.withdraw()
        self.fonts = setup_style(self)
        self._choose_language(hidden, language)
        self.title(f"{APP_NAME} {APP_VERSION}")
        width = min(px(1240), self.winfo_screenwidth() - px(40))
        height = min(px(800), self.winfo_screenheight() - px(80))
        self.geometry(f"{width}x{height}")

        self.scan_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.current_scan: Scanner | None = None

        default = Path.home() / "Downloads"
        self.current_root = default if default.exists() else Path.home()

        self.status = tk.StringVar(value=T('対象フォルダを確認して「スキャン」を押してください'))
        self.root_label = tk.StringVar(value=str(self.current_root))
        self.search_text = tk.StringVar(value="")
        self.file_rows: list[FileItem] = []
        self.folder_rows = []
        self.sort_state = {"files": ("size", True), "folders": ("size", True)}

        # スキャン結果の索引（詳細画面・絞り込みはすべてここから引く）
        self.scan_root: Path | None = None
        self.folder_sizes: dict[Path, int] = {}
        self.folder_file_counts: dict[Path, int] = {}
        self.folder_ctx: dict[str, FolderContext] = {}
        self.root_parent_ctx: FolderContext | None = None
        self.group_labels: dict[str, str] = {}
        self.dir_index: dict[str, list[FileItem]] = {}
        self._dir_lower: dict[str, str] = {}
        self._folder_lookup: dict[str, Path] = {}
        self.total_size = 0

        # 重複検出（通常スキャンとは別。押したときだけ走る）
        self.dup_cancel = threading.Event()
        self.current_dup: DupScanner | None = None
        self.dup_result = None
        self.dup_keep: dict[str, str] = {}   # 組の digest -> 残すファイルのパス
        self.dup_keep_auto: set[str] = set()  # 上のうち、利用者ではなくアプリが選んだ「案」の組
        # 移動するとチェックしたファイル: パス -> (組の digest, 容量)。
        # 不変条件: チェックのある組には、チェックしていない「残す」が必ず 1 件ある。
        self.dup_checked: dict[str, tuple[str, int]] = {}
        self.dup_origin: list[tuple] = []     # チェックをどう付けたか (文言の鍵, 値…)。表示のたびに訳す
        self.dup_assess: dict[str, tuple[bool, str]] = {}   # 低リスク判定の結果（押したときだけ作る）
        self.dup_fail_note: dict[str, str] = {}             # 前回の一括移動で移せなかった理由
        self.dup_page = 0
        self._dup_anchor: str | None = None   # Shift+クリックの起点（チェック欄）
        self._dup_order: list[tuple] = []     # 表示中のファイル行 (iid, 組, ファイル) の並び
        self._dup_shown: list = []            # 表示中のページの組
        self._dup_rows: dict[str, tuple] = {}   # 行の iid -> ("group"|"file", ...)
        self._dup_filter_after = None
        self._plan_prot: dict | None = None   # 一括移動の確認中だけ使う保護判定の覚え
        # このアプリがごみ箱へ移した項目の記録（アプリを閉じても残る）
        self.ledger = recyclebin.Ledger(self.LEDGER_PATH or recyclebin.default_ledger_path())
        self._pending_moves: list[tuple[str, bool, int, float]] = []
        self.bin_state: dict[str, tuple[str, str]] = {}   # 記録の key -> (状態, 説明)
        self._bin_rows: dict[str, recyclebin.Entry] = {}
        self._bin_checking = False
        # ファイル一覧・フォルダ一覧の「移動」チェック（両方の一覧で共通）。
        # ファイルは FileItem そのもの、フォルダはパスで覚えるので、絞り込み・並べ替えでは変わらない。
        self.check_files: dict[int, FileItem] = {}
        self.check_dirs: dict[str, Path] = {}
        self._check_anchor: dict[str, tuple | None] = {"files": None, "folders": None}
        self.dup_running = False              # 検出中（途中結果を表示中）
        self._dup_progress = None

        # 詳細画面から設定する絞り込み条件: kind / group / folder
        self.filters: dict[str, object] = {}
        self._file_iids: dict[str, FileItem] = {}
        self._search_after = None
        self.detail: DetailWindow | None = None

        self.summary_vars = {
            "disk": tk.StringVar(value="—"),
            "scanned": tk.StringVar(value=T('未スキャン')),
            "files": tk.StringVar(value=""),
            "folders": tk.StringVar(value=""),
        }

        self._build_ui()
        fit_wrap(self)
        self._build_menu()
        # ボタンが切れない最小サイズ（実際の文字幅から決める）
        self.update_idletasks()
        self.minsize(max(px(760), self.min_content_width()), px(520))
        self._poll_id = self.after(150, self._poll_queue)
        self._refresh_disk_summary()

    # --- 言語 ---
    def settings_path(self) -> str:
        return self.SETTINGS_PATH or i18n.default_settings_path()

    def _choose_language(self, hidden: bool, language: str | None):
        """起動時の言語。保存した言語があり「次回から表示しない」なら、選ぶ画面は出さない。

        画面を出さない起動（hidden=True。テスト・計測）では選ぶ画面を出さない。
        """
        if language is not None:
            i18n.set_language(language)
            return
        st = i18n.load_settings(self.settings_path())
        lang = st["language"] or "ja"
        if st["ask"] and not hidden:
            self.withdraw()
            lang, dont_ask = LanguageDialog(self, lang).ask()
            if dont_ask:
                try:
                    i18n.save_settings(self.settings_path(), lang, True)
                except OSError:
                    pass
            self.deiconify()
        i18n.set_language(lang)

    def _build_menu(self):
        """画面上部の「設定 → 言語 → 日本語 / English」。"""
        bar = tk.Menu(self)
        settings = tk.Menu(bar, tearoff=0)
        lang_menu = tk.Menu(settings, tearoff=0)
        self._lang_var = tk.StringVar(value=i18n.LANG)
        lang_menu.add_radiobutton(label="日本語" + (" (Japanese)" if i18n.LANG == "en" else ""),
                                  value="ja", variable=self._lang_var, command=lambda: self.set_language("ja"))
        lang_menu.add_radiobutton(label="English", value="en", variable=self._lang_var,
                                  command=lambda: self.set_language("en"))
        settings.add_cascade(label=T('言語'), menu=lang_menu)
        bar.add_cascade(label=T('設定'), menu=settings)
        self.configure(menu=bar)
        self._menubar = bar

    def set_language(self, lang: str) -> bool:
        """言語を切り替える。設定に保存し（次回の起動にも反映）、画面を今の言語で作り直す。

        スキャン結果・チェック・重複の結果・見ている場所はそのまま残す。
        スキャンや重複の検出の最中は切り替えない（途中の結果を作り直せないため）。
        """
        if lang == i18n.LANG:
            return True
        busy = (self.current_scan is not None and self.current_scan.is_alive()) or self.dup_running
        if busy:
            self._lang_var.set(i18n.LANG)
            self.status.set(T('スキャン・重複の検出中は言語を切り替えられません。終わってから切り替えてください。'))
            return False
        try:
            i18n.save_settings(self.settings_path(), lang, True)
        except OSError as exc:
            self.status.set(T('言語の設定を保存できませんでした（{0}）', exc))
        i18n.set_language(lang)
        self._rebuild_ui()
        self.status.set(T('表示の言語を切り替えました。'))
        return True

    def _rebuild_ui(self):
        """今の言語で画面を作り直し、持っているデータ（スキャン結果など）から表示し直す。"""
        tab = self.tabs.index(self.tabs.select()) if hasattr(self, "tabs") else 0
        map_here = self.treemap.current_root if hasattr(self, "treemap") else None
        direct = self.treemap.direct_file_sizes if hasattr(self, "treemap") else {}
        for var in (self.root_label, self.search_text):
            for mode, cb in var.trace_info():
                var.trace_remove(mode[0] if isinstance(mode, tuple) else mode, cb)
        for attr in ("_search_after", "_dup_filter_after"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        for w in list(self.winfo_children()):
            w.destroy()
        self.detail = None
        self.location_picker = None
        self.dup_fail_note.clear()            # 前の言語で書いたメモは消す
        self.bin_state.clear()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self._build_ui()
        fit_wrap(self)
        self._build_menu()
        self.update_idletasks()
        self.minsize(max(px(760), self.min_content_width()), px(520))
        # データから表示し直す
        self._refresh_disk_summary()
        if self.scan_root is not None:
            self.summary_vars["scanned"].set(human_size(self.total_size))
            self.summary_vars["files"].set(T('{0:,} ファイル', len(self.file_rows)))
            self.summary_vars["folders"].set(T('{0:,} フォルダ', len(self.folder_rows)))
            self.treemap.set_data(self.scan_root, self.folder_sizes, direct, self.folder_file_counts)
            if map_here is not None and map_here in self.folder_sizes:
                self.treemap.navigate(map_here, remember=False)
        else:
            self.summary_vars["scanned"].set(T('未スキャン'))
            self.summary_vars["files"].set("")
            self.summary_vars["folders"].set("")
        self.render_folders()
        self.render_files()
        self.render_dups()
        if self.dup_result is not None:
            self._dup_totals_note()
            self.dup_note_extra.set("")
        self._update_check_views()
        self.render_bin()
        try:
            self.tabs.select(tab)
        except tk.TclError:
            pass

    def _build_ui(self):
        fonts = self.fonts
        pad_x = px(16)

        # --- 上部: スキャン対象・操作・容量の概要を 2 行にまとめる ---
        header = ttk.Frame(self, padding=(pad_x, px(12), pad_x, px(10)))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)
        title = ttk.Frame(header)
        title.grid(row=0, column=0, sticky="w", padx=(0, px(20)))
        ttk.Label(title, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(title, text=f"v{APP_VERSION}", style="Muted.TLabel").pack(side="left", padx=(px(6), 0))
        target = ttk.Frame(header)
        target.grid(row=0, column=1, sticky="ew")
        ttk.Label(target, text=T('対象'), style="Muted.TLabel").pack(side="left", padx=(0, px(8)))
        self.root_path_label = ElidedLabel(target, text=self.root_label.get(), style="Strong.TLabel", font=fonts["bold"])
        self.root_path_label.pack(side="left", fill="x", expand=True)
        self.root_label.trace_add("write", lambda *_: self.root_path_label.set(self.root_label.get()))
        buttons = ttk.Frame(header)
        buttons.grid(row=0, column=2, sticky="e", padx=(px(12), 0))
        ttk.Button(buttons, text=T('フォルダを選ぶ…'), command=self.choose_folder).pack(side="left")
        self.scan_btn = ttk.Button(buttons, text=T('スキャン'), style="Accent.TButton", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=(px(8), 0))
        self.cancel_btn = ttk.Button(buttons, text=T('中止'), command=self.cancel_scan, state="disabled")
        self.cancel_btn.pack(side="left", padx=(px(8), 0))

        summary = ttk.Frame(header)
        summary.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(10), 0))
        ttk.Label(summary, text=T('ドライブ'), style="Muted.TLabel").pack(side="left")
        ttk.Label(summary, textvariable=self.summary_vars["disk"], style="Strong.TLabel").pack(side="left", padx=(px(8), px(8)))
        self.disk_bar = ttk.Progressbar(summary, length=px(110), maximum=100)
        self.disk_bar.pack(side="left")
        ttk.Separator(summary, orient="vertical").pack(side="left", fill="y", padx=px(18))
        ttk.Label(summary, text=T('スキャン結果'), style="Muted.TLabel").pack(side="left")
        for key in ("scanned", "files", "folders"):
            ttk.Label(summary, textvariable=self.summary_vars[key], style="Strong.TLabel").pack(side="left", padx=(px(10), 0))

        # --- 下部: 状態表示（進捗・完了） ---
        bottom = ttk.Frame(self, padding=(pad_x, px(4), pad_x, px(8)))
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, textvariable=self.status, style="Muted.TLabel").pack(side="left")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=pad_x, pady=(0, px(4)))

        # --- 容量マップ ---
        overview = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(overview, text=T('容量マップ'))
        self.overview_tab = overview
        self.treemap = Treemap(
            overview,
            on_open_folder=open_in_file_manager,
            on_detail=self.show_folder_detail,
            on_direct_files=lambda f: self.apply_filter("folder", (str(f), False)),
            describe=self.folder_badge,
            on_trash=self.trash_folder,
            on_reveal=self.reveal_in_folder_list,
            on_files=lambda f: self.apply_filter("folder", (str(f), True)),
        )
        self.treemap.pack(fill="both", expand=True)

        # --- フォルダ一覧 ---
        folders = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(folders, text=T('フォルダ一覧'))
        self.folders_tab = folders
        folder_top = ttk.Frame(folders)
        folder_top.pack(fill="x", pady=(0, px(8)))
        self.folder_note = tk.StringVar(value="")
        ttk.Label(folder_top, textvariable=self.folder_note, style="Strong.TLabel").pack(side="left")
        self.folder_selection_note = tk.StringVar(value="")
        ttk.Label(folder_top, textvariable=self.folder_selection_note, style="Strong.TLabel"
                  ).pack(side="left", padx=(px(14), 0))
        ttk.Button(folder_top, text=T('ごみ箱へ移動'), command=self.trash_selected_folder).pack(side="right", padx=(px(8), 0))
        ttk.Button(folder_top, text=T('詳細を表示'), command=self.detail_folder_selected).pack(side="right")
        ttk.Button(folder_top, text=T('ファイル一覧でこの場所を見る'),
                   command=self.filter_to_selected_folder).pack(side="right", padx=(0, px(8)))

        # 今どこを見ているか、と親へ戻る道。折りたたみ式の一覧だけでは
        # 「今どの階層にいるのか」が読み取りにくいので、選んだ行の場所をここに出す。
        folder_nav = ttk.Frame(folders)
        folder_nav.pack(fill="x", pady=(0, px(6)))
        self.folder_up_btn = ttk.Button(folder_nav, text=T('← 親フォルダ'), command=self.open_parent_folder,
                                        state="disabled")
        self.folder_up_btn.pack(side="left")
        ttk.Label(folder_nav, text=T('今見ている場所'), style="Muted.TLabel").pack(side="left", padx=(px(12), px(8)))
        self.folder_here = ElidedLabel(folder_nav, text="—", style="Strong.TLabel", font=fonts["bold"])
        self.folder_here.pack(side="left", fill="x", expand=True)
        ttk.Label(folders, text=T('行の ▶ を押すと、その直下のフォルダとファイルを表示します（ダブルクリック・Enter で詳細、右クリックでその他の操作。Ctrl+クリックで離れた行を追加・解除、Shift+クリックで範囲選択）'),
                  style="Muted.TLabel", wraplength=px(1000), justify="left").pack(anchor="w", pady=(0, px(6)))

        self.folder_check_bar = self._make_check_bar(folders)
        self.folder_tree, _ = self._make_table(folders, [
            ("check", T('移動'), 52, "center", False),
            ("size", T('容量'), 100, "e", False),
            ("count", T('ファイル数'), 90, "e", False),
            ("group", T('関連するアプリ・プロジェクト'), 250, "w", False),
            ("path", T('パス'), 520, "w", True),
        ], self.sort_folders, selectmode="extended")
        # 名前の列は木の列（#0）へ移す。折りたたみの ▶ と一緒に見えるようにするため。
        self.folder_tree.configure(show="tree headings")
        self.folder_tree.heading("#0", text=T('フォルダ / ファイル'), anchor="w",
                                 command=lambda: self.sort_folders("name"))
        self.folder_tree.column("#0", width=px(300), minwidth=px(160), stretch=False, anchor="w")
        self.folder_tree._titles["#0"] = T('フォルダ / ファイル')
        self.folder_tree.bind("<<TreeviewOpen>>", self._on_folder_open)
        self.folder_tree.bind("<<TreeviewSelect>>", lambda _e: (self._update_folder_here(),
                                                                self._update_folder_selection_note(),
                                                                self._retag_selection(self.folder_tree)))
        self.folder_tree.bind("<Double-1>", lambda _e: self.detail_folder_selected())
        self.folder_tree.bind("<Return>", lambda _e: self.detail_folder_selected())
        self.folder_tree.bind("<Button-3>", self._folder_context_menu)
        self.folder_tree.bind("<Delete>", lambda _e: self.trash_selected_folder())
        for seq in ("<Button-1>", "<Shift-Button-1>", "<Control-Button-1>"):
            self.folder_tree.bind(seq, self._folder_check_click)
        self.folder_tree.bind("<space>", lambda _e: self._space_check("folders"))
        self._style_check_rows(self.folder_tree)

        # --- ファイル一覧 ---
        files = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(files, text=T('ファイル一覧'))
        self.files_tab = files

        # 常設の絞り込みUI。状態は self.filters と self.search_text だけが持ち、
        # ここのコントロールは render_files() のたびにその状態へ合わせて表示し直す。
        controls = ttk.Frame(files)
        controls.pack(fill="x", pady=(0, px(6)))
        controls.columnconfigure(1, weight=3)
        controls.columnconfigure(3, weight=2)
        label_w = px(40)
        controls.columnconfigure(0, minsize=label_w)
        ttk.Label(controls, text=T('検索')).grid(row=0, column=0, sticky="w")
        search = ttk.Entry(controls, textvariable=self.search_text, width=12)
        search.grid(row=0, column=1, sticky="ew", padx=(0, px(16)))
        self.search_text.trace_add("write", lambda *_: self._schedule_render_files())
        ttk.Label(controls, text=T('種類')).grid(row=0, column=2, sticky="w", padx=(0, px(8)))
        self.kind_combo = ttk.Combobox(controls, state="readonly", width=16)
        self.kind_combo.grid(row=0, column=3, sticky="ew", padx=(0, px(16)))
        self.kind_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_kind_selected())
        self._kind_choices: list[str | None] = []
        ttk.Button(controls, text=T('詳細を表示'), command=self.detail_file_selected).grid(row=0, column=4, sticky="e")
        ttk.Button(controls, text=T('ごみ箱へ移動'), command=self.trash_selected_file).grid(
            row=0, column=5, sticky="e", padx=(px(8), 0))

        ttk.Label(controls, text=T('位置')).grid(row=1, column=0, sticky="w", pady=(px(6), 0))
        loc = ttk.Frame(controls)
        loc.grid(row=1, column=1, columnspan=4, sticky="ew", pady=(px(6), 0))
        self.location_combo = ttk.Combobox(loc, width=12)
        self.location_combo.pack(side="left", fill="x", expand=True)
        self.location_combo.bind("<Return>", lambda _e: self._on_location_entered())
        self.location_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_location_entered())
        ttk.Button(loc, text=T('階層から選ぶ…'), command=self.open_location_picker).pack(side="left", padx=(px(8), px(12)))
        self.location_direct = tk.BooleanVar(value=False)
        ttk.Checkbutton(loc, text=T('直下のみ'), variable=self.location_direct,
                        command=self._on_location_direct_toggled).pack(side="left")
        self.location_picker: LocationPicker | None = None

        # 現在の条件（個別に解除可）と該当件数・合計容量を 1 行に
        status_row = ttk.Frame(files)
        status_row.pack(fill="x", pady=(0, px(6)))
        self.file_note = tk.StringVar(value="")
        self.file_note_extra = tk.StringVar(value="")
        self.file_selection_note = tk.StringVar(value="")
        ttk.Label(status_row, textvariable=self.file_note, style="Strong.TLabel").pack(side="right")
        ttk.Label(status_row, textvariable=self.file_selection_note,
                  style="Strong.TLabel").pack(side="right", padx=(0, px(14)))
        self.filter_bar = ttk.Frame(status_row)
        self.filter_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(files, textvariable=self.file_note_extra, style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(px(4), 0))
        self._filter_message = ""

        # 複数選択。Shift で範囲、Ctrl で個別の追加・解除（Windows 標準の作法）。
        self.file_check_bar = self._make_check_bar(files, with_only=True)
        self.file_tree, _ = self._make_table(files, [
            ("check", T('移動'), 52, "center", False),
            ("name", T('ファイル'), 250, "w", False),
            ("size", T('容量'), 90, "e", False),
            ("modified", T('更新日時'), 130, "w", False),
            ("kind", T('分類'), 150, "w", False),
            ("group", T('関連するアプリ・プロジェクト'), 210, "w", False),
            ("ext", T('拡張子'), 70, "w", False),
            ("path", T('パス'), 480, "w", True),
        ], self.sort_files, selectmode="extended")
        self.file_tree.bind("<<TreeviewSelect>>", lambda _e: (self._update_file_selection_note(),
                                                              self._retag_selection(self.file_tree)))
        self.file_tree.bind("<Control-a>", self._select_all_files)
        self.file_tree.bind("<Control-A>", self._select_all_files)
        self.file_tree.bind("<Double-1>", lambda _e: self.detail_file_selected())
        self.file_tree.bind("<Return>", lambda _e: self.detail_file_selected())
        self.file_tree.bind("<Button-3>", self._file_context_menu)
        self.file_tree.bind("<Delete>", lambda _e: self.trash_selected_file())
        for seq in ("<Button-1>", "<Shift-Button-1>", "<Control-Button-1>"):
            self.file_tree.bind(seq, self._file_check_click)
        self.file_tree.bind("<space>", lambda _e: self._space_check("files"))
        self._style_check_rows(self.file_tree)

        # --- 重複ファイル ---
        dups = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(dups, text=T('重複ファイル'))
        self.dups_tab = dups

        dup_top = ttk.Frame(dups)
        dup_top.pack(fill="x", pady=(0, px(6)))
        self.dup_btn = ttk.Button(dup_top, text=T('重複ファイルを探す'), style="Accent.TButton",
                                  command=self.start_dup_scan)
        self.dup_btn.pack(side="left")
        self.dup_cancel_btn = ttk.Button(dup_top, text=T('中止'), command=self.cancel_dup_scan, state="disabled")
        self.dup_cancel_btn.pack(side="left", padx=(px(8), 0))
        # 小さいファイルは数が多いわりに空く容量が少なく、開く回数だけが増える。
        # 省くかどうかは**ユーザーが決める**。既定は「すべて」で、何も黙って省かない。
        ttk.Label(dup_top, text=T('調べる大きさ'), style="Muted.TLabel").pack(side="left", padx=(px(16), px(6)))
        self.dup_min_size = ttk.Combobox(dup_top, state="readonly", width=12,
                                         values=[T('すべて'), T('1 MB 以上'), T('10 MB 以上'), T('100 MB 以上')])
        # 既定は「すべて」。
        #
        # 1 MB 以上にすれば実測で 737 秒 → 49 秒になるが、それを既定にすると、
        # 小さいファイルばかりのフォルダで「重複なし」に見えてしまう。
        # このアプリは事実だけを出すと決めているので、**黙って省かない**。
        # 時間がかかるときに、ユーザーが自分で狭められるようにしてある。
        self.dup_min_size.current(0)
        self.dup_min_size.pack(side="left")
        ttk.Button(dup_top, text=T('詳細を表示'), command=self.detail_dup_selected).pack(side="right")
        ttk.Button(dup_top, text=T('これを残す'), command=self.keep_selected_dup).pack(side="right", padx=(0, px(8)))

        # まとめて選ぶ（3 通り）と、チェックしたものの移動。移動はどれも同じ確認を通る。
        dup_sel = ttk.Frame(dups)
        dup_sel.pack(fill="x", pady=(0, px(6)))
        ttk.Button(dup_sel, text=T('低リスク候補を選ぶ…'), command=self.dup_select_low_risk).pack(side="left")
        self.dup_all_btn = ttk.Menubutton(dup_sel, text=T('すべて選ぶ ▾'))
        self._dup_all_menu = tk.Menu(self.dup_all_btn, tearoff=0, postcommand=self._fill_dup_all_menu)
        self.dup_all_btn.configure(menu=self._dup_all_menu)
        self.dup_all_btn.pack(side="left", padx=(px(8), 0))
        ttk.Button(dup_sel, text=T('チェックをすべて外す'), command=self.dup_clear_checks).pack(side="left", padx=(px(8), 0))
        self.dup_move_btn = ttk.Button(dup_sel, text=T('チェックしたものをごみ箱へ…'), command=self.trash_selected_dup)
        self.dup_move_btn.pack(side="right")
        self.dup_check_note = tk.StringVar(value="")
        ttk.Label(dup_sel, textvariable=self.dup_check_note, style="Strong.TLabel").pack(side="right", padx=(0, px(12)))

        self.dup_note = tk.StringVar(
            value=T('スキャン済みのファイルから、中身がまったく同じものを探します（押したときだけ実行します）。'))
        ttk.Label(dups, textvariable=self.dup_note, style="Strong.TLabel").pack(anchor="w")
        self.dup_note_extra = tk.StringVar(value="")
        ttk.Label(dups, textvariable=self.dup_note_extra, style="Muted.TLabel",
                  wraplength=px(1000), justify="left").pack(anchor="w", pady=(px(2), px(6)))

        # 表示の絞り込みとページ。数万組でも描くのは 1 ページ分だけ。
        dup_view = ttk.Frame(dups)
        dup_view.pack(fill="x", pady=(0, px(6)))
        ttk.Label(dup_view, text=T('絞り込み')).pack(side="left")
        self.dup_filter = tk.StringVar(value="")
        ttk.Entry(dup_view, textvariable=self.dup_filter, width=28).pack(side="left", padx=(px(8), px(12)))
        self.dup_filter.trace_add("write", lambda *_: self._schedule_dup_filter())
        ttk.Label(dup_view, text=T('表示')).pack(side="left")
        self.dup_view_mode = ttk.Combobox(dup_view, state="readonly", width=16,
                                          values=[T('すべての組'), T('チェックのある組'), T('チェックのない組')])
        self.dup_view_mode.current(0)
        self.dup_view_mode.pack(side="left", padx=(px(8), 0))
        self.dup_view_mode.bind("<<ComboboxSelected>>", lambda _e: self._dup_goto(0))
        self.dup_next_btn = ttk.Button(dup_view, text=T('次 ▶'), command=lambda: self._dup_goto(self.dup_page + 1))
        self.dup_next_btn.pack(side="right")
        self.dup_page_note = tk.StringVar(value="")
        ttk.Label(dup_view, textvariable=self.dup_page_note, style="Muted.TLabel").pack(side="right", padx=px(8))
        self.dup_prev_btn = ttk.Button(dup_view, text=T('◀ 前'), command=lambda: self._dup_goto(self.dup_page - 1))
        self.dup_prev_btn.pack(side="right")

        ttk.Label(dups, style="Muted.TLabel", wraplength=px(1000), justify="left",
                  text=T('「移動」欄のクリックでチェックを切り替え（Shift+クリックで範囲）。行は Ctrl+クリックで離れた行を追加・解除、Shift+クリックで範囲選択でき、Space で選んだ行のチェックを切り替えます。')
                  ).pack(side="bottom", anchor="w", pady=(px(4), 0))

        dup_wrap = ttk.Frame(dups)
        dup_wrap.pack(fill="both", expand=True)
        cols = [("check", T('移動'), 60, "center", False), ("keep", T('残す'), 52, "center", False),
                ("size", T('容量'), 90, "e", False), ("modified", T('更新日時'), 130, "w", False),
                ("kind", T('分類'), 130, "w", False), ("group", T('関連するアプリ・プロジェクト'), 180, "w", False),
                ("note", T('判定'), 190, "w", False), ("path", T('パス'), 400, "w", True)]
        self.dup_tree = ttk.Treeview(dup_wrap, columns=[c[0] for c in cols], show="tree headings",
                                     selectmode="extended")
        self.dup_tree.heading("#0", text=T('重複の組 / ファイル'), anchor="w")
        self.dup_tree.column("#0", width=px(300), stretch=False, anchor="w")
        for col, title, width, anchor, stretch in cols:
            self.dup_tree.heading(col, text=title, anchor=anchor)
            self.dup_tree.column(col, width=px(width), anchor=anchor, stretch=stretch)
        ysb = ttk.Scrollbar(dup_wrap, orient="vertical", command=self.dup_tree.yview)
        xsb = ttk.Scrollbar(dup_wrap, orient="horizontal", command=self.dup_tree.xview)
        self.dup_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.dup_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        dup_wrap.rowconfigure(0, weight=1)
        dup_wrap.columnconfigure(0, weight=1)
        self.dup_tree.tag_configure("keep", foreground=P["accent_dark"])
        self.dup_tree.tag_configure("checked", background=P["checked"])
        for seq in ("<Button-1>", "<Shift-Button-1>", "<Control-Button-1>"):
            self.dup_tree.bind(seq, self._dup_click)
        self.dup_tree.bind("<Double-1>", lambda _e: self.detail_dup_selected())
        self.dup_tree.bind("<Return>", lambda _e: self.detail_dup_selected())
        self.dup_tree.bind("<Button-3>", self._dup_context_menu)
        self.dup_tree.bind("<Delete>", lambda _e: self.trash_selected_dup())
        self.dup_tree.bind("<space>", self._dup_space)
        self.dup_tree.bind("<Control-a>", self._dup_select_rows_all)
        self.dup_tree.bind("<Control-A>", self._dup_select_rows_all)
        self._update_dup_check_note()

        # --- ごみ箱から復元（このアプリが移したものだけ） ---
        bin_tab = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(bin_tab, text=T('ごみ箱から復元'))
        self.bin_tab = bin_tab
        bin_top = ttk.Frame(bin_tab)
        bin_top.pack(fill="x", pady=(0, px(6)))
        ttk.Label(bin_top, text=T('このアプリからごみ箱へ移した項目'), style="Section.TLabel").pack(side="left")
        ttk.Button(bin_top, text=T('選んだ項目を元の場所へ復元…'), style="Accent.TButton",
                   command=self.restore_selected).pack(side="right")
        ttk.Button(bin_top, text=T('元の場所を開く'), command=self.open_bin_origin).pack(side="right", padx=(0, px(8)))
        ttk.Button(bin_top, text=T('状態を確認し直す'), command=self.check_bin).pack(side="right", padx=(0, px(8)))
        ttk.Label(bin_tab, style="Muted.TLabel", wraplength=px(1000), justify="left",
                  text=T('Windows のごみ箱全体は扱いません（空にする・完全に削除する機能はありません）。並ぶのは v0.8 以降にこのアプリで移した項目だけで、ごみ箱の中の項目と照らし合わせて状態を出します。')
                  ).pack(anchor="w", pady=(0, px(6)))
        bin_filter = ttk.Frame(bin_tab)
        bin_filter.pack(fill="x", pady=(0, px(6)))
        ttk.Label(bin_filter, text=T('検索')).pack(side="left")
        self.bin_search = tk.StringVar(value="")
        ttk.Entry(bin_filter, textvariable=self.bin_search, width=26).pack(side="left", padx=(px(8), px(12)))
        self.bin_search.trace_add("write", lambda *_: self.render_bin())
        ttk.Label(bin_filter, text=T('表示')).pack(side="left")
        self.bin_mode = ttk.Combobox(bin_filter, state="readonly", width=22,
                                     values=[T('すべて'), T('ごみ箱にある（戻せる）'), T('見つからない・特定できない'), T('復元済み')])
        self.bin_mode.current(0)
        self.bin_mode.pack(side="left", padx=(px(8), px(12)))
        self.bin_mode.bind("<<ComboboxSelected>>", lambda _e: self.render_bin())
        ttk.Label(bin_filter, text=T('種別')).pack(side="left")
        self.bin_kind = ttk.Combobox(bin_filter, state="readonly", width=10, values=[T('すべて'), T('ファイル'), T('フォルダ')])
        self.bin_kind.current(0)
        self.bin_kind.pack(side="left", padx=(px(8), 0))
        self.bin_kind.bind("<<ComboboxSelected>>", lambda _e: self.render_bin())
        ttk.Button(bin_filter, text=T('記録から外す…'), command=self.forget_bin_selected).pack(side="right")
        self.bin_count = tk.StringVar(value="")
        ttk.Label(bin_filter, textvariable=self.bin_count, style="Strong.TLabel").pack(side="right", padx=px(12))
        self.bin_stale_bar = ttk.Frame(bin_tab)
        self.bin_stale = tk.StringVar(value="")
        ttk.Label(self.bin_stale_bar, textvariable=self.bin_stale, style="Warn.TLabel").pack(side="left")
        ttk.Button(self.bin_stale_bar, text=T('もう一度スキャン'), command=self.start_scan).pack(side="left", padx=px(10))
        self.bin_detail = tk.StringVar(value="")
        ttk.Label(bin_tab, textvariable=self.bin_detail, style="Muted.TLabel", wraplength=px(1000),
                  justify="left").pack(side="bottom", anchor="w", pady=(px(4), 0))
        bw = ttk.Frame(bin_tab)
        bw.pack(fill="both", expand=True)
        cols = [("state", T('状態'), 150, "w"), ("kind", T('種別'), 70, "w"), ("size", T('容量'), 90, "e"),
                ("moved", T('移動日時'), 130, "w"), ("place", T('元の場所'), 460, "w")]
        self.bin_tree = ttk.Treeview(bw, columns=[c[0] for c in cols], show="tree headings", selectmode="extended")
        self.bin_tree.heading("#0", text=T('名前'), anchor="w")
        self.bin_tree.column("#0", width=px(240), stretch=False)
        for col, title, width, anchor in cols:
            self.bin_tree.heading(col, text=title, anchor=anchor)
            self.bin_tree.column(col, width=px(width), anchor=anchor, stretch=(col == "place"))
        bsy = ttk.Scrollbar(bw, orient="vertical", command=self.bin_tree.yview)
        bsx = ttk.Scrollbar(bw, orient="horizontal", command=self.bin_tree.xview)
        self.bin_tree.configure(yscrollcommand=bsy.set, xscrollcommand=bsx.set)
        self.bin_tree.grid(row=0, column=0, sticky="nsew")
        bsy.grid(row=0, column=1, sticky="ns")
        bsx.grid(row=1, column=0, sticky="ew")
        bw.rowconfigure(0, weight=1)
        bw.columnconfigure(0, weight=1)
        self.bin_tree.tag_configure("ng", foreground=P["faint"])
        self.bin_tree.tag_configure("done", foreground=P["muted"])
        self.bin_tree.bind("<<TreeviewSelect>>", lambda _e: self._bin_show_selection())
        self.bin_tree.bind("<Return>", lambda _e: self.restore_selected())
        self.tabs.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())

        self._toolbars = [header, controls, folder_top, dup_top, dup_sel, self.treemap.back_btn.master,
                          self.file_check_bar, self.folder_check_bar]
        self._update_check_views()
        self._render_filter_bar()
        self.treemap._update_nav()      # 階層・容量の関係・中身の一覧も数え直す
        self.treemap.redraw()
        self._update_sort_arrows()

    def _make_table(self, parent, columns, on_sort, selectmode: str = "browse"):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=[c[0] for c in columns], show="headings", selectmode=selectmode)
        tree._titles = {}
        for col, title, width, anchor, stretch in columns:
            tree.heading(col, text=title, anchor=anchor, command=lambda c=col: on_sort(c))
            tree.column(col, width=px(width), minwidth=px(50), anchor=anchor, stretch=stretch)
            tree._titles[col] = title
        tree.tag_configure("odd", background=P["stripe"])
        sy = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        sx = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        return tree, wrap

    def _update_sort_arrows(self):
        """並べ替え中の列見出しに ▲▼ を付ける。"""
        for tree, key in ((self.file_tree, "files"), (self.folder_tree, "folders")):
            col, desc = self.sort_state[key]
            for c, title in tree._titles.items():
                # フォルダ一覧の名前は木の列（#0）にある
                target = "name" if c == "#0" else c
                tree.heading(c, text=f"{title} {'▼' if desc else '▲'}" if target == col else title)

    def min_content_width(self) -> int:
        """各行の必要幅のうち最大のもの（＋左右の余白）。"""
        return max(r.winfo_reqwidth() for r in self._toolbars) + px(64)

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.current_root))
        if folder:
            self.current_root = Path(folder)
            self.root_label.set(str(self.current_root))
            self.status.set(T('「スキャン」を押してください'))
            self._refresh_disk_summary()

    def _refresh_disk_summary(self):
        try:
            usage = shutil.disk_usage(self.current_root)
            pct = usage.used / usage.total * 100 if usage.total else 0
            self.summary_vars["disk"].set(
                T('{0} / {1} 使用（{2:.0f}%）', human_size(usage.used), human_size(usage.total), pct)
            )
            self.disk_bar.configure(value=pct)
        except Exception:
            self.summary_vars["disk"].set(T('取得できません'))
            self.disk_bar.configure(value=0)

    def start_scan(self):
        if self.current_scan and self.current_scan.is_alive():
            return

        self.file_rows = []
        self.folder_rows = []
        self._clear_tree(self.file_tree)
        self._file_iids = {}
        self._clear_tree(self.folder_tree)
        self.filters.clear()
        self.search_text.set("")
        self._render_filter_bar()
        if self.detail:
            self.detail.withdraw()

        # 前回の重複結果は、別のスキャンをしたら消す（古い結果を残さない）
        self.dup_cancel.set()
        self.dup_running = False
        self.dup_result = None
        self._dup_reset()
        self.check_files.clear()
        self.check_dirs.clear()
        if hasattr(self, "check_note"):
            self._update_check_views()
        if hasattr(self, "dup_tree"):
            self.render_dups()
            self.dup_note.set(T('スキャンが終わったら「重複ファイルを探す」を押してください。'))
            self.dup_note_extra.set("")

        self.cancel_event.clear()
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status.set(T('ファイルを走査しています…'))

        self.current_scan = Scanner(self.current_root, self.scan_queue, self.cancel_event)
        self.current_scan.start()

    def destroy(self):
        # 閉じたあとに残りのタイマーが動かないよう止める
        try:
            self.after_cancel(self._poll_id)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    def cancel_scan(self):
        self.cancel_event.set()
        self.status.set(T('中止しています…'))

    def _poll_queue(self):
        # 窓を閉じたあとにも残りのタイマーが来る。そのとき触ると Tcl が騒ぐので、先に降りる。
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, payload = self.scan_queue.get_nowait()
                if kind == "progress":
                    count, total, current = payload
                    self.status.set(T('走査中: {0:,}ファイル / {1}  |  {2}', count, human_size(total), current))
                elif kind == "cancelled":
                    self.status.set(T('スキャンを中止しました'))
                    self.scan_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                elif kind == "done":
                    self._show_result(payload)
                    self.scan_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                elif kind == "dup_progress":
                    if self.dup_running:
                        self._dup_progress = payload
                        self._dup_running_note()
                elif kind == "bin_checked":
                    self._apply_bin_check(payload)
                elif kind == "dup_partial":
                    if self.dup_running:
                        self._add_partial_groups(payload)
                elif kind in ("dup_done", "dup_cancelled"):
                    self.dup_running = False
                    self._show_dup_result(payload)
                    self.dup_btn.config(state="normal")
                    self.dup_cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self._poll_id = self.after(150, self._poll_queue)

    def _show_result(self, result):
        self.scan_root = self.current_root
        self._stale_restored = 0
        if hasattr(self, "bin_stale_bar"):
            self.bin_stale_bar.pack_forget()
        self.file_rows = list(result["files"])
        self.folder_sizes = result["folder_sizes"]
        self.folder_file_counts = result["folder_file_counts"]
        self.folder_ctx = result["folder_ctx"]
        self.root_parent_ctx = result["root_parent_ctx"]

        # 索引: フォルダ -> 直下のファイル、グループ -> 表示名
        dir_index = defaultdict(list)
        for f in self.file_rows:
            dir_index[f.dir].append(f)
        self.dir_index = dir_index
        self._dir_lower = {d: d.lower() for d in dir_index}
        self._folder_lookup = {os.path.normcase(str(p)): p for p in self.folder_sizes}
        self.group_labels = {
            ctx.group_key: ctx.group_label
            for ctx in self.folder_ctx.values() if ctx.group_key
        }

        self.folder_rows = [
            (p, result["folder_sizes"].get(p, 0), result["folder_file_counts"].get(p, 0),
             self.ctx_for(str(p)).group_label or "")
            for p in result["folder_sizes"].keys()
            if p != self.current_root
        ]

        self.total_size = result["total_size"]
        self.summary_vars["scanned"].set(human_size(result["total_size"]))
        self.summary_vars["files"].set(T('{0:,} ファイル', len(self.file_rows)))
        self.summary_vars["folders"].set(T('{0:,} フォルダ', len(self.folder_rows)))

        self.treemap.set_data(
            self.current_root,
            result["folder_sizes"],
            result["direct_file_sizes"],
            result["folder_file_counts"],
        )

        if self.location_picker is not None and self.location_picker.winfo_exists():
            self.location_picker.reset()

        self.render_folders()
        self.render_files()

        self.status.set(
            T('完了: {0:,}ファイル / {1}', len(self.file_rows), human_size(result["total_size"]))
            + self._skipped_text(result.get("skipped") or {"file_errors": result["errors"]})
        )

    @staticmethod
    def _skipped_text(skipped: dict) -> str:
        """除外・失敗した件数（0 件の項目は出さない）。"""
        labels = [("file_errors", T('情報を取得できなかったファイル')), ("dir_errors", T('読み取れなかったフォルダ')),
                  ("links", T('辿らなかったリンク')), ("excluded", T('除外したフォルダ'))]
        parts = [T('{0} {1:,}件', text, skipped[key]) for key, text in labels if skipped.get(key)]
        return "  ·  " + T('・').join(parts) if parts else ""

    # --- 索引の参照 ---
    def ctx_for(self, folder: str) -> FolderContext:
        """スキャン済みのフォルダ文脈。未走査（権限なし等）の場合は最も近い親のもの。"""
        cur = folder
        while True:
            ctx = self.folder_ctx.get(cur)
            if ctx is not None:
                return ctx
            parent = os.path.dirname(cur)
            if parent == cur:
                return self.root_parent_ctx or FolderContext()
            cur = parent

    def sibling_names(self, folder: str) -> set[str]:
        return {f.name.lower() for f in self.dir_index.get(folder, [])}

    def folder_breakdown(self, folder: str) -> dict[str, tuple[int, int]]:
        prefix = subtree_prefix(folder)
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for d, items in self.dir_index.items():
            if d == folder or d.startswith(prefix):
                for f in items:
                    s = stats[f.kind]
                    s[0] += f.size
                    s[1] += 1
        return {k: (v[0], v[1]) for k, v in stats.items()}

    def folder_latest(self, folder: str) -> float | None:
        prefix = subtree_prefix(folder)
        latest = None
        for d, items in self.dir_index.items():
            if d == folder or d.startswith(prefix):
                for f in items:
                    if latest is None or f.modified > latest:
                        latest = f.modified
        return latest

    def folder_badge(self, folder: Path) -> str | None:
        """容量マップの四角に添える短い説明。"""
        fs = str(folder)
        ctx = self.folder_ctx.get(fs)
        if ctx is None:
            return None
        if ctx.container and ctx.container_root == fs:
            return {"deps": T('開発: 依存・生成物'), "vcs": T('開発: 変更履歴'), "cache": T('キャッシュ'),
                    "logs": T('ログ'), "saves": T('セーブデータ?'), "backup": T('バックアップ')}[ctx.container]
        if ctx.group_key == fs and ctx.group_label:
            return L(ctx.group_label)
        return None

    # --- 詳細画面 ---
    def _detail_window(self) -> DetailWindow:
        if self.detail is None or not self.detail.winfo_exists():
            self.detail = DetailWindow(self)
        return self.detail

    def show_file_detail(self, item: FileItem):
        self._detail_window().show_file(item)

    def show_folder_detail(self, folder: Path | None):
        if folder is None or not self.folder_sizes:
            return
        self._detail_window().show_folder(folder)

    def reveal_in_folder_list(self, folder: Path):
        """フォルダ一覧でその場所を開いて選ぶ（容量マップの右クリックから）。"""
        self.reveal_folder(folder)
        self.tabs.select(self.folders_tab)

    def show_in_map(self, folder: Path):
        """容量マップでその場所を開く。

        フォルダ一覧でも同じ所を開いておく。別の操作で同じ場所へ行ったのに、
        一覧では別の階層が開いたまま——という食い違いを作らないため。
        """
        self.treemap.navigate(folder)
        if hasattr(self, "folder_tree"):
            self.reveal_folder(folder)
        self.tabs.select(self.overview_tab)
        self.raise_window(self)

    # --- 絞り込み ---
    # 条件の状態は self.filters（kind / group / folder）と self.search_text のみ。
    # 詳細画面・常設UI・条件表示の✕はすべてここを書き換えてから render_files() を呼ぶ。
    def apply_filter(self, key: str, value):
        self.apply_filter_many({key: value})

    def apply_filter_many(self, conditions: dict):
        """詳細画面などから: 条件を設定してファイル一覧を表示する。

        「位置」を指したときは、フォルダ一覧でも同じ場所を開いておく
        （どちらの画面を見ても、同じ場所が同じように開いている状態にする）。
        """
        self.filters.update(conditions)
        loc = conditions.get("folder")
        if loc and hasattr(self, "folder_tree"):
            folder = Path(loc[0] if isinstance(loc, tuple) else loc)
            if folder in self.folder_sizes:
                self.reveal_folder(folder)
        self.render_files()
        self.tabs.select(self.files_tab)
        self.raise_window(self)

    def set_filter(self, key: str, value):
        """一覧上の常設UIから: 条件を設定する（value が None なら解除）。"""
        if value is None:
            self.filters.pop(key, None)
        else:
            self.filters[key] = value
        self.render_files()

    def remove_filter(self, key: str):
        self.set_filter(key, None)

    def clear_search(self):
        self.search_text.set("")
        self.render_files()

    def clear_filters(self):
        self.filters.clear()
        self.search_text.set("")
        self.render_files()

    @staticmethod
    def kind_name(kind: str) -> str:
        return T('種類不明') if kind == "unknown" else kind_label(kind)

    def filter_label(self, key: str, value) -> str:
        if key == "kind":
            return T('種類: {0}', self.kind_name(value))
        if key == "group":
            return T('関連: {0}', L(self.group_labels.get(value, value)))
        if key == "folder":
            folder, recursive = value
            shown = folder if len(folder) <= 48 else "…" + folder[-47:]
            return T('位置: {0}（{1}）', shown, T('配下すべて') if recursive else T('直下のみ'))
        return f"{key}: {value}"

    def _render_filter_bar(self, kind_counts: dict[str, int] | None = None):
        """条件表示と常設コントロールを self.filters / search_text に合わせる。"""
        for w in self.filter_bar.winfo_children():
            w.destroy()
        q = self.search_text.get().strip()
        if not self.filters and not q:
            ttk.Label(self.filter_bar, text=T('条件なし — 検索・種類・位置、または詳細画面の「同じ種類を表示」などで絞り込めます'),
                      style="Muted.TLabel").pack(side="left")
        else:
            ttk.Label(self.filter_bar, text=T('条件'), style="Muted.TLabel").pack(side="left", padx=(0, px(8)))
            for key in ("kind", "folder", "group"):
                if key in self.filters:
                    ttk.Button(self.filter_bar, text=f"{self.filter_label(key, self.filters[key])}  ✕", style="Chip.TButton",
                               command=lambda k=key: self.remove_filter(k)).pack(side="left", padx=(0, px(6)))
            if q:
                ttk.Button(self.filter_bar, text=T('検索: {0}  ✕', q), style="Chip.TButton",
                           command=self.clear_search).pack(side="left", padx=(0, px(6)))
            ttk.Button(self.filter_bar, text=T('すべて解除'), command=self.clear_filters).pack(side="left", padx=(px(6), 0))

        # 種類: 種類以外の条件で絞った範囲での件数を添える
        counts = kind_counts or {}
        selected = self.filters.get("kind")
        kinds = [k for k in KINDS if counts.get(k) or k == selected]
        total = sum(counts.values())
        self._kind_choices = [None] + kinds
        self.kind_combo.configure(values=[T('すべての種類（{0:,}件）', total)] +
                                  [T('{0}（{1:,}件）', self.kind_name(k), counts.get(k, 0)) for k in kinds])
        self.kind_combo.current(self._kind_choices.index(selected))

        # 位置: 候補はスキャン対象と直下のフォルダ（容量順）
        folder = self.filters.get("folder")
        if self.scan_root is not None:
            tops = sorted(self.treemap.child_dirs.get(self.scan_root, []),
                          key=lambda p: self.folder_sizes.get(p, 0), reverse=True)
            self.location_combo.configure(values=[str(self.scan_root)] + [str(p) for p in tops])
        self.location_combo.set(folder[0] if folder else "")
        self.location_direct.set(bool(folder) and not folder[1])

    def _on_kind_selected(self):
        idx = self.kind_combo.current()
        if 0 <= idx < len(self._kind_choices):
            self.set_filter("kind", self._kind_choices[idx])

    def resolve_folder(self, text: str) -> Path | None:
        """入力されたパスをスキャン済みのフォルダに対応付ける（大文字小文字・区切り・相対パスを許容）。"""
        text = text.strip().strip('"').strip()
        if not text or self.scan_root is None:
            return None
        p = Path(text)
        if not p.is_absolute():
            p = self.scan_root / p
        return self._folder_lookup.get(os.path.normcase(os.path.normpath(str(p))))

    def _on_location_entered(self):
        text = self.location_combo.get()
        if not text.strip():
            self.set_filter("folder", None)
            return
        folder = self.resolve_folder(text)
        if folder is None:
            self._filter_message = T('「{0}」はスキャン済みのフォルダに見つかりません。', text.strip())
            self.render_files()
            return
        self.set_filter("folder", (str(folder), not self.location_direct.get()))

    def _on_location_direct_toggled(self):
        folder = self.filters.get("folder")
        if folder:
            self.set_filter("folder", (folder[0], not self.location_direct.get()))

    def open_location_picker(self):
        if not self.folder_sizes:
            return
        if self.location_picker is None or not self.location_picker.winfo_exists():
            self.location_picker = LocationPicker(self)
        folder = self.filters.get("folder")
        self.location_picker.show(Path(folder[0]) if folder else self.scan_root)

    def _filtered_rows(self) -> tuple[list[FileItem], dict[str, int]]:
        """全条件を AND で適用する。種類以外の条件で絞った段階の種類別件数も返す。"""
        rows = self.file_rows
        f = self.filters
        if "folder" in f:
            folder, recursive = f["folder"]
            if recursive:
                prefix = subtree_prefix(folder)
                rows = [item for d, items in self.dir_index.items()
                        if d == folder or d.startswith(prefix) for item in items]
            else:
                rows = self.dir_index.get(folder, [])
        if "group" in f:
            g = f["group"]
            rows = [r for r in rows if r.group == g]
        q = self.search_text.get().strip().lower()
        if q:
            dl = self._dir_lower
            rows = [r for r in rows if q in r.name.lower() or q in dl.get(r.dir, "")]
        kind_counts = Counter(r.kind for r in rows)
        if "kind" in f:
            k = f["kind"]
            rows = [r for r in rows if r.kind == k]
        if self.checked_only.get():
            rows = [r for r in rows if id(r) in self.check_files]
        return rows, kind_counts

    def _schedule_render_files(self):
        # 入力のたびに全件を処理しないよう、少し待ってからまとめて絞り込む。
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = self.after(250, self.render_files)

    def _clear_tree(self, tree):
        tree.delete(*tree.get_children())

    def sort_files(self, column):
        current_col, desc = self.sort_state["files"]
        self.sort_state["files"] = (column, not desc if column == current_col else True)
        self.render_files()

    def render_files(self):
        if self._search_after:
            self.after_cancel(self._search_after)
            self._search_after = None
        self._clear_tree(self.file_tree)
        self._file_iids = {}
        rows, kind_counts = self._filtered_rows()
        self._render_filter_bar(kind_counts)

        col, desc = self.sort_state["files"]
        groups = self.group_labels

        def key(f):
            if col == "name":
                return f.name.lower()
            if col == "check":
                return (id(f) in self.check_files, f.size)
            if col == "size":
                return f.size
            if col == "modified":
                return f.modified
            if col == "kind":
                return kind_label(f.kind)
            if col == "group":
                return L(groups.get(f.group, "")) if f.group else ""
            if col == "ext":
                return f.ext
            if col == "path":
                return (f.dir.lower(), f.name.lower())
            return f.size

        # 全件を並べ替えず、表示する上位だけを取り出す。
        if len(rows) > FILE_DISPLAY_LIMIT:
            pick = heapq.nlargest if desc else heapq.nsmallest
            shown = pick(FILE_DISPLAY_LIMIT, rows, key=key)
        else:
            shown = sorted(rows, key=key, reverse=desc)

        prefixes = self._check_prefixes()
        for n, f in enumerate(shown):
            mark = self._check_mark("file", f, prefixes)
            iid = self.file_tree.insert(
                "", "end", tags=self._tags_with_mark(("row_odd",) if n % 2 else (), mark),
                values=(
                    mark,
                    f.name,
                    human_size(f.size),
                    fmt_date(f.modified),
                    self.kind_name(f.kind),
                    L(groups.get(f.group, "")) if f.group else "",
                    f.ext or T('(なし)'),
                    str(f.path),
                )
            )
            self._file_iids[iid] = f

        if self.file_rows:
            total = sum(r.size for r in rows)
            note = T('該当 {0:,}件 / {1}', len(rows), human_size(total))
            if self._filter_message:
                note = f"{self._filter_message}  {note}"
            self.file_note.set(note)
            self.file_note_extra.set(
                T('並べ替え順の上位 {0:,} 件を表示しています。条件を絞ると残りも表示できます。', len(shown))
                if len(rows) > len(shown) else "")
        else:
            self.file_note.set("")
            self.file_note_extra.set("")
        self._update_sort_arrows()
        self._filter_message = ""

    def sort_folders(self, column):
        current_col, desc = self.sort_state["folders"]
        self.sort_state["folders"] = (column, not desc if column == current_col else True)
        self.render_folders()

    def render_folders(self):
        """フォルダ一覧を階層で描く。

        まずスキャン対象の直下だけを出し、▶ を押した所だけ中を読む（`_on_folder_open`）。
        全階層を先に広げると、深いフォルダで一覧が数万行になって使いものにならない。

        中身は **スキャン結果から作る**（ディスクは読み直さない）ので、
        容量マップ・ファイル一覧・位置の絞り込みと同じ数字になる。
        """
        self._clear_tree(self.folder_tree)
        self._folder_children_done: set[str] = set()
        self._folder_file_rows: dict[str, FileItem] = {}
        self.folder_note.set(T('{0:,} フォルダ', len(self.folder_rows)) if self.folder_rows else "")
        self._update_sort_arrows()
        if self.scan_root is None:
            self._update_folder_here()
            return
        root = self.scan_root
        self.folder_tree.insert(
            "", "end", iid=str(root), text=root.name or str(root), open=True,
            values=(self._check_mark("dir", root), human_size(self.folder_sizes.get(root, 0)),
                    f"{self.folder_file_counts.get(root, 0):,}",
                    L(self.ctx_for(str(root)).group_label) or "", str(root)))
        self._fill_folder_children(str(root))
        self._update_folder_here()
        self._update_folder_selection_note()

    def _folder_sort_key(self):
        """一覧の並べ替え設定を、階層のきょうだいの並びにも使う。"""
        col, desc = self.sort_state["folders"]

        def key(entry):
            kind, path, size, count, group = entry
            if col == "name":
                return os.path.basename(path).lower()
            if col == "count":
                return count
            if col == "group":
                return group or ""
            if col == "path":
                return path.lower()
            return size
        return key, desc

    def _fill_folder_children(self, parent_path: str):
        """その直下のフォルダとファイルを、一覧へ並べる。"""
        if parent_path in self._folder_children_done:
            return
        self._folder_children_done.add(parent_path)
        parent = Path(parent_path)
        entries = []
        for child in self.treemap.child_dirs.get(parent, []):
            entries.append(("dir", str(child), self.folder_sizes.get(child, 0),
                            self.folder_file_counts.get(child, 0),
                            L(self.ctx_for(str(child)).group_label) or ""))
        for f in self.dir_index.get(parent_path, []):
            entries.append(("file", str(f.path), f.size, 0,
                            L(self.group_labels.get(f.group, f.group)) if f.group else ""))
        key, desc = self._folder_sort_key()
        entries.sort(key=key, reverse=desc)
        # 一度に出す数は抑える（深い所に数万ファイルが入っていることがある）
        shown = entries[:FOLDER_DISPLAY_LIMIT]
        for kind, path, size, count, group in shown:
            if kind == "dir":
                mark = self._check_mark("dir", Path(path))
                node = self.folder_tree.insert(
                    parent_path, "end", iid=path, text=os.path.basename(path) or path,
                    tags=self._tags_with_mark((), mark),
                    values=(mark, human_size(size), f"{count:,}", group, path))
                if self.treemap.child_dirs.get(Path(path)) or self.dir_index.get(path):
                    # 中身があることを ▶ で示す（開いたときに本当の中身を入れる）
                    self.folder_tree.insert(node, "end", iid=path + PLACEHOLDER_SUFFIX, text="…")
            else:
                item = next((f for f in self.dir_index.get(parent_path, []) if str(f.path) == path), None)
                iid = "f:" + path
                mark = self._check_mark("file", item) if item is not None else ""
                self.folder_tree.insert(
                    parent_path, "end", iid=iid, text=os.path.basename(path),
                    tags=self._tags_with_mark((), mark),
                    values=(mark, human_size(size), "", group, path))
                if item is not None:
                    self._folder_file_rows[iid] = item
        if len(entries) > len(shown):
            self.folder_tree.insert(parent_path, "end", iid=parent_path + "|more", text="…",
                                    values=("", "", "", "", T('ほか {0:,} 件（上位のみ表示）', len(entries) - len(shown))))

    def _on_folder_open(self, _event=None):
        """▶ を押したとき、その直下を読み込む（仮の子は入れ替える）。"""
        iid = self.folder_tree.focus()
        if not iid or iid.startswith("f:"):
            return
        for child in self.folder_tree.get_children(iid):
            if child.endswith(PLACEHOLDER_SUFFIX):
                self.folder_tree.delete(child)
        self._fill_folder_children(iid)
        self._update_folder_here()

    def _folder_row_target(self, iid: str):
        """行が、フォルダなのかファイルなのか。("dir", Path) / ("file", FileItem) / (None, None)"""
        if iid.startswith("f:"):
            item = self._folder_file_rows.get(iid)
            return ("file", item) if item is not None else (None, None)
        if iid.endswith("|more") or iid.endswith(PLACEHOLDER_SUFFIX):
            return None, None
        return "dir", Path(iid)

    def _selected_folder_row(self):
        """操作の中心になる 1 行（複数選択中は、最後に触った行）。"""
        sel = self.folder_tree.selection()
        if not sel:
            return None, None
        focus = self.folder_tree.focus()
        return self._folder_row_target(focus if focus in sel else sel[0])

    def _selected_folder_rows(self) -> list[tuple[str, object]]:
        """選んでいる行すべて（見た目の並び順）。"""
        out = []
        for iid in self.folder_tree.selection():
            kind, target = self._folder_row_target(iid)
            if kind is not None:
                out.append((kind, target))
        return out

    @staticmethod
    def _dedupe_nested(rows):
        """選んだフォルダの中にある行を外す（フォルダごと移すので二重に数えない）。

        返り値: (フォルダ, ファイル, 外した件数)
        """
        folders = [t for k, t in rows if k == "dir"]
        prefixes = [subtree_prefix(os.path.normcase(str(f))) for f in folders]

        def covered(path: str, own: str | None = None) -> bool:
            p = os.path.normcase(path)
            return any(p.startswith(pre) for pre in prefixes if pre != own)

        kept_dirs = [f for f in folders
                     if not covered(str(f), subtree_prefix(os.path.normcase(str(f))))]
        files = [t for k, t in rows if k == "file"]
        kept_files = [i for i in files if not covered(str(i.path))]
        return kept_dirs, kept_files, (len(folders) - len(kept_dirs)) + (len(files) - len(kept_files))

    def _update_folder_selection_note(self):
        """選択の件数と合計容量（フォルダは中身ごと）。何が操作対象かを取り違えないために出す。"""
        rows = self._selected_folder_rows()
        if not rows:
            self.folder_selection_note.set("")
            return
        folders, files, _nested = self._dedupe_nested(rows)
        total = sum(self.folder_sizes.get(f, 0) for f in folders) + sum(i.size for i in files)
        if len(rows) == 1:
            self.folder_selection_note.set(T('選択 1 件（{0}）', human_size(total)))
            return
        parts = [T('フォルダ {0:,}', len(folders))] if folders else []
        if files:
            parts.append(T('ファイル {0:,}', len(files)))
        self.folder_selection_note.set(T('選択 {0:,} 件（{1}）・合計 {2}', len(rows), T('・').join(parts), human_size(total)))

    def _update_folder_here(self):
        """今見ている場所と、親へ戻れるかどうかを出す。"""
        kind, target = self._selected_folder_row()
        if kind == "file" and target is not None:
            here = Path(target.dir)
        elif kind == "dir" and target is not None:
            here = target
        else:
            here = self.scan_root
        if here is None:
            self.folder_here.set("—")
            self.folder_up_btn.configure(state="disabled")
            return
        self.folder_here.set(str(here))
        can_up = (self.scan_root is not None and here != self.scan_root
                  and here.parent in self.folder_sizes)
        self.folder_up_btn.configure(state="normal" if can_up else "disabled")

    def open_parent_folder(self):
        """一つ上へ。一覧の中で選び直すだけで、別の画面へは飛ばない。"""
        kind, target = self._selected_folder_row()
        here = Path(target.dir) if kind == "file" and target else target
        if here is None or self.scan_root is None or here == self.scan_root:
            return
        parent = here.parent
        iid = str(parent)
        if self.folder_tree.exists(iid):
            self.folder_tree.see(iid)
            self.folder_tree.selection_set(iid)
            self.folder_tree.focus(iid)
        self._update_folder_here()

    def reveal_folder(self, folder: Path):
        """その場所を一覧の中で開いて選ぶ（親を順に広げる）。

        容量マップや位置の絞り込みから来ても、**同じ場所が同じように開く**ようにする。
        """
        if self.scan_root is None or folder not in self.folder_sizes:
            return False
        chain = []
        cur = folder
        while cur != self.scan_root and cur != cur.parent:
            chain.append(cur)
            cur = cur.parent
        chain.append(self.scan_root)
        for p in reversed(chain):
            iid = str(p)
            if not self.folder_tree.exists(iid):
                return False
            for child in self.folder_tree.get_children(iid):
                if child.endswith(PLACEHOLDER_SUFFIX):
                    self.folder_tree.delete(child)
            self._fill_folder_children(iid)
            self.folder_tree.item(iid, open=True)
        iid = str(folder)
        if self.folder_tree.exists(iid):
            self.folder_tree.see(iid)
            self.folder_tree.selection_set(iid)
            self.folder_tree.focus(iid)
            self._update_folder_here()
            return True
        return False

    def filter_to_selected_folder(self):
        """選んだ場所で、ファイル一覧を絞る（既存の「位置」の絞り込みをそのまま使う）。"""
        kind, target = self._selected_folder_row()
        folder = Path(target.dir) if kind == "file" and target else target
        if folder is None:
            return
        self.apply_filter("folder", (str(folder), False))

    # --- 一覧からの操作 ---
    # ------------------------------------------------------------------ 重複

    DUP_PAGE_GROUPS = 100   # 1 ページに描く組の数（数万組でも一覧を重くしない）

    def _dup_reset(self):
        """重複結果に付いていた選択（残す・チェック・判定）をすべて捨てる。"""
        self.dup_keep.clear()
        self.dup_keep_auto.clear()
        self.dup_checked.clear()
        self.dup_origin.clear()
        self.dup_assess.clear()
        self.dup_fail_note.clear()
        self.dup_page = 0
        self._dup_anchor = None

    def start_dup_scan(self):
        """重複検出を始める。通常スキャンとは別で、押したときだけ走る。"""
        if self.current_dup and self.current_dup.is_alive():
            return
        if not self.file_rows:
            self.dup_note.set(T('先に「スキャン」でフォルダを読み込んでください。'))
            return
        # 閉じた画面の後始末を、ここ（画面のスレッド）で済ませておく。読み込み用のスレッドの中で
        # 後始末が走ると、Tk の部品を別スレッドから触ることになるため。
        gc.collect()
        # 確定した組から順に表示する（途中結果）。検出中は見るだけで、チェック・移動はできない。
        self.dup_result = duplicates.DupResult([], duplicates.DupSkipped(), 0, 0)
        self.dup_running = True
        self._dup_progress = None
        self._dup_started = time.monotonic()
        self._dup_reset()
        self.render_dups()
        self.dup_cancel.clear()
        self.dup_btn.config(state="disabled")
        self.dup_cancel_btn.config(state="normal")
        floor = self._dup_min_bytes()
        target = [f for f in self.file_rows if f.size >= floor]
        self.dup_note.set(T('検出中: {0:,} ファイルの中身を確かめています…', len(target)))
        self.dup_note_extra.set(
            T('容量が同じもの同士だけを候補にし、先頭と末尾を読んで振り分けてから、残ったものだけ全体を読みます。ファイルを開く回数だけ時間がかかります。')
            + (T('（{0} 未満の {1:,} ファイルは調べていません）', human_size(floor), len(self.file_rows) - len(target)) if floor > 1 else ""))
        self.current_dup = DupScanner(list(self.file_rows), self.scan_queue, self.dup_cancel,
                                      min_size=self._dup_min_bytes())
        self.current_dup.start()

    # 「調べる大きさ」の選択肢（表示の文言ではなく、何番目を選んだかで決める。言語を切り替えても同じ）
    _DUP_MIN_CHOICES = [1, 1024 ** 2, 10 * 1024 ** 2, 100 * 1024 ** 2]

    def _dup_min_bytes(self) -> int:
        i = self.dup_min_size.current()
        return self._DUP_MIN_CHOICES[i] if 0 <= i < len(self._DUP_MIN_CHOICES) else 1

    def cancel_dup_scan(self):
        self.dup_cancel.set()
        self.dup_note.set(T('中止しています…（読みかけのファイルが終わりしだい止めます）'))

    def _dup_running_note(self):
        """検出中の表示。確定した分と、まだ確かめていない分をはっきり分ける。"""
        r = self.dup_result
        n_groups = len(r.groups) if r else 0
        reclaim = r.reclaimable if r else 0
        phase, done, total = "edges", 0, 0
        if self._dup_progress:
            phase, done, total = self._dup_progress[:3]
        what = {"edges": T('中身を確かめています'), "hash": T('中身全体を読み比べています')}.get(phase, T('確かめています'))
        elapsed = time.monotonic() - getattr(self, "_dup_started", time.monotonic())
        self.dup_note.set(
            T('検出中（途中結果）: {0}… 候補 {1:,} / {2:,} 件  ／  ここまでに確定した組 {3:,} ・ その分で空く見込み {4}  ／  {5:.0f} 秒', what, done, total, n_groups, human_size(reclaim), elapsed))
        self.dup_note_extra.set(
            T('まだ確かめていない候補が残っています。表示中の組数・容量は全体の結果ではありません。効き目（容量 × 本数）の大きい候補から確かめ、中身全体が一致した組だけを順に並べています。検出中はチェック・移動はできません（終わるか中止すると操作できます）。'))

    def _add_partial_groups(self, new_groups):
        """確定した組を途中結果に加える。表示中のページが埋まっていなければ描き足す。"""
        r = self.dup_result
        if r is None:
            return
        r.groups.extend(new_groups)
        page_full = len(self._dup_shown) >= self.DUP_PAGE_GROUPS
        if not page_full:
            self.render_dups()
        else:
            self._update_dup_page_note()
        self._dup_running_note()

    def _dup_busy(self) -> bool:
        """検出中は、選択・移動を受け付けない（組の一覧が変わり続けているため）。"""
        if self.dup_running:
            self.dup_note_extra.set(T('検出中はチェック・移動はできません。終わるか「中止」を押すと操作できます。'))
            return True
        return False

    def _show_dup_result(self, result):
        """検出結果を組ごとに並べる。読めなかったものは別に数えて出す。"""
        self.dup_result = result
        self._dup_reset()
        self.render_dups()
        head = T('中止しました（途中までの結果）') if result.cancelled else T('完了')
        self._dup_totals_note(head)
        lines = result.skipped.as_lines()
        extra = [T('「見かけの合計」は同じ中身を何度も数えた値で、これだけ空くわけではありません。')]
        floor = self._dup_min_bytes()
        if floor > 1:
            extra.insert(0, T('{0} 未満のファイルは調べていません（「調べる大きさ」で変えられます）。', human_size(floor)))
        if lines:
            extra.append(T('調べなかった・読めなかったもの: ') + T('・').join(lines)
                         + T('（これらは「重複なし」とは判断していません）'))
        if result.cancelled:
            extra.insert(0, T('中止したため、まだ確かめていない候補 {0:,} 件があります。上の組数・容量は全体の結果ではありません（ほかにも重複があるかもしれません）。', getattr(result, 'unchecked', 0)))
        t = getattr(result, "timings", None) or {}
        if t.get("wall_sec") is not None:
            extra.append(T('所要 {0:.1f} 秒。', t['wall_sec']))
        self.dup_note_extra.set("  ".join(extra))

    def _dup_totals_note(self, head: str = ""):
        r = self.dup_result
        if r is None:
            return
        prefix = f"{head}: " if head else ""
        if not r.groups:
            self.dup_note.set(prefix + (T('中身がまったく同じファイルの組は見つかりませんでした。') if head
                                        else T('残っている重複の組はありません。')))
        else:
            self.dup_note.set(
                T('{0}{1:,} 組 ・ {2:,} ファイル  ／  見かけの合計 {3}  ／  1 組に 1 本ずつ残すと空く容量 {4}', prefix, len(r.groups), r.duplicate_files, human_size(r.apparent_size), human_size(r.reclaimable)))

    # --- 一覧（ページ単位で描く） ---
    def _dup_view_groups(self) -> list:
        """絞り込み・表示の条件に合う組（検出結果の並び順のまま）。"""
        r = self.dup_result
        if not r:
            return []
        q = self.dup_filter.get().strip().lower()
        mode = self.dup_view_mode.current()
        checked = self.dup_checked
        out = []
        for g in r.groups:
            if q and not any(q in f.path.lower() for f in g.files):
                continue
            if mode > 0 and (mode == 1) != any(f.path in checked for f in g.files):
                continue
            out.append(g)
        return out

    def _schedule_dup_filter(self):
        if self._dup_filter_after:
            self.after_cancel(self._dup_filter_after)
        self._dup_filter_after = self.after(250, lambda: self._dup_goto(0))

    def _dup_goto(self, page: int):
        self._dup_filter_after = None
        self.dup_page = page
        self.render_dups()

    def render_dups(self):
        tree = self.dup_tree
        tree.delete(*tree.get_children(""))
        self._dup_rows.clear()
        self._dup_order = []
        self._dup_shown = []
        view = self._dup_view_groups()
        n = self.DUP_PAGE_GROUPS
        pages = max(1, -(-len(view) // n))
        self.dup_page = max(0, min(self.dup_page, pages - 1))
        start = self.dup_page * n
        self._dup_shown = view[start:start + n]
        for gi, g in enumerate(self._dup_shown, start=start):
            gid = f"g{gi}"
            tree.insert("", "end", iid=gid, open=True,
                        text=T('{0} 個が同じ中身  ({1} × {2})', g.count, human_size(g.size), g.count),
                        values=self._dup_group_values(g))
            self._dup_rows[gid] = ("group", g)
            for fi, f in enumerate(g.files):
                item = f.key if isinstance(f.key, FileItem) else None
                iid = f"{gid}f{fi}"
                values, tags = self._dup_file_values(g, f, item)
                tree.insert(gid, "end", iid=iid, text=os.path.basename(f.path), values=values, tags=tags)
                self._dup_rows[iid] = ("file", g, f, item)
                self._dup_order.append((iid, g, f))
        self._update_dup_page_note(view)
        self._update_dup_check_note()

    def _update_dup_page_note(self, view=None):
        if view is None:
            view = self._dup_view_groups()
        n = self.DUP_PAGE_GROUPS
        pages = max(1, -(-len(view) // n))
        start = self.dup_page * n
        total = len(self.dup_result.groups) if self.dup_result else 0
        if not self.dup_result:
            self.dup_page_note.set("")
        elif view:
            note = T('組 {0:,}–{1:,} / {2:,}', start + 1, start + len(self._dup_shown), len(view))
            if len(view) != total:
                note += T('（全 {0:,} 組のうち条件に合うもの）', total)
            if self.dup_running:
                note += T('（検出中・増えていきます）')
            self.dup_page_note.set(note)
        else:
            self.dup_page_note.set(T('検出中…') if self.dup_running else T('条件に合う組はありません'))
        self.dup_prev_btn.configure(state="normal" if self.dup_page > 0 else "disabled")
        self.dup_next_btn.configure(state="normal" if self.dup_page < pages - 1 else "disabled")

    def _dup_group_values(self, g):
        n = sum(1 for f in g.files if f.path in self.dup_checked)
        mark = "☑" if n and n == g.count - 1 else (f"▣ {n}" if n else "☐")
        return (mark, "", human_size(g.reclaimable), "", "", "",
                T('チェック {0} / {1}', n, g.count) if n else "", T('1 本残すと {0} 空きます', human_size(g.reclaimable)))

    def _dup_file_values(self, g, f, item):
        keep = self.dup_keep.get(g.digest) == f.path
        checked = f.path in self.dup_checked
        check = "—" if keep else ("☑" if checked else "☐")
        keep_mark = ("◎" if g.digest in self.dup_keep_auto else "◉") if keep else ""
        kind = self.kind_name(item.kind) if item else ""
        group = (L(self.group_labels.get(item.group, item.group)) if item and item.group else "")
        note = self.dup_fail_note.get(f.path) or self._dup_assess_label(f.path)
        tags = (("keep",) if keep else ()) + (("checked",) if checked else ())
        return (check, keep_mark, human_size(f.size), fmt_date(f.modified), kind, group, note, f.path), tags

    def _dup_assess_label(self, path: str) -> str:
        a = self.dup_assess.get(path)
        if a is None:
            return ""
        if a[0]:
            return T('低リスク候補')
        return T('個別確認: ') + T(dupselect.REASONS.get(a[1], a[1])).split(T('（'))[0]

    def _dup_refresh_rows(self):
        """描いてある行の表示だけを今の状態に合わせる（並びもスクロール位置も変えない）。"""
        tree = self.dup_tree
        for iid, entry in self._dup_rows.items():
            if entry[0] == "group":
                tree.item(iid, values=self._dup_group_values(entry[1]))
            else:
                values, tags = self._dup_file_values(entry[1], entry[2], entry[3])
                tree.item(iid, values=values, tags=tags)
        self._update_dup_check_note()

    def dup_scope_labels(self) -> list[str]:
        return [T(key, *args) for key, *args in self.dup_origin] or [T('個別に選んだもの')]

    def _dup_add_origin(self, key: str, *args):
        entry = (key, *args)
        if entry not in self.dup_origin:
            self.dup_origin.append(entry)

    def _update_dup_check_note(self):
        n = len(self.dup_checked)
        if not n:
            self.dup_check_note.set(T('チェックなし') if self.dup_result and self.dup_result.groups else "")
            return
        size = sum(v[1] for v in self.dup_checked.values())
        groups = len({v[0] for v in self.dup_checked.values()})
        self.dup_check_note.set(T('チェック中 {0:,} 件 ・ {1}（{2:,} 組）  範囲: {3}', n, human_size(size), groups, T(' ＋ ').join(self.dup_scope_labels())))

    # --- 残す・チェック（不変条件: チェックのある組には、チェックしていない「残す」が 1 件ある） ---
    def _dup_groups_by_digest(self) -> dict:
        return {g.digest: g for g in self.dup_result.groups} if self.dup_result else {}

    def _dup_location(self, path: str) -> str | None:
        return self.ctx_for(os.path.dirname(path)).location

    def _dup_pick_keep(self, g, exclude: set) -> str | None:
        """残す側の案。コピー名でない → ダウンロード以外 → 古い → パスが短い の順。"""
        cands = [f for f in g.files if f.path not in exclude]
        if not cands:
            return None
        return min(cands, key=lambda f: dupselect.keep_preference(f.path, f.modified,
                                                                  self._dup_location(f.path))).path

    def _dup_ensure_keep(self, g):
        """組の「残す」を不変条件どおりに直す。アプリの案は、チェックが無くなれば外す。"""
        d = g.digest
        paths = {f.path for f in g.files}
        keep = self.dup_keep.get(d)
        if keep is not None and keep not in paths:
            self.dup_keep.pop(d, None)
            self.dup_keep_auto.discard(d)
            keep = None
        checked = {p for p in paths if p in self.dup_checked}
        if not checked:
            if d in self.dup_keep_auto:
                self.dup_keep.pop(d, None)
                self.dup_keep_auto.discard(d)
            return
        if keep is None or keep in checked:
            alt = self._dup_pick_keep(g, checked)
            if alt is None:            # 全部チェックされていた: 案の 1 件はチェックを外して残す
                alt = self._dup_pick_keep(g, set())
                self.dup_checked.pop(alt, None)
            self.dup_keep[d] = alt
            self.dup_keep_auto.add(d)

    def _dup_check(self, g, f, on: bool) -> str | None:
        """1 件のチェックを付ける・外す。できないときは理由を返す。"""
        if on:
            if f.path in self.dup_checked:
                return None
            keep = self.dup_keep.get(g.digest)
            if keep == f.path and g.digest not in self.dup_keep_auto:
                return T('「残す」に選んだファイルにはチェックを付けられません（残す側を変えるには、残したい行で「これを残す」）。')
            if not any(x.path != f.path and x.path not in self.dup_checked for x in g.files):
                return T('各組で最低 1 件は残します。組の全部にチェックを付けることはできません。')
            self.dup_checked[f.path] = (g.digest, g.size)
        else:
            if self.dup_checked.pop(f.path, None) is None:
                return None
        self._dup_ensure_keep(g)
        if not self.dup_checked:
            self.dup_origin.clear()
        return None

    def _dup_set_group(self, g, on: bool):
        """組ごと: 「残す」以外の全部にチェック / 全部外す。"""
        if on:
            keep = self.dup_keep.get(g.digest)
            if keep is None or keep not in {f.path for f in g.files}:
                self.dup_keep[g.digest] = self._dup_pick_keep(g, set())
                self.dup_keep_auto.add(g.digest)
                keep = self.dup_keep[g.digest]
            for f in g.files:
                if f.path != keep:
                    self.dup_checked[f.path] = (g.digest, g.size)
        else:
            for f in g.files:
                self.dup_checked.pop(f.path, None)
            self._dup_ensure_keep(g)
            if not self.dup_checked:
                self.dup_origin.clear()

    def _dup_set_keep(self, g, path: str):
        """利用者が「残す」を決める。そのファイルのチェックは外す（前の残す側を移動対象には加えない）。"""
        self.dup_checked.pop(path, None)
        self.dup_keep[g.digest] = path
        self.dup_keep_auto.discard(g.digest)
        if not self.dup_checked:
            self.dup_origin.clear()

    def _dup_uncheck_paths(self, paths):
        by = self._dup_groups_by_digest()
        touched = set()
        for p in paths:
            v = self.dup_checked.pop(p, None)
            if v:
                touched.add(v[0])
        for d in touched:
            if d in by:
                self._dup_ensure_keep(by[d])
        if not self.dup_checked:
            self.dup_origin.clear()

    def _dup_click(self, event):
        """「移動」欄のクリックでチェックを切り替える。ほかの欄は通常の行選択（Ctrl・Shift も標準どおり）。"""
        tree = self.dup_tree
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        if self._dup_busy():
            return "break"
        entry = self._dup_rows.get(tree.identify_row(event.y))
        if not entry:
            return None
        msgs = []
        if entry[0] == "group":
            g = entry[1]
            on = any(f.path not in self.dup_checked and f.path != self.dup_keep.get(g.digest) for f in g.files)
            self._dup_set_group(g, on)
            if on:
                self._dup_add_origin(N_('個別に選んだもの'))
        else:
            _k, g, f, _item = entry
            order = [x[2].path for x in self._dup_order]
            if event.state & 0x0001 and self._dup_anchor in order and f.path in order:
                on = self._dup_anchor in self.dup_checked
                a, b = sorted((order.index(self._dup_anchor), order.index(f.path)))
                refused = 0
                for _iid, g2, f2 in self._dup_order[a:b + 1]:
                    if self._dup_check(g2, f2, on):
                        refused += 1
                if refused:
                    msgs.append(T('範囲のうち {0:,} 件は変えていません（「残す」の行、または各組で最低 1 件を残すため）。', refused))
            else:
                on = f.path not in self.dup_checked
                m = self._dup_check(g, f, on)
                if m:
                    msgs.append(m)
                self._dup_anchor = f.path
            if on and self.dup_checked:
                self._dup_add_origin(N_('個別に選んだもの'))
        self._dup_refresh_rows()
        if msgs:
            self.dup_note_extra.set(msgs[0])
        return "break"

    def _dup_space(self, _event=None):
        """選んだ行のチェックを切り替える（1 件でも未チェックがあれば全部付ける、なければ全部外す）。"""
        if self._dup_busy():
            return "break"
        entries = [self._dup_rows.get(i) for i in self.dup_tree.selection()]
        entries = [e for e in entries if e]
        if not entries:
            return "break"

        def free(g, f):
            return f.path != self.dup_keep.get(g.digest) or g.digest in self.dup_keep_auto

        on = any((e[0] == "group" and any(x.path not in self.dup_checked and free(e[1], x) for x in e[1].files))
                 or (e[0] == "file" and e[2].path not in self.dup_checked and free(e[1], e[2]))
                 for e in entries)
        refused = 0
        for e in entries:
            if e[0] == "group":
                self._dup_set_group(e[1], on)
            elif self._dup_check(e[1], e[2], on):
                refused += 1
        if on and self.dup_checked:
            self._dup_add_origin(N_('個別に選んだもの'))
        self._dup_refresh_rows()
        if refused:
            self.dup_note_extra.set(T('{0:,} 件は変えていません（「残す」の行、または各組で最低 1 件を残すため）。', refused))
        return "break"

    def _dup_select_rows_all(self, _event=None):
        rows = list(self._dup_rows)
        if rows:
            self.dup_tree.selection_set(rows)
        return "break"

    def _fill_dup_all_menu(self):
        m = self._dup_all_menu
        m.delete(0, "end")
        r = self.dup_result
        if not r or not r.groups:
            m.add_command(label=T('重複の検出結果がありません'), state="disabled")
            return
        shown = len(self._dup_shown)
        m.add_command(label=T('表示中の {0:,} 組（このページ）をすべて選ぶ', shown),
                      command=lambda: self.dup_select_all("page"))
        if len(self._dup_view_groups()) != len(r.groups):
            m.add_command(label=T('絞り込みに一致する全 {0:,} 組をすべて選ぶ', len(self._dup_view_groups())),
                          command=lambda: self.dup_select_all("filtered"))
        m.add_command(label=T('検出結果の全 {0:,} 組をすべて選ぶ（表示していない組も含む）', len(r.groups)),
                      command=lambda: self.dup_select_all("all"))

    def dup_select_all(self, scope: str):
        """各組で「残す」1 件以外にチェックを付ける。今のチェックに加える（移動はしない）。"""
        if self._dup_busy():
            return
        if not self.dup_result:
            return
        if scope == "page":
            groups = list(self._dup_shown)
            origin = (N_('表示中の {0:,} 組（ページ {1}）'), len(groups), self.dup_page + 1)
        elif scope == "filtered":
            groups = self._dup_view_groups()
            origin = (N_('絞り込みに一致する全 {0:,} 組'), len(groups))
        else:
            groups = list(self.dup_result.groups)
            origin = (N_('検出結果の全 {0:,} 組'), len(groups))
        label = T(*origin)
        for g in groups:
            self._dup_set_group(g, True)
        if groups:
            self._dup_add_origin(*origin)
        self._dup_refresh_rows()
        self.dup_note_extra.set(
            T('{0}の、各組の「残す」以外にチェックを付けました。まだ移動していません。「残す」（◎ はアプリの案）は、残したい行で「これを残す」を押すと変えられます。', label))

    def dup_clear_checks(self):
        self.dup_checked.clear()
        self.dup_origin.clear()
        for d in list(self.dup_keep_auto):
            self.dup_keep.pop(d, None)
        self.dup_keep_auto.clear()
        self._dup_refresh_rows()

    # --- 低リスク候補 ---
    def _file_protected(self, item: FileItem, cache: dict) -> str | None:
        """保護判定（計画づくり用に 1 回の操作の間だけ覚えておく。実行直前には 1 件ずつ判定し直す）。"""
        path = str(item.path)
        if path not in cache:
            cache[path] = self.protected_reason(path, False)
        return cache[path]

    def _dup_assess_file(self, f, live: set, sib: dict, prot: dict) -> tuple[bool, str]:
        item = f.key if isinstance(f.key, FileItem) else None
        if item is None or id(item) not in live:
            return False, "missing"
        ctx = self.ctx_for(item.dir)
        names = sib.get(item.dir)
        if names is None:
            names = sib[item.dir] = self.sibling_names(item.dir)
        c = classify_file(item.name, item.size, ctx, names)
        return dupselect.assess(item.name, item.dir, c.kind, c.certainty, ctx, self._file_protected(item, prot))

    def _live_ids(self) -> set:
        return {id(f) for f in self.file_rows}

    def confirm_low_risk(self, info: dict) -> bool:
        return LowRiskDialog(self, info).ask()

    def dup_select_low_risk(self):
        """低リスク候補にチェックを付ける（今のチェックと置き換える。移動はしない）。"""
        if self._dup_busy():
            return
        r = self.dup_result
        if not r or not r.groups:
            self.dup_note_extra.set(T('先に「重複ファイルを探す」を実行してください。'))
            return
        self.status.set(T('低リスク候補を判定しています…'))
        self.update_idletasks()
        live, sib, prot = self._live_ids(), {}, {}
        assess = {f.path: self._dup_assess_file(f, live, sib, prot) for g in r.groups for f in g.files}
        checks: dict[str, tuple[str, int]] = {}
        keeps: dict[str, str] = {}
        kept_ok = 0
        for g in r.groups:
            ok = {f.path for f in g.files if assess[f.path][0]}
            if not ok:
                continue
            explicit = self.dup_keep.get(g.digest) if g.digest not in self.dup_keep_auto else None
            if explicit is not None and explicit not in {f.path for f in g.files}:
                explicit = None
            if explicit is not None:
                keep = explicit
            elif len(ok) < g.count:
                keep = self._dup_pick_keep(g, ok)        # 条件に合わない側（どのみち残る）から
            else:
                keep = self._dup_pick_keep(g, set())
            move = [f for f in g.files if f.path in ok and f.path != keep]
            if not move:
                continue
            kept_ok += keep in ok
            if explicit is None:
                keeps[g.digest] = keep
            for f in move:
                checks[f.path] = (g.digest, g.size)
        size_of = {f.path: f.size for g in r.groups for f in g.files}
        excluded = Counter(code for ok, code in assess.values() if not ok)
        info = {
            "count": len(checks), "size": sum(v[1] for v in checks.values()),
            "groups": len({v[0] for v in checks.values()}), "kept_ok": kept_ok,
            "excluded_count": sum(excluded.values()),
            "excluded_size": sum(size_of[p] for p, (ok, _c) in assess.items() if not ok),
            "excluded": [(T(dupselect.REASONS.get(k, k)), n) for k, n in excluded.most_common()],
            "current_checked": len(self.dup_checked),
        }
        self.dup_assess = assess       # 判定は「判定」欄に出す（キャンセルしても見られる）
        self.status.set("")
        if not self.confirm_low_risk(info):
            self._dup_refresh_rows()
            self.dup_note_extra.set(T('低リスク候補の判定を「判定」欄に表示しました（チェックは変えていません）。'))
            return
        for d in list(self.dup_keep_auto):
            self.dup_keep.pop(d, None)
        self.dup_keep_auto.clear()
        self.dup_checked = checks
        for d, keep in keeps.items():
            self.dup_keep[d] = keep
            self.dup_keep_auto.add(d)
        self.dup_origin = [(N_('低リスク候補'),)] if checks else []
        self.render_dups()
        self.dup_note_extra.set(
            T('低リスク候補 {0:,} 件にチェックを付けました（まだ移動していません）。条件に合わない {1:,} 件は「個別確認」として「判定」欄に理由を出しています。', info['count'], info['excluded_count']))

    # --- 1 件の移動 ---
    def _selected_dup(self):
        sel = self.dup_tree.selection()
        if not sel:
            return None
        focus = self.dup_tree.focus()
        return self._dup_rows.get(focus if focus in sel else sel[0])

    def keep_selected_dup(self):
        """選んだ 1 本を「残す」に印を付ける。どれを残すかはユーザーが決める。もう一度押すと外れる。"""
        if self._dup_busy():
            return
        row = self._selected_dup()
        if not row or row[0] != "file":
            self.dup_note_extra.set(T('残すファイルの行を選んでから「これを残す」を押してください。'))
            return
        _kind, g, f, _item = row
        if self.dup_keep.get(g.digest) == f.path and g.digest not in self.dup_keep_auto:
            self.dup_keep.pop(g.digest, None)       # もう一度押したら外す
            self._dup_ensure_keep(g)                # チェックが残っていれば、案を置き直す
        else:
            self._dup_set_keep(g, f.path)
        self._dup_refresh_rows()

    def detail_dup_selected(self):
        row = self._selected_dup()
        if not row:
            return
        if row[0] == "group":
            self.dup_note_extra.set(T('組の中のファイルを選ぶと、そのファイルの詳細を表示します。'))
            return
        _kind, _g, f, item = row
        if item is not None:
            self.show_file_detail(item)
        else:
            self.dup_note_extra.set(T('このファイルはスキャン結果に見つかりません（移動された可能性があります）。'))

    def trash_selected_dup(self):
        """チェックしたものを移動する。チェックが無ければ、選んでいる 1 行だけを移動する。"""
        if self._dup_busy():
            return
        if self.dup_checked:
            # 「すべて選ぶ」「低リスク候補」で付けたチェックは、1 件でも 2 段階の確認を通す
            if len(self.dup_checked) > 1 or any(o[0] != N_('個別に選んだもの') for o in self.dup_origin):
                self._dup_bulk_move()
                return
            path, (digest, _size) = next(iter(self.dup_checked.items()))
            g = self._dup_groups_by_digest().get(digest)
            f = next((x for x in g.files if x.path == path), None) if g else None
            if f is not None:
                self._trash_one_dup(g, f)
            return
        rows = [self._dup_rows.get(i) for i in self.dup_tree.selection()]
        files = [r for r in rows if r and r[0] == "file"]
        if not files:
            self.dup_note_extra.set(T('移動するファイルにチェックを付けるか、ファイルの行を選んでください（組の見出しは選べません）。'))
            return
        if len(files) > 1:
            self.dup_note_extra.set(T('複数をまとめて移動するときはチェックを付けてください（選んだ行は Space でチェックできます）。'))
            return
        _kind, g, f, _item = files[0]
        self._trash_one_dup(g, f)

    def _trash_one_dup(self, g, f):
        item = f.key if isinstance(f.key, FileItem) else None
        if item is None:
            self.dup_note_extra.set(T('このファイルはスキャン結果に見つかりません。再スキャンしてください。'))
            return
        kept = self.dup_keep.get(g.digest)
        auto = g.digest in self.dup_keep_auto
        if kept == f.path and not auto:
            self.dup_note_extra.set(T('これは「残す」に選んだファイルです。別の行を選んでください。'))
            return
        if kept is None or kept == f.path or kept not in {x.path for x in g.files}:
            kept = self._dup_pick_keep(g, {f.path})
            if kept is None:
                self.dup_note_extra.set(T('この組には残せるファイルがありません。'))
                return
            self.dup_keep[g.digest] = kept
            self.dup_keep_auto.add(g.digest)
            auto = True
            self._dup_refresh_rows()
        keep_item = next((x.key for x in g.files if x.path == kept), None)
        if not isinstance(keep_item, FileItem):
            self.dup_note_extra.set(T('残す側のファイルが見つかりません。もう一度検出してください。'))
            return
        # 残す側・移す側の両方が、検出時から変わっていないことを確かめる
        for side, target in ((T('残す'), keep_item), (T('移動する'), item)):
            problem = self.check_unchanged(target)
            if problem:
                self._refuse(target.name, problem,
                             T('{0}ファイルが検出後に変わっています。もう一度「重複ファイルを探す」を実行してください。', side))
                return
        keep_text = f"{keep_item.name}\n{keep_item.dir}"
        if auto:
            keep_text += T('\n（アプリが選んだ案です。変えるには、この画面を閉じ、残したい行で「これを残す」を押してください）')
        self.trash_file(item, extra_facts=[
                (T('残すファイル'), keep_text),
                (T('この組'), T('{0} 個が同じ中身 ・ 1 本残すと {1} 空きます', g.count, human_size(g.reclaimable)))],
                extra_cautions=[
                    T('同じ中身でも、置かれている場所によって用途が違うことがあります（アプリやプロジェクトが決まった場所を見に行く、など）。'),
                    T('1 件残ることは、移動しても問題がないという意味ではありません。'),
                    T('残す側のファイルは移動しません。移動するのは選んだ 1 本だけです。')])

    # --- まとめて移動（2 段階の確認） ---
    def _dup_plan(self) -> list:
        """チェックに基づく移動計画。組ごとに「残す」1 件・移すもの・移せないもの（理由つき）。"""
        r = self.dup_result
        if not r or not self.dup_checked:
            return []
        live = self._live_ids()
        prot: dict = self._plan_prot if self._plan_prot is not None else {}
        wanted = {v[0] for v in self.dup_checked.values()}
        plan = []
        for g in r.groups:
            if g.digest not in wanted:
                continue
            self._dup_ensure_keep(g)
            keep_path = self.dup_keep.get(g.digest)
            keep = next((f for f in g.files if f.path == keep_path), None)
            targets, blocked = [], []
            for f in g.files:
                if f.path not in self.dup_checked or f.path == keep_path:
                    continue
                item = f.key if isinstance(f.key, FileItem) else None
                if item is None or id(item) not in live:
                    blocked.append((f, T('スキャン結果にありません（移動済みの可能性）')))
                    continue
                reason = self._file_protected(item, prot)
                if reason:
                    blocked.append((f, reason))
                    continue
                targets.append(f)
            if targets or blocked:
                plan.append(DupPlanGroup(g, keep, g.digest in self.dup_keep_auto, targets, blocked))
        return plan

    def _dup_bulk_summary(self, plan) -> dict:
        targets = [f for p in plan for f in p.targets]
        locations, more = dupselect.top_locations([(f.path, f.size) for f in targets],
                                                  str(self.scan_root) if self.scan_root else None)
        live, sib, prot = self._live_ids(), {}, {}
        cats = Counter((self.dup_assess.get(f.path) or self._dup_assess_file(f, live, sib, prot))[1]
                       for f in targets)
        categories = [(T('低リスク候補の条件に合うもの') if k == "ok" else T(dupselect.REASONS.get(k, k)), n)
                      for k, n in cats.most_common()]
        return {
            "count": len(targets), "size": sum(f.size for f in targets),
            "groups": sum(1 for p in plan if p.targets), "scopes": self.dup_scope_labels(),
            "locations": locations, "more_locations": more, "categories": categories,
            "blocked": sum(len(p.blocked) for p in plan),
            "auto_keeps": sum(1 for p in plan if p.targets and p.keep_auto),
        }

    def confirm_dup_stage1(self, summary: dict) -> bool:
        return DupBulkWarningDialog(self, summary).ask()

    def confirm_dup_stage2(self) -> bool:
        return DupBulkReviewDialog(self).ask()

    def export_dup_plan_csv(self, path: str):
        """移動計画を CSV に書き出す（全件を別の道具で確かめられるように）。"""
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow([T('組'), T('操作'), T('ファイル名'), T('フォルダ'), T('容量（バイト）'), T('更新日時')])
            for n, p in enumerate(self._dup_plan(), start=1):
                rows = ([((T('残す（アプリの案）') if p.keep_auto else T('残す（指定）')), p.keep)] if p.keep else [])
                rows += [(T('ごみ箱へ移動'), f) for f in p.targets]
                rows += [(T('移動しない: {0}', why), f) for f, why in p.blocked]
                for action, f in rows:
                    w.writerow([n, action, os.path.basename(f.path), os.path.dirname(f.path), f.size,
                                fmt_date(f.modified)])

    def _dup_keep_precheck(self, keep_item, live: set):
        """組の「残す」側が、検出後に消えた・変わっていないか（組ごとに 1 回だけ確かめる）。"""
        state: dict[str, str] = {}

        def pre():
            if "r" not in state:
                if keep_item is None or id(keep_item) not in live:
                    state["r"] = T('残す側のファイルがスキャン結果にありません')
                else:
                    problem = self.check_unchanged(keep_item)
                    state["r"] = T('残す側のファイルが{0}', problem) if problem else ""
            return ("skipped", T('この組は移動していません — {0}', state['r'])) if state["r"] else None
        return pre

    def _dup_bulk_move(self):
        self._plan_prot = {}          # 確認画面で何度作り直しても、保護判定は 1 件 1 回
        try:
            accepted = self._dup_bulk_confirm()
        finally:
            self._plan_prot = None
        if not accepted:
            return
        # 実行: 計画を作り直し、1 件ずつ直前に確かめて移す（保護判定もここでやり直す）
        plan = self._dup_plan()
        live = self._live_ids()
        steps = []
        for p in plan:
            if not p.targets:
                continue
            keep_item = p.keep.key if p.keep is not None and isinstance(p.keep.key, FileItem) else None
            pre = self._dup_keep_precheck(keep_item, live)
            for f in p.targets:
                steps.append(TrashStep(f.key, f.key.name, f.size, self._file_trash_run(f.key, live, pre)))
        moved, failed, skipped = self._run_trash_steps(steps, T('重複ファイルをごみ箱へ移動'))
        # 作り直した計画で移せなくなったもの（確認中に保護対象になった・消えた）も「移動しなかった」に数える
        failed = [(TrashStep(f.key if isinstance(f.key, FileItem) else f, os.path.basename(f.path), f.size, None), why)
                  for p in plan for f, why in p.blocked] + failed

        # 移動しなかったものはチェックを外して理由を残す。未実行はチェックを残す（続きから実行できる）。
        by = self._dup_groups_by_digest()
        for step, why in failed:
            path = str(step.obj.path)
            v = self.dup_checked.pop(path, None)
            self.dup_fail_note[path] = T('移動しなかった: ') + why
            if v and v[0] in by:
                self._dup_ensure_keep(by[v[0]])
        for step, why in skipped:
            self.dup_fail_note[str(step.obj.path)] = T('未実行: ') + why
        if moved:
            self._remove_files_from_index([s.obj for s in moved])
        else:
            self.render_dups()
        self._report_trash_outcome(moved, failed, skipped)

    def _dup_bulk_confirm(self) -> bool:
        """2 段階の確認。どちらかで止めたら False（何も移動しない）。"""
        plan = self._dup_plan()
        if not any(p.targets for p in plan):
            blocked = [(f, why) for p in plan for f, why in p.blocked]
            self.notify(T('ごみ箱へ移動'), T('チェックしたファイルはどれも移動できません。\n\n')
                        + "\n".join(T('・{0} — {1}', os.path.basename(f.path), w) for f, w in blocked[:10]), error=True)
            return False
        self.status.set(T('確認の準備をしています…'))
        self.update_idletasks()
        summary = self._dup_bulk_summary(plan)
        self.status.set("")
        if not self.confirm_dup_stage1(summary):
            self.status.set(T('キャンセルしました（何も移動していません）'))
            return False
        accepted = self.confirm_dup_stage2()
        self.render_dups()                 # 2 段目で残す側・チェックを変えていれば反映する
        if not accepted:
            self.status.set(T('キャンセルしました（何も移動していません。2 段目で変えた残す側・チェックはそのままです）'))
        return accepted

    def _drop_paths_from_dups(self, paths: set, inside=None):
        """ごみ箱へ移したファイルを重複結果からも外し、組の値・チェック・残す側を直す。"""
        r = self.dup_result
        if not r:
            return
        changed = False
        for g in r.groups:
            before = len(g.files)
            gone = [f.path for f in g.files
                    if f.path in paths or (inside is not None and inside(os.path.dirname(f.path)))]
            if not gone:
                continue
            changed = True
            gone_set = set(gone)
            g.files = [f for f in g.files if f.path not in gone_set]
            for p in gone:
                self.dup_checked.pop(p, None)
                self.dup_fail_note.pop(p, None)
                self.dup_assess.pop(p, None)
            if len(g.files) < 2:
                for f in g.files:
                    self.dup_checked.pop(f.path, None)
                self.dup_keep.pop(g.digest, None)
                self.dup_keep_auto.discard(g.digest)
            elif len(g.files) != before:
                self._dup_ensure_keep(g)
        if not changed:
            return
        r.groups = [g for g in r.groups if len(g.files) >= 2]
        if not self.dup_checked:
            self.dup_origin.clear()
        self.render_dups()
        self._dup_totals_note()

    def _drop_from_dups(self, item: FileItem):
        self._drop_paths_from_dups({str(item.path)})

    def _dup_context_menu(self, event):
        """右クリック。選んである行の上なら選択を崩さない（複数選んでから右クリックできるように）。"""
        row = self.dup_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.dup_tree.selection():
            self.dup_tree.selection_set(row)
        self.dup_tree.focus(row)
        entry = self._dup_rows.get(row)
        n_sel = len(self.dup_tree.selection())
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=T('チェックを切り替える（選択 {0:,} 行）', n_sel) if n_sel > 1 else T('チェックを切り替える'),
                         command=self._dup_space)
        if entry and entry[0] == "file" and n_sel == 1:
            f = entry[2]
            menu.add_command(label=T('これを残す'), command=self.keep_selected_dup)
            menu.add_command(label=T('詳細を表示'), command=self.detail_dup_selected)
            menu.add_command(label=T('エクスプローラーで開く'),
                             command=lambda p=Path(f.path): open_in_file_manager(p))
        elif entry and entry[0] == "group":
            menu.add_command(label=T('この組を開く'), command=lambda: self.dup_tree.item(row, open=True))
        menu.add_separator()
        n = len(self.dup_checked)
        menu.add_command(label=T('チェックした {0:,} 件をごみ箱へ…', n) if n else T('ごみ箱へ移動…'),
                         command=self.trash_selected_dup)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # --- ごみ箱から復元 ---
    BIN_STATE_TEXT = {"in_bin": N_('ごみ箱にあります'), "gone": N_('見つかりません'), "unresolved": N_('特定できません'),
                      "restored": N_('復元済み')}

    def _on_tab_changed(self):
        try:
            if self.tabs.select() == str(self.bin_tab):
                self.render_bin()
                self.check_bin()
        except tk.TclError:
            pass

    def _bin_state(self, e: recyclebin.Entry) -> tuple[str, str]:
        if e.state == "restored":
            return "restored", T(e.note) if e.note else T('このアプリで元の場所へ戻しました')
        if not e.r_name:
            return "unresolved", T(e.note) if e.note else T('ごみ箱の中の項目を特定できていません')
        return self.bin_state.get(e.key, ("in_bin", T('ごみ箱にあります（確認中）')))

    def render_bin(self):
        tree = self.bin_tree
        tree.delete(*tree.get_children())
        self._bin_rows.clear()
        q = self.bin_search.get().strip().lower()
        mode = self.bin_mode.current()
        kind = self.bin_kind.current()
        rows = []
        for e in self.ledger.entries:
            state, _why = self._bin_state(e)
            if q and q not in e.orig_path.lower():
                continue
            if mode == 1 and state != "in_bin":
                continue
            if mode == 2 and state not in ("gone", "unresolved"):
                continue
            if mode == 3 and state != "restored":
                continue
            if (kind == 1 and e.is_dir) or (kind == 2 and not e.is_dir):
                continue
            rows.append((e, state))
        rows.sort(key=lambda r: r[0].moved_at, reverse=True)
        for n, (e, state) in enumerate(rows[:FILE_DISPLAY_LIMIT]):
            iid = f"b{n}"
            tree.insert("", "end", iid=iid, text=("📁 " if e.is_dir else "") + e.name,
                        values=(T(self.BIN_STATE_TEXT.get(state, state)), T('フォルダ') if e.is_dir else T('ファイル'),
                                human_size(e.size), fmt_date(e.moved_at), os.path.dirname(e.orig_path)),
                        tags=("done",) if state == "restored" else (("ng",) if state != "in_bin" else ()))
            self._bin_rows[iid] = e
        total = len(self.ledger.entries)
        in_bin = sum(1 for e in self.ledger.entries if self._bin_state(e)[0] == "in_bin")
        note = T('表示 {0:,} 件 / 記録 {1:,} 件（ごみ箱にある {2:,} 件）', len(rows), total, in_bin)
        if len(rows) > FILE_DISPLAY_LIMIT:
            note += T('・新しい {0:,} 件を表示', FILE_DISPLAY_LIMIT)
        self.bin_count.set(note)
        if self.ledger.load_error:
            self.bin_detail.set(self.ledger.load_error)

    def _bin_show_selection(self):
        sel = [self._bin_rows[i] for i in self.bin_tree.selection() if i in self._bin_rows]
        if len(sel) == 1:
            e = sel[0]
            _state, why = self._bin_state(e)
            self.bin_detail.set(f"{e.orig_path} — {why}")
        elif sel:
            self.bin_detail.set(T('選択 {0:,} 件 ・ 合計 {1}', len(sel), human_size(sum(e.size for e in sel))))
        else:
            self.bin_detail.set("")

    @staticmethod
    def _compute_bin_check(entries) -> dict:
        """記録と、ごみ箱の中の実体を照らし合わせる（読むだけ。別スレッドで動かせる）。"""
        return {e.key: recyclebin.check(e) for e in entries if e.state != "restored" and e.r_name}

    def check_bin(self):
        """状態の確認を別スレッドで行う（件数が多くても画面を止めない）。"""
        if self._bin_checking:
            return
        targets = [e for e in self.ledger.entries if e.state != "restored" and e.r_name]
        if not targets:
            self.render_bin()
            return
        self._bin_checking = True
        self.bin_count.set(self.bin_count.get() + T('  — 状態を確認しています…'))

        def work():
            self.scan_queue.put(("bin_checked", self._compute_bin_check(targets)))
        threading.Thread(target=work, daemon=True).start()

    def _apply_bin_check(self, result: dict):
        self._bin_checking = False
        self.bin_state.update(result)
        self.render_bin()

    def open_bin_origin(self):
        sel = [self._bin_rows[i] for i in self.bin_tree.selection() if i in self._bin_rows]
        if not sel:
            return
        target = sel[0].orig_path
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        if os.path.isdir(folder):
            open_in_file_manager(Path(folder))
        else:
            self.bin_detail.set(T('元の場所「{0}」がありません。', folder))

    def confirm_restore(self, rows) -> bool:
        return RestoreConfirmDialog(self, rows).ask()

    def ask_yes_no(self, title: str, text: str) -> bool:
        return YesNoDialog(self, title, text).ask()

    def _restore_plan(self, entries) -> list[tuple]:
        """(記録, 問題, 補足)。親フォルダも一緒に戻すなら、子は「親のあとで戻す」として通す。"""
        entries = sorted(entries, key=lambda e: e.orig_path.count(os.sep))
        will_exist: set[str] = set()
        in_bin_paths = {os.path.normcase(e.orig_path): e for e in self.ledger.entries
                        if self._bin_state(e)[0] == "in_bin"}
        rows = []
        for e in entries:
            problem = recyclebin.restore_problem(e)
            note = ""
            parent = os.path.normcase(os.path.dirname(e.orig_path))
            if problem is not None and problem.code == "no_parent":
                if any(parent == w or parent.startswith(subtree_prefix(w)) for w in will_exist):
                    if not any(parent == w for w in will_exist):
                        # 戻すフォルダのさらに中の場所: そのフォルダが戻ってから確かめる
                        problem = None
                        note = T('先に戻すフォルダの中へ戻します')
                    else:
                        problem = None
                        note = T('親フォルダを先に戻してから戻します')
                else:
                    holder = next((x for p, x in in_bin_paths.items()
                                   if parent == p or parent.startswith(subtree_prefix(p))), None)
                    if holder is not None:
                        problem = recyclebin.RestoreError(
                            "no_parent", T('{0}。元のフォルダ「{1}」はこの一覧にあり、ごみ箱にあります。先にそれを復元してください', problem, holder.name))
            if problem is None:
                will_exist.add(os.path.normcase(e.orig_path))
            rows.append((e, problem, note))
        return rows

    def restore_selected(self):
        sel = [self._bin_rows[i] for i in self.bin_tree.selection() if i in self._bin_rows]
        if not sel:
            self.bin_detail.set(T('元の場所へ戻す項目を選んでください（Ctrl・Shift で複数選べます）。'))
            return
        rows = self._restore_plan(sel)
        if not self.confirm_restore(rows):
            self.status.set(T('キャンセルしました（何も戻していません）'))
            return
        results = []
        restored = []
        for e, problem, _note in rows:
            place = os.path.dirname(e.orig_path)
            if problem is not None:
                results.append((e.name, place, T('戻さなかった: {0}', problem)))
                continue
            try:
                extra = recyclebin.restore(e)     # 直前にもう一度確かめる（同名・親フォルダ・ごみ箱の実体）
            except recyclebin.RestoreError as exc:
                results.append((e.name, place, T('戻せなかった: {0}', exc)))
                continue
            restored.append(e)
            results.append((e.name, place, T('元に戻した') + (T('（{0}）', extra) if extra else "")))
        try:
            self.ledger.save()
        except OSError as exc:
            results.append((T('（記録）'), self.ledger.path, T('記録を保存できませんでした: {0}', exc)))
        ng = len(results) - len(restored)
        text = T('元に戻した: {0:,} 件', len(restored)) + (T('\n戻さなかった・戻せなかった: {0:,} 件', ng) if ng else "")
        if restored:
            self._mark_scan_stale(restored)
            text += T('\n\n戻したものは、今のスキャン結果には含まれていません。最新にするには「スキャン」を押してください。')
        self.status.set(text.split("\n\n")[0].replace("\n", T(' ／ ')))
        self.show_trash_results(T('復元の結果') if not ng else (T('一部だけ戻しました') if restored else T('戻しませんでした')),
                                text, results, error=not restored)
        self.render_bin()

    def _mark_scan_stale(self, restored):
        root = self.scan_root
        if root is None:
            return
        pre = subtree_prefix(os.path.normcase(str(root)))
        inside = [e for e in restored if os.path.normcase(e.orig_path).startswith(pre)]
        if not inside:
            return
        self._stale_restored = getattr(self, "_stale_restored", 0) + len(inside)
        self.bin_stale.set(T('スキャン対象の中に {0:,} 件を戻しました。今の一覧・容量マップには入っていません（スキャン結果が古くなっています）。', self._stale_restored))
        self.bin_stale_bar.pack(fill="x", pady=(0, px(6)), before=self.bin_tree.master)

    def forget_bin_selected(self):
        sel = [self._bin_rows[i] for i in self.bin_tree.selection() if i in self._bin_rows]
        if not sel:
            return
        still = [e for e in sel if self._bin_state(e)[0] == "in_bin"]
        text = (T('選んだ {0:,} 件の記録を、この一覧から外します。ごみ箱の中身は何も変えません。', len(sel))
                + (T('\n\nうち {0:,} 件はまだごみ箱にあります。外すと、このアプリからは戻せなくなります（Windows のごみ箱からは戻せます）。', len(still)) if still else ""))
        if not self.ask_yes_no(T('記録から外す'), text):
            return
        self.ledger.remove(sel)
        self.render_bin()

    # --- ファイル一覧・フォルダ一覧の「移動」チェック ---
    def _make_check_bar(self, parent, with_only: bool = False):
        """チェックの件数・容量と、まとめての移動（ファイル一覧・フォルダ一覧で共通の表示）。"""
        if not hasattr(self, "check_note"):
            self.check_note = tk.StringVar(value="")
            self.checked_only = tk.BooleanVar(value=False)
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, px(6)))
        ttk.Label(bar, textvariable=self.check_note, style="Strong.TLabel").pack(side="left")
        which = "files" if with_only else "folders"
        ttk.Button(bar, text=T('チェックしたものを確認してごみ箱へ…'),
                   command=lambda: self.trash_checked(which)).pack(side="right")
        ttk.Button(bar, text=T('チェックを外す'), command=self.clear_checks).pack(side="right", padx=(0, px(8)))
        if with_only:
            ttk.Checkbutton(bar, text=T('チェックしたものだけ表示'), variable=self.checked_only,
                            command=self.render_files).pack(side="right", padx=(0, px(12)))
        return bar

    @staticmethod
    def _style_check_rows(tree):
        """チェックした行は濃い青・白い文字。チェックしたフォルダの中の行は薄い青（一緒に移る）。"""
        tree.tag_configure("covered", background=P["covered_row"], foreground=P["text"])
        tree.tag_configure("checked", background=P["check_row"], foreground=P["check_row_text"])
        tree.tag_configure("checked_sel", background=P["check_row_selected"], foreground=P["text"])

    def _retag_selection(self, tree):
        """選択が変わったとき、チェックの行の色（選んでいる／いない）を合わせ直す。"""
        sel = set(tree.selection())
        stack = list(tree.get_children(""))
        while stack:
            iid = stack.pop()
            stack.extend(tree.get_children(iid))
            try:
                mark = tree.set(iid, "check")
            except tk.TclError:
                continue
            tags = tree.item(iid, "tags")
            new = self._tags_with_mark(tags, mark, iid in sel)
            if tuple(tags) != new:
                tree.item(iid, tags=new)

    def _is_checked(self, kind: str, target) -> bool:
        return str(target) in self.check_dirs if kind == "dir" else id(target) in self.check_files

    def _set_checked(self, kind: str, target, on: bool):
        if kind == "dir":
            if on:
                self.check_dirs[str(target)] = target
            else:
                self.check_dirs.pop(str(target), None)
        elif on:
            self.check_files[id(target)] = target
        else:
            self.check_files.pop(id(target), None)

    def _check_prefixes(self) -> list[str]:
        return [subtree_prefix(os.path.normcase(k)) for k in self.check_dirs]

    def _check_mark(self, kind: str, target, prefixes=None) -> str:
        """☑ チェック済み ／ ▣ チェックしたフォルダに含まれる（フォルダと一緒に移る） ／ ☐"""
        if self._is_checked(kind, target):
            return "☑"
        if self.check_dirs:
            if prefixes is None:
                prefixes = self._check_prefixes()
            p = os.path.normcase(str(target.path if kind == "file" else target))
            if any(p.startswith(pre) for pre in prefixes):
                return "▣"
        return "☐"

    COLOR_TAGS = ("odd", "checked", "checked_sel", "covered")

    @staticmethod
    def _tags_with_mark(tags, mark: str, selected: bool = False) -> tuple:
        """行の色のタグを 1 つだけ付ける（色の付かない印 row_odd などはそのまま残す）。

        ttk.Treeview では、同じ行に色を指定したタグが複数あると、どれの背景・文字色が使われるかが
        タグの順や選択の状態で変わる（縞模様の背景にチェックの白い文字が乗って読めなくなった）。
        そこで色のタグは 1 行に 1 つにし、選んでいるチェックの行は、選択の色とどちらが優先されても
        読める専用の色（checked_sel）にする。
        """
        base = tuple(t for t in tags if t not in App.COLOR_TAGS)
        if mark == "☑":
            color = "checked_sel" if selected else "checked"
        elif mark == "▣":
            color = "covered"
        elif "row_odd" in base:
            color = "odd"
        else:
            return base
        return (color,) + base

    def _checked_rows(self) -> list[tuple[str, object]]:
        return ([("dir", p) for p in self.check_dirs.values()]
                + [("file", f) for f in self.check_files.values()])

    def _update_check_views(self):
        """チェックの印（両方の一覧）と、件数・合計容量の表示を今の状態に合わせる。"""
        prefixes = self._check_prefixes()
        tree = self.file_tree
        sel = set(tree.selection())
        for iid, item in self._file_iids.items():
            if tree.exists(iid):
                mark = self._check_mark("file", item, prefixes)
                tree.set(iid, "check", mark)
                tree.item(iid, tags=self._tags_with_mark(tree.item(iid, "tags"), mark, iid in sel))
        fsel = set(self.folder_tree.selection())

        def walk(parent):
            for iid in self.folder_tree.get_children(parent):
                kind, target = self._folder_row_target(iid)
                if kind is not None:
                    mark = self._check_mark(kind, target, prefixes)
                    self.folder_tree.set(iid, "check", mark)
                    self.folder_tree.item(iid, tags=self._tags_with_mark(self.folder_tree.item(iid, "tags"),
                                                                         mark, iid in fsel))
                walk(iid)
        walk("")
        rows = self._checked_rows()
        if not rows:
            self.check_note.set(T('移動のチェックなし（「移動」欄のクリック・Space でチェック）'))
            return
        folders, files, nested = self._dedupe_nested(rows)
        total = sum(self.folder_sizes.get(f, 0) for f in folders) + sum(i.size for i in files)
        parts = [T('フォルダ {0:,}', len(self.check_dirs))] if self.check_dirs else []
        if self.check_files:
            parts.append(T('ファイル {0:,}', len(self.check_files)))
        text = T('チェック {0:,} 件（{1}）・合計 {2}', len(rows), T('・').join(parts), human_size(total))
        if nested:
            text += T('（うち {0:,} 件はチェックしたフォルダの中）', nested)
        self.check_note.set(text)

    def _toggle_rows(self, which: str, target, order, event):
        """クリックで 1 行、Shift+クリックで起点からここまでを同じ状態にそろえる。"""
        anchor = self._check_anchor.get(which)
        if event.state & 0x0001 and anchor is not None and anchor in order and target in order:
            on = self._is_checked(*anchor)
            a, b = sorted((order.index(anchor), order.index(target)))
            for kind, t in order[a:b + 1]:
                self._set_checked(kind, t, on)
        else:
            self._set_checked(*target, not self._is_checked(*target))
            self._check_anchor[which] = target
        self._update_check_views()

    def _file_check_click(self, event):
        tree = self.file_tree
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        item = self._file_iids.get(tree.identify_row(event.y))
        if item is None:
            return "break"
        order = [("file", self._file_iids[i]) for i in tree.get_children("") if i in self._file_iids]
        self._toggle_rows("files", ("file", item), order, event)
        return "break"

    def _visible_folder_rows(self) -> list[tuple[str, object]]:
        out = []

        def walk(parent):
            for iid in self.folder_tree.get_children(parent):
                kind, target = self._folder_row_target(iid)
                if kind is not None:
                    out.append((kind, target))
                if self.folder_tree.item(iid, "open"):
                    walk(iid)
        walk("")
        return out

    def _folder_check_click(self, event):
        tree = self.folder_tree
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        kind, target = self._folder_row_target(tree.identify_row(event.y) or "|more")
        if kind is None:
            return "break"
        self._toggle_rows("folders", (kind, target), self._visible_folder_rows(), event)
        return "break"

    def _space_check(self, which: str):
        """選んでいる行のチェックを切り替える（1 件でも未チェックがあれば全部付け、なければ全部外す）。"""
        if which == "files":
            rows = [("file", i) for i in self._selected_files()]
        else:
            rows = self._selected_folder_rows()
        if rows:
            on = any(not self._is_checked(k, t) for k, t in rows)
            for k, t in rows:
                self._set_checked(k, t, on)
            self._update_check_views()
        return "break"

    def _uncheck_objs(self, objs):
        """確認画面で外したもの・移せなかったもののチェックを外す。"""
        for o in objs:
            if isinstance(o, FolderTarget):
                self.check_dirs.pop(str(o.path), None)
            elif isinstance(o, FileItem):
                self.check_files.pop(id(o), None)
        self._update_check_views()

    def clear_checks(self):
        self.check_files.clear()
        self.check_dirs.clear()
        self._update_check_views()
        if self.checked_only.get():
            self.render_files()

    def _hidden_checked(self, which: str | None) -> tuple[int, str]:
        """チェックしたもののうち、今の一覧に表示されていない件数と、その内訳の説明。"""
        if which == "files":
            shown = {id(f) for f in self._file_iids.values()}
            hidden_files = sum(1 for k in self.check_files if k not in shown)
            hidden_dirs = len(self.check_dirs)          # ファイル一覧にはフォルダの行が無い
            parts = []
            if hidden_files:
                parts.append(T('絞り込み・表示件数の上限で表示していないファイル {0:,} 件', hidden_files))
            if hidden_dirs:
                parts.append(T('フォルダ一覧でチェックしたフォルダ {0:,} 件（中身ごと）', hidden_dirs))
            return hidden_files + hidden_dirs, T('・').join(parts)
        if which == "folders":
            shown = {(k, str(t) if k == "dir" else id(t)) for k, t in self._visible_folder_rows()}
            n_files = sum(1 for k in self.check_files if ("file", k) not in shown)
            n_dirs = sum(1 for k in self.check_dirs if ("dir", k) not in shown)
            parts = []
            if n_dirs:
                parts.append(T('折りたたまれていて見えていないフォルダ {0:,} 件', n_dirs))
            if n_files:
                parts.append(T('見えていない（折りたたみ・ファイル一覧でチェック）ファイル {0:,} 件', n_files))
            return n_files + n_dirs, T('・').join(parts)
        return 0, ""

    def trash_checked(self, which: str | None = None):
        """チェックしたもの（ファイル一覧・フォルダ一覧で共通）を、全件を確認してからごみ箱へ。

        which は操作した一覧（"files" / "folders"）。その一覧で見えていないチェック済みの項目が
        対象に含まれるときは、件数を確認画面で目立たせて出す（見えている行だけを操作したと誤解させない）。
        """
        rows = self._checked_rows()
        if not rows:
            self.status.set(T('移動するものにチェックを付けてください（「移動」欄のクリック、または行を選んで Space）。'))
            return
        n_hidden, detail = self._hidden_checked(which)
        warn = (T('今の一覧に表示されていない {0:,} 件も対象に含まれます（{1}）。下の一覧で全件を確かめてください。', n_hidden, detail)) if n_hidden else None
        self.trash_many(rows, from_checks=True, warn=warn)

    def _selected_file(self) -> FileItem | None:
        """1 件だけ選ばれているときのファイル。複数なら None。"""
        sel = self.file_tree.selection()
        return self._file_iids.get(sel[0]) if len(sel) == 1 else None

    def _selected_files(self) -> list[FileItem]:
        """選ばれているファイル（並び順は一覧の見た目どおり）。"""
        chosen = set(self.file_tree.selection())
        return [self._file_iids[i] for i in self.file_tree.get_children("")
                if i in chosen and i in self._file_iids]

    def _select_all_files(self, _event=None):
        rows = self.file_tree.get_children("")
        if rows:
            self.file_tree.selection_set(rows)
        return "break"

    def _update_file_selection_note(self):
        """選択の件数と合計容量。何が操作対象かを取り違えないために出す。"""
        items = self._selected_files()
        if not items:
            self.file_selection_note.set("")
        elif len(items) == 1:
            self.file_selection_note.set(T('選択 1 件（{0}）', human_size(items[0].size)))
        else:
            total = sum(i.size for i in items)
            self.file_selection_note.set(T('選択 {0:,} 件 ・ 合計 {1}', len(items), human_size(total)))

    def _selected_folder(self) -> Path | None:
        """選んでいるフォルダ。ファイルの行を選んでいるときは None。"""
        kind, target = self._selected_folder_row()
        return target if kind == "dir" else None

    def detail_file_selected(self):
        """詳細は 1 件のときだけ。複数選択のまま押されたら、その旨を伝える。"""
        sel = self.file_tree.selection()
        if len(sel) > 1:
            self.file_note_extra.set(
                T('詳細は 1 件ずつ表示します（今は {0:,} 件選択中）。1 行だけ選んでから押してください。', len(sel)))
            return
        item = self._selected_file()
        if item:
            self.show_file_detail(item)

    def detail_folder_selected(self):
        """フォルダ一覧の行の詳細。中身のファイルを選んでいればファイルの詳細を出す。"""
        n = len(self.folder_tree.selection())
        if n > 1:
            self.status.set(T('詳細は 1 件ずつ表示します（今は {0:,} 件選択中）。1 行だけ選んでから押してください。', n))
            return
        kind, target = self._selected_folder_row()
        if kind == "dir" and target is not None:
            self.show_folder_detail(target)
        elif kind == "file" and target is not None:
            self.show_file_detail(target)

    def open_file_selected(self):
        item = self._selected_file()
        if item:
            open_in_file_manager(item.path)

    def open_folder_row_selected(self):
        """フォルダ一覧の行をエクスプローラーで開く（フォルダでもファイルでも）。"""
        kind, target = self._selected_folder_row()
        if kind == "dir" and target is not None:
            open_in_file_manager(target)
        elif kind == "file" and target is not None:
            open_in_file_manager(target.path)

    def open_folder_selected(self):
        folder = self._selected_folder()
        if folder:
            open_in_file_manager(folder)

    # --- ごみ箱へ移動（ファイル・フォルダ） ---
    # 流れ: 対象外の確認 → 現在の状態の再確認 → 確認画面（「移動する」のみ実行）
    #       → 直前にもう一度再確認 → Windows のごみ箱へ → 索引・表示を更新
    def trash_selected_file(self):
        """「ごみ箱へ移動」ボタン・Delete キー（ファイル一覧）。

        チェックがあればチェックした全件（両方の一覧で共通）。なければ従来どおり選んでいる行。
        """
        if self._checked_rows():
            self.trash_checked("files")
            return
        items = self._selected_files()
        if not items:
            return
        if len(items) == 1:
            self.trash_file(items[0])
            return
        self.trash_files(items)

    def confirm_trash_many(self, movable, blocked, note=None, on_exclude=None, warn=None) -> bool:
        return MultiTrashConfirmDialog(self, movable, blocked, note=note, on_exclude=on_exclude, warn=warn).ask()

    def trash_files(self, items: list[FileItem]) -> None:
        """複数のファイルをごみ箱へ移す（ファイル一覧で複数選んだとき）。"""
        self.trash_many([("file", i) for i in items])

    def trash_selected_folder(self):
        """フォルダ一覧からごみ箱へ。中身のファイルを選んでいればそのファイルを移す。

        どちらの道も既存の `protected_reason` と移動直前の再確認を通る。
        中身を見られるようにしたからといって、守っている場所が操作できるようにはならない。
        複数選んでいれば、フォルダ・ファイルをまとめて 1 つの確認画面に出す。
        チェックがあれば、選択ではなくチェックした全件が対象（ファイル一覧と同じ）。
        """
        if self._checked_rows():
            self.trash_checked("folders")
            return
        rows = self._selected_folder_rows()
        if len(rows) > 1:
            self.trash_folder_rows(rows)
            return
        kind, target = self._selected_folder_row()
        if kind == "file" and target is not None:
            self.trash_file(target)
            return
        folder = self._selected_folder()
        if folder:
            self.trash_folder(folder)

    def trash_folder_rows(self, rows) -> None:
        """フォルダ一覧で複数選んだ行（フォルダ・ファイル混在）をまとめて移す。"""
        self.trash_many(rows)

    def trash_many(self, rows, from_checks: bool = False, warn: str | None = None) -> None:
        """ファイル・フォルダをまとめてごみ箱へ移す。一覧の複数選択・チェック・フォルダ一覧で共通。

        **1 件ずつと同じ確かめ方を、全件に対して行う。**
          保護対象か（`protected_reason`）・リンクでないか
          スキャン後に変わっていないか — 確認の前と、移す直前の二度（フォルダは中身を数え直す）
          ごみ箱に入るか（入らず完全削除になるなら移さない。`trash.move_to_trash` が確かめる）
        チェックしたフォルダの中にある行は、フォルダと一緒に移るので二重に処理しない。
        まとめて移すときは、スキャン後に中身が変わったフォルダは移さない（1 件ずつ確かめてもらう）。
        """
        # 移せないフォルダ（守っている場所・リンク）を先に分ける。その中の行は「フォルダと一緒に
        # 移る」扱いにしない（親が移らないのに、選んだ子まで黙って外すことになるため）。
        blocked: list[tuple[object, str]] = []
        usable = []
        for kind, target in rows:
            if kind == "dir":
                fs = str(target)
                placeholder = FolderTarget(target, self.folder_sizes.get(target, 0), None, T('フォルダ'), False)
                reason = (T('一覧にありません（移動済みの可能性）') if target not in self.folder_sizes
                          else self.protected_reason(fs, True))
                if not reason and trash.is_link_or_junction(fs):
                    reason = T('リンク（ジャンクション）のため')
                if reason:
                    blocked.append((placeholder, reason))
                    continue
            usable.append((kind, target))
        folders, files, nested = self._dedupe_nested(usable)
        live = self._live_ids()
        movable: list[tuple[object, str]] = []
        for folder in folders:
            fs = str(folder)
            scan_size = self.folder_sizes.get(folder, 0)
            placeholder = FolderTarget(folder, scan_size, None, T('フォルダ'), False)
            try:
                now = trash.measure_folder(fs)
            except FileNotFoundError:
                blocked.append((placeholder, T('スキャン後に移動または削除されたため、元の場所に見つかりません')))
                continue
            if (now.files, now.size) != (self.folder_file_counts.get(folder, 0), scan_size):
                blocked.append((placeholder, T('スキャン後に中身が変化しています（1 件ずつ確認して移動してください）')))
                continue
            if now.errors:
                blocked.append((placeholder, T('読み取れない項目があり、中身を確認できません')))
                continue
            kind_text, risky = self._folder_kind_risk(fs)[:2]
            text = T('フォルダ（中身ごと {0:,} ファイル）・{1}', now.files, kind_text)
            movable.append((FolderTarget(folder, now.size, now, text, risky), text))
        for item in files:
            if id(item) not in live:
                blocked.append((item, T('一覧にありません（移動済みの可能性）')))
                continue
            reason = self.protected_reason(str(item.path), False) or self.check_unchanged(item)
            if reason:
                blocked.append((item, reason))
                continue
            movable.append((item, self.trash_risk(item)[0]))

        if from_checks:
            # 移せないと分かったものはチェックを外す（残しておいても、次も移せない）
            self._uncheck_objs([o for o, _w in blocked])
        if not movable:
            self.notify(T('ごみ箱へ移動'), T('選んだものはどれも移動できません。\n\n')
                        + "\n".join(T('・{0} — {1}', i.name, w) for i, w in blocked[:10]), error=True)
            return
        note = (T('チェック・選択したフォルダの中にある {0:,} 件は、フォルダと一緒に移動します（別々には処理しません）。', nested)
                if nested else None)
        on_exclude = self._uncheck_objs if from_checks else None
        if not self.confirm_trash_many(movable, blocked, note=note, on_exclude=on_exclude, warn=warn):
            self.status.set(T('キャンセルしました（{0:,} 件は変更していません）', len(movable)))
            return
        if not movable:
            return

        steps = []
        for obj, _k in movable:
            if isinstance(obj, FolderTarget):
                steps.append(TrashStep(obj, obj.name, obj.size, self._folder_trash_run(obj)))
            else:
                steps.append(TrashStep(obj, obj.name, obj.size, self._file_trash_run(obj, live)))
        moved, failed, skipped = self._run_trash_steps(steps, T('ごみ箱へ移動'))
        if from_checks:
            self._uncheck_objs([st.obj for st, _w in failed])
        failed = [(TrashStep(o, o.name, o.size, None), w) for o, w in blocked] + failed
        preds = [self._remove_files_from_index([st.obj for st in moved if isinstance(st.obj, FileItem)],
                                               refresh=False)]
        for st in moved:
            if isinstance(st.obj, FolderTarget):
                preds.append(self._remove_folder_from_index(st.obj.path, refresh=False))
        if moved:
            self._refresh_after_trash(lambda kind, target: any(pr(kind, target) for pr in preds))
        self._report_trash_outcome(moved, failed, skipped)
        if nested and moved:
            self.status.set(self.status.get() + T('（フォルダの中にあった {0:,} 件はフォルダと一緒に移動）', nested))
        self._update_file_selection_note()
        self._update_folder_selection_note()
        self._update_check_views()

    # --- まとめて移すときの共通部分 ---
    PROGRESS_MIN = 20   # これ以上の件数なら、進み具合と「中止」を出す

    def make_progress(self, title: str, total: int):
        return BulkProgress(self, title, total)

    def _file_trash_run(self, item: FileItem, live: set, pre=None):
        """1 ファイルを移す手順。移す直前に、保護対象・スキャン後の変化・消失をこの 1 件について確かめる。"""
        def run():
            if pre is not None:
                r = pre()
                if r:
                    return r
            if id(item) not in live:
                return "failed", T('一覧にありません（移動済みの可能性）')
            reason = self.protected_reason(str(item.path), False)
            if reason:
                return "failed", reason
            problem = self.check_unchanged(item)
            if problem:
                return "failed", problem
            self._trash_move(str(item.path), item.size, False)
            return None
        return run

    def _folder_trash_run(self, target: FolderTarget):
        def run():
            fs = str(target.path)
            reason = self.protected_reason(fs, True)
            if reason:
                return "failed", reason
            if trash.is_link_or_junction(fs):
                return "failed", T('リンク（ジャンクション）のため')
            try:
                again = trash.measure_folder(fs)
            except FileNotFoundError:
                return "failed", T('確認後に移動または削除されたため、元の場所に見つかりません')
            if again.key() != target.measure.key():
                return "failed", T('確認画面を表示している間に中身が変化しました')
            self._trash_move(fs, again.size, True)
            return None
        return run

    def _run_trash_steps(self, steps: list, title: str):
        """順に移す。(移動した, 移動しなかった[(手順, 理由)], 未実行[(手順, 理由)]) を返す。

        「中止」を押したら、そこから先は実行しない（未実行として数える）。
        索引・表示の更新は呼び手がまとめて 1 回行う（1 件ごとに描き直すと数万件で終わらない）。
        """
        moved, failed, skipped = [], [], []
        progress = self.make_progress(title, len(steps)) if len(steps) >= self.PROGRESS_MIN else None
        try:
            for n, step in enumerate(steps):
                if progress is not None and progress.cancelled:
                    skipped.extend((s, T('中止したため実行していません')) for s in steps[n:])
                    break
                try:
                    r = step.run()
                except trash.TrashError as exc:
                    r = ("failed", str(exc))
                except OSError as exc:
                    r = ("failed", T('確認できませんでした（{0}）', exc.strerror or exc))
                if r is None:
                    moved.append(step)
                elif r[0] == "skipped":
                    skipped.append((step, r[1]))
                else:
                    failed.append((step, r[1]))
                if progress is not None:
                    progress.step(n + 1, len(moved), len(failed))
        finally:
            if progress is not None:
                progress.close()
            self._flush_trash_records()
        return moved, failed, skipped

    def show_trash_results(self, title: str, text: str, rows, error: bool = False):
        """対象ごとの結果を出す（テストでは差し替えて、画面は出さない）。"""
        TrashResultDialog(self, title, text, rows).show()

    def _report_trash_outcome(self, moved, failed, skipped):
        """成功・失敗（移動しなかった）・未実行を分けて伝える。一部でも失敗・未実行なら対象ごとに一覧で出す。"""
        total = sum(s.size for s in moved)
        head = T('移動した: {0:,} 件（{1}）', len(moved), human_size(total))
        counts = [head]
        if failed:
            counts.append(T('移動しなかった（確認で止めた・失敗）: {0:,} 件', len(failed)))
        if skipped:
            counts.append(T('未実行: {0:,} 件', len(skipped)))
        detail = []
        for label, rows in ((T('移動しなかったもの'), failed), (T('未実行のもの'), skipped)):
            if rows:
                detail.append(f"\n{label}")
                detail += [T('・{0} — {1}', s.name, w) for s, w in rows[:8]]
                if len(rows) > 8:
                    detail.append(T('・ほか {0:,} 件', len(rows) - 8))
        self.status.set(T('ごみ箱へ移動 — ') + T(' ／ ').join(counts)
                        + (T('。ごみ箱から元に戻せます。') if moved else ""))
        if moved and not failed and not skipped:
            return
        title = T('一部だけ移動しました') if moved else T('移動しませんでした')

        def place(obj):
            d = getattr(obj, "dir", None)
            return str(d) if d is not None else os.path.dirname(str(getattr(obj, "path", "")))
        rows = ([(st.name, place(st.obj), T('移動した')) for st in moved]
                + [(st.name, place(st.obj), T('移動しなかった: {0}', w)) for st, w in failed]
                + [(st.name, place(st.obj), T('未実行: {0}', w)) for st, w in skipped])
        text = "\n".join(counts) + "\n" + "\n".join(detail)
        if failed or skipped:
            text += T('\n\n移動しなかったもの・未実行のものはそのまま元の場所にあります。')
        self.show_trash_results(title, text, rows, error=not moved)

    def item_risky(self, item) -> bool:
        """確認画面で注意を強めるか（フォルダは確認前に判定済み）。"""
        if isinstance(item, FolderTarget):
            return item.risky
        return self.trash_risk(item)[2]

    def protected_reason(self, path: str, is_dir: bool) -> str | None:
        """スキャン対象のルートや重要な場所など、操作対象にしない理由。"""
        if is_dir and self.scan_root is not None:
            p = os.path.normcase(os.path.normpath(path))
            root = os.path.normcase(os.path.normpath(str(self.scan_root)))
            if p == root:
                return T('スキャン対象のルートフォルダのため')
            if root.startswith(subtree_prefix(p)):
                return T('スキャン対象のルートを含むフォルダのため')
        return trash.protected_reason(path, is_dir)

    def check_unchanged(self, item: FileItem) -> str | None:
        """スキャン後に消えた・変わった場合はその内容を返す（古い情報のまま処理しないため）。"""
        try:
            st = os.stat(item.path)
        except FileNotFoundError:
            return T('スキャン後に移動または削除されたため、元の場所に見つかりません')
        except OSError as exc:
            return T('現在の状態を確認できません（{0}）', exc.strerror or exc)
        if not stat.S_ISREG(st.st_mode):
            return T('スキャン後にファイル以外のものに置き換わっています')
        try:
            # os.stat は古い値を返すことがある（Python 3.12 以降の近道）。開いて正確な値で確かめる
            st = recyclebin.exact_stat(str(item.path))
        except FileNotFoundError:
            return T('スキャン後に移動または削除されたため、元の場所に見つかりません')
        except OSError as exc:
            return T('現在の状態を確認できません（{0}）', exc.strerror or exc)
        changes = []
        if st.st_size != item.size:
            old, new = human_size(item.size), human_size(st.st_size)
            if old == new:  # 丸めた表示では差が見えない場合はバイト数で示す
                old, new = T('{0:,} バイト', item.size), T('{0:,} バイト', st.st_size)
            changes.append(T('容量 {0} → {1}', old, new))
        if abs(st.st_mtime - item.modified) > 0.001:
            changes.append(T('更新日時 {0} → {1}', fmt_date(item.modified), fmt_date(st.st_mtime)))
        if changes:
            return T('スキャン後に変更されています（') + T('、').join(changes) + T('）')
        return None

    def _risky_ctx(self, ctx: FolderContext) -> bool:
        return (ctx.group_type in ("system", "app", "game")
                or (ctx.location or "").startswith(("Program Files", "AppData")))

    def trash_risk(self, item: FileItem):
        """確認画面に出す種類・注意と、注意を強めるべきか。"""
        ctx = self.ctx_for(item.dir)
        c = classify_file(item.name, item.size, ctx, self.sibling_names(item.dir))
        kind_text = L(c.kind_display) + (T('（{0}）', L(c.possibility)) if c.possibility else "")
        if c.group_label:
            kind_text += T(' ／ 関連: {0}', L(c.group_label))
        return kind_text, [L(x) for x in c.cautions], c.kind in TRASH_RISKY_KINDS or self._risky_ctx(ctx)

    def confirm_trash(self, spec: dict) -> bool:
        return TrashConfirmDialog(self, spec).ask()

    def raise_window(self, win):
        """ウィンドウを前面に出す。前面に出す操作はすべてここを通す（テストでは差し替えて出さない）。"""
        win.deiconify()
        win.lift()
        win.focus_force()

    def notify(self, title: str, text: str, error: bool = False):
        (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=self)

    def _hwnd(self) -> int:
        try:
            return int(self.wm_frame(), 16)
        except (tk.TclError, ValueError):
            return 0

    def _refuse(self, name: str, reason: str, detail: str = ""):
        self.status.set(T('移動しませんでした: {0} — {1}', name, reason))
        self.notify(T('ごみ箱へ移動'), T('「{0}」は{1}。\n{2}', name, reason, detail or T('移動しませんでした。')), error=True)

    def _move(self, name: str, path: str, size: int, is_dir: bool = False) -> bool:
        try:
            self._trash_move(path, size, is_dir)
            return True
        except trash.TrashError as exc:
            self.status.set(T('ごみ箱へ移動できませんでした: {0} — {1}', name, exc))
            self.notify(T('ごみ箱へ移動できませんでした'), T('「{0}」\n\n理由: {1}', name, exc), error=True)
            return False
        finally:
            self._flush_trash_records()

    def _trash_move(self, path: str, size: int, is_dir: bool):
        """ごみ箱へ移す（すべての経路がここを通る）。移せたら、あとでごみ箱の中の項目を特定して記録する。"""
        t0 = time.time()
        trash.move_to_trash(path, size, hwnd=self._hwnd())
        self._pending_moves.append((path, is_dir, size, t0))

    def _flush_trash_records(self):
        """移した項目を、ごみ箱の中の組（$R / $I）と結び付けて記録する（まとめて 1 回ごみ箱を読む）。"""
        if not self._pending_moves:
            return
        moved, self._pending_moves = self._pending_moves, []
        try:
            entries = recyclebin.locate(moved, self.ledger.claimed())
            self.ledger.add(entries)
        except OSError as exc:
            self.status.set(self.status.get() + T('（ごみ箱の記録を保存できませんでした: {0}）', exc))
            return
        if hasattr(self, "bin_tree"):
            self.render_bin()

    def trash_file(self, item: FileItem, extra_facts=None, extra_cautions=None) -> bool:
        """確認のうえ 1 ファイルをごみ箱へ移す。移した場合だけ True。

        extra_facts / extra_cautions は重複の整理から渡す。
        「どれを残すのか」を移す直前の画面にも出すためで、判断そのものは変えない。
        """
        if item not in self.dir_index.get(item.dir, []):
            return False  # 既に一覧にない（移動済みなど）
        reason = self.protected_reason(str(item.path), False)
        if reason:
            self._refuse(item.name, reason, T('このアプリからは移動できません。'))
            return False
        problem = self.check_unchanged(item)
        if problem:
            self._refuse(item.name, problem, T('古い情報のままでは移動しません。再スキャンしてから操作してください。'))
            return False
        kind_text, cautions, risky = self.trash_risk(item)
        spec = {"name": item.name, "path": str(item.path), "is_folder": False, "risky": risky,
                "cautions": list(extra_cautions or []) + list(cautions), "warnings": [],
                "facts": [(T('容量'), T('{0}（{1:,} バイト）', human_size(item.size), item.size)),
                          (T('更新日時'), fmt_date(item.modified)), (T('種類'), kind_text)]
                         + list(extra_facts or [])}
        if not self.confirm_trash(spec):
            self.status.set(T('キャンセルしました（{0} は変更していません）', item.name))
            return False
        problem = self.check_unchanged(item)  # 確認画面を開いている間の変化も確かめる
        if problem:
            self._refuse(item.name, problem, T('移動しませんでした。再スキャンしてください。'))
            return False
        if not self._move(item.name, str(item.path), item.size, False):
            return False
        self._remove_file_from_index(item)
        self.status.set(T('「{0}」（{1}）をごみ箱へ移動しました。ごみ箱から元に戻せます。', item.name, human_size(item.size)))
        return True

    def trash_folder(self, folder: Path) -> bool:
        """確認のうえフォルダを中身ごとごみ箱へ移す。移した場合だけ True。"""
        fs = str(folder)
        name = folder.name or fs
        if folder not in self.folder_sizes:
            return False
        reason = self.protected_reason(fs, True)
        if reason:
            self._refuse(name, reason, T('このアプリからは移動できません。'))
            return False
        if trash.is_link_or_junction(fs):
            self._refuse(name, T('リンク（ジャンクション）のため'), T('リンク先を誤って移動しないよう、対象外にしています。'))
            return False
        try:
            now = trash.measure_folder(fs)  # スキャン時点の集計を確定値として扱わず、数え直す
        except FileNotFoundError:
            self._refuse(name, T('スキャン後に移動または削除されたため、元の場所に見つかりません'),
                         T('古い情報のままでは移動しません。再スキャンしてください。'))
            return False
        scan_files, scan_size = self.folder_file_counts.get(folder, 0), self.folder_sizes.get(folder, 0)
        scan_dirs = sum(1 for p in self.folder_sizes if p != folder and str(p).startswith(subtree_prefix(fs)))
        warnings = []
        if (now.files, now.size) != (scan_files, scan_size):
            warnings.append(T('スキャン後に中身が変化しています（ファイル {0:,} → {1:,} 件、容量 {2} → {3}）。スキャン時点の件数・容量は確定値ではありません。移動するのは現在の中身です。', scan_files, now.files, human_size(scan_size), human_size(now.size)))
        if now.errors:
            warnings.append(T('読み取れない項目が {0:,} 件あり、現在の件数・容量は一部しか確認できていません。', now.errors))
        kind_text, risky, cautions = self._folder_kind_risk(fs)
        spec = {"name": name, "path": fs, "is_folder": True, "risky": risky, "warnings": warnings,
                "cautions": cautions,
                "facts": [(T('スキャン時点'), T('{0:,} ファイル・サブフォルダ {1:,}・合計 {2}', scan_files, scan_dirs, human_size(scan_size))),
                          (T('現在（再確認）'), T('{0:,} ファイル・サブフォルダ {1:,}・合計 {2}（{3:,} バイト）', now.files, now.dirs, human_size(now.size), now.size)),
                          (T('種類'), kind_text)]}
        if not self.confirm_trash(spec):
            self.status.set(T('キャンセルしました（{0} は変更していません）', name))
            return False
        try:
            again = trash.measure_folder(fs)
        except FileNotFoundError:
            again = None
        if again is None or again.key() != now.key():
            self._refuse(name, T('確認画面を表示している間に中身が変化しました'), T('移動しませんでした。内容を確かめてからやり直してください。'))
            return False
        if not self._move(name, fs, again.size, True):
            return False
        self._remove_folder_from_index(folder)
        self.status.set(T('フォルダ「{0}」（{1:,} ファイル・{2}）を中身ごとごみ箱へ移動しました。ごみ箱から元に戻せます。', name, now.files, human_size(now.size)))
        return True

    def _folder_kind_risk(self, fs: str):
        """フォルダの種類の表示・注意を強めるか・注意の文（確認画面用）。"""
        ctx = self.ctx_for(fs)
        stats = self.folder_breakdown(fs)
        total = sum(v[0] for v in stats.values())
        dominant = max(stats, key=lambda k: stats[k][0]) if stats else None
        if dominant and total and stats[dominant][0] / total < 0.5:
            dominant = None
        _, flabel, _, _ = folder_kind(ctx, fs, dominant)
        kind_text = flabel + (T(' ／ 関連: {0}', L(ctx.group_label)) if ctx.group_label else "")
        risky = (self._risky_ctx(ctx) or ctx.container in ("deps", "vcs", "saves")
                 or (ctx.group_key == fs and ctx.group_type is not None))
        return kind_text, risky, folder_cautions(ctx, dominant)

    # --- 移動後の索引・表示の更新 ---
    def _subtract_from_ancestors(self, start: Path, size: int, count: int):
        cursor = start
        while True:
            if cursor in self.folder_sizes:
                self.folder_sizes[cursor] -= size
                self.folder_file_counts[cursor] -= count
            if cursor == self.scan_root or cursor.parent == cursor:
                break
            cursor = cursor.parent

    def _remove_file_from_index(self, item: FileItem):
        self._remove_files_from_index([item])

    def _remove_files_from_index(self, items: list[FileItem], refresh: bool = True):
        """移したファイルを索引から外す。何件でも、描き直しは最後に 1 回だけ。

        返り値は「消えたものか」を答える関数（詳細画面の更新に使う）。
        """
        ids = {id(i) for i in items}
        pred = lambda kind, target: kind == "file" and id(target) in ids  # noqa: E731
        if not items:
            return pred
        for i in ids:
            self.check_files.pop(i, None)
        self.file_rows = [f for f in self.file_rows if id(f) not in ids]
        by_dir: dict[str, list[FileItem]] = defaultdict(list)
        for item in items:
            by_dir[item.dir].append(item)
        direct = self.treemap.direct_file_sizes
        for d, gone in by_dir.items():
            siblings = [f for f in self.dir_index.get(d, []) if id(f) not in ids]
            self.dir_index[d] = siblings
            size = sum(i.size for i in gone)
            folder = Path(d)
            direct[folder] = direct.get(folder, 0) - size
            self._subtract_from_ancestors(folder, size, len(gone))
            self.total_size -= size
            # 同じフォルダのファイルは周囲のファイル名も手がかりにしているため分類し直す
            ctx = self.ctx_for(d)
            names = self.sibling_names(d)
            for f in siblings:
                f.kind = classify_file(f.name, f.size, ctx, names).kind
        # 重複結果にも同じファイルが載っている。ここで外さないと、
        # 既に無いものを「重複」として見せ続けることになる。
        self._drop_paths_from_dups({str(i.path) for i in items})
        if refresh:
            self._refresh_after_trash(pred)
        return pred

    def _remove_folder_from_index(self, folder: Path, refresh: bool = True):
        fs = str(folder)
        prefix = subtree_prefix(fs)
        inside = lambda d: d == fs or d.startswith(prefix)  # noqa: E731
        size, count = self.folder_sizes.get(folder, 0), self.folder_file_counts.get(folder, 0)
        removed = set()
        for d in [d for d in self.dir_index if inside(d)]:
            removed.update(id(f) for f in self.dir_index.pop(d))
            self._dir_lower.pop(d, None)
        self.file_rows = [f for f in self.file_rows if id(f) not in removed]
        direct = self.treemap.direct_file_sizes
        for p in [p for p in self.folder_sizes if inside(str(p))]:
            self.folder_sizes.pop(p, None)
            self.folder_file_counts.pop(p, None)
            direct.pop(p, None)
            self.treemap.child_dirs.pop(p, None)
            self.folder_ctx.pop(str(p), None)
            self._folder_lookup.pop(os.path.normcase(str(p)), None)
        siblings = self.treemap.child_dirs.get(folder.parent)
        if siblings and folder in siblings:
            siblings.remove(folder)
        self._subtract_from_ancestors(folder.parent, size, count)
        self.total_size -= size
        self.folder_rows = [row for row in self.folder_rows if not inside(str(row[0]))]
        # 消えたフォルダを指す絞り込み条件は外す（該当 0 件のまま残さない）
        loc = self.filters.get("folder")
        if loc and inside(loc[0]):
            self.filters.pop("folder")
        grp = self.filters.get("group")
        if grp and inside(grp):
            self.filters.pop("group")
        if self.treemap.current_root is not None and inside(str(self.treemap.current_root)):
            self.treemap.current_root = folder.parent
            self.treemap._update_nav()

        # フォルダごと消えたなら、その中のファイルも重複結果・チェックから外す
        self._drop_paths_from_dups(set(), inside)
        for key in [k for k in self.check_dirs if inside(k)]:
            self.check_dirs.pop(key, None)
        for key in [k for k, f in self.check_files.items() if id(f) in removed]:
            self.check_files.pop(key, None)

        def is_removed(kind, target):
            if kind == "file":
                return inside(target.dir)
            return kind == "folder" and inside(str(target))
        if refresh:
            self._refresh_after_trash(is_removed)
        return is_removed

    def _refresh_after_trash(self, is_removed):
        self.folder_rows = [(p, self.folder_sizes.get(p, 0), self.folder_file_counts.get(p, 0), g)
                            for p, _, _, g in self.folder_rows]
        self.summary_vars["scanned"].set(human_size(self.total_size))
        self.summary_vars["files"].set(T('{0:,} ファイル', len(self.file_rows)))
        self.summary_vars["folders"].set(T('{0:,} フォルダ', len(self.folder_rows)))
        self.treemap._update_nav()      # 階層・容量の関係・中身の一覧も数え直す
        self.treemap.redraw()
        if self.location_picker is not None and self.location_picker.winfo_exists():
            self.location_picker.reset()
        self.render_folders()
        self.render_files()
        self._update_check_views()
        if self.detail is not None and self.detail.winfo_exists() and self.detail.current:
            kind, target = self.detail.current
            if kind in ("file", "folder") and is_removed(kind, target):
                self.detail.show_removed(target)
            else:
                self.detail.refresh()

    def _check_menu_items(self, menu, which: str, kind: str, target, name: str, single_trash):
        """右クリックの「ごみ箱へ移動」まわり。チェックの中の行か外の行かで、対象をはっきり分ける。

        - チェックの中の行（☑・チェックしたフォルダの中 ▣）: チェックした全件が対象
        - チェックの外の行: その 1 件だけが対象（ほかの行のチェックは外さない）
        返り値: メニューを作ったら True（チェックが 1 件も無いときは False で、従来の選択の動作に任せる）
        """
        rows = self._checked_rows()
        if not rows:
            return False
        folders, files, _n = self._dedupe_nested(rows)
        total = sum(self.folder_sizes.get(f, 0) for f in folders) + sum(i.size for i in files)
        n_hidden, _detail = self._hidden_checked(which)
        head = T('チェック {0:,} 件 ・ 合計 {1}', len(rows), human_size(total))
        if n_hidden:
            head += T('（うち {0:,} 件はこの一覧に表示されていません）', n_hidden)
        mark = self._check_mark(kind, target)
        menu.add_separator()
        menu.add_command(label=head, state="disabled")
        if mark in ("☑", "▣"):
            menu.add_command(label=T('チェックした {0:,} 件をごみ箱へ移動…', len(rows)),
                             command=lambda: self.trash_checked(which))
            if mark == "☑":
                menu.add_command(label=T('この行のチェックを外す'),
                                 command=lambda: (self._set_checked(kind, target, False), self._update_check_views()))
        else:
            menu.add_command(label=T('この 1 件（{0}）だけをごみ箱へ移動…', name), command=single_trash)
            menu.add_command(label=T('チェックした {0:,} 件をごみ箱へ移動…（この行は含みません）', len(rows)),
                             command=lambda: self.trash_checked(which))
            menu.add_command(label=T('この行にもチェックを付ける'),
                             command=lambda: (self._set_checked(kind, target, True), self._update_check_views()))
        return True

    def _file_context_menu(self, event):
        """右クリックの選択の扱いを、Windows の一覧と同じにする。

        既に選んである行の上で押したときは**選択を崩さない**（複数選んでから
        右クリック、ができなくなるため）。選んでいない行の上なら、その 1 行に
        切り替える。行の外なら何もしない。チェックがあるときは、チェックの中の行か外の行かで
        「チェックした全件」と「この 1 件」を分けて出す（チェックは勝手に外さない）。
        """
        row = self.file_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.file_tree.selection():
            self.file_tree.selection_set(row)
        self.file_tree.focus(row)
        self._update_file_selection_note()
        item = self._file_iids.get(row)
        menu = tk.Menu(self, tearoff=0)
        if item is not None and self._checked_rows():
            menu.add_command(label=T('詳細を表示'), command=lambda: self.show_file_detail(item))
            menu.add_command(label=T('エクスプローラーで開く'), command=lambda: open_in_file_manager(item.path))
            self._check_menu_items(menu, "files", "file", item, item.name, lambda: self.trash_file(item))
            menu.tk_popup(event.x_root, event.y_root)
            return
        items = self._selected_files()
        many = len(items) > 1
        if many:
            total = sum(i.size for i in items)
            menu.add_command(label=T('選択 {0:,} 件 ・ 合計 {1}', len(items), human_size(total)), state="disabled")
            menu.add_separator()
            menu.add_command(label=T('詳細を表示（1 件だけ選んでください）'), state="disabled")
        else:
            menu.add_command(label=T('詳細を表示'), command=self.detail_file_selected)
        menu.add_command(label=T('エクスプローラーで開く'), command=self.open_file_selected,
                         state="disabled" if many else "normal")
        menu.add_separator()
        menu.add_command(label=T('移動のチェックを付ける（{0:,} 件）', len(items)) if many else T('移動のチェックを付ける'),
                         command=lambda: self._space_check("files"))
        menu.add_command(label=(T('選んだ {0:,} 件をごみ箱へ移動…', len(items)) if many else T('ごみ箱へ移動…')),
                         command=self.trash_selected_file)
        menu.tk_popup(event.x_root, event.y_root)

    def _folder_context_menu(self, event):
        """右クリック。ファイル一覧と同じく、選んである行の上なら選択を崩さない。"""
        row = self.folder_tree.identify_row(event.y)
        if not row or row.endswith("|more") or row.endswith(PLACEHOLDER_SUFFIX):
            return
        if row not in self.folder_tree.selection():
            self.folder_tree.selection_set(row)
        self.folder_tree.focus(row)
        self._update_folder_here()
        self._update_folder_selection_note()
        menu = tk.Menu(self, tearoff=0)
        kind, target = self._folder_row_target(row)
        if kind is not None and target is not None and self._checked_rows():
            name = target.name if kind == "file" else (target.name or str(target))
            menu.add_command(label=T('詳細を表示'),
                             command=lambda: (self.show_file_detail(target) if kind == "file"
                                              else self.show_folder_detail(target)))
            menu.add_command(label=T('エクスプローラーで開く'),
                             command=lambda: open_in_file_manager(target.path if kind == "file" else target))
            single = (lambda: self.trash_file(target)) if kind == "file" else (lambda: self.trash_folder(target))
            self._check_menu_items(menu, "folders", kind, target, name, single)
            menu.tk_popup(event.x_root, event.y_root)
            return
        rows = self._selected_folder_rows()
        if len(rows) > 1:
            folders, files, _n = self._dedupe_nested(rows)
            total = sum(self.folder_sizes.get(f, 0) for f in folders) + sum(i.size for i in files)
            menu.add_command(label=T('選択 {0:,} 件 ・ 合計 {1}', len(rows), human_size(total)), state="disabled")
            menu.add_separator()
            menu.add_command(label=T('詳細を表示（1 件だけ選んでください）'), state="disabled")
            menu.add_separator()
            menu.add_command(label=T('移動のチェックを切り替える（{0:,} 件）', len(rows)),
                             command=lambda: self._space_check("folders"))
            menu.add_command(label=T('ごみ箱へ移動（{0:,} 件）', len(rows)), command=self.trash_selected_folder)
            menu.tk_popup(event.x_root, event.y_root)
            return
        kind, target = self._selected_folder_row()
        menu.add_command(label=T('詳細を表示'), command=self.detail_folder_selected)
        if kind == "dir" and target is not None:
            menu.add_command(label=T('中身を開く'),
                             command=lambda p=target: (self.folder_tree.item(str(p), open=True),
                                                       self._fill_folder_children(str(p)),
                                                       self._update_folder_here()))
            menu.add_command(label=T('容量マップで表示'), command=lambda p=target: self.show_in_map(p))
            menu.add_command(label=T('ファイル一覧でこの場所を見る'), command=self.filter_to_selected_folder)
        elif kind == "file" and target is not None:
            menu.add_command(label=T('ファイル一覧でこのフォルダを見る'), command=self.filter_to_selected_folder)
        menu.add_command(label=T('エクスプローラーで開く'), command=self.open_folder_row_selected)
        menu.add_separator()
        menu.add_command(label=T('移動のチェックを切り替える'), command=lambda: self._space_check("folders"))
        menu.add_command(label=T('ごみ箱へ移動'), command=self.trash_selected_folder)
        menu.tk_popup(event.x_root, event.y_root)

if __name__ == "__main__":
    enable_dpi_awareness()
    App().mainloop()
