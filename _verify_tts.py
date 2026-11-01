"""gemini_tts のユニットチェック（ネットワーク不要）. 実行: python _verify_tts.py"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str((ROOT / cfg["paths"]["video_engine_dir"]).resolve()))

import gemini_tts  # noqa: E402
import generate_video as gv  # noqa: E402

_failed = 0


def check(name: str, cond: bool) -> None:
    global _failed
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failed += 1


def _wav_bytes(sec: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * sec))
    return buf.getvalue()


def _ok_response(audio: bytes) -> httpx.Response:
    data = base64.b64encode(audio).decode()
    return httpx.Response(200, json={"id": "x", "status": "completed",
                                     "steps": [{"content": [{"type": "audio", "data": data}]}]})


# --- cache_key ---
k1 = gemini_tts.cache_key("m", "Kore", "s", "こんにちは")
check("cache_key: 同じ入力は同じキー", k1 == gemini_tts.cache_key("m", "Kore", "s", "こんにちは"))
check("cache_key: 声が違えば別キー", k1 != gemini_tts.cache_key("m", "Puck", "s", "こんにちは"))
check("cache_key: 文が違えば別キー", k1 != gemini_tts.cache_key("m", "Kore", "s", "こんばんは"))

# --- parse_retry_seconds ---
check("retry: 時分秒", gemini_tts.parse_retry_seconds("Please retry in 6h7m39s or upgrade") == 6 * 3600 + 7 * 60 + 39)
check("retry: 秒のみ", gemini_tts.parse_retry_seconds("Please retry in 31.5s.") == 31.5)
check("retry: retryDelay", gemini_tts.parse_retry_seconds('{"retryDelay": "17s"}') == 17.0)
check("retry: 情報なし", gemini_tts.parse_retry_seconds("You exceeded your current quota") is None)

# --- request_audio ---
wav = _wav_bytes()


def _client(responses: list[httpx.Response], seen: list) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return responses.pop(0)
    return httpx.Client(transport=httpx.MockTransport(handler))


seen: list = []
out = gemini_tts.request_audio("本文", model="m", voice="Kore", style="calm", api_key="k",
                               client=_client([_ok_response(wav)], seen), sleep=lambda s: None)
check("request: 入れ子の音声データを取り出す", out == wav)
ann = seen[0]["input"][0]["content"][0]
check("request: 本文には読む文だけ", ann["text"] == "本文")
check("request: 話し方は speech_metadata.style", ann["annotations"] == [{"type": "speech_metadata", "style": "calm"}])
check("request: 声の指定", seen[0]["generation_config"]["speech_config"] == [{"voice": "Kore"}])

slept: list = []
seen = []
out = gemini_tts.request_audio(
    "本文", model="m", voice="Kore", style="s", api_key="k", sleep=slept.append,
    client=_client([httpx.Response(429, text="Please retry in 20s."), _ok_response(wav)], seen))
check("request: 1分の制限は待って再試行", out == wav and len(seen) == 2 and slept == [21.0])

try:
    gemini_tts.request_audio(
        "本文", model="m", voice="Kore", style="s", api_key="k", sleep=lambda s: None,
        client=_client([httpx.Response(429, text="limit: 10 requests per day on Free Tier. Please retry in 6h7m39s")], []))
    check("request: 1日の上限は即停止", False)
except gemini_tts.GeminiQuotaExceeded as e:
    check("request: 1日の上限は即停止", "voicevox" in str(e))

seen = []
try:
    gemini_tts.request_audio("本文", model="m", voice="Kore", style="s", api_key="k", sleep=lambda s: None,
                             client=_client([httpx.Response(400, text="API key not valid")], seen))
    check("request: 400 は再試行せず失敗", False)
except gemini_tts.GeminiQuotaExceeded:
    check("request: 400 は再試行せず失敗", False)
except RuntimeError:
    check("request: 400 は再試行せず失敗", len(seen) == 1)

seen = []
try:
    gemini_tts.request_audio("本文", model="m", voice="Kore", style="s", api_key="k", sleep=lambda s: None,
                             client=_client([httpx.Response(503, text="busy")] * 5, seen))
    check("request: 5xx は上限回数で失敗", False)
except RuntimeError:
    check("request: 5xx は上限回数で失敗", len(seen) == 5)

_net_calls: list = []


def _flaky(req: httpx.Request) -> httpx.Response:
    _net_calls.append(1)
    if len(_net_calls) == 1:
        raise httpx.ConnectError("boom", request=req)
    return _ok_response(wav)


out = gemini_tts.request_audio("本文", model="m", voice="Kore", style="s", api_key="k", sleep=lambda s: None,
                               client=httpx.Client(transport=httpx.MockTransport(_flaky)))
check("request: 通信エラーは再試行して回復", out == wav and len(_net_calls) == 2)


def _down(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timeout", request=req)


try:
    gemini_tts.request_audio("本文", model="m", voice="Kore", style="s", api_key="k", sleep=lambda s: None,
                             client=httpx.Client(transport=httpx.MockTransport(_down)))
    check("request: 通信エラーが続けば失敗", False)
except RuntimeError as e:
    check("request: 通信エラーが続けば失敗", "接続できません" in str(e))

# --- register / synthesize（キャッシュ） ---
gemini_tts.register(gv)
gemini_tts.register(gv)
check("register: gemini が登録される", gv._TTS_PROVIDERS.get("gemini") is not None)
check("register: get_tts_provider で取得できる", isinstance(gv.get_tts_provider("gemini"), gv.TTSProvider))

calls: list = []
orig_request, orig_key = gemini_tts.request_audio, gemini_tts.resolve_gemini_api_key
gemini_tts.request_audio = lambda text, **kw: (calls.append((text, kw)), wav)[1]
gemini_tts.resolve_gemini_api_key = lambda: "k"
try:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        conf = SimpleNamespace(tts_options={"model": "m", "voice": "Kore", "style": "s",
                                            "cache_dir": str(td / "cache")})
        tts = gv.get_tts_provider("gemini")
        tts.synthesize("ポンテだよ", td / "a" / "slide_01.mp3", conf)
        check("synthesize: MP3 を出力", (td / "a" / "slide_01.mp3").stat().st_size > 0)
        check("synthesize: 生データを残さない", not list((td / "a").glob("*.raw")))
        tts.synthesize("ポンテだよ", td / "b" / "slide_01.mp3", conf)
        check("synthesize: 同じ文はキャッシュ再利用（API 1回）", len(calls) == 1 and (td / "b" / "slide_01.mp3").exists())
        tts.synthesize("ポンテです", td / "b" / "slide_02.mp3", conf)
        check("synthesize: 文が変われば API を呼ぶ", len(calls) == 2)
        check("synthesize: config の声・話し方を渡す", calls[0][1] == {"model": "m", "voice": "Kore", "style": "s", "api_key": "k"})
finally:
    gemini_tts.request_audio, gemini_tts.resolve_gemini_api_key = orig_request, orig_key

# --- check_api_key ---
try:
    gemini_tts.resolve_gemini_api_key = lambda: "k"
    gemini_tts.check_api_key(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    check("check_api_key: 200 は通る", True)
    try:
        gemini_tts.check_api_key(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))))
        check("check_api_key: 無効なキーで停止", False)
    except RuntimeError:
        check("check_api_key: 無効なキーで停止", True)
finally:
    gemini_tts.resolve_gemini_api_key = orig_key

# --- QA2 で追加: ミリ秒・キーのマスク・壊れたキャッシュ ---
check("retry: ミリ秒を分と取り違えない", gemini_tts.parse_retry_seconds("retry in 500ms") is None)
try:
    gemini_tts.request_audio("本文", model="m", voice="Kore", style="s", api_key="SECRETKEY123",
                             sleep=lambda s: None,
                             client=_client([httpx.Response(400, text="bad key SECRETKEY123")], []))
except RuntimeError as e:
    check("request: エラー文にキーを出さない", "SECRETKEY123" not in str(e))

gemini_tts.request_audio = lambda text, **kw: (calls.append((text, kw)), wav)[1]
gemini_tts.resolve_gemini_api_key = lambda: "k"
try:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        conf = SimpleNamespace(tts_options={"model": "m", "voice": "Kore", "style": "s",
                                            "cache_dir": str(td / "cache")})
        tts = gv.get_tts_provider("gemini")
        tts.synthesize("ポンテだよ", td / "a.mp3", conf)
        cached = next((td / "cache").glob("*.mp3"))
        before = len(calls)
        for bad in (b"", b"garbage" * 10):
            cached.write_bytes(bad)
            tts.synthesize("ポンテだよ", td / "b.mp3", conf)
        check("synthesize: 壊れたキャッシュ（空・不正）は作り直す", len(calls) == before + 2 and gemini_tts._is_playable(cached))
        check("synthesize: 一時ファイルを残さない", not list((td / "cache").glob("*.tmp")))
finally:
    gemini_tts.request_audio, gemini_tts.resolve_gemini_api_key = orig_request, orig_key

# --- config ---
check("config: provider=gemini / voice=Kore", cfg["tts"]["provider"] == "gemini" and cfg["tts"]["options"]["voice"] == "Kore")
check("config: VOICEVOX の設定を戻し用に保持", cfg["tts"]["options"].get("speaker") == 14)

print("\n=== ALL TTS CHECKS PASSED ===" if not _failed else f"\n=== {_failed} TTS CHECK(S) FAILED ===")
sys.exit(1 if _failed else 0)
