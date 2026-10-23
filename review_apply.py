"""確認で見つかった修正を1回で反映する: バックアップ → 書き換え → 作り直し → 確認まとめ.

修正内容はJSONファイルで渡す（全項目を先に検証し、1つでも不正なら何も書き換えない）:

    {
      "titles": {"1": "数千のAIが脳データを32分で解析"},
      "points": {"2": ["要点1", "要点2", "要点3"]},
      "serif":  [{"old": "台本(script_DATE.md)にある文字列そのまま", "new": "置き換え後の文"}]
    }

- titles / points: script_meta_DATE.json の articles[n-1] を書き換える（読みがなは自動で外す）
- serif: script_DATE.md の文字列を置換する。old は台本に**ちょうど1回**現れること
  （ルビ込みでそのまま写す）。new の漢字ルビは自動付与されるので書かなくてよい
- 書き換え前のファイルは state/old/ に時刻付きで保存（前のバックアップを上書きしない）

使い方:
    python review_apply.py 20261023 fixes.json            # 反映して --from slides で作り直す
    python review_apply.py 20261023 fixes.json --no-rebuild
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class FixError(ValueError):
    """修正内容が不正（何も書き換えていない）."""


def plan_fixes(fixes: dict, script_md: str, meta: dict) -> tuple[str, dict, list[str]]:
    """修正を検証して適用後の (台本, script_meta, 変更内容の説明) を返す。不正なら FixError."""
    from script_generator import (_HOOK_TITLE_MAX, _HOOK_TITLE_MIN_RETRY, _POINT_MAX,
                                  _POINTS_COUNT, _clean_point, add_ruby_to_text,
                                  strip_reading_parens)

    if not isinstance(fixes, dict):
        raise FixError("修正内容はJSONオブジェクト（{\"titles\": ..., \"points\": ..., \"serif\": [...]}）にする")
    for key, typ in (("titles", dict), ("points", dict), ("serif", list)):
        if key in fixes and not isinstance(fixes[key], typ):
            raise FixError(f"{key} は{'オブジェクト' if typ is dict else '配列'}にする")
    if any(not isinstance(x, dict) for x in fixes.get("serif") or []):
        raise FixError("serif の各要素は {\"old\": ..., \"new\": ...} にする")
    unknown = set(fixes) - {"titles", "points", "serif"}
    if unknown:
        raise FixError(f"未知のキー: {sorted(unknown)}（titles / points / serif のみ）")
    meta = json.loads(json.dumps(meta))  # 検証が全部通るまで元データに触れない
    arts = meta.get("articles", [])
    errors: list[str] = []
    changes: list[str] = []
    resolved: list[tuple[int, str]] = []  # 人が直した (記事番号, 項目)

    def article(key) -> dict | None:
        try:
            n = int(key)
        except (TypeError, ValueError):
            errors.append(f"記事番号が数字でない: {key!r}")
            return None
        if not 1 <= n <= len(arts):
            errors.append(f"記事番号 {n} が範囲外（1〜{len(arts)}）")
            return None
        return arts[n - 1]

    for key, title in (fixes.get("titles") or {}).items():
        a = article(key)
        t = strip_reading_parens(title)
        if a is None:
            continue
        if not t or len(t) > _HOOK_TITLE_MAX:
            errors.append(f"topic{key} の題名は1〜{_HOOK_TITLE_MAX}字にする（{len(t)}字）: {t}")
            continue
        if len(t) < _HOOK_TITLE_MIN_RETRY:
            changes.append(f"注意: topic{key} の題名が短め（{len(t)}字）")
        changes.append(f"topic{key} 題名: {a.get('hook_title', '')} → {t}")
        a["hook_title"] = t
        resolved.append((int(key), "hook_title"))

    for key, pts in (fixes.get("points") or {}).items():
        a = article(key)
        if a is None:
            continue
        cleaned = [_clean_point(p) for p in pts] if isinstance(pts, list) else []
        if not 1 <= len(cleaned) <= _POINTS_COUNT or any(not p or len(p) > _POINT_MAX for p in cleaned):
            errors.append(f"topic{key} の要点は1〜{_POINTS_COUNT}個・各{_POINT_MAX}字以内: {pts}")
            continue
        changes.append(f"topic{key} 要点: {a.get('points', [])} → {cleaned}")
        a["points"] = cleaned
        resolved.append((int(key), "points"))

    for k, item in enumerate(fixes.get("serif") or [], 1):
        old = item.get("old", "")
        new = item.get("new", "")
        if not isinstance(old, str) or not isinstance(new, str) or not old or not new:
            errors.append(f"serif[{k}]: old と new の両方が（文字列で）必要")
            continue
        count = script_md.count(old)
        if count != 1:
            errors.append(f"serif[{k}]: old が台本に{count}回見つかった（ちょうど1回にする）: {old}")
            continue
        # 書かれたルビ（読みの手直し）は尊重し、無い漢字にだけ自動付与する
        new_ruby = add_ruby_to_text(new)
        script_md = script_md.replace(old, new_ruby)
        changes.append(f"セリフ: {old} → {new_ruby}")

    if errors:
        raise FixError("\n".join(errors))
    if not changes:
        raise FixError("修正が1件もない")
    _resolve_factcheck(meta, resolved)
    return script_md, meta, changes


def _resolve_factcheck(meta: dict, resolved: list[tuple[int, str]]) -> None:
    """人が直した題名・要点の「要確認」を解消済みにする（同じWARNが残り続けないように）.

    セリフの修正はどの項目（紹介・解説・考察）か機械的に決められないので、要確認のまま残す。
    """
    fc = meta.get("factcheck") or {}
    for a in fc.get("articles") or []:
        keep = [u for u in a.get("unresolved", [])
                if (a.get("index"), u.get("field")) not in resolved]
        for u in a.get("unresolved", []):
            if u not in keep:
                a.setdefault("resolved_by_human", []).append(u)
        a["unresolved"] = keep
    if fc.get("status") == "unresolved" and not any(a.get("unresolved") for a in fc.get("articles") or []):
        fc["status"] = "fixed"


def apply_fixes(date_str: str, fixes: dict, config_path: Path = Path("config.json"),
                rebuild: bool = True, topic: str = "") -> list[str]:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    state_dir = Path(config["paths"]["state_dir"])
    script_path = state_dir / f"script_{date_str}.md"
    meta_path = state_dir / f"script_meta_{date_str}.json"
    script_md = script_path.read_text(encoding="utf-8")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    new_md, new_meta, changes = plan_fixes(fixes, script_md, meta)

    # バックアップ（時刻付き。前の pre_edit を上書きしない）
    old_dir = state_dir / "old"
    old_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S_%f")  # 1秒以内の連続実行でも上書きしない
    shutil.copy2(script_path, old_dir / f"script_{date_str}.pre_edit_{stamp}.md")
    shutil.copy2(meta_path, old_dir / f"script_meta_{date_str}.pre_edit_{stamp}.json")

    script_path.write_text(new_md, encoding="utf-8")
    meta_path.write_text(json.dumps(new_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    changes.append(f"バックアップ: {old_dir}/*{date_str}.pre_edit_{stamp}.*")

    if rebuild:
        # script_meta は script フェーズでしか書かれないので --from slides で安全に作り直せる
        from orchestrator import run_pipeline
        run_pipeline(date_str=date_str, config_path=Path(config_path),
                     skip_upload=True, from_phase="slides", topic=topic)
    return changes


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="確認で見つかった修正を反映して作り直す")
    parser.add_argument("date", help="YYYYMMDD")
    parser.add_argument("fixes", help="修正内容のJSONファイル")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--topic", default="", help="custom-news のトピック（作り直しと題名プレビュー用）")
    parser.add_argument("--no-rebuild", action="store_true", help="書き換えだけ行い、作り直さない")
    args = parser.parse_args()

    try:
        fixes = json.loads(Path(args.fixes).read_text(encoding="utf-8"))
        changes = apply_fixes(args.date, fixes, Path(args.config), rebuild=not args.no_rebuild,
                              topic=args.topic)
    except (FixError, OSError, ValueError) as e:  # ValueError: JSON不正。OSError: ファイルなし
        print(f"修正を反映しませんでした（ファイルは変更なし）:\n{e}")
        sys.exit(2)
    print("\n=== 反映した修正 ===")
    for c in changes:
        print(f"- {c}")

    from review_report import build_review, print_checks
    path, checks = build_review(args.date, Path(args.config), args.topic)
    print(f"\n確認用まとめ: {path}")
    print_checks(checks)
    sys.exit(1 if any(c["level"] == "FAIL" for c in checks) else 0)


if __name__ == "__main__":
    main()
