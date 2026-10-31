"""英語記事のドル金額（$X billion など）と日本語の「◯億ドル」の桁ずれを見つけて直す.

2026-11-01: 要約LLMが "$740 billion" を「740億ドル」（正しくは7400億ドル）と10倍小さく訳し、
事実確認AIも誤った要約と照らすので素通りした。billion=10億・million=100万・trillion=1兆の
換算は機械的に確かめられるので、LLMに任せず英語原文と突き合わせる。

直すのは「日本語の金額が英語原文のどの金額とも一致せず、かつ英語原文に
同じ数字（740 と 740）で 10/100/1000/10000 倍の金額がある」ときだけ（換算ミスの形に限る）。
誤訳で大きくなる向き（$5M →「5億ドル」）や「3億5000万ドル」の崩れは対象外（安全側）。
「原文にある金額」は通貨記号なし・mln・500M・500,000,000 なども広く拾い、
一致すれば直さない（原文の書き方を拾い損ねて正しい訳を壊さないため。QA 2026-11-01）。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_EN_UNITS = {
    "trillion": Decimal(10) ** 12, "tn": Decimal(10) ** 12, "t": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9, "bln": Decimal(10) ** 9, "bn": Decimal(10) ** 9, "b": Decimal(10) ** 9,
    "million": Decimal(10) ** 6, "mln": Decimal(10) ** 6, "mn": Decimal(10) ** 6, "m": Decimal(10) ** 6,
}
_JA_UNITS = {"兆": Decimal(10) ** 12, "億": Decimal(10) ** 8, "万": Decimal(10) ** 4}
_NUM = r"\d[\d,]*(?:\.\d+)?"

_EN_WORD_UNITS = r"trillion|billion|million|bln|mln|tn|bn|mn"
# 直す候補（ドルと明記された金額）: "$740 billion" / "US$1.2 trillion" / "USD 500 million" /
# "$500-million" / "$42.4B" / "740 billion dollars"
_EN_DOLLAR_RE = re.compile(
    rf"(?:(?:US\s?)?\$|\bUSD)\s?(?P<n1>{_NUM})(?:[\s-]*(?P<u1>{_EN_WORD_UNITS})\b|(?P<u3>[TBM])\b)"
    rf"|(?P<n2>{_NUM})[\s-]*(?P<u2>trillion|billion|million)[\s-]+(?:US\s+|U\.S\.\s+)?dollars",
    re.IGNORECASE,
)
# 原文にある金額（通貨を問わず広く拾う。ここに一致する日本語の金額は直さない）
_EN_LOOSE_RE = re.compile(
    rf"(?P<n1>{_NUM})(?:[\s-]*(?P<u1>{_EN_WORD_UNITS})\b|(?P<u3>[TBM])\b)"
    rf"|(?<![\d.,])(?P<big>\d{{1,3}}(?:,\d{{3}}){{2,}})(?![\d,])",
    re.IGNORECASE,
)
# 「7400億ドル」「1兆2000億ドル」「500万米ドル」（間の読みがなは事前に消す）
_JA_DOLLAR_RE = re.compile(
    rf"(?P<n1>{_NUM})\s*(?P<u1>[兆億万])(?:\s*(?P<n2>{_NUM})\s*(?P<u2>[億万]))?\s*(?:米)?ドル"
)
_READING_RE = re.compile(r"[(（][ぁ-んァ-ヶー・ ]+[)）]")
_FACTORS = (10, 100, 1000, 10000)


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def english_amounts(text: str) -> list[tuple[Decimal, Decimal]]:
    """英語本文のドル金額を [(書かれた数字, ドル額)] で返す."""
    out = []
    for m in _EN_DOLLAR_RE.finditer(_norm(text)):
        raw = _dec(m.group("n1") or m.group("n2"))
        unit = (m.group("u1") or m.group("u2") or m.group("u3")).lower()
        if raw is not None:
            out.append((raw, raw * _EN_UNITS[unit]))
    return out


def _known_values(text: str) -> set[Decimal]:
    """原文に出てくる金額らしい値を広く集める（ドル以外・単位なしの大きな数も含む）."""
    vals = {usd for _, usd in english_amounts(text)}
    for m in _EN_LOOSE_RE.finditer(_norm(text)):
        if m.group("big"):
            v = _dec(m.group("big"))
        else:
            raw = _dec(m.group("n1"))
            unit = (m.group("u1") or m.group("u3")).lower()
            v = raw * _EN_UNITS[unit] if raw is not None else None
        if v is not None:
            vals.add(v)
    return vals


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def ja_amount_text(usd: Decimal) -> str:
    """ドル額を日本語の数字表記にする（「ドル」は付けない）. 例: 7.4e11 → 7400億."""
    if usd >= _JA_UNITS["兆"]:
        cho, rest = divmod(usd, _JA_UNITS["兆"])
        if rest and rest % _JA_UNITS["億"] == 0:
            return f"{_fmt(cho)}兆{_fmt(rest / _JA_UNITS['億'])}億"
        return f"{_fmt(usd / _JA_UNITS['兆'])}兆"
    if usd >= _JA_UNITS["億"]:
        return f"{_fmt(usd / _JA_UNITS['億'])}億"
    if usd >= _JA_UNITS["万"]:
        return f"{_fmt(usd / _JA_UNITS['万'])}万"
    return _fmt(usd)


def _ja_matches(text: str):
    for m in _JA_DOLLAR_RE.finditer(text):
        v1 = _dec(m.group("n1"))
        if v1 is None:
            continue
        value = v1 * _JA_UNITS[m.group("u1")]
        if m.group("n2"):
            v2 = _dec(m.group("n2"))
            if v2 is None or _JA_UNITS[m.group("u2")] >= _JA_UNITS[m.group("u1")]:
                continue
            value += v2 * _JA_UNITS[m.group("u2")]
        yield m, v1, value


def _expected_for(raw: Decimal, value: Decimal, en: list[tuple[Decimal, Decimal]],
                  known: set[Decimal]) -> Decimal | None:
    """日本語の金額が換算ミスなら、英語原文での正しいドル額を返す."""
    if value in known:
        return None
    for en_raw, usd in en:
        if en_raw == raw and any(value * f == usd for f in _FACTORS):
            return usd
    return None


def fix_money_units(ja_text: str, en_source: str) -> tuple[str, list[str]]:
    """日本語文中の桁ずれしたドル金額を英語原文に合わせて直す. (直した文, 変更内容) を返す.

    読みがな付きの文（セリフ）には使わない（位置がずれるため）。要約・事実・題名用。
    """
    en = english_amounts(en_source)
    known = _known_values(en_source)
    if not ja_text or not en:
        return ja_text, []
    text = ja_text  # 全角記号を書き換えないよう正規化しない（\d と Decimal は全角数字も読める）
    changes: list[str] = []
    pieces, last = [], 0
    for m, raw, value in _ja_matches(text):
        usd = _expected_for(raw, value, en, known)
        if usd is None:
            continue
        before = text[m.start():m.end()]
        after = f"{ja_amount_text(usd)}{'米' if '米ドル' in before else ''}ドル"
        pieces += [text[last:m.start()], after]
        last = m.end()
        changes.append(f"{before} → {after}")
    if not changes:
        return ja_text, []
    pieces.append(text[last:])
    return "".join(pieces), changes


def suspicious_amounts(texts: list[str], en_source: str) -> list[str]:
    """題名・要点・セリフに残った桁ずれの疑い（「740億ドル → 7400億ドル」の形）を返す."""
    en = english_amounts(en_source)
    known = _known_values(en_source)
    if not en:
        return []
    found: list[str] = []
    for t in texts:
        clean = _READING_RE.sub("", _norm(t))
        for m, raw, value in _ja_matches(clean):
            usd = _expected_for(raw, value, en, known)
            if usd is not None:
                item = f"{m.group(0)} → {ja_amount_text(usd)}ドル"
                if item not in found:
                    found.append(item)
    return found


def english_amount_numbers(en_source: str) -> set[str]:
    """英語原文の金額を正しく日本語にしたときの数字（例: 7400）. 元記事にある数字として扱う."""
    nums: set[str] = set()
    for _, usd in english_amounts(en_source):
        nums.update(re.findall(r"\d+(?:\.\d+)?", ja_amount_text(usd)))
    return nums
