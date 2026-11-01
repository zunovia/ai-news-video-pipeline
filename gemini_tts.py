"""Gemini TTS プロバイダー（911-plan の動画エンジンに実行時登録する）.

911-plan の generate_video.TTSProvider を継承し、`_TTS_PROVIDERS["gemini"]` に
登録する。911-plan 側のコードは変更しない。

- Gemini API の Interactions エンドポイント（REST）を直接呼ぶ。
  インストール済みの google-genai SDK は speech_metadata 注釈を送れないため。
- 話し方は speech_metadata.style で指定する。本文に指示を書くと読み上げられる
  （2026-11-01 実測: 「明るく…読んでください」まで音声になった）。
- 同じ (model, voice, style, text) の音声はキャッシュから再利用する。
  ルビ修正後の作り直しで API 回数（無料枠は1日10回）を消費しないため。
- 1分あたりの制限は待って再試行し、1日の上限は分かりやすいエラーで止める。
  VOICEVOX への自動切り替えはしない（1本の動画で声が混ざるのを防ぐ）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from api_utils import resolve_gemini_api_key

logger = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
_DEFAULT_MODEL = "gemini-3.8-flash-tts"
_DEFAULT_VOICE = "Kore"
_DEFAULT_STYLE = "bright, friendly, upbeat news explainer, brisk pace"
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "state" / "tts_cache"
# 「retry in 6h7m39s」のような待ち時間がこれより長ければ1日の上限とみなす
_MAX_WAIT_SEC = 120
_MAX_ATTEMPTS = 5

_RETRY_IN_RE = re.compile(r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", re.I)


class GeminiQuotaExceeded(RuntimeError):
    """1日の上限など、待っても回復しないレート制限."""


def cache_key(model: str, voice: str, style: str, text: str) -> str:
    """音声キャッシュのキー（入力が同じなら同じ音声になる前提）."""
    raw = "\x1f".join([model, voice, style, text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def parse_retry_seconds(body: str) -> float | None:
    """エラー本文の待ち時間（retry in …／retryDelay）を秒で返す."""
    m = _RETRY_IN_RE.search(body)
    if m and any(m.groups()):
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body)
    if m:
        return float(m.group(1))
    return None


def _find_audio_data(obj) -> str | None:
    """Interactions の応答から base64 音声データを探す（steps の奥にある）."""
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, str) and len(data) > 1000:
            return data
        for v in obj.values():
            found = _find_audio_data(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_audio_data(v)
            if found:
                return found
    return None


def request_audio(text: str, *, model: str, voice: str, style: str,
                  api_key: str, client: httpx.Client | None = None,
                  sleep=time.sleep) -> bytes:
    """Gemini TTS を呼び、音声バイト列（WAV または 24kHz/16bit/mono PCM）を返す."""
    body = {
        "model": model,
        "input": [{"type": "user_input", "content": [{
            "type": "text",
            "text": text,
            "annotations": [{"type": "speech_metadata", "style": style}],
        }]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    own_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.post(_ENDPOINT, headers={"x-goog-api-key": api_key}, json=body)
            except httpx.TransportError as e:
                if attempt == _MAX_ATTEMPTS:
                    raise RuntimeError(f"Gemini TTS に接続できません（{_MAX_ATTEMPTS}回）: {type(e).__name__}") from e
                logger.warning("Gemini TTS 通信エラー %s — %d秒待って再試行 (%d/%d)",
                               type(e).__name__, 10 * attempt, attempt, _MAX_ATTEMPTS)
                sleep(10 * attempt)
                continue
            if resp.status_code == 200:
                data = _find_audio_data(resp.json())
                if not data:
                    raise RuntimeError("Gemini TTS: 応答に音声データがありません")
                return base64.b64decode(data)
            detail = resp.text[:300].replace(api_key, "***") if api_key else resp.text[:300]
            if resp.status_code == 429:
                wait = parse_retry_seconds(resp.text)
                if wait is not None and wait > _MAX_WAIT_SEC:
                    raise GeminiQuotaExceeded(
                        f"Gemini TTS の上限に達しました（再開まで約{wait / 3600:.1f}時間）。"
                        "課金を有効にするか、config.json の tts.provider を \"voicevox\" に戻してください。"
                        f" 詳細: {detail}")
                wait = wait if wait is not None else 30.0
            elif resp.status_code in (500, 502, 503, 504):
                wait = 10.0 * attempt
            else:
                raise RuntimeError(f"Gemini TTS が失敗しました（HTTP {resp.status_code}）: {detail}")
            if attempt == _MAX_ATTEMPTS:
                raise RuntimeError(f"Gemini TTS が{_MAX_ATTEMPTS}回失敗しました（HTTP {resp.status_code}）: {detail}")
            logger.warning("Gemini TTS HTTP %d — %.0f秒待って再試行 (%d/%d)",
                           resp.status_code, wait + 1, attempt, _MAX_ATTEMPTS)
            sleep(wait + 1)
    finally:
        if own_client:
            client.close()
    raise RuntimeError("unreachable")


def check_api_key(client: httpx.Client | None = None) -> None:
    """キーが有効か、音声を作る前に確かめる（モデル一覧の取得なので TTS の回数は使わない）."""
    own_client = client is None
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.get("https://generativelanguage.googleapis.com/v1beta/models",
                          headers={"x-goog-api-key": resolve_gemini_api_key()})
    finally:
        if own_client:
            client.close()
    if resp.status_code != 200:
        raise RuntimeError(
            f"Gemini API キーが使えません（HTTP {resp.status_code}）。"
            ".gemini_api_key を確認するか、tts.provider を \"voicevox\" に戻してください。")


def _is_playable(path: Path) -> bool:
    """ffprobe で音声として読めて、長さがあるか（キャッシュの健全性チェック）."""
    if path.stat().st_size == 0:
        return False
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and float(r.stdout.strip()) > 0.1
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def make_provider_class(gv):
    """911-plan の TTSProvider を継承した GeminiTTS クラスを作る."""

    class GeminiTTS(gv.TTSProvider):
        """Gemini API の音声合成（キャッシュ付き）."""

        def synthesize(self, text: str, output_path: Path, config) -> None:
            opts = config.tts_options or {}
            model = opts.get("model", _DEFAULT_MODEL)
            voice = opts.get("voice", _DEFAULT_VOICE)
            style = opts.get("style", _DEFAULT_STYLE)
            cache_dir = Path(opts.get("cache_dir") or _DEFAULT_CACHE_DIR)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{cache_key(model, voice, style, text)}.mp3"
            output_path.parent.mkdir(parents=True, exist_ok=True)

            if cached.exists():
                if _is_playable(cached):
                    shutil.copyfile(cached, output_path)
                    logger.info("  TTS出力 (Gemini/キャッシュ): %s", output_path.name)
                    return
                # 0バイト・途中で切れたキャッシュは捨てて作り直す（残すと毎回同じ壊れた音声になる）
                logger.warning("  TTSキャッシュが壊れているため作り直します: %s", cached.name)
                cached.unlink(missing_ok=True)

            audio = request_audio(text, model=model, voice=voice, style=style,
                                  api_key=resolve_gemini_api_key())
            raw = output_path.with_suffix(".gemini.raw")
            raw.write_bytes(audio)
            fmt = [] if audio[:4] == b"RIFF" else ["-f", "s16le", "-ar", "24000", "-ac", "1"]
            try:
                gv._run_ffmpeg([*fmt, "-i", str(raw), "-c:a", "libmp3lame", "-q:a", "4",
                                str(output_path)], "Gemini TTS → MP3変換")
            finally:
                raw.unlink(missing_ok=True)
            # 書き込み途中のファイルを他の実行が読まないよう、一時ファイル経由で置き換える
            tmp = cached.with_name(f"{cached.name}.{os.getpid()}.tmp")
            shutil.copyfile(output_path, tmp)
            os.replace(tmp, cached)
            logger.info("  TTS出力 (Gemini %s/%s): %s", model, voice, output_path.name)

    return GeminiTTS


def register(gv) -> None:
    """generate_video の TTS プロバイダー表に "gemini" を登録する（何度呼んでも安全）."""
    if "gemini" not in gv._TTS_PROVIDERS:
        gv._TTS_PROVIDERS["gemini"] = make_provider_class(gv)
