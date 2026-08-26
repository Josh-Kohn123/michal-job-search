# NBN Job Board → Telegram

Polls the [Nefesh B'Nefesh job board](https://www.nbn.org.il/jobboard/) every 30
minutes from GitHub Actions and posts each newly published listing to a Telegram
chat.

## How the data is sourced

The board is a WordPress site (at `/jobboard/`, a separate install from the main
`nbn.org.il` site) running the **WP Job Manager** plugin. Listings are the
`job_listing` custom post type, and that post type is **exposed on the site's own
public REST API**:

```
https://www.nbn.org.il/jobboard/wp-json/wp/v2/job-listings
```

So this is not HTML scraping — it reads the same database rows the page renders,
as JSON. That matters for accuracy:

| Concern | How the API handles it |
|---|---|
| Markup changes | Irrelevant; no HTML parsing of listings |
| Expired / filled jobs | The API returns `publish` status only, so listings WP Job Manager retires disappear on their own. `_filled` is also checked explicitly. |
| Pagination | `X-WP-Total` / `X-WP-TotalPages` headers give exact counts (~300 live listings) |
| Ordering | `orderby=date&order=desc` — newest first, reliably |
| Location / category / type | Real taxonomy terms, resolved by name via `_embed=wp:term` |
| Contact details | `meta._application` — the actual apply-to email or URL |

Fields read per listing:

```
id, date, link, title.rendered, content.rendered
meta._company_name, meta._company_website, meta._application,
meta._remote_position, meta._filled, meta._job_salary*
taxonomies: job_listing_region, job_listing_category, job_listing_type
```

`robots.txt` does not disallow `/jobboard/` or `/wp-json/`, and the API is
unauthenticated and unthrottled. Each run makes two requests (200 most recent
listings — roughly a month of postings, against a board that publishes ~6/day),
so a 30-minute poll has a very large safety margin against missed jobs.

### Why not a date cursor

New listings are matched by **job id against a stored set**, not by a
`?after=<timestamp>` filter. A job submitted on Monday but approved on Thursday
keeps its Monday `post_date`, so a strict date cursor would silently skip it.
Comparing ids is immune to that.

## Layout

```
.github/workflows/nbn-jobs.yml   schedule, secrets, state commit
scraper/nbn.py                   REST client, normalisation, filtering
scraper/telegram.py              Bot API client (throttle, 429 backoff)
scraper/main.py                  orchestration, state, message formatting
state/seen.json                  ids already announced (committed by the workflow)
```

State lives in `state/seen.json` and is committed back to the repo by the
workflow. Ids are recorded only after their message is actually delivered, so a
Telegram outage mid-run retries the remainder on the next poll instead of losing
those jobs.

## Setup

**1. Create the bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot` →
copy the token.

**2. Get the chat id.** Send your bot any message, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
```

For a group, add the bot to the group first and post a message there. Group ids
are negative (e.g. `-1001234567890`). For a channel, use `@channelusername` and
make the bot an admin.

**3. Add repository secrets** — Settings → Secrets and variables → Actions →
*Secrets*:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | the chat/group/channel id |

**4. Push the repo to GitHub**, then run the workflow once manually
(Actions → *NBN Job Board → Telegram* → Run workflow) with mode **`test-message`**
to confirm delivery.

**5. Seed the baseline.** The very first `normal` run records the ~300 currently
posted jobs as seen and sends nothing — this is deliberate, so you don't get 300
messages at once. From the run after that, only genuinely new listings arrive.
The scheduled runs then take over automatically.

> GitHub disables scheduled workflows in repos with no activity for 60 days.
> The state commit on each run counts as activity, so this stays alive on its own.

## Optional filters

Set these as repository **variables** (same page, *Variables* tab). All are
optional; leaving them unset sends every new listing.

| Variable | Effect |
|---|---|
| `KEYWORDS` | Comma-separated. Send only jobs matching at least one (title + company + description + tags) |
| `EXCLUDE_KEYWORDS` | Comma-separated. Drop jobs matching any of these |
| `REGIONS` | e.g. `Jerusalem,Tel Aviv,Central Region` |
| `CATEGORIES` | e.g. `Hi-tech,Education,Non-Profit` |
| `JOB_TYPES` | e.g. `Full Time,Part Time,From Home` |
| `REMOTE_ONLY` | `true` = only remote-flagged positions |
| `MAX_MESSAGES_PER_RUN` | Default `20`; the remainder is summarised in one message |

Filtered-out jobs are still marked as seen, so widening a filter later won't
dump a backlog into the chat.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in your token and chat id

.venv/bin/python scraper/main.py --dry-run       # print, send nothing, write nothing
.venv/bin/python scraper/main.py --test-message  # verify Telegram wiring
.venv/bin/python scraper/main.py --seed          # mark current jobs seen, notify nothing
.venv/bin/python scraper/main.py                 # normal run
```

`--dry-run` never writes state, so it is safe to run repeatedly.

## Adjusting the schedule

Edit the cron in `.github/workflows/nbn-jobs.yml`. Note that GitHub's scheduler
is best-effort and can run several minutes late under load — the id-based state
makes late or skipped runs harmless.
