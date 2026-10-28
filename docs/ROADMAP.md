# Roadmap і критерії приймання

Початковий план складено 2026-10-14; фактичний статус і незакриті критерії — у [IMPLEMENTATION](IMPLEMENTATION.md). Етапи послідовні за воротами приймання; окремі задачі можуть виконуватися паралельно. Часові оцінки встановлюються після вимірювання складності, тому тут немає вигаданих дат.

## Правило приймання

Кожен етап завершується лише тоді, коли є запущений результат, автоматизовані перевірки, короткий ручний сценарій, фактичні логи/скриншоти за потреби та оновлена документація. Мокові дані можуть застосовуватися в тестах, але не підтверджують інтеграційний критерій. Невиконаний пункт лишає етап відкритим.

| Етап | Залежність | Результат |
|---|---|---|
| 0. Discovery | — | Підтверджені API, ринки, правила й межі MVP |
| 1. Foundation | 0 | Локальний Compose, база, API, health |
| 2. Market Data | 1 | Живі MEXC дані та якість потоку |
| 3. Paper Core | 2 | Портфелі, ордери, fills, ledger |
| 4. Strategy & Risk | 3 | Боти, три стратегії, незалежний gate |
| 5. Paper MVP UI | 4 | Наскрізний сценарій у браузері |
| 6. AI Engine | 5 | Керована AI-аналітика без обходу ризику |
| 7. Analytics & Backtesting | 5 | Відтворювані тести й порівняння |
| 8. Automation & Hardening | 6, 7 | Оповіщення, відмовостійкість, операційні перевірки |
| 9. Local News Model (Paper research) | Якість даних/Paper 2–4, контракти AI 6 і replay 7; після операційної перевірки 8 | Дослідний 4-годинний прогноз BTC_USDT; обрано GDELT aggregate forward source, модель і Paper gate ще відкриті |
| 10. Live readiness | 8; окреме рішення користувача, незалежно від результату етапу 9 | Документована готовність, Live усе ще вимкнений |

## 0. Discovery та замороження початкових контрактів

**Роботи:** підтвердити офіційну MEXC Futures документацію, доступність публічних даних, контрактні правила для першого інструмента, funding/fees, історію, WS semantics і rate limits; уточнити продуктові питання в [рішеннях](DECISIONS.md); зафіксувати канонічний формат market event і початкову модель маржі; перевірити підтримку потрібних образів на Apple Silicon.

**Приймання:** існує матриця джерел із URL і датою перевірки; для кожного критичного поля визначено джерело або явне обмеження; погоджено перший інструмент, таймфрейми, режим маржі та сценарій MVP; невідомі біржові правила не приховані в конфігурації.

## 1. Foundation

**Роботи:** створити репозиторій реалізації, Next.js, FastAPI, trading/AI worker, PostgreSQL, Redis, міграції, конфігурацію середовищ, `compose.yaml`, health checks, локальний hot reload, базовий журнал та outbox. Live adapter відсутній у runtime.

**Приймання:** `docker compose up -d --build` на Apple Silicon піднімає всі сервіси; UI і API відкриваються локально; health показує залежності; дані переживають перезапуск контейнерів; секретів у git/логах немає; чистий запуск і перезапуск перевірено на порожньому volume.

## 2. Market Data

**Роботи:** MEXC public REST/WS adapter, каталог контрактів, фінальні OHLCV, ticker/bid/ask, за доступності book/funding/OI, нормалізація, reconnect/gap/backfill, метрики актуальності, базові індикатори.

**Приймання:** BTC/USDT або підтверджений на етапі 0 інструмент має живий графік з `source_time` та ознакою затримки без приватного ключа (якщо public доступ підтверджено); після втрати WS UI показує stale і бот не отримує новий придатний до торгівлі event; replay дубліката/gap не породжує зайву свічку; дані й правила інструмента відповідають контрольному запиту до MEXC.

## 3. Paper Core

**Роботи:** незалежні віртуальні портфелі, ордерний state machine, market/перший підтримуваний тип limit, partial fills, fees, spread/slippage, маржа, LONG/SHORT, SL/TP, funding за доступними даними, ліквідаційна модель з позначеною точністю, immutable ledger, reset через нову епоху.

