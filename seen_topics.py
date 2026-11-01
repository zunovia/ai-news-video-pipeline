"""扱ったニュースの台帳（state/seen_topics.json）と URL・題名の同一判定.

過去分の重複排除は、以前は news_{date}.json の URL 完全一致だけだった。news JSON は
手で state/old に移したり消したりするうえ、別媒体の同じ話は URL が違うため、
同じ話が 10/24・10/25・10/27 の3回投稿された。

台帳は追記のみで、過去の行は消さない。収集のたびに次の2つから取り込み直す
（何度実行しても同じ結果になる）:
  - state/ と state/old/ の news_*.json（選定記事。rank = 選定順位）
  - state/upload_log.json（投稿済みの印。news JSON が残っていない日は題名だけの行を作る）

行の形: {"key", "url", "title", "title_ja", "summary_ja", "date", "topic", "rank", "uploaded"}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

LEDGER_NAME = "seen_topics.json"

# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

# 追跡用のクエリ（記事の中身に関係しない）。utm_* は前方一致で別途除く
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "cmpid", "ocid", "oc", "ref", "ref_src", "spm", "guccounter",
    "_hsenc", "_hsmi", "mkt_tok", "yclid",
}


def clean_link(url: str) -> str:
    """追跡用クエリ（utm_* など）とフラグメントを除いたリンクを返す（保存用）."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in pairs
             if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS]
    if len(query) == len(pairs) and not parts.fragment:
        return url  # 何も除かないときは元の文字列のまま（クエリの書き方を変えない）
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def url_key(url: str) -> str:
    """重複判定用の正規化キー（http/https・www.・末尾の / ・大文字ホストの違いを無視）."""
    url = clean_link(url)
    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


# ---------------------------------------------------------------------------
# Title similarity (LLM が使えないときの簡易判定)
# ---------------------------------------------------------------------------

_ASCII_WORD_RE = re.compile(r"[a-z0-9][a-z0-9.\-]*[a-z0-9]|[a-z0-9]")
_KATAKANA_RE = re.compile(r"[ァ-ヶー]{2,}")
_KANJI_RE = re.compile(r"[一-鿿々]+")
# どの記事にも出る語は手がかりにならないので数えない
_STOP_TOKENS = {
    "ai", "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
    "its", "it", "new", "how", "why", "what", "says", "say", "will", "now", "from", "by", "as",
    "at", "be", "this", "that", "after", "into", "about", "more", "can", "your", "you",
    "モデル", "ニュース", "サービス", "発表", "公開", "開始", "可能", "登場", "企業",
}


def title_tokens(text: str) -> set[str]:
    """題名を比較用の語に分ける（英数字の語・カタカナ語・漢字の2文字組）."""
    t = (text or "").lower()
    tokens = set(_ASCII_WORD_RE.findall(t))
    tokens |= set(_KATAKANA_RE.findall(t))
    for run in _KANJI_RE.findall(t):
        if len(run) == 1:
            continue
        tokens |= {run[i:i + 2] for i in range(len(run) - 1)}
    return {tok for tok in tokens if tok not in _STOP_TOKENS}


