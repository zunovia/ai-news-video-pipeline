"""Phase 4: 動画生成モジュール.

911-planのgenerate_video.pyを再利用して、
スライド画像＋台本から動画（MP4）を生成する。

台本パースは912-plan独自のslide_generator.parse_slides()を使用する。
911-planの_SERIF_TEXT_RE（lazy match）はネスト括弧（例: 「「Cowork」を...」）で
セリフが最初の」で切断されるバグがあるため、改良版正規表現を持つ
slide_generator.parse_slides()の結果をSlideEntryに変換して使う。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Serif → TTS text conversion (shared by video build and duration estimation)
# ---------------------------------------------------------------------------

# 括弧内の演出指示を除去（例: 「（3秒の間の後）...」）
_DIRECTION_RE = re.compile(r"（[^）]*）")
# 英語(カタカナ) → カタカナ（VOICEVOXはカタカナを読む）
_ENG_RUBY_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_\s\-\.]*[A-Za-z0-9]|[A-Za-z0-9])[(（]([ぁ-んァ-ヶー]+)[)）]"
)
# 漢字のルビ記法を除去: '人工知能(じんこうちのう)' → '人工知能'
_KANJI_RUBY_RE = re.compile(r"[(（][ぁ-んァ-ヶー]+[)）]")


def serif_to_tts_text(serif: str) -> str:
    """セリフからTTSが実際に読み上げるテキストを生成する（ルビ・演出指示を除去）."""
    text = _DIRECTION_RE.sub("", serif).strip()
    text = _ENG_RUBY_RE.sub(r"\2", text)
    text = _KANJI_RUBY_RE.sub("", text)
    return text


def serif_to_caption_text(serif: str) -> str:
    """セリフから字幕表示用テキストを生成する.

    TTS用と違い、英語はカタカナ読みではなく英語表記のまま残す
    （例: Siri(シリ) → Siri）。漢字ルビ・演出指示は除去する。
    """
    text = _DIRECTION_RE.sub("", serif).strip()
    text = _ENG_RUBY_RE.sub(r"\1", text)
    text = _KANJI_RUBY_RE.sub("", text)
    return text


# VOICEVOX(speaking_rate=1.0)の実測値は約6.4文字/秒。推定を長めに見積もって
# 確実に上限内へ収めるため、やや遅め(6.0)を採用する。
_SHORTS_EST_CPS = 6.0
# スライドごとの固定オーバーヘッド(パディング等)の安全マージン（秒）
_SHORTS_SLIDE_OVERHEAD = 0.6


def _estimate_slide_seconds(serif: str) -> float:
    """セリフのTTS読み上げ尺を文字数から推定する（保守的=長め）."""
    if not serif:
        return _SHORTS_SLIDE_OVERHEAD
    return len(serif_to_tts_text(serif)) / _SHORTS_EST_CPS + _SHORTS_SLIDE_OVERHEAD


def _group_article_units(middle_slides: list[dict]) -> list[list[dict]]:
    """中間スライドを「記事ユニット」（紹介＋ポイント）へグループ化する.

    「〜のポイント」スライドは直前の紹介スライドに属する続きなので同じユニットへ。
    """
    units: list[list[dict]] = []
    for s in middle_slides:
        title = s.get("title", "")
        if title.endswith("ポイント") and units:
            units[-1].append(s)
        else:
            if title.endswith("ポイント"):
                # 異常台本の早期検出: 紹介スライドのない孤立ポイント
                logger.warning("Point slide has no preceding intro: %s", title)
            units.append([s])
    return units


def select_topic_units(slides: list[dict], top_n: int) -> list[list[dict]]:
    """「1ニュース=1本」Shorts用に、記事ユニットを先頭からtop_n件返す.

    オープニング・インデックス・キーワード・エンディングは含めない
    （単体で完結する45〜75秒のショート動画にするため）。

    Args:
        slides: parse_slides()の結果。
        top_n: 動画化する記事数（ニュースは重要度順に並んでいる前提）。

    Returns:
        記事ユニットのリスト。各ユニットは [紹介スライド, ポイントスライド]。
    """
    middle = [
        s for s in slides
        if not s.get("is_opening") and not s.get("is_ending")
        and not s.get("is_index") and not s.get("is_keyword")
    ]
    units = _group_article_units(middle)
    selected = units[:top_n]
    logger.info("Topic unit selection: %d articles → %d shorts videos",
                len(units), len(selected))
    return selected


def select_shorts_slides(slides: list[dict], max_seconds: float) -> list[dict]:
    """Shorts用にスライドを尺予算内へ自動選択する.

    YouTube Shortsは3分以内のみ。オープニングとエンディングは必ず残し、
    インデックススライドは除外、記事は「紹介＋ポイント」を1ユニットとして
    予算に収まる分だけ先頭から順に採用する（記事が途中で切れないようにする）。

    Args:
        slides: parse_slides()の結果。各dictは is_opening/is_ending/is_index を持つ。
        max_seconds: Shorts全体の目標上限秒数（推定ベース）。

    Returns:
        採用したスライドのリスト（元の順序を保持）。
    """
    opening = [s for s in slides if s.get("is_opening")]
    ending = [s for s in slides if s.get("is_ending")]
    # 中間スライド（インデックス・オープニング・エンディングを除く）
    middle = [
        s for s in slides
        if not s.get("is_opening") and not s.get("is_ending") and not s.get("is_index")
    ]

    units = _group_article_units(middle)

    reserved = sum(_estimate_slide_seconds(s.get("serif", "")) for s in opening + ending)
    budget = max_seconds - reserved

    selected_units: list[list[dict]] = []
    used = 0.0
    for unit in units:
        unit_sec = sum(_estimate_slide_seconds(s.get("serif", "")) for s in unit)
        if used + unit_sec <= budget:
            selected_units.append(unit)
            used += unit_sec
        else:
            # 予算超過。これ以降のユニットは採用しない（順序・記事完結性を維持）
            break

    selected_middle = [s for unit in selected_units for s in unit]
    result = opening + selected_middle + ending

    dropped = len(slides) - len(result)
    est_total = reserved + used
    logger.info(
        "Shorts slide selection: %d → %d slides (%d articles kept, %d slides dropped), "
        "est. %.0fs (budget %.0fs)",
        len(slides), len(result), len(selected_units), dropped, est_total, max_seconds,
    )
    return result


def load_config(config_path: Path = Path("config.json")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def setup_engine_path(config: dict) -> Path:
    """Add 911-plan to sys.path and return its directory."""
    engine_dir = Path(config["paths"]["video_engine_dir"]).resolve()
    if not engine_dir.exists():
        raise FileNotFoundError(f"Video engine directory not found: {engine_dir}")
    engine_str = str(engine_dir)
    if engine_str not in sys.path:
        sys.path.insert(0, engine_str)
    return engine_dir


def build_engine_config(config: dict, images_dir: Path, script_path: Path, output_dir: Path) -> Path:
    """Create a temporary config.json for 911-plan's Config.from_json()."""
    engine_config = {
        "tts": {
            "provider": config["tts"]["provider"],
            "language": config["tts"]["language"],
            "voice": config["tts"]["voice"],
            "speaking_rate": config["tts"]["speaking_rate"],
            "pitch": config["tts"]["pitch"],
            "options": config["tts"]["options"],
        },
        "video": config["video"],
        "bgm": {
            **config["bgm"],
            "file": str(Path(config["bgm"]["file"]).resolve()) if config["bgm"].get("file") else "",
        },
        "paths": {
            "images_dir": str(images_dir.resolve()),
            "script_file": str(script_path.resolve()),
            "output_dir": str(output_dir.resolve()),
        },
    }

    # Write temp config next to output dir so relative paths resolve correctly
    tmp_config = output_dir / "_temp_engine_config.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w", encoding="utf-8") as f:
        json.dump(engine_config, f, ensure_ascii=False, indent=2)

    return tmp_config