**Приймання:** таблиці сценаріїв із [Paper Trading](PAPER_TRADING.md) проходять автоматично; кожен fill має відповідні збалансовані ledger entries; рестарт і повтор команди не створюють дубль; два портфелі не змішують баланси; ні в одному paper-шляху не викликається приватний MEXC API; у UI/API видно фактичне джерело fees/funding та обмеження моделі.

## 4. Strategy & Risk

**Роботи:** бот lifecycle, Trend Following, Pullback, Breakout, версіоновані пресети, Risk Engine з лімітами та kill switch, idempotency, окремі журнали рішень.

**Приймання:** кожен intent отримує risk verdict до broker call; тести доводять блокування при stale data, втраті, перевищенні плеча/кількості позицій/ризику, дублікаті та kill switch; три боти на тому самому потоці працюють із різними конфігураціями й незалежними портфелями; після рестарту немає повторного входу на вже обробленій події.

## 5. Paper MVP UI — перша наскрізна віха

**Роботи:** Dashboard, Bots wizard, Positions, Settings, графік, журнал, Simple/Advanced/Expert, базова аналітика, темна тема, UA/EN, адаптивність, статус data quality.

**Приймання:** на `localhost:3030` користувач вибирає перший інструмент, бачить живу ціну MEXC, створює paper-портфель на 1000 USDT, налаштовує і запускає бота, отримує віртуальну угоду з видимими fees, SL/TP, PnL, risk verdict та джерелом даних; після refresh/restart історія збережена; mobile viewport проходить ручний критичний сценарій; без OpenAI-ключа сценарій працює. Якщо ринок не дає природного сигналу під час перевірки, використати контрольований тестовий market event у тестовому середовищі та окремо підтвердити живий потік.

## 6. AI Engine

**Простий Paper model draft (2026-10-21; уточнення 2026-10-22):** зберігає ліміт повного notional із плечем і `stop_loss_pct`/`take_profit_pct` як верхні межі відсоткової відстані від entry. Після окремого допуску модель має вибирати точні додатні відстані `≤` відповідних caps; це ще не реалізовано. `MODEL_NOT_APPROVED` блокує `ready/start` та worker execution. Це UX/контрактна підготовка за вибором користувача, не приймання Stage 6 або 9: реальна модель, forward gate, перевірка точних рівнів та Paper/Risk активація лишаються відкритими.

**Роботи:** структурований контекст, конфігурація моделі/промпту/частоти, JSON validation, budget cap, журнал, порівняння версій, fail-to-WAIT; UI пояснення.

**Приймання:** валідна AI-пропозиція проходить Risk Engine; невалідна відповідь, таймаут, вичерпаний бюджет або stale context не створюють ордер; лог містить версію моделі/промпту, context ID і результат; вимкнення OpenAI не ламає базовий Paper Trading; секрет не з'являється в журналі або frontend.

## 7. Analytics & Backtesting

**Роботи:** історичний dataset із provenance, replay ×1/×10/×50/×100, backtest через той самий strategy/risk contract, fees/funding/slippage assumptions, звіти PnL/drawdown/win rate/profit factor/equity curve, порівняння ботів.

**Приймання:** той самий dataset, seed і конфігурація дають ідентичний журнал та метрики; тест із майбутнім екстремумом не впливає на попереднє рішення; немає look-ahead на незакритій свічці; звіт містить витрати й обмеження джерела; результати двох ботів мають спільний часовий інтервал і правила симуляції.

## 8. Automation & Hardening

**Роботи:** Telegram alerts, спостережуваність, резервне копіювання/відновлення, fault injection для WS/API/DB/Redis, довгий paper soak, ресурси й продуктивність Apple Silicon, UX крайових станів.

**Приймання:** алерт про kill switch, stale data й падіння worker доходить у тестовий канал без витоку секретів; після контрольованого збою та restore ledger збалансований, а невідомі ордери не дублюються; paper soak має зафіксовану тривалість, конфігурацію, кількість подій, помилки й ресурсні метрики; критичні інциденти виправлені або блокують наступний етап.

