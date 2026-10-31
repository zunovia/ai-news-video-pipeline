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

from cleaning import cleaning_short
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
{cleaning}

ポンテの口調ルール:
- やさしくてわかりやすい言葉を使う。専門用語は必ずかんたんな言葉で言い換える
- 「〜だよ！」「〜なんだ！」のような親しみやすい口調（ただし視聴者を子ども扱いしない）
- ちょっとドジで好奇心いっぱい。リアクションは具体的に（何にどう驚いたかを言う）

文字数ルール（1本60秒前後に収まるよう厳守。ふりがな括弧内は数えない）:
- hook_title: 15〜24文字
- intro: 50〜70文字
- explanation: 90〜120文字
- insight: 30〜55文字
- points: 3個（各8〜18文字）
- ending: 40〜60文字
- 長すぎるセリフは自動で末尾の文が削られてしまうので、必ず範囲内に収めること

フック見出し(hook_title)のルール（そのまま動画タイトルになる。最重要）:
- そのニュースで一番驚く事実を、固有名詞や数字入りで言い切る
- 内容に合えば「【衝撃】」「【朗報】」「【速報】」のような頭ラベルを付けてよい
- 「？」で終わる問いかけ型も可。記事タイトルの逐語コピーは禁止
- 必ず「何がどうなった」まで言い切った完結した文にする（英字の製品名で文字数を使い切り、
  「〜でローカル」のように途中で切れた見出しは禁止。製品名は短く、結論の言葉を優先する）
- 見出しには読みがなのカッコ書きを付けない（✕「Claude(クロード)が…」→ ○「Claudeが…」）
- 数字は元の事実どおりに使う（「500万行のコードを分析して493件発見」を
  「500万行のバグ」のように、数字の対象をすり替えない）

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

要点(points)のルール（画面に箇条書きで表示する図解カード。読み上げはしない）:
- そのニュースの要点をちょうど3個、各8〜18文字の体言止め・短文で書く
- 必ず「要約」「使える事実」に書かれている事実だけを使う。推測・誇張・意見は禁止
- 数字・固有名詞があれば優先して入れる（例: 「19項目中14項目で首位」「60言語に対応」）
- 制限・注意点（「一般提供は未定」「一部の組織のみ」など）が要約にあれば1個はそれにする
- 英語にカタカナ読みは付けない。カギ括弧・句点は使わない

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
points: ["FaceIDの開発者が新会社を設立", "脳の健康をAIで手軽にチェック", "高価な脳ドックの代わりを目指す"]

