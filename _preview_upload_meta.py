# -*- coding: utf-8 -*-
"""アップロードせずに per-topic Shorts のメタデータを確認するドライラン."""
import json
import sys
from pathlib import Path

from youtube_uploader import generate_topic_shorts_metadata, load_config

date_str = sys.argv[1] if len(sys.argv) > 1 else "20260716"
config = load_config(Path("config.json"))
news = json.loads(Path(f"state/news_{date_str}.json").read_text(encoding="utf-8"))
meta = json.loads(Path(f"state/script_meta_{date_str}.json").read_text(encoding="utf-8"))

top_n = config.get("shorts", {}).get("topics_per_day", 3)
for i in range(1, top_n + 1):
    article = news["articles"][i - 1]
    hook = meta.get("articles", [{}] * i)[i - 1].get("hook_title", "")
    m = generate_topic_shorts_metadata(date_str, config, article, hook, i)
    print(f"=== topic{i} ===")
    print(f"title   : {m['title']}  ({len(m['title'])}字)")
    print(f"privacy : {m['privacy']}  playlist: {m['playlist']}  kind: {m['kind']}")
    print(f"tags    : {', '.join(m['tags'][:6])}...")
    print("--- description ---")
    print(m["description"])
    print()
