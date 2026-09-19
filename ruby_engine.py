"""ルビ（ふりがな）自動付与エンジン.

形態素解析器 fugashi (MeCab + unidic-lite) で文全体を解析し、文脈に基づく
正確な読みでルビ記法 漢字(ふりがな) を付与する。

旧実装（script_generator.add_ruby_to_text）は漢字の連なりだけを切り出して
pykakasi に渡していたため、送り仮名の文脈が失われ誤読が多発した:
    作る → 作(さく)る ／ 新しい → 新(しん)しい ／ 賢くなる → 賢(さとし)くなる
本エンジンは文全体を解析するため、これらを正しく読める:
    作る → 作(つく)る ／ 新しい → 新(あたら)しい ／ 賢くなる → 賢(かしこ)くなる

設計:
- 連続する漢字含みトークンは1つのルビ単位に結合する
  （大[ダイ]+変身[ヘンシン] → 大変身(だいへんしん)。分割表示や基本漢字スキップ
   による「隣接漢字へのルビ誤適用」を防ぐ）
- トークンの送り仮名（前後の共通かな）はルビの外に出す（見逃せ → 見逃(みのが)せ）
- 既存のルビ注釈（手修正・英語カタカナ読み）は保護して触らない
- 小1-2の基本漢字のみで構成される語にはルビを付けない（従来仕様を踏襲）
- fugashi が無い環境では pykakasi の文全体変換にフォールバック
  （旧実装と違い文脈を保つため、フォールバックでも精度は大きく落ちない）
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 漢字（CJK統合漢字＋拡張A＋々）
_KANJI_CHAR_RE = re.compile(r"[一-鿿㐀-䶿々]")

# 既存ルビ注釈（保護対象）:
#   漢字(+送り仮名)(ふりがな) / English(カタカナ・ひらがな)
_PROTECTED_RUBY_RE = re.compile(
    r"([一-鿿㐀-䶿々]+[ぁ-ん]*)[(（]([ぁ-んァ-ヶー]+)[)）]"
    r"|"
    r"([A-Za-z0-9][A-Za-z0-9_\s\-\.]*[A-Za-z0-9]|[A-Za-z0-9])[(（]([ぁ-んァ-ヶー]+)[)）]"
)

# 漢字ベースのルビ注釈のみ（LLM出力の除去用。英語カタカナ読みは残す）
_KANJI_RUBY_STRIP_RE = re.compile(
    r"([一-鿿㐀-䶿々]+[ぁ-ん]*)[(（][ぁ-んァ-ヶー]+[)）]"
)

# 小1-2で習う基本漢字（これのみで構成される語はルビ不要）
_BASIC_KANJI = frozenset(
    "一二三四五六七八九十百千万円年月日時分秒上下左右中大小山川田"
    "人口目手足耳本文字学校先生子女男力気天火水金土木花草虫犬猫"
    "石竹糸車音雨空星村町名正出入立休早白青赤玉王何今見行来言"
)

_KATA_TO_HIRA = str.maketrans(
    {chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)}
)


def _kata_to_hira(text: str) -> str:
    """カタカナをひらがなへ変換する（長音「ー」はそのまま残す）."""
    return text.translate(_KATA_TO_HIRA)


def _is_kana(ch: str) -> bool:
    return "ぁ" <= ch <= "ん" or ch == "ー"


def strip_kanji_ruby(text: str) -> str:
    """漢字ベースのルビ注釈を除去する（英語カタカナ読みは保持）.

    LLM出力に混入した（誤りうる）漢字ルビを取り除き、本エンジンの自動付与に
    一本化するために使う。二重ルビ（例: 楽(らく)しく(たのしく)）は1回の置換
    では取り切れないため、固定点まで繰り返す。
    """
    prev = None
    while prev != text:
        prev = text
        text = _KANJI_RUBY_STRIP_RE.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Morphological backends
# ---------------------------------------------------------------------------

_tagger = None
_tagger_failed = False


def _get_tagger():
    """fugashi Tagger を遅延初期化する（失敗時は None）."""
    global _tagger, _tagger_failed
    if _tagger is None and not _tagger_failed:
        try:
            import fugashi
            _tagger = fugashi.Tagger()
        except Exception:
            _tagger_failed = True
            logger.warning(
                "fugashi unavailable — falling back to pykakasi sentence mode "
                "(install with: pip install fugashi unidic-lite)"
            )
    return _tagger


def _tokenize_fugashi(text: str) -> list[tuple[str, str, str]] | None:
    """fugashi で (表層形, ひらがな読み, 品詞) のリストを返す。読み不明は空文字."""
    tagger = _get_tagger()
    if tagger is None:
        return None
    tokens = []
    idx = 0
    for word in tagger(text):
        surface = word.surface
        # MeCabが読み飛ばした空白等を表層位置から復元する
        pos = text.find(surface, idx)
        if pos > idx:
            tokens.append((text[idx:pos], "", ""))
        if pos < 0:
            logger.debug("Surface not found in text (normalization?): %r", surface)
        idx = (pos if pos >= 0 else idx) + len(surface)
        kana = word.feature.kana or ""
        pos1 = word.feature.pos1 or ""
        tokens.append((surface, _kata_to_hira(kana) if kana else "", pos1))
    if idx < len(text):
        tokens.append((text[idx:], "", ""))
    return tokens


def _tokenize_pykakasi(text: str) -> list[tuple[str, str, str]]:
    """pykakasi の文全体変換で (表層形, ひらがな読み, 品詞="") のリストを返す.

    pykakasi は品詞情報を持たないため空文字を返す（結合判定は文字種のみで近似）。
    """
    import pykakasi
    kks = pykakasi.kakasi()
    return [(item["orig"], item["hira"], "") for item in kks.convert(text)]


# ---------------------------------------------------------------------------
# Annotation core
# ---------------------------------------------------------------------------

def _align_okurigana(surface: str, reading: str) -> tuple[str, str, str, str]:
    """表層形と読みの前後共通かなを送り仮名として分離する.

    Returns:
        (前置きかな, ルビ対象コア, コアの読み, 後置きかな)
    """
    # 後ろの共通かな（送り仮名）
    s_end, r_end = len(surface), len(reading)
    while (s_end > 0 and r_end > 0 and _is_kana(surface[s_end - 1])
           and surface[s_end - 1] == reading[r_end - 1]):
        s_end -= 1
        r_end -= 1
    # 前の共通かな（接頭の「お」など）
    s_start, r_start = 0, 0
    while (s_start < s_end and r_start < r_end and _is_kana(surface[s_start])
           and surface[s_start] == reading[r_start]):
        s_start += 1
        r_start += 1
    return (surface[:s_start], surface[s_start:s_end],
            reading[r_start:r_end], surface[s_end:])


def _annotate_mixed_core(core: str, reading: str) -> str | None:
    """漢字+かな+漢字 が混在するコアを、漢字の塊ごとのルビに分割する.

    例: 取り組/とりく → 取(と)り組(く)、思い出/おもいで → 思(おも)い出(で)
    表示用の正規表現は「漢字列＋かな列(読み)」しか解釈できないため、
    コア全体への一括ルビ（取り組(とりく)）は表示崩れになる。
    整合が取れない場合は None を返す（呼び出し側でルビなしにフォールバック）。
    """
    # コアを漢字の塊／かなの塊の交互の並びに分割（それ以外の文字が混ざれば断念）
    runs = re.findall(r"[一-鿿㐀-䶿々]+|[ぁ-んー]+", core)
    if "".join(runs) != core:
        return None

    out: list[str] = []
    pos = 0
    i = 0
    while i < len(runs):
        run = runs[i]
        if _KANJI_CHAR_RE.match(run[0]):
            # 漢字の塊: 読みは「次のかな塊が読み中に現れる位置」まで
            if i + 1 < len(runs):
                next_kana = runs[i + 1]
                idx = reading.find(next_kana, pos + 1)  # 漢字の読みは最低1文字
                if idx < 0:
                    return None
                kanji_reading = reading[pos:idx]
                pos = idx
            else:
                kanji_reading = reading[pos:]
                pos = len(reading)
            if not kanji_reading:
                return None
            out.append(f"{run}({kanji_reading})")
        else:
            # かなの塊: 読み側と一致して消費されるはず
            if not reading.startswith(run, pos):
                return None
            pos += len(run)
            out.append(run)
        i += 1

    if pos != len(reading):
        return None
    return "".join(out)


def _annotate_group(surfaces: list[str], readings: list[str]) -> str:
    """連続する漢字含みトークン群を1つのルビ単位として注釈する."""
    surface = "".join(surfaces)
    reading = "".join(readings)

    # 読みが取れないトークンが混ざっていたら無理に付けない
    if not reading or any(not r for r in readings):
        return surface

    # 全て基本漢字（＋かな）なら付けない（小1-2は読める前提）
    kanji_chars = [c for c in surface if _KANJI_CHAR_RE.match(c)]
    if kanji_chars and all(c in _BASIC_KANJI or c == "々" for c in kanji_chars):
        return surface

    prefix, core, core_reading, suffix = _align_okurigana(surface, reading)
    if not core or not core_reading or core == core_reading:
        return surface
    # コアが漢字で始まらない（解析の揺れ）場合は安全側でスキップ
    if not _KANJI_CHAR_RE.match(core[0]):
        return surface

    # コア内部に「かな→漢字」の並びがある場合は分割ルビ（表示崩れ防止）
    if re.search(r"[ぁ-んー][一-鿿㐀-䶿々]", core):
        segmented = _annotate_mixed_core(core, core_reading)
        if segmented is None:
            # 整合不能: 誤表示になるくらいならルビなしで返す
            return surface
        return f"{prefix}{segmented}{suffix}"

    return f"{prefix}{core}({core_reading}){suffix}"


def _annotate_segment(text: str) -> str:
    """保護領域を含まないプレーンテキストにルビを付与する."""
    if not _KANJI_CHAR_RE.search(text):
        return text

    tokens = _tokenize_fugashi(text)
    if tokens is None:
        tokens = _tokenize_pykakasi(text)

    out: list[str] = []
    group_s: list[str] = []
    group_r: list[str] = []

    def _flush() -> None:
        if group_s:
            out.append(_annotate_group(group_s, group_r))
            group_s.clear()
            group_r.clear()

    # 結合条件: 直前トークンの末尾が漢字、かつ現トークンの先頭が漢字、かつ
    # 現トークンが名詞・接辞のとき（複合名詞: 大+変身、人工+知能 など）。
    # 動詞・形容詞は結合しない（画面+見た → 画面(がめん)見た、
    # 一番+賢い → 一番(いちばん)賢(かしこ)い のように語の単位を保つ）。
    # 品詞が取れないフォールバック時（pykakasi）は文字種のみで近似する。
    _MERGEABLE_POS = ("名詞", "接尾辞", "接頭辞", "")
    prev_ends_kanji = False
    for surface, reading, pos1 in tokens:
        if _KANJI_CHAR_RE.search(surface):
            starts_kanji = bool(_KANJI_CHAR_RE.match(surface[0]))
            mergeable = prev_ends_kanji and starts_kanji and pos1 in _MERGEABLE_POS
            if group_s and not mergeable:
                _flush()
            group_s.append(surface)
            group_r.append(reading if reading else "")
            prev_ends_kanji = bool(_KANJI_CHAR_RE.match(surface[-1]))
        else:
            _flush()
            out.append(surface)
            prev_ends_kanji = False
    _flush()
    return "".join(out)


def annotate_ruby(text: str) -> str:
    """テキスト中の漢字に文脈に基づくルビ記法 漢字(ふりがな) を付与する.

    既存のルビ注釈（手修正・英語カタカナ読み）はそのまま保護する。
    """
    if not text:
        return text

    parts: list[str] = []
    last_end = 0
    for m in _PROTECTED_RUBY_RE.finditer(text):
        parts.append(_annotate_segment(text[last_end:m.start()]))
        parts.append(m.group(0))  # 既存注釈はそのまま
        last_end = m.end()
    parts.append(_annotate_segment(text[last_end:]))
    return "".join(parts)
