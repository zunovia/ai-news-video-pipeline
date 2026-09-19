"""Phase 2: 台本生成モジュール.

収集済みニュースデータからJinja2テンプレートを使って
911-plan互換のMarkdown台本を生成する。
LLM API（Gemini/Claude）でテンプレートの可変部分のみを生成（コスト最適化）。
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

# 日付フォールバックは日本時間（JST）基準（UTCだと深夜〜朝9時の実行で前日になる）
_JST = timezone(timedelta(hours=9))
from pathlib import Path

import re

import jinja2

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ruby post-processing (auto-annotate kanji with furigana)
# ---------------------------------------------------------------------------
# 形態素解析ベースの ruby_engine に一本化。
# 旧実装（漢字の連なりだけをpykakasiに渡す方式）は送り仮名の文脈が失われ
# 誤読が多発した（作(さく)る・賢(さとし)くなる・見逃(けんとう)せない等）ため廃止。

from ruby_engine import annotate_ruby, strip_kanji_ruby


def add_ruby_to_text(text: str) -> str:
    """Add ruby annotations to kanji that don't already have them.

    ruby_engine.annotate_ruby への互換ラッパー（slide_generator からも使用される）。
    既存のルビ注釈（手修正・英語カタカナ読み）は保護される。
    """
    return annotate_ruby(text)

def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_news(news_path: Path) -> dict:
    with open(news_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# LLM-based script generation
# ---------------------------------------------------------------------------

SCRIPT_SYSTEM_PROMPT_TEMPLATE = """\
あなたは「ポンテ」というかわいいAIロボットキャラクターです。
AI初心者の大人向けYouTubeチャンネル「ポンテのAI教室」で{topic}ニュースを、
1ニュース=1本・60秒前後のショート動画として紹介します。
視聴者は専門知識のない大人（仕事や家庭で忙しく、AIの話題についていきたい人）です。

ポンテの口調ルール:
- やさしくてわかりやすい言葉を使う。専門用語は必ずかんたんな言葉で言い換える
- 「〜だよ！」「〜なんだ！」のような親しみやすい口調（ただし視聴者を子ども扱いしない）
- ちょっとドジで好奇心いっぱい。リアクションは具体的に（何にどう驚いたかを言う）

文字数ルール（1本60秒前後に収まるよう厳守。ふりがな括弧内は数えない）:
- hook_title: 15〜24文字
- intro: 50〜70文字
- explanation: 90〜120文字
- insight: 30〜55文字
- ending: 40〜60文字
- 長すぎるセリフは自動で末尾の文が削られてしまうので、必ず範囲内に収めること

フック見出し(hook_title)のルール（そのまま動画タイトルになる。最重要）:
- そのニュースで一番驚く事実を、固有名詞や数字入りで言い切る
- 内容に合えば「【衝撃】」「【朗報】」「【速報】」のような頭ラベルを付けてよい
- 「？」で終わる問いかけ型も可。記事タイトルの逐語コピーは禁止
- 必ず「何がどうなった」まで言い切った完結した文にする（英字の製品名で文字数を使い切り、
  「〜でローカル」のように途中で切れた見出しは禁止。製品名は短く、結論の言葉を優先する）

紹介セリフ(intro)のルール（動画の冒頭3秒が勝負）:
- 挨拶・前置きは禁止（「みんなー」「今日は」「次のニュースは」などで始めない）
- 1文目: このニュースで一番驚く事実・数字をいきなり言い切る
- 2文目: それがなぜ気になるか・視聴者に何の関係があるかを一言
- 記事タイトルをそのまま読み上げるのは禁止。自分の言葉で言い換える

解説セリフ(explanation)のルール:
- 次の3ステップを各1文で構成する:
  (1) 何が起きたかをかんたんな言葉で説明する
  (2) たとえ話や身近な例で「つまりこういうこと」とイメージさせる（事実・数字があれば使う）
  (3) 視聴者の生活・仕事・お金にどう関係するかを一言そえる
- 締めの一言はそのニュースの内容に合わせて毎回変える。中身のない汎用的な感想は禁止

ポンテの考察(insight)のルール（この動画だけの独自視点。手を抜かない）:
- ポンテ自身の意見・ツッコミ・予想を1〜2文で言う。事実の繰り返しは禁止
- 切り口はニュースごとに変える（便利な面／気をつけたい面／こう使えそう／
  提供側の狙い／半年後どうなりそうか など）
- 「ポンテはこう思うな。」「正直、これは〜」のような一人称の言い方で始めてよいが、
  同じ書き出しを複数のニュースで繰り返さないこと（全ニュースで言い回しを変える）
- 3本のうち1本くらいは、最後をそのニュースに固有の具体的な問いかけ（〜？）で
  締めてよい（例: 「あなたの仕事なら、まず何を任せてみたい？」）。
  ただし「みんなはどう思う？」のような中身のない汎用の問いは禁止

禁止フレーズ（これらや似た定型文を絶対に使わない）:
- 「知れば知るほどおもしろくなるよね」「ポンテ、思わず目が丸くなっちゃったよ」
- 「こういうニュースを知ってると、ちょっとかっこいいよね」「これからどうなるか楽しみだね」
- 「すごい時代になってきたね」「未来がワクワクするね」「ワクワク」を含むあらゆる表現
- 「どんどん進化している」「どんどん進歩している」のような中身のない汎用感想
- 「ポンテのアンテナがピピッと反応したよ」「ポンテも負けないようにがんばるぞー」
- 「今日もひとつ賢くなったね」「みんなはどう思う？」「ポンテ、もっと調べたくなっちゃった」

