"""改良実装のオフライン検証スクリプト（LLM/ネットワーク不要）.

検証項目:
1. daily_script.j2 render → parse_slides 往復（label短縮インデックス含む）
2. select_shorts_slides が slide_generator / video_builder で同一結果
3. _safe_label / _short_title_label のルビ括弧切断処理
4. article_extractor のヒューリスティック抽出
5. generate_metadata / generate_shorts_metadata の形式
6. cluster正規化ロジック（LLM呼び出し部はモック）
7. hook_title の括弧修復（未閉じ括弧・無限ループ・空タイトル回帰）
"""

import json
import sys
from pathlib import Path

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    # detail は失敗時のみ表示する（PASS行に「TIMEOUT」等の文言が出て紛らわしいため）
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- 1. render_script → parse_slides round trip -----------------------------
from script_generator import render_script, _safe_label, add_ruby_to_text
from slide_generator import parse_slides, classify_slide

news_data = {
    "articles": [
        {"title_ja": f"テストニュース{i}のタイトルですよ長いです", "title": f"Test {i}",
         "source": "TestFeed", "link": f"https://example.com/{i}"}
        for i in range(1, 6)
    ],
}
script_data = {
    "articles": [
        {"label": f"テスト見出し{i}", "intro": f"紹介セリフ{i}だよ！",
         "explanation": f"解説(かいせつ)セリフ{i}なんだ！"}
        for i in range(1, 6)
    ],
    "ending": "今日はここまで！また明日(あした)ね！",
    "keywords": [
        {"term": "生成(せいせい)AI", "desc": "新しく作り出せるAIだよ！"},
        {"term": "API", "desc": "プログラム同士の約束事だよ！"},
        {"term": "ロボット", "desc": "動く機械だよ！"},
    ],
}

md = render_script(news_data, script_data, Path("templates/daily_script.j2"), "20260611", topic="AI")
tmp = Path("state/_verify_script.md")
tmp.write_text(md, encoding="utf-8")
slides = parse_slides(tmp)

check("台本レンダー→パース: スライド数=14", len(slides) == 14, f"got {len(slides)}")
check("オープニング検出", slides[0]["is_opening"])
check("インデックス検出", slides[1]["is_index"])
check("エンディング検出", slides[-1]["is_ending"])
check("キーワード検出", slides[-2]["is_keyword"])

index_serif = slides[1]["serif"]
check("インデックスにlabel使用", "テスト見出し3" in index_serif, index_serif[:80])
check("インデックスにフルタイトル不使用", "タイトルですよ長いです" not in index_serif)
check("インデックスセリフ短縮(<200字)", len(index_serif) < 200, f"{len(index_serif)}字")

news_slides = [s for s in slides if classify_slide(s, len(slides)) == "news"]
check("ニューススライド10枚", len(news_slides) == 10, f"got {len(news_slides)}")
point_titles = [s for s in news_slides if s["title"].endswith("ポイント")]
check("ポイントスライド5枚（video_builderユニット化互換）", len(point_titles) == 5)

# --- 2. select_shorts_slides 一貫性 ------------------------------------------
from video_builder import select_shorts_slides

sel_a = select_shorts_slides(slides, 170)
sel_b = select_shorts_slides(slides, 170)
check("Shorts選定が決定的", [s["number"] for s in sel_a] == [s["number"] for s in sel_b])
check("Shorts選定: インデックス除外", all(not s.get("is_index") for s in sel_a))
check("Shorts選定: オープニング+エンディング保持",
      sel_a[0].get("is_opening") and sel_a[-1].get("is_ending"))

# --- 3. ラベル切断処理 --------------------------------------------------------
check("_safe_label: ルビ括弧切断除去",
      _safe_label("人工知能(じんこうちのう)が大変化する未来のはなし", 12) == "人工知能",
      repr(_safe_label("人工知能(じんこうちのう)が大変化する未来のはなし", 12)))
check("_safe_label: 短文そのまま", _safe_label("短い見出し") == "短い見出し")

from youtube_uploader import _short_title_label
check("_short_title_label: 18字切断",
      len(_short_title_label("これはとても長いニュースタイトルでして十八文字を超えます")) <= 18)

# --- 4. article_extractor -----------------------------------------------------
from article_extractor import extract_main_text

sample_html = """
<html><head><script>var x=1;</script><style>.a{}</style></head>
<body><nav><p>ナビゲーションメニューのリンク集がここに並んでいますがこれは本文ではないので除去されるべき要素です</p></nav>
<article>
<p>これは記事の本文の最初の段落です。実際のニュース記事と同じ程度の長さを持つ段落で、本文として抽出されるべきテキストがここに書かれています。この段落は六十文字の閾値を確実に超えています。</p>
<p>短い段落</p>
<p>これは記事の本文の二番目の段落です。こちらも実際の記事と同じ程度の長さがあり、本文として抽出されるべき内容を含んでいます。ヒューリスティック抽出の検証のための十分な長さです。</p>
</article>
<footer><p>フッターにあるコピーライト表記などの長い文章もここにありますがこれも本文ではないため除去されるべきです</p></footer>
</body></html>
"""
text = extract_main_text(sample_html)
check("本文抽出: 記事段落を抽出", "最初の段落" in text and "二番目の段落" in text)
check("本文抽出: nav/footer除去", "ナビゲーション" not in text and "コピーライト" not in text)
check("本文抽出: 短段落除外", "短い段落" not in text)
check("本文抽出: 空HTML→空文字", extract_main_text("") == "")

# --- 5. metadata ---------------------------------------------------------------
from youtube_uploader import generate_metadata, generate_shorts_metadata

config = json.loads(Path("config.json").read_text(encoding="utf-8"))
nd = {"articles": [{"title_ja": "OpenAIが新しいAIモデルを発表して世界が驚いた", "link": "https://x.com/1"},
                   {"title_ja": "記事2", "link": "https://x.com/2"}]}
meta = generate_metadata("20260611", config, "daily", news_data=nd)
check("通常タイトル: トップ記事label入り", "OpenAI" in meta["title"], meta["title"])
check("通常タイトル: ハッシュタグなし", "#" not in meta["title"], meta["title"])
check("通常タイトル: ほかN本", "ほか1本" in meta["title"])
check("通常説明文: ハッシュタグあり", "#AI" in meta["description"])
check("通常説明文: 音声クレジット（config.tts.credit）", config.get("tts", {}).get("credit", "") in meta["description"])
check("通常メタ: kind=daily", meta.get("kind") == "daily")
check("タイトル100字以内", len(meta["title"]) <= 100, f"{len(meta['title'])}字")

smeta = generate_shorts_metadata("20260611", config, news_data=nd)
check("Shortsタイトル: #shorts維持", "#shorts" in smeta["title"], smeta["title"][:80])
check("Shortsメタ: kind=shorts", smeta.get("kind") == "shorts")

meta_empty = generate_metadata("20260611", config, "daily", news_data=None)
check("通常タイトル: news_dataなしでも生成", "AIニュース" in meta_empty["title"], meta_empty["title"])

# --- 6. cluster正規化（LLMモック） ---------------------------------------------
import news_collector as nc


class _FakeClient:
    def __init__(self, response):
        self._r = response

    def generate(self, *a, **k):
        return self._r


articles = [
    {"title_ja": f"記事{i}", "title": f"a{i}", "source": f"src{(i - 1) % 3}", "score": 10 - i}
    for i in range(1, 7)
]
# クラスタ: 1と4が同一トピック、5が欠落（→単独補完されるべき）、99は範囲外
fake_response = json.dumps({"clusters": [[1, 4], [2], [3, 99], [6]]})

_orig = nc.LLMClient if hasattr(nc, "LLMClient") else None
import api_utils
_orig_client = api_utils.LLMClient
api_utils.LLMClient = lambda cfg: _FakeClient(fake_response)
try:
    result = nc.cluster_topics_with_llm(articles, {"provider": "gemini"}, pool_size=6)
finally:
    api_utils.LLMClient = _orig_client

titles = [a["title_ja"] for a in result]
check("クラスタ: 全記事保持（demoted含む）", sorted(titles) == sorted(a["title_ja"] for a in articles))
top4 = titles[:4]
check("クラスタ: 重複記事4が先頭グループから降格", "記事4" not in top4, str(top4))
check("クラスタ: 欠落5が単独補完で残存", "記事5" in titles)
check("クラスタ: 代表がスコア順", titles[0] == "記事1")

# クラスタ失敗時のフォールバック
api_utils.LLMClient = lambda cfg: _FakeClient("これはJSONではありません")
try:
    result2 = nc.cluster_topics_with_llm(articles, {"provider": "gemini"}, pool_size=6)
finally:
    api_utils.LLMClient = _orig_client
check("クラスタ失敗→元の順序維持", [a["title_ja"] for a in result2] == [a["title_ja"] for a in articles])

# --- 7. プロンプトに禁止リスト・label仕様が含まれるか ---------------------------
from script_generator import SCRIPT_SYSTEM_PROMPT_TEMPLATE

p = SCRIPT_SYSTEM_PROMPT_TEMPLATE
check("プロンプト: 禁止フレーズ列挙", "知れば知るほど" in p and "禁止フレーズ" in p)
check("プロンプト: タイトル逐語読み禁止", "そのまま読み上げるのは禁止" in p)
check("プロンプト: 3ステップ解説", "たとえ話" in p)
check("プロンプト: few-shot例", "良い例" in p)
check("プロンプト: 大人向けターゲット", "大人" in p and "小中学生" not in p)
check("プロンプト: 冒頭挨拶禁止", "挨拶・前置きは禁止" in p)
check("プロンプト: hook_title仕様", "hook_title" in p)
check("プロンプト: insight（考察）仕様", "insight" in p and "考察" in p)

# --- 8. 1ニュース=1本 Shorts（2026-07刷新） -------------------------------------
from video_builder import select_topic_units

units = select_topic_units(slides, 3)
check("トピック選定: 3ユニット", len(units) == 3, f"got {len(units)}")
check("トピック選定: 各ユニット2枚（紹介+ポイント）",
      all(len(u) == 2 and u[1]["title"].endswith("ポイント") for u in units))
flat = [s for u in units for s in u]
check("トピック選定: opening/index/keyword/ending除外",
      all(not s.get("is_opening") and not s.get("is_index")
          and not s.get("is_keyword") and not s.get("is_ending") for s in flat))
check("トピック選定: 重要度順（先頭3記事）",
      [s["number"] for s in flat] == [3, 4, 5, 6, 7, 8],
      str([s["number"] for s in flat]))

units_many = select_topic_units(slides, 99)
check("トピック選定: top_n>記事数なら全記事", len(units_many) == 5)

# insight（考察）がポイントスライドのセリフ末尾に結合されるか
# （insight以外はscript_dataと完全に同一にし、推定尺の差分がinsight由来であることを保証）
script_data_insight = {
    "articles": [
        {**a, "insight": f"ポンテはこう思うな{i}！"}
        for i, a in enumerate(script_data["articles"], 1)
    ],
    "ending": script_data["ending"],
    "keywords": script_data["keywords"],
}
md2 = render_script(news_data, script_data_insight, Path("templates/daily_script.j2"),
                    "20260716", topic="AI")
tmp2 = Path("state/_verify_script2.md")
tmp2.write_text(md2, encoding="utf-8")
slides2 = parse_slides(tmp2)
point2 = [s for s in slides2 if s["title"].endswith("ポイント")]
check("insight結合: ポイントセリフ末尾に考察",
      all(f"ポンテはこう思うな{i}！" in s["serif"] for i, s in enumerate(point2, 1)))
tmp2.unlink(missing_ok=True)

# 推定尺にinsightが含まれるか（insight有り > insight無し）
from script_generator import estimate_script_seconds
est_without = estimate_script_seconds(script_data, news_data["articles"])
est_with = estimate_script_seconds(script_data_insight, news_data["articles"])
check("推定尺: insight分が加算される", est_with > est_without,
      f"{est_without:.0f}s → {est_with:.0f}s")

# --- 9. フックタイトル -----------------------------------------------------------
from script_generator import _fallback_hook_title, normalize_hook_titles

h = _fallback_hook_title("OpenAIが新しいAIモデルGPT-5.6を発表、業界に衝撃が走っています")
check("フック: 26字以内", len(h) <= 26, repr(h))
check("フック: 英単語途中で切らない", not h.endswith(("GPT-", "GPT")), repr(h))
h2 = _fallback_hook_title("AIで宿題はどうなる？専門家が語る教育現場の大きな変化について")
check("フック: ？で切る", h2 == "AIで宿題はどうなる？", repr(h2))
h3 = _fallback_hook_title("人工知能(じんこうちのう)の未来")
check("フック: ルビ括弧除去", "(" not in h3, repr(h3))

sd = {"articles": [{"hook_title": ""}, {"hook_title": "この見出しはとても長すぎて二十六文字の上限を超えてしまっています"}]}
na = [{"title_ja": "短いタイトル"}, {"title_ja": "元記事タイトルから作り直されるはず"}]
normalize_hook_titles(sd, na)
check("フック正規化: 欠落→タイトルから生成", sd["articles"][0]["hook_title"] == "短いタイトル")
check("フック正規化: 過長→フォールバック",
      len(sd["articles"][1]["hook_title"]) <= 26, repr(sd["articles"][1]["hook_title"]))

# _short_title_label の英単語切断修正
lbl = _short_title_label("OpenAIの新AIモデル「GPT-5.6」がすごい進化を遂げました")
check("_short_title_label: 英単語途中で切らない",
      not lbl.endswith(("GPT-", "GPT")), repr(lbl))