**Обраний операційний горизонт перед малим Live pilot (2026-10-22):** 7 послідовних діб Paper на справжньому public market stream із наперед зафіксованими ботом/версією конфігурації, стартом і кінцем UTC, журналом усіх рішень, risk verdict, ордерів/виконань, балансів, stale/unknown станів, downtime, алертів і ресурсів. Будь-який збій, який відкриває обхід Risk Engine або губить причинність/облік, блокує pilot до виправлення й повторної перевірки відповідного сценарію. Нуль або замало угод за тиждень означає, що якість стратегії не оцінено. Цей тиждень є операційним Paper gate; він не скорочує окремі 56 діб forward для GKG-моделі та не вмикає Live.

## 9. Local News Model — Paper research

**Місце в черзі:** read-only перевірку історії оголошень (N0) можна виконати після поточного gate якості public MEXC/Paper даних. Інтеграцію N1–N5 починати після стабілізації наявного event-time replay, журналу AI-пропозицій та їхнього відображення; news event-time replay входить до N2. Відкриті критерії етапів 6–8 не оголошувати завершеними лише через цей модуль. Повний [план і послідовні критерії](plans/2026-10-16-mexc-news-local-model-design.md).

**G4/T1 уточнення приймання:** GKG candidate sample має фіксувати фактичний `decision_at` після final 4h Candle і повного GKG coverage, не пізніше `anchor+15m`; target є close наступної свічки в `anchor+4h`, а не рівно 4h після рішення. Раніший G4 v1 dataset не приймати для training/forward. N5a для DOC не є GKG forward gate; перед N3b/N5 потрібні source-compatible frozen artifacts та реальний latency/coverage доказ.

**S11/M1 market provenance:** новий default-off immutable final REST Hour4 journal та GKG export v3 дозволяють перевірити as-of market corrections у disposable DB. Active PostgreSQL/Compose не мігрували; real GKG eligibility лишається `WAIT`, доки немає увімкненого журналу з доведеним coverage і rules/cost provenance. Stage 2 Paper path продовжує використовувати mutable `Candle`, а Stage 9 не переходить із `collect-only`.

**S11/M2a rules/cost provenance:** default-off immutable public Contract Info journal фіксує локальні as-of версії `BTC_USDT`; окремий сценарний `2×public taker fee + predeclared funding/slippage` рахується Decimal і позначений як припущення, не фактичний fill/account тариф. G4/G5b/N5b ще не використовують quote як перевірений cost provenance, active DB не мігрували; Stage 9 лишається `collect-only`.

**S11/M2b freshness fix:** незмінні public rules треба повторно засвідчувати в append-only журналі при успішному scheduled refresh після ≥30 хв; швидкі повтори дедуплікуються. Без нового refresh as-of quote після 1 години лишається stale. Mutable Paper rules cache, G5b gate і Stage 9 статус не змінені.

**S11/N5b GKG preregistration:** [окремий GKG numeric protocol](plans/2026-10-19-s11-n5b-gkg-forward-protocol.md) фіксує 16 as-of slots, `decision_at≤anchor+15m`, next nominal 4h close label з `label_cutoff=target+15m`, immutable M1 market/rules/cost provenance, 56 UTC діб/336 anchors та paired real artifacts. Це лише `collect-only` план: реального GKG-compatible freeze manifest і artifacts немає, тому forward clock не стартував; N5a лишається DOC-only. Paper activation потребує Stage 8 та Risk Engine, Live заборонений.

**Бюджетний блокер N5b:** загальний мережевий збір новин обмежено **5 GB** з мінімальним збереженням числових даних. Початковий N5b потребує 96 повних GKG ZIP/добу × 56 діб; два виміряні ZIP по 3.3 і 5.9 MB показують несумісність такого плану з лімітом. Реальний пілот зупинився після двох release. Перед новим freeze потрібні окреме рішення про source/cadence, нова версія dataset/protocol та власний enforceable загальний byte cap; повний 15m збір за старим N5b не відновлювати.

