# -*- coding: utf-8 -*-
"""読み取り専用: madeForKids設定とトークンのスコープ確認"""
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

tok = Path("state/old/youtube_token.json")
info = json.loads(tok.read_text(encoding="utf-8"))
print("token scopes:", info.get("scopes"))
creds = Credentials.from_authorized_user_info(info)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
yt = build("youtube", "v3", credentials=creds)
res = yt.videos().list(part="status,snippet", id="34JA4vAGa_c,Xv89E4oczUI").execute()
for v in res["items"]:
    st = v["status"]
    print(v["id"], "| madeForKids:", st.get("madeForKids"),
          "| selfDeclared:", st.get("selfDeclaredMadeForKids"),
          "| privacy:", st.get("privacyStatus"),
          "| publishAt:", st.get("publishAt"),
          "| lang:", v["snippet"].get("defaultAudioLanguage"))
