"""Phase 1: AIニュース収集モジュール.

RSSフィードとGoogle Custom Search APIからAIニュースを収集し、
LLM API（Gemini/Claude）でスコアリング・要約してTOP N記事を選定する。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

# 日付の既定値は日本時間（JST）基準（UTCだと深夜〜朝9時の実行で前日になる）
_JST = timezone(timedelta(hours=9))
from pathlib import Path

from email.utils import parsedate_to_datetime

import re

import feedparser
import httpx

logger = logging.getLogger(__name__)

# Maximum age (in hours) for an article to be considered "fresh"
_MAX_ARTICLE_AGE_HOURS = 48

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# RSS collection
# ---------------------------------------------------------------------------

def collect_from_rss(feeds: list[str], max_per_feed: int = 10) -> list[dict]:
    """Collect articles from RSS feeds."""
    articles = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                articles.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", "")[:500],
                    "published": entry.get("published", ""),
                    "source": feed.feed.get("title", url),
                })
        except Exception:
            logger.warning("Failed to parse RSS feed: %s", url, exc_info=True)
    logger.info("Collected %d articles from %d RSS feeds", len(articles), len(feeds))
    return articles


# ---------------------------------------------------------------------------
# Google Custom Search
# ---------------------------------------------------------------------------

def collect_from_google_cse(
    queries: list[str],
    api_key: str,
    engine_id: str,
) -> list[dict]:
    """Collect articles via Google Custom Search API."""
    articles = []
    with httpx.Client(timeout=15) as client:
        for query in queries:
            try:
                resp = client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={
                        "key": api_key,
                        "cx": engine_id,
                        "q": query,
                        "num": 5,
                        "dateRestrict": "d1",
                    },
                )
                resp.raise_for_status()
                for item in resp.json().get("items", []):
                    articles.append({
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "summary": item.get("snippet", ""),
                        "published": "",
                        "source": "Google CSE",
                    })
            except Exception:
                logger.warning("Google CSE query failed: %s", query, exc_info=True)
    logger.info("Collected %d articles from Google CSE", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicate articles by URL."""
    seen: set[str] = set()
    unique = []
    for a in articles:
        url = a["link"]
        if url and url not in seen:
            seen.add(url)
            unique.append(a)
    return unique


def deduplicate_against_past(
    articles: list[dict],
    state_dir: Path,
    current_date: str,
    lookback_days: int = 7,
) -> list[dict]:
    """Remove articles that were already selected in past days' news."""
    past_urls: set[str] = set()
    past_titles: set[str] = set()

    # Load past news JSONs (up to lookback_days)
    from datetime import timedelta
    try:
        current_dt = datetime.strptime(current_date, "%Y%m%d")
    except ValueError:
        return articles

    for i in range(1, lookback_days + 1):
        past_dt = current_dt - timedelta(days=i)
        past_path = state_dir / f"news_{past_dt.strftime('%Y%m%d')}.json"
        if not past_path.exists():
            continue
        try:
            with open(past_path, encoding="utf-8") as f:
                past_data = json.load(f)
            for a in past_data.get("articles", []):
                if a.get("link"):
                    past_urls.add(a["link"])
                # Also track Japanese titles for fuzzy match
                if a.get("title_ja"):
                    past_titles.add(a["title_ja"])
        except Exception:
            logger.warning("Failed to read past news: %s", past_path)

    if not past_urls and not past_titles:
        return articles

    filtered = []
    for a in articles:
        if a.get("link") in past_urls:
            logger.info("Dedup (past URL): %s", a.get("title", "")[:60])
            continue
        filtered.append(a)

    logger.info("Past dedup: %d → %d articles (checked %d days)", len(articles), len(filtered), lookback_days)
    return filtered


# ---------------------------------------------------------------------------
# OG image extraction
# ---------------------------------------------------------------------------

_OG_IMAGE_RE = re.compile(
    r'<meta\s+(?:[^>]*?)property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta\s+(?:[^>]*?)content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
    re.IGNORECASE,
)


