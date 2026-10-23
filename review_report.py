"""アップロード前の確認: 確認用まとめ（state/review_DATE.md）とプリフライトチェック.

1ニュース=1本 Shorts の確認材料（題名・要点・セリフ・元記事の事実・事実確認の結果・
動画の長さ・字幕）を1ファイルにまとめ、アップロードを止めるべき問題（FAIL）と
人の確認が要る問題（WARN）を機械的に洗い出す。

使い方:
    python review_report.py 20261023            # まとめを作り、チェック結果を表示
    python review_report.py 20261023 --check    # チェックだけ（FAILがあれば終了コード1）

video_shorts フェーズの最後に自動で作られ、upload_shorts の直前にも
プリフライトが走る（FAILがあればアップロードしない）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# 動画タイトルに残ってはいけない読みがな（script_generator.strip_reading_parens と同じ規則）
_READING_PARENS_RE = re.compile(r"[(（][ぁ-んァ-ヶー・ ]+[)）]")
# 見出しが助詞・読点で終わる＝途中で切れている疑い（例: 「…で」「…の」）
_DANGLING_END_RE = re.compile(r"[のでにをがとはへや、・]$")
_HOOK_TITLE_MIN = 15
_HOOK_TITLE_MAX = 26
_YT_TITLE_MAX = 100
_SHORTS_HARD_MAX_SEC = 180
_POINTS_COUNT = 3


# 数字の照合: 10以上の整数・小数だけを見る（「1体」「3個」のような小さな数は誤検知が多い）
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    t = unicodedata.normalize("NFKC", text or "").replace(",", "")
    return {m for m in _NUM_RE.findall(t) if "." in m or int(m) >= 10}


def unsupported_numbers(article: dict, texts: list[str]) -> list[str]:
    """題名・要点・セリフの数字のうち、元記事（タイトル・要約・事実）に無いものを返す.

    「500万行のバグ」のような数字の対象のすり替えは拾えないが、
    元記事に無い数字（LLMの作り話・桁違い）は機械的に確実に拾える。
    """
    source = " ".join([
        str(article.get("title_ja", "")), str(article.get("title", "")),
        str(article.get("summary_ja_long", "")), str(article.get("summary_ja", "")),
        " ".join(str(f) for f in article.get("facts") or []),
    ])
    known = _numbers(source)
    found: list[str] = []
    for t in texts:
        for num in sorted(_numbers(t)):
            if num not in known and num not in found:
                found.append(num)
    return found


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _video_duration(path: Path) -> float | None:
    try:
        from video_builder import _ffprobe_duration
        return _ffprobe_duration(path)
    except Exception as e:
        logger.warning("Could not read duration of %s: %s", path, e)
        return None


def collect_topics(date_str: str, config: dict, topic: str = "") -> list[dict]:
    """トピック（動画1本）ごとの確認材料を集める."""
    state_dir = Path(config["paths"]["state_dir"])
    output_dir = Path(config["paths"]["output_dir"])
    top_n = config.get("shorts", {}).get("topics_per_day", 3)

    news = _load_json(state_dir / f"news_{date_str}.json").get("articles", [])
    meta = _load_json(state_dir / f"script_meta_{date_str}.json")
    meta_arts = meta.get("articles", [])
    fc_arts = {a.get("index"): a for a in (meta.get("factcheck") or {}).get("articles", [])}

    # 台本のセリフ（紹介・ポイント）。動画化と同じ選び方で取り出す
    units: list[list[dict]] = []
    script_path = state_dir / f"script_{date_str}.md"
    if script_path.exists():
        from slide_generator import parse_slides
        from video_builder import select_topic_units
        units = select_topic_units(parse_slides(script_path), top_n)

    topics = []
    # ニュースが top_n 件に満たない日は動画もその件数しか作られない（無い動画をFAILにしない）
    count = min(top_n, len(news)) if news else top_n
    for i in range(1, count + 1):
        article = news[i - 1] if i - 1 < len(news) else {}
        ma = meta_arts[i - 1] if i - 1 < len(meta_arts) else {}
        hook = ma.get("hook_title", "") or ""
        yt_title = ""
        if article:
            from youtube_uploader import generate_topic_shorts_metadata
            yt_title = generate_topic_shorts_metadata(date_str, config, article, hook, i,
                                                      topic=topic)["title"]
        video = output_dir / f"{date_str}_shorts_topic{i}.mp4"
        srt = state_dir / "captions" / f"{date_str}_topic{i}.srt"
        topics.append({
            "index": i,
            "article": article,
            "hook_title": hook,
            "yt_title": yt_title,
            "points": ma.get("points") or [],
            "serifs": [s.get("serif", "") for s in units[i - 1]] if i - 1 < len(units) else [],
            "factcheck": fc_arts.get(i),
            "video": video,
            "duration": _video_duration(video) if video.exists() else None,
            "caption": srt if srt.exists() else None,
        })
    return topics


def preflight(date_str: str, config: dict, topics: list[dict] | None = None,
              topic: str = "") -> list[dict]:
    """アップロード前チェック。各項目 {level: FAIL|WARN, topic, check, detail} のリストを返す."""
    state_dir = Path(config["paths"]["state_dir"])
    max_sec = config.get("shorts", {}).get("max_topic_duration_sec", 75)
    topics = topics if topics is not None else collect_topics(date_str, config, topic)
    meta = _load_json(state_dir / f"script_meta_{date_str}.json")
    out: list[dict] = []

    def add(level: str, topic: int, check: str, detail: str = "") -> None:
        out.append({"level": level, "topic": topic, "check": check, "detail": detail})

    if not meta:
        add("FAIL", 0, "script_meta がない", str(state_dir / f"script_meta_{date_str}.json"))
    elif meta.get("source") != "llm":
        add("WARN", 0, "台本がフォールバック（定型文）", meta.get("error", ""))
    fc = meta.get("factcheck") or {}
    if meta and not fc:
        add("WARN", 0, "事実確認の記録がない", "旧形式の台本。目視で元記事と照合する")
    elif fc.get("status") == "error":
        add("WARN", 0, "事実確認が失敗", fc.get("error", ""))

    for t in topics:
        i, hook = t["index"], t["hook_title"]
        if not t["video"].exists():
            add("FAIL", i, "動画がない", str(t["video"]))
        elif t["duration"] is not None:
            if t["duration"] > _SHORTS_HARD_MAX_SEC:
                add("FAIL", i, "動画が3分超（Shortsにならない）", f"{t['duration']:.1f}秒")
            elif t["duration"] > max_sec:
                add("WARN", i, "動画が想定より長い", f"{t['duration']:.1f}秒 > {max_sec}秒")
        if not hook:
            add("FAIL", i, "題名（hook_title）が空")
        else:
            if _READING_PARENS_RE.search(hook):
                add("FAIL", i, "題名に読みがなのカッコ書き", hook)
            n = len(hook)
            if n > _HOOK_TITLE_MAX or n < _HOOK_TITLE_MIN:
                add("WARN", i, f"題名の長さが{_HOOK_TITLE_MIN}〜{_HOOK_TITLE_MAX}字の範囲外", f"{n}字: {hook}")
            if _DANGLING_END_RE.search(hook):
                add("WARN", i, "題名が途中で切れている疑い", hook)
        if len(t["yt_title"]) > _YT_TITLE_MAX:
            add("FAIL", i, "YouTube題名が100字超", t["yt_title"])
        if len(t["points"]) != _POINTS_COUNT:
            add("WARN", i, "画面の要点が3個でない", f"{len(t['points'])}個")
        if t["caption"] is None:
            add("WARN", i, "字幕ファイルがない")
        if t["article"]:
            nums = unsupported_numbers(t["article"], [hook, *t["points"], *t["serifs"]])
            if nums:
                add("WARN", i, "元記事にない数字", "、".join(nums))
        fca = t["factcheck"]
        if fca and fca.get("unresolved"):
            for u in fca["unresolved"]:
                add("WARN", i, f"事実確認の未解決（{u.get('field', '')}）", u.get("problem", ""))
    return out


def _fmt_fix_value(v) -> str:
    return " / ".join(v) if isinstance(v, list) else str(v)


def render_review(date_str: str, topics: list[dict], checks: list[dict], meta: dict) -> str:
    fails = [c for c in checks if c["level"] == "FAIL"]
    warns = [c for c in checks if c["level"] == "WARN"]
    fc = meta.get("factcheck") or {}
    lines = [
        f"# アップロード前の確認 {date_str}",
        "",
        f"- 台本: **{meta.get('source', '?')}** ／ 事実確認: **{fc.get('status', '記録なし')}**",
        f"- プリフライト: **{'FAIL ' + str(len(fails)) + '件' if fails else 'FAILなし'}**"
        f" ／ 要確認(WARN) {len(warns)}件",
        "",
    ]
    if checks:
        lines += ["## チェック結果", ""]
        for c in fails + warns:
            where = f"topic{c['topic']}" if c["topic"] else "全体"
            lines.append(f"- **{c['level']}** [{where}] {c['check']}"
                         + (f" — {c['detail']}" if c["detail"] else ""))
        lines.append("")

    for t in topics:
        a = t["article"]
        dur = f"{t['duration']:.1f}秒" if t["duration"] is not None else "動画なし"
        lines += [
            f"## topic{t['index']}: {t['hook_title']}",
            "",
            f"- YouTube題名: `{t['yt_title']}`（{len(t['yt_title'])}字）",
            f"- 動画: {dur} ／ 字幕: {'あり' if t['caption'] else 'なし'}",
            f"- 元記事: {a.get('title_ja') or a.get('title', '')}",
            f"  - {a.get('link', '')}",
            "",
            "**画面の要点**",
            "",
        ]
        lines += [f"{k}. {p}" for k, p in enumerate(t["points"], 1)] or ["（なし）"]
        lines += ["", "**セリフ**（紹介 → ポイント）", ""]
        lines += [f"> {s}" for s in t["serifs"]] or ["（台本なし）"]
        lines += ["", "**元記事の要約・事実**", "",
                  a.get("summary_ja_long") or a.get("summary_ja", "")]
        lines += [f"- {f}" for f in (a.get("facts") or [])]
        fca = t["factcheck"]
        lines += ["", "**事実確認**", ""]
        if not fca:
            lines.append("（記録なし — 目視で照合）")
        else:
            for f in fca.get("fixed", []):
                lines.append(f"- 自動修正（{f['field']}）: {f.get('problem', '')}")
                lines.append(f"  - 前: {_fmt_fix_value(f.get('before', ''))}")
                lines.append(f"  - 後: {_fmt_fix_value(f.get('after', ''))}")
            for u in fca.get("unresolved", []):
                lines.append(f"- **要確認**（{u['field']}）: {u.get('problem', '')}")
                if u.get("suggestion"):
                    lines.append(f"  - 修正案: {_fmt_fix_value(u['suggestion'])}")
            if not fca.get("fixed") and not fca.get("unresolved"):
                lines.append("- 問題なし")
        lines.append("")
    return "\n".join(lines)


def build_review(date_str: str, config_path: Path = Path("config.json"),
                 topic: str = "") -> tuple[Path, list[dict]]:
    """state/review_DATE.md を書き出し、(パス, チェック結果) を返す."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    state_dir = Path(config["paths"]["state_dir"])
    topics = collect_topics(date_str, config, topic)
    checks = preflight(date_str, config, topics)
    meta = _load_json(state_dir / f"script_meta_{date_str}.json")
    path = state_dir / f"review_{date_str}.md"
    path.write_text(render_review(date_str, topics, checks, meta), encoding="utf-8")
    return path, checks


def print_checks(checks: list[dict]) -> None:
    if not checks:
        print("プリフライト: 問題なし")
    for c in checks:
        where = f"topic{c['topic']}" if c["topic"] else "全体"
        print(f"[{c['level']}] {where}: {c['check']}" + (f" — {c['detail']}" if c["detail"] else ""))


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="アップロード前の確認まとめ・プリフライト")
    parser.add_argument("date", help="YYYYMMDD")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--check", action="store_true", help="チェックだけ行う（まとめは書かない）")
    parser.add_argument("--topic", default="", help="custom-news のトピック（題名プレビュー用）")
    args = parser.parse_args()
    if args.check:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        checks = preflight(args.date, config, topic=args.topic)
    else:
        path, checks = build_review(args.date, Path(args.config), args.topic)
        print(f"確認用まとめ: {path}")
    print_checks(checks)
    sys.exit(1 if any(c["level"] == "FAIL" for c in checks) else 0)


if __name__ == "__main__":
    main()
