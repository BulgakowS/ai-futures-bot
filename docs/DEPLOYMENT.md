# Розгортання й операційна модель

## Локальне середовище

S02 реалізував `compose.yaml` із `api`, `trading`, `postgres`, `redis`; S06 додав локальний Next.js `web`, а S07 — опціональний AI backend, вимкнений за замовчуванням. `make setup` створює локальний `.env`, `docker compose -p ai-futures up -d --build` запускає API, worker і web. Образи PostgreSQL 16.9, Redis 7.4.4, Python 3.12 та Node.js 22 підтримують ARM64.

Локальні адреси: `localhost:3030` для UI і `localhost:8000` для API/OpenAPI. Web і API прив'язані лише до `127.0.0.1`; PostgreSQL/Redis без published ports. Дані БД і потрібні журнали — у persistent volumes; backend hot reload — через `compose.dev.yaml` (`make dev`). `.env.example` містить placeholders; `make setup` генерує local DB/auth secrets у gitignored `.env` з mode 0600. Контейнер web читає `BACKEND_AUTH_TOKEN` лише server-side та звертається до `http://api:8000`; браузер бачить тільки same-origin BFF. Для локальної розробки web: `cd apps/web && npm ci && BACKEND_URL=http://127.0.0.1:8000 BACKEND_AUTH_TOKEN=<local secret> npm run dev` із секретом у локальному середовищі, не в shell history. Публічний хостинг або LAN доступ не налаштовані.

## Конфігурація й секрети

`make setup` створює ignored `.env.credentials` з mode 0600 і 32-byte `CREDENTIAL_MASTER_KEY` у URL-safe base64; Compose додає цей файл лише до `api` container. `trading` примусово очищує цю змінну, `web` отримує лише свій backend token. Файл не класти в БД/backup БД або спільну `.env`; втрачений master key робить active ciphertext невідновним. Відсутній/невалідний/невідповідний ключ повертає 503 на credential API. Не виводити `docker compose config` або env у журнали. Master-key rotation, перенос на інший host, encrypted backup і WAL retention потребують окремого operational рішення; сам факт наявності credential не відкриває Live.

Конфігурація має відділяти `APP_MODE=paper` від серверного Live gate. MEXC public market data, за підтвердження доступу, не вимагає приватного ключа. OpenAI й Telegram — опціональні. Пізніше Live-ключ матиме окремий секрет, мінімальні дозволи, без withdrawal, ротацію й перевірку прав. Секрети не передаються браузеру, не логуються і не потрапляють до backup у відкритому вигляді.

## Health і операції

Readiness перевіряє БД/міграції та доступність потрібних worker-ів; liveness лише живість процесу. UI показує окремо стан market data, AI, trading і backlog. Логи структуровані з correlation ID без секретів. Метрики: age market data, WS reconnect/gap, latency decision/risk, ордери зі статусом UNKNOWN, ledger mismatch, queue depth, помилки API, AI spend. Резервне копіювання БД, тест restore і процедура аварійної зупинки — критерії етапу 8.

## S09: аварійна зупинка й відновлення локального Paper

1. Зупинити нові рішення: `docker compose -p ai-futures stop trading`. Якщо PostgreSQL доступна, встановити `kill_switch=true` через автентифікований Paper API або `PaperService.kill(True)` із локального backend; перевірити `GET /api/v1/state`. Якщо БД недоступна, worker залишити зупиненим до її відновлення. Не ставити нових ордерів під час невідомого стану.
2. Перевірити `GET /api/v1/health/ready`, market quality, `paper_order` зі статусом `BROKER_UNKNOWN`/`UNKNOWN`, pending orders, `paper_ledger` та `outbox_event`. У поточному Paper немає приватного MEXC broker: невідомий стан не можна вважати виконанням; bot gate блокує нові входи при unresolved order.
3. Зробити резервну копію **поза репозиторієм**: `.venv/bin/python scripts/backup_restore.py --output /private/tmp/ai-futures-YYYYMMDD.dump`. Скрипт створює файл mode 0600, бере consistent `pg_dump -Fc`, відновлює у випадкову окрему `s09_restore_*` БД, перевіряє міграцію, баланс кожного ledger row і FK для paper/backtest, потім видаляє лише тимчасову БД. Існуючий файл не перезаписує. Файл містить торгові дані; зберігати лише в зашифрованому сховищі за локальною політикою та видаляти тимчасові копії. Ця команда **не замінює активну БД**.
4. Для реального recovery з backup спершу створити **нову окрему** PostgreSQL БД, відновити dump у неї через `pg_restore --exit-on-error --no-owner --no-acl`, перевірити ті самі інваріанти й лише потім вручну перевести backend `DATABASE_URL` на перевірену БД. Не запускати два worker-и одночасно та не відновлювати dump поверх активної БД. Після переключення залишити kill switch увімкненим, звірити pending/unknown orders і ledger, запустити API та worker, підтвердити readiness і market reconciliation. Лише після цього оператор може окремо вирішити, чи дозволяти Paper entries.

`scripts/fault_smoke.py` безпечно для **локального Paper з увімкненим kill switch** послідовно зупиняє Redis, PostgreSQL, API та trading, перевіряє деградацію readiness і відновлює кожен сервіс у `finally`. `scripts/short_soak.py --seconds 60 --interval 10` знімає фактичні health/market/CPU/RAM та DB counts без ордерів. Це коротка діагностика, не довгий soak або SLA.

## S09: опціональні Telegram alerts

