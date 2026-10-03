"""Phase 3: スライド画像生成モジュール.

台本MarkdownをパースしてJinja2 HTMLテンプレートでスライドを生成し、
Playwrightで1920x1080のPNG画像にレンダリングする。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import base64

import jinja2

logger = logging.getLogger(__name__)

# Expression image names and mapping
EXPRESSIONS = ["normal", "surprised", "thinking", "clumsy"]

_WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
_WEEKDAY_CLASSES = ["day-mon", "day-tue", "day-wed", "day-thu", "day-fri", "day-sat", "day-sun"]

# ルビ記法:
#   漢字(ふりがな) → <ruby>漢字<rt>ふりがな</rt></ruby>
#   漢字+送り仮名(読み) → <ruby>漢字+送り仮名<rt>読み</rt></ruby>
#   English(カタカナ/ひらがな) → <ruby>English<rt>読み</rt></ruby>
_RUBY_RE = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf々]+[ぁ-ん]*)[(\uff08]([ぁ-んァ-ヶー]+)[)\uff09]"
    r"|"
    r"([A-Za-z0-9][A-Za-z0-9_\s\-\.]*[A-Za-z0-9]|[A-Za-z0-9])[(\uff08]([ぁ-んァ-ヶー]+)[)\uff09]"
)

# キーワード箇条書き: - **term**: desc
_KEYWORD_ITEM_RE = re.compile(r"^-\s+\*\*(.+?)\*\*:\s*(.+)$", re.MULTILINE)


def format_date_display(date_str: str) -> str:
    """Convert YYYYMMDD to '2026年5月12日（月）' format."""
    from datetime import datetime
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = _WEEKDAYS_JA[dt.weekday()]
    return f"{dt.year}年{dt.month}月{dt.day}日（{weekday}）"


def get_day_class(date_str: str) -> str:
    """Return CSS class for the day of the week (e.g. 'day-mon')."""
    from datetime import datetime
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return _WEEKDAY_CLASSES[dt.weekday()]
    except (ValueError, KeyError):
        return "day-mon"


def is_friday(date_str: str) -> bool:
    """Check if the given YYYYMMDD date is a Friday."""
    from datetime import datetime
    try:
        return datetime.strptime(date_str, "%Y%m%d").weekday() == 4
    except (ValueError, KeyError):
        return False


def apply_ruby(text: str) -> str:
    """Convert ruby notation to HTML <ruby> tags.

    Supports:
      漢字(ふりがな) → <ruby>漢字<rt>ふりがな</rt></ruby>
      English(カタカナ) → <ruby>English<rt>カタカナ</rt></ruby>
    """
    def _replace(m):
        if m.group(1):  # kanji + hiragana
            return f"<ruby>{m.group(1)}<rt>{m.group(2)}</rt></ruby>"
        else:  # English + katakana
            return f"<ruby>{m.group(3)}<rt>{m.group(4)}</rt></ruby>"
    return _RUBY_RE.sub(_replace, text)


_RUBY_EXTRACT_RE = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf々]+[ぁ-ん]*)[(\uff08]([ぁ-んァ-ヶー]+)[)\uff09]"
    r"|"
    r"([A-Za-z0-9][A-Za-z0-9_\s\-\.]*[A-Za-z0-9]|[A-Za-z0-9])[(\uff08]([ぁ-んァ-ヶー]+)[)\uff09]"
)


def _extract_ruby_dict(text: str) -> dict[str, str]:
    """セリフからルビ記法を抽出して {漢字: ふりがな, English: カタカナ} 辞書を返す."""
    ruby_dict: dict[str, str] = {}
    for m in _RUBY_EXTRACT_RE.finditer(text):
        if m.group(1):  # 漢字(ふりがな or カタカナ)
            ruby_dict[m.group(1)] = m.group(2)
        elif m.group(3):  # English(カタカナ)
            ruby_dict[m.group(3)] = m.group(4)
    return ruby_dict


_KANJI_ONE_RE = re.compile(r"[一-鿿㐀-䶿々]")


def _is_kanji_char(ch: str) -> bool:
    return bool(ch) and bool(_KANJI_ONE_RE.match(ch))


def _is_alnum_ascii(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


def _apply_ruby_dict(text: str, ruby_dict: dict[str, str]) -> str:
    """セリフ由来のルビ辞書をタイトルへ転写する（手修正ルビの伝播用）.

    語の境界を尊重し、より長い語の「内側」へ部分マッチさせない:
      - 漢字語は前後が漢字のとき適用しない（＝より大きな熟語の一部）。
        例: 「能力(のうりょく)」を「診断能力」の中に入れて
            診断能力(のうりょく) という壊れたルビを作らない（診断の読みが消える）。
        この場合は転写せず、後段の add_ruby_to_text（形態素解析）に熟語全体を任せる。
      - 英数語は前後が英数字のとき適用しない（＝より大きな英単語の一部）。
        例: 「AI(エーアイ)」を「OpenAI」の中に入れて OpenAI(エーアイ) を作らない。
    既存のルビ注釈と重複する箇所もスキップする。
    """
    for word in sorted(ruby_dict, key=len, reverse=True):
        if not word:
            continue
        protected = [(m.start(), m.end()) for m in _RUBY_EXTRACT_RE.finditer(text)]
        starts_kanji = _is_kanji_char(word[0])
        ends_kanji = _is_kanji_char(word[-1])
        starts_alnum = _is_alnum_ascii(word[0])
        ends_alnum = _is_alnum_ascii(word[-1])

        for m in re.finditer(re.escape(word), text):
            s, e = m.start(), m.end()
            # 既存ルビと重複
            if any(s < pe and e > ps for ps, pe in protected):
                continue
            before = text[s - 1] if s > 0 else ""
            after = text[e] if e < len(text) else ""
            # より大きな熟語の一部（漢字が隣接）なら適用しない
            if (starts_kanji and _is_kanji_char(before)) or (ends_kanji and _is_kanji_char(after)):
                continue
            # より大きな英単語の一部（英数字が隣接）なら適用しない
            if (starts_alnum and _is_alnum_ascii(before)) or (ends_alnum and _is_alnum_ascii(after)):
                continue
            # この出現位置に転写（最初の有効な出現のみ。位置指定で確実に置換）
            text = text[:s] + f"{word}({ruby_dict[word]})" + text[e:]
            break
    return text


def _thumbnail_hook(title_ja: str, limit: int = 22) -> str:
    """サムネイル用の短いフック見出しを記事タイトルから作る.

    長い記事タイトル全文はサムネで読みづらいため、最初の文の切れ目
    （？！。）で切ってパンチのある見出しにする。早い位置に区切りが
    無ければ読点で、それも無ければ limit 文字で省略する。
    """
    t = (title_ja or "").strip()
    if len(t) <= limit:
        return t
    for i, ch in enumerate(t):
        if ch in "？！?!" and 4 <= i <= limit:
            return t[: i + 1]
    for i, ch in enumerate(t):
        if ch in "。、" and 8 <= i <= limit:
            return t[:i]
    return t[:limit].rstrip("、。 ") + "…"


def _parse_keyword_items(body: str) -> list[dict]:
    """Parse markdown bullet list '- **term**: desc' into list of dicts."""
    items = []
    for m in _KEYWORD_ITEM_RE.finditer(body):
        items.append({"term": m.group(1).strip(), "desc": m.group(2).strip()})
    return items


# 911-plan互換の正規表現
_SLIDE_HEADER_RE = re.compile(r"^###\s+スライド(\d+)[：:]\s*(.+)$", re.MULTILINE)
_SERIF_TEXT_RE = re.compile(r"「((?:[^「」]|「[^」]*」)*)」", re.DOTALL)


def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def parse_slides(script_path: Path) -> list[dict]:
    """Parse script markdown into slide data."""
    text = script_path.read_text(encoding="utf-8")

    # Strip narration section if present
    narration_marker = "## 語りパート"
    if narration_marker in text:
        text = text[:text.index(narration_marker)]

    headers = list(_SLIDE_HEADER_RE.finditer(text))
    if not headers:
        raise ValueError(f"No slide headers found in {script_path}")

    slides = []
    for i, match in enumerate(headers):
        number = int(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]

        serif_match = _SERIF_TEXT_RE.search(body)
        serif = serif_match.group(1) if serif_match else ""

        slides.append({
            "number": number,
            "title": title,
            "serif": serif,
            "body": body.strip(),
            "is_opening": "オープニング" in title,
            "is_ending": "エンディング" in title,
            "is_index": "ニュース一覧" in title,
            "is_keyword": "今日のキーワード" in title,
        })

    return slides


def classify_slide(slide: dict, total_slides: int) -> str:
    """Determine slide type for template rendering."""
    if slide["is_opening"]:
        return "opening"
    if slide["is_index"]:
        return "index"
    if slide.get("is_keyword"):
        return "keyword"
    if slide["is_ending"]:
        return "ending"
    return "news"


def _load_ponte_expressions(config: dict) -> dict[str, str]:
    """Load all 4 expression PNGs as base64 data URIs. Returns dict keyed by expression name."""
    assets_dir = Path(config["paths"]["assets_dir"]).resolve()
    ponte_dir = assets_dir / "ponte"
    expressions = {}
    for name in EXPRESSIONS:
        img_path = ponte_dir / f"ponte_{name}.png"
        if img_path.exists():
            img_data = base64.b64encode(img_path.read_bytes()).decode()
            expressions[name] = f"data:image/png;base64,{img_data}"
        else:
            logger.warning("Expression image not found: %s", img_path)
    return expressions


def _get_expression(slide_type: str, news_counter: int) -> str:
    """Determine which expression to use based on slide type and news counter.

    Mapping:
    - opening / index / keyword: normal
    - news (odd counter = intro): surprised
    - news (even counter = explanation): thinking
    - ending: clumsy
    """
    if slide_type == "ending":
        return "clumsy"
    if slide_type == "news":
        return "surprised" if news_counter % 2 == 1 else "thinking"
    return "normal"


def render_slide_html(
    slide: dict,
    slide_type: str,
    template_path: Path,
    config: dict,
    news_number: int = 0,
    index_items: list[str] | None = None,
    keyword_items: list[dict] | None = None,
    bg_variant: str = "",
    ponte_expressions: dict[str, str] | None = None,
    news_counter: int = 0,
    og_image: str = "",
    source_label: str = "",
    news_total: int = 0,
    hook_title: str = "",
    points: list[str] | None = None,
    captions_burned: bool = False,
) -> str:
    """Render a single slide to HTML.

    hook_title / points は script_meta 由来（Shortsの大見出し・図解カード）。
    captions_burned=True のときセリフは動画側で音声同期字幕として焼き込むため、
    テンプレートはセリフ全文を描かず字幕用の空き帯を確保する。
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        autoescape=False,
    )
    template = env.get_template(template_path.name)

    channel = config.get("channel", {})

    # Select expression-specific image if available, else fall back to ponte.png
    expression = _get_expression(slide_type, news_counter)
    if ponte_expressions and expression in ponte_expressions:
        ponte_image = ponte_expressions[expression]
    else:
        assets_dir = Path(config["paths"]["assets_dir"]).resolve()
        ponte_image_path = assets_dir / "ponte" / "ponte.png"
        if ponte_image_path.exists():
            img_data = base64.b64encode(ponte_image_path.read_bytes()).decode()
            ponte_image = f"data:image/png;base64,{img_data}"
        else:
            ponte_image = ""

    date_str = config.get("_date_display", config.get("_date", ""))

    # セリフ中のルビ記法を辞書化 → タイトルの漢字に適用（手修正を優先）
    from script_generator import add_ruby_to_text
    # NOTE: 以前はルビ括弧込みの生テキストを300文字で切っていたため、ルビの多い
    # セリフが「決定(けってい」のように途中で欠けていた。文字数はscript_generator側で
    # 制御済みなので切らない（表示側は text_len に応じたフォント縮小で吸収する）。
    serif_text = slide["serif"] or ""
    ruby_dict = _extract_ruby_dict(serif_text)
    title_with_ruby = _apply_ruby_dict(slide["title"], ruby_dict)
    title_html = apply_ruby(add_ruby_to_text(title_with_ruby))
    text_html = apply_ruby(serif_text)
    # ルビ・記号を除いた表示文字数（テンプレート側の長文フォント縮小判定に使う）
    text_len = len(_RUBY_RE.sub(lambda m: m.group(1) or m.group(3) or "", serif_text))

    # Shorts: フック見出し・要点カード（ルビは形態素解析で自動付与。セリフ由来の手修正ルビを優先）
    hook_title_html = apply_ruby(add_ruby_to_text(_apply_ruby_dict(hook_title, ruby_dict))) if hook_title else ""
    points_html = [apply_ruby(add_ruby_to_text(_apply_ruby_dict(pt, ruby_dict))) for pt in (points or [])]

    # Apply ruby to keyword items
    rendered_keywords = []
    if keyword_items:
        for kw in keyword_items:
            rendered_keywords.append({
                "term": apply_ruby(kw["term"]),
                "desc": apply_ruby(kw["desc"]),
            })

    # 金曜日は「また来週ね！」にする
    ending_message = "また来週(らいしゅう)ね！" if is_friday(config.get("_date", "")) else "また明日ね！"
    ending_message_html = apply_ruby(ending_message)

    # 曜日テーマクラス
    day_class = get_day_class(config.get("_date", ""))

    return template.render(
        slide_type=slide_type,
        title=title_html,
        text=text_html,
        news_number=news_number,
        channel_name=channel.get("name", "ポンテのAI教室"),
        tagline=channel.get("tagline", "むずかしいAIを、かんたんに。"),
        date=date_str,
        ponte_image=ponte_image,
        index_heading="TODAY'S AI NEWS",
        index_items=index_items or [],
        keyword_items=rendered_keywords,
        bg_variant=bg_variant,
        og_image=og_image,
        ending_message=ending_message_html,
        day_class=day_class,
        source_label=source_label,
        news_total=news_total,
        text_len=text_len,
        hook_title=hook_title_html,
        points=points_html,
        captions_burned=captions_burned,
    )