JSON形式のみで回答（説明文不要）。
"""

# Backward compatibility
SCRIPT_SYSTEM_PROMPT = SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(topic="AI", cleaning=cleaning_short())


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
# 切り詰め後の考察がこれ未満なら中身のない前置きだけとみなして捨てる
_INSIGHT_MIN_USABLE = 15
_ENDING_TTS_LIMIT = 70
_KEYWORD_DESC_TTS_LIMIT = 45
_MAX_KEYWORDS = 3
_HOOK_TITLE_MAX = 26
# 図解カードの要点（points）の上限。超過時はリトライ→それでも長い要点は捨てる
# （途中で切ると意味が壊れるため切り詰めない）
_POINT_MAX = 20
_POINTS_COUNT = 3
# 読点切り／過長見出しの切り詰め結果として採用する最小文字数（"Anthropic" 単体などを防ぐ）
_HOOK_TITLE_MIN_USABLE = 12
# これ未満の見出しは「製品名だけ」になりがちなのでLLMに作り直させる（切り詰めはしない）
_HOOK_TITLE_MIN_RETRY = 15
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


# 読みがなのカッコ書き（Claude(クロード)・漢字(かんじ)）。題名・要点には残さない
# （スライド表示時に ruby_dict で付け直されるため、データ側は素の文字列にする）
_READING_PARENS_RE = re.compile(r"[(（][ぁ-んァ-ヶー・ ]+[)）]")


def strip_reading_parens(text) -> str:
    """読みがなのカッコ書きを除去し、空白を正規化する."""
    t = _READING_PARENS_RE.sub("", str(text or ""))
    return re.sub(r"\s+", " ", t).strip()


def _clean_point(p) -> str:
    """要点1個を表示用に整える（空白正規化・LLMが付けた読み/句点/括弧を除去）."""
    t = strip_reading_parens(p)  # ルビ記法は表示時に付け直す
    return t.strip("「」『』。・-－ ")


def normalize_points(points) -> list[str]:
    """要点リストを検証・整形する。過長な要点は切らずに捨て、最大3個に揃える."""
    if not isinstance(points, list):
        return []
    out: list[str] = []
    for p in points:
        t = _clean_point(p)
        if t and len(t) <= _POINT_MAX and t not in out:
            out.append(t)
    return out[:_POINTS_COUNT]


def _article_issues(i: int, a: dict) -> list[str]:
    """記事1件の台本の問題（文字数・要点数）をLLMへのフィードバック文で返す."""
    issues = []
    # 過長な見出しは後段で機械的に切り詰めるしかなく語の途中で切れるため、
    # 文字数超過はLLMに作り直させる（読みがなのカッコ書きは数えない＝表示される長さ）
    hook = str(a.get("hook_title") or "")
    n = len(strip_reading_parens(hook))
    if n > _HOOK_TITLE_MAX:
        issues.append(f"ニュース{i}のhook_titleが長すぎます（{n}文字）。"
                      "15〜24文字の、途中で切れない完結した見出しに作り直してください")
    elif 0 < n < _HOOK_TITLE_MIN_RETRY:
        # 「Gemini 4 Argon」のような製品名だけの見出しはフックにならない
        issues.append(f"ニュース{i}のhook_titleが短すぎます（{n}文字）。"
                      "製品名だけでなく「何がどうなった」まで言い切る15〜24文字の見出しにしてください")
    if _READING_PARENS_RE.search(hook):
        issues.append(f"ニュース{i}のhook_titleに読みがなのカッコ書きがあります。"
                      "動画タイトルになるので読みがなを外してください")
    n = _tts_len(a.get("intro", ""))
    if n > _INTRO_TTS_LIMIT:
        issues.append(f"ニュース{i}のintroが長すぎます（読み上げ{n}文字）。50〜70文字に短縮してください")
    n = _tts_len(a.get("explanation", ""))
    if n > _EXPLANATION_TTS_LIMIT:
        issues.append(f"ニュース{i}のexplanationが長すぎます（読み上げ{n}文字）。90〜120文字に短縮してください")
    n = _tts_len(a.get("insight", ""))
    if n > _INSIGHT_TTS_LIMIT:
        issues.append(f"ニュース{i}のinsightが長すぎます（読み上げ{n}文字）。30〜55文字に短縮してください")
    pts = a.get("points")
    if not isinstance(pts, list) or len(pts) < _POINTS_COUNT:
        issues.append(f"ニュース{i}のpointsが3個ありません。要約の事実から8〜18文字の要点を3個作ってください")
    else:
        for p in pts:
            if len(_clean_point(p)) > _POINT_MAX:
                issues.append(f"ニュース{i}のpoints「{p}」が長すぎます。8〜18文字に短縮してください")
    return issues


def _global_issues(result: dict) -> list[str]:
    """記事に属さない項目（エンディング）の問題を返す."""
    n = _tts_len(result.get("ending", ""))
    if n > _ENDING_TTS_LIMIT:
        return [f"endingが長すぎます（読み上げ{n}文字）。40〜60文字に短縮してください"]
    return []


def _validate_script_lengths(result: dict) -> list[str]:
    """セリフの読み上げ文字数を検証し、超過項目のフィードバック文を返す."""
    issues = []
    for i, a in enumerate(result.get("articles", []), 1):
        issues += _article_issues(i, a)
    return issues + _global_issues(result)


def _cap_script_lengths(result: dict) -> dict:
    """リトライ後も超過しているセリフを文単位トリムで強制的に上限内へ収める."""
    for a in result.get("articles", []):
        a["points"] = normalize_points(a.get("points"))
        a["intro"] = _trim_serif_to_limit(a.get("intro", ""), _INTRO_TTS_LIMIT)
        a["explanation"] = _trim_serif_to_limit(a.get("explanation", ""), _EXPLANATION_TTS_LIMIT)
        if a.get("insight"):
            a["insight"] = _trim_serif_to_limit(a["insight"], _INSIGHT_TTS_LIMIT)
            # 切り詰めで「ポンテはこう思うな。」のような前置きだけが残ったら考察ごと捨てる
            if _tts_len(a["insight"]) < _INSIGHT_MIN_USABLE:
                a["insight"] = ""
    if result.get("ending"):
        result["ending"] = _trim_serif_to_limit(result["ending"], _ENDING_TTS_LIMIT)
    keywords = result.get("keywords", [])[:_MAX_KEYWORDS]
    for kw in keywords:
        if kw.get("desc"):
            kw["desc"] = _trim_serif_to_limit(kw["desc"], _KEYWORD_DESC_TTS_LIMIT)
    result["keywords"] = keywords
    return result


# 1記事の台本が持つキー（部分修正のマージ対象）
_ARTICLE_KEYS = ("label", "hook_title", "intro", "explanation", "insight", "points")


def _article_source_block(i: int, article: dict) -> str:
    """LLMに渡す元記事の材料（タイトル・要約・使える事実）."""
    # リッチ要約（summary_ja_long/facts/kid_angle）があれば優先使用（旧JSON互換）
    summary = (article.get("summary_ja_long")
               or article.get("summary_ja", article.get("summary", "")))
    lines = [
        f"ニュース{i}:",
        f"  タイトル: {article.get('title_ja', article.get('title', ''))}",
        f"  要約: {summary}",
    ]
    facts = article.get("facts")
    if isinstance(facts, list) and facts:
        lines.append("  使える事実: " + " / ".join(str(f) for f in facts))
    if article.get("kid_angle"):
        lines.append(f"  生活・仕事との関係: {article['kid_angle']}")
    lines.append(f"  ソース: {article.get('source', '')}")
    return "\n".join(lines)


def _parse_json_object(text: str) -> dict:
    """LLM応答から最初の { 〜最後の } をJSONとして取り出す."""
    text = (text or "").strip()
    if "{" not in text:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def _merge_repair(result: dict, repaired: dict) -> int:
    """部分修正の応答を台本にマージする。問題が減らない修正は採用しない.

    記事ごとに「修正後の問題数 <= 修正前」のときだけ置き換えるので、
    1か所直すと別の記事が崩れる（全体再生成の）もぐら叩きが起きない。
    Returns: 採用した修正の件数。
    """
    accepted = 0
    arts = result.get("articles", [])
    for item in repaired.get("articles") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not 1 <= idx <= len(arts):
            continue
        before = arts[idx - 1]
        cand = dict(before)
        cand.update({k: v for k, v in item.items() if k in _ARTICLE_KEYS and v})
        if len(_article_issues(idx, cand)) <= len(_article_issues(idx, before)):
            arts[idx - 1] = cand
            accepted += 1
    ending = repaired.get("ending")
    if ending and isinstance(ending, str):
        cand = dict(result, ending=ending)
        if len(_global_issues(cand)) <= len(_global_issues(result)):
            result["ending"] = ending
            accepted += 1
    return accepted


def _repair_script(client, system_prompt: str, result: dict, articles: list[dict],
                   max_tokens: int, max_rounds: int) -> dict:
    """問題のある記事・項目だけをLLMに直させる（問題のない部分はそのまま残す）."""
    for rnd in range(1, max_rounds + 1):
        bad = {i: iss for i, a in enumerate(result.get("articles", []), 1)
               if (iss := _article_issues(i, a))}
        gl = _global_issues(result)
        if not bad and not gl:
            return result
        logger.info("Script repair round %d/%d: %s", rnd, max_rounds,
                    " / ".join(sum(bad.values(), []) + gl))
        parts = []
        for i, iss in bad.items():
            cur = {k: result["articles"][i - 1].get(k, "") for k in _ARTICLE_KEYS}
            parts.append(
                _article_source_block(i, articles[i - 1] if i - 1 < len(articles) else {})
                + "\n  現在の台本: " + json.dumps(dict(index=i, **cur), ensure_ascii=False)
                + "\n  直すこと:\n  - " + "\n  - ".join(iss)
            )
        if gl:
            parts.append(f"エンディング（現在）: {result.get('ending', '')}\n  直すこと:\n  - "
                         + "\n  - ".join(gl))
        prompt = (
            "以下の台本には問題があります。「直すこと」に書かれた項目だけを直してください。\n"
            "直すことに書かれていない項目は一字一句変えずに、そのまま返してください。\n"
            "事実は各ニュースの要約・使える事実だけを使ってください。\n\n"
            + "\n\n".join(parts)
            + '\n\nJSON形式で回答（直したニュースだけ。indexは上と同じ番号）:\n'
            '{"articles": [{"index": 1, "label": "...", "hook_title": "...", "intro": "...",'
            ' "explanation": "...", "insight": "...", "points": ["...", "...", "..."]}]'
            + (', "ending": "..."' if gl else "") + "}"
        )
        try:
            repaired = _parse_json_object(client.generate(system_prompt, prompt, max_tokens))
        except Exception as e:
            logger.warning("Script repair round %d failed (keeping current script): %s", rnd, e)
            break
        if _merge_repair(result, repaired) == 0:
            logger.info("Script repair round %d: no improving fix accepted", rnd)
    return result


def generate_article_scripts(
    articles: list[dict],
    llm_config: dict,
    topic: str = "AI",
    max_retries: int = 2,
    max_repair_rounds: int = 3,
) -> dict:
    """Generate intro and explanation scripts for each article.

    1回生成したあとは、問題のある記事・項目だけを部分修正する（全体の再生成はしない。
    以前は全体を作り直していたため、直すたびに別の記事が崩れて問題数が増減していた）。
    max_retries は生成自体の失敗（JSONが壊れている等）のリトライ回数。
    """
    from api_utils import LLMClient
    import time

    client = LLMClient(llm_config)
    max_tokens = llm_config.get(llm_config.get("provider", "gemini"), {}).get("max_tokens_script", 8000)
    system_prompt = SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(topic=topic, cleaning=cleaning_short())

    prompt_parts = [_article_source_block(i, a) for i, a in enumerate(articles, 1)]

    user_prompt = (
        "以下のニュースそれぞれについて、ポンテの口調でフック見出し・紹介文・解説文・考察を生成してください。\n"
        "また、エンディングのセリフも1つ生成してください。\n"
        "さらに、ニュース全体から重要な専門用語をちょうど3個抽出し、keywordsとして返してください。\n"
        "漢字にふりがなは付けないでください（自動付与されます）。"
        "英語の言葉にはカタカナ読み（例: Siri(シリ)）を付けてください（ただしhook_titleとpointsには付けない）。\n\n"
        + "\n\n".join(prompt_parts)
        + '\n\nJSON形式で回答:\n'
        '{\n'
        '  "articles": [\n'
        '    {"label": "ニュースの内容がわかる12〜18文字の短い見出し（「ニュース1」のような番号だけのラベルは禁止）",'
        ' "hook_title": "動画タイトルになるフック見出し（15〜24文字。固有名詞・数字入りで言い切る。読みがな不要）",'
        ' "intro": "紹介セリフ（50〜70文字。挨拶禁止・1文目で一番驚く事実を言い切る）",'
        ' "explanation": "解説セリフ（90〜120文字）",'
        ' "insight": "ポンテの考察（30〜55文字。独自の意見・ツッコミ・予想）",'
        ' "points": ["要点1（8〜18文字・事実のみ）", "要点2", "要点3"]}\n'
        '  ],\n'
        '  "ending": "エンディングセリフ（40〜60文字）",\n'
        '  "keywords": [\n'
        '    {"term": "用語（漢字にふりがな不要。英語にはカタカナ読み）", "reading": "よみがな", "desc": "ポンテ口調の解説（25〜35文字）"}\n'
        '  ]\n'
        '}'
    )

    last_error: Exception | None = None
    result: dict | None = None
    for attempt in range(max_retries + 1):
        text = ""
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info("Script generation retry %d/%d after %ds...", attempt, max_retries, wait)
                time.sleep(wait)
            text = client.generate(system_prompt, user_prompt, max_tokens)
            result = _parse_json_object(text)
            if not result.get("articles"):
                raise ValueError("Response JSON has no articles")
            break
        except Exception as e:
            result = None
            last_error = e
            logger.warning(
                "Script generation attempt %d/%d failed: %s — raw response head: %r",
                attempt + 1, max_retries + 1, e, (text or "")[:500],
            )
    if result is None:
        raise ValueError(f"Script generation failed after {max_retries + 1} attempts: {last_error}")

    # 問題のある項目だけ部分修正 → それでも残る超過は文単位トリムで強制的に収める
    result = _repair_script(client, system_prompt, result, articles, max_tokens, max_repair_rounds)
    remaining = _validate_script_lengths(result)
    if remaining:
        logger.warning("Script issues remain after repair — trimming to hard caps: %s",
                       " / ".join(remaining))
    return _cap_script_lengths(result)


# ---------------------------------------------------------------------------
# Fact check（生成役とは別の確認役。元記事の要約・事実だけと照合する）
# ---------------------------------------------------------------------------

FACTCHECK_SYSTEM_PROMPT = """\
あなたはニュース動画の事実確認担当です。台本を書いた人とは別の、独立した確認役です。
__CLEANING__
台本の各項目を、与えられた「要約」「使える事実」だけと照合し、次に当たるものを指摘してください:
- 元記事にない事実・数字・固有名詞・会社名
- 元記事と矛盾する内容（数字の対象のすり替え、主語・対象の取り違え。
  例: 「500万行のコードを分析し493件の問題を発見」を「500万行のバグ」と言う、
  マウスの脳細胞データの研究を「脳を解析」と人間の話のように言う）
