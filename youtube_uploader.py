"""Phase 5: YouTube投稿モジュール.

YouTube Data API v3を使って動画をアップロードする。
OAuth 2.0認証（初回ブラウザ認証、以後トークン再利用）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    # 字幕アップロード（captions.insert）に必須
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"


def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_authenticated_service(
    client_secrets_file: str,
    token_file: str,
):
    """Build YouTube API service with OAuth 2.0 credentials."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(token_file)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
        # 既存トークンに必要スコープが不足している場合は再認証を促す
        # （字幕API用のforce-ssl追加時など。ブラウザが一度開く）
        if creds and creds.scopes and not set(YOUTUBE_SCOPES).issubset(set(creds.scopes)):
            logger.info("Existing token lacks required scopes — re-authorization needed (browser will open)")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                client_secrets_file,
                YOUTUBE_SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds)


def _short_title_label(title: str, max_chars: int = 18) -> str:
    """記事タイトルからタイトル用の短い見出しを作る（切断されたルビ括弧は除去）."""
    label = (title or "").strip()
    if len(label) > max_chars:
        cut = label[:max_chars]
        # 英単語の途中で切れた場合は単語境界まで戻す（"GPT-ほか4本" のような切断を防ぐ）
        if (re.match(r"[A-Za-z0-9\-\.]", label[max_chars])
                and re.search(r"[A-Za-z0-9\-\.]$", cut)):
            trimmed = re.sub(r"[A-Za-z0-9\-\.]+$", "", cut)
            if trimmed:
                cut = trimmed
        label = re.sub(r"[(（][^)）]*$", "", cut)
    return label.rstrip("、。・:： ")


def _voicevox_credit(config: dict) -> str:
    """VOICEVOX利用規約に基づくクレジット表記（config.tts.credit で上書き可）."""
    return config.get("tts", {}).get("credit", "VOICEVOX:冥鳴ひまり")


# 実際に検索ボリュームのある製品・企業名（正規表記へのマップ）。
# タグは小文字キーで照合し、ハッシュタグ・tagsには正規表記を使う。
# 根拠: 伸びている競合Shortsのタイトルはエンティティ名ハッシュタグ
# （#OpenAI #ChatGPT #NVIDIA等）を付けており、視聴者は製品名で検索する。
_KNOWN_ENTITIES = {
    "chatgpt": "ChatGPT", "openai": "OpenAI", "claude": "Claude", "anthropic": "Anthropic",
    "gemini": "Gemini", "google": "Google", "copilot": "Copilot", "microsoft": "Microsoft",
    "apple": "Apple", "siri": "Siri", "iphone": "iPhone", "ios": "iOS",
    "nvidia": "NVIDIA", "meta": "Meta", "instagram": "Instagram", "facebook": "Facebook",
    "spotify": "Spotify", "tesla": "Tesla", "grok": "Grok", "xai": "xAI",
    "amazon": "Amazon", "alexa": "Alexa", "sora": "Sora", "midjourney": "Midjourney",
    "deepseek": "DeepSeek", "llama": "Llama", "mistral": "Mistral", "perplexity": "Perplexity",
    "notebooklm": "NotebookLM", "youtube": "YouTube", "tiktok": "TikTok", "line": "LINE",
    "windows": "Windows", "android": "Android", "pixel": "Pixel", "galaxy": "Galaxy",
    "netflix": "Netflix", "adobe": "Adobe", "canva": "Canva", "slack": "Slack",
    "github": "GitHub", "cursor": "Cursor", "notion": "Notion", "zoom": "Zoom",
    "sony": "Sony", "toyota": "トヨタ", "softbank": "ソフトバンク", "rakuten": "楽天",
    "samsung": "サムスン", "intel": "Intel", "amd": "AMD", "qualcomm": "Qualcomm",
    "faceid": "FaceID", "whisper": "Whisper", "genspark": "Genspark",
    "qwen": "Qwen", "minimax": "MiniMax", "gemma": "Gemma", "veo": "Veo",
    "suno": "Suno", "huggingface": "HuggingFace", "groq": "Groq", "manus": "Manus",
}

