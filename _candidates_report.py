"""UI候補（templates/candidates/<案名>/slide_shorts.html）の比較画像を一括生成する.

出力（すべて state/preview_candidates/ 配下。本番の state ファイルには触れない）:
  <案名>/slide_N.png        実データで描画した各スライド
  <案名>/_overview.png      全スライド一覧
  <案名>/_days.png          同じスライドを7曜日の色で並べた画像（タイトル＋ポイント）
  <案名>/_caption.png       焼き込み字幕を重ねた合成画像（字幕帯 x=236,y=1160〜1480）
  _compare.png              4案を横に並べた比較（同じ記事のタイトル＋ポイント）

例:  python _candidates_report.py            # 全案
     python _candidates_report.py soft       # 1案だけ
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import video_builder as vb
import slide_generator as sg
from _make_overview import dir_overview, font

NAMES = ["broadcast", "editorial", "soft", "datatech"]
LABELS = {"broadcast": "A broadcast 報道番組風", "editorial": "B editorial 雑誌風",
          "soft": "C soft やわらかカード", "datatech": "D datatech データ・テック"}
DATE = "20261101"
DATA = ["--script", "state/old/script_20261101.md", "--news", "state/old/news_20261101.json",
        "--meta", "state/old/script_meta_20261101.json", "--date", DATE]
OUT = Path("state/preview_candidates")
INTRO, POINT = 3, 4  # 記事1（M3E Canvas）の紹介/ポイントスライド番号
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_JA = dict(zip(DAYS, "月火水木金土日"))
# 字幕サンプル（実台本のセリフ。ルビ記法つき）。1行目: 通常 / 2行目: 長め
CAPS = {
    INTRO: "画面設計(がめんせっけい)を文章(ぶんしょう)で説明(せつめい)する苦労(くろう)から解放(かいほう)されるってわけ！",
    POINT: "M3E Canvas(エムスリーイーキャンバス)というツールは、ボタンやリストなどの部品(ぶひん)をドラッグ＆ドロップで並(なら)べると、",
}


def render(name: str, out: str, day: str | None = None) -> None:
    cmd = [sys.executable, "_render_preview.py", "--template", str(next(p for p in (Path(f"templates/variants/{name}/slide_shorts.html"), Path(f"templates/candidates/{name}/slide_shorts.html")) if p.exists())),
           "--shorts", "--out", out, *DATA]
    if day:
        cmd += ["--day", day]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])


def label(img: Image.Image, text: str, h: int = 40) -> Image.Image:
    c = Image.new("RGB", (img.width, img.height + h), (250, 250, 250))
    c.paste(img, (0, h))
    ImageDraw.Draw(c).text((10, 6), text, fill=(20, 20, 20), font=font(26))
    return c


def hstack(imgs: list[Image.Image], gap: int = 16, bg=(235, 235, 238)) -> Image.Image:
    w = sum(i.width for i in imgs) + gap * (len(imgs) + 1)
    h = max(i.height for i in imgs) + gap * 2
    c = Image.new("RGB", (w, h), bg)
    x = gap
    for i in imgs:
        c.paste(i, (x, gap))
        x += i.width + gap
    return c


def scaled(path: Path, width: int) -> Image.Image:
    im = Image.open(path).convert("RGB")
    return im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def days_image(name: str) -> None:
    """7曜日 × (タイトル,ポイント) を 2行×7列で並べる。"""
    rows = {INTRO: [], POINT: []}
    for d in DAYS:
        tmp = f"preview_candidates/_days_tmp/{name}_{d}"
        render(name, tmp, d)
        for n in rows:
            rows[n].append(label(scaled(Path("state") / tmp / f"slide_{n}.png", 280), f"{DAY_JA[d]}曜 ({d})", 36))
    top = hstack(rows[INTRO], 12)
    bot = hstack(rows[POINT], 12)
    W = max(top.width, bot.width)
    c = Image.new("RGB", (W, top.height + bot.height + 56), (235, 235, 238))
    ImageDraw.Draw(c).text((12, 10), f"{LABELS[name]} ｜ 7曜日の色（上: タイトル / 下: ポイント）", fill=(20, 20, 20), font=font(28))
    c.paste(top, (0, 50))
    c.paste(bot, (0, 50 + top.height))
    c.save(OUT / name / "_days.png")


def caption_image(name: str, day: str = "mon") -> None:
    """焼き込み字幕（video_builder と同じ描画）を字幕帯に重ねた合成画像。"""
    accent = vb._DAY_ACCENTS[sg.get_day_class(DATE)]
    box = vb._CAPTION_BOX
    x, y = box["x"], box["bottom"] - box["height"]
    pngs = asyncio.run(vb._render_caption_pngs(list(CAPS.values()), OUT / "_cap_tmp", accent))
    tiles = []
    for (n, _), cp in zip(CAPS.items(), pngs):
        base = Image.open(OUT / name / f"slide_{n}.png").convert("RGBA")
        base.alpha_composite(Image.open(cp).convert("RGBA"), (x, y))
        # 字幕帯の上端(y=1160)に点線ガイド（本文がこの線を越えていないことの確認用）
        d = ImageDraw.Draw(base)
        for xx in range(0, base.width, 24):
            d.line([(xx, 1160), (xx + 10, 1160)], fill=(255, 0, 80, 255), width=3)
        tiles.append(base.convert("RGB"))
    out = hstack([scaled_im(t, 540) for t in tiles], 16)
    out.save(OUT / name / "_caption.png")


def scaled_im(im: Image.Image, width: int) -> Image.Image:
    return im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def compare(names: list[str]) -> None:
    cols = []
    for n in names:
        col = [label(scaled(OUT / n / f"slide_{k}.png", 400), LABELS[n] if k == INTRO else "　") for k in (INTRO, POINT)]
        cols.append(col)
    row1 = hstack([c[0] for c in cols], 16)
    row2 = hstack([c[1] for c in cols], 16)
    img = Image.new("RGB", (row1.width, row1.height + row2.height), (235, 235, 238))
    img.paste(row1, (0, 0))
    img.paste(row2, (0, row1.height))
    img.save(OUT / "_compare.png")


def main() -> None:
    names = sys.argv[1:] or NAMES
    for n in names:
        render(n, f"preview_candidates/{n}")
        dir_overview(OUT / n, f"{LABELS[n]} ｜ Shorts 9:16（{DATE}データ）", 6)
        days_image(n)
        caption_image(n)
        print("done", n)
    if set(NAMES) <= {p.name for p in OUT.iterdir() if p.is_dir()}:
        compare(NAMES)
        print("compare ->", OUT / "_compare.png")


if __name__ == "__main__":
    main()
