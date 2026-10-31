# AI News YouTube Video Pipeline

Automate the full pipeline: news collection, script generation, slide creation, video build, and YouTube upload.

**Format (updated 2026-07-16):** The pipeline now produces **per-topic Shorts only** — one 9:16 video per news article (top 3 articles/day, 40-75s each, Ken Burns motion, hook titles). The 16:9 daily digest is DISABLED (`config.json` → `pipeline.daily_video_enabled: false`). Target audience: **AI-beginner adults** (ポンテ character and friendly tone retained). Rollback: set `pipeline.daily_video_enabled: true` + `shorts.per_topic: false` for the old digest behavior.

## Instructions

All commands MUST run with working directory `C:/02.Claude_Agent/912-plan/`.

**Build flow (updated 2026-10-23):** the pipeline now self-checks before you review. Script is generated only for the top N articles → only the broken fields are repaired (no full regeneration) → a separate fact-check LLM compares title/serif/points with the source and auto-fixes clear errors → videos are built → `state/review_DATE.md` collects everything you need to review in one file → `upload_shorts` refuses to upload if the preflight has a FAIL. Your job is to read the review file, decide fixes, and apply them with one command.

### Step 1: Determine date

If the user specified a date, use it in YYYYMMDD format.
Otherwise, get today's date in JST (OS-timezone independent):
```
python -c "from datetime import datetime, timedelta, timezone; print(datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d'))"
```
Store the result as DATE for all subsequent steps.
Note: `--date` can also be omitted entirely — `orchestrator.py` defaults to today in JST (`today_jst()`).

### Step 2: Run pipeline (without upload)

```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --skip-upload --force
```

Run it in the background (10-20 minutes). Phases: collect → script → slides_shorts → video_shorts. If it fails, show the full error output and stop.

What happens automatically (no manual step needed):
- **VOICEVOX** is started headless if it is not running (`orchestrator.ensure_voicevox`; engine path `tts.options.engine_path`, default `C:\Users\zunov\AppData\Local\Programs\VOICEVOX\vv-engine\run.exe`). Only if the pipeline fails with "VOICEVOX did not start", start it manually: `Start-Process -FilePath "<engine>" -ArgumentList "--host","127.0.0.1","--port","50021" -WindowStyle Hidden`.
- News: past 7 days deduplicated (also checks `state/old`), cross-outlet duplicates clustered, max 2 per source, full text summarized into `summary_ja_long` / `facts`.
- Script: only the top `shorts.topics_per_day` articles. Length/format problems (title 15-24 chars, no reading parentheses in titles, exactly 3 points, serif caps) are repaired **field by field**; a repair is only accepted if it does not make that article worse.
- Fact check (`script.factcheck`): a separate verifier (temperature 0) compares hook_title / intro / explanation / insight / points with `summary_ja_long` / `facts`. Clear errors (`severity: error`) with a valid fix are applied and re-checked (max 2 rounds); `minor` nitpicks are ignored on purpose (fixing them made titles vague — 2026-10-23). Results go to `script_meta_DATE.json` → `factcheck` (`ok` / `fixed` / `unresolved` / `error` / `skipped`).
- Review file: `state/review_DATE.md` + preflight summary in `state/pipeline_DATE.json` → `preflight`.

### Step 3: Review — read `state/review_DATE.md`

Read `C:/02.Claude_Agent/912-plan/state/review_DATE.md` (one file: YouTube title per topic, on-screen points, serif, source summary + facts, fact-check fixes and 要確認 items, video length, caption status, preflight results). Regenerate it any time with `PYTHONIOENCODING=utf-8 python review_report.py DATE`.

