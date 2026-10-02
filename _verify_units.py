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
check("通常説明文: VOICEVOXクレジット", "VOICEVOX" in meta["description"])
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
check("config: VOICEVOXクレジット設定", "VOICEVOX" in config.get("tts", {}).get("credit", ""))
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

print()
if failures:
    print(f"=== {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("=== ALL UNIT CHECKS PASSED ===")
