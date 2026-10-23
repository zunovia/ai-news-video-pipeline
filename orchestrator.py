"""Phase 6: パイプライン制御（オーケストレーター）.

ニュース収集 → 台本生成 → スライド生成 → 動画生成 → YouTube投稿を
順次実行し、進捗を管理する。途中再開・単一フェーズ実行に対応。
"""

from __future__ import annotations

import argparse
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PHASES = ["collect", "script", "slides", "video", "upload"]
SHORTS_PHASES = ["slides_shorts", "video_shorts", "upload_shorts"]

# 日付の既定値は日本時間（JST）基準。UTC基準だと日本の深夜〜朝9時の実行で
# 前日の日付になってしまう（このパイプラインは深夜0時台に実行されることが多い）
_JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    """日本時間での今日の日付（YYYYMMDD）を返す."""
    return datetime.now(_JST).strftime("%Y%m%d")


def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# VOICEVOX（TTSエンジン）の自動起動
# ---------------------------------------------------------------------------

_VOICEVOX_DEFAULT_ENGINE = r"C:\Users\zunov\AppData\Local\Programs\VOICEVOX\vv-engine\run.exe"


def _voicevox_alive(base_url: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_voicevox(config_path: Path, wait_sec: int = 90) -> None:
    """TTSがVOICEVOXなら、エンジンが応答しない場合に非表示で起動して待つ.

    起動できなければ例外（動画フェーズの途中でTTSが全滅するより先に止める）。
    """
    import subprocess
    import time
    from urllib.parse import urlparse

    tts = load_config(config_path).get("tts", {})
    if tts.get("provider") != "voicevox":
        return
    opts = tts.get("options", {})
    base_url = opts.get("base_url", "http://localhost:50021")
    if _voicevox_alive(base_url):
        return
    engine = Path(opts.get("engine_path", _VOICEVOX_DEFAULT_ENGINE))
    if not engine.exists():
        raise RuntimeError(f"VOICEVOX is not running and engine not found: {engine}")
    port = str(urlparse(base_url).port or 50021)
    logger.info("VOICEVOX not responding — starting %s (port %s)", engine, port)
    subprocess.Popen(
        [str(engine), "--host", "127.0.0.1", "--port", port],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        time.sleep(3)
        if _voicevox_alive(base_url):
            logger.info("VOICEVOX is up")
            return
    raise RuntimeError(f"VOICEVOX did not start within {wait_sec}s ({base_url})")


# ---------------------------------------------------------------------------
# Phase executors
# ---------------------------------------------------------------------------

def run_collect(date_str: str, config_path: Path, state: dict, topic: str = "") -> dict:
    from news_collector import collect_news
    news_path = collect_news(config_path, date_str, topic=topic)
    state["news_path"] = str(news_path)
    state["phases"]["collect"] = "done"
    return state


def run_script(date_str: str, config_path: Path, state: dict, script_type: str, topic: str = "") -> dict:
    from script_generator import generate_script
    news_path = Path(state.get("news_path", f"state/news_{date_str}.json"))
    script_path = generate_script(news_path, config_path, script_type, topic=topic)
    state["script_path"] = str(script_path)

    # 台本の生成元（llm / fallback）をパイプライン状態に記録して一目で分かるようにする
    config = load_config(config_path)
    meta_path = Path(config["paths"]["state_dir"]) / f"script_meta_{date_str}.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                state["script_source"] = json.load(f).get("source", "unknown")
        except Exception:
            state["script_source"] = "unknown"

    state["phases"]["script"] = "done"
    return state


def run_slides(date_str: str, config_path: Path, state: dict) -> dict:
    from slide_generator import generate_slides
    script_path = Path(state.get("script_path", f"state/script_{date_str}.md"))
    images_dir = generate_slides(script_path, config_path, date_str)
    state["images_dir"] = str(images_dir)

    # カスタムサムネイル生成（config.youtube.custom_thumbnail で制御。失敗しても続行）
    try:
        from slide_generator import generate_thumbnail
        thumb_path = generate_thumbnail(date_str, config_path)
        if thumb_path:
            state["thumbnail_path"] = str(thumb_path)
    except Exception as e:
        logger.warning("Thumbnail generation failed (continuing): %s", e)

    state["phases"]["slides"] = "done"
    return state


def run_video(date_str: str, config_path: Path, state: dict) -> dict:
    from video_builder import build_video
    ensure_voicevox(config_path)
    video_path = build_video(date_str, config_path)
    state["video_path"] = str(video_path)
    state["phases"]["video"] = "done"
    return state


def run_upload(date_str: str, config_path: Path, state: dict, script_type: str, topic: str = "") -> dict:
    from youtube_uploader import upload
    video_url = upload(date_str, config_path, script_type, topic=topic)
    state["video_url"] = video_url
    state["phases"]["upload"] = "done"
    return state


# --- Shorts phase runners ---

def run_slides_shorts(date_str: str, config_path: Path, state: dict) -> dict:
    from slide_generator import generate_slides
    script_path = Path(state.get("script_path", f"state/script_{date_str}.md"))
    images_dir = generate_slides(script_path, config_path, date_str, shorts=True)
    state["images_shorts_dir"] = str(images_dir)
    state["phases"]["slides_shorts"] = "done"
    return state


def _per_topic_enabled(config_path: Path) -> bool:
    return load_config(config_path).get("shorts", {}).get("per_topic", False)


def run_video_shorts(date_str: str, config_path: Path, state: dict) -> dict:
    ensure_voicevox(config_path)
    if _per_topic_enabled(config_path):
        # 「1ニュース=1本」モード: トップN記事それぞれを独立したShortsに
        from video_builder import build_topic_shorts
        video_paths = build_topic_shorts(date_str, config_path)
        state["video_shorts_paths"] = [str(p) for p in video_paths]
        # 確認用まとめ（題名・要点・セリフ・元記事・事実確認・チェック結果）を1ファイルに
        try:
            from review_report import build_review
            review_path, checks = build_review(date_str, config_path, state.get("topic", ""))
            state["review_path"] = str(review_path)
            state["preflight"] = {lv: sum(1 for c in checks if c["level"] == lv)
                                  for lv in ("FAIL", "WARN")}
            logger.info("Review: %s (FAIL %d / WARN %d)", review_path,
                        state["preflight"]["FAIL"], state["preflight"]["WARN"])
        except Exception as e:
            logger.warning("Review report failed (continuing): %s", e)
    else:
        from video_builder import build_video
        video_path = build_video(date_str, config_path, shorts=True)
        state["video_shorts_path"] = str(video_path)
    state["phases"]["video_shorts"] = "done"
    return state


def run_upload_shorts(date_str: str, config_path: Path, state: dict, script_type: str, topic: str = "") -> dict:
    if _per_topic_enabled(config_path):
        # プリフライト: 題名の読みがな残り・動画欠落・3分超などがあればアップロードしない
        from review_report import preflight
        fails = [c for c in preflight(date_str, load_config(config_path), topic=topic)
                 if c["level"] == "FAIL"]
        if fails:
            raise RuntimeError("Preflight FAIL — upload aborted: " + " / ".join(
                f"topic{c['topic']} {c['check']} {c['detail']}".strip() for c in fails))
        from youtube_uploader import upload_topic_shorts
        video_urls = upload_topic_shorts(date_str, config_path, topic=topic)
        state["video_shorts_urls"] = video_urls
    else:
        from youtube_uploader import upload
        video_url = upload(date_str, config_path, script_type, shorts=True, topic=topic)
        state["video_shorts_url"] = video_url
    state["phases"]["upload_shorts"] = "done"
    return state


PHASE_RUNNERS = {
    "collect": lambda d, c, s, t, tp: run_collect(d, c, s, topic=tp),
    "script": lambda d, c, s, t, tp: run_script(d, c, s, t, topic=tp),
    "slides": lambda d, c, s, t, tp: run_slides(d, c, s),
    "video": lambda d, c, s, t, tp: run_video(d, c, s),
    "upload": lambda d, c, s, t, tp: run_upload(d, c, s, t, topic=tp),
    "slides_shorts": lambda d, c, s, t, tp: run_slides_shorts(d, c, s),
    "video_shorts": lambda d, c, s, t, tp: run_video_shorts(d, c, s),
    "upload_shorts": lambda d, c, s, t, tp: run_upload_shorts(d, c, s, t, topic=tp),
}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    date_str: str | None = None,
    config_path: Path = Path("config.json"),
    step: str | None = None,
    script_type: str = "daily",
    skip_upload: bool = False,
    shorts: bool = False,
    force: bool = False,
    topic: str = "",
    from_phase: str = "",
) -> dict:
    """Run the full pipeline or a single step.

    Args:
        date_str: Date in YYYYMMDD format. Defaults to today.
        config_path: Path to config.json.
        step: If set, run only this phase.
        script_type: "daily" or "weekly".
        skip_upload: If True, skip the upload phase.
        shorts: If True, also generate and upload Shorts video.
        force: If True, reset all phases and re-run from scratch.
        topic: Custom news topic (e.g. "車", "宇宙"). Empty = default AI news.
        from_phase: If set, reset this phase and all subsequent phases, then run them.

    Returns:
        Final state dict.
    """
    if script_type != "daily":
        raise NotImplementedError(
            f"script_type '{script_type}' is not yet implemented. Only 'daily' is supported."
        )

    config = load_config(config_path)
    state_dir = Path(config["paths"]["state_dir"])

    if date_str is None:
        date_str = today_jst()

    state_path = state_dir / f"pipeline_{date_str}.json"
    state = load_state(state_path)

    # Force mode: reset all phases
    if force:
        state["phases"] = {}
        state.pop("error", None)
        logger.info("Force mode: resetting all phases")

    # From-phase mode: reset this phase and all subsequent phases
    if from_phase and not force:
        all_phase_order = ["collect", "script", "slides", "slides_shorts",
                           "video", "video_shorts", "upload", "upload_shorts"]
        if from_phase in all_phase_order:
            idx = all_phase_order.index(from_phase)
            for p in all_phase_order[idx:]:
                state.get("phases", {}).pop(p, None)
            state.pop("error", None)
            logger.info("From-phase mode: resetting from '%s' onward", from_phase)

    # Initialize state
    if "phases" not in state:
        state["phases"] = {}
    state["date"] = date_str
    state["type"] = script_type
    if topic:
        state["topic"] = topic
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # Determine which phases to run
    # 16:9まとめ動画は config.pipeline.daily_video_enabled で無効化できる
    # （2026-07刷新: 長尺デイリーは廃止し「1ニュース=1本」Shortsのみ運用。
    #   --step で個別フェーズを指定した場合はこの制御を通らず手動実行できる）
    daily_enabled = config.get("pipeline", {}).get("daily_video_enabled", True)
    if not daily_enabled:
        all_phases = ["collect", "script", "slides_shorts", "video_shorts", "upload_shorts"]
        logger.info("16:9 daily video is disabled (pipeline.daily_video_enabled=false) "
                    "— running Shorts-only pipeline")
    elif shorts:
        # Insert Shorts phases after their corresponding regular phases
        # slides → slides_shorts → video → video_shorts → upload → upload_shorts
        all_phases = ["collect", "script", "slides", "slides_shorts",
                      "video", "video_shorts", "upload", "upload_shorts"]
    else:
        all_phases = list(PHASES)

    if step:
        phases_to_run = [step]
    else:
        phases_to_run = [p for p in all_phases if state["phases"].get(p) != "done"]
        if skip_upload:
            phases_to_run = [p for p in phases_to_run if p not in ("upload", "upload_shorts")]

    logger.info(
        "Pipeline: date=%s, type=%s, phases=%s",
        date_str, script_type, phases_to_run,
    )

    for phase in phases_to_run:
        if phase not in PHASE_RUNNERS:
            raise ValueError(f"Unknown phase: {phase}")

        logger.info("--- Starting phase: %s ---", phase)
        state["phases"][phase] = "running"
        save_state(state_path, state)

        try:
            state = PHASE_RUNNERS[phase](date_str, config_path, state, script_type, topic)
            save_state(state_path, state)
            logger.info("--- Phase %s completed ---", phase)
        except Exception as e:
            state["phases"][phase] = "failed"
            state["error"] = {
                "phase": phase,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }
            save_state(state_path, state)
            logger.error("Phase %s failed: %s", phase, e)
            raise

    # Clear previous error on successful completion
    state.pop("error", None)
    save_state(state_path, state)

    logger.info("Pipeline completed for %s", date_str)
    return state


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="AI News Video Pipeline Orchestrator",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date in YYYYMMDD format (default: today)",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Config file path",
    )
    parser.add_argument(
        "--step",
        default=None,
        choices=PHASES + SHORTS_PHASES,
        help="Run only this phase",
    )
    parser.add_argument(
        "--type",
        default="daily",
        choices=["daily", "weekly"],
        help="Script type",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip YouTube upload phase",
    )
    parser.add_argument(
        "--shorts",
        action="store_true",
        help="Also generate and upload Shorts (9:16) video",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reset all phases and re-run from scratch",
    )
    parser.add_argument(
        "--topic",
        default="",
        help="Custom news topic (e.g. '車', '宇宙'). Default: AI news",
    )
    parser.add_argument(
        "--from",
        dest="from_phase",
        default="",
        choices=["", "collect", "script", "slides", "slides_shorts",
                 "video", "video_shorts", "upload", "upload_shorts"],
        help="Re-run from this phase onward (resets this and all subsequent phases)",
    )
    args = parser.parse_args()

    state = run_pipeline(
        date_str=args.date,
        config_path=Path(args.config),
        step=args.step,
        script_type=args.type,
        skip_upload=args.skip_upload,
        shorts=args.shorts,
        force=args.force,
        topic=args.topic,
        from_phase=args.from_phase,
    )

    print("\n=== Pipeline Result ===")
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
