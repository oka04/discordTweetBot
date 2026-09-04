import asyncio
import json
import os
from pathlib import Path

import requests
from twikit import Client

STATE_FILE = Path("last_seen.json")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TARGET_USERNAME = os.environ["TARGET_USERNAME"]


def load_last_seen_id() -> str | None:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("last_seen_id")
    return None


def save_last_seen_id(tweet_id: str) -> None:
    STATE_FILE.write_text(
        json.dumps({"last_seen_id": tweet_id}, ensure_ascii=False),
        encoding="utf-8",
    )


def post_to_discord(tweet_url: str) -> None:
    response = requests.post(DISCORD_WEBHOOK_URL, json={"content": tweet_url})
    response.raise_for_status()


async def main() -> None:
    client = Client(language="ja-JP")

    cookies = json.loads(os.environ["X_COOKIES"])
    client.set_cookies(cookies)

    user = await client.get_user_by_screen_name(TARGET_USERNAME)
    tweets = await client.get_user_tweets(user.id, "Tweets")

    last_seen_id = load_last_seen_id()
    new_tweets = []
    for tweet in tweets:
        if last_seen_id is not None and int(tweet.id) <= int(last_seen_id):
            break
        new_tweets.append(tweet)

    if not new_tweets:
        print("新しい投稿はありませんでした。")
        return

    for tweet in reversed(new_tweets):
        tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet.id}"
        post_to_discord(tweet_url)
        print(f"投稿しました: {tweet_url}")

    newest_id = new_tweets[0].id
    save_last_seen_id(newest_id)


if __name__ == "__main__":
    asyncio.run(main())
