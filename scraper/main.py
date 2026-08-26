#!/usr/bin/env python3
"""Poll the Nefesh B'Nefesh job board and push new listings to Telegram.

Run every 30 minutes from GitHub Actions. State (the set of job ids already
announced) lives in state/seen.json and is committed back by the workflow, so
restarts and re-runs never re-send the same job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nbn import Job, fetch_recent, matches           # noqa: E402
from telegram import Telegram, TelegramError, esc    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "seen.json"
STATE_LIMIT = 5000        # ids retained; ~2 years of postings
SNIPPET_CHARS = 400


# --------------------------------------------------------------------------- config

def env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [p.strip().lower() for p in os.environ.get(name, "").split(",") if p.strip()]


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": [], "last_run": None, "initialised": False}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("WARNING: state file is corrupt; starting fresh", file=sys.stderr)
        return {"seen_ids": [], "last_run": None, "initialised": False}
    data.setdefault("seen_ids", [])
    data.setdefault("initialised", bool(data["seen_ids"]))
    return data


def save_state(state: dict, new_ids: list[int]) -> None:
    # Newest ids first, so pruning drops the oldest.
    merged = list(dict.fromkeys([*new_ids, *state.get("seen_ids", [])]))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "seen_ids": merged[:STATE_LIMIT],
                "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "initialised": True,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- format

def format_job(job: Job) -> str:
    lines = [f"💼 <b>{esc(job.title)}</b>"]

    if job.company:
        lines.append(f"🏢 {esc(job.company)}")

    where = job.location or ("Remote" if job.remote else "")
    if where:
        lines.append(f"📍 {esc(where)}" + (" · Remote" if job.remote and job.location else ""))

    meta_bits = job.job_types + job.categories
    if meta_bits:
        lines.append(f"🏷 {esc(' · '.join(meta_bits))}")
    if job.salary:
        lines.append(f"💰 {esc(job.salary)}")

    posted = job.posted.replace("T", " ")[:16]
    if posted:
        lines.append(f"🕒 Posted {esc(posted)}")

    if job.description:
        snippet = job.description[:SNIPPET_CHARS].rsplit(" ", 1)[0]
        if len(job.description) > SNIPPET_CHARS:
            snippet += "…"
        lines += ["", esc(snippet)]

    lines += ["", f'🔗 <a href="{esc(job.url)}">View listing</a>']
    if job.apply_to and "@" in job.apply_to:
        lines.append(f"✉️ Apply: {esc(job.apply_to)}")
    elif job.apply_to.startswith("http"):
        lines.append(f'✉️ <a href="{esc(job.apply_to)}">Apply</a>')

    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print messages instead of sending; leaves state untouched")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently posted as seen without notifying")
    ap.add_argument("--test-message", action="store_true",
                    help="send a single test message to verify Telegram wiring")
    args = ap.parse_args()

    dry_run = args.dry_run or env_flag("DRY_RUN")
    seed = args.seed or env_flag("SEED_ONLY")
    max_messages = env_int("MAX_MESSAGES_PER_RUN", 20)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if args.test_message:
        try:
            bot = Telegram(token, chat_id)
            bot.send(f"✅ NBN job board watcher is connected (bot: @{esc(bot.check())}).")
        except TelegramError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print("Test message sent.")
        return 0

    state = load_state()
    seen = set(state["seen_ids"])
    first_run = not state["initialised"]

    jobs = fetch_recent()
    print(f"Fetched {len(jobs)} recent listings from the NBN job board.")
    if not jobs:
        print("No listings returned; leaving state untouched.")
        return 0

    fresh = [j for j in jobs if j.id not in seen and not j.filled]
    print(f"{len(fresh)} not previously seen.")

    kept = [
        j for j in fresh
        if matches(
            j,
            keywords=env_list("KEYWORDS"),
            exclude=env_list("EXCLUDE_KEYWORDS"),
            regions=env_list("REGIONS"),
            categories=env_list("CATEGORIES"),
            job_types=env_list("JOB_TYPES"),
            remote_only=env_flag("REMOTE_ONLY"),
        )
    ]
    if len(kept) != len(fresh):
        print(f"{len(fresh) - len(kept)} filtered out by KEYWORDS/REGIONS/etc.")

    # Every id we looked at is recorded, including filtered ones, so a later
    # filter change doesn't dump a backlog of old jobs into the chat.
    all_ids = [j.id for j in jobs]

    if first_run or seed:
        reason = "first run" if first_run else "SEED_ONLY"
        print(f"{reason}: recording {len(all_ids)} listings as seen without notifying.")
        if not dry_run:
            save_state(state, all_ids)
        return 0

    if not kept:
        print("Nothing new to announce.")
        if not dry_run:
            save_state(state, all_ids)
        return 0

    # Oldest first so the chat reads chronologically.
    kept.reverse()
    overflow = kept[max_messages:]
    kept = kept[:max_messages]

    if dry_run:
        for job in kept:
            print("-" * 60)
            print(format_job(job))
        print("-" * 60)
        print(f"DRY RUN: {len(kept)} message(s) would be sent; state not written.")
        return 0

    try:
        bot = Telegram(token, chat_id)
    except TelegramError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    sent, sent_ids = 0, []
    for job in kept:
        try:
            bot.send(format_job(job))
        except TelegramError as exc:
            print(f"ERROR sending job {job.id}: {exc}", file=sys.stderr)
            break
        sent += 1
        sent_ids.append(job.id)

    if overflow:
        try:
            bot.send(
                f"ℹ️ {len(overflow)} more new listing(s) this run were not sent "
                f"individually (MAX_MESSAGES_PER_RUN={max_messages}). "
                f'See the <a href="https://www.nbn.org.il/jobboard/">job board</a>.'
            )
            sent_ids += [j.id for j in overflow]
        except TelegramError as exc:
            print(f"ERROR sending overflow notice: {exc}", file=sys.stderr)

    print(f"Sent {sent} job message(s).")

    if sent == len(kept):
        # Clean run: record every id we looked at, filtered ones included, so a
        # later filter change doesn't dump a backlog of old jobs into the chat.
        save_state(state, all_ids)
        return 0

    # A send failed partway: record only what actually went out, so the
    # remainder is retried on the next poll.
    if sent_ids:
        save_state(state, sent_ids)
    return 1


if __name__ == "__main__":
    sys.exit(main())
