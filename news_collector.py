"""Phase 1: AIニュース収集モジュール.

RSSフィード（カスタムトピックは Google News RSS）からニュースを収集し、
LLM API（Gemini/Claude）でスコアリング・要約してTOP N記事を選定する。
"""

from __future__ import annotations

import argparse
import html as _html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

# 日付の既定値は日本時間（JST）基準（UTCだと深夜〜朝9時の実行で前日になる）
_JST = timezone(timedelta(hours=9))
from pathlib import Path

from email.utils import parsedate_to_datetime

import re

import feedparser
import httpx

from api_utils import LLMAuthError
from seen_topics import clean_link, url_key

logger = logging.getLogger(__name__)

# Maximum age (in hours) for an article to be considered "fresh"
_MAX_ARTICLE_AGE_HOURS = 48
# 未来の日付の許容幅。タイムゾーンの書き間違い（JST を GMT と表記など、最大 +14h）は
# 「今」として扱い、それより先の日付は信用できないので外す
_FUTURE_TOLERANCE_HOURS = 14
# フィードごとの上限（新しさで絞ってから数える。GIGAZINE は1日30件近く出る）
_MAX_PER_FEED = 30

# 一部のサイト（openai.com など）は "Mozilla/5.0" だけの UA を 403 で拒否するため、
# ブラウザと同じ形の UA を送る
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_FEED_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}
_PAGE_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def topic_slug(topic: str) -> str:
    """カスタムトピックをファイル名に使える形にする（例: "宇宙 開発" → "宇宙_開発"）."""
    slug = re.sub(r"[^\w]+", "_", (topic or "").strip()).strip("_")[:40]
    return slug or "topic"


def news_json_path(state_dir: Path, date_str: str, topic: str = "") -> Path:
    """news JSON の保存先。カスタムトピックは AI ニュースを上書きしないよう別ファイルにする."""
    if topic:
        return Path(state_dir) / f"news_{date_str}_{topic_slug(topic)}.json"
    return Path(state_dir) / f"news_{date_str}.json"


# ---------------------------------------------------------------------------
# RSS collection
# ---------------------------------------------------------------------------

# タグだけを消す（"<" の直後が英字・/・! のもの）。本文の "a < b > c" は消さない
_TAG_RE = re.compile(r"</?[A-Za-z!][^>]*>")
_WS_RE = re.compile(r"\s+")


def _clean_text(text: str) -> str:
    """RSS の題名・概要からタグと文字参照を除く（"&amp;#8217;" のような二重エスケープにも対応）."""
    text = text or ""
    for _ in range(3):
        unescaped = _html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", _html.unescape(text)).strip()


def _entry_published_at(entry) -> str:
    """feedparser が UTC に直した日時（published_parsed / updated_parsed）を ISO 形式で返す."""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                continue
    return ""


def _entry_to_article(entry, source: str) -> dict:
    return {
        "title": _clean_text(entry.get("title", "")),
        "link": clean_link(entry.get("link", "")),
        "summary": _clean_text(entry.get("summary", ""))[:500],
        "published": entry.get("published", "") or entry.get("updated", ""),
        "published_at": _entry_published_at(entry),
        "source": source,
    }


def _fetch_feed(client: httpx.Client, url: str, stat: dict):
    """フィードを httpx で取得して feedparser に渡す。失敗・異常は stat に残し WARN を出す.

    feedparser.parse(url) は DNS 失敗や 429 でも例外を出さず0件を返すため、
    取得は httpx（タイムアウト・UA付き）で行う。
    """
    try:
        resp = client.get(url)
    except httpx.HTTPError as e:
        stat["error"] = f"{type(e).__name__}: {e}"[:200]
        logger.warning("Feed fetch failed: %s (%s)", url, stat["error"])
        return None
    stat["status"] = resp.status_code
    if resp.history:
        stat["final_url"] = str(resp.url)
        logger.warning("Feed redirected (update config.json): %s -> %s", url, resp.url)
    if resp.status_code != 200:
        logger.warning("Feed returned HTTP %d: %s", resp.status_code, url)
        return None
    feed = feedparser.parse(resp.content)
    if feed.bozo:
        stat["bozo"] = repr(feed.get("bozo_exception"))[:200]
        logger.warning("Feed is malformed (bozo): %s — %s", url, stat["bozo"])
    return feed


def _sort_newest_first(articles: list[dict]) -> list[dict]:
    """新しい順に並べる（日付のない記事は末尾。並びは安定）."""
    return sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)


