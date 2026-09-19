# -*- coding: utf-8 -*-
"""読み取り専用: 直近アップロード動画の実メタデータ詳細を取得（既定10本、引数で変更可）

使い方: python _inspect_recent_meta.py [本数]
"""
import json
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

sys.stdout.reconfigure(encoding="utf-8")

TOKEN_CANDIDATES = [Path("state/youtube_token.json"), Path("state/old/youtube_token.json")]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    token_path = next(p for p in TOKEN_CANDIDATES if p.exists())
    creds = Credentials.from_authorized_user_info(json.loads(token_path.read_text(encoding="utf-8")))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    res = yt.playlistItems().list(part="contentDetails", playlistId=uploads_pl, maxResults=min(n, 50)).execute()
    video_ids = [i["contentDetails"]["videoId"] for i in res.get("items", [])][:n]

    res = yt.videos().list(
        part="snippet,status,statistics,contentDetails,topicDetails",
        id=",".join(video_ids),
    ).execute()
    items = {v["id"]: v for v in res.get("items", [])}
    for vid in video_ids:
        v = items.get(vid)
        if not v:
            print(f"!! 取得失敗: {vid}")
            continue
        sn, st, stat, cd = v["snippet"], v["status"], v.get("statistics", {}), v["contentDetails"]
        print("=" * 70)
        print(f"ID: {v['id']}  https://youtu.be/{v['id']}")
        print(f"タイトル: {sn['title']}")
        print(f"公開日時: {sn['publishedAt']}  privacy={st['privacyStatus']}  uploadStatus={st.get('uploadStatus')}")
        print(f"再生={stat.get('viewCount')} 高評価={stat.get('likeCount')} コメント={stat.get('commentCount')}")
        print(f"尺={cd['duration']}  definition={cd.get('definition')}  caption={cd.get('caption')}")
        print(f"categoryId={sn.get('categoryId')}  defaultLanguage={sn.get('defaultLanguage')}  defaultAudioLanguage={sn.get('defaultAudioLanguage')}")
        print(f"madeForKids={st.get('madeForKids')}  selfDeclaredMadeForKids={st.get('selfDeclaredMadeForKids')}  license={st.get('license')}  embeddable={st.get('embeddable')}")
        if st.get("rejectionReason") or st.get("failureReason"):
            print(f"!! rejection={st.get('rejectionReason')} failure={st.get('failureReason')}")
        print(f"タグ({len(sn.get('tags', []))}): {sn.get('tags')}")
        print(f"topicCategories: {v.get('topicDetails', {}).get('topicCategories')}")
        desc = sn.get("description", "")
        print(f"--- 説明文 ({len(desc)}字) ---")
        print(desc[:600])


if __name__ == "__main__":
    main()