# 日本語のジャンル語（タイトル・要約に含まれていたらtagsに追加）
_JA_TOPIC_TAGS = [
    "生成AI", "画像生成", "動画生成", "AIエージェント", "自動運転", "ロボット",
    "スマートスピーカー", "音声認識", "半導体", "セキュリティ", "医療", "教育",
]

_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\.\-]*")

# 一般英単語と衝突する曖昧キー。ブランド表記（大文字を含むトークン）のときだけ
# 採用する（"the notion of AI" のnotionや "meta tags" のmetaを拾わない）。
_AMBIGUOUS_ENTITY_KEYS = {
    "notion", "meta", "slack", "cursor", "zoom", "pixel", "apple",
    "sora", "grok", "windows", "galaxy", "whisper",
}


def extract_entity_keywords(*texts: str, limit: int = 4) -> list[str]:
    """記事テキストから検索されやすいエンティティ（製品・企業名）を抽出する.

    出現順を保ち、既知エンティティのみ正規表記で返す。GPT-5.6等のモデル名は
    「GPT」に正規化する（ChatGPTは専用エントリが先に一致する）。
    一般英単語と衝突する曖昧キーはブランド表記（大文字入り）のみ採用し、
    LINEは常に全大文字表記のみ採用する。
    """
    joined = " ".join(t for t in texts if t)
    found: list[str] = []
    for token in _ENTITY_TOKEN_RE.findall(joined):
        bare = token.rstrip(".-")
        key = bare.lower()
        # LINE（メッセージアプリ）はブランド表記が常に全大文字
        if key == "line" and bare != "LINE":
            continue
        # 曖昧キーは大文字を含むブランド表記のみ（一般英単語の誤マッチ防止）
        if key in _AMBIGUOUS_ENTITY_KEYS and bare == key:
            continue
        name = _KNOWN_ENTITIES.get(key)
        if not name:
            # バージョン付き表記（Qwen3.8 / Llama4 / Gemma3 等）は末尾の数字を落として照合
            base = key.rstrip("0123456789.-")
            if base != key and base not in _AMBIGUOUS_ENTITY_KEYS:
                name = _KNOWN_ENTITIES.get(base)
        if not name and key.startswith("gpt"):
            name = "GPT"
        if name and name not in found:
            found.append(name)
            if len(found) >= limit:
                break
    return found


def _ja_topic_tags(*texts: str, limit: int = 2) -> list[str]:
    """日本語ジャンル語（生成AI・ロボット等）をテキストから拾ってtags用に返す."""
    joined = " ".join(t for t in texts if t)
    return [t for t in _JA_TOPIC_TAGS if t in joined][:limit]


