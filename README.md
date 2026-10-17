# petalert

A Telegram bot that checks house-sitting sites every ~30 minutes and messages
you when **new open dates** match your filters — both on brand-new listings and
on existing listings where the owner has added new dates. The only source so
far is [TrustedHousesitters](https://www.trustedhousesitters.com) (the search
page's internal JSON API, no login).

- Multiple users, each with their own subscriptions (`/new`, `/list`).
- Access via single-use invite codes.
- Filters: continent, country (or "all except…"), a date window (the sit must
  fit entirely inside it), min/max days, pets ("has at least one of").
- Optional residential proxy (e.g. Decodo): a fallback by default, or always-on.

> **Note:** this uses an undocumented internal API of TrustedHousesitters and
> may break whenever they change it. Automated access may be against their
> terms of service — keep the request rate low and use it at your own risk.

## How it works

```
Telegram ⇄ aiogram (long polling) ── invite-code gate → /new (FSM) → /list
                │
             SQLite ⇄ Worker (asyncio, ~30 min ± 10%) → sources["ths"].search()
                         · identical filters share one request
                         · new subscription baseline: everything current is silently "seen"
                         · page 1 every tick, full crawl daily / after downtime
                         · dedup by date-slot id, marked only after a successful send
```

| File | Purpose |
|---|---|
| `petalert/models.py` | `Filter`, `Listing`, `Slot`, local filtering (authoritative; the API filter is only an optimization) |
| `petalert/sources/ths.py` | THS query and parsing, `api-version` self-healing on 406 |
| `petalert/http.py` | retries, backoff, proxy, at most one request every 2 s |
| `petalert/scheduler.py` | worker: grouping, baseline, dedup, delivery, admin health alerts |
| `petalert/db.py` | SQLite: subscriptions, `delivered`, `query_state`, invite codes |
| `petalert/bot/` | dialog, management, access gate, message texts, sending |

**Adding a site:** a module in `petalert/sources/` implementing `Source`
(`countries`, `search` → normalized `Listing`s), plus a line in `REGISTRY` and
`build_sources`. The dialog shows a site picker automatically once there are two.

## Running locally

```bash
uv sync --extra dev
cp .env.example .env        # BOT_TOKEN of a separate dev bot, ADMIN_USER_IDS = your user_id
uv run pytest               # unit tests
PETALERT_LIVE=1 uv run pytest tests/test_live_ths.py   # live THS: catches API drift

# what the bot sees, without Telegram
uv run python -m petalert.sources countries --continent europe
uv run python -m petalert.sources search --continent europe --country portugal --min-days 14
uv run python -m petalert.sources search --continent europe --exclude united-kingdom

uv run python -m petalert codes generate 5   # codes + deep links t.me/<bot>?start=<code>
POLL_INTERVAL_MIN=1 uv run python -m petalert
```

You can find your `user_id` via @userinfobot. Admins listed in
`ADMIN_USER_IDS` get in without a code.

## Invite codes

```bash
uv run python -m petalert codes generate 100   # a pack for 100 people
uv run python -m petalert codes list           # still unused
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
`PROXY_MODE=always`. Traffic is ~32 KB (gzip) per page of 100 listings, i.e.
~2 MB per day per filter.

First deployment (on the server, as a sudo user — `deploy` here):

```bash
sudo useradd --system --home /var/lib/petalert --shell /usr/sbin/nologin petalert
sudo install -d -o petalert -g petalert -m 750 /var/lib/petalert
sudo install -d -o deploy -g deploy /srv/petalert
git clone https://github.com/VladislavKorzh/petalert.git /srv/petalert
cd /srv/petalert && uv sync --frozen --no-dev --python /usr/bin/python3.12
cp .env.example .env && chgrp petalert .env && chmod 640 .env   # fill in; DB_PATH=/var/lib/petalert/petalert.sqlite3
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
- **THS limit: 1000 results per search.** For a broad filter (all of Europe,
  ~2650) the baseline and the daily crawl see the 1000 newest. An older listing
  triggers an alert only once it's re-published and moves to the top.
- **Assumption:** new dates bump a listing to the top of the feed (`published`).
  The daily full crawl is a safety net, and `full_crawl_new` in the log shows
  whether it ever finds anything.
