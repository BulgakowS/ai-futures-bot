# Рішення, припущення й перевірки

Статус: 2026-10-14. Джерело продуктових рішень — попереднє обговорення з користувачем про MEXC-ботів. Цей файл розділяє узгоджене бачення і технічні пропозиції, які ще треба перевірити.

## Узгоджено

| ID | Рішення |
|---|---|
| D-01 | Початкова система працює локально на MacBook Apple Silicon через один Docker Compose із кількома контейнерами. |
| D-02 | Paper Trading використовує реальні дані MEXC і віртуальні ордери; це режим за замовчуванням. |
| D-03 | Кілька ботів і незалежних віртуальних портфелів можуть працювати паралельно. |
| D-04 | AI аналізує структуровані дані, а незалежний Risk Engine перевіряє кожне рішення до виконання. |
| D-05 | Live Trading передбачений архітектурно, але залишається вимкненим до окремого рішення. |
| D-06 | Потрібні backtesting/replay, аналітика, Telegram, адаптивний UI, темна тема, UA/EN. |
| D-07 | Базовий стек: Next.js, React, TypeScript, Tailwind, shadcn/ui, FastAPI/Python, PostgreSQL, Redis, OpenAI API, Lightweight Charts. |
| D-08 | Перші стратегії: Trend Following, Pullback, Breakout; решта — наступні етапи. |

## Проєктні рішення для специфікації

| ID | Рішення | Причина |
|---|---|---|
| P-01 | Модульний моноліт у backend, окремі процеси API, trading і AI worker | Менше розподілених транзакцій на старті, ізоляція довготривалих задач. |
| P-02 | Події та бухгалтерські записи незмінні; поточні стани є проєкціями | Аудит і відновлення після перезапуску. |
| P-03 | Один нормалізований потік ринкових даних роздається всім ботам | Сумісне порівняння стратегій і контроль навантаження на біржу. |
| P-04 | MVP ставить Paper Trading та risk gate перед інтеграцією AI | Можна перевірити торговий контур без платного API. |
| P-05 | Жоден API-контракт у `docs/API.md` не вважається реалізованим | Це дизайн, а не опис наявного сервера. |

## Обов'язково перевірити до реалізації

| ID | Питання | Коли |
|---|---|---|
| V-01 | Чинні умови доступу до публічних MEXC Futures REST/WebSocket, перелік полів, rate limits, правила перепідключення й використання даних | До етапу Market Data |
| V-02 | Контрактні специфікації, мінімальні обсяги, точність, комісії, funding, маржа й ліквідація для обраних інструментів | До симулятора і Live readiness |
| V-03 | Доступність історичних даних, funding та open interest для обраної глибини тестів | До backtesting |
| V-04 | Поточні API-можливості OpenAI: структурована відповідь, ліміти, доступні моделі та ціни | До AI Engine |
| V-05 | Технічна й правова допустимість live-торгівлі для конкретного акаунта та юрисдикції | Лише перед окремим Live етапом |

## Відкриті продуктові питання

Однокористувацький локальний сценарій є початковим припущенням. Під час етапу 0 слід визначити: перелік початкових ринків і таймфреймів; режим маржі та hedged/one-way; цільову частоту ботів; бюджет AI; політику зберігання історії; чи потрібен доступ поза локальною мережею. До відповіді ці параметри мають бути конфігурованими або позначеними як обмеження MVP. Не перетворювати їх на непомітні значення за замовчуванням.

## S01 Discovery — 2026-10-14

| ID | Статус | Результат |
|---|---|---|
| P-06 | Технічне рішення | Public adapter default REST base https://api.mexc.com, contract info /api/v1/contract/detail/country; WS wss://contract.mexc.com/edge. Чинні офіційні docs і read-only probes — у MEXC_API.md. |
| P-07 | Технічне рішення | Quantity у контрактах; BTC quantity=contracts×contractSize. Decimal parsing, dynamic rules і їх snapshot/version обов'язкові. |
| P-08 | Технічне рішення | WS candle provisional, final через REST reconciliation після interval end; сигнал один раз на final window. Public fees і settlement funding мають provenance, missing history не домислюється. |
| A-01 | Configurable припущення, не окреме погодження | Початковий BTC_USDT, isolated one-way Paper, 1000 USDT, 1m/5m/15m. Початкові risk limits консервативні й configurable; max exchange leverage не використовувати як рекомендований. |
| A-02 | Configurable припущення, потребує fault tests | Ticker TTL 5s, ping 15s, clock skew cap 2s, rules refresh 1h, сумарно ≤2 REST requests/s. |

