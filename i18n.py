"""表示する文言の切り替え（日本語 / English）と、言語の設定の保存。

## 使い方

    T("次の {0:,} 件を移動します", n)     # 今の言語の文言。日本語の文をそのまま鍵にする
    N_("すべて")                           # 印だけ（読み込み時に決まる定数）。表示する場所で T() を通す
    L(ctx.group_label)                     # 分類処理（classifier.py）が作った日本語の文を、表示の直前に訳す

英語の訳は i18n_en.py の EN（鍵は日本語の文、値は英語の文）。訳が無い鍵は日本語のまま出てしまうので、
test_i18n.py が「すべての文言に訳があること」「英語の画面に日本語が残らないこと」を確かめる。

## 分類処理の文（L）

classifier.py は、スキャンした結果（フォルダの関連アプリ・保存場所など）を日本語の文で持つ。
スキャン中に言語を決め打ちすると、あとで言語を切り替えたときに古い言語の文が残るため、
分類処理の文は日本語のまま持ち、表示の直前に L() で訳す。L() は i18n_classifier.py の型
（「ゲーム: {}（Steam）」など）で文を読み解き、利用者のデータ（フォルダ名・ファイル名）は訳さずに残す。

## 設定の保存

%LOCALAPPDATA%\\PCManagementCenter\\settings.json（ごみ箱の記録 trash_log.json とは別のファイル）。
壊れていても起動できるよう、読めなければ「未設定」として扱う。
"""

from __future__ import annotations

import json
import os

LANGUAGES = {"ja": "日本語", "en": "English"}
LANG = "ja"
_EN: dict[str, str] | None = None
_CLS: list | None = None


def _en() -> dict[str, str]:
    global _EN
    if _EN is None:
        from i18n_en import EN
        _EN = EN
    return _EN


def set_language(lang: str) -> None:
    global LANG
    LANG = lang if lang in LANGUAGES else "ja"
    _L_CACHE.clear()


def T(text: str, *args, **kwargs) -> str:
    """今の言語の文言。引数があれば str.format で埋める。"""
    template = _en().get(text, text) if LANG == "en" else text
    if args or kwargs:
        return template.format(*args, **kwargs)
    return template


def N_(text: str) -> str:
    """翻訳する文言だという印（読み込み時に決まる定数用）。表示するときに T() を通す。"""
    return text


def DATA(text: str) -> str:
    """表示しない日本語（ファイル名の照合に使う「コピー」「旧」など）。翻訳しない印。"""
    return text


# --- 分類処理の文（classifier.py）を表示の直前に訳す ---------------------------------

def _classifier_templates():
    """(型を {} で分けた文字列の並び, 英語の型, 各値を訳すか) の一覧。"""
    global _CLS
    if _CLS is None:
        from i18n_classifier import TEMPLATES
        en = _en()
        _CLS = [(ja.split("{}"), en[ja], flags) for ja, flags in TEMPLATES if "{}" in ja and ja in en]
    return _CLS


def _splits(parts: list[str], text: str, limit: int = 64):
    """型の固定部分 parts が text に当てはまる区切り方を、すべて（多くても limit 通り）挙げる。

    「データ（プログラム・…）です。（ゲーム: X（Steam））」のように同じ記号が何度も出る文は、
    最初に見つかった所で区切ると誤る。区切り方の候補を全部試して、いちばんよく訳せるものを選ぶ。
    """
    head, tail = parts[0], parts[-1]
    if not text.startswith(head) or not text.endswith(tail) or len(text) < len(head) + len(tail):
        return
    body = text[len(head):len(text) - len(tail)] if tail else text[len(head):]
    middles = parts[1:-1]
    out = []

    def walk(pos: int, k: int, groups: list[str]):
        if len(out) >= limit:
            return
        if k == len(middles):
            if pos <= len(body):
                out.append(groups + [body[pos:]])
            return
        lit = middles[k]
        start = pos + 1                     # 値は 1 文字以上
        while True:
            i = body.find(lit, start)
            if i < 0:
                return
            walk(i + len(lit), k + 1, groups + [body[pos:i]])
            start = i + 1

    if len(body) >= len(middles) + 1:
        walk(0, 0, [])
    yield from (g for g in out if all(g))


_L_CACHE: dict[str, str] = {}
_LIST_SPLIT = __import__("re").compile(r"・|、|, ")


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "　" <= ch <= "ヿ" or "㐀" <= ch <= "鿿" or "＀" <= ch <= "￯")


def L(text: str | None) -> str:
    """分類処理が作った日本語の文を、今の言語で返す（日本語ならそのまま）。

    知っている型に当てはまらない文（利用者のフォルダ名など）は、そのまま返す。
    当てはまる区切り方が複数あるときは、日本語がいちばん残らないもの（全部訳せるもの）を選ぶ。
    """
    if text is None:
        return ""
    if LANG != "en" or not text:
        return text
    en = _en()
    if text in en:
        return en[text]
    hit = _L_CACHE.get(text)
    if hit is not None:
        return hit
    # 表示される文全体に残る日本語の量が少ない読み解きを選ぶ。同じなら、固定部分の長い型（より具体的な型）。
    # 利用者の日本語の名前は、どの読み解きでも同じだけ残るので、比べる妨げにはならない。
    best, best_key = text, (_cjk_count(text), 0)
    if best_key[0]:
        for parts, tpl, flags in _classifier_templates():
            fixed = sum(len(p) for p in parts)
            for groups in _splits(parts, text):
                args = [L(g) if (i < len(flags) and flags[i]) else g for i, g in enumerate(groups)]
                try:
                    cand = tpl.format(*args)
                except (IndexError, KeyError):
                    continue
                key = (_cjk_count(cand), -fixed)
                if key < best_key:
                    best, best_key = cand, key
        # 「A・B」「A、B」「A, B」のように並べた一覧は、1 つずつ訳す
        pieces = _LIST_SPLIT.split(text)
        if best_key[0] and len(pieces) > 1:
            cand = ", ".join(L(x) for x in pieces)
            key = (_cjk_count(cand), 0)
            if key < best_key:
                best, best_key = cand, key
    if len(_L_CACHE) > 20000:
        _L_CACHE.clear()
    _L_CACHE[text] = best
    return best


# --- 言語の設定 -------------------------------------------------------------------

def default_settings_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PCManagementCenter", "settings.json")


def load_settings(path: str) -> dict:
    """{"language": "ja"|"en"|None, "ask": bool, "error": str}。壊れていても例外にしない。"""
    out = {"language": None, "ask": True, "error": ""}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as exc:
        out["error"] = str(exc)
        return out
    if not isinstance(data, dict):
        out["error"] = "not an object"
        return out
    lang = data.get("language")
    if lang in LANGUAGES:
        out["language"] = lang
        out["ask"] = not bool(data.get("dont_ask", False))
    return out


def save_settings(path: str, language: str, dont_ask: bool) -> None:
    """言語を保存する（書き終えてから置き換えるので、途中で壊れない）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
            if isinstance(old, dict):
                data = old
    except (OSError, ValueError):
        pass
    data.update({"version": 1, "language": language, "dont_ask": bool(dont_ask)})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)