def title_similarity(a: str, b: str) -> tuple[float, int]:
    """(Dice 係数, 共通語の数) を返す."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0, 0
    shared = len(ta & tb)
    return 2 * shared / (len(ta) + len(tb)), shared


# 固有名とみなさない一般語（英語・カタカナ）。shares_entity() の比較から外す
_GENERIC_NAME_TOKENS = _STOP_TOKENS | {
    "agent", "agents", "model", "models", "tool", "tools", "data", "app", "apps", "chatbot", "chatbots",
    "startup", "startups", "company", "companies", "users", "user", "research", "study", "chip", "chips",
    "news", "update", "feature", "features", "security", "privacy", "code", "software", "tech", "big",
    "first", "best", "report", "launch", "launches", "says", "said", "gets", "make", "makes", "using",
    "エージェント", "ツール", "チャット", "チャットボット", "データ", "ネットワーク", "ユーザー", "ユーザ",
    "システム", "アプリ", "セキュリティ", "ロボット", "プラットフォーム", "コード", "ソフトウェア",
    "ハードウェア", "スマートフォン", "スマホ", "ブラウザ", "インターネット", "ネット", "テクノロジー",
    "デバイス", "クラウド", "サーバー", "コンピューター", "パソコン", "プログラム", "ウェブサイト", "サイト",
    "スタートアップ", "ビジネス", "オープンソース", "エンジニア", "プロジェクト", "リスク", "コスト",
    "ルール", "アップデート", "ゲーム", "ドキュメント", "ファイル", "メール", "メモリ", "チップ", "カメラ",
    "ユーザー", "コンテンツ", "メディア", "サポート", "テスト", "トップ",
}


# 固有名とみなさない漢字の一般語。漢字の連続からこれらを取り除いた残りが3字以上なら固有名の候補にする
# （例: 「国防総省」は残る。「人工知能」「生成」「発表」は取り除かれる）。長い語を先に並べる
_GENERIC_KANJI_WORDS = sorted({
    "人工知能", "機械学習", "深層学習", "生成", "発表", "公開", "開始", "提供", "開発", "導入", "利用", "活用",
    "対応", "搭載", "投入", "強化", "拡大", "計画", "検討", "実施", "予定", "可能", "登場", "企業", "会社",
    "業界", "市場", "技術", "研究", "調査", "報告", "分析", "問題", "影響", "最新", "新型", "新機能", "新製品",
    "情報", "世界", "日本", "米国", "中国", "政府", "自動", "安全", "対策", "規制", "法案", "契約", "投資",
    "買収", "提携", "協力", "最大", "最高", "最初", "今後", "今回", "全面", "大規模", "大手", "大型", "言語",
    "画像", "動画", "音声", "人間", "社会", "未来", "時代", "仕組み", "能力", "性能", "精度", "速度", "価格",
    "無料", "有料", "機能", "製品", "事業", "部門", "責任者", "担当者", "関係者", "専門家", "利用者",
}, key=len, reverse=True)
_GENERIC_KANJI_RE = re.compile("|".join(map(re.escape, _GENERIC_KANJI_WORDS)))
_KANJI_NAME_RE = re.compile(r"[一-鿿々]{3,}")


def _kanji_names(text: str) -> set[str]:
    """漢字だけの固有名の候補（国防総省・経済産業省・東京大学など）. 一般語を除いた残りが3字以上のもの."""
    names = set()
    for run in _KANJI_NAME_RE.findall(text):
        for piece in _GENERIC_KANJI_RE.split(run):
            if len(piece) >= 3:
                names.add(piece)
    return names


def _name_tokens(text: str) -> set[str]:
    """固有名らしい語（3文字以上の英数字の語・カタカナ語・漢字語。数字を含む語は2文字でも可）."""
    t = (text or "").lower()
    names = {w for w in _ASCII_WORD_RE.findall(t) if len(w) >= 3 or any(c.isdigit() for c in w)}
    names |= {w for w in _KATAKANA_RE.findall(t) if len(w) >= 3}
    names |= _kanji_names(t)
    return {w for w in names if w not in _GENERIC_NAME_TOKENS}


def shares_entity(a: dict, b: dict) -> bool:
    """2つの記事の題名（日本語・英語）に共通の固有名（会社・製品名など）があるか."""
    na = _name_tokens(f"{a.get('title', '')} {a.get('title_ja', '')}")
    nb = _name_tokens(f"{b.get('title', '')} {b.get('title_ja', '')}")
    return bool(na & nb)


def is_similar_title(a: dict, b: dict, threshold: float = 0.5, min_shared: int = 3) -> bool:
    """日本語題名どうし・英語題名どうしのどちらかが十分似ていれば同じ話とみなす.

    LLM 判定が失敗したときの控えめな代替（誤って別の話を消さないよう閾値は高め）。
    """
    for key in ("title_ja", "title"):
        x, y = a.get(key, ""), b.get(key, "")
        if x and y:
            score, shared = title_similarity(x, y)
            if score >= threshold and shared >= min_shared:
                return True
    return False


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

_NEWS_FILE_RE = re.compile(r"^news_(\d{8})(?:_(.+))?\.json$")
_TOPIC_KIND_RE = re.compile(r"^shorts_topic(\d+)$")
# YouTube 題名からハッシュタグを除いて、題名だけの行の比較材料にする
_HASHTAG_RE = re.compile(r"\s*#\S+")


def _ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_NAME


def load_ledger(state_dir: Path) -> list[dict]:
    path = _ledger_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("entries is not a list")
        # 手で編集した行などで形が崩れていても、使える行だけで続ける
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("date"), str)]
    except (OSError, ValueError):
        # 壊れた台帳は捨てずに退避し、news JSON / upload_log から作り直す
        backup = path.with_suffix(".broken.json")
        try:
            path.replace(backup)
            logger.warning("seen_topics.json is unreadable — moved to %s and rebuilding", backup.name)
        except OSError:
            logger.warning("seen_topics.json is unreadable — rebuilding in memory", exc_info=True)
        return []


def save_ledger(state_dir: Path, entries: list[dict]) -> None:
    path = _ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def _entry_from_article(article: dict, date_str: str, topic: str, rank: int) -> dict:
    def text(key: str) -> str:  # 手編集などで null・数値が入っていても落ちないように
        value = article.get(key)
        return value if isinstance(value, str) else ""

    return {
        "key": url_key(text("link")),
        "url": text("link"),
        "title": text("title"),
        "title_ja": text("title_ja"),
        "summary_ja": text("summary_ja"),
        "date": date_str,
        "topic": topic or "AI",
        "rank": rank,
        "uploaded": False,
    }


def _merge_news_file(entries: list[dict], path: Path) -> bool:
    m = _NEWS_FILE_RE.match(path.name)
    if not m:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ledger: failed to read %s", path)
        return False
    if not isinstance(data, dict) or not isinstance(data.get("articles", []), list):
        logger.warning("Ledger: unexpected shape in %s — skipped", path)
        return False
    date_str = str(data.get("date") or m.group(1))
    topic = str(data.get("topic") or "AI")
    changed = False
    for rank, a in enumerate(data.get("articles", []), 1):
        if not isinstance(a, dict):
            continue
        new = _entry_from_article(a, date_str, topic, rank)
        same = next((e for e in entries if e["date"] == date_str and (
            (new["key"] and e.get("key") == new["key"])
            or (not new["key"] and e.get("title") == new["title"]))), None)
        if same:
            continue
        # upload_log だけから作った題名だけの行（同じ日・同じ順位）があれば中身を埋める
        placeholder = next((e for e in entries if e["date"] == date_str and not e.get("key")
                            and e.get("rank") == rank and e.get("from") == "upload_log"), None)
        if placeholder:
            new["uploaded"] = placeholder.get("uploaded", False)
            placeholder.clear()
            placeholder.update(new)
        else:
            entries.append(new)
        changed = True
    return changed


def _merge_upload_log(entries: list[dict], state_dir: Path) -> bool:
    log_path = Path(state_dir) / "upload_log.json"
    if not log_path.exists():
        return False
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ledger: upload_log.json is unreadable — skipping upload marks")
        return False
    changed = False
    for item in log if isinstance(log, list) else []:
        if not isinstance(item, dict):
            continue
        date_str, kind = str(item.get("date", "")), str(item.get("kind", ""))
        if not date_str:
            continue
        m = _TOPIC_KIND_RE.match(kind)
        if m:
            rank = int(m.group(1))
            targets = [e for e in entries if e["date"] == date_str and e.get("rank") == rank]
            if not targets:
                title = _HASHTAG_RE.sub("", str(item.get("title") or "")).strip()
                entries.append({"key": "", "url": "", "title": "", "title_ja": title,
                                "summary_ja": "", "date": date_str, "topic": "AI",
                                "rank": rank, "uploaded": True, "from": "upload_log"})
                changed = True
                continue
        else:
            # まとめ動画（daily / shorts）はその日の選定記事を全部扱ったものとみなす
            targets = [e for e in entries if e["date"] == date_str]
        for e in targets:
            if not e.get("uploaded"):
                e["uploaded"] = True
                changed = True
    return changed


def sync_ledger(state_dir: Path) -> list[dict]:
    """台帳を読み、news_*.json（state と state/old）と upload_log.json から取り込み直して返す."""
    state_dir = Path(state_dir)
    entries = load_ledger(state_dir)
    changed = not _ledger_path(state_dir).exists()
    for folder in (state_dir, state_dir / "old"):
        if folder.is_dir():
            for path in sorted(folder.glob("news_*.json")):
                changed |= _merge_news_file(entries, path)
    changed |= _merge_upload_log(entries, state_dir)
    if changed:
        save_ledger(state_dir, entries)
    return entries


def record_selection(state_dir: Path, date_str: str, topic: str, articles: list[dict]) -> None:
    """今日の選定記事を台帳に書く（同じ日・同じトピックの未投稿の行は今回の選定で置き換える）."""
    topic = topic or "AI"
    entries = sync_ledger(state_dir)
    entries = [e for e in entries
               if not (e["date"] == date_str and e.get("topic", "AI") == topic and not e.get("uploaded"))]
    for rank, a in enumerate(articles, 1):
        entries.append(_entry_from_article(a, date_str, topic, rank))
    save_ledger(state_dir, entries)


def recent_entries(entries: list[dict], current_date: str, lookback_days: int) -> list[dict]:
    """current_date の前日から lookback_days 日前までの行（当日は含めない＝作り直しを妨げない）."""
    try:
        current_dt = datetime.strptime(current_date, "%Y%m%d")
    except ValueError:
        return []
    oldest = (current_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    return [e for e in entries if oldest <= e.get("date", "") < current_date]
