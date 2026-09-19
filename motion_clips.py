"""Ken Burns モーションクリップ生成モジュール.

レンダリング済みスライドPNGから、ゆっくりズームイン/アウトする動画クリップ
(slide_N.mp4) を生成する。911-plan エンジンの video_clips_dir 経路に渡すことで、
静止画スライドの代わりにモーション付きスライドで動画を組み立てられる。

911-plan/generate_video.py との整合のための技術的制約:
- create_scene_from_video() は -stream_loop -1 + -c:v copy + -t でトリムするため、
  クリップは全フレームキーフレーム（-g 1 = イントラオンリー）で生成する。
  これによりストリームコピーのトリムがフレーム精度になり、
  xfade の offset 計算（get_video_duration ベース）と整合する。
- 解像度・fps はエンジン設定と完全一致させる（エンジン側に scale フィルタが無いため）。
- クリップ長は推定シーン尺より十分長くし、-stream_loop のループ巻き戻り
  （ズームが初期値へ戻るジャンプ）が映像に出ないようにする。
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 推定誤差マージン: 推定尺（保守的=長め）に対してさらに上乗せする倍率と固定秒
_DURATION_MARGIN_RATIO = 1.15
_DURATION_MARGIN_SEC = 2.0


def _run_ffmpeg(args: list[str], description: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"{description} failed (ffmpeg exit {result.returncode}):\n{result.stderr[-2000:]}")


def generate_motion_clip(
    image_path: Path,
    output_path: Path,
    duration_sec: float,
    resolution: tuple[int, int] | list[int],
    fps: int = 30,
    max_zoom: float = 1.10,
    zoom_in: bool = True,
) -> Path:
    """スライドPNG 1枚から Ken Burns ズームクリップ (slide_N.mp4) を生成する.

    Args:
        image_path: 入力スライドPNG（解像度は resolution と同じ前提）。
        output_path: 出力MP4パス。
        duration_sec: クリップ長（秒）。シーン尺より長くすること。
        resolution: (width, height)。エンジン設定と完全一致させる。
        fps: フレームレート。エンジン設定と完全一致させる。
        max_zoom: 最大ズーム倍率（1.10 = 10%ズーム）。
        zoom_in: True=ズームイン、False=ズームアウト。
    """
    width, height = int(resolution[0]), int(resolution[1])
    frames = max(int(duration_sec * fps), fps)

    # クリップ尻でちょうど max_zoom に到達する速度
    zoom_speed = (max_zoom - 1.0) / frames
    if zoom_in:
        z_expr = f"min(1+{zoom_speed:.8f}*on,{max_zoom})"
    else:
        z_expr = f"max({max_zoom}-{zoom_speed:.8f}*on,1.0)"

    # 2倍中間スケールで zoompan の座標丸めジッタを抑制
    vf = (
        f"scale=iw*2:ih*2,"
        f"zoompan=z='{z_expr}'"
        f":x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
        f":d={frames}:s={width}x{height}:fps={fps},"
        f"format=yuv420p"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-i", str(image_path),
            "-vf", vf,
            "-frames:v", str(frames),
            "-c:v", "libx264",
            "-g", "1",  # 全フレームキーフレーム（-c:v copy トリムのフレーム精度に必須）
            "-crf", "23",
            "-preset", "veryfast",
            "-an",
            str(output_path),
        ],
        f"モーションクリップ生成 ({output_path.name})",
    )
    return output_path


def generate_motion_clips(
    slide_specs: list[dict],
    images_dir: Path,
    clips_dir: Path,
    resolution: tuple[int, int] | list[int],
    fps: int = 30,
    max_zoom: float = 1.10,
) -> Path:
    """全スライドのモーションクリップを生成する.

    911-plan の resolve_video_clip_path() は video_clips_dir 指定時に
    全スライド分のクリップを要求する（欠落で FileNotFoundError）ため、
    slide_specs の全エントリについて必ずクリップを生成する。

    Args:
        slide_specs: [{"number": int, "duration": float}] のリスト。
                     duration は推定シーン尺（マージンはここで上乗せする）。
        images_dir: slide_{N}.png のあるディレクトリ。
        clips_dir: slide_{N}.mp4 の出力先。
        resolution: (width, height)。
        fps: フレームレート。
        max_zoom: 最大ズーム倍率。
    """
    clips_dir.mkdir(parents=True, exist_ok=True)

    for i, spec in enumerate(slide_specs):
        number = spec["number"]
        image_path = images_dir / f"slide_{number}.png"
        if not image_path.exists():
            raise FileNotFoundError(f"Slide image not found for motion clip: {image_path}")

        clip_duration = spec["duration"] * _DURATION_MARGIN_RATIO + _DURATION_MARGIN_SEC
        output_path = clips_dir / f"slide_{number}.mp4"

        # 奇数番目=ズームイン / 偶数番目=ズームアウトで単調さを避ける
        zoom_in = (i % 2 == 0)

        logger.info("Motion clip %d/%d: slide_%d (%.1fs, zoom %s)",
                    i + 1, len(slide_specs), number, clip_duration,
                    "in" if zoom_in else "out")
        generate_motion_clip(
            image_path, output_path, clip_duration,
            resolution=resolution, fps=fps, max_zoom=max_zoom, zoom_in=zoom_in,
        )

    logger.info("Generated %d motion clips in %s", len(slide_specs), clips_dir)
    return clips_dir


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ken Burns Motion Clip Generator")
    parser.add_argument("--images", required=True, help="Slide images directory")
    parser.add_argument("--script", required=True, help="Script markdown path")
    parser.add_argument("--out", required=True, help="Output clips directory")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-zoom", type=float, default=1.10)
    args = parser.parse_args()

    from slide_generator import parse_slides
    from video_builder import _estimate_slide_seconds

    slides = parse_slides(Path(args.script))
    specs = [{"number": s["number"], "duration": _estimate_slide_seconds(s.get("serif", ""))}
             for s in slides]
    generate_motion_clips(
        specs, Path(args.images), Path(args.out),
        resolution=(args.width, args.height), fps=args.fps, max_zoom=args.max_zoom,
    )


if __name__ == "__main__":
    main()
