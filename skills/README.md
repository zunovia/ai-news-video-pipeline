# Claude Code スラッシュコマンド（スキル）定義

このフォルダは `~/.claude/commands/` に置いているスキル定義のバージョン管理用コピーです。
Claude Code はこのフォルダを直接読みません。使うときは次のようにコピーしてください。

```powershell
Copy-Item skills\*.md "$env:USERPROFILE\.claude\commands\"
```

| ファイル | 用途 |
|---|---|
| `ai-news.md` | `/ai-news` — 今日のAIニュースを収集し、1記事1本の Shorts を生成・アップロード |
| `custom-news.md` | `/custom-news` — 任意トピックを指定してニュース動画を生成 |
| `ruby-edit.md` | `/ruby-edit` — 台本のルビ（ふりがな）編集 |
| `youtube-upload.md` | `/youtube-upload` — 生成済み動画の YouTube アップロード管理 |
| `yt-review.md` | `/yt-review` — チャンネルの定期レビュー |
| `ニュース動画ＭＤ使い方.txt` | 各コマンドの使い方メモ |

スキルを編集したら、`~/.claude/commands/` 側とこのフォルダの両方を更新してからコミットしてください。