Then report to the user:
1. **Preflight**: list every FAIL (blocks upload) and WARN. Typical WARNs: `元記事にない数字` (a number in title/points/serif that is not in the source — check it), `題名が途中で切れている疑い`, `画面の要点が3個でない`, `事実確認の未解決`, `金額の桁ずれの疑い（英語原文と比較）` (a dollar amount that is exactly 10/100/1000x smaller than the English original, e.g. `$740 billion` written as `740億ドル` — always fix it; since 2026-11-01 the collector auto-corrects these in summaries/facts and lists them in the review file as `金額の桁を英語原文に合わせて自動修正`).
2. **Fact-check result**: show what was auto-fixed (before → after) so the user knows the titles changed, and every 要確認 item with its suggestion.
3. **Your own check** — the verifier is an LLM and can miss things, so still read each topic's title / points / serif against the source facts yourself. Watch for: **hook titles that end mid-word or are not a complete statement** (e.g. `…で首`, `…立ち上`; the preflight only catches particle endings — propose a fixed version ≤26 chars), number-target swaps ("500万行を分析し493件" → "500万行のバグ"), research results turned into "治療法" or "実用化", ranges stated as a single value ("数カ月〜1年" → "1年"), limited releases described as generally available, wrong company names, accounting terms mistranslated (negative free cash flow / spending called `赤字`), group totals stated as one company's figure. For large dollar amounts (≥100億ドル) open the English original (WebFetch) and check billion→億 (1 billion = 10億).
4. Present the fixes together with options (A: fix all and rebuild / B: titles only / C: upload as-is).

If `script_meta_DATE.json` has `"source": "fallback"`, the LLM script failed (canned phrases) — show its `error` and offer `python orchestrator.py --date DATE --skip-upload --from script`.

### Step 4: Apply fixes (one command)

Write the fixes to a JSON file in the scratchpad, then run `review_apply.py` — it validates everything first (nothing is written if any item is invalid), backs up to `state/old/*.pre_edit_HHMMSS.*`, applies, rebuilds with `--from slides` (slides, videos, captions), and prints the new review/preflight:

```json
{
  "titles": {"2": "Claudeが隠れた弱点493件を発見"},
  "points": {"1": ["数千AIが脳細胞データを32分で分析", "人間なら数カ月から1年の作業", "アルツハイマー病などの研究に"]},
  "serif":  [{"old": "人間(にんげん)なら1年かかる", "new": "人間なら数カ月から1年かかる"}]
}
```
```
cd C:/02.Claude_Agent/912-plan && PYTHONIOENCODING=utf-8 python review_apply.py DATE <fixes.json>
```
- `titles` / `points`: keys are topic numbers (1-based). Reading parentheses are stripped automatically; titles must be ≤26 chars.
- `serif.old` must appear **exactly once** in `state/script_DATE.md` — copy it from the file including its ruby, e.g. `人間(にんげん)なら1年`. `serif.new` can be written without kanji ruby (added automatically); keep English readings like `OpenAI(オープンエーアイ)` if you write English words.
- Exit code 2 = fixes rejected (message says why, files untouched); 1 = rebuilt but preflight still has a FAIL.

### Step 5: Ruby (furigana) review

Check the serif ruby in the review file. Formats: `漢字(ふりがな)`, `挑む(いどむ)`, `English(カタカナ)`. If the user wants a reading fixed, use the same `serif` old/new mechanism in Step 4 (write the corrected ruby explicitly in `new`, e.g. `"new": "脳(のう)データ"` — written ruby is kept as-is, only unannotated kanji get auto ruby), or edit `state/script_DATE.md` and run `python orchestrator.py --date DATE --skip-upload --from slides`. Ruby edits in `**セリフ**` lines are reflected on slides and burned-in captions; titles inherit serif ruby.

### Step 6: YouTube settings (for reference)