def _slides_to_entries(slides: list[dict], gv_module) -> list:
    """slide_generator.parse_slides()の結果をgv.SlideEntryのリストに変換する.

    slide_generator.parse_slides()は改良版正規表現
    ``r"「((?:[^「」]|「[^」]*」)*)」"`` を使うため、
    ネスト括弧（例: 「「Cowork」を発表」）を正しく抽出できる。
    911-planのparse_script()で使われるlazy matchパターン
    ``r"「(.+?)」"`` は最初の」で停止するためネスト括弧を切断するバグがある。

    Args:
        slides: slide_generator.parse_slides()が返すdictリスト。
                各dictは {"number", "title", "serif", "is_opening",
                          "is_ending", "is_index"} を持つ。
        gv_module: インポート済みのgenerate_videoモジュール。
                   SlideEntryクラスおよび演出指示除去ロジックに使用する。

    Returns:
        gv.SlideEntryのリスト。
    """
    entries = []
    for slide in slides:
        serif = slide["serif"]
        if serif:
            # ルビ記法・演出指示を除去してTTS読み上げテキストを生成
            text = serif_to_tts_text(serif)
            entries.append(
                gv_module.SlideEntry(
                    number=slide["number"],
                    title=slide["title"],
                    text=text,
                    is_silent=False,
                )
            )
        else:
            entries.append(
                gv_module.SlideEntry(
                    number=slide["number"],
                    title=slide["title"],
                    text="",
                    is_silent=True,
                )
            )
    return entries


