# Модель даних

PostgreSQL — джерело істини для конфігурацій, позицій, ордерів, fills, ledger та журналу рішень. Redis призначений для черг, короткоживучого cache і fan-out; його очищення не повинно втратити баланс або створити нові угоди. Нижче — логічна модель, не схема реалізованої БД.

## Основні сутності

| Сутність | Ключові поля / зв'язки | Правило |
|---|---|---|
| `market_instrument` | canonical ID, exchange symbol, contract metadata version, status | Історичні версії не переписувати |
| `market_event` / `candle` | instrument, source/receive time, sequence, quality, raw ref | Унікальність за ключем джерела; provenance |
| `portfolio` | ID, mode, currency, start balance, epoch, status | Ізоляція режимів і ботів |
| `bot` / `bot_config_version` | portfolio, strategy, risk profile, AI policy, status | Запуск посилається на незмінну версію |
| `decision` | bot, context ID, strategy/AI output, reason, timestamp | Записувати також WAIT і помилки |
| `risk_verdict` | decision/intent, rules version, status, adjustments, codes | Обов'язковий перед ордером |
| `order_intent` / `order` | idempotency key, broker mode, state, amount, price | Окремий intent і стан ордера |
| `fill` | order, price, quantity, fee, source event | Append-only, дедуплікація fill ID |
| `position` | portfolio, instrument, side, quantity, entry, margin, SL/TP | Проєкція з fills і ledger |
| `ledger_transaction` / `ledger_entry` | portfolio, account, amount, currency, event ID | Append-only, дебети = кредити |
| `funding_event` | position, rate, amount, source time, source | Не вигадувати при відсутності даних |
| `backtest_run` / `dataset` | checksums, config versions, seed, metrics | Відтворюваність |
| `audit_event` / `outbox_event` | actor, action, correlation ID, payload ref | Незмінний причинний слід |

## Цілісність

Кожен `order` має попередній `risk_verdict` з режимом і портфелем, що збігаються. Кожен `fill` належить одному ордеру; `filled_quantity <= ordered_quantity`. Кожен ledger transaction збалансований за валютою. Унікальні ключі оберігають market event, команду, ордер і fill від повтору. Cash/equity-проєкції відтворюються з ledger; розбіжність спричиняє halt. Всі суми зберігаються як exact decimal, час — UTC з timezone.

## Ізоляція й зберігання

Paper, backtest і live мають окремі namespace/режимні обмеження. Запит UI ніколи не поєднує баланси різних режимів без явно позначеної агрегації. Market raw data можуть мати обмежений retention, але посилання, checksum та мінімальний snapshot для аудиту рішень зберігаються за визначеною політикою. Політику retention, обсяг історії та backup encryption треба затвердити на етапі 0/1. Секрети не зберігати в цій схемі у відкритому вигляді.

## Міграції та відновлення

Кожна зміна схеми має forward migration і перевірений backup/restore сценарій. Після рестарту система читає незавершені ордери, звіряє їх із брокером відповідного режиму, відновлює проєкції та не запускає нові входи, доки невідомі стани не вирішені. Для paper reconciliation використовувати внутрішній журнал подій; для майбутнього live — підтверджені дані біржі.

## Foundation S02

Фактична початкова Alembic revision `6ea272857462`: `app_state` (paper-only singleton), `worker_heartbeat`, `audit_event`, `outbox_event`. Bootstrap атомарно створює AppState та одну audit/outbox пару; повтор не переписує стан. `Database.sessions()` — async read/session, `Database.sessions.begin()` — atomic commit/rollback. `Base` і `UTCDateTimeType` знаходяться в `ai_futures.db`, моделі — `ai_futures.models`. Нові моделі імпортуються у Alembic env і додаються окремими migrations. Жодної domain таблиці/торгового seed ще немає; append-only enforcement і outbox delivery будуть додані разом із domain.

