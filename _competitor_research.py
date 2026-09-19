# -*- coding: utf-8 -*-
"""読み取り専用: 競合AIニュース/解説チャンネルの実数値調査（YouTube Data API）"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

KNOWN_CHANNELS = [
    "UCkRr4i0GaOuFQ7arOIWcpzg",  # IT・AI速報チャンネル【ずんだもん解説】
    "UC_dLmVT-fjTCxTVrrVhhoyw",  # AI時代のテック速報【ずんだもん解説】
]
QUERIES = ["AIニュース", "AI ニュース ずんだもん", "ChatGPT 使い方 初心者", "生成AI 解説"]
RELEVANT = re.compile(r"AI|ずんだ|ChatGPT|テック|IT|生成", re.I)
EXCLUDE = re.compile(r"TBS|日テレ|FNN|ABEMA|フジテレビ|PIVOT|バラエティ|ゴシップ|1分だけ|コースケ")


def dur_sec(d):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def main():
    tok = Path("state/old/youtube_token.json")
    creds = Credentials.from_authorized_user_info(json.loads(tok.read_text(encoding="utf-8")))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    after = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ch_hits = {}
    for q in QUERIES:
        res = yt.search().list(
            part="snippet", q=q, type="video", order="viewCount",
            publishedAfter=after, regionCode="JP", relevanceLanguage="ja", maxResults=25,
        ).execute()
        for it in res.get("items", []):
            ch_hits.setdefault(it["snippet"]["channelId"], it["snippet"]["channelTitle"])

    ids = list(dict.fromkeys(KNOWN_CHANNELS + list(ch_hits)))
    chans = {}
    for i in range(0, len(ids), 50):
        res = yt.channels().list(part="snippet,statistics,contentDetails", id=",".join(ids[i:i+50])).execute()
        for c in res.get("items", []):
            st = c["statistics"]
            chans[c["id"]] = {
                "title": c["snippet"]["title"],
                "subs": int(st.get("subscriberCount", 0)),
                "views": int(st.get("viewCount", 0)),
                "videos": int(st.get("videoCount", 0)),
                "uploads": c["contentDetails"]["relatedPlaylists"]["uploads"],
                "since": c["snippet"]["publishedAt"][:10],
            }

    targets = [
        (cid, c) for cid, c in chans.items()
        if (RELEVANT.search(c["title"]) and not EXCLUDE.search(c["title"])) or cid in KNOWN_CHANNELS
    ]
    targets.sort(key=lambda kv: -kv[1]["subs"])
    print("=== 比較対象チャンネル（登録者順） ===")
    for cid, c in targets:
        print(f"{c['subs']:>9,}人 {c['views']:>12,}再生 {c['videos']:>5}本 開設{c['since']} | {c['title'][:40]}")

    print("\n=== 各チャンネル直近12本（頻度・尺・再生） ===")
    for cid, c in targets:
        try:
            pl = yt.playlistItems().list(part="contentDetails", playlistId=c["uploads"], maxResults=12).execute()
        except Exception as e:
            print(f"\n## {c['title']}  (取得失敗: {e})")
            continue
        vids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if not vids:
            continue
        res = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(vids)).execute()
        rows = []
        for v in res.get("items", []):
            d = dur_sec(v.get("contentDetails", {}).get("duration"))
            if d == 0:
                continue  # 予約ライブ等
            rows.append({
                "pub": v["snippet"]["publishedAt"][:10],
                "views": int(v.get("statistics", {}).get("viewCount", 0)),
                "dur": d,
                "title": v["snippet"]["title"][:56],
            })
        if not rows:
            continue
        rows.sort(key=lambda r: r["pub"], reverse=True)
        dates = sorted({r["pub"] for r in rows})
        span = max((datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days, 1)
        per_week = len(rows) / span * 7
        med = median(r["views"] for r in rows)
        shorts_n = sum(1 for r in rows if r["dur"] <= 183)
        print(f"\n## {c['title']}（登録{c['subs']:,}・開設{c['since']}）: {per_week:.1f}本/週, 中央値{med:,.0f}再生, Shorts比率{shorts_n}/{len(rows)}")
        for r in rows[:9]:
            print(f"  {r['pub']} views={r['views']:<8,} {r['dur']:>5}s {r['title']}")


if __name__ == "__main__":
    main()
