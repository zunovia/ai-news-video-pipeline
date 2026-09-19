"""台本の推定尺チェック: 新旧台本の合計推定秒数を比較する."""
from pathlib import Path

from slide_generator import parse_slides
from video_builder import _estimate_slide_seconds, select_shorts_slides, serif_to_tts_text

for path in [Path("state/old/script_20260610.md"), Path("state/script_20260611.md")]:
    slides = parse_slides(path)
    total = sum(_estimate_slide_seconds(s.get("serif", "")) for s in slides)
    chars = sum(len(serif_to_tts_text(s.get("serif", ""))) for s in slides)
    # クロスフェードの重なり分を引く（0.5s × (N-1)）
    xfade = 0.5 * (len(slides) - 1)
    print(f"{path.name}: {len(slides)}枚 / TTS文字数={chars} / 推定 {total - xfade:.0f}秒 ({(total - xfade) / 60:.1f}分)")
    for s in slides:
        sec = _estimate_slide_seconds(s.get("serif", ""))
        n = len(serif_to_tts_text(s.get("serif", "")))
        flag = " ★長い" if n > 160 and not s.get("is_index") and not s.get("is_keyword") else ""
        print(f"  slide{s['number']:>2} {s['title'][:18]:<20} {n:>3}字 {sec:>5.1f}s{flag}")

    sel = select_shorts_slides(slides, 170)
    sel_total = sum(_estimate_slide_seconds(s.get("serif", "")) for s in sel) - 0.5 * (len(sel) - 1)
    articles = sum(1 for s in sel if not s.get("is_opening") and not s.get("is_ending")
                   and not s.get("is_index") and not s.get("is_keyword")
                   and not s["title"].endswith("ポイント"))
    print(f"  Shorts選定: {len(sel)}枚 / 記事{articles}本 / 推定 {sel_total:.0f}秒")
    print()