S03 migration `53dc9d872e11`: `instrument_rules` (symbol PK, raw/version/validated), `market_snapshot` (symbol+kind PK, source_time/received_at/event/config), `candle` (symbol+timeframe+window_start PK, final/payload hash/event). JSON financial fields — decimal strings; contract quantity canonical. Cache mutable; торговий append-only ledger додасть S04.

S05 migrations `6b355247a35e` і `17ac924e1cd9`: `bot` (paper-only, один портфель на бот, lifecycle/current watermark), `bot_config` (PK bot+version, PostgreSQL immutable trigger), `bot_decision` (унікальний bot+timeframe+window, candle ID/hash, `PROCESSING|DONE|ERROR`, proposal, стратегічна причина, окрема `risk_reason`, paper IDs). Завершене рішення захищене PostgreSQL trigger від UPDATE/DELETE; `PROCESSING` можна завершити після recovery. Portfolio і bot config створюються в одній DB transaction. Дублікат final candle/window не породжує другого рішення чи ордера, а pending proposal повторно відправляється у PaperService із незмінним idempotency key.

S07 migration `89a1e19e9d07`: `ai_analysis` має глобальний PK `analysis_id`, portfolio FK, hash request, model/prompt version, sanitized context/proposal, `REQUESTED|PENDING_PAPER|DONE`, UTC timestamps/config version та paper причинні IDs. `ai_usage` має 1:1 FK analysis, attempted flag, Decimal-string USD reservation/estimate й token counts. Обидва журнали ізольовані portfolio ID; cap серіалізується блокуванням paper portfolio row. Raw provider response, prompt текст і ключ не зберігаються. PostgreSQL append-only trigger для AI журналу та retention policy ще не додано; завершений рядок сервіс не змінює, але це поки не захищено на рівні DB.

S08 migration `d7d5a8b4c601`: `backtest_dataset` містить checksum (unique), symbol/timeframe/source, JSON заморожених final candles із source/receive UTC, event IDs/hash, gap, rules snapshot/version і funding settlements; `backtest_run` має `mode=backtest` DB check, FK dataset, report checksum і JSON окремого рахунку/позицій/рішень/intent/verdict/order/fill/ledger/equity/metrics. Ці таблиці не посилаються на paper portfolio/order/ledger, тому replay не може через FK чи сервіс змінити Paper. API не дозволяє UPDATE/DELETE, але PostgreSQL immutable triggers та незалежна реконструкція звіту з нормалізованих подій ще відкриті.

S09 migrations `a8195da109f2` і `b7638c3920e4`: `alert_state` з ключем `(kind, subject)` фіксує active/changed/recovered UTC для дедуплікації переходів через рестарт. Alert payload у `outbox_event` містить лише фіксований kind/subject; токен Telegram у БД не зберігається. Відновлення перевіряється `scripts/backup_restore.py` у тимчасовій окремій PostgreSQL БД: migration head, кількості paper portfolio/order/fill/ledger і backtest dataset/run, баланс ledger та відсутність orphan FK. Це перевірка backup, не автоматичне переключення активної БД. Політика шифрування, retention і відновлення на іншому хості ще потребують окремого приймання.

S10B/F3 migration `e0e429e4cf31`: `funding_settlement` — public rate journal із PK `(symbol,settle_time)`, Decimal-string rate, `collect_cycle`, raw JSON/hash, source, observed UTC і config version; PostgreSQL trigger забороняє UPDATE/DELETE. `funding_coverage` — один mutable per-symbol verified watermark із `[covered_from,covered_until)`, status, gap UTC, source/hash/observed UTC/config version. Рядки funding спільні як read-only market evidence; cash postings залишаються лише в ізольованих `paper_ledger` епохах, а backtest має окремі dataset/run. Міграція не backfill-ить ставки й початково не припускає покриття; worker має звірити public history. У SQLite unit/integration tests immutability забезпечує сервіс та унікальний ключ, PostgreSQL trigger перевіряється окремо.