**Роботи:** після N0b користувач обрав [GDELT-owned агреговані дані](GDELT_SOURCE.md) замість MEXC announcements для нового forward збору. G1 додає лише числовий журнал `Volume Intensity` із UTC `first_seen_at`, ревізіями та coverage; G2 додав окремий synthetic offline dataset/feature contract із as-of ревізіями та 4h label. MEXC `BTC_USDT` market і 4h horizon збережено. Історичний MEXC title-based план та fixtures не переадаптовані; GDELT дані не подаються в них автоматично. Перед локальним навчанням потрібні persisted real export/snapshot, підтверджений forward coverage і source-compatible frozen artifacts. Єдина G1 real проба 52/96 слотів не доводить viability; strict G2 повертає `WAIT` на gap. G3 — окремий default-off GKG candidate collector числових BTC theme count/tone. G4 додав окремий GKG 16-slot 4h dataset і test-only bounded SQLite export; два real G3 batch не доводять coverage. G6 ручний bounded test-only pilot почався 2026-10-19, але зупинився після двох release через clock skew; G6a виправляє pacing, повного 16-slot журналу ще немає. G1/G2/N5a не перемкнено. У першому релізі пропозиція не викликає брокер і не створює paper-ордер. Live Trading не входить до цього етапу.

**Приймання:** API і право на використання даних перевірено з датою/джерелами; відсутні, змінені або запізнілі новини не створюють сигнал заднім числом; той самий dataset/seed дає відтворюваний результат; часовий split і forward-тест не допускають майбутньої інформації; звіт показує внесок новин проти market-only, витрати, невизначеність і `WAIT`; кожен прогноз має причинні ID, UTC, версії моделі/даних/конфігурації. Недостатньо придатних прикладів або відсутність перевіреного приросту залишають систему в режимі збору/аналізу без торгового впливу. Користувач дозволив майбутні автоматичні **лише Paper** угоди після окремого model forward gate та чинного Risk Engine; цей дозвіл не вмикає їх зараз. Live лишається забороненим.

## 10. Live readiness, без увімкнення торгівлі

**Локальна credential чернетка (2026-10-21):** додано encrypted at-rest сховище кількох `mexc_futures` credentials і версіонований вибір для бота. Це metadata/configuration-only: жодного authenticated MEXC запиту, перевірки прав, broker, Live activation чи зміни Paper. Критерій наступного етапу — окрема перевірка чинного приватного API/scopes, provenance account-specific fee, backup/key recovery, авторизація UI/BFF, shared Risk/reconciliation та решта Stage 8/10 перед окремим рішенням власника про Live.

**Роботи:** перевірити актуальний приватний API, scopes і правові обмеження; окремий live broker contract і reconciliation; dry-run та відмовні сценарії; незалежний security/risk review; runbook аварійного вимкнення. Виконання реальних угод не входить у цей етап.

