import asyncio
import json
import os
import time
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
POST_INTERVAL_SECONDS = 2  # Discordへの連続投稿の間隔(レート制限回避のため)
DISCORD_MAX_RETRIES = 5
PINNED_TWEET_CHECK_COUNT = 2  # 固定ツイート対策として、無条件にチェックする先頭の件数


class PartialSendError(Exception):
    """新しい投稿の一部しか送信できなかったことを表す例外"""

    def __init__(self, sent_count: int, total_count: int, original_error: Exception):
        self.sent_count = sent_count
        self.total_count = total_count
        self.original_error = original_error
        remaining = total_count - sent_count
        super().__init__(
            f"{total_count}件中{sent_count}件送信した時点でエラーが発生し、"
            f"残り{remaining}件が未送信です。"
        )


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
    """
    DiscordのWebhookは短時間に連続で送るとレート制限(429)を返してくる。
    429が返ってきた場合は、Discordが教えてくれる「あと何秒待って」という
    値(retry_after)の分だけ待ってから、自動でもう一度送り直す。
    """
    for attempt in range(1, DISCORD_MAX_RETRIES + 1):
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})

        if response.status_code == 429:
            try:
                retry_after = response.json().get("retry_after", 1)
            except ValueError:
                retry_after = 1
            wait_seconds = float(retry_after) + 0.5
            print(
                f"Discordのレート制限に達しました。{wait_seconds:.1f}秒待ってリトライします "
                f"({attempt}/{DISCORD_MAX_RETRIES}回目)"
            )
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        return

    raise RuntimeError("Discordへの投稿がリトライ上限に達しました(レート制限が解消しませんでした)")


def classify_error(exc: Exception) -> str:
    if isinstance(exc, PartialSendError):
        exc = exc.original_error

    exception_name = type(exc).__name__.lower()
    message = str(exc).lower()

    if (
        "login" in message
        or "log in" in message
        or "auth" in message
        or "unauthorized" in message
        or "401" in message
        or "invalidsession" in exception_name
        or "logged-out" in message
    ):
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

    先頭2件は固定ツイートが混ざっている可能性があるので、新しいかどうかに
    関わらず無条件にチェックする。3件目以降は、新しい投稿が続く限り見ていき、
    新しくない投稿に出会った時点でそのページの確認を打ち切る。
    1ページ全部が新しい投稿だった場合は、次のページも確認しに行く。
    """
    last_seen_id_int = int(last_seen_id) if last_seen_id is not None else None

    new_tweets = []
    page = await client.get_user_tweets(user_id, "Tweets")
    pages_checked = 0

    while page:
        pages_checked += 1
        page_tweets = list(page)
        stopped_early = False

        for position, tweet in enumerate(page_tweets):
            tweet_id_int = int(tweet.id)
            is_new = last_seen_id_int is None or tweet_id_int > last_seen_id_int

            if is_new:
                new_tweets.append(tweet)
            elif position >= PINNED_TWEET_CHECK_COUNT:
                stopped_early = True
                break
            # position が先頭数件以内で新しくなかった場合は、固定ツイートの
            # 可能性があるので打ち切らずに次の判定へ進む

        if last_seen_id_int is None:
            # 初回実行時は過去分を大量通知しないよう、1ページ目だけ見て終わる
            break

        if stopped_early:
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
        state["last_notification_was_error"] = False
        save_state(state)
        return

    for index, tweet in enumerate(new_tweets):
        tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet.id}"

        try:
            post_to_discord(tweet_url)
        except Exception as exc:
            raise PartialSendError(
                sent_count=index, total_count=len(new_tweets), original_error=exc
            ) from exc

        print(f"投稿しました: {tweet_url}")

        # 1件送るたびに、その都度「ここまで送った」を保存しておく。
        # こうしておけば、この後の投稿でエラーが起きても、
        # 既に送信済みの分を次回また送り直してしまうことがない。
        state["last_seen_id"] = tweet.id
        state["last_notification_was_error"] = False
        save_state(state)

        is_last = index == len(new_tweets) - 1
        if not is_last:
            time.sleep(POST_INTERVAL_SECONDS)


async def main() -> None:
    state_before_run = load_state()
    was_previously_errored = state_before_run.get("last_notification_was_error", False)

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
    else:
        if was_previously_errored:
            try:
                post_to_discord("\u2705 前回発生していたエラーから復旧し、正常に動作しています。")
                print("復旧通知をDiscordに送信しました。")
            except Exception:
                print("復旧通知の送信に失敗しました。")
                traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