読みがな（ルビ）のルール:
- 漢字にふりがなを付けないこと（後工程のシステムが正確に自動付与するため、
  自分で付けると二重・誤りの原因になる）
- 英語・アルファベットの言葉にだけ、カタカナ読みを付ける
  形式: 英語(カタカナ) 例: Siri(シリ)、Apple(アップル)、ChatGPT(チャットジーピーティー)

セリフの中のカギ括弧「」は1重まで（カギ括弧の中にカギ括弧を入れない）。

良い例（このレベルの具体性と長さを目指す）:
hook_title: 「【衝撃】FaceIDの生みの親が脳のAI診断に参入」
intro: 「iPhone(アイフォン)の顔認証を作った本人が、今度は脳の病気をAIで見つける会社を立ち上げたんだ！顔の次は脳って、話が大きすぎない？」
explanation: 「FaceID(フェイスアイディー)を生んだ技術者の新会社は、血液検査みたいに手軽な脳の健康チェックをAIで実現しようとしているんだ。脳ドックは今は高くて大がかりだけど、それがぐっと身近になるかもしれないよ！」
insight: 「ポンテはこう思うな。健康診断の項目に、脳のAIチェックが入る日は意外と近いかも！」

JSON形式のみで回答（説明文不要）。
"""

# Backward compatibility
SCRIPT_SYSTEM_PROMPT = SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(topic="AI")


# ---------------------------------------------------------------------------
# Duration control (3-minute budget enforcement)
# ---------------------------------------------------------------------------
# LLMは文字数指定を守れないことがあるため、読み上げ文字数（ルビ・演出指示を
# 除いたTTS実文字数）をコード側で検証し、超過時はフィードバック付きリトライ、
# それでも超過なら文単位で決定的にトリムして3分尺を保証する。

# 各フィールドの読み上げ文字数上限（プロンプト指定よりやや緩いハードキャップ）
_INTRO_TTS_LIMIT = 80
_EXPLANATION_TTS_LIMIT = 125
_INSIGHT_TTS_LIMIT = 60
_ENDING_TTS_LIMIT = 70
_KEYWORD_DESC_TTS_LIMIT = 45
_MAX_KEYWORDS = 3
_HOOK_TITLE_MAX = 26
# 閉じ括弧まで伸ばすことを許す超過分（固有名詞「Gemini 3.8 Live」等を丸ごと残すため）
_HOOK_TITLE_SLACK = 8
# 括弧修復ループの反復上限（暴走防止）
_REPAIR_MAX_ITER = 32

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")


def _tts_len(text: str) -> int:
    """セリフの読み上げ文字数（ルビ括弧・演出指示を除く）を返す."""
    from video_builder import serif_to_tts_text
    return len(serif_to_tts_text(text or ""))


def _trim_serif_to_limit(text: str, limit: int) -> str:
    """読み上げ文字数が limit を超えるセリフを文単位で末尾から切り詰める.

    最低でも先頭1文は残す（1文だけで超過する場合はそのまま返す）。
    カギ括弧「」の途中で切れた場合はさらに1文ずつ削って括弧の対応を保つ。
    """
    if _tts_len(text) <= limit:
        return text
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s]
    if not sentences:
        return text
    out = sentences[0]
    kept = 1
    for s in sentences[kept:]:
        if _tts_len(out + s) > limit:
            break
        out += s
        kept += 1
    # 文区切り（。！？）がカギ括弧の内側にもマッチするため、切り詰めで
    # 「が閉じないまま終わることがある。対応が崩れていたら末尾の文を削る
    while kept > 1 and out.count("「") != out.count("」"):
        kept -= 1
        out = "".join(sentences[:kept])
    return out


def _validate_script_lengths(result: dict) -> list[str]:
    """セリフの読み上げ文字数を検証し、超過項目のフィードバック文を返す."""
    issues = []
    for i, a in enumerate(result.get("articles", []), 1):
        n = _tts_len(a.get("intro", ""))
        if n > _INTRO_TTS_LIMIT:
            issues.append(f"ニュース{i}のintroが長すぎます（読み上げ{n}文字）。50〜70文字に短縮してください")
        n = _tts_len(a.get("explanation", ""))
        if n > _EXPLANATION_TTS_LIMIT:
            issues.append(f"ニュース{i}のexplanationが長すぎます（読み上げ{n}文字）。90〜120文字に短縮してください")
        n = _tts_len(a.get("insight", ""))
        if n > _INSIGHT_TTS_LIMIT:
            issues.append(f"ニュース{i}のinsightが長すぎます（読み上げ{n}文字）。30〜55文字に短縮してください")
    n = _tts_len(result.get("ending", ""))
    if n > _ENDING_TTS_LIMIT:
        issues.append(f"endingが長すぎます（読み上げ{n}文字）。40〜60文字に短縮してください")
    return issues


def _cap_script_lengths(result: dict) -> dict:
    """リトライ後も超過しているセリフを文単位トリムで強制的に上限内へ収める."""
    for a in result.get("articles", []):
        a["intro"] = _trim_serif_to_limit(a.get("intro", ""), _INTRO_TTS_LIMIT)
        a["explanation"] = _trim_serif_to_limit(a.get("explanation", ""), _EXPLANATION_TTS_LIMIT)
        if a.get("insight"):
            a["insight"] = _trim_serif_to_limit(a["insight"], _INSIGHT_TTS_LIMIT)
    if result.get("ending"):
        result["ending"] = _trim_serif_to_limit(result["ending"], _ENDING_TTS_LIMIT)
    keywords = result.get("keywords", [])[:_MAX_KEYWORDS]
    for kw in keywords:
        if kw.get("desc"):
            kw["desc"] = _trim_serif_to_limit(kw["desc"], _KEYWORD_DESC_TTS_LIMIT)
    result["keywords"] = keywords
    return result


def generate_article_scripts(
    articles: list[dict],
    llm_config: dict,
    topic: str = "AI",
    max_retries: int = 2,
) -> dict:
    """Generate intro and explanation scripts for each article (with retry)."""
    from api_utils import LLMClient
    import time

    client = LLMClient(llm_config)
    max_tokens = llm_config.get(llm_config.get("provider", "gemini"), {}).get("max_tokens_script", 8000)
    system_prompt = SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(topic=topic)

    prompt_parts = []
    for i, article in enumerate(articles, 1):
        # リッチ要約（summary_ja_long/facts/kid_angle）があれば優先使用（旧JSON互換）
        summary = (article.get("summary_ja_long")
                   or article.get("summary_ja", article.get("summary", "")))
        lines = [
            f"ニュース{i}:",
            f"  タイトル: {article.get('title_ja', article['title'])}",
            f"  要約: {summary}",
        ]
        facts = article.get("facts")
        if isinstance(facts, list) and facts:
            lines.append("  使える事実: " + " / ".join(str(f) for f in facts))
        if article.get("kid_angle"):
            lines.append(f"  生活・仕事との関係: {article['kid_angle']}")
        lines.append(f"  ソース: {article.get('source', '')}")
        prompt_parts.append("\n".join(lines))

    user_prompt = (
        "以下のニュースそれぞれについて、ポンテの口調でフック見出し・紹介文・解説文・考察を生成してください。\n"
        "また、エンディングのセリフも1つ生成してください。\n"
        "さらに、ニュース全体から重要な専門用語をちょうど3個抽出し、keywordsとして返してください。\n"
        "漢字にふりがなは付けないでください（自動付与されます）。"
        "英語の言葉にはカタカナ読み（例: Siri(シリ)）を付けてください。\n\n"
        + "\n\n".join(prompt_parts)
        + '\n\nJSON形式で回答:\n'
        '{\n'
        '  "articles": [\n'
        '    {"label": "ニュースの内容がわかる12〜18文字の短い見出し（「ニュース1」のような番号だけのラベルは禁止）",'
        ' "hook_title": "動画タイトルになるフック見出し（15〜24文字。固有名詞・数字入りで言い切る）",'
        ' "intro": "紹介セリフ（50〜70文字。挨拶禁止・1文目で一番驚く事実を言い切る）",'
        ' "explanation": "解説セリフ（90〜120文字）",'
        ' "insight": "ポンテの考察（30〜55文字。独自の意見・ツッコミ・予想）"}\n'
        '  ],\n'
        '  "ending": "エンディングセリフ（40〜60文字）",\n'
        '  "keywords": [\n'
        '    {"term": "用語（漢字にふりがな不要。英語にはカタカナ読み）", "reading": "よみがな", "desc": "ポンテ口調の解説（25〜35文字）"}\n'
        '  ]\n'
        '}'
    )

    last_error: Exception | None = None
    best_result: dict | None = None
    feedback = ""
    for attempt in range(max_retries + 1):
        text = ""
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info("Script generation retry %d/%d after %ds...", attempt, max_retries, wait)
                time.sleep(wait)

            text = client.generate(system_prompt, user_prompt + feedback, max_tokens)
            text = (text or "").strip()

            if "{" not in text:
                raise ValueError(f"No JSON object in response: {text[:200]}")
            json_str = text[text.index("{"):text.rindex("}") + 1]
            result = json.loads(json_str)
            if not result.get("articles"):
                raise ValueError("Response JSON has no articles")

            # 読み上げ文字数の検証（3分尺の担保）。超過時はフィードバック付きリトライ
            best_result = result
            issues = _validate_script_lengths(result)
            if not issues:
                return _cap_script_lengths(result)
            logger.info("Script length issues (attempt %d): %s",
                        attempt + 1, " / ".join(issues))
            feedback = (
                "\n\n【前回の出力の問題点】以下を修正して、全体をもう一度生成してください:\n- "
                + "\n- ".join(issues)
            )
        except Exception as e:
            last_error = e
            logger.warning(
                "Script generation attempt %d/%d failed: %s — raw response head: %r",
                attempt + 1, max_retries + 1, e, text[:500],
            )

    if best_result is not None:
        # リトライ後も長い場合は文単位トリムで強制的に収める（フォールバックより良い）
        logger.warning("Length issues remain after retries — trimming serifs to hard caps")
        return _cap_script_lengths(best_result)
    raise ValueError(f"Script generation failed after {max_retries + 1} attempts: {last_error}")


def _drop_last_sentence(text: str) -> str:
    """セリフの最後の1文を削る（1文しかない場合はそのまま返す）."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s]
    if len(sentences) <= 1:
        return text
    return "".join(sentences[:-1])


