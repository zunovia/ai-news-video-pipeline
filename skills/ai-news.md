# AI News YouTube Video Pipeline

Automate the full pipeline: news collection, script generation, slide creation, video build, and YouTube upload.

**Format (updated 2026-07-16):** The pipeline now produces **per-topic Shorts only** — one 9:16 video per news article (top 3 articles/day, 40-75s each, Ken Burns motion, hook titles). The 16:9 daily digest is DISABLED (`config.json` → `pipeline.daily_video_enabled: false`). Target audience: **AI-beginner adults** (ポンテ character and friendly tone retained). Rollback: set `pipeline.daily_video_enabled: true` + `shorts.per_topic: false` for the old digest behavior.

## Instructions

All commands MUST run with working directory `C:/02.Claude_Agent/912-plan/`.

### Step 1: Check VOICEVOX (auto-start if needed)

Run: `curl -s http://localhost:50021/version`

If VOICEVOX is not running (curl fails or no response), start it headless via PowerShell:
```powershell
Start-Process -FilePath "C:\Users\zunov\AppData\Local\Programs\VOICEVOX\vv-engine\run.exe" -ArgumentList "--host","127.0.0.1","--port","50021" -WindowStyle Hidden
```
Then wait ~15 seconds and re-check `curl -s http://localhost:50021/version` (retry up to 3 times).
Only if it still fails after retries, tell the user it could not be started and STOP.

### Step 2: Determine date

If the user specified a date, use it in YYYYMMDD format.
Otherwise, get today's date in JST (OS-timezone independent):
```
python -c "from datetime import datetime, timedelta, timezone; print(datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d'))"
```
Store the result as DATE for all subsequent steps.
Note: `--date` can also be omitted entirely — `orchestrator.py` defaults to today in JST (`today_jst()`).

### Step 3: Run pipeline (without upload)

```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --skip-upload --force
```

This runs: news collection, script generation, Shorts slide generation, and per-topic video build (with Ken Burns motion clips). Phases: collect → script → slides_shorts → video_shorts. This step takes 10-20 minutes. If it fails, show the full error output and stop.

Notes:
- Past 7 days' news are automatically deduplicated (same URLs are excluded).
- Cross-outlet duplicate stories are clustered by LLM and only the best one per story is selected (`news.topic_dedup` in config.json). Max 2 articles per source.
- Article full text is fetched and summarized (summary_ja_long / facts / kid_angle=生活・仕事との関係) as script material.
- The script has per-article: `hook_title` (video title, saved to script_meta), `intro` (hook-first, no greeting), `explanation`, and `insight` (ポンテの考察 — unique perspective, appended to the ポイント slide serif). Length caps are enforced by LLM retry + sentence-level trimming (quote-bracket balance is preserved).
- Per-topic duration: each video is intro+ポイント only (no opening/index/keywords/ending), est. 40-75s (`shorts.max_topic_duration_sec`).
- `shorts.topics_per_day` (default 3) controls how many articles become videos (top-scored first).

### Step 4: Show script for review

Read and display the generated script: `C:/02.Claude_Agent/912-plan/state/script_DATE.md`

**IMPORTANT — Check script source**: Read `C:/02.Claude_Agent/912-plan/state/script_meta_DATE.json` and verify `"source": "llm"`. If it says `"fallback"`, the LLM script generation failed (canned template phrases were used). Show the `error` field to the user and offer to re-run: `python orchestrator.py --date DATE --shorts --skip-upload --from script`

**IMPORTANT — Check hook titles (added 2026-10-03)**: Print every `articles[].hook_title` in `script_meta_DATE.json` (use `PYTHONIOENCODING=utf-8`; Windows console garbles Japanese otherwise). Flag any title that ends mid-word (e.g. `…で首`, `…立ち上`) or isn't a complete statement, and propose a fixed version ≤26 chars (`_HOOK_TITLE_MAX`). Overlong LLM titles are retried since 2026-10-03, but the final fallback truncation can still yield noun-ending fragments.

**IMPORTANT — Fact-check the script against the source**: Compare each topic's serif with `summary_ja_long` / `facts` in `state/news_DATE.json`. Flag claims not in the source or that contradict it (e.g. 2026-10-03: script said a model was cheap "今のうちなら" and companies would switch, while the source said it was limited-release only; company name "HENNGE" vs actual new company "HENNGE AI"). Present title + content fixes together with options (A: fix all and rebuild / B: titles only / C: upload as-is).

**IMPORTANT — Check on-screen points (added 2026-10-04)**: Each article in `script_meta_DATE.json` now has `points` (3 short bullets ≤20 chars) shown as numbered cards on the ポイント slide. They are displayed in large type, so fact-check them against `summary_ja_long` / `facts` just like the serif (no exaggeration, no claims missing from the source). Fix by editing `articles[].points` in `script_meta_DATE.json` (same as hook titles), then `--from slides`. Empty `points` → the slide shows the article image instead (not an error).