def build_video(
    date_str: str,
    config_path: Path = Path("config.json"),
    shorts: bool = False,
) -> Path:
    """Build video from slides + script using 911-plan engine.

    Returns path to the generated MP4 file.
    """
    config = load_config(config_path)
    state_dir = Path(config["paths"]["state_dir"])
    output_dir = Path(config["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    script_path = state_dir / f"script_{date_str}.md"
    images_dir = state_dir / ("images_shorts" if shorts else "images")

    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    if not images_dir.exists():
        raise FileNotFoundError(f"Images directory not found: {images_dir}")

    # Setup 911-plan import
    engine_dir = setup_engine_path(config)

    # Override resolution for Shorts
    if shorts:
        config = {**config, "video": {
            **config["video"],
            "resolution": config["video"].get("shorts_resolution", [1080, 1920]),
        }}

    # Create engine config
    tmp_config = build_engine_config(config, images_dir, script_path, output_dir)

    try:
        # Import 911-plan modules
        import generate_video as gv

        # Import 912-plan slide_generator for correct nested-bracket parsing.
        # slide_generator uses r"「((?:[^「」]|「[^」]*」)*)」" which handles
        # nested 「」 brackets. gv.parse_script() uses r"「(.+?)」" (lazy match)
        # which stops at the first 」 and truncates serif text in nested cases.
        import slide_generator as sg

        # Load config
        engine_cfg = gv.Config.from_json(tmp_config)

        # Parse script via slide_generator (improved regex), then convert to SlideEntry
        raw_slides = sg.parse_slides(script_path)
        # Shorts: 3分(180秒)以内に収まるようスライドを尺予算で自動選択
        # （オープニング+エンディングは必ず残し、インデックスは除外、記事は完全な
        #   ペア単位で予算内まで採用）。上限は config.video.shorts_max_duration_sec。
        if shorts:
            max_sec = config["video"].get("shorts_max_duration_sec", 170)
            raw_slides = select_shorts_slides(raw_slides, max_sec)
        entries = _slides_to_entries(raw_slides, gv)
        logger.info("Parsed %d slides from script (via slide_generator)", len(entries))

        # Ken Burns モーション: 有効時は全スライドのズームクリップを生成し、
        # エンジンの動画クリップ経路（video_clips_dir）で組み立てる。
        # 注意: video_clips_dir 指定時はエンジンが全スライド分のクリップを要求するため、
        # 採用確定後の raw_slides 全件分を生成する。
        clips_dir = None
        motion_cfg = config["video"].get("motion", {})
        if motion_cfg.get("enabled", False):
            from motion_clips import generate_motion_clips

            silence_sec = config["video"].get("silence_duration", 3.5)
            padding = (config["video"].get("padding_before", 0.3)
                       + config["video"].get("padding_after", 0.3))
            specs = []
            for s in raw_slides:
                serif = s.get("serif", "")
                if serif:
                    duration = _estimate_slide_seconds(serif) + padding
                else:
                    duration = silence_sec + padding
                specs.append({"number": s["number"], "duration": duration})

            clips_dir = state_dir / ("clips_shorts" if shorts else "clips")
            logger.info("Motion enabled: generating %d Ken Burns clips → %s",
                        len(specs), clips_dir)
            generate_motion_clips(
                specs,
                images_dir,
                clips_dir,
                resolution=config["video"]["resolution"],
                fps=config["video"].get("fps", 30),
                max_zoom=motion_cfg.get("max_zoom", 1.10),
            )

        # Generate video
        result = gv.process_slides(entries, engine_cfg, video_clips_dir=clips_dir)

        if result is None:
            raise RuntimeError("Video generation returned None")

        # Rename to dated filename
        suffix = "shorts" if shorts else "daily"
        final_path = output_dir / f"{date_str}_{suffix}.mp4"
        if result != final_path:
            if final_path.exists():
                final_path.unlink()
            result.rename(final_path)

        logger.info("Video generated: %s", final_path)
        return final_path

    finally:
        # Cleanup temp config
        if tmp_config.exists():
            tmp_config.unlink()


# ---------------------------------------------------------------------------
# 字幕（SRT）生成 — 台本テキスト＋TTS音声の実尺から正確な字幕を作る
# ---------------------------------------------------------------------------

# 句読点の直後で分割する。ただし直後が閉じ括弧（』」)）の場合は分割しない
# （『無断使用だ！』のような引用が2キューに割れるのを防ぐ）
_CAPTION_SENTENCE_RE = re.compile(r"(?<=[。！？!?])(?![』」)）])")


def _ffprobe_duration(path: Path) -> float:
    """メディアファイルの実尺（秒）をffprobeで取得する."""
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr[-500:]}")
    return float(result.stdout.strip())


def _format_srt_time(sec: float) -> str:
    """秒を SRT のタイムコード（HH:MM:SS,mmm）に変換する."""
    ms = max(int(round(sec * 1000)), 0)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _sentence_cues(text: str, start: float, duration: float) -> list[tuple[float, float, str]]:
    """1スライド分の字幕テキストを文単位のキューに分割する.

    各文の表示時間は文字数に比例配分する（TTSの読み上げ速度がほぼ一定のため）。
    """
    sentences = [s.strip() for s in _CAPTION_SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        return []
    total_chars = sum(len(s) for s in sentences)
    cues = []
    cursor = start
    for s in sentences:
        d = duration * len(s) / total_chars
        cues.append((cursor, min(cursor + d, start + duration), s))
        cursor += d
    return cues


def generate_topic_captions(
    date_str: str,
    config_path: Path = Path("config.json"),
) -> list[Path]:
    """「1ニュース=1本」Shorts各動画のSRT字幕を生成する.

    タイミングはTTS音声（output/audio/slide_NN.mp3）の実尺と、エンジンの
    シーン構成（padding_before + 音声 + padding_after、シーン間はクロス
    フェードで transition_duration 秒重なる）から計算する。
    出力: state/captions/{date}_topic{i}.srt

    動画ビルド後に呼ぶこと（音声ファイルが必要）。失敗しても動画は無事なので
    呼び出し側は非致命として扱ってよい。
    """
    config = load_config(config_path)
    state_dir = Path(config["paths"]["state_dir"])
    output_dir = Path(config["paths"]["output_dir"])
    audio_dir = output_dir / "audio"
    captions_dir = state_dir / "captions"
    captions_dir.mkdir(parents=True, exist_ok=True)

    script_path = state_dir / f"script_{date_str}.md"
    import slide_generator as sg
    raw_slides = sg.parse_slides(script_path)
    top_n = config.get("shorts", {}).get("topics_per_day", 3)
    units = select_topic_units(raw_slides, top_n)

    video_cfg = config.get("video", {})
    pb = video_cfg.get("padding_before", 0.3)
    pa = video_cfg.get("padding_after", 0.3)
    trans = video_cfg.get("transition_duration", 0.5)

    out_paths: list[Path] = []
    for i, unit in enumerate(units, 1):
        cues: list[tuple[float, float, str]] = []
        t = 0.0  # タイムライン上の現シーン開始位置
        for s in unit:
            audio_path = audio_dir / f"slide_{s['number']:02d}.mp3"
            if not audio_path.exists():
                raise FileNotFoundError(f"Audio not found for caption timing: {audio_path}")
            audio_dur = _ffprobe_duration(audio_path)
            text = serif_to_caption_text(s.get("serif", ""))
            cues.extend(_sentence_cues(text, t + pb, audio_dur))
            # 次のシーンはクロスフェードで trans 秒早く（重なって）始まる
            t += pb + audio_dur + pa - trans

        srt_lines = []
        for idx, (a, b, txt) in enumerate(cues, 1):
            srt_lines.append(f"{idx}\n{_format_srt_time(a)} --> {_format_srt_time(b)}\n{txt}\n")
        srt_path = captions_dir / f"{date_str}_topic{i}.srt"
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        # 検証: 計算上の総尺と実動画の尺のズレを警告（0.8秒超で通知）
        video_path = output_dir / f"{date_str}_shorts_topic{i}.mp4"
        if video_path.exists() and cues:
            expected = t + trans  # 最後のシーンは重なりを引かない
            actual = _ffprobe_duration(video_path)
            if abs(expected - actual) > 0.8:
                logger.warning("Caption timing drift for topic %d: expected %.1fs vs video %.1fs",
                               i, expected, actual)
        logger.info("Caption generated: %s (%d cues)", srt_path, len(cues))
        out_paths.append(srt_path)

    return out_paths


def build_topic_shorts(
    date_str: str,
    config_path: Path = Path("config.json"),
) -> list[Path]:
    """「1ニュース=1本」のShorts動画をトップN記事分ビルドする.

    台本の記事ユニット（紹介＋ポイント）ごとに独立した9:16動画を生成する。
    出力: output/{date}_shorts_topic{i}.mp4（i=1..N、ニュースの重要度順）。

    Returns:
        生成したMP4パスのリスト。
    """
    config = load_config(config_path)
    state_dir = Path(config["paths"]["state_dir"])
    output_dir = Path(config["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    script_path = state_dir / f"script_{date_str}.md"
    images_dir = state_dir / "images_shorts"
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    if not images_dir.exists():
        raise FileNotFoundError(f"Images directory not found: {images_dir}")

    setup_engine_path(config)

    shorts_cfg = config.get("shorts", {})
    top_n = shorts_cfg.get("topics_per_day", 3)
    max_topic_sec = shorts_cfg.get("max_topic_duration_sec", 75)

    # Shorts解像度で上書き
    config = {**config, "video": {
        **config["video"],
        "resolution": config["video"].get("shorts_resolution", [1080, 1920]),
    }}

    tmp_config = build_engine_config(config, images_dir, script_path, output_dir)

    try:
        import generate_video as gv
        import slide_generator as sg

        engine_cfg = gv.Config.from_json(tmp_config)

        raw_slides = sg.parse_slides(script_path)
        units = select_topic_units(raw_slides, top_n)
        if not units:
            raise ValueError(f"No article units found in script: {script_path}")

        # Ken Burns モーション: 採用スライド全件分のクリップを先に生成する
        # （クリップは slide_{number}.mp4 でスライド番号キーのため全ユニット共有可）
        clips_dir = None
        motion_cfg = config["video"].get("motion", {})
        if motion_cfg.get("enabled", False):
            from motion_clips import generate_motion_clips

            silence_sec = config["video"].get("silence_duration", 3.5)
            padding = (config["video"].get("padding_before", 0.3)
                       + config["video"].get("padding_after", 0.3))
            specs = []
            for unit in units:
                for s in unit:
                    serif = s.get("serif", "")
                    duration = (_estimate_slide_seconds(serif) + padding
                                if serif else silence_sec + padding)
                    specs.append({"number": s["number"], "duration": duration})

            clips_dir = state_dir / "clips_shorts"
            logger.info("Motion enabled: generating %d Ken Burns clips → %s",
                        len(specs), clips_dir)
            generate_motion_clips(
                specs,
                images_dir,
                clips_dir,
                resolution=config["video"]["resolution"],
                fps=config["video"].get("fps", 30),
                max_zoom=motion_cfg.get("max_zoom", 1.10),
            )

        outputs: list[Path] = []
        for i, unit in enumerate(units, 1):
            est = sum(_estimate_slide_seconds(s.get("serif", "")) for s in unit)
            if est > max_topic_sec:
                logger.warning("Topic %d estimated %.0fs > %ds budget (continuing)",
                               i, est, max_topic_sec)

            entries = _slides_to_entries(unit, gv)
            logger.info("--- Building topic short %d/%d (%d slides, est. %.0fs) ---",
                        i, len(units), len(unit), est)
            result = gv.process_slides(entries, engine_cfg, video_clips_dir=clips_dir)
            if result is None:
                raise RuntimeError(f"Video generation returned None (topic {i})")

            final_path = output_dir / f"{date_str}_shorts_topic{i}.mp4"
            if result != final_path:
                if final_path.exists():
                    final_path.unlink()
                result.rename(final_path)
            logger.info("Topic short generated: %s", final_path)
            outputs.append(final_path)

        # 字幕（SRT）を生成（非致命: 失敗しても動画ビルドは成功扱い）
        try:
            generate_topic_captions(date_str, config_path)
        except Exception as e:
            logger.warning("Caption generation failed (videos still OK): %s", e)

        return outputs

    finally:
        if tmp_config.exists():
            tmp_config.unlink()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Video Builder")
    parser.add_argument("--date", required=True, help="Date string YYYYMMDD")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--shorts", action="store_true", help="Build Shorts (9:16) video")
    parser.add_argument("--topic-shorts", action="store_true",
                        help="Build per-topic Shorts videos (1 news = 1 video)")
    args = parser.parse_args()
    if args.topic_shorts:
        paths = build_topic_shorts(args.date, Path(args.config))
        for p in paths:
            print(f"Output: {p}")
    else:
        path = build_video(args.date, Path(args.config), args.shorts)
        print(f"Output: {path}")


if __name__ == "__main__":
    main()
