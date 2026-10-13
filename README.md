# Fare tracker

Checks flight prices twice a day for the routes and dates in `watches.toml` and sends you a Telegram message when a fare is under your target or drops 15%+ below its 30-day low. It runs free on GitHub Actions. Ignav charges for fare data.

## Setup (about 15 minutes)

**1. Ignav API key.** Sign up at https://ignav.com/signup. The first 1,000 requests are free, then $2 per 1,000.

**2. Telegram bot.**
- In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
- Send your new bot any message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`. That number is your **chat ID**.

**3. GitHub repo.**
- Create a **private** repo and upload everything in this folder, including `.github/`.
- Go to Settings → Secrets and variables → Actions → New repository secret, and add:
  `IGNAV_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Go to the Actions tab → fare-tracker → **Run workflow** to test it straight away.

**4. Edit `watches.toml`** with your routes and dates, then commit.

## Checking cost before you commit

```
python tracker.py --plan
```
This shows searches per run and an estimated monthly cost. Each departure/return date pair is one call, so `nights = [7, 10]` over 20 days is 80 calls. Narrow `depart_days` to cut that down. The run aborts if it exceeds `max_calls_per_run`.

## Adding watches in plain English (optional)

```
pip install anthropic
export ANTHROPIC_API_KEY=...
python add_watch.py --append "Heathrow to Lagos, leave a Thu or Fri in Dec, 7-10 nights, under £550, max 1 stop"
```
Claude turns the sentence into a `[[watch]]` entry. Check it, then commit.

## How alerts behave

- You get one message per route per run, listing the cheapest matching dates and a booking link for the best one.
- You're not re-alerted about the same dates unless the price falls further.
- Prices are search quotes. Confirm on the booking page before paying.

## Running locally

```
export IGNAV_API_KEY=...
python tracker.py            # without Telegram secrets, alerts print to the terminal
```
Requires Python 3.11+ and nothing else.

## Notes

- Price history lives in `fares.db`, which the workflow commits after each run. These commits also stop GitHub from pausing the schedule for inactivity.
- The cron schedule is in UTC. If you change how often it runs, update `runs_per_day` so `--plan` stays accurate.
- To pause, disable the workflow in the Actions tab.