# トリムのカギ括弧対応保持
from script_generator import _trim_serif_to_limit
long_serif = ("最初の文はふつうの説明だよ！例えば、「もっとアップテンポにして」とか"
              "「この曲はいつ出たの？と聞けるんだ！すごく便利で楽しい機能だから期待だね！")
trimmed = _trim_serif_to_limit(long_serif, 40)
check("トリム: カギ括弧の対応を保つ", trimmed.count("「") == trimmed.count("」"), repr(trimmed))
check("トリム: 上限内", len(trimmed) <= 40 or trimmed == long_serif.split("！")[0] + "！")

# LLM手抜きラベル（ニュースN）の破棄
md3 = render_script(
    {"articles": [{"title_ja": "実際の記事タイトルがここに入ります", "title": "t", "source": "s", "link": "u"}]},
    {"articles": [{"label": "ニュース1", "intro": "紹介だよ！", "explanation": "解説なんだ！"}],
     "ending": "またね！", "keywords": []},
    Path("templates/daily_script.j2"), "20260716", topic="AI")
check("手抜きラベル破棄: タイトル由来に置換",
      "1つ目、実際の記事タイトル" in md3, md3[md3.index("1つ目"):md3.index("1つ目") + 30])

# --- 10. per-topic Shortsメタデータ ---------------------------------------------
from youtube_uploader import generate_topic_shorts_metadata

art = {"title_ja": "OpenAIが家族向けChatGPTを発表", "summary_ja": "家族で使えるAI。",
       "summary_ja_long": "OpenAIが家族向けのChatGPTプランを発表した。", "link": "https://example.com/n1"}
tmeta = generate_topic_shorts_metadata("20260716", config, art, "【朗報】家族向けChatGPT登場", 1)
check("トピックメタ: フックがタイトル先頭", tmeta["title"].startswith("【朗報】"), tmeta["title"])
check("トピックメタ: 固定4タグ＋エンティティ1つ以内",
      "#ai #news #shorts #short" in tmeta["title"] and tmeta["title"].count("#") in (4, 5), tmeta["title"])
check("トピックメタ: kind=shorts_topic1", tmeta["kind"] == "shorts_topic1")
check("トピックメタ: 元記事リンク", "https://example.com/n1" in tmeta["description"])
check("トピックメタ: VOICEVOXクレジットなし", "VOICEVOX" not in tmeta["description"])
check("トピックメタ: タイトル100字以内", len(tmeta["title"]) <= 100)

tmeta_nohook = generate_topic_shorts_metadata("20260716", config, art, "", 2)
check("トピックメタ: フック欠落時も生成", "OpenAI" in tmeta_nohook["title"], tmeta_nohook["title"])

# --- 10.5 エンティティ抽出（タグ・ハッシュタグSEO） --------------------------------
from youtube_uploader import extract_entity_keywords, _ja_topic_tags

ents = extract_entity_keywords("OpenAIが新モデルGPT-5.6を発表、ChatGPTも進化")
check("エンティティ: 出現順で抽出", ents[:2] == ["OpenAI", "GPT"], str(ents))
check("エンティティ: ChatGPT個別認識", "ChatGPT" in ents, str(ents))
check("エンティティ: 重複なし", len(ents) == len(set(ents)))
check("エンティティ: limit遵守", len(extract_entity_keywords("OpenAI Google Apple Meta NVIDIA Tesla", limit=3)) == 3)
check("エンティティ: 未知語は無視", extract_entity_keywords("Foobar Bazqux") == [])
check("エンティティ: 空入力", extract_entity_keywords("") == [])
check("エンティティ: 一般語notionを拾わない", extract_entity_keywords("the notion of AI safety") == [])
check("エンティティ: ブランドNotionは拾う", extract_entity_keywords("Notionの新AI機能") == ["Notion"])
check("エンティティ: 一般語metaを拾わない", extract_entity_keywords("add meta tags to html") == [])
check("エンティティ: 小文字lineを拾わない", extract_entity_keywords("a new train Line and line") == [])
check("エンティティ: 全大文字LINEは拾う", extract_entity_keywords("LINEの新機能") == ["LINE"])
check("ジャンル語: 生成AI検出", "生成AI" in _ja_topic_tags("生成AIで画像生成が進化"))

# タイトルハッシュタグにエンティティが入るか
tmeta_ent = generate_topic_shorts_metadata(
    "20260716", config,
    {"title_ja": "OpenAIが家族向けChatGPTを発表", "summary_ja": "家族向けAI。", "link": "https://x.com/1"},
    "【朗報】家族向けChatGPT登場", 1)
check("SEOタイトル: エンティティハッシュタグ", "#ChatGPT" in tmeta_ent["title"] or "#OpenAI" in tmeta_ent["title"],
      tmeta_ent["title"])
check("SEOタイトル: 固定4タグ＋エンティティ1つ", tmeta_ent["title"].count("#") == 5, tmeta_ent["title"])
check("SEOタグ: エンティティが先頭", tmeta_ent["tags"][0] in ("ChatGPT", "OpenAI"), str(tmeta_ent["tags"][:4]))
check("SEOタグ: 15個以内", len(tmeta_ent["tags"]) <= 15, str(len(tmeta_ent["tags"])))
check("SEO説明文: 末尾ハッシュタグ行なし", "#生成AI" not in tmeta_ent["description"]
      and tmeta_ent["description"].rstrip().endswith("surc.online/"))

# エンティティなしの記事でも従来形式で成立するか
tmeta_noent = generate_topic_shorts_metadata(
    "20260716", config,
    {"title_ja": "量子計算の新手法が発見される", "summary_ja": "新発見。", "link": "https://x.com/2"},
    "量子計算に新手法", 2)
check("SEOタイトル: エンティティ無し→固定4タグ", tmeta_noent["title"].count("#") == 4, tmeta_noent["title"])

# --- 10.7 字幕（SRT）生成 ----------------------------------------------------------
from video_builder import serif_to_caption_text, _format_srt_time, _sentence_cues

cap = serif_to_caption_text("Siri(シリ)が賢(かしこ)くなるよ！（3秒の間）すごいね！")
check("字幕テキスト: 英語表記を維持", cap.startswith("Siri"), repr(cap))
check("字幕テキスト: 漢字ルビ除去", "(かしこ" not in cap and "賢くなる" in cap, repr(cap))
check("字幕テキスト: 演出指示除去", "3秒" not in cap, repr(cap))

check("SRT時刻: 0秒", _format_srt_time(0) == "00:00:00,000")
check("SRT時刻: 61.5秒", _format_srt_time(61.5) == "00:01:01,500")
check("SRT時刻: 負値ガード", _format_srt_time(-1) == "00:00:00,000")

cues = _sentence_cues("一文目だよ！二文目はもっと長い文章なんだ！", 1.0, 10.0)
check("字幕キュー: 文数", len(cues) == 2, str(cues))
check("字幕キュー: 開始時刻", abs(cues[0][0] - 1.0) < 0.001)
check("字幕キュー: 終了=開始+音声長", abs(cues[-1][1] - 11.0) < 0.01, str(cues[-1]))
check("字幕キュー: 長い文に長い時間", (cues[1][1] - cues[1][0]) > (cues[0][1] - cues[0][0]))
check("字幕キュー: 空テキスト", _sentence_cues("", 0, 5) == [])
cues_q = _sentence_cues("団体は『無断使用だ！』って怒っているんだよ。次の文だよ！", 0, 10.0)
check("字幕キュー: 括弧内の！で分割しない",
      len(cues_q) == 2 and cues_q[0][2].endswith("』って怒っているんだよ。"), str([c[2] for c in cues_q]))

# アップロード側の配線（スコープ・caption_file）
import youtube_uploader as _yu
check("字幕: force-sslスコープ追加", any("force-ssl" in s for s in _yu.YOUTUBE_SCOPES))
check("字幕: upload_caption関数存在", callable(getattr(_yu, "upload_caption", None)))

# --- 10.8 日付既定値がJST基準か ---------------------------------------------------
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
import orchestrator as _orch

_jst_now = _dt.now(_tz(_td(hours=9)))
check("日付既定値: today_jst()がJSTの今日", _orch.today_jst() == _jst_now.strftime("%Y%m%d"),
      f"{_orch.today_jst()} vs JST {_jst_now:%Y%m%d} (UTC {_dt.now(_tz.utc):%Y%m%d})")
import inspect
_src = inspect.getsource(_orch.run_pipeline)
check("日付既定値: run_pipelineがtoday_jst使用", "today_jst()" in _src)
import news_collector as _nc
_src_nc = inspect.getsource(_nc.collect_news)
check("日付既定値: collect_newsがJST使用", "_JST" in _src_nc)

# --- 11. config: 刷新フラグ -------------------------------------------------------
check("config: 長尺デイリー無効", config.get("pipeline", {}).get("daily_video_enabled") is False)
check("config: per_topic有効", config.get("shorts", {}).get("per_topic") is True)
check("config: topics_per_day=3", config.get("shorts", {}).get("topics_per_day") == 3)
check("config: Ken Burns有効", config.get("video", {}).get("motion", {}).get("enabled") is True)
check("config: 音声クレジット設定", bool(config.get("tts", {}).get("credit", "")))
check("config: 子ども向けタグ除去",
      not any(t in ("小学生", "中学生") for t in config.get("youtube", {}).get("default_tags", [])))

# --- 12. NGコンテンツ除外フィルタ（2026-08-18 わいせつ系恒久除外） -----------------
_ng_block = [
    {"title": "Woman claims her stepfather used Grok to transform childhood photo into explicit imagery", "summary": ""},
    {"title": "義父がGrokで幼少期の写真をわいせつ画像に加工したと女性が主張", "summary": ""},
    {"title": "AI-generated sexual content floods platform", "summary": ""},
    {"title": "These Masturbation Consultants Were Hired to Please AI", "summary": ""},
]
_ng_pass = [
    {"title": "GPT-5.6 Sol Ultrafast mode released", "summary": ""},
    {"title": "AIが個性的なデザインを生成する新サービスが登場", "summary": ""},
    {"title": "理性的な判断をするAIエージェントの研究", "summary": ""},
    {"title": "知性的なAIアシスタントの未来", "summary": ""},
    {"title": "Research on asexual reproduction in starfish yields AI insights", "summary": ""},
]
check("NGフィルタ: NG記事4件を全て除外", len(_nc.filter_excluded_content(list(_ng_block))) == 0)
check("NGフィルタ: 合法記事5件を誤検知しない（個性的/理性的/知性的/asexual含む）",
      len(_nc.filter_excluded_content(list(_ng_pass))) == len(_ng_pass))
check("NGフィルタ: extra_keywords=Noneで安全", len(_nc.filter_excluded_content(list(_ng_pass), None)) == len(_ng_pass))
check("NGフィルタ: config追加キーワードが効く",
      len(_nc.filter_excluded_content([{"title": "ギャンブル必勝法とAI", "summary": ""}], ["ギャンブル"])) == 0)
check("NGフィルタ: collect_newsに組み込み済み", "filter_excluded_content" in _src_nc)
check("NGフィルタ: configにexclude_keywordsキー", isinstance(config.get("news", {}).get("exclude_keywords"), list))
check("NGフィルタ: LLMプロンプトにNGコンテンツ節", "NGコンテンツ" in _nc.SCORING_SYSTEM_PROMPT_TEMPLATE)

# --- 13. Shortsテンプレート契約（2026-08-18 UI刷新） ---------------------------
import jinja2 as _j2
_env = _j2.Environment(loader=_j2.FileSystemLoader("templates"), undefined=_j2.StrictUndefined, autoescape=False)
_tpl = _env.get_template("slide_shorts.html")
_base_ctx = dict(
    title="テストニュース", text="セリフだよ！", news_number=1, channel_name="ポンテのAI教室",
    tagline="むずかしいAIを、かんたんに。", date="2026年8月18日 (火)", ponte_image="",
    index_heading="TODAY'S AI NEWS", index_items=[], keyword_items=[{"term": "AI", "desc": "説明"}],
    bg_variant="bg-1", og_image="data:image/png;base64,x", ending_message="また明日ね！",
    day_class="day-tue", source_label="itmedia.co.jp", news_total=3, text_len=6,
    hook_title="", points=[], captions_burned=False,
)
for _st in ("opening", "keyword", "ending"):
    _html = _tpl.render(slide_type=_st, **_base_ctx)
    check(f"Shortsテンプレ: {_st}がStrictUndefinedで描画可", bool(_html))
_news_html = _tpl.render(slide_type="news", **_base_ctx)
check("Shortsテンプレ: 通常news=見出し＋hero",
      'class="headline"' in _news_html and 'class="hero"' in _news_html and 'class="point-badge"' not in _news_html)
_pt_ctx = dict(_base_ctx, title="<ruby>透<rt>す</rt></ruby>かし技術のポイント")
_pt_html = _tpl.render(slide_type="news", **_pt_ctx)
check("Shortsテンプレ: ポイントnews=バッジ＋heroなし（ルビHTML越しに判定）",
      'class="point-badge"' in _pt_html and 'class="hero"' not in _pt_html and "slide--point" in _pt_html)
check("Shortsテンプレ: ポイントのトピック行から「のポイント」除去", "のポイント" not in _pt_html)

# --- 13b. 字幕焼き込みモード（2026-10-04 刷新: 大見出し＋要点カード＋同期字幕） ----------
_cap_ctx = dict(_base_ctx, captions_burned=True, hook_title="【速報】新AIが首位", points=["要点A", "要点B", "要点C"])
_cap_news = _tpl.render(slide_type="news", **_cap_ctx)
check("字幕モード: 紹介=hook見出し＋元記事タイトル＋hero",
      'class="hook"' in _cap_news and "【速報】新AIが首位" in _cap_news and 'class="src-title"' in _cap_news
      and 'class="hero"' in _cap_news)
