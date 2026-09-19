"""記事HTML本文抽出ユーティリティ.

依存追加なしのヒューリスティック実装。trafilatura がインストール済みの環境では
そちらを優先使用する（requirements.txt には追加しない＝オプション扱い）。

ペイウォール・JSレンダリングサイトでは本文が取れないことがあるが、その場合は
空文字列を返し、呼び出し側が従来のスニペットにフォールバックする。
"""

from __future__ import annotations

import html as _html
import logging
import re

logger = logging.getLogger(__name__)

# 本文と無関係なブロック要素を丸ごと除去（開始〜終了タグ、後方参照で対応付け）
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|aside|form)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL)
_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(fragment: str) -> str:
    """HTMLフラグメントからタグを除去し、実体参照を解決して平文化する."""
    text = _TAG_RE.sub("", fragment)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _extract_paragraphs(scope: str, min_paragraph_chars: int) -> list[str]:
    """スコープ内の <p> タグから、本文らしい長さの段落のみを抽出する."""
    paragraphs = []
    for m in _P_RE.finditer(scope):
        ptext = _strip_tags(m.group(1))
        if len(ptext) >= min_paragraph_chars:
            paragraphs.append(ptext)
    return paragraphs


def extract_main_text(
    html: str,
    max_chars: int = 3000,
    min_paragraph_chars: int = 60,
) -> str:
    """HTMLから記事本文らしいテキストを抽出する.

    優先順位:
      1. trafilatura（インストール済みの場合のみ）
      2. <article> タグ内の長い <p> 段落
      3. ドキュメント全体の長い <p> 段落

    Returns:
        本文テキスト（先頭 max_chars 文字）。抽出できなければ空文字列。
    """
    if not html:
        return ""

    # 1) trafilatura があれば優先（精度が高い）
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html) or ""
        if len(text) >= 200:
            return text[:max_chars]
    except ImportError:
        pass
    except Exception:
        logger.debug("trafilatura extraction failed — falling back to heuristic")

    # 2) ヒューリスティック抽出
    cleaned = _COMMENT_RE.sub("", html)
    cleaned = _SCRIPT_STYLE_RE.sub("", cleaned)

    m = _ARTICLE_RE.search(cleaned)
    paragraphs: list[str] = []
    if m:
        paragraphs = _extract_paragraphs(m.group(1), min_paragraph_chars)
    if not paragraphs:
        paragraphs = _extract_paragraphs(cleaned, min_paragraph_chars)

    return "\n".join(paragraphs)[:max_chars]