def generate_metadata(
    date_str: str,
    config: dict,
    script_type: str = "daily",
    news_data: dict | None = None,
    topic: str = "",
) -> dict:
    """Generate video title, description, and tags."""
    channel = config.get("channel", {})
    yt_cfg = config.get("youtube", {})
    topic_label = topic if topic else "AI"

    if script_type == "daily":
        # SEO: トップ記事の内容をタイトルに含め、ハッシュタグは説明文側へ移す
        date_part = f"{int(date_str[4:6])}/{int(date_str[6:])}"
        top_label = ""
        article_count = 0
        if news_data and news_data.get("articles"):
            articles = news_data["articles"]
            article_count = len(articles)
            top_label = _short_title_label(articles[0].get("title_ja", ""))
        if top_label:
            others = f"ほか{article_count - 1}本" if article_count > 1 else ""
            title = f"【{topic_label}ニュース】{date_part} {top_label}{others}｜{channel.get('name', 'ポンテのAI教室')}"
        else:
            title = f"【{topic_label}ニュース】{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}の{topic_label}ニュース｜{channel.get('name', 'ポンテのAI教室')}"
        description = (
            f"🤖 {channel.get('name', 'ポンテのAI教室')}のデイリー{topic_label}ニュース！\n\n"
            f"今日の{topic_label}ニュースをポンテがわかりやすく紹介するよ！\n\n"
            f"📌 {channel.get('tagline', 'むずかしいAIを、かんたんに。')}\n\n"
        )
    else:
        title = f"【週間{topic_label}まとめ】今週の{topic_label}ニュースまとめ｜{channel.get('name', 'ポンテのAI教室')} #AI #AIニュース #初心者 #ポンテのAIニュース"
        description = (
            f"🤖 {channel.get('name', 'ポンテのAI教室')}のウィークリーまとめ！\n\n"
            f"今週の{topic_label}ニュースをポンテがまとめて紹介するよ！\n\n"
            f"📌 {channel.get('tagline', 'むずかしいAIを、かんたんに。')}\n\n"
        )

    # Add news source URLs if available
    if news_data and news_data.get("articles"):
        description += "📰 今日のニュース\n"
        for i, article in enumerate(news_data["articles"], 1):
            article_title = article.get("title_ja", article.get("title", ""))
            link = article.get("link", "")
            if link:
                description += f"{i}. {article_title}\n   {link}\n"
            else:
                description += f"{i}. {article_title}\n"
        description += "\n"

    description += (
        "🔔 チャンネル登録よろしくね！\n\n"
        "運営： https://surc.online/\n"
        f"音声: {_voicevox_credit(config)}\n\n"
        "#AI #AIニュース #初心者"
    )

    tags = ["AI", "人工知能", "AIニュース", "生成AI", "AI活用", "AI入門", "初心者", "ポンテ", "AI教室"]
    if news_data and news_data.get("articles"):
        # 記事中のエンティティ（製品・企業名）を先頭に追加（検索クエリ一致用）
        entities = extract_entity_keywords(
            *[a.get("title_ja", a.get("title", "")) for a in news_data["articles"]], limit=5)
        tags = list(dict.fromkeys(entities + tags))[:15]
    if topic and topic != "AI":
        tags = list(dict.fromkeys([topic, f"{topic}ニュース"] + tags))[:15]

    playlist = f"今日の{topic_label}ニュース（初心者向け）"

    return {
        "title": title[:100],
        "description": description,
        "tags": tags,
        "category_id": yt_cfg.get("default_category_id", "28"),
        "privacy": yt_cfg.get("initial_privacy", "private"),
        "playlist": playlist,
        "date": date_str,
        "kind": script_type,
    }


def generate_shorts_metadata(
    date_str: str,
    config: dict,
    news_data: dict | None = None,
    topic: str = "",
) -> dict:
    """Generate metadata for YouTube Shorts upload."""
    channel = config.get("channel", {})
    yt_cfg = config.get("youtube", {})
    topic_label = topic if topic else "AI"

    title = f"今日の{topic_label}ニュース {date_str[4:6]}/{date_str[6:]}｜{channel.get('name', 'ポンテのAI教室')} #AIニュース #shorts"

    description = (
        f"🤖 {channel.get('name', 'ポンテのAI教室')}のデイリー{topic_label}ニュース（ショート版）\n\n"
        f"📌 {channel.get('tagline', 'むずかしいAIを、かんたんに。')}\n\n"
    )

    if news_data and news_data.get("articles"):
        description += "📰 今日のニュース\n"
        for i, article in enumerate(news_data["articles"], 1):
            article_title = article.get("title_ja", article.get("title", ""))
            link = article.get("link", "")
            if link:
                description += f"{i}. {article_title}\n   {link}\n"
            else:
                description += f"{i}. {article_title}\n"
        description += "\n"

    description += (
        "🔔 チャンネル登録よろしくね！\n\n"
        "運営： https://surc.online/\n"
        f"音声: {_voicevox_credit(config)}"
    )

    shorts_tags = ["shorts", "AI", "人工知能", "AIニュース", "生成AI", "AI活用", "AI入門", "初心者", "ポンテ", "AI教室"]
    if news_data and news_data.get("articles"):
        entities = extract_entity_keywords(
            *[a.get("title_ja", a.get("title", "")) for a in news_data["articles"]], limit=5)
        shorts_tags = list(dict.fromkeys(entities + shorts_tags))[:15]
    if topic and topic != "AI":
        shorts_tags = list(dict.fromkeys([topic, f"{topic}ニュース"] + shorts_tags))[:15]

    playlist = f"今日の{topic_label}ニュース（初心者向け）"

    return {
        "title": title[:100],
        "description": description,
        "tags": shorts_tags,
        "category_id": yt_cfg.get("default_category_id", "28"),
        "privacy": yt_cfg.get("initial_privacy", "private"),
        "playlist": playlist,
        "date": date_str,
        "kind": "shorts",
    }


