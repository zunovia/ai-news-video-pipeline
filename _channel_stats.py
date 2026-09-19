# -*- coding: utf-8 -*-
"""読み取り専用: チャンネル統計と直近動画の再生数・公開状態を取得する分析用スクリプト"""
import json
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_CANDIDATES = [Path("state/youtube_token.json"), Path("state/old/youtube_token.json")]


def main():
    token_path = next(p for p in TOKEN_CANDIDATES if p.exists())
    creds = Credentials.from_authorized_user_info(json.loads(token_path.read_text(encoding="utf-8")))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    ch = yt.channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
    c = ch["items"][0]
    stats = c["statistics"]
    print("=== チャンネル ===")
    print(f"名前: {c['snippet']['title']}")
    print(f"登録者: {stats.get('subscriberCount')}  総再生: {stats.get('viewCount')}  動画数: {stats.get('videoCount')}")
    uploads_pl = c["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids = []
    req = yt.playlistItems().list(part="contentDetails", playlistId=uploads_pl, maxResults=50)
    while req and len(video_ids) < 100:
        res = req.execute()
        video_ids += [i["contentDetails"]["videoId"] for i in res.get("items", [])]
        req = yt.playlistItems().list_next(req, res)

    print(f"\n=== 直近動画 {len(video_ids)}本 ===")
    rows = []
    for i in range(0, len(video_ids), 50):
        res = yt.videos().list(part="snippet,statistics,status", id=",".join(video_ids[i:i+50])).execute()
        for v in res.get("items", []):
            s = v.get("statistics", {})
            rows.append({
                "published": v["snippet"]["publishedAt"][:10],
                "privacy": v["status"]["privacyStatus"],
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "title": v["snippet"]["title"][:60],
            })
    rows.sort(key=lambda r: r["published"], reverse=True)
    total_views = sum(r["views"] for r in rows)
    public_n = sum(1 for r in rows if r["privacy"] == "public")
    private_n = sum(1 for r in rows if r["privacy"] == "private")
    print(f"合計再生: {total_views}  public: {public_n}本  private: {private_n}本  unlisted: {len(rows)-public_n-private_n}本")
    for r in rows:
        print(f"{r['published']}  {r['privacy']:<8}  views={r['views']:<6} likes={r['likes']:<4} {r['title']}")


if __name__ == "__main__":
    main()
