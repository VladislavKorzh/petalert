# petalert

A Telegram bot that checks house-sitting sites every ~30 minutes and messages
you when **new open dates** match your filters — both on brand-new listings and
on existing listings where the owner has added new dates. Sources, no login
needed for either:

- [TrustedHousesitters](https://www.trustedhousesitters.com) — the search
  page's internal JSON API;
- [MindMyHouse](https://www.mindmyhouse.com) — the search page's HTML: its map
  script lists every assignment on the site (~200), so one request covers it.

- A subscription is a set of filters; it watches every site you have on
  (`/sites`, all on by default). A site you turn on is indexed silently first.
- Only news: a listing (re)published after you subscribed, or dates created
  after you subscribed. On THS an old listing the bot merely sees for the first
  time (or a sit that only re-opens) is noted silently, never alerted.
  MindMyHouse has no publication times or date ids, so there any sit or date
  range not on the site when you subscribed counts as new — including edited
  dates.
- One new sit arrives as a card with a photo; several at once as one table
  (dates, days, place; nearest dates first), where a number opens the card in
  the same message and ◀ ▶ flip through them.
- Multiple users, each with their own subscriptions (`/new`, `/list`) and sites (`/sites`).
- Access via single-use invite codes.
- Filters: continent, country (or "all except…"), a date window (the sit must
  fit entirely inside it), min/max days, pets ("has at least one of").
- Optional residential proxy (e.g. Decodo): a fallback by default, or always-on.

> **Note:** this uses an undocumented internal API of TrustedHousesitters and
> the HTML of MindMyHouse, and may break whenever they change them. Automated
> access may be against their terms of service — keep the request rate low and
> use it at your own risk.

## How it works

```
Telegram ⇄ aiogram (long polling) ── invite-code gate → /new (FSM) → /list, /sites
                │
             SQLite ⇄ Worker (asyncio, ~30 min ± 10%) → sources["ths" | "mmh"].search()
                         · each subscription × each site its chat has on
                         · identical (site, filter) pairs share one request
                         · baseline per subscription and site: everything current
                           is silently "seen" (new, resumed, or the site just turned on)
                         · page 1 every tick (the whole filter if ≤ 300 results),
                           full crawl every 3 h / after downtime
                         · dedup by date-slot id, marked only after a successful send
                         · news = published after the baseline, or a slot id above
                           the highest one the baseline saw (THS ids grow over time)
```

| File | Purpose |
|---|---|
| `petalert/models.py` | `Filter`, `Listing`, `Slot`, local filtering (authoritative; the API filter is only an optimization) |
| `petalert/sources/ths.py` | THS query and parsing, `api-version` self-healing on 406 |
| `petalert/sources/mmh.py` | MindMyHouse page parsing, its country → continent table |
| `petalert/http.py` | retries, backoff, proxy, at most one request every 2 s |
| `petalert/scheduler.py` | worker: grouping, baseline, dedup, delivery, admin health alerts |
| `petalert/db.py` | SQLite: subscriptions, per-site baselines (`watch`), sites per chat (`chat_source`), `delivered`, `query_state`, alert tables, invite codes |
| `petalert/bot/` | dialog, management, access gate, message texts, sending |

**Adding a site:** a module in `petalert/sources/` implementing `Source`
(`countries`, `search` → normalized `Listing`s), plus a line in `REGISTRY`.
Every subscription then watches it (indexing it silently first), and users can
turn it off in `/sites`. A site whose one search returns everything sets
`always_complete = True`, so the worker schedules no extra full crawls for it.

## Running locally

```bash
uv sync --extra dev
cp .env.example .env        # BOT_TOKEN of a separate dev bot, ADMIN_USER_IDS = your user_id
uv run pytest               # unit tests
PETALERT_LIVE=1 uv run pytest tests/test_live_*.py     # live sites: catches drift

# what the bot sees, without Telegram
uv run python -m petalert.sources countries --continent europe
uv run python -m petalert.sources search --continent europe --country portugal --min-days 14
uv run python -m petalert.sources search --continent europe --exclude united-kingdom
uv run python -m petalert.sources --source mmh search --continent europe

uv run python -m petalert codes generate 5   # codes + deep links t.me/<bot>?start=<code>
POLL_INTERVAL_MIN=1 uv run python -m petalert
```

Use a **separate dev bot token** locally: two processes polling the same token
fight over updates (`TelegramConflictError`), and both would send alerts.
You can find your `user_id` via @userinfobot. Admins listed in
`ADMIN_USER_IDS` get in without a code.

## Invite codes

Codes live in the database of the bot that will check them, so generate them
where that bot runs — on the server for production:

```bash
ssh $HOST 'cd /srv/petalert && sudo -u petalert .venv/bin/python -m petalert codes generate 100'
ssh $HOST 'cd /srv/petalert && sudo -u petalert .venv/bin/python -m petalert codes list'   # still unused
```

A code is single-use: it grants one Telegram account access for good. The
person either opens the deep link or sends the code to the bot as text
(`K7QM-4XPA`; case and dash don't matter).

## Deploying (VPS, systemd)

| What | Where | Owner |
|---|---|---|
| code and venv | `/srv/petalert` (git checkout) | `deploy` |
| `.env` | `/srv/petalert/.env` | `deploy:petalert`, 640 |
| SQLite | `/var/lib/petalert/petalert.sqlite3` | `petalert` |
| unit | `/etc/systemd/system/petalert.service` | root |

**THS blocks datacenter IPs** (from DigitalOcean every page returns 403 from an
AWS WAF), so on a VPS you need `PROXY_URL` (a residential rotating proxy) and
`PROXY_MODE=always`. Traffic is ~110–120 KB (gzip) per page of 100 listings, per
distinct filter: ~6 MB/day for the page-1 ticks, plus a full crawl every
`FULL_CRAWL_HOURS` (3) of about `results / 100` pages. Excluded countries are
dropped locally, so they still count: "Europe except the UK" fetches all
~2100 European results, ~22 pages ≈ 2.5 MB per crawl, ~25 MB/day ≈ 0.75 GB/month
in total. A single country is usually one request. Subscriptions with the same
filter share the requests; at most 4 scheduled full crawls run per tick.
MindMyHouse is one ~45 KB request per tick whatever the filters (~2 MB/day).

First deployment (on the server, as a sudo user — `deploy` here):

```bash
sudo useradd --system --home /var/lib/petalert --shell /usr/sbin/nologin petalert
sudo install -d -o petalert -g petalert -m 750 /var/lib/petalert
sudo install -d -o deploy -g deploy /srv/petalert
git clone https://github.com/VladislavKorzh/petalert.git /srv/petalert
cd /srv/petalert && uv sync --frozen --no-dev --python /usr/bin/python3.12
sudo install -m 640 -o deploy -g petalert .env.example .env   # then fill in; DB_PATH=/var/lib/petalert/petalert.sqlite3
sudo -u petalert .venv/bin/python -m petalert codes generate 100 > ~/petalert-codes.txt
sudo cp deploy/petalert.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now petalert
journalctl -u petalert -f
```

Updating (`HOST` is the server's ssh alias):

```bash
ssh $HOST 'cd /srv/petalert && git pull --ff-only \
  && uv sync --frozen --no-dev --python /usr/bin/python3.12 \
  && sudo systemctl restart petalert'
```

**Re-indexing a site.** After a change on our side that makes old listings
matchable for the first time — e.g. a country added to `COUNTRIES` in
`sources/mmh.py` (the bot tells the admin when it meets an unknown one) — every
subscription must mark what matches there now as seen, or those listings would
be alerted as new. The bot checks right after it starts, so do it between stop
and start, chained so a failed step never starts the bot:

```bash
ssh $HOST 'cd /srv/petalert && sudo systemctl stop petalert && git pull --ff-only \
  && uv sync --frozen --no-dev --python /usr/bin/python3.12 \
  && sudo -u petalert .venv/bin/python -m petalert rebaseline mmh \
  && sudo systemctl start petalert'
```

It is quiet (no messages to users); sits that appeared since the last check
are marked seen too.

If `deploy/petalert.service` changed, also `sudo cp` it into
`/etc/systemd/system/` and run `sudo systemctl daemon-reload` before restarting.

Each tick logs `groups`, `full`, `listings`, `fresh`, `sent`, `failed`,
`full_crawl_new`.

## Health and risks

- **A source fails** 3 ticks in a row (~1.5 h) → a message to `ADMIN_CHAT_ID`;
  another one once it recovers.
- **`api-version` changed** → the bot re-reads it from the THS page and tells
  the admin; update `THS_API_VERSION` in `.env` too.
- **401** from THS means the Referer/auth requirements changed — the code needs
  a fix.
- **A user blocked the bot** → their subscriptions are paused.
- **A subscription's date window is over** → it is paused and the user is told.
- **THS refuses to page past 1000 results**, so full crawls page with a cursor
  (`published.before` the last listing seen) instead of page numbers: no cap,
  and listings leaving the feed mid-crawl don't hide others.
- **New dates without a bump.** New listings and most new dates appear at the
  top of the `published` feed, which every tick reads. But owners sometimes add
  dates to an old listing without bumping it (~0.5% of new slots, measured
  2026-10-17); those are found by the full crawl, so they arrive up to
  `FULL_CRAWL_HOURS` late (immediately for filters with ≤ 300 results, which are
  fetched whole every tick). `full_crawl_new` in the log counts new slots a
  full crawl found beyond page 1 — a steady 0 means a longer interval is fine.
- **If THS stops honouring the cursor** (`filters.published`), full crawls fail
  loudly (admin alert after 3 ticks) instead of quietly returning page 1.
- **Listings with no `published` date** (roughly 1 in 1000) sort after everything
  and can't be reached by the cursor. The full crawl reads them from the last
  plain pages (above 1000 results: per country, found by comparing the country
  facet with and without a cursor); only undated listings in a single country
  with more than 1000 results (e.g. the UK) stay out of reach.
- **Database backups** are not part of this repo. A consistent copy, safe while
  the bot runs (put it in a daily timer, or enable server snapshots):
  `sudo -u petalert /srv/petalert/.venv/bin/python -c "import sqlite3; sqlite3.connect('/var/lib/petalert/petalert.sqlite3').backup(sqlite3.connect('/var/lib/petalert/backup.sqlite3'))"`