V-01 частково перевірено: чинні docs, public REST, ticker/kline WS та pong підтверджені; reconnect, binary/gzip і rate-limit fault tests ще потрібні. V-02 частково: BTC_USDT contractSize/tick/lot/tiers/public fees/funding підтверджені; account-specific fees і точна ліквідація не підтверджені. V-03 частково: 2000 candles/request і funding history page доступні; повна історична глибина, gaps та історичний OI не перевірені. Live readiness не змінена. Apple Silicon Docker daemon підтверджений основним агентом (29.8.0, aarch64); конкретні Compose images і запуск перевірити у S02.

## S02 Foundation — 2026-10-14

P-09: central Python package `services/api/ai_futures`, shared SQLAlchemy async sessions, runtime PostgreSQL only; SQLite в тестах. Bootstrap Bearer token із `.env` захищає всі мутації; loopback binding та відсутність CORS для браузера. Frontend отримуватиме session/server proxy у S06. Worker heartbeat зберігається в DB (5s interval, stale 20s configurable); readiness залежить від DB revision/Redis/state/worker, liveness — лише API process. AI не має окремого idle контейнера до S07. Торгівлі Foundation не виконує.

## S05 Bots — 2026-10-15

P-10: bot scheduler опитує збережені REST-final candles і обробляє найновіше вікно один раз на бот; старі пропущені вікна не використовуються для пізнього входу. `bot_decision` з durable proposal перед Paper submit та стабільним ключем забезпечує recovery після збою між paper commit і journal finalize. PostgreSQL row lock серіалізує lifecycle і доставку; PaperService окремо повторно перевіряє market/risk. Вибір latest-only — консервативне рішення до окремого повного replay/backtesting. Готових торгових пресетів немає до відтворюваної перевірки.

## S06 Web — 2026-10-15

P-11: для локального paper MVP використано Next.js із server-side BFF та власний SVG final OHLCV chart замість цільових Tailwind/shadcn/Lightweight Charts із D-07. Це звужує залежності й дозволяє перевірити причинний paper flow зараз; trade markers, zoom і повний набір chart взаємодій лишаються відкритими. Browser Bearer token не видається. Web та API мають loopback binding; віддалена автентифікація не вводиться мовчки. Це відхилення від UI бібліотек, не від paper/risk контрактів.

## S07 AI — 2026-10-15

P-12: мінімальна AI інтеграція виконується на запит у API з portfolio-scoped policy, без окремого AI worker і без втручання в rule-based scheduler. Це дає перевірюваний fail-to-WAIT і paper risk path без ключа або фонового платного навантаження. `submit_to_paper` вимкнений за замовчуванням; будь-який увімкнений AI intent іде через PaperService. Це тимчасове відхилення від цільових immutable bot-level AI config, scheduler, арбітражу AI/rule сигналів і UI; їх потрібно реалізувати та протестувати до повного прийняття roadmap етапу 6. Для безпеки raw provider response і prompt text не зберігаються; замість них журнал має sanitized validated proposal/context та hash prompt.

## S08 Backtesting — 2026-10-15

P-13: для першого історичного наскрізного сценарію replay є чистою синхронною функцією над immutable JSON snapshot, а облік і причинні журнали зберігаються в окремому `backtest_run` (режим обмежений DB), без використання PaperService чи live market cache під час виконання. Імпорт читає тільки збережені REST-final MEXC свічки; модельний fill відкладається до наступного final close. Окремий backtest risk gate застосовує `RiskProfile` і правила інструмента, бо production `PaperService.check()` вимагає поточні ticker/depth/kill/ledger й не може безпечно читати їх як історію. Це тимчасове відхилення від повного shared strategy/risk contract; parity перевірка, immutable DB triggers, partial fills, liquidation та UI replay потрібні перед повним прийняттям roadmap 7. Seed залишено в контракті для майбутніх стохастичних моделей; ця модель детермінована без RNG.

## S11/G1 GDELT aggregate source — 2026-10-18