- 誇張・断定しすぎ（元記事が「可能性」「研究段階」「限定提供」「報告」なのに、確定・一般化して言う。
  研究成果を「治療法開発」「実用化」に飛躍させる、「数カ月〜1年」を「1年」と言い切る など）
- 会計用語の取り違え（「負のフリーキャッシュフロー」「支出」「資金流出」を「赤字」と言う、
  売上を利益と言う など。「赤字」は元記事が損失と書いているときだけ正しい）
- 合計の数字を1社の数字のように言う（「4社で7400億ドル」を「Googleが7400億ドル」と言う）
指摘しないもの: 口調、言い回し、わかりやすい言い換え、たとえ話、カッコ内の読みがな、
文字数や要点の個数などの形式（形式は別の仕組みで確認する）。
確認して問題がなかった項目は issues に入れない（「問題ない」「許容範囲」と書く指摘は出さない）。

指摘には重さ(severity)を付ける:
- "error": 視聴者が事実を誤解する（数字・対象・主語の誤り、元記事にない事実、可能性や研究段階を確定と言う）
- "minor": 事実としては正しいが、細部を省いている・より正確に言える・ニュアンスが少し違う
元記事のすべての事実を台本に入れる必要はない。省略は誤りではない（minor にもしない）。
同じ項目に複数の問題があるときは1件にまとめ、fix はそれを全部直した全文にする。
考察(insight)は意見なので、意見であること自体は問題にしない
（ただし意見の中に、事実として誤った前提や元記事にない断定があれば指摘する）。

