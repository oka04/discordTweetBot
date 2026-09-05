import asyncio
import json
import os
import traceback
from pathlib import Path

import requests
from twikit import Client

STATE_FILE = Path("last_seen.json")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TARGET_USERNAME = os.environ["TARGET_USERNAME"]

ERROR_LOGIN_FAILED = "E001"
ERROR_USER_NOT_FOUND = "E002"
ERROR_RATE_LIMITED = "E003"
ERROR_NETWORK = "E004"
ERROR_UNKNOWN = "E999"

ERROR_LIST_NOTE = "エラー一覧.md"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_seen_id": None, "last_notification_was_error": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def post_to_discord(content: str) -> None:
    response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
    response.raise_for_status()


def classify_error(exc: Exception) -> str:
    message = str(exc).lower()

    if "login" in message or "auth" in message or "unauthorized" in message or "401" in message:
        return ERROR_LOGIN_FAILED
    if "not found" in message or "does not exist" in message or "404" in message:
        return ERROR_USER_NOT_FOUND
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return ERROR_RATE_LIMITED
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return ERROR_NETWORK
    return ERROR_UNKNOWN


def build_error_message(error_code: str, exc: Exception) -> str:
    return (
        f"\u26a0\ufe0f Xの投稿チェックでエラーが発生しました(エラー番号: {error_code})\n"
        f"詳細: {exc}\n"
        f"対処方法は倉庫内の {ERROR_LIST_NOTE} の {error_code} の項目を見てください。"
    )


async def check_and_notify() -> None:
    client = Client(language="ja-JP")

    cookies = json.loads(os.environ["X_COOKIES"])
    client.set_cookies(cookies)

    user = await client.get_user_by_screen_name(TARGET_USERNAME)
    tweets = await client.get_user_tweets(user.id, "Tweets")

    state = load_state()
    last_seen_id = state.get("last_seen_id")

    new_tweets = []
    for tweet in tweets:
        if last_seen_id is not None and int(tweet.id) <= int(last_seen_id):
            break
        new_tweets.append(tweet)

    if not new_tweets:
        print("新しい投稿はありませんでした。")
    else:
        for tweet in reversed(new_tweets):
            tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet.id}"
            post_to_discord(tweet_url)
            print(f"投稿しました: {tweet_url}")

        state["last_seen_id"] = new_tweets[0].id

    state["last_notification_was_error"] = False
    save_state(state)


async def main() -> None:
    try:
        await check_and_notify()
    except Exception as exc:
        traceback.print_exc()

        state = load_state()
        error_code = classify_error(exc)

        if state.get("last_notification_was_error"):
            print(
                f"エラー({error_code})が発生しましたが、前回もエラー通知済みのため、"
                "今回はDiscordへの通知をスキップします。"
            )
        else:
            try:
                post_to_discord(build_error_message(error_code, exc))
            except Exception:
                print("Discordへのエラー通知自体にも失敗しました。")
                traceback.print_exc()

        state["last_notification_was_error"] = True
        save_state(state)

        raise


if __name__ == "__main__":
    asyncio.run(main())