def collect_from_rss(
    feeds: list[str],
    max_per_feed: int = _MAX_PER_FEED,
    max_age_hours: int | None = None,
    stats: list[dict] | None = None,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Collect articles from RSS feeds.

    フィードごとに新しい順に並べ、max_age_hours 以内に絞ってから max_per_feed 件に制限する
    （先に件数で切ると、1日に何十件も出るフィードの新しいAI記事を取りこぼす）。
    stats を渡すと、フィードごとの件数・HTTP状態・異常をそこに追記する。
    """
    articles = []
    with httpx.Client(timeout=20, follow_redirects=True, headers=_FEED_HEADERS) as client:
        for url in feeds:
            stat: dict = {"url": url, "status": None, "entries": 0, "fresh": 0, "kept": 0}
            if stats is not None:
                stats.append(stat)
            feed = _fetch_feed(client, url, stat)
            if feed is None:
                continue
            source = _clean_text(feed.feed.get("title", "")) or url
            items = [_entry_to_article(e, source) for e in feed.entries]
            stat["entries"] = len(items)
            if not items:
                logger.warning("Feed has 0 entries: %s", url)
                continue
            items = _sort_newest_first(items)
            if max_age_hours is not None:
                items = [a for a in items
                         if _freshness(a, max_age_hours, reference_time) in ("fresh", "unknown")]
            stat["fresh"] = len(items)
            items = items[:max_per_feed]
            stat["kept"] = len(items)
            articles.extend(items)
    logger.info("Collected %d articles from %d RSS feeds", len(articles), len(feeds))
    return articles


def interleave_by_source(articles: list[dict]) -> list[dict]:
    """フィードごとに1件ずつ順番に並べる（件数上限で後ろのフィードが丸ごと落ちないように）."""
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(a.get("source", ""), []).append(a)
    out: list[dict] = []
    queues = list(groups.values())
    while queues:
        out.extend(q.pop(0) for q in queues)
        queues = [q for q in queues if q]
    return out


# ---------------------------------------------------------------------------
# Google News RSS（カスタムトピック。Google CSE は新規受付終了・2027年に既存も移行が必要）
# ---------------------------------------------------------------------------

_GOOGLE_NEWS_EDITIONS = (
    {"hl": "ja", "gl": "JP", "ceid": "JP:ja"},
    {"hl": "en-US", "gl": "US", "ceid": "US:en"},
)


def collect_from_google_news(
    topic: str,
    max_per_feed: int = _MAX_PER_FEED,
    max_age_hours: int | None = None,
    stats: list[dict] | None = None,
) -> list[dict]:
    """Google News RSS（日本語版と英語版）からトピックのニュースを集める.

    リンクは news.google.com の転送用URL。元記事のURLは本文取得のときに
    resolve_google_news_url() で解決する（候補全件ではなく選定時だけ）。
    """
    from urllib.parse import quote

    articles = []
    with httpx.Client(timeout=20, follow_redirects=True, headers=_FEED_HEADERS) as client:
        for ed in _GOOGLE_NEWS_EDITIONS:
            url = (f"https://news.google.com/rss/search?q={quote(topic + ' when:2d')}"
                   f"&hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}")
            stat: dict = {"url": url, "status": None, "entries": 0, "fresh": 0, "kept": 0}
            if stats is not None:
                stats.append(stat)
            feed = _fetch_feed(client, url, stat)
            if feed is None:
                continue
            items = []
            for e in feed.entries:
                a = _entry_to_article(e, "Google News")
                src = e.get("source") or {}
                publisher = _clean_text(src.get("title", "")) if hasattr(src, "get") else ""
                if publisher:
                    a["source"] = publisher
                    # 題名の末尾「 - 媒体名」を外す
                    suffix = f" - {publisher}"
                    if a["title"].endswith(suffix):
                        a["title"] = a["title"][: -len(suffix)].strip()
                if hasattr(src, "get") and src.get("href"):
                    a["source_url"] = src.get("href")
                # 概要は「題名＋媒体名」のリンクだけなので材料にならない
                a["summary"] = ""
                items.append(a)
            stat["entries"] = len(items)
            if not items:
                logger.warning("Google News has 0 entries: %s", url)
                continue
            items = _sort_newest_first(items)
            if max_age_hours is not None:
                items = [a for a in items if _freshness(a, max_age_hours) in ("fresh", "unknown")]
            stat["fresh"] = len(items)
            items = items[:max_per_feed]
            stat["kept"] = len(items)
            articles.extend(items)
    logger.info("Collected %d articles from Google News for '%s'", len(articles), topic)
    return articles


_GNEWS_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GNEWS_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_GNEWS_ID_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([^?/#]+)")


def is_google_news_url(url: str) -> bool:
    return bool(_GNEWS_ID_RE.search(url or ""))


def resolve_google_news_url(client: httpx.Client, url: str) -> str:
    """Google News の転送用URLを元記事のURLに解決する。失敗したら "" を返す.

    2024年以降の転送URLは base64 では解けないため、記事ページに埋め込まれた
    署名（data-n-a-sg）と時刻（data-n-a-ts）を使って batchexecute に問い合わせる。
    """
    m = _GNEWS_ID_RE.search(url or "")
    if not m:
        return ""
    gid = m.group(1)
    try:
        page = client.get(f"https://news.google.com/rss/articles/{gid}", headers=_PAGE_HEADERS)
        sg, ts = _GNEWS_SG_RE.search(page.text), _GNEWS_TS_RE.search(page.text)
        if not (sg and ts):
            return ""
        req = ["Fbv4je", (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,'
            'null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{gid}",{ts.group(1)},"{sg.group(1)}"]'
        )]
        resp = client.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data={"f.req": json.dumps([[req]])},
            headers={**_PAGE_HEADERS,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        return _parse_batchexecute_url(resp.text)
    except Exception:  # httpx.HTTPError 以外（InvalidURL など）でも選定ごと落とさない
        logger.warning("Google News URL resolve failed: %s", url[:80], exc_info=False)
        return ""


def _parse_batchexecute_url(text: str) -> str:
    """batchexecute の応答（先頭に )]}' が付いた JSON の入れ子）から元記事のURLを取り出す."""
    body = text[text.find("["):] if "[" in text else ""
    try:
        outer, _ = json.JSONDecoder().raw_decode(body)  # 後ろに続く塊は読まない
    except ValueError:
        return ""
    for row in outer if isinstance(outer, list) else []:
        if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and isinstance(row[2], str):
            try:
                inner = json.loads(row[2])
            except ValueError:
                continue
            if (isinstance(inner, list) and len(inner) > 1 and inner[0] == "garturlres"
                    and isinstance(inner[1], str) and inner[1].startswith(("http://", "https://"))):
                return inner[1]
    return ""


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
    """Remove duplicate articles by URL (utm・末尾の / ・フラグメントの違いは同じURLとみなす)."""
    seen: set[str] = set()
    unique = []
    for a in articles:
        key = url_key(a.get("link", ""))
        if key and key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def past_url_keys(
    state_dir: Path,
    current_date: str,
    lookback_days: int = 7,
    warnings: list[dict] | None = None,
) -> set[str]:
    """過去 lookback_days に使った記事の URL キー（url_key）の集合. 台帳が読めないときは空集合."""
    from seen_topics import recent_entries, sync_ledger

    try:
        past = recent_entries(sync_ledger(state_dir), current_date, lookback_days)
    except Exception as e:
        logger.warning("Ledger unavailable — skipping past URL dedup", exc_info=True)
        if warnings is not None:
            warnings.append({"check": "過去の重複判定ができなかった（台帳 seen_topics.json）",
                             "detail": f"過去に投稿した記事が混ざっていないか確認する（{e}）"[:300]})
        return set()
    return {e["key"] for e in past if e.get("key")}


def deduplicate_against_past(
    articles: list[dict],
    state_dir: Path,
    current_date: str,
    lookback_days: int = 7,
    warnings: list[dict] | None = None,
) -> list[dict]:
    """Remove articles whose URL was already selected in the past lookback_days.

    過去分は台帳（state/seen_topics.json）で見る。台帳は news_*.json（state と state/old）
    と upload_log.json から毎回取り込み直すため、news JSON を移動・削除しても抜けない。
    媒体違いの同じ話は deduplicate_stories_against_past()（LLM 判定）で落とす。
    台帳が読めないときは重複判定をせずに続ける（収集は止めない。warnings に残す）。
    """
    past_keys = past_url_keys(state_dir, current_date, lookback_days, warnings=warnings)
    if not past_keys:
        return articles

    filtered = []
    for a in articles:
        if url_key(a.get("link", "")) in past_keys:
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


# OG 画像の上限（これより大きいものはスライドに埋め込まない）と、受け付ける形式→拡張子
_MAX_OG_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_PAGE_BYTES = 5 * 1024 * 1024  # 記事ページの取得上限（Wired の本文は約47万字目＝約0.5MB なので十分）
_OG_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                   "image/webp": ".webp", "image/gif": ".gif"}
# 本文が文字化けしているとみなす置換文字（U+FFFD）の割合
_MAX_REPLACEMENT_RATIO = 0.01
# 要約・facts を作るのに必要な材料（本文かRSS概要）の最低文字数
_MIN_MATERIAL_CHARS = 200

_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)
# Shift_JIS 宣言のページは機種依存文字（①・㈱など）を含むことが多いので上位互換の cp932 で読む
_CHARSET_ALIASES = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932",
                    "x-sjis": "cp932", "windows-31j": "cp932", "ms932": "cp932"}