# 読みがなのカッコ書き（script_generator.strip_reading_parens と同じ規則。
# こちらは形態素解析等の重い依存を持ち込まないよう正規表現だけ持つ）
_READING_PARENS_RE = re.compile(r"[(（][ぁ-んァ-ヶー・ ]+[)）]")


def generate_topic_shorts_metadata(
    date_str: str,
    config: dict,
    article: dict,
    hook_title: str,
    topic_index: int,
    topic: str = "",
) -> dict:
    """「1ニュース=1本」Shorts 1本分のメタデータを生成する.

    Args:
        article: news JSON の記事1件（title_ja / summary_ja / link 等）。
        hook_title: script_meta 由来のフック見出し（動画タイトルの主役）。
        topic_index: 1始まりのトピック番号（upload_log の識別用）。
    """
    channel = config.get("channel", {})
    yt_cfg = config.get("youtube", {})
    topic_label = topic if topic else "AI"

    # 読みがなのカッコ書き（Claude(クロード)等）は題名・概要欄に出さない（二重の防止）
    hook = _READING_PARENS_RE.sub("", hook_title or "").strip() or _short_title_label(
        article.get("title_ja", article.get("title", "")), max_chars=24)

    # 記事から検索されやすいエンティティ（製品・企業名）を抽出
    entities = extract_entity_keywords(
        hook,
        article.get("title_ja", article.get("title", "")),
        article.get("summary_ja_long") or article.get("summary_ja", ""),
    )

    # 題名のハッシュタグ: 固定4つ＋関連エンティティ1つ（あれば）: #ChatGPT #OpenAI 等
    title_hashtags = ["ai", "news", "shorts", "short"] + ([entities[0]] if entities else [])
    title = f"{hook} " + " ".join(f"#{h}" for h in title_hashtags)

    summary = article.get("summary_ja_long") or article.get("summary_ja", "")
    if len(summary) > 280:
        cut = summary[:280]
        # 文の途中で切らない（範囲内の最後の句点まで）
        if "。" in cut:
            cut = cut[: cut.rindex("。") + 1]
        summary = cut
    description = f"🤖 {hook}\n\n"
    if summary:
        description += f"{summary}\n\n"
    link = article.get("link", "")
    if link:
        description += f"📰 元記事: {link}\n\n"
    description += (
        f"📌 {channel.get('tagline', 'むずかしいAIを、かんたんに。')}\n"
        "🔔 チャンネル登録よろしくね！\n\n"
        "運営： https://surc.online/"
    )

    # tags: エンティティ＋日本語ジャンル語を先頭に（検索クエリに一致しやすい順）
    base_tags = ["shorts", "AI", "人工知能", "AIニュース", "生成AI", "AI活用", "AI入門", "初心者", "ポンテ", "AI教室"]
    ja_topics = _ja_topic_tags(
        article.get("title_ja", ""), article.get("summary_ja_long") or article.get("summary_ja", ""))
    tags = list(dict.fromkeys(entities + ja_topics + base_tags))[:15]
    if topic and topic != "AI":
        tags = list(dict.fromkeys([topic, f"{topic}ニュース"] + tags))[:15]

    return {
        "title": title[:100],
        "description": description,
        "tags": tags,
        "category_id": yt_cfg.get("default_category_id", "28"),
        "privacy": yt_cfg.get("initial_privacy", "private"),
        "playlist": f"今日の{topic_label}ニュース（初心者向け）",
        "date": date_str,
        "kind": f"shorts_topic{topic_index}",
    }