P-15: за вибором користувача новий forward збір Stage 9 використовує лише власну агреговану числову серію GDELT `Volume Intensity` для `bitcoin`, окремо від synthetic MEXC announcement контрактів. Це архітектурна зміна source/feature типу: старі title-based dataset/model fixtures не можна вважати GDELT-compatible. MEXC BTC_USDT market та 4h horizon залишаються. G1 має лише append-only numeric point/coverage journal і вручну запусканий, default-off poller; не запускає training/inference/Paper. Див. [source contract](GDELT_SOURCE.md), N2b/N3b/N5 відкриті.

## S10J Telegram delivery gate — 2026-10-17

P-14: server config має окремі `TELEGRAM_DELIVERY_ENABLED=false` і обов'язковий при ввімкненні `TELEGRAM_DELIVERY_CUTOFF_UTC`. Credential pair не є згодою на доставку. Cutoff є UTC-aware, не майбутній; SQL consumer вибирає лише події з `occurred_at >= cutoff`, не змінюючи давні queued rows. Це additive config/query рішення без міграції: outbox зберігає історію й audit, а оператор може окремо обрати межу після рішення щодо backlog. Межа включна; зміна її назад може включити старі події. Фактичне ввімкнення потребує окремого рішення користувача; тестовий канал ще не перевірено.

## S11/G4/T1 GKG decision time — 2026-10-18

P-16: GKG dataset v1 помилково вимагав `current.received_at <= anchor`, хоча final MEXC Candle надходить після номінального 4h close. Для GKG dataset v2 `anchor` лишається фіксованою межею свічки, а `decision_at` є першим моментом доступності фінальної поточної свічки та повних 16 GKG слотів, з fail-closed затримкою ≤15 хв. Features фіксуються as-of `decision_at`; label — close наступної 4h свічки (`anchor+4h`), отже ефективний час до target коротший за 4h. Schema/hash/manifest версія змінена, v1 snapshots несумісні. Це локальне виправлення GKG candidate, без зміни G1/G2 DOC чи synthetic N4; N5a не можна вважати активованим для GKG. Immutable market revisions або frozen attestation ще потрібні для доведеної історичної відтворюваності.

## S11/M1 Hour4 immutable market journal — 2026-10-19

P-17: Для real GKG eligibility mutable `Candle` не є джерелом as-of історії. Обрано окремий append-only `MarketCandleObservation` з атомарним записом при фінальній REST 4h reconciliation; чинний `Candle` і Paper contract зберігаються. Альтернативи — mutable timestamp attestation і повна заміна Candle — не дають відповідно історії corrections або є зайво широкою зміною Paper. Default-off flag відділяє деплой коду від міграції active PostgreSQL. GKG persisted export читає тільки journal; порожній/off journal дає `WAIT`, pre-decision correction зберігає чинний conflict gate `WAIT`, post-decision correction не переписує feature. Окремий export seal v3 потрібний для включення market observation/rules/source provenance; G4 dataset feature schema v2 не змінюється.

## S11/M2a локальні rules observations і сценарні витрати — 2026-10-19

P-18: MEXC public Contract Info не доводить біржовий effective timestamp чи персональний fee schedule. Тому `InstrumentRulesObservation.effective_at=observed_at` означає тільки локальну as-of доступність; версії журналюються append-only та default-off окремо від mutable `InstrumentRules`. Для дослідницького fixed-notional сценарію дозволено лише свіжі, валідовані public NORMAL/BASE fee terms і заздалегідь оголошені Decimal funding/slippage припущення. Quote зберігає observation ID/hash і checksum припущень, але не використовується як actual trading cost або автоматичний G5b/N5b gate. Невідомий account fee, справжній funding/fill та недоведений effective time блокують claims про реальні витрати. Через загальну межу збору новин ≤5 GB старий 15m full-ZIP N5b збір не продовжувати; зміна source/cadence потребує окремого версійованого рішення.

## S11/G7a/M3 budgeted research journal — 2026-10-20