check("字幕モード: セリフ全文を描かない（動画側で焼き込む）",
      "セリフだよ！" not in _cap_news and 'class="serif-card' not in _cap_news)
_cap_pt = _tpl.render(slide_type="news", **dict(_cap_ctx, title="テストのポイント"))
check("字幕モード: ポイント=要点カード3枚＋トピック帯にhook",
      _cap_pt.count('class="point-card"') == 3 and "【速報】新AIが首位" in _cap_pt and 'class="point-badge"' in _cap_pt)
_cap_pt0 = _tpl.render(slide_type="news", **dict(_cap_ctx, title="テストのポイント", points=[]))
check("字幕モード: 要点なし→記事画像で代替（カード0枚・崩れなし）",
      'class="point-card"' not in _cap_pt0 and "hero-small" in _cap_pt0)
_cap_nohook = _tpl.render(slide_type="news", **dict(_cap_ctx, hook_title=""))
check("字幕モード: hookなし→従来の記事タイトル見出し", 'class="headline"' in _cap_nohook)

from video_builder import _split_long_sentence, _raw_sentence_cues, _CAPTION_BOX

# LIMIT は必ず _CAPTION_BOX の上端（bottom - height）と一致させること。
# ズレると見出し/元記事タイトルが動画側で焼き込まれる字幕カードと重なる
# （2026-10-04 QA2回目で発覚: LIMIT=1170固定値がbox上端1080と100px乖離していた）。
_cap_box_top = _CAPTION_BOX["bottom"] - _CAPTION_BOX["height"]
check("字幕モード: 字幕帯への重なり防止スクリプトのLIMITが_CAPTION_BOX上端と一致",
      f"var LIMIT = {_cap_box_top}" in _cap_news and "src.style.display = 'none'" in _cap_news,
      f"expected LIMIT={_cap_box_top}")
# 要点カード（最大20文字×3）も字幕帯にかかりうる（2026-10-04 QA2回目で実測確認:
# 20文字×3＋記事画像ありの実寸でbottom=1102>1080となり重なっていた）ため縮小ロジックが必要
check("字幕モード: 要点カードの縮小ロジックを含む（字幕帯への重なり対策）",
      "points.getBoundingClientRect().bottom > LIMIT" in _cap_pt)
_long = "CaddyやFambotみたいなサービスは、あなたがメッセージで「明日(あす)の予定(よてい)を教(おし)えて、すぐに」と指示(しじ)するだけで、勝手(かって)にカレンダーを見て対応(たいおう)してくれるんだ。"
_parts = _split_long_sentence(_long)
check("字幕分割: 長文を読点で分割", len(_parts) >= 2, str(_parts))
check("字幕分割: 連結で元に戻る（文字欠けなし）", "".join(_parts) == _long)
check("字幕分割: 括弧の内側の読点では切らない",
      all(pp.count("「") == pp.count("」") for pp in _parts), str(_parts))
check("字幕分割: ルビ括弧を壊さない", all(pp.count("(") == pp.count(")") for pp in _parts), str(_parts))
check("字幕分割: 短文はそのまま", _split_long_sentence("短い文だよ。") == ["短い文だよ。"])
_rc = _raw_sentence_cues("一文目(いちぶんめ)だよ！" + _long, 2.0, 20.0)
check("字幕キュー(生): ルビ記法を保持", "(いちぶんめ)" in _rc[0][2], str(_rc[0]))
check("字幕キュー(生): 開始と終了", abs(_rc[0][0] - 2.0) < 1e-6 and abs(_rc[-1][1] - 22.0) < 0.01)
check("字幕キュー(生): 単調増加・重なりなし",
      all(_rc[k][1] <= _rc[k + 1][0] + 1e-6 for k in range(len(_rc) - 1)))
check("字幕キュー(生): 空", _raw_sentence_cues("", 0, 5) == [])
_tplsrc = Path("templates/slide_shorts.html").read_text(encoding="utf-8")
check("字幕帯: テンプレのポンテ下端=動画側の字幕帯下端（1920-440=1480）",
      "bottom: 440px" in _tplsrc and _CAPTION_BOX["bottom"] == 1480)

from script_generator import normalize_points, _validate_script_lengths, _cap_script_lengths
check("要点: 過長は切らずに捨てる", normalize_points(["短い要点", "あ" * 25, "二つ目の要点"]) == ["短い要点", "二つ目の要点"])
check("要点: 最大3個・重複除去", normalize_points(["A1", "A1", "B2", "C3", "D4"]) == ["A1", "B2", "C3"])
check("要点: ルビ記法と句点を除去", normalize_points(["Siri(シリ)が対応。"]) == ["Siriが対応"])
check("要点: リスト以外は空", normalize_points(None) == [] and normalize_points("x") == [])
_ok_art = {"hook_title": "新AIが19項目中14項目で首位に", "intro": "あ" * 50, "explanation": "い" * 90,
           "insight": "う" * 30, "points": ["要点その一", "要点その二", "要点その三"]}
check("検証: 正常な記事は問題なし", _validate_script_lengths({"articles": [_ok_art]}) == [])
check("検証: 製品名だけの短いhookはリトライ対象",
      any("短すぎ" in x for x in _validate_script_lengths({"articles": [dict(_ok_art, hook_title="Gemini 4 Argon")]})))
check("検証: points欠落はリトライ対象",
      any("points" in x for x in _validate_script_lengths({"articles": [dict(_ok_art, points=[])]})))
_capd = _cap_script_lengths({"articles": [dict(_ok_art, insight="ポンテはこう思うな。" + "え" * 70 + "。")]})
check("切り詰め: 前置きだけ残る考察は捨てる", _capd["articles"][0]["insight"] == "", repr(_capd["articles"][0]["insight"]))

tmp.unlink(missing_ok=True)


# --- 7. hook_title の括弧修復（_repair_open_bracket / _fallback_hook_title）----
import random as _random
import threading as _threading

from script_generator import (
    _HOOK_TITLE_MIN_USABLE,
    _TITLE_BRACKETS,
    _fallback_hook_title,
    _repair_open_bracket,
    _strip_unmatched_brackets,
)


def _call_with_timeout(fn, args, timeout=3.0):
    """関数が有限時間で返ることを検証する（無限ループ検出用）."""
    box = {}
    th = _threading.Thread(target=lambda: box.update(v=fn(*args)), daemon=True)
    th.start()
    th.join(timeout)
    return (not th.is_alive()), box.get("v")

# 7-1. 実運用ケース: 固有名詞を閉じ括弧まで残す
for _src, _want in [
    ("Hermes Agent新機能「Local Models」でローカルAIを簡単実行",
     "Hermes Agent新機能「Local Models」"),
    ("安全柵不要で人間と協働可能な新型ロボット「Digit 5」登場",
     "安全柵不要で人間と協働可能な新型ロボット「Digit 5」"),
    ("Googleの新AI音声モデル「Gemini 3.8 Live」—会話しながら裏でタスク実行が可能に",
     "Googleの新AI音声モデル「Gemini 3.8 Live」"),
]:
    _got = _fallback_hook_title(_src)
    check(f"hook_title: 括弧を閉じて固有名詞を保持（{_want[:12]}…）", _got == _want, f"got={_got}")

# 7-2. 括弧が閉じないまま途中で終わる見出しを作らない
for _src in [
    "Hermes Agent新機能「Local Models」でローカルAIを簡単実行",
    "安全柵不要で人間と協働可能な新型ロボット「Digit 5」登場",
    "これはとても長い日本語のタイトルで「閉じ括弧がずっとずっとずっとずっと先にある」ケース",
]:
    _got = _fallback_hook_title(_src)
    check(f"hook_title: 未閉じ括弧が残らない（{_src[:10]}…）",
          _got.count("「") == _got.count("」"), f"got={_got}")

# 7-3. 回帰: 括弧を含まないタイトルは従来どおり（修復はno-op）
check("hook_title: 括弧なしは短縮のみ",
      _fallback_hook_title("AI企業トップが規制を求める理由と背景") == "AI企業トップが規制を求める理由と背景")
# 語の途中で切らない（形態素境界で切る）ため、ちょうど26文字にはならない
_got = _fallback_hook_title("AIプロジェクトの墓場：失敗に終わったAI企業とプロジェクト一覧")
check("hook_title: 括弧なし長文は26文字以内・語境界で切る",
      _HOOK_TITLE_MIN_USABLE <= len(_got) <= 26 and _got == "AIプロジェクトの墓場：失敗に終わったAI企業", f"got={_got}")
# 7-3b. 回帰（2026-10-03）: 日本語の語の途中で切れない（"…で首" "…立ち上" が出ていた）
for _src, _bad in [("Gemini 4 Argonが19項目中14項目で首位を獲得", "首"),
                   ("【衝撃】HENNGEが取締役2人だけでAI企業立ち上げ", "立ち上")]:
    _got = _fallback_hook_title(_src)
    check(f"hook_title: 語の途中で切れない（{_src[:10]}…）",
          not _got.endswith(_bad) and len(_got) <= 26, f"got={_got}")

# 7-4. 無限ループ回帰（多重括弧混在。過去に while True が振動してハングした）
for _c, _f, _sl in [
    ("“「『”1", "“「『”1", 2),
    ("C”3bA【『」【》A』”", "C”3bA【『」【》A』”13b", 0),
]:
    _fin, _val = _call_with_timeout(_repair_open_bracket, (_c, _f, _sl))
    check(f"_repair_open_bracket: 有限時間で返る（{_c[:8]}…）", _fin, "TIMEOUT（無限ループ）")

# 7-5. fuzz: ハングしない / 空にしない / 括弧の開閉が必ず一致する
_random.seed(20260917)
_alpha = "「」『』【】《》“”ABCabc123 。、"
_hang = _empty = 0
_unbalanced = None
for _ in range(3000):
    _full = "".join(_random.choice(_alpha) for _ in range(_random.randint(1, 60)))
    _cut = _full[: _random.randint(0, len(_full))]
    _fin, _ = _call_with_timeout(_repair_open_bracket, (_cut, _full, _random.randint(0, 10)), 1.0)
    if not _fin:
        _hang += 1
        break
    _out = _fallback_hook_title(_full)
    # 括弧以外の実体がある入力なら、空タイトルになってはいけない
    if _full.strip("。、・:： " + "".join(_TITLE_BRACKETS) + "".join(_TITLE_BRACKETS.values())) and not _out:
        _empty += 1
        break
    if _unbalanced is None:
        for _op, _cl in _TITLE_BRACKETS.items():
            if _out.count(_op) != _out.count(_cl):
                _unbalanced = (_full, _out, _op)
                break
    if _unbalanced:
        break
check("_repair_open_bracket: fuzz 3000件でハングなし", _hang == 0)
check("hook_title: fuzz 3000件で空タイトルを生成しない", _empty == 0)
check("hook_title: fuzz 3000件で括弧の開閉が必ず一致", _unbalanced is None,
      f"src={_unbalanced[0]!r} out={_unbalanced[1]!r} bracket={_unbalanced[2]!r}" if _unbalanced else "")

# 7-5b. 孤立した閉じ括弧（元タイトル側が非対称）でも開閉を一致させる
for _src in [
    "AIが人間を超えた」と専門家が発言し世界に衝撃が走った件について",
    "』だけが残るとても長いタイトルでトランケートが確実に発生するケースを検証する",
]:
    _got = _fallback_hook_title(_src)
    check(f"hook_title: 孤立した閉じ括弧を除去（{_src[:8]}…）",
          all(_got.count(_o) == _got.count(_c) for _o, _c in _TITLE_BRACKETS.items()),
          f"got={_got}")

# 7-5c. _strip_unmatched_brackets 単体（入れ子・交差・対応済みは無改変）
check("_strip_unmatched_brackets: 対応済みは無改変",
      _strip_unmatched_brackets("A「B『C』D」E") == "A「B『C』D」E")
check("_strip_unmatched_brackets: 孤立閉じを除去",
      _strip_unmatched_brackets("A」B") == "AB")
check("_strip_unmatched_brackets: 未閉じ開きを除去",
      _strip_unmatched_brackets("A「B") == "AB")
check("_strip_unmatched_brackets: 交差した括弧を整合",
      all(_strip_unmatched_brackets("「A『B」C』").count(_o)
          == _strip_unmatched_brackets("「A『B」C』").count(_c)
          for _o, _c in _TITLE_BRACKETS.items()))
check("_strip_unmatched_brackets: 空文字は空文字", _strip_unmatched_brackets("") == "")

# 7-5d. 現実の見出しに壊れた括弧が1文字混入しても実体を失わない（極端な短縮の検知）
_HEADLINE_TMPL = [
    "OpenAIが新しいAIモデルGPT-6を発表し業界に衝撃が走った",
    "Metaが緊急発表、次世代ARグラスを来年春に投入すると明かした",
    "経済産業省がAI企業への支援策デジタル田園都市AI基金を発表",
    "テスラの新型ヒューマノイドOptimus Gen3が工場配備を開始した",
    "Amazonが倉庫ロボットSparrow 2を全米展開へ本格移行すると発表",
]
_BROKEN = "「』【》“”"
_random.seed(31415)
_short = 0
_worst = None
for _ in range(1500):
    _base = _random.choice(_HEADLINE_TMPL)
    _src = _base
    for _ in range(_random.randint(1, 2)):
        _at = _random.randrange(len(_src))
        _src = _src[:_at] + _random.choice(_BROKEN) + _src[_at:]
    _got = _fallback_hook_title(_src)
    if len(_got) <= 8:
        _short += 1
        if _worst is None:
            _worst = (_src, _got)