S11/N1a migration `a4e7d9c20b61` (після `f7c6482e5a01`): `announcement_version` — окремий від Paper/AI журнал synthetic-only N1a з UUID row ID, SHA-256 canonical MEXC URL ID, `(canonical_id,version_number)` unique, title/language/post/first-seen/fetched/available UTC, canonical payload hash, observation kind, source/schema/config version. Кожна нова версія має `available_at=max(post_time,first_seen_at)`; старий backfill `post_time` не переписує causality. PostgreSQL і SQLite migration triggers забороняють UPDATE/DELETE; на PostgreSQL застосування міграції ще не виконували. `announcement_coverage` — mutable поточний scan per source/language: сторінки, внутрішні gap, unseen count, page hash shift, last success/attempt/error. Він не є доказом повноти мінливого історичного API. HTTP poller і зовнішній feed відсутні; запис сервісу default-off і дозволений лише в `APP_ENV=test` з явним fixture flag. Міграцію прогнали лише на disposable SQLite, не на active Compose.

S11/N4a migration `b8c9e4a01722` після `a4e7d9c20b61`: `news_forecast` містить один append-only decision на BTC_USDT/UTC 4h anchor (unique), deterministic forecast/correlation UUID, фіксований horizon, UTC створення, model/artifact/dataset/config/source versions, feature/context/decision hashes, market/news IDs, quality/reason/probabilities. `news_forecast_proposal` має 1:1 FK, deterministic proposal UUID, Decimal-string entry/SL/TP, rules/policy versions і hash. DB triggers блокують UPDATE/DELETE; повтор того самого decision після restart повертає попередні ID, відмінний decision для sealed window відхиляється. Запис default-off і тільки `APP_ENV=test` з `FORECAST_FIXTURE_ENABLED=true`; міграцію перевірено на disposable SQLite, але **не застосовано до active PostgreSQL**. Це не Paper order journal і не production model registry.

S11/N2b migration `c5e8a21d9f40` після `b8c9e4a01722`: `announcement_coverage_observation` додає append-only UTC snapshot кожної test fixture page ingestion або failure разом із source/version/config/scan ID, status, gap/shift/pages, останнім success і error code. Mutable `announcement_coverage` лишається лише latest view; N2b export читає історію observations для as-of anchor. PostgreSQL і SQLite UPDATE/DELETE triggers захищають snapshots; disposable SQLite та окремий Unix-socket PostgreSQL перевірили trigger, а одна конкурентна пара same-edit ingest дала одну нову версію. Ширше concurrency й snapshot isolation не доведено. Міграцію виконували тільки на disposable SQLite, активний PostgreSQL/Compose не змінювали.

S11/G1 migration `e5a6b7c8d9e0` після `c5e8a21d9f40`: окремий `gdelt_volume_revision` з numeric Decimal-string `Volume Intensity` за UTC 15m `point_at`, `first_seen_at`, `observed_at`, sequence revision, causal `scan_id`, source/config version. `gdelt_volume_scan` — append-only стан кожного ручного one-shot poll (`ok|gap|error`, missing 15m buckets, insert/revision counts, bounded error code). Жодного title/URL/full text/raw response, FK до announcement, forecast або Paper немає. PostgreSQL/SQLite UPDATE/DELETE triggers; disposable SQLite перевірено, active PostgreSQL не мігрували.

S11/G3 migration `f2a6c1d9e8b0` після `e5a6b7c8d9e0`: незалежні `gkg_scan` (bounded manual attempts), `gkg_slot_observation` (15m `ok|repeat|error|unknown`) і `gkg_release_revision` (UTC batch/first-seen/observed-at, immutable revision, total/BTC rows, nullable mean tone, ZIP SHA-256 та manifest MD5/size). Усі містять causal scan/source/config fields; PostgreSQL/SQLite UPDATE/DELETE triggers. Немає raw ZIP, URL/title/text або FK до G1/G2/моделі/Paper. Перевірено лише disposable SQLite migration cycle, active PostgreSQL не мігрували.

