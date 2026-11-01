"""記事HTML本文抽出ユーティリティ.

trafilatura（requirements.txt に記載）を優先使用し、入っていない環境では依存なしの
ヒューリスティック実装にフォールバックする。どちらの結果にも、重複行の除去と
媒体ごとの既知の定型文（寄付のお願い・イベント広告・会員登録案内など）の除去をかける。

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

# 本文に紛れ込む定型文（1行まるごと捨てる）。2026-10〜11 の実データで確認したもの。
# 記事本文の普通の文を消さないよう、媒体固有の言い回しに絞る。
_BOILERPLATE_LINE_RES = [
    re.compile(p) for p in (
        # GIGAZINE: 寄付のお願い・支援者リスト・カテゴリ/投稿者行・英語版への案内
        r"親愛なる読者の皆さまへ",
        r"GIGAZINEを支援してくれたメンバー",
        r"^in .{1,80}, Posted by [\w.-]+$",
        r"^You can read the machine translated English article",
        # ITmedia: 会員限定の案内・著作権表示・生成AI利用の注記
        r"この記事は会員限定です",
        r"^Copyright ©",
        r"本記事は制作段階でChatGPT等の生成系AIサービスを利用しています",
        r"^続きを読む",
        r"^コーナーリンク$",
        r"^※試験運用中です$",
        r"^こんなメディアも見られています$",
        r"に関連する情報をお探しであれば",
        # どの媒体にも出る、1語だけのナビゲーション行・広告行（行全体が一致するときだけ）
        r"^(関連記事|関連リンク|関連ニュース|コメント|page|PR|広告|- ?\d{1,2})$",
        r"【PR】$",
        # Publickey: 前後の記事へのリンク
        r"^≪前の記事",
        r"次の記事≫$",
        # Wired: コメント欄・ページ先頭へのリンク
        r"^(Comments|Back to top)$",
        # MIT Technology Review: ニュースレター登録の案内
        r"^Stay connected$",
        r"^Get the latest updates from$",
        r"^MIT Technology Review$",
        r"^Discover special offers, top stories, upcoming events, and more\.$",
        # VentureBeat: 寄稿プログラムの案内
        r"^Welcome to the VentureBeat community!$",
        r"^Our guest posting program is where",
        r"^Read more from our guest post program",
    )
]
# 広告の決まり文句。本文の文にも出うるので、広告らしい短い行（200字以下）だけに当てる
_SHORT_AD_LINE_MAX = 200
_SHORT_AD_LINE_RES = [
    re.compile(p) for p in (
        # TechCrunch: 自社イベント（Founder Summit / Disrupt）の広告
        r"(?i)\bregister (now|here)\b",
        r"(?i)^buy one .{0,40}ticket",
        r"(?i)\bget your pass\b",
    )
]


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


def clean_extracted_text(text: str) -> str:
    """抽出した本文から、重複行（The Verge のリード重複など）と既知の定型文を除く."""
    seen: set[str] = set()
    kept: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line in seen:
            continue
        if any(p.search(line) for p in _BOILERPLATE_LINE_RES):
            continue
        if len(line) <= _SHORT_AD_LINE_MAX and any(p.search(line) for p in _SHORT_AD_LINE_RES):
            continue
        seen.add(line)
        kept.append(line)
    return "\n".join(kept)


def extract_main_text(
    html: str,
    max_chars: int = 3000,
    min_paragraph_chars: int = 60,
) -> str:
    """HTMLから記事本文らしいテキストを抽出する.

    優先順位:
      1. trafilatura（インストール済みの場合）
      2. <article> タグ内の長い <p> 段落
      3. ドキュメント全体の長い <p> 段落

    Returns:
        本文テキスト（定型文を除いた先頭 max_chars 文字）。抽出できなければ空文字列。
    """
    if not html:
        return ""

    # 1) trafilatura があれば優先（精度が高い）
    try:
        import trafilatura  # type: ignore

        text = clean_extracted_text(trafilatura.extract(html) or "")
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

    return clean_extracted_text("\n".join(paragraphs))[:max_chars]