**Приймання:** всі пункти [Live gate](RISK_ENGINE.md#live-gate) задокументовані й підтверджені; флаг Live за замовчуванням вимкнений і тестами доведено, що без окремої явної активації приватний ордер неможливий; є окремий документований запит на ввімкнення. Якщо хоча б один пункт не виконано, Live лишається закритим.

**Малий реальний pilot після 7 діб:** лише окреме рішення власника з конкретними числовими лімітами S21, обраним біржовим credential, підтвердженими Stage 8/10 gates, актуальними account fees/rules і незалежно перевіреним broker/reconciliation. До цього жодних приватних ордерів; обсяг бюджету, час старту й stop conditions не виводяться автоматично з тривалості Paper тесту.

**S12 read-only discovery (2026-10-20):** поточні офіційні [MEXC API docs і 0-Fee Fest](MEXC_API.md#s12-live0-fee-read-only-discovery-2026-10-20) перевірено; public Contract Info без `symbol` дав каталог `data=list` на 1108 рядків. Публічні zero-fee flags не доводять персональний тариф, а умови акції прямо виключають API users із 0 fees. Приватні fee/order/reconciliation endpoints вивчено лише за документацією, без акаунтного запиту чи ордера. Runtime лишається BTC_USDT/Paper-only; Stage 8 operational gate і всі критерії цього Stage 10 залишаються відкритими. Новий запит на роботу над Live readiness не вмикає Live і не є дозволом на реальний ордер.

**Історична S15 maker-only умова (2026-10-20; замінена S23):** власник розглядатиме Live лише після окремого письмового підтвердження MEXC, що саме його API trades на конкретному контракті мають 0% maker, та свіжого strict account `NORMAL` fee snapshot із maker=0. Taker=0 не вимагається; public S14 0/0 promo catalog не є цим доказом. Pure [S15 assessment](MEXC_API.md#s15-isolated-api-maker-0-assessment-2026-10-20) не підключений до Live і не засвідчує справжність evidence. Зараз MEXC-підтвердження немає, тому умова не виконана. Для майбутнього обмеженого pilot потрібні числові max position (USDT або % equity), TP/SL у %, leverage; значення ще не надані й не налаштовані. Усі інші Stage 8/10 умови лишаються обов'язковими.

**Історичний S17 двоетапний відбір (2026-10-20; замінений S23):** перед S15 account/API maker evidence pure assessment тепер вимагає свіжий exact-symbol S14 public zero-zone proxy shortlist і синхронний active/API-allowed contract snapshot. Скриншот MEXC показав шість USDT-M символів, наявних у S14 snapshot, але public proxy не гарантує тотожність UI зони чи API fee eligibility; старий snapshot не проходить freshness gate. У майбутньому користувач хоче автоматичний вибір серед поточних кандидатів за liquidity/spread, із deny на stale/unknown; ranking і числові пороги не реалізовано. Max position для pilot — повний notional exposure з leverage, не margin. [Деталі](MEXC_API.md#s17-public-zone-shortlist-before-account-eligibility-2026-10-20).

**S23 чинний напрям відбору (2026-10-21):** майбутній автоматичний universe — усі поточні active, API-allowed USDT-M perpetual з Contract Info, включно зі stock/TradFi; зона 0 комісій і письмове підтвердження 0% maker більше не є вимогами чи Live blockers. Окремий pure [generic catalog/shortlist](MEXC_API.md#s23-generic-usdt-m-research-universe-2026-10-21) ранжує лише research кандидати зі свіжого public snapshot і caller ticker evidence, `max_pairs≤3`, `trade_ready=false`. Майбутній Live entry допускається лише через Post Only maker intent; reduce-only захисні виходи окремо враховують можливу taker fee. Потрібні свіжий authenticated account fee для конкретного symbol, cost-aware Risk Engine, перевірена depth/liquidity, числові pilot limits і всі решта Stage 8/10 gates; broker та Live не підключено. S14/S15/S17/S19 артефакти лишаються історією і не є активним шляхом допуску.

## Після основної лінії

Mean Reversion, Grid, DCA, AI Adaptive, custom strategy builder, advanced portfolio analytics, можлива гібридна схема Vercel (лише UI) + VPS (довготривалий engine). Кожну функцію оцінювати окремо, не включати її в завершений MVP заднім числом.

**S11/G7a sparse GKG v3 (2026-10-19):** окремий default-off one-shot test-only collector для одного fixed `anchor−15m` ZIP на UTC 4h anchor. Append-only SQLite claim до HEAD/GET резервує 8 MiB ZIP + 64 KiB overhead + 16 KiB read overrun під загальний 5,000,000,000 B cap із 134,217,728 B консервативним opening debit; crash/error не дозволяють повтор anchor. Реальний HTTP CLI приймає тільки єдиний ignored журнал `data/research/s11-gkg-sparse.sqlite`; temp DB призначені для mock тестів. Тільки числові GDELT-derived aggregate/hash/UTC provenance; G3/G4/G5b/N5b залишаються неактивним 16-slot `collect-only` контрактом. Реального G7a GET, sparse dataset/model, market/cost freeze чи Paper wiring немає. Перед реальним збором потрібні один стійкий ledger і заборона паралельних legacy news GET. [Sparse outline](plans/2026-10-19-s11-g7a-sparse-gkg-v3-outline.md).

**S11/G7b/N5c (2026-10-19):** окремий pure one-slot `gdelt-gkg-sparse-market-dataset-v3` і test-only bounded SQLite export з G7a claim/result та immutable M1 Hour4; один sample або `WAIT` на anchor, UTC as-of decision ≤+15m, label наступного close as-of target+15m, known BTC zero окремий від unknown/error/crash. [N5c](plans/2026-10-19-s11-n5c-gkg-sparse-forward-protocol.md) фіксує 56 діб/336 anchors, один slot на anchor, ≤5 GB cap і парні пороги. Export читає лише disposable temp snapshot, не постійний journal. Немає real sparse cohort, cost/rules freeze, artifacts, forward clock, inference або Paper; Stage 9 — `collect-only`, Live заборонений.

**S11/G7c (2026-10-20):** окремий synthetic-only offline trainer для G7b one-slot GKG, детермінована пара `market-only`/`sparse-gkg+market`, chronological embargo й validation-only threshold. Persisted-real readiness fail-closed без verified cost/rules/freeze; реальних artifacts, N5c clock, production inference або Paper wiring немає. Synthetic metrics не є прийманням Stage 9; наступні критерії — real one-slot coverage, M1/rules/cost provenance, source-compatible freeze і forward gate.

**S11/G7d (2026-10-20):** додано лише synthetic-only offline as-of projection і inference для нового one-slot anchor з окремим frozen training manifest. Новий row має власні feature/source hashes, тому checksum нового dataset не мусить збігатися з навчальним; artifact, manifest, source schema/versions, UTC as-of, відсутність label/target та `anchor>test_end` перевіряються fail-closed. Це не real model promotion: без trusted snapshot і доказу відсутності omitted corrections, реального cost/rules freeze, N5c forward і незалежного Risk/Paper gate Stage 9 лишається `collect-only`; Live заборонений.

**S11/M3 (2026-10-20):** ізольований default-off test-only one-shot public MEXC Hour4/rules poll у тому самому fixed sparse research SQLite. Лише поточний UTC 4h anchor ≤+15 хв, два final попередні Hour4, clock skew і immutable M1/M2 записи; жодного active Compose/Paper/Live. Mock інтеграція пройдена; реальний запуск і поточна офіційна Contract Info перевірка відкриті.

**S11 локальний запуск (2026-10-21):** додано та о `12:04 UTC` встановлено macOS LaunchAgent для одноразового research runner UTC 4h anchors у fixed ignored SQLite; `RunAtLoad` завершився з кодом `0`, а фактичну своєчасність майбутніх anchors треба перевірити окремо. GKG claim лишається незворотним; пропущені вікна не відновлюються. Це не запускає N5c forward clock або Paper/Live.

### S21: чернетка лімітів майбутнього Live pilot (2026-10-20)

Для кожного наявного Paper бота можна окремо зберегти повний, версіонований набір **чернеткових** лімітів: `max_pairs` 1–3, per-pair і aggregate full notional (USDT або `% equity`), max leverage, TP/SL у %, daily loss (USDT або `% equity`). `PCT_EQUITY` задається цілим відсотковим значенням: `5` означає 5%, не `0.05`; daily loss має бути меншим за 100% equity. Числових значень за замовчуванням немає. Відсутній запис означає `PILOT_CONFIG_MISSING`; збережений запис повертає `LIVE_DISABLED`, бо server-side Live gate залишається закритим. Це не відбір пар, не перевірка біржових меж плеча й не дозвіл на ордер. Майбутній runtime перед кожним наміром мусить перевіряти актуальні правила MEXC та спільні ліміти відкритих позицій і pending intents. Розширення до 4–5 пар потребує окремого aggregate exposure/soak review.

**S22 preview (2026-10-20):** додано тільки pure fail-closed [оцінювач](RISK_ENGINE.md#s22-ізольований-live-pilot-preview-2026-10-20) повної експозиції positions + невиконаних pending залишків + кандидата, distinct pairs, leverage, TP/SL і daily loss із caller-provided validated rules/source UTC. Позитивна оцінка не відкриває Live: `live_allowed=false`; немає інтеграції з broker/worker. Наступний критерій — атомарний shared gate із reconciliation/резервуванням, перевіркою біржових правил і всіх Stage 8/10 перед окремим рішенням власника.

**S24 cost preview (2026-10-21):** S21 draft тепер вимагає `max_round_trip_cost_pct` без числового default; старі JSON drafts без поля fail closed. Окремий pure [розрахунок](RISK_ENGINE.md#s24-ізольований-post-only-cost-preview-2026-10-21) одного Post Only entry приймає exact-symbol свіжі authenticated account maker/taker fee evidence, валідовані spread/liquidity evidence та явні невід'ємні exit slippage/funding reserves. Decimal оцінка включає повний spread і можливу taker комісію захисного виходу; rebate не зменшує бюджет. Навіть позитивний результат має `live_allowed=false`. Жодного runtime/broker/network підключення; Stage 8/10 лишаються відкритими.