check("hook_title: 壊れた括弧混入でも見出しが激減しない（1500件）", _short == 0,
      f"{_short}件が8文字以下。例: src={_worst[0]!r} out={_worst[1]!r}" if _worst else "")

# 7-6. 未閉じ括弧が文頭にあり閉じ括弧が存在しない → 空にせず単純カットへフォールバック
_lead = _fallback_hook_title("「開き括弧だけがずっと続いていて閉じない場合のタイトルはどうなるかを確認するテスト")
check("hook_title: 先頭未閉じ括弧でも空にならない", bool(_lead), f"got={_lead!r}")

# --- 14. ビルド改善（2026-10-23: 部分修正・題名の読み除去・事実確認・プリフライト・修正反映） ----
import json as _json
import tempfile as _tempfile

from script_generator import (
    FACTCHECK_SYSTEM_PROMPT, SCRIPT_SYSTEM_PROMPT_TEMPLATE as _SPT, _article_issues, _is_format_only,
    _merge_repair, _repair_script, factcheck_and_fix, normalize_hook_titles, strip_reading_parens,
)
from cleaning import cleaning_short

# 14-1. 題名の読みがな除去（Claude(クロード) が YouTube 題名に出た事故の再発防止）
check("読み除去: 英語の読み", strip_reading_parens("Claude(クロード)が500万行") == "Claudeが500万行")
check("読み除去: 全角カッコ・漢字の読み", strip_reading_parens("脳（のう）を解析") == "脳を解析")
check("読み除去: 読み以外のカッコは残す", strip_reading_parens("GPT-5(新版)が登場") == "GPT-5(新版)が登場")
_sd = {"articles": [{"hook_title": "Claude(クロード)が隠れた弱点493件を発見"}]}
normalize_hook_titles(_sd, [{"title_ja": "x"}])
check("normalize_hook_titles: 読みを外す", _sd["articles"][0]["hook_title"] == "Claudeが隠れた弱点493件を発見")
_ok14 = {"hook_title": "新AIが19項目中14項目で首位に", "intro": "あ" * 50, "explanation": "い" * 90,
         "insight": "う" * 30, "points": ["要点その一", "要点その二", "要点その三"]}
check("検証: 題名の読みは指摘対象", any("読みがな" in x for x in
      _article_issues(1, dict(_ok14, hook_title="Claude(クロード)が隠れた弱点493件を発見"))))
check("検証: 題名の長さは読み抜きで数える",
      not any("長すぎ" in x for x in _article_issues(1, dict(_ok14, hook_title="Anthropic(アンスロピック)が新AIを公開しました"))))
check("検証: 要点2個は指摘対象", any("points" in x for x in _article_issues(1, dict(_ok14, points=["a1", "b2"]))))

# 14-2. 部分修正: 問題のない記事は変えず、悪化する修正は採用しない
_res = {"articles": [dict(_ok14), dict(_ok14, intro="え" * 120)], "ending": "お" * 50}
_n = _merge_repair(_res, {"articles": [
    {"index": 1, "intro": "か" * 200},             # 問題のない記事を悪化させる修正 → 不採用
    {"index": 2, "intro": "き" * 60},              # 問題を直す修正 → 採用
    {"index": 9, "intro": "x"},                    # 範囲外 → 無視
]})
check("部分修正: 改善だけ採用", _n == 1 and _res["articles"][0]["intro"] == "あ" * 50
      and _res["articles"][1]["intro"] == "き" * 60, repr(_n))