def upload_video(
    video_path: Path,
    metadata: dict,
    config_path: Path = Path("config.json"),
) -> str:
    """Upload video to YouTube. Returns video URL."""
    from googleapiclient.http import MediaFileUpload

    config = load_config(config_path)
    yt_cfg = config.get("youtube", {})

    service = get_authenticated_service(
        yt_cfg["client_secrets_file"],
        yt_cfg["token_file"],
    )

    # recording date from metadata (YYYYMMDD → YYYY-MM-DD)
    date_str = metadata.get("date", "")
    recording_date = ""
    if len(date_str) == 8:
        recording_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T00:00:00Z"

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["category_id"],
            "defaultLanguage": metadata.get("language", "ja"),
            "defaultAudioLanguage": metadata.get("language", "ja"),
        },
        "status": {
            "privacyStatus": metadata.get("privacy", "private"),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
        },
        "recordingDetails": {
            "recordingDate": recording_date,
        },
    }

    # アップロード本体（5xx・接続エラーのみ最大3回リトライ。4xxは即時失敗）
    from googleapiclient.errors import HttpError

    waits = (10, 30, 60)
    response = None
    for attempt in range(3):
        try:
            media = MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=10 * 1024 * 1024,  # 10MB chunks
            )
            request = service.videos().insert(
                part="snippet,status,recordingDetails",
                body=body,
                media_body=media,
            )

            logger.info("Uploading: %s%s", video_path,
                        f" (attempt {attempt + 1})" if attempt else "")
            response = None
            while response is None:
                # next_chunk 自体もチャンク単位で指数バックオフリトライする
                status, response = request.next_chunk(num_retries=5)
                if status:
                    logger.info("Upload progress: %d%%", int(status.progress() * 100))
            break
        except (HttpError, OSError, ConnectionError) as e:
            if isinstance(e, HttpError) and e.resp is not None and int(e.resp.status) < 500:
                raise  # 4xx はリトライしても無駄（認証・クォータ等）
            if attempt >= 2:
                raise
            logger.warning("Upload attempt %d failed (%s) — retrying in %ds",
                           attempt + 1, e, waits[attempt])
            time.sleep(waits[attempt])

    video_id = response["id"]
    video_url = f"https://youtu.be/{video_id}"
    logger.info("Upload complete: %s", video_url)

    # カスタムサムネイル（metadata["thumbnail"] にパスがある場合のみ。
    # 失敗してもアップロード自体は成功扱い＝チャンネル未認証等のエラー対策）
    thumbnail_set = False
    thumb_path = metadata.get("thumbnail", "")
    if thumb_path and Path(thumb_path).exists():
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
            ).execute()
            thumbnail_set = True
            logger.info("Custom thumbnail set: %s", thumb_path)
        except Exception as e:
            logger.warning("Failed to set custom thumbnail (upload still OK): %s", e)

    # 字幕（SRT）の自動添付（metadata["caption_file"] にパスがある場合のみ。
    # 失敗してもアップロード自体は成功扱い）
    caption_set = False
    caption_file = metadata.get("caption_file", "")
    if caption_file and Path(caption_file).exists():
        try:
            upload_caption(service, video_id, Path(caption_file),
                           language=metadata.get("language", "ja"))
            caption_set = True
            logger.info("Caption uploaded: %s", caption_file)
        except Exception as e:
            logger.warning("Failed to upload caption (video upload still OK): %s", e)

    # Add to playlist if specified
    playlist_name = metadata.get("playlist")
    if playlist_name:
        _add_to_playlist(service, video_id, playlist_name)

    # アップロード結果を永続ログへ追記
    try:
        state_dir = Path(config["paths"]["state_dir"])
        _append_upload_log(state_dir, {
            "date": metadata.get("date", ""),
            "kind": metadata.get("kind", ""),
            "video_id": video_id,
            "url": video_url,
            "title": metadata.get("title", ""),
            "privacy": metadata.get("privacy", ""),
            "playlist": playlist_name or "",
            "thumbnail_set": thumbnail_set,
            "caption_set": caption_set,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Failed to write upload log: %s", e)

    return video_url


def upload_caption(service, video_id: str, caption_path: Path, language: str = "ja") -> None:
    """SRT字幕トラックを動画に添付する（captions.insert、要force-sslスコープ）."""
    from googleapiclient.http import MediaFileUpload

    service.captions().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": "",
                "isDraft": False,
            }
        },
        media_body=MediaFileUpload(str(caption_path), mimetype="application/octet-stream"),
    ).execute()


