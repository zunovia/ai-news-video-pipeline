# Custom Topic News YouTube Video Pipeline

Generate YouTube news videos on any topic you choose (e.g. cars, space, gaming, crypto).

**Format (updated 2026-07-16):** Per-topic Shorts only — one 9:16 video per article (top 3, 40-75s, hook titles, Ken Burns). The 16:9 digest is disabled via `config.json` → `pipeline.daily_video_enabled: false`. Target: AI-beginner adults. See `/ai-news` for the config quick reference.

The user provides the topic as an argument: `/custom-news 車` or `/custom-news 宇宙`

## Instructions

All commands MUST run with working directory `C:/02.Claude_Agent/912-plan/`.

### Step 0: Parse topic

The user's argument is the topic keyword. If no argument is provided, ask the user what topic they want.

Store the topic as TOPIC for all subsequent steps.

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
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --skip-upload --force --topic "TOPIC"
```

This runs: Google CSE news collection for the specified topic, script generation, Shorts slide generation, and per-topic video build (Ken Burns motion). Phases: collect → script → slides_shorts → video_shorts. This step takes 10-20 minutes. If it fails, show the full error output and stop.

Notes:
- Past 7 days' news are automatically deduplicated (same URLs are excluded).
- Cross-outlet duplicate stories are clustered by LLM and only the best one per story is selected. Max 2 articles per source.
- Article full text is fetched and summarized as script material. Each Short is intro+ポイント only, est. 40-75s.
- Script fields per article: hook_title (video title) / intro (hook-first) / explanation / insight (ポンテの考察).

### Step 4: Show script for review

Read and display the generated script: `C:/02.Claude_Agent/912-plan/state/script_DATE.md`

**IMPORTANT — Check script source**: Read `C:/02.Claude_Agent/912-plan/state/script_meta_DATE.json` and verify `"source": "llm"`. If it says `"fallback"`, show the `error` field and offer to re-run with `--from script`.

**IMPORTANT — Check hook titles (added 2026-10-03)**: Print every `articles[].hook_title` in `script_meta_DATE.json` (use `PYTHONIOENCODING=utf-8`; Windows console garbles Japanese otherwise). Flag any title that ends mid-word (e.g. `…で首`, `…立ち上`) or isn't a complete statement, and propose a fixed version ≤26 chars (`_HOOK_TITLE_MAX`). Overlong LLM titles are retried since 2026-10-03, but the final fallback truncation can still yield noun-ending fragments.

**IMPORTANT — Fact-check the script against the source**: Compare each topic's serif with `summary_ja_long` / `facts` in `state/news_DATE.json`. Flag claims not in the source or that contradict it (e.g. 2026-10-03: script said a model was cheap "今のうちなら" and companies would switch, while the source said it was limited-release only; company name "HENNGE" vs actual new company "HENNGE AI"). Present title + content fixes together with options (A: fix all and rebuild / B: titles only / C: upload as-is).

**IMPORTANT — Check on-screen points (added 2026-10-04)**: Each article in `script_meta_DATE.json` now has `points` (3 short bullets ≤20 chars) shown as numbered cards on the ポイント slide; spoken text is burned in as synced ruby captions (`shorts.burn_captions: true`, rollback `false`). Fact-check `points` against `summary_ja_long` / `facts` like the serif; fix by editing `articles[].points` in script_meta, then `--from slides`. Empty `points` → article image shown instead.

**Applying fixes**: First back up to `state/old/` (`script_DATE.pre_edit.md`, `script_meta_DATE.pre_edit.json`). Edit hook titles directly in `state/script_meta_DATE.json` — safe with `--from slides` because script_meta is only written in the script phase. Edit serif text in `state/script_DATE.md`, then run the `--from slides` command from Step 5 (captions are regenerated too). Before upload, preview final titles with `PYTHONIOENCODING=utf-8 python _preview_upload_meta.py DATE`.

### Step 5: Ruby (furigana) review

Check the script for ruby annotations. The script uses these formats:
- `漢字(ふりがな)` or `漢字（ふりがな）` — kanji reading
- `漢字+送り仮名(読み)` — kanji with okurigana (e.g. `挑む(いどむ)`)
- `English(カタカナ)` or `English(ひらがな)` — English word reading

Ask the user if they want to fix any ruby readings. If yes, edit the script file and re-run:
```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --skip-upload --from slides --topic "TOPIC"
```

Ruby edits in both `### スライドN：タイトル` and `**セリフ**` lines are reflected on slides. Title ruby is inherited from serif ruby if not explicitly set.

### Step 6: Show YouTube metadata preview

Display the upload settings applied to EACH per-topic Short:
- Topic: TOPIC
- Title: `{hook_title} #ai #news #shorts #short #{エンティティ}` (hook_title from `state/script_meta_DATE.json`; fixed 4 hashtags + up to 1 auto-extracted search entity like #ChatGPT; fixed 4 only if none. Note: #TOPICニュース hashtag no longer in title — spec changed 2026-07-24, shared with ai-news)
- Description: hook + summary + 元記事 link + site link, ending with `運営： https://surc.online/` (VOICEVOX credit line and trailing hashtag line removed 2026-07-24 by user decision)
- Tags: TOPIC + adult-beginner set (no 小学生/中学生)
- Language: ja / Audience: Not made for kids / Privacy: private (publish manually after review)
- Playlist: 今日のTOPICニュース（初心者向け）

### Step 7: Show output file paths

List the generated files:
- Topic Shorts: `C:/02.Claude_Agent/912-plan/output/DATE_shorts_topic1.mp4` … `_topic3.mp4`

Verify the video files exist (each 40-75s). If missing, warn the user.

### Step 8: Ask for upload approval

Ask: "Upload all N topic Shorts to YouTube? (yes/no)"

Do NOT upload without explicit user approval.

### Step 9: Upload (if approved)

```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --step upload_shorts --topic "TOPIC"
```

This uploads ALL `DATE_shorts_topic*.mp4` files with per-article hook titles.

Show the upload results (video URLs). If an upload fails, show the error and ask the user how to proceed.

## Examples

```
/custom-news 車
/custom-news 宇宙
/custom-news ゲーム
/custom-news 仮想通貨
/custom-news プログラミング教育
```

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
| `--shorts` | Generate both regular and Shorts videos |
| `--skip-upload` | Skip YouTube upload (for review first) |
| `--date YYYYMMDD` | Specify date (default: today) |
| `--topic "KEYWORD"` | Custom news topic |
| `--step PHASE` | Run a single phase only |
| `--from PHASE` | Reset and re-run from a specific phase onward (e.g. `--from slides`) |