Applied to EACH per-topic Short (the actual titles are already in the review file):
- Title: `{hook_title} #ai #news #shorts #short #{エンティティ}` — fixed 4 hashtags + up to 1 search entity (#ChatGPT / #OpenAI …) from `youtube_uploader._KNOWN_ENTITIES` (add new product names there). Reading parentheses are stripped from titles at upload as a second safeguard.
- Description: hook + summary (sentence-boundary cut) + 元記事 link + site link, ending `運営： https://surc.online/`. No VOICEVOX credit line, no trailing hashtag line (user decision 2026-07-24).
- Tags: entities + `_JA_TOPIC_TAGS` + adult-beginner base set, max 15, no 小学生/中学生.
- Language ja / Not made for kids / Altered content: No / **Privacy: private** / Playlist: 今日のAIニュース（初心者向け） / no custom thumbnail.
- Captions: `state/captions/DATE_topic{i}.srt` attached after upload (non-fatal; see `caption_set` in `state/upload_log.json`).

Output files: `output/DATE_shorts_topic1.mp4` … `_topic3.mp4` (slides in `state/images_shorts/`). To eyeball a frame: `ffmpeg -ss 3 -i output/DATE_shorts_topic1.mp4 -frames:v 1 frame.png`. Layout: intro slide = article image + big hook_title; ポイント slide = hook strip + 3 point cards; spoken text burned in as synced ruby captions (rollback: `shorts.burn_captions: false`, old template in `templates/old/pre_captions_20261004/`). Ken Burns motion (`video.motion.enabled`) stays on as the inauthentic-content defense.

### Step 7: Ask for upload approval

Ask: "Upload all N topic Shorts to YouTube? (yes/no)" — mention any remaining WARN items in the same message.

Do NOT upload without explicit user approval.

### Step 8: Upload (if approved)

```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --step upload_shorts
```

The preflight runs first; if it reports a FAIL (title with reading parentheses, missing video, over 3 minutes, title over 100 chars, empty title) the upload is aborted with the reason — fix it via Step 4 and retry. Then all `DATE_shorts_topic*.mp4` are uploaded with their hook titles (`youtube_uploader.upload_topic_shorts()`), results appended to `state/upload_log.json`.

Show the upload results (video URLs, `caption_set`). If an upload fails, show the error and ask the user how to proceed.

## Ponte character assets

Character images are in `C:/02.Claude_Agent/912-plan/assets/ponte/`.

| File | Usage | Source |
|---|---|---|
| `ponte_normal.png` | Opening, index, news slides (default) | `ponte.png` + antenna ball from `表情バリエーション.png` |
| `ponte_surprised.png` | News slides (surprising topics) | Extracted from `表情バリエーション.png` |
| `ponte_thinking.png` | News slides (analytical topics) | Extracted from `表情バリエーション.png` |
| `ponte_clumsy.png` | Ending slide | Extracted from `表情バリエーション.png` |

Source files (do not use directly in pipeline):
- `ponte.png` — Full character (normal expression), antenna ball top slightly cropped
- `表情バリエーション.png` — 4-expression sheet with complete antenna balls
- `ponte_character.png`, `ponte_body.png` etc. — Design references

**Re-extracting images:** If expression PNGs need to be regenerated, extract from `表情バリエーション.png` using flood-fill transparency (scipy.ndimage.label, edge-connected background removal). The antenna ball must include the yellow glow above it — add transparent padding (60px top) to ensure the glow is not clipped. For `ponte_normal.png`, use `ponte.png` as base and composite the complete ball from the sheet.

## Flag reference

| Flag | Purpose |
|---|---|
| `--force` | Reset all phases, re-run from scratch |
| `--shorts` | (Legacy) only relevant when `pipeline.daily_video_enabled: true`; Shorts-only mode ignores it |
| `--skip-upload` | Skip YouTube upload (for review first) |
| `--date YYYYMMDD` | Specify date (default: today) |
| `--step PHASE` | Run a single phase only (e.g. `upload_shorts`) |
| `--from PHASE` | Reset and re-run from a specific phase onward (e.g. `--from slides` re-renders shorts slides + videos) |

## Config quick reference (2026-07 revamp)

| Key | Value | Meaning |
|---|---|---|
| `pipeline.daily_video_enabled` | `false` | 16:9 daily digest disabled (rollback: `true`) |
| `shorts.per_topic` | `true` | 1 news = 1 Short (rollback: `false` = old digest Short) |
| `shorts.topics_per_day` | `3` | How many articles become Shorts |
| `shorts.max_topic_duration_sec` | `75` | Per-video duration budget (warning only) |
| `video.motion.enabled` | `true` | Ken Burns motion (inauthentic-content defense) |
| `shorts.burn_captions` | `true` | Burned-in synced captions + hook/points layout (rollback: `false`) |
| `script.factcheck.enabled` | `true` | Separate fact-check LLM after script generation (2026-10-23). `false` = skip (review file then says 記録なし) |
| `script.factcheck.auto_fix` | `true` | Apply the verifier's fixes for clear errors. `false` = report only (all findings become 要確認) |
| `tts.credit` | `VOICEVOX:冥鳴ひまり` | Credit line — since 2026-07-24 only used by the disabled 16:9 daily / digest-Shorts descriptions (removed from per-topic Shorts by user decision) |