def _append_upload_log(state_dir: Path, entry: dict) -> None:
    """state/upload_log.json にアップロード結果を追記する（破損時は新規作成）."""
    log_path = state_dir / "upload_log.json"
    records: list = []
    if log_path.exists():
        try:
            records = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                records = []
        except Exception:
            records = []
    records.append(entry)
    log_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Upload log appended: %s", log_path)


def _find_or_create_playlist(service, title: str) -> str:
    """Find a playlist by title or create it. Returns playlist ID."""
    # Search existing playlists
    request = service.playlists().list(part="snippet", mine=True, maxResults=50)
    response = request.execute()
    for item in response.get("items", []):
        if item["snippet"]["title"] == title:
            logger.info("Found playlist: %s (%s)", title, item["id"])
            return item["id"]

    # Create new playlist
    body = {
        "snippet": {
            "title": title,
            "description": f"{title}の動画一覧",
        },
        "status": {"privacyStatus": "public"},
    }
    response = service.playlists().insert(part="snippet,status", body=body).execute()
    playlist_id = response["id"]
    logger.info("Created playlist: %s (%s)", title, playlist_id)
    return playlist_id


def _add_to_playlist(service, video_id: str, playlist_name: str) -> None:
    """Add a video to a playlist by name."""
    try:
        playlist_id = _find_or_create_playlist(service, playlist_name)
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            },
        }
        service.playlistItems().insert(part="snippet", body=body).execute()
        logger.info("Added video %s to playlist '%s'", video_id, playlist_name)
    except Exception as e:
        logger.warning("Failed to add to playlist '%s': %s", playlist_name, e)


def _news_json_path(state_dir: Path, date_str: str, topic: str = "") -> Path:
    """news JSON の場所（カスタムトピックは news_{date}_{slug}.json）."""
    from news_collector import news_json_path
    return news_json_path(state_dir, date_str, topic)