def estimate_script_seconds(script_data: dict, news_articles: list[dict]) -> float:
    """テンプレートレンダー後の動画推定尺（秒）を、レンダー前に近似計算する.

    video_builder の推定式（TTS文字数/6.0 + スライドあたり0.6秒オーバーヘッド、
    クロスフェード重なり -0.5秒×(N-1)）に合わせる。
    """
    articles = script_data.get("articles", [])
    keywords = script_data.get("keywords", [])

    # インデックスセリフ（daily_script.j2 の構造を近似）
    index_text = "今日のニュースはこちら！"
    for i, (sa, na) in enumerate(zip(articles, news_articles), 1):
        label = _safe_label(sa.get("label") or na.get("title_ja", ""))
        index_text += f"{i}つ目、{label}。"
    index_text += "どれも気になるよね！さっそく見ていこう！"

    # キーワードセリフ
    kw_text = "今日出てきた大事な言葉を確認しよう！"
    for i, kw in enumerate(keywords):
        kw_text += f"{kw.get('term', '')}は、{kw.get('desc', '')}"
        if i < len(keywords) - 1:
            kw_text += "次は、"
    kw_text += "しっかり覚えておこうね！"

    # エンディング（render_script が概要欄ガイドを追記する分を含める）
    url_guide = "概要欄にニュースの元の記事リンクを載せているから、気になった人はチェックしてみてね！"
    ending_text = script_data.get("ending", "") + url_guide

    opening_len = len("みんなー！ポンテだよ！今日のニュース、いってみよう！")
    total_chars = (
        opening_len
        + _tts_len(index_text)
        + sum(_tts_len(a.get("intro", "")) + _tts_len(a.get("explanation", ""))
              + _tts_len(a.get("insight", "")) for a in articles)
        + _tts_len(kw_text)
        + _tts_len(ending_text)
    )

    num_slides = 2 + 2 * len(articles) + 2  # opening + index + 記事×2 + keyword + ending
    return total_chars / 6.0 + 0.6 * num_slides - 0.5 * (num_slides - 1)