async def render_html_to_png(
    html_content: str,
    output_path: Path,
    width: int = 1920,
    height: int = 1080,
) -> None:
    """Render HTML string to PNG using Playwright."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html_content, wait_until="networkidle")
        await page.screenshot(path=str(output_path), full_page=False)
        await browser.close()


async def generate_slides_async(
    script_path: Path,
    config_path: Path = Path("config.json"),
    date_str: str = "",
    shorts: bool = False,
    template_override: Path | None = None,
    out_subdir: str | None = None,
    news_json_override: Path | None = None,
) -> Path:
    """Generate all slide images from a script. Returns images directory.

    template_override / out_subdir / news_json_override は UI プレビュー用の
    後方互換オプション（未指定時は従来動作）。本番パイプラインからは使わない。
    """
    from playwright.async_api import async_playwright

    config = load_config(config_path)
    state_dir = Path(config["paths"]["state_dir"])
    templates_dir = Path(config["paths"]["templates_dir"])

    if shorts:
        template_path = templates_dir / "slide_shorts.html"
        resolution = config.get("video", {}).get("shorts_resolution", [1080, 1920])
    else:
        template_path = templates_dir / "slide.html"
        resolution = config.get("video", {}).get("resolution", [1920, 1080])

    if template_override is not None:
        template_path = template_override

    if date_str:
        config["_date"] = date_str
    else:
        # Extract from script filename
        stem = script_path.stem
        config["_date"] = stem.replace("script_", "")

    # Formatted date for display (e.g. "2026年5月13日（水）")
    try:
        config["_date_display"] = format_date_display(config["_date"])
    except (ValueError, KeyError):
        config["_date_display"] = config["_date"]

    slides = parse_slides(script_path)
    images_dir = state_dir / (out_subdir or ("images_shorts" if shorts else "images"))
    images_dir.mkdir(parents=True, exist_ok=True)

    # per_topicモードでは旧レンダリングの残骸PNG（採用外のスライド番号）が
    # レビュー時の混乱を招くため、レンダリング前に削除する
    if shorts and config.get("shorts", {}).get("per_topic", False) and out_subdir is None:
        for old_png in images_dir.glob("slide_*.png"):
            old_png.unlink()

    # Shorts: video_builder と同じ選定ロジックを実行し、採用スライドのみレンダリングする。
    # （両者とも同じ parse_slides 結果＋同じ設定から決定的に計算するため一致する）
    # - per_topic モード: 「1ニュース=1本」。トップN記事のユニットのみ
    #   （オープニング・インデックス・キーワード・エンディングなし）
    # - レガシーモード: 尺予算によるダイジェスト選定
    per_topic = False
    if shorts:
        shorts_cfg = config.get("shorts", {})
        if shorts_cfg.get("per_topic", False):
            from video_builder import select_topic_units
            units = select_topic_units(slides, shorts_cfg.get("topics_per_day", 3))
            slides = [s for unit in units for s in unit]
            per_topic = True
        else:
            from video_builder import select_shorts_slides
            max_sec = config.get("video", {}).get("shorts_max_duration_sec", 170)
            slides = select_shorts_slides(slides, max_sec)

    # Load expression images once
    ponte_expressions = _load_ponte_expressions(config)

    # Load OG images + 出典ドメイン from news JSON (keyed by 1-based article index)
    og_images: dict[int, str] = {}
    source_labels: dict[int, str] = {}
    date_key = config.get("_date", date_str)
    news_json_path = news_json_override or (state_dir / f"news_{date_key}.json")
    if news_json_path.exists():
        import json as _json
        with open(news_json_path, encoding="utf-8") as f:
            news_data = _json.load(f)
        for idx, article in enumerate(news_data.get("articles", []), 1):
            source_labels[idx] = article.get("source_domain") or ""
            og_path = article.get("og_image", "")
            if og_path:
                og_file = Path(og_path)
                if og_file.exists():
                    img_data = base64.b64encode(og_file.read_bytes()).decode()
                    suffix = og_file.suffix.lstrip(".")
                    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "jpeg")
                    og_images[idx] = f"data:image/{mime};base64,{img_data}"

    # script_meta: フック見出し・要点（1始まりの記事番号キー。news JSON と同じ並び）
    hook_titles: dict[int, str] = {}
    article_points: dict[int, list[str]] = {}
    meta_path = state_dir / f"script_meta_{date_key}.json"
    if meta_path.exists():
        import json as _json
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            for idx, a in enumerate(meta.get("articles", []), 1):
                hook_titles[idx] = a.get("hook_title", "") or ""
                article_points[idx] = [p for p in (a.get("points") or []) if p]
        except (ValueError, OSError) as e:
            logger.warning("script_meta unreadable (%s) — slides fall back to article titles", e)
    captions_burned = bool(shorts and config.get("shorts", {}).get("burn_captions", False))

    # インデックススライド用: ニュースタイトル一覧を事前抽出（セリフのルビを優先）
    from script_generator import add_ruby_to_text
    news_titles = []
    for s in slides:
        if (not s["is_opening"] and not s["is_ending"] and not s["is_index"]
                and not s.get("is_keyword")
                and "のポイント" not in s["title"]):
            rd = _extract_ruby_dict(s["serif"])
            t = _apply_ruby_dict(s["title"], rd)
            news_titles.append(apply_ruby(add_ruby_to_text(t)))

    news_counter = 0
    # Track which article number we're on (each article = 2 slides: intro + point)
    article_index = 0

    # このデッキ内の記事数（紹介スライドの数 = ヘッダーの「n/M」表示の分母）
    news_total = sum(
        1 for s in slides
        if classify_slide(s, len(slides)) == "news" and not s["title"].endswith("ポイント")
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": resolution[0], "height": resolution[1]},
        )

        for slide in slides:
            slide_type = classify_slide(slide, len(slides))
            if slide_type == "news":
                news_counter += 1
                # Odd = intro slide (1,3,5...), even = explanation slide (2,4,6...)
                if news_counter % 2 == 1:
                    article_index += 1

            # Background variant for news slides (cycles through 5 colors)
            bg_variant = f"bg-{(news_counter - 1) % 5 + 1}" if slide_type == "news" else ""

            # OG image for this article (show on both intro and explanation slides)
            og_image = og_images.get(article_index, "") if slide_type == "news" else ""

            # 出典バッジ（記事ページのドメイン名）
            source_label = source_labels.get(article_index, "") if slide_type == "news" else ""

            # Parse keyword items from markdown body for keyword slides
            kw_items = None
            if slide_type == "keyword":
                kw_items = _parse_keyword_items(slide.get("body", ""))

            # per_topic モードでは各動画が単体で完結するため、
            # ニュース番号・進捗「n/M」は表示しない（0で非表示）
            html = render_slide_html(
                slide, slide_type, template_path, config,
                0 if per_topic else article_index,
                index_items=news_titles if slide_type == "index" else None,
                keyword_items=kw_items,
                bg_variant=bg_variant,
                ponte_expressions=ponte_expressions,
                news_counter=news_counter,
                og_image=og_image,
                source_label=source_label,
                news_total=0 if per_topic else news_total,
                hook_title=hook_titles.get(article_index, "") if (shorts and slide_type == "news") else "",
                points=article_points.get(article_index) if (shorts and slide_type == "news") else None,
                captions_burned=captions_burned,
            )

            await page.set_content(html, wait_until="networkidle")
            output_path = images_dir / f"slide_{slide['number']}.png"
            await page.screenshot(path=str(output_path), full_page=False)
            logger.info("Generated slide: %s", output_path)

        await browser.close()

    logger.info("Generated %d slides in %s", len(slides), images_dir)
    return images_dir


def generate_slides(
    script_path: Path,
    config_path: Path = Path("config.json"),
    date_str: str = "",
    shorts: bool = False,
) -> Path:
    """Synchronous wrapper for slide generation."""
    import asyncio
    return asyncio.run(generate_slides_async(script_path, config_path, date_str, shorts))


# ---------------------------------------------------------------------------
# Custom thumbnail (1280x720 JPEG for YouTube)
# ---------------------------------------------------------------------------

async def generate_thumbnail_async(
    date_str: str,
    config_path: Path = Path("config.json"),
    template_override: Path | None = None,
    news_json_override: Path | None = None,
    out_override: Path | None = None,
    force: bool = False,
) -> Path | None:
    """トップ記事のカスタムサムネイル（1280x720 JPEG）を生成する.

    config.youtube.custom_thumbnail が false の場合や news JSON が無い場合は
    None を返す（サムネイルなしでもアップロードは成立する）。

    template_override / news_json_override / out_override / force は UI プレビュー用の
    後方互換オプション（未指定時は従来動作）。
    """
    config = load_config(config_path)
    if not force and not config.get("youtube", {}).get("custom_thumbnail", False):
        return None

    state_dir = Path(config["paths"]["state_dir"])
    templates_dir = Path(config["paths"]["templates_dir"])
    template_path = template_override or (templates_dir / "thumbnail.html")
    if not template_path.exists():
        logger.warning("Thumbnail template not found: %s", template_path)
        return None

    news_json_path = news_json_override or (state_dir / f"news_{date_str}.json")
    if not news_json_path.exists():
        logger.warning("News JSON not found for thumbnail: %s", news_json_path)
        return None
    with open(news_json_path, encoding="utf-8") as f:
        news_data = json.load(f)
    articles = news_data.get("articles", [])
    if not articles:
        return None
    top = articles[0]

    # トップ記事タイトル → サムネ用の短いフック見出し（ルビ付き）
    from script_generator import add_ruby_to_text
    title = _thumbnail_hook(top.get("title_ja", top.get("title", "")))
    title_html = apply_ruby(add_ruby_to_text(title))

    # OG画像（背景用）
    og_image = ""
    og_path = top.get("og_image", "")
    if og_path and Path(og_path).exists():
        og_file = Path(og_path)
        img_data = base64.b64encode(og_file.read_bytes()).decode()
        suffix = og_file.suffix.lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "jpeg")
        og_image = f"data:image/{mime};base64,{img_data}"

    # ポンテ画像（normal）
    ponte_expressions = _load_ponte_expressions(config)
    ponte_image = ponte_expressions.get("normal", "")

    channel = config.get("channel", {})
    try:
        date_display = format_date_display(date_str)
    except (ValueError, KeyError):
        date_display = date_str

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        autoescape=False,
    )
    html = env.get_template(template_path.name).render(
        title=title_html,
        og_image=og_image,
        ponte_image=ponte_image,
        channel_name=channel.get("name", "ポンテのAI教室"),
        tagline=channel.get("tagline", "むずかしいAIを、かんたんに。"),
        date=date_display,
        day_class=get_day_class(date_str),
        news_count=len(articles),
        topic=news_data.get("topic", "AI"),
    )

    output_path = out_override or (state_dir / f"thumbnail_{date_str}.jpg")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        await page.set_content(html, wait_until="networkidle")
        # YouTubeサムネイルは2MB制限のためJPEGで出力
        await page.screenshot(path=str(output_path), type="jpeg", quality=90, full_page=False)
        await browser.close()

    logger.info("Generated thumbnail: %s", output_path)
    return output_path


def generate_thumbnail(
    date_str: str,
    config_path: Path = Path("config.json"),
) -> Path | None:
    """Synchronous wrapper for thumbnail generation."""
    import asyncio
    return asyncio.run(generate_thumbnail_async(date_str, config_path))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Slide Generator")
    parser.add_argument("--script", required=True, help="Path to script markdown")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--date", default="", help="Date string for display")
    parser.add_argument("--shorts", action="store_true", help="Generate Shorts (9:16) slides")
    args = parser.parse_args()
    path = generate_slides(Path(args.script), Path(args.config), args.date, args.shorts)
    print(f"Output: {path}")


if __name__ == "__main__":
    main()
