# petalert

A Telegram bot that checks house-sitting sites every ~30 minutes and messages
you when **new open dates** match your filters — both on brand-new listings and
on existing listings where the owner has added new dates. Sources, no login
needed for any:

- [TrustedHousesitters](https://www.trustedhousesitters.com) — the search
  page's internal JSON API;
- [MindMyHouse](https://www.mindmyhouse.com) — the search page's HTML: its map
  script lists every assignment on the site (~200), so one request covers it;
- [Nomador](https://www.nomador.com) — its map API lists every open stay
  (~820) in one request; places, dates and pets come from the result pages,
  read in full every 3 hours, and in between from the page of each new stay.

- A subscription is a set of filters; it watches every site you have on
  (`/sites`, all on by default). A site you turn on is indexed silently first.
- Only news: a listing (re)published after you subscribed, or dates created
  after you subscribed. On THS an old listing the bot merely sees for the first
  time (or a sit that only re-opens) is noted silently, never alerted. Nor is
  a sit already sent whose dates the host deleted and added again.
  MindMyHouse and Nomador have no publication times or growing ids, so there
  any sit or date range not on the site when you subscribed counts as new
  (on MindMyHouse, edited dates too).
- One new sit arrives as a card with a photo; several at once as one table
  (dates, days, place; nearest dates first; 50 a page), where a number opens
  the listing on its site and ◀ ▶ flip the pages. The number buttons say the site
  too, `1(T)`, `2(N)`, and the line under the table names them.
- `/search` for what is open now, by start date — see [Search](#search).
- Multiple users, each with their own subscriptions (`/new`, `/list`) and sites (`/sites`).
- Access via single-use invite codes.
- Filters: continent, country (or "all except…"), a date window (the sit must
  fit entirely inside it), min/max days, pets ("has at least one of").
- Optional residential proxy (e.g. Decodo): a fallback by default, or always-on.

> **Note:** this uses undocumented internal APIs of TrustedHousesitters and
> Nomador and the HTML of MindMyHouse and Nomador, and may break whenever they
> change them. Nomador's Cloudflare turns away Python's own TLS handshake, so
> those requests present Chrome's (curl_cffi). Automated access may be against
> their terms of service — keep the request rate low and use it at your own risk.

## How it works

```
Telegram ⇄ aiogram (long polling) ── invite-code gate → /new (FSM) → /list, /sites
                │
             SQLite ⇄ Worker (asyncio, ~30 min ± 10%) → sources["ths" | "mmh" | "nmd"].search()
                         · each subscription × each site its chat has on
                         · identical (site, filter) pairs share one request
                         · baseline per subscription and site: everything current
                           is silently "seen" (new, resumed, or the site just turned on)
                         · page 1 every tick (the whole filter if ≤ 300 results),
                           full crawl every 3 h / after downtime
                         · dedup by date-slot id and by listing + dates,
                           marked only after a successful send
                         · news = published after the baseline, or a slot id above
                           the highest one the baseline saw (THS ids grow over time)
```

| File | Purpose |
|---|---|
| `petalert/models.py` | `Filter`, `Listing`, `Slot`, local filtering (authoritative; the API filter is only an optimization) |
| `petalert/search.py` | `/search`: the query, reading the sites by start date (`Source.by_start`) |
| `petalert/geo.py` | ISO country codes → names and continents (as THS groups them) |
| `petalert/sources/ths.py` | THS query and parsing, `api-version` self-healing on 406 |
| `petalert/sources/mmh.py` | MindMyHouse page parsing, its country → continent table |
| `petalert/sources/nmd.py` | Nomador: the map, the result pages crawled by stay length, stay pages; an index of the site kept between crawls |
| `petalert/http.py` | retries, backoff, proxy, Chrome's TLS handshake on request (curl_cffi), at most one request every 2 s |
| `petalert/scheduler.py` | worker: grouping, baseline, dedup, delivery, admin health alerts |
| `petalert/db.py` | SQLite: subscriptions, per-site baselines (`watch`), sites per chat (`chat_source`), `delivered`, `query_state`, alert tables, invite codes |
| `petalert/bot/` | dialog, management, access gate, message texts, sending |

**Adding a site:** a module in `petalert/sources/` implementing `Source`
(`countries`, `search`, `by_start` → normalized `Listing`s), plus a line in `REGISTRY`.
Every subscription then watches it (indexing it silently first), and users can
turn it off in `/sites`. A site whose one search returns everything sets
`always_complete = True`, so the worker schedules no extra full crawls for it.
Table buttons mark a site with the first letter of its `title`: give a new site
one that no other site starts with.

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
uv run python -m petalert.sources --source nmd search --continent europe   # crawls first: ~2 min
uv run python -m petalert search "eu(-uk), ±3, 30.11.2026 -"   # what /search finds

uv run python -m petalert codes generate 5   # codes + deep links t.me/<bot>?start=<code>
POLL_INTERVAL_MIN=1 uv run python -m petalert
```

Use a **separate dev bot token** locally: two processes polling the same token
fight over updates (`TelegramConflictError`), and both would send alerts.
You can find your `user_id` via @userinfobot. Admins listed in
`ADMIN_USER_IDS` get in without a code.

## Search

`/search` (or just a message with a date in it) lists the open sits by **start
date** on the sites you have on, as the same table as the alerts — a number
opens the sit on its site:

| Query | Finds sits starting |
|---|---|
| `eu(-uk), ±3, 30.11.2026` | in Europe except the UK, 27 Nov – 3 Dec |
| `eu(-uk), +5, 30.11.2026` | the same, 30 Nov – 5 Dec (`-2`: 28 – 30 Nov; `+5 -2` both) |
| `eu(-uk), ±3, 30.11.2026 -` | the same, from 27 Nov on (nearest first) |
| `pt, es, 1.12 - 20.12` | in Portugal or Spain, 1 – 20 Dec (no year: the next one) |
| `north america 2026-12-01` | in North America on 1 Dec |
| `eu, 1.12 - 20.12, min 14` | in Europe, 1 – 20 Dec, 14 days or longer |

- Places: `eu`/`europe`, `asia`, `africa`, `oceania`, `north america`,
  `south america`; countries by ISO code (`pt`, `uk` = `gb`) or name;
  `-xx` (also inside `eu(…)`) leaves a country out. No place = anywhere.
  Matching is by the listing's country code, which every site gives.
- `±N` (`+-N`, `+/-N`) widens the range of start dates on both sides, `+N`
  only after, `-N` only before (up to 180 days each); how long a sit lasts
  doesn't matter (but see `min N`). A search starts from tomorrow.
- `min N` (`min 14`, `min 14 days`) keeps sits of at least N days, counted as
  the sites count them (end − start). THS gets it in the query and then sorts
  by the first slot that long, so a long minimum reads fewer pages.
- At most 100 sits, nearest start first; the reply says when there are more.

THS has a "Start date" sort (`SIT_START_DATE`, found in the site's JS), so a
search reads one continent or country nearest-start first and stops once it is
past the range or has 100 matches — usually one or two pages (~110 KB each
through the proxy), not a crawl. Plain page numbers reach 1000 listings; a
search that needs more says so. If THS ever stops honouring the sort, the
search reports THS as failed and the admin hears about it. MindMyHouse and
Nomador answer from what they have cached (Nomador: its index of the site).

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
MindMyHouse is one ~45 KB request per tick whatever the filters, and it goes
direct: the site doesn't block datacenter IPs, so the proxy is only its fallback.
Nomador goes direct too, whatever the filters: its map (~26 KB) once a tick,
the page of each new stay (~27 KB, ~40 a day), and every `FULL_CRAWL_HOURS` a
crawl of ~85 result pages (~5 KB each, ~3 minutes at one request every 2 s).
Its Cloudflare turns away Python's own TLS handshake whatever the IP, which is
why those requests go through curl_cffi as Chrome; whether it also blocks
datacenter IPs is unknown — if it does, the 403s retry through the proxy.

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
`sources/mmh.py`, or a country name to `geo.py` for Nomador (the bot tells the
admin when it meets an unknown one) — every subscription must mark what matches
there now as seen, or those listings would be alerted as new. The bot checks
right after it starts, so do it between stop and start, chained so a failed
step never starts the bot:

```bash
ssh $HOST 'cd /srv/petalert && sudo systemctl stop petalert && git pull --ff-only \
  && uv sync --frozen --no-dev --python /usr/bin/python3.12 \
  && sudo -u petalert .venv/bin/python -m petalert rebaseline mmh \
  && sudo systemctl start petalert'
```

It is quiet (no messages to users); sits that appeared since the last check
are marked seen too. For Nomador it is `rebaseline nmd`.

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
- **Nomador after a restart** needs a few minutes to index the site. Until
  then `/new` lists only the other sites' countries and `/search` reports
  Nomador as failed ("try again later"); the worker's first tick waits for it.
- **Nomador's result pages stop at 500 stays** per search (a later page repeats
  page 50), so its crawl splits the site by stay length (`BANDS` in
  `sources/nmd.py`: ~270, ~220 and ~330 open stays on 2026-10-21). A band that
  fills all 50 pages is reported to the admin; the stays it can't reach are
  still read one by one from the map, 30 a tick. `test_live_nmd.py` fails
  before that, once a band passes 400.
- **Nomador HTTP 403 "blocked by a Cloudflare challenge"** means Chrome's
  handshake no longer passes either: update curl_cffi
  (`uv lock --upgrade-package curl-cffi`) and run `test_live_nmd.py`.
- **Database backups** are not part of this repo. A consistent copy, safe while
  the bot runs (put it in a daily timer, or enable server snapshots):
  `sudo -u petalert /srv/petalert/.venv/bin/python -c "import sqlite3; sqlite3.connect('/var/lib/petalert/petalert.sqlite3').backup(sqlite3.connect('/var/lib/petalert/backup.sqlite3'))"`