def enforce_duration_budget(
    script_data: dict,
    news_articles: list[dict],
    max_estimated_sec: float,
) -> dict:
    """台本全体の推定尺が予算を超える場合、最も長い解説から文単位で切り詰める.

    LLMリトライをすり抜けた超過分を決定的に補正し、3分尺を保証する最終ゲート。
    解説は読み上げ90文字を下限とし、それ以上削れなくなったら警告して打ち切る。
    """
    est = estimate_script_seconds(script_data, news_articles)
    if est <= max_estimated_sec:
        return script_data

    logger.info("Duration budget: estimated %.0fs > %.0fs — trimming longest explanations",
                est, max_estimated_sec)
    for _ in range(20):  # 暴走防止
        est = estimate_script_seconds(script_data, news_articles)
        if est <= max_estimated_sec:
            break
        # 読み上げが最も長い解説（90文字超）を1文削る
        candidates = [a for a in script_data.get("articles", [])
                      if _tts_len(a.get("explanation", "")) > 90]
        if not candidates:
            logger.warning("Duration budget: cannot trim further (est %.0fs > %.0fs)",
                           est, max_estimated_sec)
            break
        longest = max(candidates, key=lambda a: _tts_len(a.get("explanation", "")))
        trimmed = _drop_last_sentence(longest["explanation"])
        if trimmed == longest["explanation"]:
            logger.warning("Duration budget: single-sentence explanation cannot be trimmed")
            break
        longest["explanation"] = trimmed

    logger.info("Duration budget: final estimate %.0fs (max %.0fs)",
                estimate_script_seconds(script_data, news_articles), max_estimated_sec)
    return script_data


# ---------------------------------------------------------------------------
# Fallback script generation (no API required)
# ---------------------------------------------------------------------------

# 注意: LLMプロンプトの「禁止フレーズ」リストと重複する文言をここに入れないこと
# （フォールバック時もLLM時も同じ品質基準を保つため）
_EXPLANATION_CLOSINGS = [
    # ワクワク系
    "みんなの生活(せいかつ)にも関係(かんけい)してくるかもしれないね！",
    "これからもっとすごくなるかもしれないよ！",
    "新(あたら)しい世界(せかい)が広(ひろ)がっていくね！",
    # びっくり系
    "ポンテもびっくりしちゃった！",
    "大人(おとな)もびっくりするニュースだよね！",
    "えー！って声(こえ)が出(で)ちゃうよね！",
    # 考えさせる系
    "知(し)っておくと友達(ともだち)にも教(おし)えてあげられるよ！",
    "世界(せかい)がどんどん変(か)わっていくね！",
    "みんなも自分(じぶん)だったらどうするか、考(かんが)えてみてね！",
    "お家(うち)の人(ひと)にも教(おし)えてあげよう！",
    "学校(がっこう)の友達(ともだち)と話(はな)してみるのもいいかも！",
    "これって実(じつ)はみんなにも関係(かんけい)あるんだよ！",
    # ポンテらしい一言系
    "ポンテ的(てき)にはすっごく気(き)になるニュースだったよ！",
    "ポンテ、メモメモ…って書(か)いておこうっと！",
    # 応援・前向き系
    "みんなも新(あたら)しいことにチャレンジしてみよう！",
    "未来(みらい)をつくるのは、みんなかもしれないよ！",
]