def upload_topic_shorts(
    date_str: str,
    config_path: Path = Path("config.json"),
    topic: str = "",
) -> list[str]:
    """「1ニュース=1本」Shorts（{date}_shorts_topic*.mp4）を全件アップロードする.

    タイトルは script_meta_{date}.json の hook_title（LLM生成のフック見出し）を
    使い、欠落時は news JSON の記事タイトルから決定的に生成する。

    Returns:
        アップロードした動画URLのリスト（トピック順）。
    """
    config = load_config(config_path)
    output_dir = Path(config["paths"]["output_dir"])
    state_dir = Path(config["paths"]["state_dir"])

    # news JSON（記事情報）と script_meta（hook_title）を読み込む
    news_articles: list[dict] = []
    news_path = _news_json_path(state_dir, date_str, topic)
    if news_path.exists():
        with open(news_path, encoding="utf-8") as f:
            news_articles = json.load(f).get("articles", [])

    meta_articles: list[dict] = []
    meta_path = state_dir / f"script_meta_{date_str}.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta_articles = json.load(f).get("articles", [])
        except Exception as e:
            logger.warning("Failed to read script_meta (hook titles unavailable): %s", e)

    video_paths = sorted(
        output_dir.glob(f"{date_str}_shorts_topic*.mp4"),
        key=lambda p: int(re.search(r"topic(\d+)", p.stem).group(1)),
    )
    if not video_paths:
        raise FileNotFoundError(
            f"No topic shorts found: {output_dir / (date_str + '_shorts_topic*.mp4')}")

    # 残骸ファイル防衛: topics_per_day を超える本数（過去実行の残り等）は
    # 誤アップロードしない
    top_n = config.get("shorts", {}).get("topics_per_day", 3)
    if len(video_paths) > top_n:
        logger.warning("Found %d topic videos but topics_per_day=%d — uploading only the first %d",
                       len(video_paths), top_n, top_n)
        video_paths = video_paths[:top_n]

    urls: list[str] = []
    for video_path in video_paths:
        i = int(re.search(r"topic(\d+)", video_path.stem).group(1))
        article = news_articles[i - 1] if i - 1 < len(news_articles) else {}
        hook = meta_articles[i - 1].get("hook_title", "") if i - 1 < len(meta_articles) else ""
        metadata = generate_topic_shorts_metadata(
            date_str, config, article, hook, i, topic=topic)
        # 字幕（SRT）: video_builder.generate_topic_captions が生成していれば添付
        srt_path = state_dir / "captions" / f"{date_str}_topic{i}.srt"
        if srt_path.exists():
            metadata["caption_file"] = str(srt_path)
        else:
            logger.info("No caption file for topic %d (skipping): %s", i, srt_path)
        logger.info("Uploading topic short %d/%d: %s", i, len(video_paths), metadata["title"])
        urls.append(upload_video(video_path, metadata, config_path))
    return urls


def upload(
    date_str: str,
    config_path: Path = Path("config.json"),
    script_type: str = "daily",
    shorts: bool = False,
    topic: str = "",
) -> str:
    """Full upload flow. Returns video URL."""
    config = load_config(config_path)
    output_dir = Path(config["paths"]["output_dir"])
    state_dir = Path(config["paths"]["state_dir"])

    if shorts:
        video_path = output_dir / f"{date_str}_shorts.mp4"
    else:
        video_path = output_dir / f"{date_str}_{script_type}.mp4"

    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    # Load news data for URL inclusion in description
    news_data = None
    news_path = _news_json_path(state_dir, date_str, topic)
    if news_path.exists():
        with open(news_path, encoding="utf-8") as f:
            news_data = json.load(f)

    if shorts:
        metadata = generate_shorts_metadata(date_str, config, news_data=news_data, topic=topic)
    else:
        metadata = generate_metadata(date_str, config, script_type, news_data=news_data, topic=topic)
        # カスタムサムネイル（通常版のみ。Shortsはフィードでサムネ表示されないためスキップ）
        if config.get("youtube", {}).get("custom_thumbnail", False):
            thumb_path = state_dir / f"thumbnail_{date_str}.jpg"
            if thumb_path.exists():
                metadata["thumbnail"] = str(thumb_path)
            else:
                logger.info("Custom thumbnail not found (skipping): %s", thumb_path)
    return upload_video(video_path, metadata, config_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="YouTube Uploader")
    parser.add_argument("--date", required=True, help="Date string YYYYMMDD")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--type", default="daily", choices=["daily", "weekly"])
    parser.add_argument("--shorts", action="store_true", help="Upload as YouTube Shorts")
    parser.add_argument("--topic-shorts", action="store_true",
                        help="Upload all per-topic Shorts videos (1 news = 1 video)")
    args = parser.parse_args()
    if args.topic_shorts:
        urls = upload_topic_shorts(args.date, Path(args.config))
        for u in urls:
            print(f"Uploaded: {u}")
    else:
        url = upload(args.date, Path(args.config), args.type, args.shorts)
        print(f"Uploaded: {url}")


if __name__ == "__main__":
    main()
