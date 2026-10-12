# YouTube チャンネル定期レビュー（サーコミュ / 912-plan）

チャンネルの配信状態・再生実績・YouTubeポリシー動向を定期的に精査し、改善アクションを更新するスキル。**週1回の実行を推奨**（ユーザーが `/yt-review` と打つ、または「週次レビューして」と発話）。

## 背景（必読）

- 2026-07-11にAIニュースShortsへの配信抑制が発生（520再生→翌日3）。YouTubeの2026-07-13 inauthentic contentポリシー明確化（テンプレ量産・スライドショー＋合成音声・記事逐語読み上げの抑制）と符合。詳細はメモリ `912-improvement-progress.md` 追補4〜6。
- Analytics API連携はユーザー決定で**実施しない**（再認証不要の読み取りスクリプトのみ使用）。CTR・インプレッション・流入元はユーザーがYouTube Studioの数値/スクショを共有する。
- 実験は「週1・1変数」原則。

## Instructions

All commands MUST run with working directory `C:/02.Claude_Agent/912-plan/` and `PYTHONIOENCODING=utf-8`.

### Step 1: データ取得（読み取り専用スクリプト3本）

```
python _channel_stats.py        # チャンネル統計＋直近100本の再生/公開状態
python _channel_deep.py         # 種別ごとの成績・公開時刻・7/11前後比較
python _inspect_recent_meta.py 10   # 直近10本の実メタデータ（タイトル/タグ/説明文/設定）
```

### Step 2: 前回レビューとの比較

`state/weekly_review_log.md` を読み（無ければ今回作成）、前回の数値・実験内容・宿題と比較する:
- 直近1週間に公開したShortsの再生数中央値（目標: 旧好調期の100〜500 → 4週間で中央値1,000）
- 抑制状態の変化（0〜3再生が続いているか、回復の兆しがあるか）
- メタデータの劣化チェック: タイトル切断・ハッシュタグ過多（3個超）・説明文の破損・madeForKids等の設定異常

### Step 3: ユーザーにStudioデータを依頼

APIでは取れない以下をユーザーに確認（スクショまたは数値）:
1. コンテンツ一覧の「制限」列に表示がないか
2. アナリティクス→リーチ→インプレッション数・Shortsフィード表示回数（動画別）
- インプレ≒0 → 配信抑制が継続（コンテンツ改善よりも量産シグナル解除・チャンネル戦略を優先）
- インプレあり低視聴 → フック/タイトル改善を優先

### Step 4: ポリシー・環境動向の確認（WebSearch）

以下を軽く確認（各1クエリ程度、深追いしない）:
- YouTube inauthentic content / AI生成コンテンツポリシーの新しい発表・執行波
- Shorts小規模チャンネルのアルゴリズム変更情報

### Step 5: 判定と提案

1. **4週ゲート判定**（起点2026-07-17）: 中央値が回復傾向にあるか。4週経過して回復なしなら以下を提示:
   - 選択肢A: 新チャンネルで再出発（改善後フォーマット＋一貫ジャンル）
   - 選択肢B: YouTube比重を下げTechFeed Daily（news.surc.online）相互送客へ軸足移動
2. **今週の実験1つ**を提案（1変数のみ。例: 公開時刻変更、insight強化、視覚バリエーション、1日1本化）
3. 発見した技術的問題（タイトル切断等）は修正を提案

### Step 6: 記録

1. `state/weekly_review_log.md` に追記（日付・主要数値・判定・今週の実験・宿題）
2. 大きな方針変更・発見があった場合のみ、メモリ `912-improvement-progress.md` に追補として要約を追記（数値の細かい記録はweekly_review_log.md側に置く）

## 診断スクリプト一覧（全て読み取り専用）

| スクリプト | 内容 |
|---|---|
| `_channel_stats.py` | チャンネル統計＋直近100本の再生/公開状態 |
| `_channel_deep.py` | 種別成績・尺・公開時刻・崩落前後比較 |
| `_inspect_recent_meta.py [N]` | 直近N本の実メタデータ詳細（既定10） |
| `_competitor_research.py` | 競合チャンネル実測（必要時のみ） |
| `_check_kids_flag.py` | madeForKidsフラグ確認（必要時のみ） |

## 注意

- スクリプトは全て読み取り専用。動画の削除・非公開化・メタデータ変更は必ずユーザー承認を得てから
- Windowsコンソールでは `PYTHONIOENCODING=utf-8` が必要（cp932でUnicodeEncodeError）
- トークンは `state/youtube_token.json`（無ければ `state/old/youtube_token.json`）