P-19: Повний GKG ZIP кожні 15 хв не вкладається у 5 GB на 56 діб. Окремий `gdelt-gkg-sparse-v3` бере тільки один наперед заданий batch `anchor−15m` на UTC 4h рішення, без adaptive selection/backfill; цей 15m зріз не представляє всі попередні 4 години. Numeric-only results і незворотний byte claim зберігаються в одному fixed ignored `data/research/s11-gkg-sparse.sqlite`, який не можна перенести в активну Paper DB або обнулити новим файлом. Для дослідницького M1/M2 provenance той самий SQLite приймає лише default-off public Hour4/rules one-shot M3; це тимчасове відхилення від production PostgreSQL, ізольоване від active Compose, broker та Paper ledger. Exporter працює лише з disposable read-only snapshot цього журналу. Перед production залежністю потрібні чинна офіційна MEXC Contract Info перевірка, PostgreSQL concurrency/migration, real coverage/cost freeze та окремий runtime inference/Risk gate. Старі G3/G4/G5b/N5b не перейменовуються на sparse v3.

## S12 Live fee eligibility discovery — 2026-10-20

P-20: Публічний zero-fee flag, нульові `makerFeeRate/takerFeeRate` та сторінка 0-Fee Fest є лише кандидатами для відображення/дослідження. Офіційні умови [0-Fee Fest](https://www.mexc.com/zero-fee) прямо виключають API users із 0 fees; тому майбутній API broker не може призначати ставку 0 за цими публічними ознаками. Персональний fee schedule потребує окремого дозволеного private read та перевірки фактичних deal fees, збереження джерела й часу; невідомий тариф не можна підміняти акційним. Це рішення про fail-closed cost provenance для майбутнього Live, без приватного виклику, broker implementation або зміни закритого Live gate. Джерела й межі — [MEXC API discovery](MEXC_API.md#s12-live0-fee-read-only-discovery-2026-10-20).

## S23 fee-aware USDT-M universe — 2026-10-21

P-21: Власник скасував попередній 0-fee zone / 0% maker напрям. Для майбутнього автоматичного відбору обрано всі поточні active, API-allowed USDT-M perpetual з public Contract Info, незалежно від promo flags, public fee та класу crypto/stock/TradFi. Історичні S14/S15/S17/S19 артефакти збережено для аудиту, але не підключати їхній zero-zone/zero-maker assessment до майбутнього Live gate. Окремий pure S23 catalog/shortlist працює лише з caller public JSON/ticker evidence, з Decimal spread, явною свіжістю, fail-closed unknown і cap 3; він завжди research-only. Для майбутнього Live нові entry intents мають бути Post Only maker, а захисні reduce-only виходи можуть сплатити taker fee; свіжий authenticated account fee й cost-aware shared risk gate мають бюджетувати обидва сценарії. Жодного broker/order або Live enablement це рішення не реалізує. Альтернатива переробити S14/S15 in place відхилена, бо зруйнувала б provenance історичних evidence і тестів.

## Simple model Paper draft — 2026-10-21

P-22: Після уточнення користувача простий бот з навчанням спочатку є лише Paper `DRAFT` із max full notional однієї позиції та SL/TP. У вже наявній `BotConfig` JSON зберігається окремий `kind=learned_paper`; нової таблиці чи шляху до брокера немає. Lifecycle та worker явно відмовляють із `MODEL_NOT_APPROVED`, доки окремо не завершені real forward promotion, sizing/rules перевірки та спільний Risk Engine gate. Rule-based боти зберігають попередній шлях. Це дозволяє зафіксувати намір користувача без фальшивого твердження про готову модель або автоматичну торгівлю.

P-22 уточнення 2026-10-22: Для `learned_paper` наявні `stop_loss_pct` і `take_profit_pct` у `BotConfig` означають **верхні межі відсоткової відстані** від entry. Ключі й Decimal діапазон `(0,100)` залишаються для сумісності з уже збереженими `DRAFT`. Після окремого model promotion структурована пропозиція має вибрати точні `0 < SL/TP distance ≤` відповідного cap і пройти актуальні правила інструмента та Paper Risk Engine. Це уточнення не змінює семантику однойменних полів чернетки Live pilot чи чинних rule-based стратегій; model execution і broker path досі відсутні.
P-23: Обрано AES-256-GCM ciphertext у PostgreSQL і окремий API-only локальний env master key, а не env-only окремі акаунти чи plaintext DB. Це дає кілька записів і versioned bot selection, не залишаючи material відкритим у DB/backup. AAD зв'язує ciphertext з ID/exchange/version; ротація інвалідовує старий binding, revoke очищує поточний material. Обсяг лише `mexc_futures` input та інертна конфігурація; Live gate не змінено. [Дизайн](plans/2026-10-21-exchange-credential-vault-design.md).
