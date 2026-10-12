# YouTube Upload Manager

Manage and execute YouTube video uploads. Preview metadata, adjust settings, and upload videos.

Arguments: `[DATE] [OPTIONS]`
- DATE: YYYYMMDD format (default: today)
- OPTIONS: `--shorts-only`, `--regular-only`, `--preview-only`, `--topic "KEYWORD"`

## Instructions

All commands MUST run with working directory `C:/02.Claude_Agent/912-plan/`.

### Step 1: Determine date and options

Parse user arguments:
- If a date (YYYYMMDD) is provided, use it as DATE
- If no date, get today's date: `python -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d'))"`
- Check for options: `--shorts-only`, `--regular-only`, `--preview-only`, `--topic`

### Step 2: Verify video files exist

Check the following files exist:
```
ls -la C:/02.Claude_Agent/912-plan/output/DATE_daily.mp4
ls -la C:/02.Claude_Agent/912-plan/output/DATE_shorts.mp4
```

Report file sizes. If neither file exists, tell the user:
"No video files found for DATE. Run `/ai-news` or `/custom-news` first to generate videos."
Then STOP.

### Step 3: Load news data and generate metadata preview

Run the following Python to preview metadata for both regular and Shorts:

```python
cd C:/02.Claude_Agent/912-plan && python -c "
import json
from youtube_uploader import load_config, generate_metadata, generate_shorts_metadata
from pathlib import Path

config = load_config()
date_str = 'DATE'
topic = 'TOPIC_OR_EMPTY'

news_data = None
news_path = Path(f'state/news_{date_str}.json')
if news_path.exists():
    with open(news_path, encoding='utf-8') as f:
        news_data = json.load(f)

print('=== Regular Video Metadata ===')
meta = generate_metadata(date_str, config, 'daily', news_data, topic=topic)
for k, v in meta.items():
    if k == 'description':
        print(f'{k}:')
        for line in str(v).split(chr(10)):
            print(f'  {line}')
    else:
        print(f'{k}: {v}')

print()
print('=== Shorts Video Metadata ===')
meta_s = generate_shorts_metadata(date_str, config, news_data, topic=topic)
for k, v in meta_s.items():
    if k == 'description':
        print(f'{k}:')
        for line in str(v).split(chr(10)):
            print(f'  {line}')
    else:
        print(f'{k}: {v}')
"
```

### Step 4: Show upload settings summary

Display all upload settings in a clear table:

| Setting | Value |
|---|---|
| Title & Description Language | Japanese (ja) |
| Audio Language | Japanese (ja) |
| Audience | Not made for kids |
| Altered Content | No (containsSyntheticMedia: false) |
| Recording Date | DATE |
| Privacy | private (from config) |
| Playlist | (from metadata) |
| Title hashtags | #ai #aiニュース #初心者 #ポンテのAIニュース |

### Step 5: Ask user for changes

Ask: "Do you want to change any settings before uploading?"

Possible changes the user may request:
- **Privacy**: Change to `public`, `unlisted`, or `private`
  - Modify config.json `youtube.initial_privacy` value
- **Title**: Edit the title text
  - Note: title is auto-generated, manual edits apply to this upload only
- **Tags**: Add or remove tags
  - Modify config.json `youtube.default_tags` array
- **Playlist**: Change playlist name
- **Category**: Change category ID (default: 28 = Science & Technology)
  - Common IDs: 22=People & Blogs, 24=Entertainment, 25=News, 27=Education, 28=Science & Technology

If the user says no changes, proceed to Step 6.
If `--preview-only` was specified, STOP here.

### Step 6: Ask for upload approval

Ask: "Ready to upload. Which videos should I upload?"

Options:
1. Both (regular + Shorts)
2. Regular only
3. Shorts only
4. Cancel

If `--shorts-only` was specified, skip regular. If `--regular-only`, skip Shorts.

### Step 7: Execute upload

For regular video:
```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --shorts --step upload --topic "TOPIC"
```

For Shorts video:
```
cd C:/02.Claude_Agent/912-plan && python orchestrator.py --date DATE --shorts --step upload_shorts --topic "TOPIC"
```

Show the returned video URL after each upload.

### Step 8: Post-upload summary

Display:
- Regular video URL (if uploaded)
- Shorts video URL (if uploaded)
- Playlist assigned
- Privacy status
- Reminder: "To change privacy to public, update in YouTube Studio or change `initial_privacy` in config.json and re-upload."

## Quick commands

```
/youtube-upload                          # Upload today's videos
/youtube-upload 20260514                 # Upload specific date
/youtube-upload --preview-only           # Preview metadata only, no upload
/youtube-upload --shorts-only            # Upload Shorts only
/youtube-upload --regular-only           # Upload regular video only
/youtube-upload 20260514 --topic 車      # Upload with custom topic metadata
```

## Config reference

YouTube settings in `C:/02.Claude_Agent/912-plan/config.json`:

```json
"youtube": {
    "client_secrets_file": "client_secret.json",
    "token_file": "state/youtube_token.json",
    "default_category_id": "28",
    "default_tags": ["AI", "人工知能", "AIニュース", "ポンテ", "AI教室", "小学生", "中学生"],
    "initial_privacy": "private"
}
```

## Upload settings (auto-applied via API)

| Setting | API Field | Value |
|---|---|---|
| Title & Description Language | snippet.defaultLanguage | ja |
| Audio Language | snippet.defaultAudioLanguage | ja |
| Audience | status.selfDeclaredMadeForKids | false |
| Altered Content | status.containsSyntheticMedia | false |
| Recording Date | recordingDetails.recordingDate | auto from date |
| Title hashtags | (appended to title) | #ai #aiニュース #初心者 #ポンテのAIニュース |
| Caption Certification | N/A | Set in YouTube Studio defaults |

## Troubleshooting

- **Token expired**: Delete `state/youtube_token.json` and re-run. Browser auth will open.
- **Scope error**: Delete `state/youtube_token.json`. Scopes include `youtube.upload` + `youtube` (for playlists).
- **Upload stuck**: Check internet connection. Uploads use 10MB chunks with resumable upload.
- **Wrong metadata**: Use `--preview-only` first to verify before uploading.