_INTRO_VARIATIONS = [
    # ストレート系
    "次のニュースはこれだよ！{title}っていうニュースなんだ！",
    "続(つづ)いてはこちら！{title}だよ！",
    "お次(つぎ)はこのニュース！{title}！チェックしてみよう！",
    "どんどんいくよー！{title}っていうニュースだよ！",
    "さあ次のニュース！{title}！",
    # テンション高め系
    "じゃじゃーん！次はこれ！{title}だよ！",
    "はいはーい！お次はこちら！{title}！",
    "注目(ちゅうもく)！{title}っていうニュースがあるんだ！",
    "おおっと！こんなニュースもあるよ！{title}！",
    "ここからがすごいよ！{title}！",
    "わくわく！次はね、{title}っていうニュースだよ！",
    # ポンテらしい導入系
    "ポンテが見(み)つけてきたよ！{title}だって！",
    "ポンテが気(き)になったニュースはこれ！{title}！",
    "ポンテのおすすめニュース！{title}だよ！",
    "ポンテ、このニュースは見逃(みのが)せない！{title}！",
    "ポンテが選(えら)んだ次のニュースは…{title}！",
    # 語りかけ系
    "みんな聞(き)いて聞いて！{title}だよ！",
    "さてさて、次はどんなニュースかな？{title}だよ！",
    "次のニュース、知(し)ってるかな？{title}！",
    "えっ、これ知(し)ってた？{title}っていうニュースだよ！",
    "みんなに教(おし)えたいニュースがあるよ！{title}！",
]


_KEYWORD_POOL = [
    {"term": "人工知能(じんこうちのう)", "desc": "コンピュータが人間(にんげん)みたいに考(かんが)える技術(ぎじゅつ)だよ！"},
    {"term": "機械学習(きかいがくしゅう)", "desc": "データからルールを自分(じぶん)で見(み)つける方法(ほうほう)なんだ！"},
    {"term": "アルゴリズム", "desc": "問題(もんだい)を解(と)くための手順(てじゅん)のことだよ！"},
    {"term": "ニューラルネットワーク", "desc": "人間(にんげん)の脳(のう)のしくみをまねしたAIの仕組(しく)みだよ！"},
    {"term": "自然言語処理(しぜんげんごしょり)", "desc": "AIが人間(にんげん)の言葉(ことば)を理解(りかい)する技術(ぎじゅつ)のことだよ！"},
    {"term": "大規模言語(だいきぼげんご)モデル", "desc": "たくさんの文章(ぶんしょう)を学(まな)んで会話(かいわ)できるAIのことだよ！"},
    {"term": "生成(せいせい)AI", "desc": "文章(ぶんしょう)や画像(がぞう)を新(あたら)しく作(つく)り出(だ)せるAIだよ！"},
    {"term": "チャットボット", "desc": "AIが人間(にんげん)みたいにおしゃべりしてくれるプログラムだよ！"},
    {"term": "クラウドコンピューティング", "desc": "インターネット上(じょう)のコンピュータを借(か)りて使(つか)う仕組(しく)みだよ！"},
    {"term": "API", "desc": "いろんなプログラム同士(どうし)がおしゃべりするための約束事(やくそくごと)だよ！"},
    {"term": "データサイエンス", "desc": "たくさんのデータから役立(やくだ)つ情報(じょうほう)を見(み)つける学問(がくもん)だよ！"},
    {"term": "ディープラーニング", "desc": "何層(なんそう)もの計算(けいさん)を重(かさ)ねて学(まな)ぶAIの方法(ほうほう)だよ！"},
    {"term": "IoT", "desc": "家電(かでん)やセンサーがインターネットにつながる仕組(しく)みだよ！"},
    {"term": "ロボティクス", "desc": "ロボットを作(つく)ったり動(うご)かしたりする技術(ぎじゅつ)だよ！"},
    {"term": "音声認識(おんせいにんしき)", "desc": "AIが人(ひと)の声(こえ)を聞(き)き取(と)って文字(もじ)にする技術(ぎじゅつ)だよ！"},
    {"term": "画像認識(がぞうにんしき)", "desc": "AIが写真(しゃしん)や動画(どうが)の中身(なかみ)を理解(りかい)する技術(ぎじゅつ)だよ！"},
    {"term": "プロンプト", "desc": "AIに指示(しじ)を出(だ)すための文章(ぶんしょう)のことだよ！"},
    {"term": "ファインチューニング", "desc": "AIを特定(とくてい)の目的(もくてき)に合(あ)わせて調整(ちょうせい)することだよ！"},
    {"term": "エッジコンピューティング", "desc": "データをクラウドに送(おく)らず手元(てもと)の機器(きき)で処理(しょり)する方法(ほうほう)だよ！"},
    {"term": "オープンソース", "desc": "プログラムの中身(なかみ)を誰(だれ)でも見(み)られるようにすることだよ！"},
    {"term": "自動運転(じどううんてん)", "desc": "AIが車(くるま)を自分(じぶん)で運転(うんてん)してくれる技術(ぎじゅつ)だよ！"},
]


