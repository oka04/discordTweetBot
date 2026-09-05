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

MAX_PAGES = 10  # 取りこぼし防止のため、最大でこのページ数までさかのぼって確認する


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


async def collect_new_tweets(client: Client, user_id: str, last_seen_id: str | None) -> list:
    """
    last_seen_idより新しいツイートを、必要なら複数ページさかのぼって集める。

    「先頭のツイートが古かったら即座に打ち切る」という判定方法だと、
    ・実行間隔が長く空いて1ページ分より多く投稿されていた場合
    ・ピン留めツイートが先頭に出てきて古いIDのまま並んでいる場合
    に新しい投稿を見逃してしまうため、ページの中身を全部確認してから
    次のページに進むかどうかを判断する方式にしている。
    """
    last_seen_id_int = int(last_seen_id) if last_seen_id is not None else None

    new_tweets = []
    page = await client.get_user_tweets(user_id, "Tweets")
    pages_checked = 0

    while page:
        pages_checked += 1
        page_has_new = False

        for tweet in page:
            tweet_id_int = int(tweet.id)
            if last_seen_id_int is None or tweet_id_int > last_seen_id_int:
                new_tweets.append(tweet)
                page_has_new = True

        # 初回実行(まだlast_seen_idが無い)は、過去分を大量通知しないよう
        # 1ページ目だけ確認して終わりにする
        if last_seen_id_int is None:
            break

        # このページに新しい投稿が1件も無ければ、十分さかのぼれたと判断して終了
        if not page_has_new:
            break

        if pages_checked >= MAX_PAGES:
            print(
                f"警告: {MAX_PAGES}ページ確認しましたが、まだ新しい投稿がありそうです。"
                "取りこぼしが出た可能性があります。"
            )
            break

        try:
            page = await page.next()
        except Exception:
            break

    new_tweets.sort(key=lambda t: int(t.id))
    return new_tweets


async def check_and_notify() -> None:
    client = Client(language="ja-JP")

    cookies = json.loads(os.environ["X_COOKIES"])
    client.set_cookies(cookies)

    user = await client.get_user_by_screen_name(TARGET_USERNAME)

    state = load_state()
    last_seen_id = state.get("last_seen_id")

    new_tweets = await collect_new_tweets(client, user.id, last_seen_id)

    if not new_tweets:
        print("新しい投稿はありませんでした。")
    else:
        for tweet in new_tweets:
            tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet.id}"
            post_to_discord(tweet_url)
            print(f"投稿しました: {tweet_url}")

        state["last_seen_id"] = new_tweets[-1].id

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
