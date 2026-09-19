"""UIプレビュー描画ハーネス.

本番テンプレート(templates/slide.html 等)を汚さずに、候補テンプレを
実データ(state/old の 6/30 台本・news)で描画して目視確認するための足場。

例:
  python _render_preview.py --template templates/slide_classroom.html --out preview
  python _render_preview.py --template templates/slide_shorts_classroom.html --shorts --out preview_shorts
  python _render_preview.py --thumbnail --template templates/thumbnail_classroom.html --out preview
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import slide_generator as sg

DEF_DATE = "20260630"
DEF_SCRIPT = "state/old/script_20260630.md"
DEF_NEWS = "state/old/news_20260630.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=DEF_DATE)
    ap.add_argument("--script", default=DEF_SCRIPT)
    ap.add_argument("--news", default=DEF_NEWS)
    ap.add_argument("--template", required=True)
    ap.add_argument("--shorts", action="store_true")
    ap.add_argument("--thumbnail", action="store_true")
    ap.add_argument("--out", required=True, help="state 配下の出力サブディレクトリ名")
    args = ap.parse_args()

    if args.thumbnail:
        out_dir = Path("state") / args.out
        out_dir.mkdir(parents=True, exist_ok=True)
        path = asyncio.run(sg.generate_thumbnail_async(
            args.date,
            template_override=Path(args.template),
            news_json_override=Path(args.news),
            out_override=out_dir / f"thumbnail_{args.date}.jpg",
            force=True,
        ))
        print("thumbnail:", path)
        return

    asyncio.run(sg.generate_slides_async(
        Path(args.script),
        date_str=args.date,
        shorts=args.shorts,
        template_override=Path(args.template),
        out_subdir=args.out,
        news_json_override=Path(args.news),
    ))
    print("slides done ->", "state/" + args.out)


if __name__ == "__main__":
    main()