def generate_article_scripts_fallback(articles: list[dict]) -> dict:
    """Generate scripts from article data without API calls."""
    import hashlib
    import random

    # 日付＋記事タイトルからシードを作り、毎日異なる組み合わせにする
    seed_text = "".join(a.get("title", "") for a in articles)
    rng = random.Random(hashlib.md5(seed_text.encode()).hexdigest())

    # シャッフルしたコピーから重複なく選ぶ
    intros = list(_INTRO_VARIATIONS)
    closings = list(_EXPLANATION_CLOSINGS)
    rng.shuffle(intros)
    rng.shuffle(closings)

    script_articles = []
    for i, article in enumerate(articles):
        title = article.get("title_ja", article.get("title", "AIニュース"))
        summary = article.get("summary_ja", article.get("summary", ""))[:100]
        # 末尾の句点を除去してから結合（「。。」防止）
        summary = summary.rstrip("。")
        intro = intros[i % len(intros)].format(title=title)
        closing = closings[i % len(closings)]
        script_articles.append({
            "hook_title": _fallback_hook_title(title),
            "intro": intro,
            "explanation": f"{summary}。{closing}",
            # フォールバックでは独自考察を捏造しない（空なら結合されない）
            "insight": "",
        })

    # 日付ごとに異なるキーワードを選出（記事タイトルからハッシュでシード生成）
    seed_text = "".join(a.get("title", "") for a in articles)
    seed = int(hashlib.md5(seed_text.encode()).hexdigest(), 16)
    pool = list(_KEYWORD_POOL)
    selected = []
    for _ in range(3):
        idx = seed % len(pool)
        selected.append(pool.pop(idx))
        seed = seed // len(pool) + idx

    return {
        "articles": script_articles,
        "ending": "今日のニュースはここまで！みんな、また明日(あした)ね！チャンネル登録(とうろく)、よろしくね！バイバーイ！",
        "keywords": selected,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _safe_label(text: str, max_chars: int = 20) -> str:
    """インデックス読み上げ用の短い見出しを作る。途中で切れたルビ括弧は除去する."""
    label = (text or "").strip()
    if len(label) > max_chars:
        label = label[:max_chars]
        # 切断で閉じ括弧を失ったルビ注釈を除去（例: "発表(はっぴょ" → "発表"）
        label = re.sub(r"[(（][^)）]*$", "", label)
    return label.rstrip("、。・:：")


# 見出し切断時に整合を取る対括弧（開き → 閉じ）
_TITLE_BRACKETS = {"「": "」", "『": "』", "【": "】", "《": "》", "“": "”"}


def _strip_unmatched_brackets(text: str) -> str:
    """対応相手のいない括弧文字だけを取り除く（スタック法・1パス）.

    切り詰めヒューリスティックの精度に依存せず「開き数＝閉じ数」を構造的に保証する
    最終防御層。元テキスト側が既に非対称な場合（"…を超えた」と専門家が…"）にも効く。
    """
    closers = {cl: op for op, cl in _TITLE_BRACKETS.items()}
    stack: list[int] = []        # 未対応の開き括弧の位置
    drop: set[int] = set()       # 除去する文字位置
    for i, ch in enumerate(text):
        if ch in _TITLE_BRACKETS:
            stack.append(i)
        elif ch in closers:
            # 直近の未対応開き括弧が同種なら対応成立、そうでなければ孤立した閉じ括弧
            if stack and text[stack[-1]] == closers[ch]:
                stack.pop()
            else:
                drop.add(i)
    drop.update(stack)           # 最後まで閉じられなかった開き括弧
    if not drop:
        return text
    return "".join(ch for i, ch in enumerate(text) if i not in drop)


def _repair_open_bracket(cut: str, full: str, slack: int) -> str:
    """切断で開いたままになった括弧を修復する.

    元テキスト側に閉じ括弧があり、そこまでが `len(cut) + slack` に収まるなら
    閉じ括弧まで伸ばす（固有名詞を丸ごと残す）。収まらなければ孤立した開き括弧を
    1文字だけ落とす。フォールバックの優先順位は「情報保持 > 括弧整合の見た目」で、
    括弧の対応そのものは呼び出し側の _strip_unmatched_brackets が最終保証する。

    括弧の種類が入り混じったタイトルでは「伸ばす→別の未閉じ括弧が露出→縮める」が
    循環しうるため、訪問済みの状態を記録して再訪時点で打ち切る（必ず有限回で終了）。
    修復不能で空になる場合は呼び出し側が元の cut にフォールバックできるよう
    空文字列を返す。
    """
    limit = len(cut) + slack
    seen = {cut}
    for _ in range(_REPAIR_MAX_ITER):
        # 最も内側の「未閉じの開き括弧」を探す
        pos = -1
        for op, cl in _TITLE_BRACKETS.items():
            i = cut.rfind(op)
            if i > pos and cut.count(op, i) > cut.count(cl, i):
                pos = i
        if pos < 0:
            return cut
        close = _TITLE_BRACKETS[cut[pos]]
        end = full.find(close, pos)
        if 0 <= end < limit:
            nxt = full[: end + 1]
        else:
            # 閉じ括弧が見つからない／遠すぎる。ここで開き括弧の手前まで切り捨てると
            # 見出し冒頭に壊れた括弧が1文字あるだけで実体が消し飛ぶため（"O《penAIが…"
            # → "O"）、情報保持を優先して孤立した開き括弧1文字だけを除去する。
            nxt = (cut[:pos] + cut[pos + 1:]).rstrip("、。・:： ")
        if not nxt:
            return ""
        if nxt in seen:
            # 状態が循環した。孤立した開き括弧を落として確定的に打ち切る
            # （残る不整合は呼び出し側の _strip_unmatched_brackets が保証する）。
            return (cut[:pos] + cut[pos + 1:]).rstrip("、。・:： ")
        seen.add(nxt)
        cut = nxt
    logger.warning("_repair_open_bracket: did not converge for %r", full)
    return cut


def _fallback_hook_title(title_ja: str, limit: int = _HOOK_TITLE_MAX) -> str:
    """LLMのhook_titleが無い/長すぎる場合のフック見出しを記事タイトルから作る.

    最初の文の切れ目（？！。）で切り、無ければ読点、それも無ければ
    英単語の途中で切れない位置まで戻して切り詰める。
    最後に必ず括弧の対応を整えてから返す。
    """
    t = strip_kanji_ruby((title_ja or "").strip())
    # ルビ括弧（英語カタカナ読み）はタイトル表示に不要なので除去
    t = re.sub(r"[(（][ぁ-んァ-ヶー]+[)）]", "", t)
    # 対応の取れない括弧を除去して「開き数＝閉じ数」を保証する（最終防御層）
    out = _strip_unmatched_brackets(_truncate_hook_title(t, limit)).rstrip("、。・:： ")
    if not out:
        # 切り出した先頭が括弧・句読点だけだった場合は、先に元タイトル側の
        # 括弧の不整合を除いてから切り直す（空タイトルの生成を避ける）。
        cleaned = _strip_unmatched_brackets(t)
        if cleaned != t:
            out = _strip_unmatched_brackets(
                _truncate_hook_title(cleaned, limit)).rstrip("、。・:： ")
    # ここでも空なら入力が括弧・句読点のみの退化ケース。非整合な見出しを返すより
    # 空を返し、呼び出し側（youtube_uploader）に代替ラベルを立てさせる。
    return out


def _truncate_hook_title(t: str, limit: int) -> str:
    """フック見出しの本体を切り詰める（括弧の整合は呼び出し側で担保）."""
    if len(t) <= limit:
        return t
    for i, ch in enumerate(t):
        if ch in "？！?!" and 6 <= i <= limit:
            return t[: i + 1]
    for i, ch in enumerate(t):
        if ch in "。、" and 8 <= i <= limit:
            return t[:i]
    cut = t[:limit]
    # 英単語の途中で切れた場合は単語境界まで戻す（"GPT-" のような切断を防ぐ）
    if (re.match(r"[A-Za-z0-9\-\.]", t[limit])
            and re.search(r"[A-Za-z0-9\-\.]$", cut)):
        trimmed = re.sub(r"[A-Za-z0-9\-\.]+$", "", cut)
        if trimmed:
            cut = trimmed
    # 切断で開いたままの括弧を修復（"…「Local" → "…「Local Models」"）
    plain = cut.rstrip("、。・:： ")
    repaired = _repair_open_bracket(plain, t, _HOOK_TITLE_SLACK)
    # 修復で実体（括弧以外の文字）が失われる場合は単純カットを採用する。
    # _repair_open_bracket は開き括弧の手前まで詰めるため、括弧が密集した見出しでは
    # 中身が丸ごと消えることがあり、そのまま使うと空タイトルになる。
    if _strip_unmatched_brackets(repaired).strip("、。・:： "):
        return repaired
    return plain


def normalize_hook_titles(script_data: dict, news_articles: list[dict]) -> dict:
    """各記事の hook_title を検証・補正する（欠落・過長は記事タイトルから決定的に生成）."""
    for sa, na in zip(script_data.get("articles", []), news_articles):
        hook = re.sub(r"\s+", " ", str(sa.get("hook_title") or "")).strip()
        if not hook or len(hook) > _HOOK_TITLE_MAX:
            fallback = _fallback_hook_title(na.get("title_ja", na.get("title", "")))
            if hook:
                logger.info("hook_title too long (%d chars) — using fallback: %s",
                            len(hook), fallback)
            hook = fallback
        sa["hook_title"] = hook
    return script_data


def render_script(
    news_data: dict,
    script_data: dict,
    template_path: Path,
    date_str: str,
    keywords: list[dict] | None = None,
    **kwargs,
) -> str:
    """Render the final script using Jinja2 template."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        autoescape=False,
    )
    template = env.get_template(template_path.name)

    articles = []
    for i, article in enumerate(news_data.get("articles", [])):
        script_articles = script_data.get("articles", [])
        script_item = script_articles[i] if i < len(script_articles) else {}
        title_ja = article.get("title_ja", article.get("title", f"ニュース{i+1}"))
        # LLMの手抜きラベル（「ニュース1」等）は捨ててタイトルから作り直す
        label_src = (script_item.get("label") or "").strip()
        if re.fullmatch(r"ニュース\d+", label_src):
            label_src = ""
        articles.append({
            "title_ja": title_ja,
            # インデックスの読み上げ用短縮見出し（LLM生成。なければタイトル先頭から作る）
            "label": _safe_label(label_src or title_ja),
            "intro": script_item.get("intro", "このニュースを紹介するよ！"),
            "explanation": script_item.get("explanation", "すごいニュースだね！"),
            # ポンテの考察（独自視点）。解説スライドのセリフ末尾に結合される
            "insight": script_item.get("insight", ""),
            "source": article.get("source", ""),
            "link": article.get("link", ""),
        })

    # 金曜日は「また明日」→「また来週」に置換
    from datetime import datetime as _dt
    try:
        _is_friday = _dt.strptime(date_str, "%Y%m%d").weekday() == 4
    except (ValueError, KeyError):
        _is_friday = False

    # Append URL guide to ending if not already present
    # （or でつなぐことで空文字列のendingもデフォルトにフォールバックさせる）
    ending = script_data.get("ending") or "今日のニュースはここまで！また明日ね、バイバーイ！"
    if _is_friday:
        ending = ending.replace("また明日", "また来週(らいしゅう)")
    url_guide = "概要欄(がいようらん)にニュースの元(もと)の記事(きじ)リンクを載(の)せているから、気(き)になった人(ひと)はチェックしてみてね！"
    if "概要欄" not in ending:
        ending = ending.rstrip("！!") + "！" + url_guide

    return template.render(
        articles=articles,
        ending=ending,
        date=date_str,
        keywords=keywords or script_data.get("keywords", []),
        topic=kwargs.get("topic", "AI"),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_script(
    news_path: Path,
    config_path: Path = Path("config.json"),
    script_type: str = "daily",
    topic: str = "",
) -> Path:
    """Generate a script from news data. Returns path to output markdown."""
    config = load_config(config_path)
    llm_cfg = config.get("llm", {})
    state_dir = Path(config["paths"]["state_dir"])
    templates_dir = Path(config["paths"]["templates_dir"])

    news_data = load_news(news_path)
    date_str = news_data.get("date", datetime.now(_JST).strftime("%Y%m%d"))
    topic_label = topic if topic else news_data.get("topic", "AI")

    # Generate scripts via LLM (with fallback)
    allow_fallback = config.get("script", {}).get("allow_fallback", True)
    script_source = "llm"
    script_error = ""
    try:
        script_data = generate_article_scripts(news_data["articles"], llm_cfg, topic=topic_label)
    except Exception as e:
        script_error = str(e)
        if not allow_fallback:
            logger.error("LLM script generation failed and script.allow_fallback=false: %s", e)
            raise
        logger.warning("LLM script generation failed, using fallback: %s", e)
        script_data = generate_article_scripts_fallback(news_data["articles"])
        script_source = "fallback"

    # hook_title の検証・補正（欠落・過長は記事タイトルから決定的に生成）
    script_data = normalize_hook_titles(script_data, news_data["articles"])

    # 3分尺の最終保証は16:9まとめ動画専用。1ニュース=1本Shorts運用
    # （pipeline.daily_video_enabled=false）では各セリフの個別上限だけで
    # 1本60秒前後が保証されるため、全体トリムはスキップする
    daily_enabled = config.get("pipeline", {}).get("daily_video_enabled", True)
    if daily_enabled:
        max_est = config.get("script", {}).get("max_estimated_sec", 205)
        script_data = enforce_duration_budget(script_data, news_data["articles"], max_est)

    # Render with Jinja2 template
    template_name = f"{script_type}_script.j2"
    template_path = templates_dir / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    script_md = render_script(news_data, script_data, template_path, date_str, topic=topic_label)

    # Apply ruby post-processing:
    # 1) LLM出力に混入した（誤りうる）漢字ルビを除去（英語カタカナ読みは保持）
    # 2) 形態素解析エンジンで正確なルビを自動付与
    # 対象: セリフ行＋キーワード箇条書き行（キーワードカードの表示に使われる）
    lines = script_md.split("\n")
    processed_lines = []
    for line in lines:
        if line.startswith("**セリフ**") or line.startswith("- **"):
            processed_lines.append(add_ruby_to_text(strip_kanji_ruby(line)))
        else:
            processed_lines.append(line)
    script_md = "\n".join(processed_lines)

    # 二重句点「。。」を「。」に修正
    script_md = script_md.replace("。。", "。")

    # Save
    output_path = state_dir / f"script_{date_str}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(script_md)

    # 台本の生成元を可視化（フォールバック使用を無言で隠さない）
    # articles: アップローダーが動画タイトル（hook_title）を組み立てる材料
    meta_path = state_dir / f"script_meta_{date_str}.json"
    meta = {
        "source": script_source,
        "error": script_error,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles": [
            {
                "hook_title": a.get("hook_title", ""),
                "label": a.get("label", ""),
            }
            for a in script_data.get("articles", [])
        ],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    if script_source == "fallback":
        logger.warning("Script was generated by FALLBACK templates (see %s)", meta_path)

    logger.info("Generated script: %s (%d chars, source=%s)", output_path, len(script_md), script_source)
    return output_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Script Generator")
    parser.add_argument("--news", required=True, help="Path to news JSON file")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--type", default="daily", choices=["daily", "weekly"])
    args = parser.parse_args()
    path = generate_script(Path(args.news), Path(args.config), args.type)
    print(f"Output: {path}")


if __name__ == "__main__":
    main()
