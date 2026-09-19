# -*- coding: utf-8 -*-
"""読み取り専用: 全動画の詳細分析（尺・公開時刻・種別ごとの成績・7/11前後比較）"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))


def iso_dur_to_sec(d):
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

    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    vids = []
    req = yt.playlistItems().list(part="contentDetails", playlistId=uploads_pl, maxResults=50)
    while req:
        res = req.execute()
        vids += [i["contentDetails"]["videoId"] for i in res.get("items", [])]
        req = yt.playlistItems().list_next(req, res)

    rows = []
    for i in range(0, len(vids), 50):
        res = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(vids[i:i+50])).execute()
        for v in res.get("items", []):
            t = v["snippet"]["title"]
            dur = iso_dur_to_sec(v["contentDetails"]["duration"])
            pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")).astimezone(JST)
            if "今日のAIニュース" in t:
                kind = "shorts_news"
            elif "【AIニュース】" in t or "のAIニュース｜ポンテ" in t:
                kind = "daily_news"
            else:
                kind = "other"
            rows.append({
                "id": v["id"], "kind": kind, "dur": dur,
                "pub": pub, "views": int(v.get("statistics", {}).get("viewCount", 0)),
                "title": t[:48],
            })

    rows.sort(key=lambda r: r["pub"])
    print(f"総動画数: {len(rows)}")
    print("\n=== 種別ごとの成績 ===")
    for kind in ["shorts_news", "daily_news", "other"]:
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        vs = [r["views"] for r in sub]
        print(f"{kind:<12} 本数={len(sub):<4} 合計={sum(vs):<7} 中央値={median(vs):<7.0f} 最大={max(vs):<7} 尺中央値={median(r['dur'] for r in sub):.0f}s")

    print("\n=== other（ニュース以外）の全動画 ===")
    for r in [r for r in rows if r["kind"] == "other"]:
        print(f"{r['pub']:%m/%d %H:%M} views={r['views']:<6} {r['dur']:>4}s {r['title']}")

    print("\n=== shorts_news 全履歴（公開時刻JST・尺・再生） ===")
    for r in [r for r in rows if r["kind"] == "shorts_news"]:
        print(f"{r['pub']:%m/%d(%a) %H:%M} views={r['views']:<6} {r['dur']:>4}s {r['title']}")

    good = [r for r in rows if r["kind"] == "shorts_news" and datetime(2026, 7, 2, tzinfo=JST) <= r["pub"] < datetime(2026, 7, 10, tzinfo=JST)]
    bad = [r for r in rows if r["kind"] == "shorts_news" and r["pub"] >= datetime(2026, 7, 10, tzinfo=JST)]
    print("\n=== 7/11崩落 前後比較（shorts） ===")
    for name, sub in [("好調期 7/2-7/9公開", good), ("崩落期 7/10以降公開", bad)]:
        if sub:
            hours = [r["pub"].hour for r in sub]
            print(f"{name}: n={len(sub)} 尺={sorted(r['dur'] for r in sub)} 公開時刻(時)={sorted(hours)} views={sorted((r['views'] for r in sub), reverse=True)}")


if __name__ == "__main__":
    main()
