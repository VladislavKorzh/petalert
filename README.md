# petalert

Telegram-бот, який кожні ~30 хв перевіряє сайти house-sitting і пише, коли під
фільтри підписки з'являються **нові відкриті дати**: і в нових оголошеннях, і
в старих, куди власник додав нові дати. Поки що одне джерело:
TrustedHousesitters (внутрішній JSON API сторінки пошуку, без логіну).

- Кілька користувачів, у кожного свої підписки (`/new`, `/list`).
- Доступ за одноразовими інвайт-кодами.
- Фільтри: континент, країна, вікно дат (сит має повністю в нього вміщатися),
  мін./макс. днів, тварини («є хоч одна з»).
- Проксі Decodo опційно: за замовчуванням лише запасний шлях після 403/429/5xx.

## Як це влаштовано

```
Telegram ⇄ aiogram (long polling) ── гейт інвайт-кодів → /new (FSM) → /list
                │
             SQLite ⇄ Worker (asyncio, ~30 хв ± 10%) → sources["ths"].search()
                         · однакові фільтри = один запит
                         · baseline нової підписки: усе поточне мовчки «бачене»
                         · сторінка 1 щотіку, повний обхід раз на добу / після простою
                         · dedup за id проміжку дат, позначаємо лише після відправки
```

| Файл | Що робить |
|---|---|
| `petalert/models.py` | `Filter`, `Listing`, `Slot`, локальна фільтрація (вона вирішальна, API — лише оптимізація) |
| `petalert/sources/ths.py` | запит і розбір THS, самолікування `api-version` при 406 |
| `petalert/http.py` | ретраї, backoff, проксі, не частіше 1 запиту на 2 с |
| `petalert/scheduler.py` | воркер: групування, baseline, dedup, доставка, health-алерти адміну |
| `petalert/db.py` | SQLite: підписки, `delivered`, `query_state`, інвайт-коди |
| `petalert/bot/` | діалог, керування, гейт доступу, тексти, відправка |

**Новий сайт:** модуль у `petalert/sources/`, що реалізує `Source`
(`countries`, `search` → нормалізовані `Listing`), плюс рядок у `REGISTRY` і
`build_sources`. Діалог покаже вибір сайту автоматично, щойно їх стане два.

## Локально

```bash
uv sync --extra dev
cp .env.example .env        # BOT_TOKEN — окремий dev-бот, ADMIN_USER_IDS — ваш user_id
uv run pytest               # юніт-тести
PETALERT_LIVE=1 uv run pytest tests/test_live_ths.py   # живий THS: ловить дрейф API

# що бачить бот, без Telegram
uv run python -m petalert.sources countries --continent europe
uv run python -m petalert.sources search --continent europe --country portugal --min-days 14

uv run python -m petalert codes generate 5   # коди + deep link t.me/<bot>?start=<код>
POLL_INTERVAL_MIN=1 uv run python -m petalert
```

Свій `user_id` можна дізнатися в @userinfobot. Адміни з `ADMIN_USER_IDS`
заходять без коду.

## Інвайт-коди

```bash
uv run python -m petalert codes generate 100   # пак на 100 людей
uv run python -m petalert codes list           # ще не використані
```

Код одноразовий: відкриває доступ одному Telegram-акаунту назавжди. Людина
або відкриває deep link, або надсилає код боту текстом (`K7QM-4XPA`,
регістр і дефіс не важливі).

## Деплой (VPS, systemd)

| Що | Де | Власник |
|---|---|---|
| код і venv | `/srv/petalert` (git checkout) | `deploy` |
| `.env` | `/srv/petalert/.env` | `deploy:petalert`, 640 |
| SQLite | `/var/lib/petalert/petalert.sqlite3` | `petalert` |
| юніт | `/etc/systemd/system/petalert.service` | root |

**THS блокує IP дата-центрів** (з DigitalOcean — 403 на весь сайт від AWS WAF),
тож на VPS обов'язково `PROXY_URL` (резидентний rotating, напр. Decodo) і
`PROXY_MODE=always`. Трафік — ~32 КБ (gzip) на сторінку зі 100 оголошень,
тобто ~2 МБ на добу на фільтр.

Перше розгортання (на сервері, від користувача з sudo — тут `deploy`):

```bash
sudo useradd --system --home /var/lib/petalert --shell /usr/sbin/nologin petalert
sudo install -d -o petalert -g petalert -m 750 /var/lib/petalert
sudo install -d -o deploy -g deploy /srv/petalert
git clone https://github.com/VladislavKorzh/petalert.git /srv/petalert
cd /srv/petalert && uv sync --frozen --no-dev --python /usr/bin/python3.12
cp .env.example .env && chgrp petalert .env && chmod 640 .env   # заповнити; DB_PATH=/var/lib/petalert/petalert.sqlite3
sudo -u petalert .venv/bin/python -m petalert codes generate 100 > ~/petalert-codes.txt
sudo cp deploy/petalert.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now petalert
journalctl -u petalert -f
```

Оновлення (`HOST` — ssh-аліас сервера):

```bash
ssh $HOST 'cd /srv/petalert && git pull --ff-only \
  && uv sync --frozen --no-dev --python /usr/bin/python3.12 \
  && sudo systemctl restart petalert'
```

Якщо змінився `deploy/petalert.service` — ще `sudo cp` його в
`/etc/systemd/system/` і `sudo systemctl daemon-reload` перед рестартом.

У лозі кожного тіку: `groups`, `full`, `listings`, `fresh`, `sent`, `failed`,
`full_crawl_new`.

## Здоров'я й ризики

- **Джерело лежить** 3 тіки поспіль (~1.5 год) → повідомлення в `ADMIN_CHAT_ID`;
  після відновлення — ще одне.
- **`api-version`** змінився → бот сам перечитує його зі сторінки THS і
  повідомляє адміна; варто оновити `THS_API_VERSION` у `.env`.
- **401** від THS означає, що змінились вимоги до Referer/авторизації — потрібна
  правка коду.
- **Користувач заблокував бота** → його підписки стають на паузу.
- **ToS THS** автоматизований доступ, імовірно, забороняє. Навантаження
  мінімальне (запит на унікальний фільтр раз на 30 хв + добовий обхід), бот
  шле лише посилання. Проксі ускладнює блокування, але не робить доступ
  дозволеним.
- **Ліміт THS: 1000 результатів на пошук.** Для широкого фільтра (уся Європа
  ~2650) baseline і добовий обхід бачать 1000 найновіших. Старіше оголошення
  дасть алерт, лише коли його перепублікують і воно підніметься нагору.
- **Припущення:** нові дати піднімають оголошення нагору стрічки (`published`).
  Добовий повний обхід страхує, а `full_crawl_new` у лозі показує, чи він щось
  взагалі знаходить.