class _FakeLLM:
    """呼ばれた順に用意した応答を返すモック（呼び出し内容も記録する）."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, system, prompt, max_tokens, temperature=None):
        self.calls.append({"system": system, "prompt": prompt, "temperature": temperature})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else _json.dumps(r, ensure_ascii=False)


_res = {"articles": [dict(_ok14), dict(_ok14, intro="え" * 120)], "ending": "お" * 50}
_fake = _FakeLLM([{"articles": [{"index": 2, "intro": "き" * 60}]}])
_repair_script(_fake, "SYS", _res, [{"title_ja": "t1"}, {"title_ja": "t2"}], 1000, 3)
check("部分修正ループ: 問題の記事だけ送り、直ったら止まる",
      len(_fake.calls) == 1 and "ニュース2" in _fake.calls[0]["prompt"]
      and "ニュース1:" not in _fake.calls[0]["prompt"] and _res["articles"][1]["intro"] == "き" * 60)

# 14-3. 事実確認: 誤りは自動修正→再確認、細部・形式の指摘は捨てる、ルール外の修正は書き直させる
_src = [{"title_ja": "脳データ分析", "summary_ja_long": "マウスの脳細胞データを32分で分析。人間なら数カ月から1年。",
         "facts": ["数千体のAI"]}]
_sd = {"articles": [dict(_ok14, hook_title="数千のAIが脳を32分で解析",
                         intro="OpenAI(オープンエーアイ)の研究で、人間なら1年かかる分析が32分で終わったんだ！")]}
_fake = _FakeLLM([
    {"articles": [{"index": 1, "issues": [
        {"field": "hook_title", "severity": "error", "problem": "マウスの脳細胞データ",
         "fix": "数千のAIがマウスの脳細胞データを32分で分析した研究結果"},  # 30字: ルール外 → 書き直し
        {"field": "intro", "severity": "error", "problem": "数カ月から1年",
         "fix": "OpenAIの研究で、人間なら数カ月から1年かかる分析が32分で終わったんだ！"},
        {"field": "intro", "severity": "error", "problem": "2件目（元の文から作った修正）", "fix": "上書きしてはいけない文です。"},
        {"field": "insight", "severity": "minor", "problem": "細部", "fix": "細部の修正"},
        {"field": "points", "problem": "pointsが2個しかない。ルールでは3個", "fix": ""},
    ]}]},
    {"fixes": [{"no": 1, "fix": "数千のAIがマウス脳データを32分で分析"}]},
    {"articles": [{"index": 1, "issues": []}]},
])
_rep = factcheck_and_fix(_sd, _src, {"provider": "claude"}, client=_fake)
_a = _sd["articles"][0]
check("事実確認: ルール外の題名は書き直して適用", _a["hook_title"] == "数千のAIがマウス脳データを32分で分析", _a["hook_title"])
check("事実確認: セリフ修正に英語の読みを引き継ぐ", "OpenAI(オープンエーアイ)" in _a["intro"] and "数カ月" in _a["intro"], _a["intro"])
check("事実確認: 同じ項目の2件目は適用しない", "上書き" not in _a["intro"])
check("事実確認: minor は直さない", _a["insight"] == "う" * 30)
check("事実確認: 形式だけの指摘は捨てる", _rep["status"] == "fixed" and not _rep["articles"][0]["unresolved"], repr(_rep))
check("事実確認: 確認役は温度0・クリーニング入り",
      all(c["temperature"] == 0 for c in _fake.calls) and cleaning_short() in _fake.calls[0]["system"])
check("事実確認: 修正後に再確認する（3回目の呼び出しは修正後の台本）",
      len(_fake.calls) == 3 and "マウス脳データ" in _fake.calls[2]["prompt"])
check("形式だけの指摘の判定", _is_format_only({"problem": "pointsが2個しかない", "fix": ""})
      and not _is_format_only({"problem": "数字が元記事と違う", "fix": ""}))

_sd = {"articles": [dict(_ok14)]}
_rep = factcheck_and_fix(_sd, _src, {"provider": "claude"}, client=_FakeLLM([RuntimeError("down")]))
check("事実確認: 失敗しても台本は残し status=error", _rep["status"] == "error" and _sd["articles"][0] == _ok14)
_sd = {"articles": [dict(_ok14, intro="人間なら1年かかる分析が32分で終わったんだ！すごいね。")]}
_rep = factcheck_and_fix(_sd, _src, {"provider": "claude"}, client=_FakeLLM([
    {"articles": [{"index": 1, "issues": [{"field": "intro", "severity": "error", "problem": "p",
                                            "fix": "人間なら数カ月から1年かかる分析が32分で終わったんだ！"}]}]},
    "壊れた応答", "まだ壊れた応答"]))
check("事実確認: 再確認だけ失敗→修正は残し要確認",
      "数カ月" in _sd["articles"][0]["intro"] and _rep["status"] == "unresolved", repr(_rep))
_sd = {"articles": [dict(_ok14, intro="人間なら1年かかる分析が32分で終わったんだ！")]}
_rep = factcheck_and_fix(_sd, _src, {"provider": "claude"}, client=_FakeLLM([
    {"articles": [{"index": 1, "issues": [{"field": "intro", "severity": "error", "problem": "p", "fix": ""}]}]}]))
check("事実確認: 修正案のない誤りは要確認に残す",
      _rep["status"] == "unresolved" and _rep["articles"][0]["unresolved"][0]["field"] == "intro")
check("プロンプト: 台本役・確認役にクリーニングの差し込み口",
      "{cleaning}" in _SPT and "__CLEANING__" in FACTCHECK_SYSTEM_PROMPT)
check("プロンプト: 題名に読みを付けない規則", "読みがなのカッコ書きを付けない" in _SPT)

# 14-4. プリフライト・確認用まとめ・修正反映
from review_report import preflight, render_review, unsupported_numbers
from review_apply import FixError, plan_fixes

check("数字照合: 元記事にない数字を拾う",
      unsupported_numbers({"summary_ja_long": "500万行のコードから493件"}, ["500万行で439件", "3個"]) == ["439"])
check("数字照合: 全角・カンマ・小数", unsupported_numbers(
    {"facts": ["精度は83.5%、1,200件"]}, ["８３.５%で1200件"]) == [])

# 14-4b. 金額の桁ずれ（2026-11-01: $740 billion を「740億ドル」と10倍小さく訳した）
from decimal import Decimal as _D
from money_units import (english_amounts, fix_money_units, ja_amount_text,
                         suspicious_amounts)
_en = ("Big Tech plans to spend more than $740 billion this year. Alphabet reported negative "
       "free cash flow of $9.1 billion. Amazon expects $220 billion in capex. "
       "It raised $500 million. A $1.2 trillion market. Revenue hit 42.4 billion dollars.")
check("金額: 英語の $X billion/million/trillion を読む",
      [u for _, u in english_amounts(_en)] == [_D("7.4e11"), _D("9.1e9"), _D("2.2e11"),
                                               _D("5e8"), _D("1.2e12"), _D("4.24e10")])
check("金額: 日本語表記", [ja_amount_text(_D(x)) for x in ("7.4e11", "9.1e9", "5e8", "1.2e12", "2e12", "1.8e6")]
      == ["7400億", "91億", "5億", "1兆2000億", "2兆", "180万"])
_txt, _chg = fix_money_units("4社で740億ドル以上！アルファベットは91億ドル、５００万ドルを調達", _en)
check("金額: 10倍・100倍の誤訳だけ直し、正しい金額と全角記号は残す",
      _txt == "4社で7400億ドル以上！アルファベットは91億ドル、5億ドルを調達" and len(_chg) == 2, _txt)
check("金額: trillion の誤訳も直す", fix_money_units("1.2億ドル市場", _en)[0] == "1兆2000億ドル市場")
check("金額: 英語原文にない金額・円は触らない",
      fix_money_units("300億ドルと740億円", _en) == ("300億ドルと740億円", []))
check("金額: 英語原文に同じ金額があれば直さない（$74 billion と $740 billion が両方ある）",
      fix_money_units("740億ドル", "$74 billion and $740 billion")[1] == []
      and fix_money_units("74億ドル", "$7.4 billion and $74 billion")[1] == [])
for _src_fp in ("USD 500 million round; total valuation $5 billion",
                "raised $500 mln; valuation $5 billion",
                "raised $500-million; valuation $5 billion",
                "raised $500,000,000; valuation $5 billion",
                "raised 500M euros-equivalent; valuation $5 billion"):
    check(f"金額: 正しい訳を壊さない（{_src_fp[:28]}…）",
          fix_money_units("5億ドルを調達", _src_fp) == ("5億ドルを調達", []))
check("金額: USD・mln・ハイフン表記の金額も直す候補にする",
      [u for _, u in english_amounts("USD 500 mln, $3-billion, US$2bn, $7B")]
      == [_D("5e8"), _D("3e9"), _D("2e9"), _D("7e9")])
check("金額: $74 billion を「74億ドル」と書いたら直す",
      fix_money_units("74億ドル", "$74 billion and $740 billion")[0] == "740億ドル")
check("金額: 原文が日本語（英語の金額なし）なら何もしない",
      fix_money_units("740億ドル", "GIGAZINEの記事本文") == ("740億ドル", []))
check("金額: セリフの読みがな越しに桁ずれを見つける",
      suspicious_amounts(["4社(しゃ)が740億(おく)ドル以上", "91億(おく)ドル"], _en)
      == ["740億ドル → 7400億ドル"])
check("数字照合: 英語原文の金額を正しく換算した数字は WARN にしない",
      unsupported_numbers({"summary_ja_long": "4社で740億ドル", "full_text": _en},
                          ["4社で7400億ドル"]) == [])
from news_collector import ENRICH_SYSTEM_PROMPT_TEMPLATE as _EPT, _fix_money_in_article
_art = {"title": "Cracks in AI spending", "title_ja": "740億ドルのAI投資",
        "summary_ja_long": "4社が740億ドル", "facts": ["アマゾンは220億ドル", "12%下落"]}
_fix_money_in_article(_art, _en)
check("要約: 題名・要約・事実の桁ずれを直して記録する",
      _art["title_ja"] == "7400億ドルのAI投資" and _art["summary_ja_long"] == "4社が7400億ドル"
      and _art["facts"] == ["アマゾンは2200億ドル", "12%下落"]
      and len(_art["money_unit_fixes"]) == 2, repr(_art))
check("プロンプト: 要約役に billion 換算と「赤字」の規則",
      "$740 billion = 7400億ドル" in _EPT and "赤字" in _EPT)
check("プロンプト: 確認役に会計用語の取り違えの規則", "会計用語の取り違え" in FACTCHECK_SYSTEM_PROMPT)

with _tempfile.TemporaryDirectory() as _td:
    _td = Path(_td)
    (_td / "state" / "captions").mkdir(parents=True)
    (_td / "output").mkdir()
    _cfg = {"paths": {"state_dir": str(_td / "state"), "output_dir": str(_td / "output")},
            "shorts": {"topics_per_day": 2, "max_topic_duration_sec": 75},
            "youtube": {}, "channel": {}}
    (_td / "state" / "news_20990101.json").write_text(_json.dumps({"articles": [
        {"title_ja": "記事1", "summary_ja_long": "OpenAIが32分で分析", "link": "https://e/1"},
        {"title_ja": "記事2", "summary_ja_long": "Claudeが493件を発見", "link": "https://e/2"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (_td / "state" / "script_meta_20990101.json").write_text(_json.dumps({
        "source": "llm", "factcheck": {"status": "fixed", "articles": [
            {"index": 1, "fixed": [], "unresolved": []},
            {"index": 2, "fixed": [], "unresolved": [{"field": "intro", "problem": "要確認の例", "suggestion": ""}]}]},
        "articles": [
            {"hook_title": "OpenAIのAIが脳データを32分で解析", "points": ["a1", "b2", "c3"]},
            {"hook_title": "Claude(クロード)が隠れた弱点を発見", "points": ["a1"]},
        ]}, ensure_ascii=False), encoding="utf-8")
    _checks = preflight("20990101", _cfg)
    _fails = {(c["topic"], c["check"]) for c in _checks if c["level"] == "FAIL"}
    _warns = {(c["topic"], c["check"]) for c in _checks if c["level"] == "WARN"}
    check("プリフライト: 動画なしはFAIL", (1, "動画がない") in _fails and (2, "動画がない") in _fails)
    check("プリフライト: 題名の読みはFAIL", (2, "題名に読みがなのカッコ書き") in _fails, repr(_fails))
    check("プリフライト: 要点不足・字幕なし・未解決の事実確認はWARN",
          (2, "画面の要点が3個でない") in _warns and (1, "字幕ファイルがない") in _warns
          and (2, "事実確認の未解決（intro）") in _warns, repr(_warns))
    _md = render_review("20990101", [], _checks, {"source": "llm", "factcheck": {"status": "fixed"}})
    check("確認用まとめ: FAIL件数を見出しに出す", "FAIL " in _md and "事実確認: **fixed**" in _md)

with _tempfile.TemporaryDirectory() as _td:
    _td = Path(_td)
    (_td / "state").mkdir()
    (_td / "output").mkdir()
    _cfg = {"paths": {"state_dir": str(_td / "state"), "output_dir": str(_td / "output")},
            "shorts": {"topics_per_day": 3}, "youtube": {}, "channel": {}}
    (_td / "state" / "news_20990102.json").write_text(_json.dumps({"articles": [
        {"title_ja": "記事1", "summary_ja_long": "s", "link": "https://e/1"}]}), encoding="utf-8")
    (_td / "state" / "script_meta_20990102.json").write_text(_json.dumps({
        "source": "llm", "factcheck": {"status": "ok", "articles": []},
        "articles": [{"hook_title": "ニュースが1本だけの日の題名です", "points": ["a1", "b2", "c3"]}]}),
        encoding="utf-8")
    _c2 = preflight("20990102", _cfg)
    check("プリフライト: ニュースが3本未満の日は無い本数を FAIL にしない",
          not any(c["topic"] >= 2 for c in _c2), repr(_c2))

_meta = {"articles": [{"hook_title": "旧題名その一です", "points": ["a"]}, {"hook_title": "旧題名その二です"}],
         "factcheck": {"status": "unresolved", "articles": [
             {"index": 1, "fixed": [], "unresolved": [{"field": "hook_title", "problem": "p"}]}]}}
_md0 = "**セリフ**: 「人間(にんげん)なら1年かかる」\n**セリフ**: 「別の文」"
_new_md, _new_meta, _chg = plan_fixes({
    "titles": {"1": "Claude(クロード)が隠れた弱点493件を発見"},
    "points": {"2": ["要点その一", "要点その二", "要点その三"]},
    "serif": [{"old": "人間(にんげん)なら1年かかる", "new": "人間なら数カ月から1年かかる"}],
}, _md0, _meta)
check("修正反映: 題名の読みを外す", _new_meta["articles"][0]["hook_title"] == "Claudeが隠れた弱点493件を発見")
check("修正反映: 要点を置き換え", _new_meta["articles"][1]["points"] == ["要点その一", "要点その二", "要点その三"])
check("修正反映: セリフ置換＋ルビ自動付与", "数(すう)カ月" in _new_md and "1年" in _new_md and "別の文" in _new_md, _new_md)
check("修正反映: 元データは変えない", _meta["articles"][0]["hook_title"] == "旧題名その一です")
check("修正反映: 人が直した題名の要確認は解消",
      _new_meta["factcheck"]["status"] == "fixed" and not _new_meta["factcheck"]["articles"][0]["unresolved"])
_md_r, _, _ = plan_fixes({"serif": [{"old": "別の文", "new": "脳(ずのう)データ"}]}, _md0, _meta)
check("修正反映: 書かれたルビ（読みの手直し）は尊重する", "脳(ずのう)データ" in _md_r, _md_r)
for _bad, _why in [
    ({"serif": [{"old": 123, "new": "x"}]}, "文字列"),
    ({"serif": [{"old": "存在しない文", "new": "x"}]}, "old が台本に0回"),
    ({"titles": {"5": "範囲外の記事番号の題名です"}}, "範囲外"),
    ({"titles": {"1": "あ" * 30}}, "1〜26字"),
    ({"points": {"1": ["あ" * 25]}}, "要点"),
    ({"title": {"1": "キー名の誤り"}}, "未知のキー"),
    ({"serif": {"old": "x", "new": "y"}}, "配列"),
    (["not", "a", "dict"], "JSONオブジェクト"),
]:
    try:
        plan_fixes(_bad, _md0, _meta)
        check(f"修正反映: 不正を拒否（{_why}）", False)
    except FixError as _e:
        check(f"修正反映: 不正を拒否（{_why}）", _why in str(_e), str(_e))

# --- 15. データ収集の修正（2026-11-01: 台帳・文字コード・材料不足・フィード監視・custom-news 分離ほか） ----
import httpx as _httpx
import news_collector as _nc2
import seen_topics as _st
import api_utils as _au
from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
from article_extractor import clean_extracted_text


def _mock_httpx(handler):
    """news_collector が作る httpx.Client を MockTransport 付きにする（ネットワーク不要）."""
    orig = _httpx.Client
    _nc2.httpx.Client = lambda **kw: orig(transport=_httpx.MockTransport(handler), **kw)
    return orig


def _rss(items: list[tuple[str, str, str]], title: str = "Feed") -> bytes:
    """(title, link, pubDate) のリストから RSS 2.0 を作る."""
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate>"
                   f"<description>&lt;p&gt;概要 &amp;amp; 本文&lt;/p&gt;</description></item>"
                   for t, l, d in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>'
            f"{body}</channel></rss>").encode("utf-8")


def _rfc(hours_ago: float) -> str:
    from email.utils import format_datetime
    return format_datetime(_dt2.now(_tz2.utc) - _td2(hours=hours_ago))


# B12: RSS の題名・概要の文字参照（二重エスケープ）とタグ
check("RSS文字参照: &amp;Truth → &Truth", _nc2._clean_text("A new startup called &amp;Truth") == "A new startup called &Truth")
check("RSS文字参照: 二重エスケープ &amp;#8217; → ’", _nc2._clean_text("Meta&amp;#8217;s Muse") == "Meta’s Muse")
check("RSSタグ除去: <p><b>…</b></p>", _nc2._clean_text("<p>Hello <b>world</b></p><br/>") == "Hello world")
check("RSSタグ除去: 本文の不等号（a &lt; b &gt; c）は消さない", _nc2._clean_text("a &lt; b &gt; c") == "a < b > c")

# E6: URL の正規化
check("URL正規化: utm・フラグメント・末尾の/・www・httpの違いを同一視",
      len({_st.url_key(u) for u in (
          "https://www.example.com/a/b/?utm_source=x&utm_medium=y#top",
          "http://example.com/a/b", "https://EXAMPLE.com/a/b/")}) == 1)
check("URL正規化: 意味のあるクエリは残す", _st.url_key("https://e.com/w?v=1") != _st.url_key("https://e.com/w?v=2"))
check("URL保存形: 追跡用クエリだけ外し、何も外さないときは元のまま",
      _st.clean_link("https://e.com/p?id=3&utm_campaign=z#c") == "https://e.com/p?id=3"
      and _st.clean_link("https://e.com/p?b=2&a=1") == "https://e.com/p?b=2&a=1")
check("og:image: 相対URLと&amp;を解決",
      _nc2._og_image_url('<meta property="og:image" content="/img/a.jpg?w=1&amp;h=2">',
                         "https://site.jp/news/1.html") == "https://site.jp/img/a.jpg?w=1&h=2")
check("og:image: 引用符なしの属性値・content が先でも取れる（Forbes Japan 形式）",
      _nc2._og_image_url("<meta content=https://img.example/a.jpg property=og:image>", "https://f.jp/x")
      == "https://img.example/a.jpg"
      and _nc2._og_image_url('<meta name="og:image" content="//cdn.example/b.png">', "https://f.jp/x")
      == "https://cdn.example/b.png")
check("dedup: utm付きと無しの同じ記事は1件",
      len(_nc2.deduplicate([{"link": "https://e.com/x?utm_source=rss"}, {"link": "https://e.com/x/"}])) == 1)

# B6: 日付の解析（タイムゾーン・小数秒・未来の日付）
_d = _nc2._parse_published_date("2026-05-12T21:30:33+09:00")
check("日付: +09:00 を UTC に正しく換算（上書きしない）", _d is not None and _d.utcoffset() == _td2(hours=9)
      and _d.astimezone(_tz2.utc).hour == 12, repr(_d))
check("日付: 小数秒と Z を読める",
      _nc2._parse_published_date("2026-05-12T12:30:33.123Z") is not None
      and _nc2._parse_published_date("2026-05-12T12:30:33.123456-04:00") is not None)
_now = _dt2.now(_tz2.utc)
_fut = [{"title": "far future", "published_at": (_now + _td2(hours=40)).isoformat()},
        {"title": "tz typo", "published_at": (_now + _td2(hours=9)).isoformat()},
        {"title": "old", "published_at": (_now - _td2(hours=60)).isoformat()},
        {"title": "no date"}]
check("日付: 遠い未来は除外・TZずれ程度の未来と日付なしは残す",
      [a["title"] for a in _nc2.filter_by_freshness(_fut, 48)] == ["tz typo", "no date"])

# B5 / B7 / B14: フィードを httpx で取り、異常を記録。新しさで絞ってから件数制限
_many = [(f"AI item {i}", f"https://g.example/{i}", _rfc(i * 0.5)) for i in range(35)]
_old_first = ([(f"old AI {i}", f"https://o.example/old{i}", _rfc(100 + i)) for i in range(12)]
              + [(f"fresh AI {i}", f"https://o.example/new{i}", _rfc(1 + i)) for i in range(11)])


def _feed_handler(req):
    u = str(req.url)
    if "many" in u:
        return _httpx.Response(200, content=_rss(_many, "Many"))
    if "oldfirst" in u:
        return _httpx.Response(200, content=_rss(_old_first, "OldFirst"))
    if "ratelimited" in u:
        return _httpx.Response(429, content=b"slow down")
    if "empty" in u:
        return _httpx.Response(200, content=_rss([], "Empty"))
    raise _httpx.ConnectError("getaddrinfo failed", request=req)


_stats: list = []
_orig_client = _mock_httpx(_feed_handler)
try:
    _got = _nc2.collect_from_rss(["https://many.example/feed", "https://oldfirst.example/feed",
                                  "https://ratelimited.example/feed", "https://empty.example/feed",
                                  "https://dns-dead.example/feed"],
                                 max_per_feed=30, max_age_hours=48, stats=_stats)
finally:
    _nc2.httpx.Client = _orig_client
_by = {s["url"].split("//")[1].split(".")[0]: s for s in _stats}
check("フィード: 1フィード30件まで（35件中）", _by["many"]["kept"] == 30, repr(_by["many"]))
check("フィード: 古い記事が先頭でも新しい11件を取りこぼさない",
      _by["oldfirst"]["kept"] == 11 and sum(1 for a in _got if a["title"].startswith("fresh AI")) == 11)
check("フィード: 新しい順に並ぶ", [a["title"] for a in _got[:2]] == ["AI item 0", "AI item 1"])
check("フィード: 429・0件・DNS失敗を記録",
      _by["ratelimited"]["status"] == 429 and _by["empty"]["entries"] == 0 and "ConnectError" in _by["dns-dead"]["error"])
check("フィード: 取れないフィードを確認用の警告にする（3件）", len(_nc2._feed_warnings(_stats)) == 3,
      repr(_nc2._feed_warnings(_stats)))
check("フィード: 概要のタグ・二重エスケープを除去", _got[0]["summary"] == "概要 & 本文", repr(_got[0]["summary"]))
check("フィード: published_at を UTC で持つ", _got[0]["published_at"].endswith("+00:00"))
_il = _nc2.interleave_by_source([{"source": "A", "n": 1}, {"source": "A", "n": 2}, {"source": "B", "n": 3}])
check("上限前にフィードを交互に並べる", [a["n"] for a in _il] == [1, 3, 2])
_feeds = config["news"]["rss_feeds"]
check("config: 引けない・転送されるフィードURLを差し替え済み",
      not any("feeds.technologyreview.com" in f or "technology-lab" in f or "/blog/rss.xml" in f
              or "blog.google/technology/ai" in f or "venturebeat.com/category" in f for f in _feeds)
      and "https://arstechnica.com/ai/feed/" in _feeds and "https://openai.com/news/rss.xml" in _feeds)

# B11: Google News RSS（カスタムトピック）
_gn_rss = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
           '<item><title>新型EVが発表 - 車ニュース</title>'
           '<link>https://news.google.com/rss/articles/CBMiABC123?oc=5</link>'
           f'<pubDate>{_rfc(2)}</pubDate><description>&lt;a href="x"&gt;新型EVが発表&lt;/a&gt;</description>'
           '<source url="https://cars.example.jp">車ニュース</source></item></channel></rss>').encode("utf-8")
_orig_client = _mock_httpx(lambda req: _httpx.Response(200, content=_gn_rss))
try:
    _gn = _nc2.collect_from_google_news("車", max_age_hours=48, stats=[])
finally:
    _nc2.httpx.Client = _orig_client
check("Google News: ja/en の2版から取得し、題名末尾の媒体名を外す",
      len(_gn) == 2 and _gn[0]["title"] == "新型EVが発表" and _gn[0]["source"] == "車ニュース"
      and _gn[0]["source_url"] == "https://cars.example.jp" and _gn[0]["summary"] == "", repr(_gn[:1]))
check("Google News: 転送URLを判定", _nc2.is_google_news_url(_gn[0]["link"])
      and not _nc2.is_google_news_url("https://cars.example.jp/a"))
_be = (")]}'\n\n" + _json.dumps([["wrb.fr", "Fbv4je", _json.dumps(
    ["garturlres", "https://cars.example.jp/articles/1?a=1&b=2", 1]), None, None, None, ""], ["di", 14]]))
check("Google News: batchexecute の応答から元記事URLを取り出す",
      _nc2._parse_batchexecute_url(_be) == "https://cars.example.jp/articles/1?a=1&b=2")
check("Google News: 壊れた応答は空文字", _nc2._parse_batchexecute_url(")]}'\n\nnot json") == "")
_src_cn = inspect.getsource(_nc2.collect_news)
check("custom-news: Google CSE だけに頼らず Google News を使う", "collect_from_google_news" in _src_cn)

# B2: 文字コード（charset ヘッダーなし・meta が shift_jis の ITmedia）
_sjis = '<html><head><meta charset="shift_jis"></head><body><p>生成AIの記事①です</p></body></html>'.encode("cp932")
check("文字コード: meta の shift_jis で読める（resp.text 相当の UTF-8 決め打ちで化けない）",
      "生成AIの記事①です" in _nc2.decode_html(_sjis, "text/html"))
check("文字コード: ヘッダーの charset を優先", "日本語" in _nc2.decode_html("日本語".encode("euc_jp"), "text/html; charset=EUC-JP"))
check("文字コード: 宣言がなくても推定して読める",
      "人工知能のニュースです" in _nc2.decode_html(("<p>" + "人工知能のニュースです。" * 20 + "</p>").encode("cp932")))
_bad_utf8 = ('<meta charset="utf-8"><p>' + "生成AIのニュース本文です。" * 200 + "</p>").encode("utf-8") + b"\xff"
check("文字コード: UTF-8 宣言で不正バイトが1つだけなら宣言どおりに読む（推定に任せて化けさせない）",
      "生成AIのニュース本文です。" in _nc2.decode_html(_bad_utf8, "text/html"))
check("文字化け率: 置換文字1%超を検出", _nc2.replacement_ratio("a" * 98 + "��") > _nc2._MAX_REPLACEMENT_RATIO
      and _nc2.replacement_ratio("正常な本文") == 0)

# B3: ブラウザと同じ形の UA（openai.com は "Mozilla/5.0" だけだと 403）
check("UA: ブラウザ形式", "AppleWebKit" in _nc2._PAGE_HEADERS["User-Agent"] and "Chrome/" in _nc2._PAGE_HEADERS["User-Agent"])

# B4: 先頭20万字で切らない（Wired は本文が46万字目以降）
_big = ("<html><head><script>" + "x" * 470000 + "</script></head><body><article>"
        + "<p>" + "これは記事の本文の段落です。十分な長さがあります。" * 4 + "</p>" * 1
        + "<p>" + "二つ目の段落も本文として抽出されるべき内容です。" * 4 + "</p></article></body></html>")
check("本文抽出: 本文が47万字目以降でも取れる", "二つ目の段落" in extract_main_text(_big))
check("本文取得: ページを先頭で切っていない", "[:200000]" not in inspect.getsource(_nc2._fetch_article_page))

# B8: 定型文・重複行の除去
_bp = "\n".join([
    "親愛なる読者の皆さまへ。…今すぐ寄付は上のボタンから！ ・これまでGIGAZINEを支援してくれたメンバーのリスト",
    "Buy One Founder Summit Ticket, Get a Second for Free. Register now.",
    "Disrupt doors open Oct. 13. Get your pass and bring someone with you at 50% off. REGISTER NOW.",
    "Apple will launch Apple Intelligence in China with iOS 27.3.",
    "Apple will launch Apple Intelligence in China with iOS 27.3.",
    "At TechCrunch Disrupt earlier this month, industry insiders tempered much of this criticism.",
    "この記事は会員限定です。会員登録すると全てご覧いただけます。",
    "in サイエンス, Posted by log1h_ik",
    "You can read the machine translated English article A study found…",
    "Copyright © ITmedia, Inc. All Rights Reserved.",
    "Stay connected",
    "関連記事",
    "Back to top",
])
_cl = clean_extracted_text(_bp).split("\n")
check("定型文: 寄付・イベント広告・会員案内・投稿者行・著作権・ナビを除去し本文は残す",
      _cl == ["Apple will launch Apple Intelligence in China with iOS 27.3.",
              "At TechCrunch Disrupt earlier this month, industry insiders tempered much of this criticism."], repr(_cl))
_long_ad = "Register now " + "本文の文章が続きます。" * 30
check("定型文: 長い本文行は広告語を含んでも消さない", clean_extracted_text(_long_ad) == _long_ad)
check("requirements: trafilatura と charset-normalizer",
      all(k in Path("requirements.txt").read_text(encoding="utf-8") for k in ("trafilatura", "charset-normalizer")))

# B10: NG キーワードの漏れと誤爆
_ng_more = ["AIで性的な画像を作るアプリが問題に", "性的ディープフェイクの被害が拡大", "Nudify apps are spreading",
            "Undress AI tools face lawsuits", "DeepNude clone returns", "Law targets NCII on platforms",
            "Grok generated nonconsensual intimate images", "写真を裸にするAIが拡散", "脱衣AIアプリを規制"]
_ng_ok = ["裸眼で見える3Dディスプレイ", "個人データが丸裸に、AIの脅威", "個性的なキャラをAIが生成",
          "女性的なデザインの新型ロボット", "脱衣所の見守りセンサーにAI", "赤裸々に語るAI開発の舞台裏",
          "性的指向のデータを扱うAIの倫理指針", "裸足で歩くロボットの研究"]
_blocked = [t for t in _ng_more if _nc2.filter_excluded_content([{"title": t, "summary": ""}])]
_wrong = [t for t in _ng_ok if not _nc2.filter_excluded_content([{"title": t, "summary": ""}])]
check("NGフィルタ: 追加語（性的な・ディープフェイク・裸・脱衣・Nudify・undress・NCII等）を除外", not _blocked, repr(_blocked))
check("NGフィルタ: 裸眼・丸裸・個性的な・女性的な・脱衣所・赤裸々・性的指向・裸足は通す", not _wrong, repr(_wrong))
_ng_compound = ["児童性的虐待コンテンツの拡散", "AI生成性的画像が問題に", "児童性犯罪の摘発にAI", "児童性的な画像を生成"]
_ng_compound_ok = ["裸子植物のゲノムをAIで解析", "慢性的な人手不足をAIで解消", "知性的なAI", "理性的な判断"]
check("NGフィルタ: 漢字に続く複合語（児童性的虐待・生成性的画像・児童性犯罪）も除外（QA指摘の回帰）",
      not [t for t in _ng_compound if _nc2.filter_excluded_content([{"title": t, "summary": ""}])])
check("NGフィルタ: 裸子植物・慢性的な・知性的な・理性的な は通す",
      not [t for t in _ng_compound_ok if not _nc2.filter_excluded_content([{"title": t, "summary": ""}])])
check("NGフィルタ: 本文取得後の再チェック（include_body）",
      len(_nc2.filter_excluded_content([{"title": "AI news", "summary": "", "full_text": "...deepnude..."}],
                                       include_body=True)) == 0
      and len(_nc2.filter_excluded_content([{"title": "AI news", "summary": "", "full_text": "...deepnude..."}])) == 1)

# B3: 材料不足は選ばず次点で埋める／本文にNG語があれば外す
_bodies = {"https://a/1": "", "https://a/2": "本文" * 150, "https://a/3": "本文にdeepnudeの話" * 30,
           "https://a/4": "本文" * 120, "https://a/5": "本文" * 200}
_calls: list = []


def _fake_fetch(client, article, fetch_full_text, max_text_chars):
    _calls.append(article["link"])
    article["full_text"] = _bodies[article["link"]]
    return "<html></html>", article["link"]


_orig_fetch, _orig_save = _nc2._fetch_article_page, _nc2._save_og_image
_nc2._fetch_article_page = _fake_fetch
_nc2._save_og_image = lambda client, article, html, url, stem: article.__setitem__("og_image", str(stem) + ".jpg")
try:
    with _tempfile.TemporaryDirectory() as _ogd:
        _ogd = Path(_ogd) / "og_images" / "20991231"
        _ogd.mkdir(parents=True)
        (_ogd / "og_2.png").write_bytes(b"stale")
        _cands = [{"title": f"t{i}", "link": f"https://a/{i}", "summary": "短い概要"} for i in range(1, 6)]
        _none, _ = _nc2.select_with_material([dict(_cands[0])], 2, _ogd, fetch_full_text=True)
        _stale_kept_on_fail = (_ogd / "og_2.png").exists() and not _none
        _sel, _skip = _nc2.select_with_material(_cands, 2, _ogd, fetch_full_text=True)
        _stale_gone = not (_ogd / "og_2.png").exists()
finally:
    _nc2._fetch_article_page, _nc2._save_og_image = _orig_fetch, _orig_save
check("材料不足: 本文なしを外し次点で top_n を満たす",
      [a["link"] for a in _sel] == ["https://a/2", "https://a/4"], repr([a["link"] for a in _sel]))
check("材料不足: 外した理由を記録（材料不足・本文NG）",
      [a.get("skip_reason", "")[:4] for a in _skip] == ["材料不足", "本文にN"], repr([a.get("skip_reason") for a in _skip]))
check("材料不足: 埋まったら残りは取得しない",
      _calls == ["https://a/1", "https://a/1", "https://a/2", "https://a/3", "https://a/4"], repr(_calls))
check("OG画像: 1件も選べなかったときは前回の画像を消さない（前回の news JSON が使う）", _stale_kept_on_fail)
check("OG画像: 採用順の番号で日付フォルダに保存し、前回の残りを消す",
      _sel[0]["og_image"].endswith(str(Path("20991231") / "og_1.jpg")) and _sel[1]["og_image"].endswith("og_2.jpg")
      and _stale_gone, repr([a["og_image"] for a in _sel]))
check("OG画像: collect_news が日付ごとのフォルダを使う", '"og_images" / (f"{date_str}_' in _src_cn)


class _CountingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, system, user, max_tokens=500, temperature=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature})
        r = self.responses.pop(0) if self.responses else ""
        if isinstance(r, Exception):
            raise r
        return r


_fake = _CountingClient([_json.dumps({"summary_ja_long": "要約です" * 20, "facts": ["事実"], "kid_angle": "x"})])
_orig_llm = _au.LLMClient
_au.LLMClient = lambda cfg: _fake
try:
    _arts = [{"title_ja": "本文なし", "summary": "短い"}, {"title_ja": "本文あり", "full_text": "本文" * 150}]
    _nc2.summarize_articles_with_llm(_arts, {"provider": "claude"})
finally:
    _au.LLMClient = _orig_llm
check("要約: 材料200字未満の記事は要約・factsを作らない",
      len(_fake.calls) == 1 and "facts" not in _arts[0] and _arts[1].get("facts") == ["事実"])

# E3 / E4 / B13: 採点の型・全件失敗・認証エラー・同点
check("採点: score を数値に（文字列・範囲外・不正値）",
      _nc2._to_score("7.5") == 7.5 and _nc2._to_score(12) == 10.0 and _nc2._to_score("abc") == 0.0
      and _nc2._to_score(None) == 0.0)
check("採点: relevant の文字列 \"false\" を False に",
      _nc2._to_bool("false") is False and _nc2._to_bool("False") is False and _nc2._to_bool("true") is True
      and _nc2._to_bool(None) is True and _nc2._to_bool(0) is False)
_fake = _CountingClient([_json.dumps({"relevant": "false", "score": "8", "title_ja": "無関係な記事", "summary_ja": "x"}),
                         _json.dumps({"relevant": "true", "score": "7.6", "title_ja": "関係ある記事", "summary_ja": "y"})])
_au.LLMClient = lambda cfg: _fake
_orig_sleep = _nc2.time.sleep
_nc2.time.sleep = lambda s: None
try:
    _sc = _nc2.score_articles_with_llm([{"title": "a", "source": "s", "summary": ""},
                                        {"title": "b", "source": "s", "summary": ""}], {"provider": "claude"})
    check("採点: 文字列の score でも並べ替えで落ちない・\"false\" は除外扱い",
          [a["title_ja"] for a in _sc] == ["関係ある記事", "無関係な記事"] and _sc[1]["relevant"] is False
          and _sc[1]["score"] == 0 and _sc[0]["score"] == 7.6)
    check("採点: プロンプトにクリーニングと小数第1位の指示",
          cleaning_short() in _fake.calls[0]["system"] and "小数第1位" in _fake.calls[0]["system"])
    _fake = _CountingClient(["壊れた応答"] * 20)
    _au.LLMClient = lambda cfg: _fake
    try:
        _nc2.score_articles_with_llm([{"title": "a", "source": "s", "summary": ""}] * 2, {"provider": "claude"})
        check("採点: 全件失敗なら例外（キーワード採点に切り替えられる）", False)
    except RuntimeError as _e:
        check("採点: 全件失敗なら例外（キーワード採点に切り替えられる）", "all 2 articles" in str(_e), str(_e))
    _fake = _CountingClient([_au.LLMAuthError("401")] * 20)
    _au.LLMClient = lambda cfg: _fake
    try:
        _nc2.score_articles_with_llm([{"title": "a", "source": "s", "summary": ""}] * 5, {"provider": "claude"})
        check("採点: 認証エラーは再試行せず即座に失敗", False)
    except _au.LLMAuthError:
        check("採点: 認証エラーは再試行せず即座に失敗", len(_fake.calls) == 1, f"{len(_fake.calls)} calls")
finally:
    _au.LLMClient = _orig_llm
    _nc2.time.sleep = _orig_sleep
check("採点: LLMAuthError は RuntimeError の一種（既存の except と互換）", issubclass(_au.LLMAuthError, RuntimeError))
_tie = sorted([{"score": 7.3, "published_at": "2026-10-31T01:00:00+00:00", "n": "old"},
               {"score": 7.3, "published_at": "2026-11-01T01:00:00+00:00", "n": "new"},
               {"score": "8.0", "published_at": "", "n": "top"}], key=_nc2.rank_key, reverse=True)
check("同点: 新しい記事を先に", [a["n"] for a in _tie] == ["top", "new", "old"])

import anthropic as _anthropic
_orig_auth = _au.resolve_anthropic_auth
_tokens = ["k1"]
_au.resolve_anthropic_auth = lambda *a, **k: ("api_key", _tokens[0])
try:
    _lc = _au.LLMClient({"provider": "claude", "claude": {"auth_mode": "api_key"}})
    _c1, _ = _lc._claude_client()
    _c2, _chg0 = _lc._claude_client()
    check("LLMクライアント: 使い回す（毎回作らない）", _c1 is _c2 and _chg0 is False)
    _tokens[0] = "k2"
    _c3, _chg = _lc._claude_client()
    check("LLMクライアント: トークンが変わったら作り直す", _c3 is not _c1 and _chg is True)

    class _AuthFail:
        class messages:
            @staticmethod
            def create(**kw):
                raise _anthropic.AuthenticationError(
                    "bad key", response=_httpx.Response(401, request=_httpx.Request("POST", "https://api")), body=None)

    _lc._client = _AuthFail()
    try:
        _lc.generate("sys", "user", 10)
        check("LLMクライアント: 401 は LLMAuthError", False)
    except _au.LLMAuthError:
        check("LLMクライアント: 401 は LLMAuthError", True)
finally:
    _au.resolve_anthropic_auth = _orig_auth

# E5: クラスタリング失敗を警告として残す
_au.LLMClient = lambda cfg: _FakeClient("これはJSONではありません")
_cw: list = []
try:
    nc.cluster_topics_with_llm(articles, {"provider": "gemini"}, pool_size=6, warnings=_cw)
finally:
    _au.LLMClient = _orig_llm
check("クラスタ失敗: 警告に残す", len(_cw) == 1 and "クラスタリング" in _cw[0]["check"])

# B1: 消えない台帳と、別媒体の同じ話の除外
with _tempfile.TemporaryDirectory() as _sd:
    _sd = Path(_sd)
    (_sd / "old").mkdir()
    (_sd / "old" / "news_20261025.json").write_text(_json.dumps({"date": "20261025", "topic": "AI", "articles": [
        {"title": "OpenAI will stop training models on the live internet", "title_ja": "OpenAI、AIモデルのインターネット学習を中止へ",
         "link": "https://techcrunch.com/2026/10/24/openai-internet/?utm_source=rss", "summary_ja": "安全のため"},
        {"title": "Agents you can text", "title_ja": "テキストで使えるAIエージェント", "link": "https://techcrunch.com/agents"},
        {"title": "Fourth story", "title_ja": "4番目の話", "link": "https://e.com/4"},
        {"title": "Fourth story", "title_ja": "4番目の話", "link": "https://e.com/4b"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (_sd / "upload_log.json").write_text(_json.dumps([
        {"date": "20261024", "kind": "shorts_topic2", "title": "OpenAI、AIの学習をネットから切り離す #ai #news #shorts"},
        {"date": "20261025", "kind": "shorts_topic1", "title": "x"},
    ], ensure_ascii=False), encoding="utf-8")
    _led = _st.sync_ledger(_sd)
    _led2 = _st.sync_ledger(_sd)
    check("台帳: news JSON（state/old）と upload_log から初期データを作る",
          (_sd / "seen_topics.json").exists() and len(_led) == 5
          and any(e["date"] == "20261024" and e["title_ja"] == "OpenAI、AIの学習をネットから切り離す" and e["uploaded"]
                  for e in _led), repr(_led))
    check("台帳: 投稿済みの印（shorts_topic1 → rank1）",
          [e["uploaded"] for e in _led if e["date"] == "20261025"] == [True, False, False, False])
    check("台帳: 何度取り込んでも増えない", len(_led2) == len(_led))
    (_sd / "old" / "news_20261025.json").unlink()  # news JSON を消しても台帳には残る
    check("台帳: news JSON を消しても消えない", len(_st.sync_ledger(_sd)) == 5)
    _cands = [{"title": "OpenAI cuts models off from the internet", "link": "https://techcrunch.com/2026/10/24/openai-internet",
               "title_ja": "OpenAI、学習中のAIをネット遮断へ"},
              {"title": "OpenAI cuts off the internet", "link": "https://www.itmedia.co.jp/x.html",
               "title_ja": "OpenAI、AIモデルのインターネット学習を中止へ　安全対策"},
              {"title": "Totally new", "link": "https://e.com/new", "title_ja": "まったく別の新しい話題です"}]
    _kept_url = _nc2.deduplicate_against_past(list(_cands), _sd, "20261027")
    check("過去URL: 台帳と照合（utm・末尾の/の違いを無視）",
          [a["link"] for a in _kept_url] == ["https://www.itmedia.co.jp/x.html", "https://e.com/new"])
    check("過去: 当日分は比べない（作り直しを妨げない）",
          len(_nc2.deduplicate_against_past(list(_cands), _sd, "20261025")) == 3)
    _fake = _CountingClient([_json.dumps({"duplicates": [{"candidate": 1, "past": "P1", "event": "OpenAIのネット遮断"}]})])
    _au.LLMClient = lambda cfg: _fake
    try:
        _kept = _nc2.deduplicate_stories_against_past(list(_kept_url), {"provider": "claude"}, _sd, "20261027",
                                                      max_rank=2)
    finally:
        _au.LLMClient = _orig_llm
    check("過去の同じ話: LLM が同じと判定した候補を除く", [a["link"] for a in _kept] == ["https://e.com/new"], repr(_kept))
    _sys = _fake.calls[0]["system"]
    check("過去の同じ話: プロンプトにクリーニング・温度0・max_tokens・投稿済みと上位のみ",
          cleaning_short() in _sys and "__CLEANING__" not in _sys and _fake.calls[0]["temperature"] == 0
          and _fake.calls[0]["max_tokens"] >= 400 and "4番目の話" not in _fake.calls[0]["user"]
          and "切り離す" in _fake.calls[0]["user"], _fake.calls[0]["user"])
    _fake = _CountingClient([_json.dumps({"duplicates": [{"candidate": 1, "past": "P99"}, {"candidate": 2}]})])
    _au.LLMClient = lambda cfg: _fake
    try:
        _kept_bad = _nc2.deduplicate_stories_against_past(list(_kept_url), {"provider": "claude"}, _sd, "20261027")
    finally:
        _au.LLMClient = _orig_llm
    check("過去の同じ話: どの過去記事か示せない判定（範囲外・欠落）は採らない", len(_kept_bad) == 2, repr(_kept_bad))
    check("過去の同じ話: テーマが近いだけなら別、という規則", "テーマや分野が近いだけ" in _nc2.PAST_DEDUP_SYSTEM_PROMPT)
    _fake = _CountingClient([_json.dumps({"duplicates": [{"candidate": 2, "past": "P1", "event": "AIの話"}]})])
    _au.LLMClient = lambda cfg: _fake
    try:
        _kept_guard = _nc2.deduplicate_stories_against_past(list(_kept_url), {"provider": "claude"}, _sd, "20261027")
    finally:
        _au.LLMClient = _orig_llm
    check("過去の同じ話: 共通の固有名も題名の似かたもない組は LLM が同じと言っても残す", len(_kept_guard) == 2)
    _fake = _CountingClient(["壊れた応答"])
    _au.LLMClient = lambda cfg: _fake
    _pw: list = []
    try:
        _kept = _nc2.deduplicate_stories_against_past(list(_kept_url), {"provider": "claude"}, _sd, "20261027",
                                                      warnings=_pw)
    finally:
        _au.LLMClient = _orig_llm
    check("過去の同じ話: LLM 失敗時は題名の似かたで判定し、別の話は残す",
          [a["link"] for a in _kept] == ["https://e.com/new"] and len(_pw) == 1, repr([a["link"] for a in _kept]))
    _fake = _CountingClient([_au.LLMAuthError("401")])
    _au.LLMClient = lambda cfg: _fake
    _aw: list = []
    try:
        _kept_auth = _nc2.deduplicate_stories_against_past(list(_kept_url), {"provider": "claude"}, _sd, "20261027",
                                                           warnings=_aw)
        check("過去の同じ話: 認証エラーでも収集を止めず簡易判定に切り替える（QA指摘）",
              [a["link"] for a in _kept_auth] == ["https://e.com/new"] and len(_aw) == 1)
    except Exception as _e:
        check("過去の同じ話: 認証エラーでも収集を止めず簡易判定に切り替える（QA指摘）", False, repr(_e))
    finally:
        _au.LLMClient = _orig_llm
    _st.record_selection(_sd, "20261027", "", [{"title": "t", "title_ja": "今日の記事", "link": "https://e.com/today"}])
    _st.record_selection(_sd, "20261027", "", [{"title": "t2", "title_ja": "作り直した記事", "link": "https://e.com/today2"}])
    _today = [e for e in _st.load_ledger(_sd) if e["date"] == "20261027"]
    check("台帳: 同じ日の作り直しは未投稿の行を置き換える", [e["title_ja"] for e in _today] == ["作り直した記事"])
with _tempfile.TemporaryDirectory() as _bd:
    _bd = Path(_bd)
    (_bd / "seen_topics.json").write_text("[]", encoding="utf-8")
    _ok_list = _st.sync_ledger(_bd) == [] and (_bd / "seen_topics.broken.json").exists()
    (_bd / "seen_topics.json").write_text(_json.dumps({"entries": [{"title": "date欠落"}, "文字列の行",
                                                                 {"date": "20261020", "key": "k", "rank": "x"}]}),
                                          encoding="utf-8")
    _ok_rows = [e.get("key") for e in _st.sync_ledger(_bd)] == ["k"]
    _ok_rank = len(_nc2.deduplicate_stories_against_past([{"title": "a"}], {"provider": "claude"}, _bd, "20261021")) == 1
    (_bd / "old").mkdir()
    (_bd / "old" / "news_20261019.json").write_text(_json.dumps({"articles": [{"link": None, "title": None}, 5]}),
                                                    encoding="utf-8")
    (_bd / "upload_log.json").write_text(_json.dumps([{"date": "20261019", "kind": "shorts_topic9", "title": 123}]),
                                         encoding="utf-8")
    try:
        _ok_types = len(_st.sync_ledger(_bd)) == 3
    except Exception as _e:
        _ok_types = False
check("台帳: 形が違う（[]・date欠落・文字列の行・数字でない rank・null の link・数値の title）でも落ちない（QA指摘）",
      _ok_list and _ok_rows and _ok_rank and _ok_types)
_orig_sync = _st.sync_ledger
_st.sync_ledger = lambda d: (_ for _ in ()).throw(OSError("disk error"))
_lw: list = []
try:
    _r1 = _nc2.deduplicate_against_past([{"link": "https://e.com/1"}], Path("."), "20261027", warnings=_lw)
    _r2 = _nc2.deduplicate_stories_against_past([{"title": "a"}], {"provider": "claude"}, Path("."), "20261027")
    check("台帳: 読めなくても収集は続け、警告に残す（QA指摘）", len(_r1) == 1 and len(_r2) == 1 and len(_lw) == 1)
except Exception as _e:
    check("台帳: 読めなくても収集は続け、警告に残す（QA指摘）", False, repr(_e))
finally:
    _st.sync_ledger = _orig_sync
check("固有名の共通: Pentagon/OpenAI は共通、Phonic-1 と Prism・Gemini と Apple は共通なし",
      _st.shares_entity({"title": "How a Contractor's Claude Agent Got Into the Pentagon"},
                        {"title": "A Defense Contractor Gave Their AI Agent Access to a Pentagon System"})
      and _st.shares_entity({"title_ja": "OpenAI、学習中のAIをネット遮断へ"}, {"title_ja": "OpenAI、AIの学習をネットから切り離す"})
      and not _st.shares_entity({"title_ja": "音声AI「Phonic-1」が登場"}, {"title_ja": "音声AI「Prism」が登場"})
      and not _st.shares_entity({"title_ja": "GeminiがGoogle Docsを直接編集"}, {"title_ja": "AppleがPages・Keynoteに新AI機能"})
      and not _st.shares_entity({"title_ja": "AIエージェントの新ツール"}, {"title_ja": "エージェント向けツールを公開"}))
check("題名の簡易判定: 別の話を同じとみなさない",
      not _st.is_similar_title({"title_ja": "AnthropicのIPO目論見書が明かす衝撃"},
                               {"title_ja": "Appleが3つのアプリにAI機能"})
      and not _st.is_similar_title({"title_ja": "OpenAIがGPT-6.1を公開"}, {"title_ja": "OpenAIが新しいブラウザを発表"}))

# E1: custom-news の保存先を分ける（AIニュースを上書きしない）
check("custom-news: news_{date}_{slug}.json に保存",
      _nc2.news_json_path(Path("state"), "20261101", "宇宙 開発") == Path("state/news_20261101_宇宙_開発.json")
      and _nc2.news_json_path(Path("state"), "20261101") == Path("state/news_20261101.json"))
check("custom-news: collect_news が保存先を news_json_path で決める", "news_json_path(state_dir, date_str, topic)" in _src_cn)
import review_report as _rr
import youtube_uploader as _yu2
check("custom-news: 確認用まとめ・アップロードも同じ場所を読む",
      _rr._news_path(Path("s"), "20261101", "車") == _yu2._news_json_path(Path("s"), "20261101", "車")
      == Path("s/news_20261101_車.json"))
_src_rp = inspect.getsource(_orch.run_pipeline)
check("custom-news: pipeline state も別ファイル", "pipeline_{date_str}_{topic_slug(topic)}.json" in _src_rp)
check("custom-news: スライドに collect の news JSON を渡す",
      "news_json_override=_news_override(state)" in inspect.getsource(_orch.run_slides_shorts))

# E2 / E5 / B5: 0件なら例外・収集の警告を確認用まとめに出す
with _tempfile.TemporaryDirectory() as _cd:
    _cd = Path(_cd)
    _ccfg = {"news": {"rss_feeds": [], "top_n": 5, "fetch_full_text": True}, "llm": {"provider": "claude"},
             "paths": {"state_dir": str(_cd / "state")}, "shorts": {"topics_per_day": 3}}
    (_cd / "config.json").write_text(_json.dumps(_ccfg), encoding="utf-8")
    try:
        _nc2.collect_news(_cd / "config.json", "20991231")
        check("収集: 0件なら例外（done にしない）", False)
    except RuntimeError as _e:
        check("収集: 0件なら例外（done にしない）", "No articles selected" in str(_e), str(_e))
    check("収集: 0件のときは news JSON を書かない", not (_cd / "state" / "news_20991231.json").exists())
    (_cd / "state" / "news_20991231.json").write_text(_json.dumps({"articles": [], "collect_warnings": [
        {"check": "フィードが取れない", "detail": "https://dead.example — HTTP 429"}]}, ensure_ascii=False), encoding="utf-8")
    _pf = _rr.preflight("20991231", {"paths": {"state_dir": str(_cd / "state"), "output_dir": str(_cd)},
                                     "shorts": {"topics_per_day": 3}}, topics=[])
    check("確認用まとめ: 収集の警告を WARN で出す",
          any(c["level"] == "WARN" and c["check"] == "収集: フィードが取れない" for c in _pf), repr(_pf))
check("収集: top_n・Shorts 本数に足りないときの警告", "Shorts の本数" in _src_cn and "より少ない" in _src_cn)
check("収集: 絞り込み前の件数と各段階の件数を保存", '"total_fetched"' in _src_cn and '"counts"' in _src_cn
      and '"feed_stats"' in _src_cn)
check("収集: pipeline state にフィードごとの件数を残す", 'state["feed_stats"]' in inspect.getsource(_orch.run_collect))

# QA1 指摘: 漢字だけの固有名の歯止め／解決後 URL の再照合／ページ取得の上限／gif／slug 表記
import seen_topics as _st3

_k = lambda t, tj="": {"title": t, "title_ja": tj}
check("固有名: 漢字だけの固有名（国防総省）が共通なら同じ話候補として採る",
      _st3.shares_entity(_k("", "国防総省がAI契約を発表"), _k("", "国防総省、新たなAI調達を開始")))
check("固有名: 経済産業省・東京大学も採る",
      _st3.shares_entity(_k("", "経済産業省が半導体支援を決定"), _k("", "経済産業省の半導体補助、第2弾")) and
      _st3.shares_entity(_k("", "東京大学が新モデルを公開"), _k("", "東京大学の研究チームが成果")))
check("固有名: 一般的な漢字語（人工知能・生成・発表）だけの共通は却下",
      not _st3.shares_entity(_k("", "人工知能の生成を発表"), _k("", "生成人工知能の導入を発表"))
      and not _st3.shares_entity(_k("", "人工知能開発の最新動向"), _k("", "人工知能開発を拡大")))
check("固有名: 漢字の固有名候補から一般語は除かれ固有名は残る",
      _st3._kanji_names("人工知能開発を発表") == set() and _st3._kanji_names("国防総省が人工知能を導入") == {"国防総省"})

_kc = {"https://a/1": "https://news.example/story", "https://a/2": "https://old.example/seen/",
       "https://a/3": "https://news.example/story?utm_source=x", "https://a/4": "https://ok.example/4",
       "https://a/5": "https://ok.example/5"}


def _fake_fetch2(client, article, fetch_full_text, max_text_chars):
    article["link"] = _kc[article["link"]]
    article["full_text"] = "本文" * 150
    return "<html></html>", article["link"]


_orig_fetch, _orig_save = _nc2._fetch_article_page, _nc2._save_og_image
_nc2._fetch_article_page = _fake_fetch2
_nc2._save_og_image = lambda client, article, html, url, stem: None
try:
    with _tempfile.TemporaryDirectory() as _ogd:
        _c2 = [{"title": f"t{i}", "link": f"https://a/{i}", "summary": ""} for i in range(1, 6)]
        _s2, _k2 = _nc2.select_with_material(_c2, 3, Path(_ogd), fetch_full_text=True,
                                             exclude_keys={_nc2.url_key("https://old.example/seen")})
finally:
    _nc2._fetch_article_page, _nc2._save_og_image = _orig_fetch, _orig_save
check("解決後URL: 過去分と同じURLは選ばず skip_reason を残す",
      [a["title"] for a in _k2 if "重複" in a.get("skip_reason", "")] == ["t2", "t3"], repr([(a["title"], a.get("skip_reason")) for a in _k2]))
check("解決後URL: 同じ回で選定済みのURL（utm違い）も重複扱い、残りで top_n を埋める",
      [a["title"] for a in _s2] == ["t1", "t4", "t5"], repr([a["title"] for a in _s2]))
check("解決後URL: collect_news が過去URLキーを渡す", "exclude_keys=past_url_keys(" in _src_cn)


class _FakeStream:
    def __init__(self, chunks):
        self.chunks, self.headers, self.url = chunks, {"content-type": "text/html; charset=utf-8"}, "https://big.example/p"
        self.read_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        for c in self.chunks:
            self.read_bytes += len(c)
            yield c


class _FakeClient:
    def __init__(self, chunks):
        self.stream_obj = _FakeStream(chunks)

    def stream(self, method, url, headers=None):
        return self.stream_obj


_head = ("<html><body><article><p>" + "これは記事の本文の段落です。十分な長さがあります。" * 6 + "</p>"
         "<p>" + "二つ目の段落も本文として抽出されるべき内容です。" * 4 + "</p></article>").encode("utf-8")
_chunks = [_head] + [b"x" * 65536] * 200  # 約13MB
_fc = _FakeClient(_chunks)
_art = {"title": "big", "link": "https://big.example/p"}
_html, _url = _nc2._fetch_article_page(_fc, _art, True, 3000)
check("ページ取得: 5MB で打ち切る（超えた分は読まない）",
      len(_html.encode("utf-8")) <= _nc2._MAX_PAGE_BYTES and _fc.stream_obj.read_bytes <= _nc2._MAX_PAGE_BYTES + 65536
      and _url == "https://big.example/p", repr((len(_html), _fc.stream_obj.read_bytes)))
check("ページ取得: 打ち切っても先頭の本文は抽出できる", "二つ目の段落" in _art.get("full_text", ""))
_fc2 = _FakeClient([_head])
check("ページ取得: 上限未満のページはそのまま読む", _nc2._fetch_article_page(_fc2, {"title": "s", "link": "https://s/"}, False, 3000)[0] == _head.decode("utf-8"))

_sg_src = Path("slide_generator.py").read_text(encoding="utf-8")
check("MIME: slide_generator の対応表に gif（OG 画像の保存形式と一致）",
      _sg_src.count('"gif": "gif"') == 2 and '".gif"' in inspect.getsource(_nc2) )
_cn_md = Path("skills/custom-news.md").read_text(encoding="utf-8")
check("skills/custom-news.md: カスタムトピックの state は pipeline_DATE_<slug>.json 表記",
      "pipeline_DATE_<slug>.json" in _cn_md.splitlines()[42] and "pipeline_DATE_<slug>.json" in _cn_md.splitlines()[45])

# --- Shorts デザインのランダム割り当て（2026-11-01: A/B/C を記事ごとに） ---
import slide_generator as _sgv
_vcfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
_names = _vcfg["shorts"]["template_variants"]
check("デザイン: config に A/B/C の3案", len(_names) == 3 and all((Path("templates") / n).exists() for n in _names))
_v1 = _sgv.pick_shorts_variants(_vcfg, "20261102")
check("デザイン: 3案すべてを1回ずつ使う（3本/日で重複なし）",
      sorted(p.parent.name for p in _v1) == ["broadcast", "editorial", "soft"])
check("デザイン: 同じ日付は何度作り直しても同じ順", _v1 == _sgv.pick_shorts_variants(_vcfg, "20261102"))
_orders = {tuple(p.parent.name for p in _sgv.pick_shorts_variants(_vcfg, f"202611{d:02d}")) for d in range(1, 29)}
check("デザイン: 日によって順番が変わる（ランダム）", len(_orders) >= 4, repr(len(_orders)))
check("デザイン: 未設定なら空（従来の slide_shorts.html）",
      _sgv.pick_shorts_variants({**_vcfg, "shorts": {}}, "20261102") == [])
try:
    _sgv.pick_shorts_variants({**_vcfg, "shorts": {"template_variants": ["variants/nope/slide_shorts.html"]}}, "20261102")
    check("デザイン: 存在しないテンプレートは即エラー", False)
except FileNotFoundError:
    check("デザイン: 存在しないテンプレートは即エラー", True)
for _n in _names:
    _t = (Path("templates") / _n).read_text(encoding="utf-8")
    check(f"デザイン: {_n} に7曜日の色", all(f".day-{d}" in _t for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")))

# --- 回帰: ポイント欠けの記事があっても記事番号（og_image/hook_title/points/デザイン）がずれない ---
from video_builder import select_topic_units as _stu2
def _mk(n, title):
    return {"number": n, "title": title, "serif": "x", "body": "", "is_opening": False,
            "is_ending": False, "is_index": False, "is_keyword": False}
_sl = [_mk(1, "記事A"), _mk(2, "記事Aのポイント"), _mk(3, "記事B"),   # B はポイント欠け
       _mk(4, "記事C"), _mk(5, "記事Cのポイント"), _mk(6, "記事D"), _mk(7, "記事Dのポイント")]
_units = _stu2(_sl, 3)
_idx, _cur = {}, 0
for _s in _sl:
    _cur = _sgv.next_article_index(_cur, _s["title"])
    _idx[_s["number"]] = _cur
check("記事番号: ポイント欠けでも後続がずれない", _idx == {1: 1, 2: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4}, repr(_idx))
check("記事番号: select_topic_units のユニット順と一致",
      all(_idx[s["number"]] == i for i, u in enumerate(_units, 1) for s in u), repr(_idx))
_vv = _sgv.pick_shorts_variants(_vcfg, "20261102")
_assign = [_vv[(_idx[u[0]["number"]] - 1) % len(_vv)].parent.name for u in _units]
check("デザイン: 3ユニットで A/B/C を1回ずつ（ポイント欠け含む）",
      sorted(_assign) == ["broadcast", "editorial", "soft"], repr(_assign))
check("記事番号: 先頭が孤立ポイントでも1件目として数える（ユニット化と同じ）",
      _sgv.next_article_index(0, "記事Zのポイント") == 1 and _sgv.next_article_index(1, "記事Zのポイント") == 1)

print()
if failures:
    print(f"=== {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("=== ALL UNIT CHECKS PASSED ===")