修正案(fix)のルール:
- その項目の全文を置き換える修正後の文。元の口調と長さ（±10文字）を保ち、問題の箇所だけ直す
- 正しい数字・固有名詞・言い切りの強さは残す（曖昧でぼんやりした文に薄めない）
- hook_title: 15〜24文字の完結した見出し。読みがなのカッコ書きを付けない
- points: ちょうど3個の配列。各8〜18文字の体言止め。読みがなを付けない
- 漢字にふりがなを付けない
- 直せない（元記事に材料がない）場合は fix を空文字にする

JSONのみで回答:
{"articles": [{"index": 1, "issues": [{"field": "hook_title|intro|explanation|insight|points",
 "severity": "error|minor", "problem": "何がどう違うか（根拠となる元記事の記述も書く）",
 "fix": "修正後の全文（pointsは配列）"}]}]}
問題がないニュースは "issues": [] にする。
"""

_FACTCHECK_FIELDS = ("hook_title", "intro", "explanation", "insight", "points")
_SERIF_LIMITS = {"intro": _INTRO_TTS_LIMIT, "explanation": _EXPLANATION_TTS_LIMIT,
                 "insight": _INSIGHT_TTS_LIMIT}


def _factcheck_call(client, script_articles: list[dict], articles: list[dict],
                    indices: list[int], max_tokens: int) -> dict[int, list[dict]]:
    """指定記事を確認役に照合させ、{記事番号: [指摘]} を返す."""
    parts = []
    for i in indices:
        sa = script_articles[i - 1]
        script = {k: sa.get(k, "") for k in _FACTCHECK_FIELDS}
        parts.append(_article_source_block(i, articles[i - 1])
                     + "\n  台本: " + json.dumps(script, ensure_ascii=False))
    prompt = "次の台本を元記事と照合してください。\n\n" + "\n\n".join(parts)
    data = _factcheck_generate(client, prompt, max_tokens)
    out: dict[int, list[dict]] = {i: [] for i in indices}
    for item in data.get("articles") or []:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError, AttributeError):
            continue
        if idx in out:
            # 自動修正・要確認にするのは誤り(error)だけ。細部(minor)まで直させると
            # 再確認のたびに数字や固有名詞が削られ、見出しがぼんやり劣化する（2026-10-23 実測）
            out[idx] = [x for x in (item.get("issues") or [])
                        if isinstance(x, dict) and x.get("field") in _FACTCHECK_FIELDS
                        and x.get("severity", "error") != "minor"
                        and not _is_format_only(x)]
    return out


# 事実ではなく形式（個数・文字数）だけを言う指摘。形式はコード側で検証済みなので捨てる
# （プロンプトで禁じても確認役が出すことがあるため）
_FORMAT_ONLY_RE = re.compile(r"個数|文字数|\d+個(?:しか|だが|必要|必須|ない|に満た)|ルールでは|配列")


def _is_format_only(issue: dict) -> bool:
    return not issue.get("fix") and bool(_FORMAT_ONLY_RE.search(str(issue.get("problem", ""))))


def _factcheck_generate(client, prompt: str, max_tokens: int) -> dict:
    """確認役を呼ぶ（温度0で判断を安定させ、JSONが崩れたら1回だけやり直す）."""
    system = FACTCHECK_SYSTEM_PROMPT.replace("__CLEANING__", cleaning_short())
    last: Exception | None = None
    for _ in range(2):
        try:
            return _parse_json_object(client.generate(system, prompt, max_tokens, temperature=0))
        except ValueError as e:  # json.JSONDecodeError を含む
            last = e
            logger.info("Fact check: malformed JSON, retrying once: %s", e)
    raise ValueError(f"fact check JSON unreadable: {last}")


def _keep_readings(text: str, script_article: dict) -> str:
    """修正文に、元のセリフの英語読み（OpenAI(オープンエーアイ)等）を引き継ぐ.

    確認役の修正案は読みを落としがちで、落ちると音声が英字読みになるため。
    """
    from slide_generator import _apply_ruby_dict, _extract_ruby_dict  # 循環import回避
    src = " ".join(str(script_article.get(k, "")) for k in ("intro", "explanation", "insight"))
    return _apply_ruby_dict(text, _extract_ruby_dict(src))


def _fix_is_usable(field: str, fix) -> bool:
    """修正案が文字数・形式のルールを守っているか（守らない修正は自動適用しない）."""
    if field == "points":
        return isinstance(fix, list) and len(normalize_points(fix)) == _POINTS_COUNT
    if not isinstance(fix, str) or not fix.strip():
        return False
    if field == "hook_title":
        n = len(strip_reading_parens(fix))
        return _HOOK_TITLE_MIN_RETRY <= n <= _HOOK_TITLE_MAX
    return 0 < _tts_len(fix) <= _SERIF_LIMITS[field]


def _field_rule(field: str) -> str:
    if field == "hook_title":
        return f"{_HOOK_TITLE_MIN_RETRY}〜24文字の完結した見出し（読みがな不要）"
    if field == "points":
        return "ちょうど3個の配列、各8〜18文字"
    return f"読み上げ{_SERIF_LIMITS[field]}文字以内（カッコ内の読みは数えない）"


def _shorten_fixes(client, items: list[dict], max_tokens: int) -> None:
    """文字数・形式ルールを外れた修正案を、意味を保ったまま規定内に直させる（items を上書き）."""
    lines = [
        f'{k}. ニュース{it["index"]}の{it["field"]}（ルール: {_field_rule(it["field"])}）: '
        + json.dumps(it["fix"], ensure_ascii=False)
        for k, it in enumerate(items, 1)
    ]
    prompt = (
        "次の修正案は文字数・形式のルールを守っていません。"
        "事実と意味を変えずに、ルールの範囲に収まるよう書き直してください。\n\n"
        + "\n".join(lines)
        + '\n\nJSONのみで回答（番号は上と同じ）: {"fixes": [{"no": 1, "fix": "書き直した全文（pointsは配列）"}]}'
    )
    data = _factcheck_generate(client, prompt, max_tokens)
    for f in data.get("fixes") or []:
        try:
            k = int(f.get("no"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= k <= len(items) and f.get("fix"):
            items[k - 1]["fix"] = f["fix"]


def factcheck_and_fix(script_data: dict, articles: list[dict], llm_config: dict,
                      auto_fix: bool = True, client=None, max_fix_rounds: int = 2) -> dict:
    """台本を元記事と照合し、誤り・誇張は修正案で自動修正する.

    確認 → 修正 → 修正した記事だけ再確認、を最大 max_fix_rounds 周回す
    （確認役の修正文に新しい誇張が入ることがあるため、修正後も必ず確認を通す）。
    ルール外の長さの修正案は1回だけ書き直させてから適用する。
    Returns: script_meta に保存する報告
      {"status": ok|fixed|unresolved|error, "articles": [{index, fixed, unresolved}], "error"}
    """
    if client is None:
        from api_utils import LLMClient
        client = LLMClient(llm_config)
    max_tokens = llm_config.get(llm_config.get("provider", "gemini"), {}).get(
        "max_tokens_factcheck", 3000)
    sarts = script_data.get("articles", [])
    n = min(len(sarts), len(articles))
    report = {i: {"index": i, "fixed": [], "unresolved": []} for i in range(1, n + 1)}
    rounds = max_fix_rounds if auto_fix else 0
    pending = list(range(1, n + 1))
    checked_once = False
    try:
        for rnd in range(rounds + 1):
            found = _factcheck_call(client, sarts, articles, pending, max_tokens)
            checked_once = True
            issues = [dict(iss, index=i) for i, lst in found.items() for iss in lst]
            if rnd == rounds:  # 最後の確認: 残った指摘は人の確認へ
                for iss in issues:
                    report[iss["index"]]["unresolved"].append(
                        {"field": iss["field"], "problem": iss.get("problem", ""),
                         "suggestion": iss.get("fix") or ""})
                break
            # 修正案があるのにルール外の長さ → まとめて1回書き直させる
            too_long = [iss for iss in issues
                        if iss.get("fix") and not _fix_is_usable(iss["field"], iss["fix"])]
            if too_long:
                try:
                    _shorten_fixes(client, too_long, max_tokens)
                except Exception as e:
                    logger.warning("Fact check: shortening fixes failed: %s", e)
            touched: list[int] = []
            done_fields: set[tuple[int, str]] = set()
            for iss in issues:
                i, field, fix = iss["index"], iss["field"], iss.get("fix")
                # 同じ項目への2件目の修正案は元の文から作られており、1件目の修正を
                # 打ち消しうるので適用しない（再確認で残っていれば改めて指摘される）
                if (i, field) in done_fields:
                    continue
                if _fix_is_usable(field, fix):
                    done_fields.add((i, field))
                    before = sarts[i - 1].get(field, "")
                    if field == "points":
                        sarts[i - 1][field] = normalize_points(fix)
                    elif field == "hook_title":
                        sarts[i - 1][field] = strip_reading_parens(fix)
                    else:
                        sarts[i - 1][field] = _keep_readings(fix.strip(), sarts[i - 1])
                    report[i]["fixed"].append({"field": field, "problem": iss.get("problem", ""),
                                               "before": before, "after": sarts[i - 1][field]})
                    if i not in touched:
                        touched.append(i)
            # 直せなかった指摘: その記事を再確認しない（＝直していない）なら、ここで要確認に回す
            # （直した記事の指摘は次の再確認で、残っていれば改めて報告される）
            for iss in issues:
                if iss["index"] not in touched and not _fix_is_usable(iss["field"], iss.get("fix")):
                    report[iss["index"]]["unresolved"].append(
                        {"field": iss["field"], "problem": iss.get("problem", ""),
                         "suggestion": iss.get("fix") or ""})
            if not touched:
                break
            pending = touched
    except Exception as e:
        if not checked_once:
            logger.warning("Fact check failed (script kept as is — review manually): %s", e)
            return {"status": "error", "error": str(e), "articles": list(report.values())}
        # 再確認だけ失敗: ここまでの修正は残し、再確認できなかった記事を要確認にする
        logger.warning("Fact check re-check failed (fixes kept; review manually): %s", e)
        for i in pending:
            report[i]["unresolved"].append({"field": "", "problem": f"修正後の再確認に失敗: {e}",
                                            "suggestion": ""})

    arts = list(report.values())
    if any(a["unresolved"] for a in arts):
        status = "unresolved"
    elif any(a["fixed"] for a in arts):
        status = "fixed"
    else:
        status = "ok"
    logger.info("Fact check: %s (fixed %d, unresolved %d)", status,
                sum(len(a["fixed"]) for a in arts), sum(len(a["unresolved"]) for a in arts))
    return {"status": status, "error": "", "articles": arts}


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


def _cut_at_word_boundary(t: str, limit: int) -> str:
    """t を limit 文字以内の最後の形態素境界で切る（末尾の助詞・記号は落とす）.

    英数字の連なり（"GPT-5" 等）の内部は境界として扱わない。
    形態素解析が使えない、または見出しとして短すぎる結果になる場合は空文字を返す。
    """
    from ruby_engine import _tokenize_fugashi

    tokens = _tokenize_fugashi(t)
    if not tokens:
        return ""
    ascii_re = re.compile(r"[A-Za-z0-9\-\.]")
    kept: list[tuple[str, str]] = []
    best: list[tuple[str, str]] = []
    pos = 0
    for surface, _reading, pos1 in tokens:
        if pos + len(surface) > limit:
            break
        pos += len(surface)
        kept.append((surface, pos1))
        if not (pos < len(t) and ascii_re.match(t[pos - 1]) and ascii_re.match(t[pos])):
            best = list(kept)
    # 「…14項目で」「…AI企業を」のような助詞止め・記号止めは見出しとして据わりが悪い
    while best and (best[-1][1] in ("助詞", "補助記号") or not best[-1][0].strip()):
        best.pop()
    out = "".join(s for s, _ in best).rstrip("、。・:： ")
    return out if len(out) >= _HOOK_TITLE_MIN_USABLE else ""


def _truncate_hook_title(t: str, limit: int) -> str:
    """フック見出しの本体を切り詰める（括弧の整合は呼び出し側で担保）."""
    if len(t) <= limit:
        return t
    for i, ch in enumerate(t):
        if ch in "？！?!" and 6 <= i <= limit:
            return t[: i + 1]
    for i, ch in enumerate(t):
        # 読点で切る場合も見出しとして成立する長さ（固有名詞1語だけにならない）を要求する
        if ch in "。、" and _HOOK_TITLE_MIN_USABLE <= i <= limit:
            return t[:i]
    # 日本語の語の途中で切れないよう、形態素境界で切る（"…で首" "…立ち上" を防ぐ）
    # 形態素解析が使えない／短くなりすぎる場合は従来の文字数カットに落とす。
    cut = _cut_at_word_boundary(t, limit) or t[:limit]
    # 英単語の途中で切れた場合は単語境界まで戻す（"GPT-" のような切断を防ぐ）。
    # ただし戻した結果が短くなりすぎる場合（先頭が短い非ASCII語だけになる等）は
    # 見出しとして成立する長さを優先し、単語途中の切断を許容する。
    if (re.match(r"[A-Za-z0-9\-\.]", t[len(cut)])
            and re.search(r"[A-Za-z0-9\-\.]$", cut)):
        trimmed = re.sub(r"[A-Za-z0-9\-\.]+$", "", cut)
        if trimmed and len(trimmed.rstrip("、。・:： ")) >= _HOOK_TITLE_MIN_USABLE:
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
    """各記事の hook_title を検証・補正する（欠落・過長は記事タイトルから決定的に生成）.

    読みがなのカッコ書き（Claude(クロード)等）は動画タイトルにそのまま出るため必ず外す
    （スライドの大見出しにはセリフ由来のルビ辞書で付け直される）。
    """
    for sa, na in zip(script_data.get("articles", []), news_articles):
        hook = strip_reading_parens(sa.get("hook_title"))
        if not hook or len(hook) > _HOOK_TITLE_MAX:
            # 過長なLLM見出しは記事タイトルより情報量が多いので、捨てずに切り詰めを優先する
            fallback = _fallback_hook_title(hook) if hook else ""
            if len(fallback) < _HOOK_TITLE_MIN_USABLE:
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

    # 1ニュース=1本Shorts運用では、動画にする上位N本だけ台本を作る
    # （使わない記事の生成・修正にLLMを使わない。news JSON 自体は全件のまま）
    daily_enabled = config.get("pipeline", {}).get("daily_video_enabled", True)
    shorts_cfg = config.get("shorts", {})
    if not daily_enabled and shorts_cfg.get("per_topic", False):
        top_n = shorts_cfg.get("topics_per_day", 3)
        if len(news_data.get("articles", [])) > top_n:
            logger.info("Script: generating only the top %d of %d articles (per-topic Shorts)",
                        top_n, len(news_data["articles"]))
            news_data = dict(news_data, articles=news_data["articles"][:top_n])

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

    # 事実確認（生成役とは別の確認役）。誤り・誇張は修正案で自動修正し、
    # 直せなかったものは script_meta / review レポートに「要確認」として残す
    fc_cfg = config.get("script", {}).get("factcheck", {})
    if script_source != "llm":
        factcheck = {"status": "skipped", "error": "fallback script", "articles": []}
    elif not fc_cfg.get("enabled", True):
        factcheck = {"status": "skipped", "error": "disabled", "articles": []}
    else:
        factcheck = factcheck_and_fix(script_data, news_data["articles"], llm_cfg,
                                      auto_fix=fc_cfg.get("auto_fix", True))
        script_data = normalize_hook_titles(script_data, news_data["articles"])

    # 3分尺の最終保証は16:9まとめ動画専用。1ニュース=1本Shorts運用
    # （pipeline.daily_video_enabled=false）では各セリフの個別上限だけで
    # 1本60秒前後が保証されるため、全体トリムはスキップする
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
                # 図解カード用の要点（スライドに箇条書き表示。読み上げはしない）
                "points": normalize_points(a.get("points")),
            }
            for a in script_data.get("articles", [])
        ],
        # 事実確認の結果（status: ok / fixed / unresolved / error / skipped）
        "factcheck": factcheck,
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