Офіційний [Telegram Bot API `sendMessage`](https://core.telegram.org/bots/api#sendmessage) перевірено **2026-10-15** через gstack `/browse`: потрібні `chat_id` і `text`, успіх повертає Message. `TELEGRAM_BOT_TOKEN` і `TELEGRAM_CHAT_ID` зберігати лише в локальному secret store/`.env` (обидва або жодного). Самі credentials **не запускають доставку**: `TELEGRAM_DELIVERY_ENABLED=false` за замовчуванням. Монітор і далі створює інциденти в PostgreSQL outbox без Telegram.

Автоматичну доставку можна окремо розглядати лише після рішення користувача щодо каналу та історичного backlog. Перед її ввімкненням зафіксувати явний `TELEGRAM_DELIVERY_CUTOFF_UTC` як наявний, не майбутній ISO 8601 час у UTC (`Z` або `+00:00`) і встановити `TELEGRAM_DELIVERY_ENABLED=true` разом із token/chat; потім rebuild/recreate API. Startup відхиляє відсутній, невалідний, не UTC або майбутній cutoff. Consumer обирає тільки `alert.telegram` із `occurred_at >= cutoff` і `published_at IS NULL`; старіші rows залишаються queued без UPDATE/DELETE. Не обирати cutoff раніше непогоджених історичних подій: значення включне, і такі події були б доставлені. Не комітити токен і не друкувати Compose config/env у evidence. Поточний Stage 8 не має дозволу на фактичне надсилання.

API monitor записує перехід у `market_stale`/`worker_down`; kill switch записує alert в тій самій транзакції, що й стан. Інцидент повторно озброюється після 30 секунд безперервного здорового стану. При окремо ввімкненій доставці consumer відправляє лише фіксований текст і ставить `published_at` після `HTTP 200` та `ok=true` з `result`; при timeout/429/5xx залишає подію для повтору. Можливий дубль після успішного HTTP, але до DB commit; Telegram `sendMessage` не надає idempotency key. API outage зупиняє і монітор, і доставку, тому для цілодобового каналу потрібен зовнішній монітор. Доставку в тестовий канал без токена/каналу не перевірено.

## S11: локальний research host на macOS

Research collector має окремий LaunchAgent `local.ai-futures.s11-research`; згенерувати його після перевірки repo й `.venv` через `.venv/bin/python scripts/run_s11_research.py --print-plist` і встановити в `~/Library/LaunchAgents/`. Plist має `APP_ENV=test`, `RunAtLoad=true`, `StartInterval=60` і ignored `data/research/s11-runner.{stdout,stderr}.log`. In-window запуск записує лише UTC час, anchor, run ID, фазу, статус і сталу reason до `data/research/s11-runner-state.jsonl`; exception text, HTTP payload, ціни та секрети там відсутні. Цей JSONL і watchdog JSONL мають mode 0600, ротацію після 1 MiB до однієї копії `.1`. Для очищення stdout/stderr зупинити LaunchAgent, обнулити файли й запустити його знову. Після перенесення checkout або `.venv` перегенерувати plist.

Окремий awake plist: `.venv/bin/python scripts/run_s11_research.py --print-awake-plist > "$HOME/Library/LaunchAgents/local.ai-futures.s11-awake.plist"`; після огляду виконати `launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ai-futures.s11-awake.plist"` і `launchctl print "gui/$(id -u)/local.ai-futures.s11-awake"`. Він запускає `/usr/bin/caffeinate -is` з `KeepAlive=true`: запобігає idle/system sleep на AC, не тримає екран і не змінює `pmset`. Після підтвердження постійного агента прибрати тимчасовий awake job, якщо він активний. Видалення: `launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ai-futures.s11-awake.plist"`, потім прибрати plist.

`caffeinate -is` не запобігає сну при закритій кришці, втраті AC або вимкненні Mac. `StartInterval=60` не будить Mac. Пропущені UTC anchors не відновлюються. Read-only watchdog: `.venv/bin/python scripts/audit_s11_research.py --anchors 6`; JSON на stdout і в ignored `data/research/s11-watchdog.jsonl`. Exit 0 означає `ok` або поточний `pending` до `+15m`; 2 — missing/late/error/unknown/stale, 1 — помилка audit. Він читає fixed SQLite тільки у `mode=ro/query_only`, не робить GET, backfill, retry чи DB write і не оцінює label заднім числом. Показує claim/result, дві M1 Hour4 та наявність свіжих rules; це не N5c gate і не cost provenance. Не запускати паралельні legacy news collectors; G7a cap лишається чинним.

Для локального audit кожні 5 хвилин з останніми двома UTC anchors, без повідомлень або мережі, згенерувати й оглянути окремий plist, потім встановити:

```sh
.venv/bin/python scripts/run_s11_research.py --print-audit-plist > "$HOME/Library/LaunchAgents/local.ai-futures.s11-audit.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ai-futures.s11-audit.plist"
launchctl print "gui/$(id -u)/local.ai-futures.s11-audit"
```

Цей LaunchAgent має `RunAtLoad=true`, `StartInterval=300`, запускає лише `scripts/audit_s11_research.py --anchors 2`; stdout/stderr — ignored `data/research/s11-audit.{stdout,stderr}.log`. Exit 2 лишається видимим у `launchctl print` і JSONL, автоматичних повідомлень немає. Для видалення: `launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ai-futures.s11-audit.plist"`, потім прибрати plist. Перед довгою роботою періодично очищувати stdout/stderr після зупинки агента; structured JSONL має ротацію 1 MiB + `.1`.

## Майбутнє перенесення

Тривалий торговий worker має працювати на постійному хості (наприклад, VPS). Можлива гібридна схема: UI на Vercel, API/engine/DB на VPS, але її треба окремо спроєктувати з автентифікацією, TLS, секретами, мережевими межами й операційними витратами. Вона не входить до локального MVP і не змінює server-side risk gate.