def enrich_articles(
    articles: list[dict],
    output_dir: Path,
    fetch_full_text: bool = False,
    max_text_chars: int = 3000,
) -> None:
    """各記事のページを取得し、og:image・本文テキスト・出典ドメインを付与する.

    旧 fetch_og_images() の拡張版。同じHTTPフェッチに相乗りして本文も抽出するため
    追加リクエストはない。各記事に以下のキーを追加する:
      - og_image: 保存したOG画像のローカルパス（取得失敗時は ""）
      - source_domain: リンクのドメイン名（www. 除去済み、出典バッジ表示用）
      - full_text: 記事本文テキスト（fetch_full_text=True 時のみ、最大 max_text_chars 文字）
    """
    from urllib.parse import urlparse

    output_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for i, article in enumerate(articles):
            article["og_image"] = ""
            article.setdefault("full_text", "")
            link = article.get("link", "")
            if not link:
                article.setdefault("source_domain", "")
                continue

            try:
                domain = urlparse(link).netloc
                article["source_domain"] = domain[4:] if domain.startswith("www.") else domain
            except Exception:
                article["source_domain"] = ""

            try:
                # Fetch the HTML page (first 200KB: meta tags + main text)
                resp = client.get(link, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                html = resp.text[:200000]
            except Exception:
                logger.warning("Failed to fetch article page: %s", link[:80], exc_info=False)
                continue

            # 本文抽出（リッチ要約の材料。失敗しても続行＝スニペットにフォールバック）
            if fetch_full_text:
                try:
                    from article_extractor import extract_main_text
                    article["full_text"] = extract_main_text(html, max_chars=max_text_chars)
                    if article["full_text"]:
                        logger.info("Full text extracted (%d chars): %s",
                                    len(article["full_text"]), article.get("title", "")[:50])
                except Exception:
                    logger.warning("Full text extraction failed for: %s", link[:80], exc_info=False)

            try:
                # Extract og:image URL
                match = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_ALT.search(html)
                if not match:
                    logger.info("No og:image found for: %s", article.get("title", "")[:50])
                    continue

                img_url = match.group(1)

                # Download the image
                img_resp = client.get(img_url)
                img_resp.raise_for_status()

                # Determine extension from content-type
                ct = img_resp.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"

                img_path = output_dir / f"og_{i + 1}{ext}"
                img_path.write_bytes(img_resp.content)
                article["og_image"] = str(img_path)
                logger.info("OG image saved: %s", img_path.name)

            except Exception:
                logger.warning("Failed to fetch OG image for: %s", link[:80], exc_info=False)


def fetch_og_images(articles: list[dict], output_dir: Path) -> None:
    """Backward-compatible alias for enrich_articles (og:image only)."""
    enrich_articles(articles, output_dir, fetch_full_text=False)


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def _parse_published_date(date_str: str) -> datetime | None:
    """Try to parse various date formats from RSS published fields."""
    if not date_str:
        return None
    # Try RFC 2822 (e.g. "Mon, 12 Jan 2026 11:30:00 GMT")
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    # Try ISO 8601 (e.g. "2026-05-12T12:30:33-04:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def filter_by_freshness(
    articles: list[dict],
    max_age_hours: int = _MAX_ARTICLE_AGE_HOURS,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Remove articles older than max_age_hours.

    Articles with unparseable dates are kept (benefit of the doubt),
    but logged as warnings.
    """
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)

    fresh = []
    for article in articles:
        pub_date = _parse_published_date(article.get("published", ""))
        if pub_date is None:
            # Can't determine age — keep with warning
            logger.debug("No parseable date, keeping: %s", article.get("title", "")[:60])
            fresh.append(article)
            continue

        # Ensure timezone-aware comparison
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)

        age_hours = (reference_time - pub_date).total_seconds() / 3600
        if age_hours <= max_age_hours:
            fresh.append(article)
        else:
            logger.info(
                "Filtered out (%.0fh old): %s",
                age_hours, article.get("title", "")[:60],
            )

    logger.info("Freshness filter: %d → %d articles (max %dh)", len(articles), len(fresh), max_age_hours)
    return fresh


# ---------------------------------------------------------------------------
# Topic-relevance pre-filter (keyword-based, runs before paid LLM scoring)
# ---------------------------------------------------------------------------

# Keywords that indicate an article is genuinely about AI. Matched
# case-insensitively against title + summary. Used ONLY in default AI mode
# (topic empty) to cheaply drop obviously non-AI articles that general-purpose
# feeds (GIGAZINE, Publickey, Ars Technica) and broad CSE queries bring in.
_AI_RELEVANCE_KEYWORDS = [
    # English
    "ai", "a.i.", "artificial intelligence", "machine learning",
    "deep learning", "neural", "llm", "large language model", "gpt",
    "chatgpt", "openai", "anthropic", "claude", "gemini", "copilot",
    "generative", "genai", "diffusion", "transformer", "agent", "agentic",
    "robot", "robotics", "automation", "autonomous", "computer vision",
    "nlp", "midjourney", "dall-e", "stable diffusion", "hugging face",
    "deepmind", "perplexity", "grok", "mistral", "llama",
    # Japanese
    "人工知能", "機械学習", "深層学習", "ディープラーニング", "ニューラル",
    "生成ai", "大規模言語モデル", "言語モデル", "チャットgpt", "ロボット",
    "自動運転", "画像生成", "機械翻訳", "エージェント", "オープンエーアイ",
]


def _has_ai_keyword(article: dict) -> bool:
    """Return True if the article title or summary mentions an AI-related keyword."""
    text = (article.get("title", "") + " " + article.get("summary", "")).lower()
    # Word-boundary check for the very short token "ai" to avoid false positives
    # like "said", "again", "email", "campaign", "detail".
    for kw in _AI_RELEVANCE_KEYWORDS:
        if kw == "ai":
            if re.search(r"(?<![a-z])ai(?![a-z])", text):
                return True
        elif kw in text:
            return True
    return False


# NG-content exclusion: obscene/sexual topics are banned from the channel
# (user decision 2026-08-18). Matched case-insensitively against title + summary.
# Extend via config.json → news.exclude_keywords.
#
# Matching rules (see _ng_keyword_pattern):
# - ASCII keywords get a leading word boundary, so "sexual" does NOT match
#   "asexual" but still matches "sexualized". Stems like "obscen"/"masturbat"
#   are intentionally open-ended on the right.
# - Japanese keywords are substring-matched. Do NOT add short words that are
#   contained in ordinary compounds (e.g. "性的" alone matches 個性的/理性的);
#   use explicit compounds instead.
_EXCLUDED_CONTENT_KEYWORDS = [
    # Japanese
    "わいせつ", "ワイセツ", "猥褻", "ポルノ", "アダルト",
    "性的コンテンツ", "性的画像", "性的暴力", "性的被害", "性的搾取", "性的虐待",
    "性被害", "性犯罪", "性搾取", "児童ポルノ", "ヌード", "風俗", "セクハラ",
    # English (leading word boundary applied; stems match derived forms)
    "obscen", "porn", "sexual", "nude", "nudity", "nsfw", "csam",
    "erotic", "masturbat", "sextortion", "explicit image", "explicit content",
    "sex abuse", "sex crime",
]


def _ng_keyword_pattern(kw: str) -> re.Pattern:
    """Compile an NG keyword into a safe match pattern.

    ASCII keywords get a leading word boundary to avoid matching inside other
    words (asexual, bisexual, ...). Non-ASCII (Japanese) keywords are plain
    substrings — safety there comes from using explicit compound words.
    """
    if kw.isascii():
        return re.compile(r"(?<![a-z0-9])" + re.escape(kw))
    return re.compile(re.escape(kw))


def filter_excluded_content(articles: list[dict], extra_keywords: list[str] | None = None) -> list[dict]:
    """Drop articles about banned content (obscene/sexual topics).

    Runs in ALL modes (default AI and custom topic) before LLM scoring.
    The LLM scoring prompt has a matching NG rule as the second line of defense.
    """
    keywords = _EXCLUDED_CONTENT_KEYWORDS + [k.lower() for k in (extra_keywords or [])]
    patterns = [(kw, _ng_keyword_pattern(kw)) for kw in keywords]
    kept = []
    for a in articles:
        text = (a.get("title", "") + " " + a.get("summary", "")).lower()
        hit = next((kw for kw, pat in patterns if pat.search(text)), None)
        if hit:
            logger.info("Excluded (NG keyword '%s'): %s", hit, a.get("title", "")[:60])
        else:
            kept.append(a)
    if len(kept) < len(articles):
        logger.info("NG-content filter: %d → %d articles (%d excluded)",
                    len(articles), len(kept), len(articles) - len(kept))
    return kept


def filter_ai_relevant(articles: list[dict]) -> list[dict]:
    """Drop articles with no AI-related keyword (default AI mode only).

    This is a cheap pre-filter to remove non-AI noise from general-purpose
    feeds before spending API calls on scoring. The LLM relevance gate in
    score_articles_with_llm() is the authoritative second-stage filter.
    """
    kept = [a for a in articles if _has_ai_keyword(a)]
    dropped = len(articles) - len(kept)
    if dropped:
        logger.info("AI keyword pre-filter: %d → %d articles (%d non-AI dropped)",
                    len(articles), len(kept), dropped)
    return kept


# ---------------------------------------------------------------------------
# LLM-based scoring
# ---------------------------------------------------------------------------

SCORING_SYSTEM_PROMPT_TEMPLATE = """\
あなたは{topic}ニュースの評価者です。AI初心者の大人向けYouTubeチャンネル「ポンテのAI教室」のために、
ニュース記事を評価してください。

【最重要】まず、この記事が本当に「{topic}」に関係する内容かを判定してください。
- {topic}が主題、または{topic}技術が中心的に登場する記事 → relevant: true
- {topic}とほぼ無関係な記事（一般的なガジェット紹介、ゲーム、政治、経済、スポーツ、
  芸能、{topic}が話の本筋でない記事など） → relevant: false
無関係な記事を絶対に true にしないでください。判断に迷う場合は false にしてください。

【NGコンテンツ】以下の内容を含む記事は、{topic}に関係していても必ず relevant: false にしてください:
- わいせつ・性的・ポルノ・アダルト関連の話題（性被害、性犯罪、性的画像の生成・加工を含む）
このチャンネルは健全な一般向けチャンネルのため、これらの話題は一切扱いません。

次に、relevant: true の記事のみ、以下の基準で1-10のスコアで評価してください
（relevant: false の場合は score は 0 にしてください）:
- 話題性（一般の人にも関係あるか）: 1-10
- わかりやすさ（専門知識のない大人にかんたんに説明できるか）: 1-10
- 面白さ（視聴者が興味を持つか）: 1-10
3つの平均を最終スコアとしてください。

日本語のタイトルと50文字以内の要約も生成してください。

JSON形式のみで回答（説明文不要）:
{{"relevant": true または false, "score": 数値, "title_ja": "日本語タイトル", "summary_ja": "50文字以内の要約"}}
"""


def _is_japanese(text: str) -> bool:
    """Check if text contains Japanese characters (hiragana, katakana, or kanji)."""
    for ch in text:
        cp = ord(ch)
        # Hiragana, Katakana, CJK Unified Ideographs
        if (0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
                0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF):
            return True
    return False


def score_articles_with_llm(
    articles: list[dict],
    llm_config: dict,
    topic: str = "AI",
    max_retries: int = 2,
) -> list[dict]:
    """Score and summarize articles using LLM API.

    Each article is scored individually with retry logic. If a single article
    fails after retries, it is marked with score=0 but does NOT cause the
    entire batch to fall back to keyword scoring.
    """
    from api_utils import LLMClient
    import time

    client = LLMClient(llm_config)
    scored = []
    max_tokens = llm_config.get(llm_config.get("provider", "gemini"), {}).get("max_tokens_scoring", 200)
    scoring_prompt = SCORING_SYSTEM_PROMPT_TEMPLATE.format(topic=topic)

    for idx, article in enumerate(articles):
        if idx > 0:
            time.sleep(0.5)  # Gentle rate limiting

        user_prompt = (
            f"タイトル: {article['title']}\n"
            f"ソース: {article['source']}\n"
            f"概要: {article['summary']}"
        )

        success = False
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    wait = 2 ** attempt
                    logger.info("  Retry %d/%d after %ds...", attempt, max_retries, wait)
                    time.sleep(wait)

                text = client.generate(scoring_prompt, user_prompt, max_tokens)
                text = text.strip()

                if "{" in text:
                    json_str = text[text.index("{"):text.rindex("}") + 1]
                    result = json.loads(json_str)
                    # Topic relevance gate: non-relevant articles get score 0
                    relevant = result.get("relevant", True)
                    article["relevant"] = bool(relevant)
                    article["score"] = result.get("score", 0) if relevant else 0
                    article["title_ja"] = result.get("title_ja", article["title"])
                    article["summary_ja"] = result.get("summary_ja", "")

                    # Validate that title_ja is actually Japanese
                    if not _is_japanese(article["title_ja"]):
                        logger.warning("  title_ja not Japanese, retrying: %s", article["title_ja"][:50])
                        continue

                    success = True
                    break
                else:
                    logger.warning("  No JSON in response (attempt %d): %s", attempt + 1, text[:100])

            except Exception:
                logger.warning("  Scoring attempt %d failed for: %s", attempt + 1, article["title"][:50], exc_info=True)

        if not success:
            logger.error("  All retries exhausted for: %s", article["title"][:50])
            article["score"] = 0
            article["relevant"] = False
            article["title_ja"] = article["title"]
            article["summary_ja"] = ""

        scored.append(article)
        logger.info("  [%d/%d] score=%s: %s", idx + 1, len(articles), article["score"], article["title"][:50])

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Cross-outlet topic deduplication + source diversity
# ---------------------------------------------------------------------------

CLUSTER_SYSTEM_PROMPT = """\
あなたはニュース編成の専門家です。同じ出来事・同じ発表を報じている記事を
グループにまとめてください（媒体が違っても同じ出来事なら同じグループ）。

ルール:
- 1記事だけのグループも必ず含めること
- 全記事番号が、ちょうど1回ずつ、いずれかのグループに含まれていること
- 迷ったら別グループにすること（過剰にまとめない）

JSON形式のみで回答（説明文不要）:
{"clusters": [[1, 4], [2], [3], [5]]}
"""


def cluster_topics_with_llm(
    scored: list[dict],
    llm_config: dict,
    pool_size: int = 15,
    max_per_source: int = 2,
) -> list[dict]:
    """媒体跨ぎの同一トピック記事を1本に集約し、ソース多様性を確保して並べ替える.

    スコア降順の上位 pool_size 件を1回のLLM呼び出しでストーリー単位にクラスタリングし、
    各クラスタからスコア最高の1件を代表として採用する。さらに同一ソースは
    max_per_source 件までに制限する（超過分は後回し）。

    Returns:
        並べ替え済みの記事リスト（先頭から top_n を取れば多様な選定になる）。
        クラスタリング失敗時は入力をそのまま返す（abortしない）。
    """
    pool = scored[:pool_size]
    rest = scored[pool_size:]
    if len(pool) <= 1:
        return scored

    from api_utils import LLMClient

    client = LLMClient(llm_config)
    provider_cfg = llm_config.get(llm_config.get("provider", "gemini"), {})
    max_tokens = max(provider_cfg.get("max_tokens_scoring", 200), 400)

    lines = []
    for i, a in enumerate(pool, 1):
        title = a.get("title_ja") or a.get("title", "")
        lines.append(f"{i}. {title} ({a.get('source', '')})")
    user_prompt = "ニュースタイトル一覧:\n" + "\n".join(lines)

    try:
        text = client.generate(CLUSTER_SYSTEM_PROMPT, user_prompt, max_tokens)
        text = (text or "").strip()
        if "{" not in text:
            raise ValueError(f"No JSON in clustering response: {text[:100]}")
        result = json.loads(text[text.index("{"):text.rindex("}") + 1])
        raw_clusters = result.get("clusters", [])
        if not isinstance(raw_clusters, list) or not raw_clusters:
            raise ValueError("clusters missing or empty")
    except Exception:
        logger.warning("Topic clustering failed — keeping score order", exc_info=True)
        return scored

    # クラスタ正規化: 範囲外を除去、重複は初出のみ、欠落は単独クラスタとして補完
    seen: set[int] = set()
    clusters: list[list[int]] = []
    for cluster in raw_clusters:
        if not isinstance(cluster, list):
            continue
        valid = []
        for n in cluster:
            if isinstance(n, int) and 1 <= n <= len(pool) and n not in seen:
                valid.append(n)
                seen.add(n)
        if valid:
            clusters.append(valid)
    for n in range(1, len(pool) + 1):
        if n not in seen:
            clusters.append([n])

    # 各クラスタの代表 = スコア最高（poolはスコア降順なので最小番号）
    representatives = []
    duplicates_dropped = 0
    for cluster in clusters:
        rep_idx = min(cluster)
        representatives.append(pool[rep_idx - 1])
        duplicates_dropped += len(cluster) - 1
    representatives.sort(key=lambda x: x.get("score", 0), reverse=True)

    # ソース多様性: 同一ソースは max_per_source 件まで（超過分は後回し）
    source_counts: dict[str, int] = {}
    diverse: list[dict] = []
    overflow: list[dict] = []
    for a in representatives:
        key = a.get("source_domain") or a.get("source", "")
        if source_counts.get(key, 0) >= max_per_source:
            overflow.append(a)
            continue
        source_counts[key] = source_counts.get(key, 0) + 1
        diverse.append(a)

    if duplicates_dropped or overflow:
        logger.info(
            "Topic dedup: %d clusters from %d articles (%d cross-outlet duplicates demoted, "
            "%d source-overflow demoted)",
            len(clusters), len(pool), duplicates_dropped, len(overflow),
        )

    # 重複記事（代表以外）もリスト末尾に残す（選定には実質使われない）
    rep_ids = {id(a) for a in diverse + overflow}
    demoted = [a for a in pool if id(a) not in rep_ids]
    return diverse + overflow + demoted + rest


# ---------------------------------------------------------------------------
# Per-article rich summarization (script material)
# ---------------------------------------------------------------------------

ENRICH_SYSTEM_PROMPT_TEMPLATE = """\
あなたはAI初心者の大人向けニュース番組の構成作家です。{topic}ニュース記事の内容を、
初心者向け台本の材料になるよう日本語で整理してください。

ルール:
- summary_ja_long: 記事の内容を正確に伝える200〜300文字の日本語要約。
  記事に書かれていないことを推測で書かない。
- facts: 台本のたとえ話や具体例に使える、記事中の具体的な事実・数字・できごとを2〜3個。
- kid_angle: このニュースが視聴者（専門知識のない大人）の生活・仕事・お金に
  どう関係するかの一言（50文字以内）。※フィールド名は互換性のためkid_angleのまま。

JSON形式のみで回答（説明文不要）:
{{"summary_ja_long": "...", "facts": ["...", "..."], "kid_angle": "..."}}
"""


def summarize_articles_with_llm(
    articles: list[dict],
    llm_config: dict,
    topic: str = "AI",
) -> None:
    """選定済み記事に summary_ja_long / facts / kid_angle を付与する（記事ごと1呼び出し）.

    本文（full_text）があればそれを、なければRSSスニペットを材料にする。
    失敗した記事は既存の summary_ja のみで続行する（abortしない）。
    """
    from api_utils import LLMClient
    import time

    client = LLMClient(llm_config)
    provider_cfg = llm_config.get(llm_config.get("provider", "gemini"), {})
    max_tokens = provider_cfg.get("max_tokens_enrich", 1000)
    system_prompt = ENRICH_SYSTEM_PROMPT_TEMPLATE.format(topic=topic)

    for idx, article in enumerate(articles):
        body = article.get("full_text") or article.get("summary", "")
        if not body:
            continue
        if idx > 0:
            time.sleep(0.5)  # Gentle rate limiting

        user_prompt = (
            f"タイトル: {article.get('title_ja', article.get('title', ''))}\n"
            f"本文:\n{body[:3000]}"
        )

        try:
            text = client.generate(system_prompt, user_prompt, max_tokens)
            text = (text or "").strip()
            if "{" not in text:
                raise ValueError(f"No JSON in enrich response: {text[:100]}")
            result = json.loads(text[text.index("{"):text.rindex("}") + 1])

            summary_long = result.get("summary_ja_long", "")
            if summary_long and _is_japanese(summary_long):
                article["summary_ja_long"] = summary_long[:400]
            facts = result.get("facts")
            if isinstance(facts, list):
                article["facts"] = [str(f)[:120] for f in facts[:3] if f]
            if result.get("kid_angle"):
                article["kid_angle"] = str(result["kid_angle"])[:80]
            logger.info("  Enriched [%d/%d]: %s", idx + 1, len(articles),
                        article.get("title_ja", "")[:50])
        except Exception:
            logger.warning("Article enrich failed (using short summary): %s",
                           article.get("title_ja", "")[:50], exc_info=False)


# ---------------------------------------------------------------------------
# Keyword-based fallback scoring (no API required)
# ---------------------------------------------------------------------------

_HIGH_INTEREST_KEYWORDS = [
    "chatgpt", "openai", "google", "apple", "microsoft", "meta",
    "robot", "autonomous", "breakthrough", "launch", "release",
    "gpt", "gemini", "claude", "llm", "generate", "image",
    "child", "school", "education", "game", "app",
]


def score_articles_fallback(articles: list[dict]) -> list[dict]:
    """Score articles using keyword matching (fallback when API is unavailable).

    NOTE: This fallback does NOT translate titles to Japanese. Articles processed
    here will have title_ja in English. The downstream validate_japanese_titles()
    check will flag these so the pipeline can warn or abort.
    """
    for article in articles:
        title_lower = (article.get("title", "") + " " + article.get("summary", "")).lower()
        score = sum(2 for kw in _HIGH_INTEREST_KEYWORDS if kw in title_lower)
        score = min(score, 10)
        article["score"] = score
        # Mark as untranslated — do NOT pretend it's Japanese
        article["title_ja"] = article["title"]
        article["summary_ja"] = article.get("summary", "")[:50]
        article["_untranslated"] = True
    articles.sort(key=lambda x: x["score"], reverse=True)
    logger.warning(
        "Fallback scoring used for %d articles — titles remain in English!", len(articles)
    )
    return articles


def validate_japanese_titles(articles: list[dict]) -> tuple[bool, int]:
    """Validate that selected articles have Japanese titles.

    Returns (all_ok, num_english) tuple.
    """
    english_count = 0
    for article in articles:
        if not _is_japanese(article.get("title_ja", "")):
            english_count += 1
            logger.warning("English title_ja detected: %s", article.get("title_ja", "")[:60])
    return english_count == 0, english_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect_news(
    config_path: Path = Path("config.json"),
    date_str: str | None = None,
    topic: str = "",
) -> Path:
    """Run the full news collection pipeline. Returns path to output JSON.

    Args:
        topic: Custom topic keyword (e.g. "車", "宇宙"). If empty, uses default AI news.
    """
    config = load_config(config_path)
    news_cfg = config["news"]
    llm_cfg = config.get("llm", {})
    state_dir = Path(config["paths"]["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)

    if date_str is None:
        date_str = datetime.now(_JST).strftime("%Y%m%d")

    articles = []

    if topic:
        # Custom topic mode: skip RSS, use Google CSE with topic queries only
        logger.info("Custom topic mode: '%s'", topic)
        cse_cfg = news_cfg.get("google_cse", {})
        cse_api_key = os.environ.get(cse_cfg.get("api_key_env", ""), "")
        cse_engine_id = os.environ.get(cse_cfg.get("engine_id_env", ""), "")
        if cse_api_key and cse_engine_id:
            topic_queries = [
                f"{topic} ニュース 今日",
                f"{topic} 最新ニュース",
                f"{topic} news today",
            ]
            articles = collect_from_google_cse(topic_queries, cse_api_key, cse_engine_id)
        else:
            logger.warning("Google CSE not configured. Cannot collect custom topic news.")
    else:
        # Default AI news mode: RSS + Google CSE
        articles = collect_from_rss(news_cfg["rss_feeds"])

        cse_cfg = news_cfg.get("google_cse", {})
        cse_api_key = os.environ.get(cse_cfg.get("api_key_env", ""), "")
        cse_engine_id = os.environ.get(cse_cfg.get("engine_id_env", ""), "")
        if cse_api_key and cse_engine_id:
            cse_articles = collect_from_google_cse(
                cse_cfg.get("queries", []),
                cse_api_key,
                cse_engine_id,
            )
            articles.extend(cse_articles)
        else:
            logger.info("Google CSE not configured, skipping")

    # 3. Deduplicate (within today)
    articles = deduplicate(articles)
    logger.info("Total unique articles: %d", len(articles))

    # 3.5 Remove articles already used in past days
    articles = deduplicate_against_past(articles, state_dir, date_str)

    # 4. Filter by freshness (reject articles older than 48h)
    max_age = news_cfg.get("max_article_age_hours", _MAX_ARTICLE_AGE_HOURS)
    articles = filter_by_freshness(articles, max_age_hours=max_age)

    # 4.3 NG-content exclusion (all modes): obscene/sexual topics are banned
    # from the channel (user decision 2026-08-18).
    articles = filter_excluded_content(articles, news_cfg.get("exclude_keywords"))

    # 4.5 AI-relevance keyword pre-filter (default AI mode only).
    # General-purpose feeds bring in non-AI noise; drop it cheaply before scoring.
    if not topic:
        articles = filter_ai_relevant(articles)
        if not articles:
            logger.warning(
                "AI keyword pre-filter removed ALL articles — feeds may be "
                "entirely non-AI today. Downstream will produce empty output."
            )

    # Limit before scoring
    max_articles = news_cfg.get("max_articles", 30)
    articles = articles[:max_articles]

    # 5. Score articles with LLM (fallback to keywords on failure)
    topic_label = topic if topic else "AI"
    try:
        scored = score_articles_with_llm(articles, llm_cfg, topic=topic_label)
    except Exception as e:
        logger.warning("LLM scoring failed, using keyword fallback: %s", e)
        scored = score_articles_fallback(articles)

    total_scored = len(scored)  # count before relevance gate (for reporting)

    # 5.5 LLM relevance gate: drop articles the LLM judged off-topic.
    # Keep this only if it leaves enough articles; otherwise fall back to all
    # scored (better to publish than to abort on an over-strict judgement).
    relevant = [a for a in scored if a.get("relevant", True) and a.get("score", 0) > 0]
    if relevant:
        if len(relevant) < len(scored):
            logger.info("LLM relevance gate: %d → %d articles (%d off-topic dropped)",
                        len(scored), len(relevant), len(scored) - len(relevant))
        scored = relevant
    else:
        logger.warning("LLM relevance gate would drop ALL articles — keeping scored set as-is")

    # 5.7 Cross-outlet topic dedup + source diversity (1 LLM call).
    # 同じ出来事を報じる媒体違いの記事を1本に集約し、同一ソース偏重を防ぐ。
    if news_cfg.get("topic_dedup", False) and len(scored) > 1:
        scored = cluster_topics_with_llm(
            scored,
            llm_cfg,
            pool_size=news_cfg.get("cluster_pool_size", 15),
        )

    # 6. Select top N
    top_n = news_cfg.get("top_n", 5)
    selected = scored[:top_n]

    # 7. Validate Japanese titles — abort if all are English (prevents silent English output)
    all_ok, eng_count = validate_japanese_titles(selected)
    if not all_ok:
        logger.error(
            "VALIDATION FAILED: %d/%d selected articles have English-only title_ja. "
            "LLM scoring likely failed. Check API key and connectivity.",
            eng_count, len(selected),
        )
        if eng_count == len(selected):
            raise RuntimeError(
                f"All {eng_count} selected articles have English-only titles. "
                "LLM scoring completely failed. Aborting to prevent English video output."
            )

    # 8. Enrich selected articles: og:image + 本文テキスト + 出典ドメイン
    og_dir = state_dir / "og_images"
    fetch_full = news_cfg.get("fetch_full_text", False)
    enrich_articles(selected, og_dir, fetch_full_text=fetch_full)

    # 8.5 記事ごとのリッチ要約（台本の材料: summary_ja_long / facts / kid_angle）
    if fetch_full:
        try:
            summarize_articles_with_llm(selected, llm_cfg, topic=topic_label)
        except Exception:
            logger.warning("Article summarization failed — proceeding with short summaries",
                           exc_info=True)

    # 9. Save
    output = {
        "date": date_str,
        "topic": topic if topic else "AI",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "total_collected": total_scored,
        "selected_count": len(selected),
        "articles": selected,
    }
    output_path = state_dir / f"news_{date_str}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("Saved %d articles to %s", len(selected), output_path)
    return output_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="AI News Collector")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--date", default=None, help="Date string YYYYMMDD")
    args = parser.parse_args()
    path = collect_news(Path(args.config), args.date)
    print(f"Output: {path}")


if __name__ == "__main__":
    main()