S11/M1 migration `d3c7a5e91f02` після `f2a6c1d9e8b0`: `market_candle_observation` — append-only фінальні REST `BTC_USDT` 4h версії, окремі від mutable `candle` і Paper. PK UUID `observation_id`, спільний для версій вікна `causal_id`, unique `(symbol,timeframe,window_start,version)`, UTC `first_seen_at`/`observed_at`/`source_time`, SHA-256 payload, Decimal-text `close`, `source/source_version/config_version/rules_version`. PostgreSQL/SQLite triggers відхиляють UPDATE/DELETE; повтор hash дедуплікує application layer. Запис атомарний із `candle` тільки за явного `MARKET_CANDLE_JOURNAL_ENABLED=true`; default-off до окремої міграції active DB. Disposable SQLite upgrade/trigger/restart/rollback перевірено; active PostgreSQL/Compose не мігрували.

S11/M2a migration `9b82e6a4713c` після M1: `instrument_rules_observation` — append-only локальні спостереження public `BTC_USDT` Contract Info. PK UUID `observation_id`, спільний для версій символу `causal_id`, unique `(symbol,version)`, UTC `first_seen_at`/`observed_at`/`effective_at`, SHA-256 normalized payload, rules/source/config versions і JSON payload. `effective_at=observed_at` означає **перший локально доведений момент доступності цієї версії**, а не біржовий час набрання чинності: endpoint не надає такого timestamp. Швидкий повтор ідентичного payload дедуплікується; зміна та повернення старого payload дають нові версії. SQLite/PostgreSQL UPDATE/DELETE triggers блокують зміну; журнал і mutable `instrument_rules` пишуться атомарно лише при `INSTRUMENT_RULES_JOURNAL_ENABLED=true`. Default off; active PostgreSQL/Compose не мігрували. Окрема disposable SQLite перевірка `upgrade → check → downgrade M1 → upgrade → check` пройшла.

S11/M2b уточнення: ідентичні public rules дедуплікуються тільки для швидких повторів (<30 хв від останнього запису). Після ≥30 хв новий успішний refresh додає immutable re-observation з тим самим payload hash, новим `version`/`observed_at` і спільним causal/first-seen; це поновлює as-of свіжість при незмінних правилах. Відсутній refresh не створює рядка й лишає 1h stale gate чинним. Схема/міграція і mutable Paper semantics не змінилися.

## S21 Live pilot draft journal (2026-10-20)

`live_pilot_bot_config(bot_id,version,payload,created_at,config_version)` — окремий append-only журнал чернеток на існуючого Paper бота, з FK на `bot.id`. `(bot_id,version)` є primary key; PostgreSQL/SQLite triggers забороняють UPDATE і DELETE. Новий бот не має рядка до першого повного PUT. Цей журнал не входить у Paper `bot_config`, `paper_portfolio.risk_profile`, баланс, ордери чи позиції; версія стратегії та активний Paper scheduler незалежні.

## Локальне сховище credentials (2026-10-21)

Міграція `3f2c7a91d604` додає `exchange_credential` (UUID, exchange discriminator, label, 4-char hint, AES-GCM nonce/ciphertext, version, active/revoked, UTC timestamps, config version) та append-only `bot_credential_selection(bot_id,version,credential_id,credential_version,created_at,config_version)`. PostgreSQL/SQLite trigger забороняє UPDATE/DELETE selection; credential row змінюється лише при rotation/revoke. Revoke очищує поточний ciphertext/nonce/hint; історичні WAL/backup можуть зберігати попередній ciphertext. У БД немає master key і plaintext material; Paper рахунки/конфігурації/журнали не посилаються на selection у торговому шляху.
