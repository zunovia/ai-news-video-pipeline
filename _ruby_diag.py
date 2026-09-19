"""新ルビエンジンの精度・保護・エッジケース検証."""
import sys

from ruby_engine import annotate_ruby, strip_kanji_ruby
from slide_generator import _extract_ruby_dict, _apply_ruby_dict
from video_builder import serif_to_tts_text

failures = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual!r}" + ("" if ok else f" (expected {expected!r})"))
    if not ok:
        failures.append(name)


def _title_index_ruby(title: str, serif: str) -> str:
    """インデックス/タイトル表示と同じ経路でルビを生成する."""
    rd = _extract_ruby_dict(serif)
    return annotate_ruby(_apply_ruby_dict(title, rd))


# --- 旧実装で誤読していたケース（送り仮名・文脈） ---
check("作る", annotate_ruby("作る"), "作(つく)る")
check("新しい", annotate_ruby("新しい"), "新(あたら)しい")
check("使える", annotate_ruby("使える"), "使(つか)える")
check("賢くなる", annotate_ruby("賢くなる"), "賢(かしこ)くなる")
check("楽しく", annotate_ruby("楽しく"), "楽(たの)しく")
check("見逃せない", annotate_ruby("見逃せない"), "見逃(みのが)せない")
check("覚えていて", annotate_ruby("覚えていて"), "覚(おぼ)えていて")
check("大変身", annotate_ruby("大変身"), "大変身(だいへんしん)")

# --- 熟語・文脈読み ---
check("人工知能", annotate_ruby("人工知能"), "人工知能(じんこうちのう)")
check("発表", annotate_ruby("発表"), "発表(はっぴょう)")
check("今日(基本漢字スキップ)", annotate_ruby("今日"), "今日")
check("大きい(基本漢字スキップ)", annotate_ruby("大きい"), "大きい")

# --- 既存ルビ・英語カタカナの保護 ---
check("既存ルビ保護", annotate_ruby("発表(はっぴょう)した"), "発表(はっぴょう)した")
check("手修正ルビ保護", annotate_ruby("挑む(いどむ)"), "挑む(いどむ)")
check("英語カタカナ保護", annotate_ruby("Siri(シリ)が進化"), "Siri(シリ)が進化(しんか)")

# --- strip_kanji_ruby ---
check("strip: 漢字ルビ除去", strip_kanji_ruby("発表(はっぴょう)した"), "発表した")
check("strip: 誤ルビ除去", strip_kanji_ruby("賢(さとし)くなる"), "賢くなる")
check("strip: 英語読みは保持", strip_kanji_ruby("Siri(シリ)を発表(はっぴょう)"), "Siri(シリ)を発表")
check("strip: 二重ルビは固定点まで全除去", strip_kanji_ruby("楽(らく)しく(たのしく)"), "楽しく")

# --- 二重ルビをstrip→再付与すると正しくなるか ---
check("二重ルビ修復", annotate_ruby(strip_kanji_ruby("楽(らく)しく(たのしく)")), "楽(たの)しく")

# --- 文全体（実セリフ相当） ---
serif = "みんなのスマホのSiri(シリ)が、もっと賢くなるって知ってた？"
out = annotate_ruby(serif)
print(f"  実セリフ: {out}")
check("実セリフ: 賢く", "賢(かしこ)く" in out, True)
check("実セリフ: 知ってた", "知(し)ってた" in out, True)
check("実セリフ: Siri保護", "Siri(シリ)" in out, True)

# --- テンプレート固定文 ---
guide = "概要欄にニュースの元の記事リンクを載せているから、気になった人はチェックしてみてね！"
out2 = annotate_ruby(guide)
print(f"  ガイド文: {out2}")
check("ガイド: 概要欄", "概要欄(がいようらん)" in out2, True)
check("ガイド: 載せ", "載(の)せ" in out2, True)
check("ガイド: 記事", "記事(きじ)" in out2, True)

# --- 数字・記号混じり ---
check("数字+人(基本漢字スキップ)", annotate_ruby("4,000人"), "4,000人")
out3 = annotate_ruby("自動運転の車が危ない場面を練習")
print(f"  混合文: {out3}")
check("混合: 自動運転", "自動運転(じどううんてん)" in out3, True)
check("混合: 危ない", "危(あぶ)ない" in out3, True)

# --- 漢字+かな+漢字の混在語（分割ルビ。一括ルビは表示崩れするため） ---
check("使い方", annotate_ruby("使い方"), "使(つか)い方(かた)")
check("取り組み", annotate_ruby("取り組み"), "取(と)り組(く)み")
check("食べ物", annotate_ruby("食べ物"), "食(た)べ物(もの)")
check("思い出", annotate_ruby("思い出"), "思(おも)い出(で)")
check("乗り物", annotate_ruby("乗り物"), "乗(の)り物(もの)")
check("言い間違え(言=小2でルビなし)", annotate_ruby("言い間違え"), "言い間違(まちが)え")

# --- 品詞境界（名詞+動詞/形容詞は結合しない） ---
check("画面見ただけ", annotate_ruby("画面見ただけ"), "画面(がめん)見ただけ")
out4 = annotate_ruby("一番賢いAI")
check("一番賢い", out4, "一番(いちばん)賢(かしこ)いAI")
check("複合名詞は結合維持: 人工知能", annotate_ruby("人工知能"), "人工知能(じんこうちのう)")
check("接頭辞結合維持: 大変身", annotate_ruby("大変身"), "大変身(だいへんしん)")

# --- セリフ→タイトル転写の境界バグ（インデックス表示ルビの破損）回帰テスト ---
# バグ1: 漢字熟語の内側に部分マッチ → 診断能力(のうりょく) のような壊れたルビ
check(
    "熟語内側マッチ防止(診断能力)",
    _title_index_ruby("医師と同等の診断能力を持つ", "すごい能力(のうりょく)を持つ"),
    "医師(いし)と同等(どうとう)の診断能力(しんだんのうりょく)を持(も)つ",
)
# バグ2: 英単語の内側に部分マッチ → OpenAI(エーアイ)
check(
    "英単語内側マッチ防止(OpenAI)",
    _title_index_ruby("OpenAIと連携しAIで刷新", "AI(エーアイ)の力で"),
    "OpenAIと連携(れんけい)しAI(エーアイ)で刷新(さっしん)",
)
# バグ3: \w が日本語にマッチ → 保護領域が日本語を飲み込み「連携」のルビが消える
check(
    "保護領域の日本語飲み込み防止",
    annotate_ruby("OpenAIと連携しAI(エーアイ)で刷新する"),
    "OpenAIと連携(れんけい)しAI(エーアイ)で刷新(さっしん)する",
)

# --- TTS: 英語2語＋間に日本語＋後者だけルビ で単語が脱落しないこと ---
check(
    "TTS単語脱落防止",
    serif_to_tts_text("OpenAIと連携しAI(エーアイ)でレガシーを刷新する"),
    "OpenAIと連携しエーアイでレガシーを刷新する",
)
check(
    "TTS英語ルビ→カタカナ置換",
    serif_to_tts_text("Apple(アップル)とGoogle(グーグル)が発表"),
    "アップルとグーグルが発表",
)

# --- 空・かなのみ ---
check("空文字", annotate_ruby(""), "")
check("かなのみ", annotate_ruby("みんなだよ！"), "みんなだよ！")

print()
if failures:
    print(f"=== {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("=== ALL RUBY CHECKS PASSED ===")