def decode_html(content: bytes, content_type: str = "") -> str:
    """HTML のバイト列を正しい文字コードで文字列にする.

    resp.text は charset ヘッダーがないと UTF-8 とみなすため、meta で shift_jis を
    宣言する ITmedia のページが文字化けしていた（置換文字が数千個）。
    優先順位: BOM → ヘッダーの charset → meta charset → charset_normalizer の推定 → UTF-8。
    宣言どおりに読めない（不正なバイトがある）ときは次の候補を試す。
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace")
    declared: list[str] = []
    m = _HEADER_CHARSET_RE.search(content_type or "")
    if m:
        declared.append(m.group(1))
    m = _META_CHARSET_RE.search(content[:8192])
    if m:
        declared.append(m.group(1).decode("ascii", "ignore"))
    valid: list[str] = []
    for enc in declared:
        enc = _CHARSET_ALIASES.get(enc.lower(), enc)
        try:
            return content.decode(enc)
        except LookupError:
            continue
        except UnicodeDecodeError:
            valid.append(enc)
    # 宣言どおりの文字コードで、不正なバイトがわずか（0.1%以下）なら推定より宣言を信じる
    # （推定が外れると、置換文字を含まない文字化け＝検知できない化け方になるため）
    for enc in valid:
        text = content.decode(enc, errors="replace")
        if replacement_ratio(text) <= 0.001:
            return text
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(content[:200000]).best()
        if best is not None and best.encoding:
            enc = _CHARSET_ALIASES.get(best.encoding.lower(), best.encoding)
            return content.decode(enc, errors="replace")
    except ImportError:
        pass
    return content.decode(valid[0] if valid else "utf-8", errors="replace")


def replacement_ratio(text: str) -> float:
    """置換文字（U+FFFD）の割合。文字化けの目安."""
    return text.count("�") / len(text) if text else 0.0


def material_chars(article: dict) -> int:
    """要約の材料になる文字数（本文と RSS 概要の長いほう）."""
    return max(len(article.get("full_text") or ""), len(article.get("summary") or ""))


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def _og_image_url(html: str, page_url: str) -> str:
    """og:image の URL を取り出す（&amp; を戻し、相対 URL はページの URL から解決する）.

    属性の順序（property が先か content が先か）や引用符なしの値
    （Forbes Japan: <meta content=https://... property=og:image>）にも対応する。
    """
    from urllib.parse import urljoin

    raw = ""
    match = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_ALT.search(html)
    if match:
        raw = match.group(1)
    else:
        for tag in _META_TAG_RE.findall(html):
            attrs = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
                     for m in _ATTR_RE.finditer(tag)}
            if (attrs.get("property") or attrs.get("name") or "").lower() == "og:image" and attrs.get("content"):
                raw = attrs["content"]
                break
    if not raw:
        return ""
    url = urljoin(page_url, _html.unescape(raw.strip()))
    return url if url.startswith(("http://", "https://")) else ""


def _fetch_article_page(
    client: httpx.Client,
    article: dict,
    fetch_full_text: bool,
    max_text_chars: int,
) -> tuple[str, str]:
    """記事ページを取得して source_domain・full_text を付ける。(html, 最終URL) を返す（失敗時は ""）."""
    from urllib.parse import urlparse

    article.setdefault("full_text", "")
    link = article.get("link", "")

    # Google News の転送URLは元記事に解決してから取得する（解決できなければ本文なし）
    if is_google_news_url(link):
        resolved = resolve_google_news_url(client, link)
        if not resolved:
            logger.warning("Could not resolve Google News link (no body): %s",
                           article.get("title", "")[:50])
            fallback = article.get("source_url", "")
            article["source_domain"] = urlparse(fallback).netloc.removeprefix("www.") if fallback else ""
            return "", ""
        article["google_news_link"] = link
        article["link"] = link = clean_link(resolved)

    try:
        domain = urlparse(link).netloc
        article["source_domain"] = domain[4:] if domain.startswith("www.") else domain
    except Exception:
        article["source_domain"] = ""
    if not link:
        return "", ""

    try:
        # ページ全体を読む（先頭で切ると、Wired のように本文が 46万字目以降にあるページで本文が0字になる）
        # 上限 _MAX_PAGE_BYTES まで順に読み、超えた分は切る（巨大ページでメモリを使い切らない）
        with client.stream("GET", link, headers=_PAGE_HEADERS) as resp:
            resp.raise_for_status()
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) >= _MAX_PAGE_BYTES:
                    logger.warning("Article page truncated at %d bytes: %s", _MAX_PAGE_BYTES, link[:80])
                    del data[_MAX_PAGE_BYTES:]
                    break
            content_type = resp.headers.get("content-type", "")
            final_url = str(resp.url)
        html = decode_html(bytes(data), content_type)
    except Exception:
        logger.warning("Failed to fetch article page: %s", link[:80], exc_info=False)
        return "", ""

    # 本文抽出（リッチ要約の材料。失敗しても続行＝スニペットにフォールバック）
    if fetch_full_text:
        try:
            from article_extractor import extract_main_text
            text = extract_main_text(html, max_chars=max_text_chars)
            ratio = replacement_ratio(text)
            if ratio > _MAX_REPLACEMENT_RATIO:
                logger.warning("Full text discarded (mojibake %.1f%%): %s",
                               ratio * 100, article.get("title", "")[:50])
                text = ""
            article["full_text"] = text
            if text:
                logger.info("Full text extracted (%d chars): %s", len(text), article.get("title", "")[:50])
        except Exception:
            logger.warning("Full text extraction failed for: %s", link[:80], exc_info=False)
    return html, final_url


def _save_og_image(client: httpx.Client, article: dict, html: str, page_url: str, stem: Path) -> None:
    """og:image を stem + 拡張子 に保存し article["og_image"] に入れる（画像以外・5MB超は保存しない）."""
    article["og_image"] = ""
    img_url = _og_image_url(html, page_url)
    if not img_url:
        logger.info("No og:image found for: %s", article.get("title", "")[:50])
        return
    try:
        headers = {"User-Agent": _BROWSER_UA, "Accept": "image/*", "Referer": page_url}
        with client.stream("GET", img_url, headers=headers) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = _OG_IMAGE_TYPES.get(ct)
            if not ext:
                logger.warning("OG image skipped (content-type %r): %s", ct, img_url[:80])
                return
            if int(resp.headers.get("content-length") or 0) > _MAX_OG_IMAGE_BYTES:
                logger.warning("OG image skipped (over 5MB): %s", img_url[:80])
                return
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > _MAX_OG_IMAGE_BYTES:
                    logger.warning("OG image skipped (over 5MB): %s", img_url[:80])
                    return
        img_path = stem.with_suffix(ext)
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(bytes(data))
        article["og_image"] = str(img_path)
        logger.info("OG image saved: %s", img_path)
    except Exception:
        logger.warning("Failed to fetch OG image for: %s", article.get("link", "")[:80], exc_info=False)


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
    collect_news() は材料不足の記事を入れ替えるため select_with_material() を使う。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for i, article in enumerate(articles):
            article["og_image"] = ""
            html, page_url = _fetch_article_page(client, article, fetch_full_text, max_text_chars)
            if html:
                _save_og_image(client, article, html, page_url, output_dir / f"og_{i + 1}")


def fetch_og_images(articles: list[dict], output_dir: Path) -> None:
    """Backward-compatible alias for enrich_articles (og:image only)."""
    enrich_articles(articles, output_dir, fetch_full_text=False)


def select_with_material(
    ranked: list[dict],
    top_n: int,
    output_dir: Path,
    fetch_full_text: bool = True,
    max_text_chars: int = 3000,
    extra_ng_keywords: list[str] | None = None,
    max_attempts: int | None = None,
    exclude_keys: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """並べ替え済みの候補を上から順に取得し、使える記事だけを top_n 件選ぶ.

    fetch_full_text=True のとき、材料（本文か RSS 概要）が _MIN_MATERIAL_CHARS 字未満の記事は
    要約・facts を作らせると中身のない台本になるため選ばず、次点の記事で埋める。
    本文を取った後にもう一度 NG キーワードを確かめる（題名・概要では分からないことがある）。
    OG 画像は採用が決まった記事だけ output_dir/og_{採用順}.{ext} に保存する。
    取得後の（転送 URL を解決した）URL が exclude_keys（過去分の url_key）か選定済みの記事と
    同じなら選ばない。

    Returns:
        (selected, skipped)。skipped の各記事には skip_reason が入る。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = max_attempts or top_n + 10
    seen_keys: set[str] = set(exclude_keys or ())  # 過去の URL キー＋この回で選定済みの URL キー
    selected: list[dict] = []
    skipped: list[dict] = []
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for article in ranked[:max_attempts]:
            if len(selected) >= top_n:
                break
            article["og_image"] = ""
            html, page_url = _fetch_article_page(client, article, fetch_full_text, max_text_chars)
            # Google News の転送 URL は元記事に解決して初めて本当の URL が分かる。解決後の URL を
            # 過去分・この回で選定済みの記事と照合し直す（収集時の重複判定は転送 URL のままだった）
            key = url_key(article.get("link", ""))
            if key and key in seen_keys:
                article["skip_reason"] = "解決後のURLが過去または選定済みの記事と重複"
                logger.info("Dedup (resolved URL already used): %s", article.get("title", "")[:60])
                skipped.append(article)
                continue
            if fetch_full_text:
                chars = material_chars(article)
                if chars < _MIN_MATERIAL_CHARS:
                    article["skip_reason"] = f"材料不足（本文・概要が{chars}字）"
                    logger.warning("Skipped (material %d chars < %d): %s", chars, _MIN_MATERIAL_CHARS,
                                   article.get("title_ja") or article.get("title", "")[:50])
                    skipped.append(article)
                    continue
                if not filter_excluded_content([article], extra_ng_keywords, include_body=True):
                    article["skip_reason"] = "本文にNGキーワード"
                    skipped.append(article)
                    continue
            if html:
                _save_og_image(client, article, html, page_url, output_dir / f"og_{len(selected) + 1}")
            selected.append(article)
            if key:
                seen_keys.add(key)
    # 同じ日の前回分の残り（拡張子違いなど）を消す。選定できたときだけ（失敗時は前回の news JSON が使う）
    if selected:
        produced = {Path(a["og_image"]).name for a in selected if a.get("og_image")}
        for old in output_dir.glob("og_*"):
            if old.name not in produced:
                try:
                    old.unlink()
                except OSError:  # 開いているなどで消せなくても収集は止めない
                    logger.warning("Could not remove stale OG image: %s", old)
    return selected, skipped


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def _parse_published_date(date_str: str) -> datetime | None:
    """Try to parse various date formats from RSS published fields.

    タイムゾーン付きの日付はそのオフセットを使う（以前は %z を読んだ後に
    replace(tzinfo=UTC) で上書きしていた）。タイムゾーンのない日付だけ UTC とみなす。
    """
    if not date_str:
        return None
    dt: datetime | None = None
    # Try RFC 2822 (e.g. "Mon, 12 Jan 2026 11:30:00 GMT")
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        dt = None
    if dt is None:
        # ISO 8601（"Z"・小数秒・オフセット付きを含む。例 "2026-05-12T12:30:33.123-04:00"）
        try:
            dt = datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _article_datetime(article: dict) -> datetime | None:
    """記事の公開日時（feedparser が UTC に直した published_at を優先）."""
    iso = article.get("published_at")
    if iso:
        try:
            return datetime.fromisoformat(iso)
        except ValueError:
            pass
    return _parse_published_date(article.get("published", ""))