**Applying fixes**: First back up to `state/old/` (`script_DATE.pre_edit.md`, `script_meta_DATE.pre_edit.json`). Edit hook titles directly in `state/script_meta_DATE.json` — safe with `--from slides` because script_meta is only written in the script phase. Edit serif text in `state/script_DATE.md`, then run the `--from slides` command from Step 5 (captions are regenerated too). Before upload, preview final titles with `PYTHONIOENCODING=utf-8 python _preview_upload_meta.py DATE`.

### Step 5: Ruby (furigana) review

Check the script for ruby annotations. The script uses these formats:
- `漢字(ふりがな)` or `漢字（ふりがな）` — kanji reading
- `漢字+送り仮名(読み)` — kanji with okurigana (e.g. `挑む(いどむ)`)
- `English(カタカナ)` or `English(ひらがな)` — English word reading

Ask the user if they want to fix any ruby readings. If yes, edit the script file and re-run:
```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --skip-upload --from slides
```

Ruby edits in both `### スライドN：タイトル` and `**セリフ**` lines are reflected on slides. Title ruby is inherited from serif ruby if not explicitly set.

### Step 6: Show YouTube metadata preview

Display the upload settings that will be applied to EACH per-topic Short:
- Title: `{hook_title} #ai #news #shorts #short #{エンティティ}` — hook_title comes from `state/script_meta_DATE.json` (LLM-generated hook). Fixed 4 hashtags (#ai #news #shorts #short) + up to 1 **search-volume entity** (#ChatGPT / #OpenAI / #Spotify etc.) auto-extracted from the article via `youtube_uploader.extract_entity_keywords()` (known-entity dictionary `_KNOWN_ENTITIES`; GPT-x models normalize to GPT). If no entity found, fixed 4 only. (Spec changed 2026-07-24 per user request.)
- Description: hook + article summary (sentence-boundary truncated) + 元記事 link + site link. Ends with `運営： https://surc.online/` — NO VOICEVOX credit line and NO trailing hashtag line (both removed 2026-07-24 by user decision; user accepted the VOICEVOX credit license risk after being warned)
- Tags: entities + Japanese genre words (生成AI/ロボット等, `_JA_TOPIC_TAGS`) first, then adult-beginner base set. Max 15. NO 小学生/中学生
- To add a new searchable entity (new AI product名 etc.), edit `_KNOWN_ENTITIES` in `youtube_uploader.py`
- Language: Japanese (ja) / Audience: Not made for kids / Altered content: No
- Privacy: private (publish manually after review)
- Playlist: 今日のAIニュース（初心者向け）
- No custom thumbnail for Shorts (feed doesn't show them)
- **Captions (自動字幕)**: `state/captions/DATE_topic{i}.srt` is auto-generated during video build (`video_builder.generate_topic_captions` — exact script text + TTS audio durations, English words kept as-is) and auto-attached after upload via captions.insert (`youtube_uploader.upload_caption`, requires youtube.force-ssl scope — already authorized 2026-07-17). Caption failures are non-fatal (upload still succeeds; check `caption_set` in upload_log.json)

Show the actual titles by reading `script_meta_DATE.json` articles[].hook_title (first `shorts.topics_per_day` entries).

### Step 7: Show output file paths

List the generated files:
- Topic Shorts: `C:/02.Claude_Agent/912-plan/output/DATE_shorts_topic1.mp4` … `_topic3.mp4` (one per article, importance order)
- Slides: `C:/02.Claude_Agent/912-plan/state/images_shorts/`

Verify the video files exist and each is under 3 minutes (typically 40-75s). If missing, warn the user. Upload results are appended to `state/upload_log.json` after each upload (kind: `shorts_topic1` etc.).

Note (2026-10-04 layout): with `shorts.burn_captions: true`, slides no longer show the full serif. Intro slide = article image + big hook_title + small 元記事 title; ポイント slide = hook strip + 3 point cards. The spoken text is burned in as a ruby-annotated caption card (one sentence/phrase at a time, synced to the TTS audio) at the bottom-left beside Ponte. To review, extract a frame: `ffmpeg -ss 3 -i output/DATE_shorts_topic1.mp4 -frames:v 1 frame.png`. Rollback to the old serif-card layout: set `shorts.burn_captions: false` (old template also archived at `templates/old/pre_captions_20261004/`).

Note: Ken Burns motion (slow zoom on every slide) is controlled by `video.motion.enabled` in config.json (currently **enabled** — required as "dynamic visual" defense against YouTube's inauthentic-content policy). If video build problems occur, set it to `false` to fall back to static slides.

### Step 8: Ask for upload approval

Ask: "Upload all N topic Shorts to YouTube? (yes/no)"

Do NOT upload without explicit user approval.

### Step 9: Upload (if approved)

```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --step upload_shorts
```

This uploads ALL `DATE_shorts_topic*.mp4` files with per-article hook titles (implemented in `youtube_uploader.upload_topic_shorts()`).

Show the upload results (video URLs). If an upload fails, show the error and ask the user how to proceed.

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
| `tts.credit` | `VOICEVOX:冥鳴ひまり` | Credit line — since 2026-07-24 only used by the disabled 16:9 daily / digest-Shorts descriptions (removed from per-topic Shorts by user decision) |
