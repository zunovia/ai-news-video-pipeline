"""プレビュー一覧画像（コンタクトシート）を作る."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PRE = Path("state/preview")
PRES = Path("state/preview_shorts")

def font(sz):
    for p in ("C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/msgothic.ttc"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()

def grid(items, cols, cw, ch, title, pad=14, lh=26, bg=(244,244,246)):
    rows = (len(items) + cols - 1) // cols
    th = 44
    W = cols * cw + (cols + 1) * pad
    H = th + rows * (ch + lh) + (rows + 1) * pad
    canvas = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    d.text((pad, 10), title, fill=(20, 20, 20), font=font(26))
    for i, (img, label) in enumerate(items):
        r, c = divmod(i, cols)
        x = pad + c * (cw + pad)
        y = th + pad + r * (ch + lh + pad)
        d.rectangle([x, y, x + cw, y + ch], fill=(255, 255, 255), outline=(210, 210, 210))
        im = Image.open(img).convert("RGB")
        im.thumbnail((cw, ch))
        canvas.paste(im, (x + (cw - im.width) // 2, y + (ch - im.height) // 2))
        d.text((x + 4, y + ch + 4), label, fill=(40, 40, 40), font=font(17))
    return canvas

L16 = {
    1: "1 オープニング", 2: "2 ニュース一覧", 3: "3 ①紹介", 4: "4 ①ポイント",
    5: "5 ②紹介", 6: "6 ②ポイント", 7: "7 ③紹介", 8: "8 ③ポイント",
    9: "9 ④紹介", 10: "10 ④ポイント", 11: "11 ⑤紹介", 12: "12 ⑤ポイント",
    13: "13 キーワード", 14: "14 エンディング",
}

items16 = [(PRE / f"slide_{n}.png", L16[n]) for n in range(1, 15) if (PRE / f"slide_{n}.png").exists()]
items16.append((PRE / "thumbnail_20260630.jpg", "★ サムネイル"))
grid(items16, cols=3, cw=520, ch=293, title="教室・ノート風 ｜ 通常版16:9 一覧（6/30データ）").save(PRE / "_overview.png")

shorts_nums = sorted(int(p.stem.split("_")[1]) for p in PRES.glob("slide_*.png"))
itemsS = [(PRES / f"slide_{n}.png", f"{n} {L16.get(n,'').split(' ',1)[-1]}") for n in shorts_nums]
grid(itemsS, cols=6, cw=190, ch=338, title="教室・ノート風 ｜ Shorts 9:16 一覧（6/30データ）").save(PRES / "_overview.png")

print("overview ->", PRE / "_overview.png")
print("overview ->", PRES / "_overview.png")