def _freshness(article: dict, max_age_hours: int, reference_time: datetime | None = None) -> str:
    """"fresh" / "old" / "future" / "unknown" を返す.

    未来の日付は _FUTURE_TOLERANCE_HOURS までならタイムゾーンの書き間違いとみなして
    "fresh"、それより先は信用できない日付として "future" にする。
    """
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    pub_date = _article_datetime(article)
    if pub_date is None:
        return "unknown"
    age_hours = (reference_time - pub_date).total_seconds() / 3600
    if age_hours < -_FUTURE_TOLERANCE_HOURS:
        return "future"
    return "fresh" if age_hours <= max_age_hours else "old"


def filter_by_freshness(
    articles: list[dict],
    max_age_hours: int = _MAX_ARTICLE_AGE_HOURS,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Remove articles older than max_age_hours (and articles dated far in the future).

    Articles with unparseable dates are kept (benefit of the doubt).
    """
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)

    fresh = []
    for article in articles:
        state = _freshness(article, max_age_hours, reference_time)
        if state == "unknown":
            # Can't determine age — keep
            logger.debug("No parseable date, keeping: %s", article.get("title", "")[:60])
            fresh.append(article)
        elif state == "fresh":
            fresh.append(article)
        elif state == "future":
            logger.warning("Filtered out (date in the future, unreliable): %s — %s",
                           article.get("published", ""), article.get("title", "")[:60])
        else:
            logger.info("Filtered out (older than %dh): %s", max_age_hours, article.get("title", "")[:60])

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
#   use explicit compounds instead. Compounds like 性的虐待/性犯罪 stay plain
#   substrings so that 児童性的虐待 / 児童性犯罪 are still caught.
# - Words that are only NG in some contexts (性的な vs 個性的な, 裸 vs 裸眼/丸裸)
#   live in _EXCLUDED_CONTENT_CONTEXT_PATTERNS as explicit regexes.
_EXCLUDED_CONTENT_KEYWORDS = [
    # Japanese
    "わいせつ", "ワイセツ", "猥褻", "ポルノ", "アダルト",
    "性的コンテンツ", "性的画像", "性的暴力", "性的被害", "性的搾取", "性的虐待",
    "性的ディープフェイク",
    "性被害", "性犯罪", "性搾取", "児童ポルノ", "ヌード", "風俗", "セクハラ",
    # English (leading word boundary applied; stems match derived forms)
    "obscen", "porn", "sexual", "nude", "nudity", "nsfw", "csam",
    "erotic", "masturbat", "sextortion", "explicit image", "explicit content",
    "sex abuse", "sex crime",
    "nonconsensual intimate", "non-consensual intimate", "intimate image",
    "nudif", "undress", "deepnude", "ncii",
]

# 文脈しだいで普通の語にもなる NG 語（誤爆しやすいものは前後の字で除外する）
_EXCLUDED_CONTENT_CONTEXT_PATTERNS = [
    # 性的な: 個性的な・女性的な・知性的な・理性的な・慢性的な など「◯性」+「的な」の普通の語は除く
    # （漢字全般を除くと「児童性的な画像」まで見逃すので、よく出る字だけを挙げる）
    ("性的な", re.compile(r"(?<![個女男知理感慣惰習属特本中野酸陽陰急慢弾適毒然能産動定活続質創悲])性的な")),
    # 裸: 裸眼・裸足・赤裸々・丸裸（データが丸裸に）・裸一貫・裸電球・裸の王様・裸子植物 は普通の語
    ("裸", re.compile(r"(?<![丸赤])裸(?![眼足々一電線麦子]|の王様)")),
    # 脱衣: 脱衣所・脱衣場・脱衣かご は普通の語
    ("脱衣", re.compile(r"脱衣(?![所場室]|かご|籠)")),
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


def filter_excluded_content(
    articles: list[dict],
    extra_keywords: list[str] | None = None,
    include_body: bool = False,
) -> list[dict]:
    """Drop articles about banned content (obscene/sexual topics).

    Runs in ALL modes (default AI and custom topic) before LLM scoring.
    The LLM scoring prompt has a matching NG rule as the second line of defense.
    include_body=True のときは本文（full_text）も見る（本文取得後の再チェック用）。
    """
    keywords = _EXCLUDED_CONTENT_KEYWORDS + [k.lower() for k in (extra_keywords or [])]
    patterns = [(kw, _ng_keyword_pattern(kw)) for kw in keywords] + _EXCLUDED_CONTENT_CONTEXT_PATTERNS
    kept = []
    for a in articles:
        text = (a.get("title", "") + " " + a.get("summary", "")).lower()
        if include_body:
            text += " " + (a.get("full_text") or "").lower()
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
{cleaning}

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
3つの平均を基に、最終スコアを小数第1位まで付けてください（例: 7.4、6.8）。
平均をそのまま .3 や .7 に丸めず、ほかの記事と差がつくよう細かく評価してください。

日本語のタイトルと50文字以内の要約も生成してください。

JSON形式のみで回答（説明文不要）:
{{"relevant": true または false, "score": 数値（小数第1位まで）, "title_ja": "日本語タイトル", "summary_ja": "50文字以内の要約"}}
"""


def _to_score(value) -> float:
    """LLM が返した score を数値にする（"7.5" のような文字列・範囲外・不正値に対応）."""
    try:
        score = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return round(min(max(score, 0.0), 10.0), 2)


_FALSE_WORDS = {"false", "no", "0", "none", "null", "いいえ", "偽", "無関係"}
_TRUE_WORDS = {"true", "yes", "1", "はい", "真"}


def _to_bool(value, default: bool = True) -> bool:
    """LLM が返した relevant を真偽値にする（bool("false") が True になる問題の対策）."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    word = str(value).strip().lower()
    if word in _FALSE_WORDS:
        return False
    if word in _TRUE_WORDS:
        return True
    return default


def rank_key(article: dict) -> tuple:
    """並べ替えのキー: スコアが高い順、同点なら新しい記事を先に."""
    return (_to_score(article.get("score", 0)), article.get("published_at") or "")


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
    ただし認証エラー（LLMAuthError）は再試行せずそのまま投げ、全件失敗したときは
    RuntimeError を投げる。どちらも呼び出し側（collect_news）がキーワード採点に切り替える。
    """
    from api_utils import LLMClient
    from cleaning import cleaning_short

    client = LLMClient(llm_config)
    scored = []
    max_tokens = llm_config.get(llm_config.get("provider", "gemini"), {}).get("max_tokens_scoring", 200)
    scoring_prompt = SCORING_SYSTEM_PROMPT_TEMPLATE.format(topic=topic, cleaning=cleaning_short())
    succeeded = 0

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
                    relevant = _to_bool(result.get("relevant", True))
                    article["relevant"] = relevant
                    article["score"] = _to_score(result.get("score", 0)) if relevant else 0
                    article["title_ja"] = str(result.get("title_ja") or article["title"])
                    article["summary_ja"] = str(result.get("summary_ja") or "")

                    # Validate that title_ja is actually Japanese
                    if not _is_japanese(article["title_ja"]):
                        logger.warning("  title_ja not Japanese, retrying: %s", article["title_ja"][:50])
                        continue

                    success = True
                    break
                else:
                    logger.warning("  No JSON in response (attempt %d): %s", attempt + 1, text[:100])

            except LLMAuthError:
                raise  # 認証エラーは何度やっても直らない（全件×再試行で時間を浪費しない）
            except Exception:
                logger.warning("  Scoring attempt %d failed for: %s", attempt + 1, article["title"][:50], exc_info=True)

        if not success:
            logger.error("  All retries exhausted for: %s", article["title"][:50])
            article["score"] = 0
            article["relevant"] = False
            article["title_ja"] = article["title"]
            article["summary_ja"] = ""
        else:
            succeeded += 1

        scored.append(article)
        logger.info("  [%d/%d] score=%s: %s", idx + 1, len(articles), article["score"], article["title"][:50])

    if articles and succeeded == 0:
        raise RuntimeError(f"LLM scoring failed for all {len(articles)} articles")
    scored.sort(key=rank_key, reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Cross-outlet topic deduplication + source diversity
# ---------------------------------------------------------------------------

CLUSTER_SYSTEM_PROMPT = """\
あなたはニュース編成の専門家です。同じ出来事・同じ発表を報じている記事を
グループにまとめてください（媒体が違っても同じ出来事なら同じグループ）。
__CLEANING__

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
    warnings: list[dict] | None = None,
) -> list[dict]:
    """媒体跨ぎの同一トピック記事を1本に集約し、ソース多様性を確保して並べ替える.

    スコア降順の上位 pool_size 件を1回のLLM呼び出しでストーリー単位にクラスタリングし、
    各クラスタからスコア最高の1件を代表として採用する。さらに同一ソースは
    max_per_source 件までに制限する（超過分は後回し）。

    Returns:
        並べ替え済みの記事リスト（先頭から top_n を取れば多様な選定になる）。
        クラスタリング失敗時は入力をそのまま返す（abortしない）。失敗は warnings に残す
        （news JSON の collect_warnings → 確認用まとめ review_DATE.md に WARN として出る）。
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
        from cleaning import cleaning_short
        system = CLUSTER_SYSTEM_PROMPT.replace("__CLEANING__", cleaning_short())
        text = client.generate(system, user_prompt, max_tokens)
        text = (text or "").strip()
        if "{" not in text:
            raise ValueError(f"No JSON in clustering response: {text[:100]}")
        result = json.loads(text[text.index("{"):text.rindex("}") + 1])
        raw_clusters = result.get("clusters", [])
        if not isinstance(raw_clusters, list) or not raw_clusters:
            raise ValueError("clusters missing or empty")
    except Exception as e:
        logger.warning("Topic clustering failed — keeping score order", exc_info=True)
        if warnings is not None:
            warnings.append({"check": "媒体違いの同じ話のまとめ（クラスタリング）が失敗",
                             "detail": f"スコア順のまま選定。同じ話が2本入っていないか確認する（{e}）"[:300]})
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
    representatives.sort(key=rank_key, reverse=True)

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
# 過去に扱った話との重複（媒体違い・URL違いの同じ話）
# ---------------------------------------------------------------------------

PAST_DEDUP_SYSTEM_PROMPT = """\
あなたはニュース編成の専門家です。「過去に扱ったニュース」と「今日の候補」を比べ、
過去と同じ出来事・同じ発表を報じている候補を見つけてください
（媒体や言語が違っても、同じ出来事なら同じとみなす）。
__CLEANING__

ルール:
- 「同じ」とは、同じ主体（会社・人・団体）の同じ発表・同じ事件・同じ調査結果のこと。
  同じ出来事の別媒体の記事・言い換え・要約記事は「同じ」
- テーマや分野が近いだけ（例: どちらも「AIと出版社」「AIの安全性」の話）なら「別」
- 続報でも、新しい事実（正式発表・結果・新しい数字など）が話の中心なら「別」
- 迷ったら「別」にすること（過剰に除外しない）
- event には、両者に共通する出来事を20文字以内で書く（書けないなら「別」）
- 該当がなければ空の配列を返す

JSON形式のみで回答（説明文不要）:
{"duplicates": [{"candidate": 3, "past": "P2", "event": "共通する出来事"}]}
"""


def _rank_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def deduplicate_stories_against_past(
    articles: list[dict],
    llm_config: dict,
    state_dir: Path,
    current_date: str,
    lookback_days: int = 7,
    max_rank: int = 3,
    warnings: list[dict] | None = None,
) -> list[dict]:
    """過去 lookback_days 日に動画にした話と同じ話の候補を除く（1回のLLM呼び出し）.

    比べる相手は台帳（state/seen_topics.json）の、投稿済みの行と、その日の選定で
    max_rank 位以内（＝動画にした）の行。URL が違う別媒体の同じ話（2026-10-24/25/27 に
    3回投稿された OpenAI の件）を落とすためのもの。
    LLM が失敗したら、題名の語の重なりによる控えめな簡易判定に切り替える。
    """
    from seen_topics import is_similar_title, recent_entries, shares_entity, sync_ledger

    if not articles:
        return articles
    try:
        recent = recent_entries(sync_ledger(state_dir), current_date, lookback_days)
    except Exception:
        # 台帳が読めない警告は deduplicate_against_past() が出している
        logger.warning("Ledger unavailable — skipping past story dedup", exc_info=True)
        return articles
    past = [e for e in recent
            if e.get("uploaded") or (_rank_int(e.get("rank")) or 99) <= max_rank]
    past = [e for e in past if e.get("title_ja") or e.get("title")][-60:]
    if not past:
        return articles

    dup_pairs: dict[int, int] = {}  # 候補番号(1始まり) → 過去番号(1始まり)
    events: dict[int, str] = {}     # 候補番号 → LLM が挙げた共通の出来事（ログ用）
    try:
        from api_utils import LLMClient
        from cleaning import cleaning_short

        client = LLMClient(llm_config)
        provider_cfg = llm_config.get(llm_config.get("provider", "gemini"), {})
        max_tokens = max(provider_cfg.get("max_tokens_scoring", 200), 400)
        # 題名だけを渡す（要約も添えると、テーマが近いだけの別の出来事を「同じ」とする誤りが増えた。2026-11-01 実測）
        past_lines = [f"P{i}. {e.get('title_ja') or e.get('title', '')}"
                      + (f" / {e['title']}" if e.get("title") and e.get("title_ja") else "")
                      + f"（{e.get('date', '')}）"
                      for i, e in enumerate(past, 1)]
        cand_lines = [f"{i}. {a.get('title_ja') or a.get('title', '')}"
                      + (f" / {a['title']}" if a.get("title") and a.get("title_ja") != a.get("title") else "")
                      + f"（{a.get('source', '')}）"
                      for i, a in enumerate(articles, 1)]
        user_prompt = ("過去に扱ったニュース:\n" + "\n".join(past_lines)
                       + "\n\n今日の候補:\n" + "\n".join(cand_lines))
        system = PAST_DEDUP_SYSTEM_PROMPT.replace("__CLEANING__", cleaning_short())
        text = (client.generate(system, user_prompt, max_tokens, temperature=0) or "").strip()
        if "{" not in text:
            raise ValueError(f"No JSON in past-dedup response: {text[:100]}")
        result = json.loads(text[text.index("{"):text.rindex("}") + 1])
        raw = result.get("duplicates", [])
        if not isinstance(raw, list):
            raise ValueError("duplicates is not a list")
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                cand = int(str(item.get("candidate")).strip())
            except (TypeError, ValueError):
                continue
            m = re.search(r"\d+", str(item.get("past") or ""))
            past_no = int(m.group()) if m else 0
            # どの過去記事と同じかを示せない判定は採らない（迷ったら別＝除外しすぎない）
            if not (1 <= cand <= len(articles) and 1 <= past_no <= len(past)):
                continue
            # 歯止め: 共通の固有名（会社・製品名）も題名の似かたもない組は「テーマが近いだけ」とみなす
            # （Haiku は「音声AI」「AIと出版社」のような話題の近さで同じと判定しがち。2026-11-01 実測で6件中5件）
            if not (shares_entity(articles[cand - 1], past[past_no - 1])
                    or is_similar_title(articles[cand - 1], past[past_no - 1])):
                logger.info("Past-story dedup: LLM match rejected (no shared name): %s ⇔ %s",
                            (articles[cand - 1].get("title_ja") or articles[cand - 1].get("title", ""))[:40],
                            (past[past_no - 1].get("title_ja") or past[past_no - 1].get("title", ""))[:40])
                continue
            dup_pairs[cand] = past_no
            events[cand] = str(item.get("event") or "")[:40]
    except Exception as e:
        # 認証エラー（LLMAuthError）もここで受ける: 採点がキーワード採点に落ちた日でも収集を止めない
        logger.warning("Past-story dedup (LLM) failed — using title similarity fallback", exc_info=True)
        if warnings is not None:
            warnings.append({"check": "過去と同じ話の判定（LLM）が失敗",
                             "detail": f"題名の似かたによる簡易判定に切り替えた。過去の投稿と同じ話がないか確認する（{e}）"[:300]})
        for i, a in enumerate(articles, 1):
            for j, e in enumerate(past, 1):
                if is_similar_title(a, e):
                    dup_pairs[i] = j
                    break

    if not dup_pairs:
        return articles
    kept = []
    for i, a in enumerate(articles, 1):
        if i in dup_pairs:
            p = past[dup_pairs[i] - 1]
            logger.info("Dedup (same story as %s %s%s): %s", p.get("date", "?"),
                        (p.get("title_ja") or p.get("title", ""))[:40],
                        f" — {events[i]}" if events.get(i) else "",
                        (a.get("title_ja") or a.get("title", ""))[:50])
            continue
        kept.append(a)
    logger.info("Past story dedup: %d → %d articles", len(articles), len(kept))
    return kept


# ---------------------------------------------------------------------------
# Per-article rich summarization (script material)
# ---------------------------------------------------------------------------

ENRICH_SYSTEM_PROMPT_TEMPLATE = """\
あなたはAI初心者の大人向けニュース番組の構成作家です。{topic}ニュース記事の内容を、
初心者向け台本の材料になるよう日本語で整理してください。
{cleaning}

ルール:
- summary_ja_long: 記事の内容を正確に伝える200〜300文字の日本語要約。
  記事に書かれていないことを推測で書かない。
- facts: 台本のたとえ話や具体例に使える、記事中の具体的な事実・数字・できごとを2〜3個。
- kid_angle: このニュースが視聴者（専門知識のない大人）の生活・仕事・お金に
  どう関係するかの一言（50文字以内）。※フィールド名は互換性のためkid_angleのまま。
- 金額の換算に注意（桁を間違えやすい）: 1 billion = 10億、1 million = 100万、1 trillion = 1兆。
  例: $740 billion = 7400億ドル、$9.1 billion = 91億ドル、$500 million = 5億ドル、
  $1.2 trillion = 1兆2000億ドル。「$740 billion」を「740億ドル」と書かない。
- 会計用語を言い換えない: negative free cash flow（フリーキャッシュフローのマイナス＝資金の持ち出し）や
  spending（支出）を「赤字」と書かない。「赤字」は net loss / operating loss のときだけ使う。

JSON形式のみで回答（説明文不要）:
{{"summary_ja_long": "...", "facts": ["...", "..."], "kid_angle": "..."}}
"""


def _fix_money_in_article(article: dict, body: str) -> None:
    """要約・事実・題名の「◯億ドル」の桁ずれを英語原文に合わせて直す（2026-11-01 の 10倍誤訳対策）.

    直した内容は article["money_unit_fixes"] に残し、確認用まとめ（review）に出す。
    """
    from money_units import fix_money_units

    source = f"{article.get('title', '')}\n{body}"
    fixes: list[str] = []
    for key in ("title_ja", "summary_ja", "summary_ja_long"):
        if article.get(key):
            article[key], changes = fix_money_units(article[key], source)
            fixes += changes
    if article.get("facts"):
        new_facts = []
        for f in article["facts"]:
            fixed, changes = fix_money_units(f, source)
            new_facts.append(fixed)
            fixes += changes
        article["facts"] = new_facts
    if fixes:
        article["money_unit_fixes"] = list(dict.fromkeys(fixes))
        logger.warning("  Money unit fixed (英語原文と桁ずれ): %s — %s",
                       article.get("title_ja", "")[:40], "、".join(article["money_unit_fixes"]))


def summarize_articles_with_llm(
    articles: list[dict],
    llm_config: dict,
    topic: str = "AI",
) -> None:
    """選定済み記事に summary_ja_long / facts / kid_angle を付与する（記事ごと1呼び出し）.

    本文（full_text）があればそれを、なければRSSスニペットを材料にする。
    材料が _MIN_MATERIAL_CHARS 字未満の記事は要約・facts を作らない（推測で埋めさせない）。
    失敗した記事は既存の summary_ja のみで続行する（abortしない）。認証エラーなら残りも諦める。
    """
    from api_utils import LLMClient

    client = LLMClient(llm_config)
    provider_cfg = llm_config.get(llm_config.get("provider", "gemini"), {})
    max_tokens = provider_cfg.get("max_tokens_enrich", 1000)
    from cleaning import cleaning_short
    system_prompt = ENRICH_SYSTEM_PROMPT_TEMPLATE.format(topic=topic, cleaning=cleaning_short())

    for idx, article in enumerate(articles):
        body = article.get("full_text") or article.get("summary", "")
        if len(body) < _MIN_MATERIAL_CHARS:
            logger.warning("  Enrich skipped (material %d chars): %s", len(body),
                           article.get("title_ja", "")[:50])
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
        except LLMAuthError:
            logger.error("Article enrich stopped (LLM auth error) — remaining articles keep short summaries")
            break
        except Exception:
            logger.warning("Article enrich failed (using short summary): %s",
                           article.get("title_ja", "")[:50], exc_info=False)
        _fix_money_in_article(article, body)


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
    articles.sort(key=rank_key, reverse=True)
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

def _feed_warnings(feed_stats: list[dict]) -> list[dict]:
    """取得に失敗した・0件だったフィードを確認用の警告にする."""
    out = []
    for st in feed_stats:
        if st.get("error") or st.get("status") not in (None, 200):
            reason = st.get("error") or f"HTTP {st.get('status')}"
        elif st.get("status") == 200 and st.get("entries", 0) == 0:
            reason = "0件" + (f"（形式エラー: {st['bozo']}）" if st.get("bozo") else "")
        else:
            continue
        out.append({"check": "フィードが取れない", "detail": f"{st['url']} — {reason}"[:300]})
    return out


def collect_news(
    config_path: Path = Path("config.json"),
    date_str: str | None = None,
    topic: str = "",
) -> Path:
    """Run the full news collection pipeline. Returns path to output JSON.

    Args:
        topic: Custom topic keyword (e.g. "車", "宇宙"). If empty, uses default AI news.
            カスタムトピックは news_{date}_{slug}.json に保存する（AIニュースを上書きしない）。

    Raises:
        RuntimeError: 選定できた記事が0件のとき（空の news JSON で "done" にしない）。
    """
    config = load_config(config_path)
    news_cfg = config["news"]
    llm_cfg = config.get("llm", {})
    state_dir = Path(config["paths"]["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)

    if date_str is None:
        date_str = datetime.now(_JST).strftime("%Y%m%d")

    max_age = news_cfg.get("max_article_age_hours", _MAX_ARTICLE_AGE_HOURS)
    max_per_feed = news_cfg.get("max_per_feed", _MAX_PER_FEED)
    feed_stats: list[dict] = []
    warnings: list[dict] = []
    counts: dict[str, int] = {}
    articles = []

    cse_cfg = news_cfg.get("google_cse", {})
    cse_api_key = os.environ.get(cse_cfg.get("api_key_env", ""), "")
    cse_engine_id = os.environ.get(cse_cfg.get("engine_id_env", ""), "")

    if topic:
        # Custom topic mode: skip RSS, use Google News RSS (ja + en).
        # Google CSE は新規受付終了のため、環境変数があるときだけ追加で使う。
        logger.info("Custom topic mode: '%s'", topic)
        articles = collect_from_google_news(topic, max_per_feed=max_per_feed,
                                            max_age_hours=max_age, stats=feed_stats)
        if cse_api_key and cse_engine_id:
            topic_queries = [
                f"{topic} ニュース 今日",
                f"{topic} 最新ニュース",
                f"{topic} news today",
            ]
            articles.extend(collect_from_google_cse(topic_queries, cse_api_key, cse_engine_id))
    else:
        # Default AI news mode: RSS + Google CSE
        articles = collect_from_rss(news_cfg["rss_feeds"], max_per_feed=max_per_feed,
                                    max_age_hours=max_age, stats=feed_stats)

        if cse_api_key and cse_engine_id:
            cse_articles = collect_from_google_cse(
                cse_cfg.get("queries", []),
                cse_api_key,
                cse_engine_id,
            )
            articles.extend(cse_articles)
        else:
            logger.info("Google CSE not configured, skipping")
    counts["fetched"] = len(articles)
    warnings += _feed_warnings(feed_stats)
    for st in feed_stats:
        logger.info("Feed: %s status=%s entries=%d fresh=%d kept=%d", st["url"][:80],
                    st.get("status") or st.get("error", "")[:40], st["entries"], st["fresh"], st["kept"])

    # 3. Deduplicate (within today)
    articles = deduplicate(articles)
    counts["unique"] = len(articles)
    logger.info("Total unique articles: %d", len(articles))

    # 3.5 Remove articles already used in past days
    articles = deduplicate_against_past(articles, state_dir, date_str, warnings=warnings)
    counts["after_past_url_dedup"] = len(articles)

    # 4. Filter by freshness (reject articles older than 48h)
    articles = filter_by_freshness(articles, max_age_hours=max_age)
    counts["fresh"] = len(articles)

    # 4.3 NG-content exclusion (all modes): obscene/sexual topics are banned
    # from the channel (user decision 2026-08-18).
    articles = filter_excluded_content(articles, news_cfg.get("exclude_keywords"))
    counts["after_ng_filter"] = len(articles)

    # 4.5 AI-relevance keyword pre-filter (default AI mode only).
    # General-purpose feeds bring in non-AI noise; drop it cheaply before scoring.
    if not topic:
        articles = filter_ai_relevant(articles)
        counts["after_ai_filter"] = len(articles)
        if not articles:
            logger.warning(
                "AI keyword pre-filter removed ALL articles — feeds may be "
                "entirely non-AI today."
            )

    # Limit before scoring（フィードを交互に並べてから切る＝後ろのフィードが丸ごと落ちない）
    max_articles = news_cfg.get("max_articles", 30)
    articles = interleave_by_source(articles)[:max_articles]

    # 5. Score articles with LLM (fallback to keywords on failure)
    topic_label = topic if topic else "AI"
    try:
        scored = score_articles_with_llm(articles, llm_cfg, topic=topic_label)
    except Exception as e:
        logger.warning("LLM scoring failed, using keyword fallback: %s", e)
        warnings.append({"check": "LLM 採点が失敗しキーワード採点に切り替えた", "detail": str(e)[:300]})
        scored = score_articles_fallback(articles)

    total_scored = len(scored)  # count before relevance gate (for reporting)
    counts["scored"] = total_scored

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
    counts["relevant"] = len(scored)

    # 5.6 過去に動画にした話と同じ話（媒体違い・URL違い）を除く（1 LLM call）
    shorts_min = config.get("shorts", {}).get("topics_per_day", 3)
    if news_cfg.get("past_story_dedup", True) and scored:
        scored = deduplicate_stories_against_past(
            scored, llm_cfg, state_dir, date_str,
            max_rank=shorts_min, warnings=warnings,
        )
    counts["after_past_story_dedup"] = len(scored)

    # 5.7 Cross-outlet topic dedup + source diversity (1 LLM call).
    # 同じ出来事を報じる媒体違いの記事を1本に集約し、同一ソース偏重を防ぐ。
    if news_cfg.get("topic_dedup", False) and len(scored) > 1:
        scored = cluster_topics_with_llm(
            scored,
            llm_cfg,
            pool_size=news_cfg.get("cluster_pool_size", 15),
            warnings=warnings,
        )

    # 6+8. Select top N and enrich them: og:image + 本文テキスト + 出典ドメイン。
    # 本文を取る設定では、材料（本文・概要）が足りない記事や本文にNG語がある記事は
    # 選ばずに次点で埋める。OG 画像は日付ごとのフォルダに保存する（日をまたいで上書きしない）。
    top_n = news_cfg.get("top_n", 5)
    og_dir = state_dir / "og_images" / (f"{date_str}_{topic_slug(topic)}" if topic else date_str)
    fetch_full = news_cfg.get("fetch_full_text", False)
    selected, skipped = select_with_material(
        scored, top_n, og_dir, fetch_full_text=fetch_full,
        extra_ng_keywords=news_cfg.get("exclude_keywords"),
        exclude_keys=past_url_keys(state_dir, date_str, warnings=warnings),
    )
    counts["selected"] = len(selected)

    # 0件で "done" にしない（下流が空の台本・動画を作ってしまう）。既存の news JSON も上書きしない
    if not selected:
        problems = [w["detail"] for w in warnings if w["check"] == "フィードが取れない"]
        raise RuntimeError(
            f"No articles selected for {date_str} (topic={topic_label}). counts={counts}; "
            f"feed problems: {problems}"
        )
    if len(selected) < top_n:
        logger.warning("Only %d/%d articles selected (skipped %d for missing material)",
                       len(selected), top_n, len(skipped))
        warnings.append({"check": f"選定記事が top_n（{top_n}）より少ない",
                         "detail": f"{len(selected)}件（材料不足などで外した記事 {len(skipped)}件）"})
    if len(selected) < shorts_min:
        warnings.append({"check": f"Shorts の本数（topics_per_day={shorts_min}）に足りない",
                         "detail": f"{len(selected)}本しか作れない"})

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
        # 絞り込み前（フィードから取れた件数）と、各段階の後の件数
        "total_fetched": counts["fetched"],
        "counts": counts,
        "selected_count": len(selected),
        "feed_stats": feed_stats,
        "collect_warnings": warnings,
        "skipped_articles": [{"title": a.get("title_ja") or a.get("title", ""), "link": a.get("link", ""),
                              "reason": a.get("skip_reason", "")} for a in skipped],
        "articles": selected,
    }
    output_path = news_json_path(state_dir, date_str, topic)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 台帳に今日の選定を残す（翌日以降の重複判定に使う。投稿済みの印は upload_log から付く）
    try:
        from seen_topics import record_selection
        record_selection(state_dir, date_str, topic, selected)
    except Exception:
        logger.warning("Failed to update seen_topics ledger", exc_info=True)

    for w in warnings:
        logger.warning("Collect WARN: %s — %s", w["check"], w["detail"])
    logger.info("Saved %d articles to %s", len(selected), output_path)
    return output_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="AI News Collector")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--date", default=None, help="Date string YYYYMMDD")
    parser.add_argument("--topic", default="", help="Custom news topic (default: AI news)")
    args = parser.parse_args()
    path = collect_news(Path(args.config), args.date, topic=args.topic)
    print(f"Output: {path}")


if __name__ == "__main__":
    main()
