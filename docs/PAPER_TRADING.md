# Paper Trading

## Принцип

Ринкові дані — справжні, ордери — симульовані. Paper Broker ніколи не викликає приватний trading API. Режим використовує власні портфелі, ledger, позиції, ордери й журнали. Паралельні боти можуть отримувати однакові market events, але не спільний віртуальний баланс, якщо користувач явно не вибрав груповий портфель у майбутній версії.

## Базова модель

- Портфель: початковий депозит у USDT, wallet balance (депозит плюс проведений realized PnL мінус проведені fees/funding), доступний баланс, зарезервована маржа, equity й unrealized PnL. Скидання створює нову версію/епоху портфеля; старі журнали не переписуються.
- Позиція: інструмент, напрям LONG/SHORT, кількість, середня ціна входу, маржа, плече, SL/TP, нарахований funding, fees і стан. Для MVP обрати та зафіксувати один режим маржі після перевірки біржових правил.
- Ордер: market або limit, side, quantity, intent ID, risk verdict ID, статус, кількість виконання, середня ціна, витрати. Partial fill є окремою подією.
- Ledger: кожна зміна балансу окремим незмінним записом із причиною; сума дебетів і кредитів кожної транзакції дорівнює нулю.

## Модель виконання

Market buy виконується за ask, sell — за bid; застосовується перевірена комісія або явно заданий сценарний тариф та модель додаткового прослизання. Коли є надійна книга, обсяг проходить рівні ліквідності й може виконатися частково. Без достатньої глибини показується `estimated_fill`, а для розміру понад консервативний поріг ордер відхиляється. Limit fill не гарантується дотиком свічки: потрібен обраний механізм черги/книги або консервативне правило, задокументоване в результаті.

## Розрахунки

Усі величини — `Decimal`, внутрішня точність вища за біржову, округлення при прийманні ордера й записі ledger за правилами інструмента. Нотація нижче задає базову лінійну USDT-модель; контрактні множники й конкретну маржу треба підтвердити для вибраного MEXC-інструмента.

| Показник | Базова формула |
|---|---|
| Notional | `quantity × contract_multiplier × price` |
| Початкова маржа | `notional / leverage` |
| Fee | `filled_notional × fee_rate` для кожного fill |
| LONG unrealized PnL | `(mark_price − average_entry) × quantity × multiplier` |
| SHORT unrealized PnL | `(average_entry − mark_price) × quantity × multiplier` |
| Equity | `wallet_balance + unrealized_PnL − ще не проведені зобов'язання` |
| Доступний баланс | `wallet_balance − reserved_margin − pending_order_reserve − pending_fees` |

Зарезервована маржа зменшує доступний баланс, але не equity вдруге. При проведенні funding або fee сума змінює wallet balance та прибирається з непроведених зобов'язань, щоб уникнути подвійного віднімання. Усі проєкції перевіряються проти ledger.

Funding — окрема cash-flow подія на підтвердженому timestamp з реальною або сценарною ставкою й позначкою джерела. Ліквідація не виводиться лише з `1/leverage`: потрібні підтримувальна маржа, тарифи, mark price і правила контракту. Доки вони не перевірені, симулятор має повідомляти `liquidation_estimate` та обмежувати плече консервативно; не показувати оцінку як біржову точність.

## Пріоритет подій

Для однакового timestamp порядок є детермінованим: зміна ринку/mark → перевірка margin і ліквідації → захисні SL/TP → funding за розкладом → нові сигнали. Якщо intrabar порядок SL і TP невідомий, застосувати консервативний варіант або позначити результат неоднозначним. При втраті даних нові входи блокуються; стан відкритої позиції не «вигадується» за останньою ціною.

## Сценарії перевірки

Тести мають покривати LONG/SHORT, часткові виконання, maker/taker fees, spread/slippage, funding обох знаків, SL/TP, недостатню маржу, ліквідацію, gap даних, повторну подію, рестарт, скидання портфеля й два боти на одному потоці. Ручний наскрізний сценарій описаний у [roadmap](ROADMAP.md).

## Поточна реалізація S04 (2026-10-15)

Backend підтримує окремі paper-портфелі та епохи, market і crossing limit, часткові fills, LONG/SHORT, fees, ізольовану маржу, SL/TP і оцінку ліквідації. Fill використовує свіжий публічний REST snapshot книги з обмеженням участі 10% на рівень та scenario slippage; для limit немає черги або гарантії maker, тому застосовується taker rate з чинних public rules. Cash ledger має парні wallet/counteraccount postings із точністю 12 знаків; PostgreSQL trigger блокує UPDATE/DELETE журналу. Funding rate береться з confirmed public history, а settlement price — оцінка за попередньою final 1m candle з явним provenance. Якщо її немає, funding залишається unresolved, equity невідомий і нові входи блокуються. Підтверджений market quality є обов'язковим також для close/protective fill; kill switch не обходить цю перевірку. Повний перелік перевірок і обмежень — у [IMPLEMENTATION.md](IMPLEMENTATION.md).

## S10B/F3: funding coverage (2026-10-15)

Public history тепер зберігається в окремому незмінному журналі `(symbol, settle_time)` з rate, `collectCycle`, raw payload/hash, source, UTC observed time і config version. Worker запитує REST pages до найранішого fill усіх paper-позицій поточної епохи, включно із закритими. Сторінки навмисно перекриваються (розмір `1000,999,…`), звіряються `currentPage/pageSize/totalCount/totalPage`, дублікати/порядок, часові інтервали за записаним `collectCycle` та повторна page 1. Водяний знак `[covered_from, covered_until)` записується тільки після повної перевірки; `covered_until` дорівнює спостереженому `nextSettleTime` і не є вічним дозволом після цієї межі.

Для портфеля з held fills відсутній/прострочений/неповний watermark, невідповідність hash журналу або неоплачене settlement дають `FUNDING_UNRESOLVED`, `equity=null`, час прогалини, блок нового входу й reset. Відомі settlement після restart проводяться рівно раз за `(portfolio,epoch,position,settle_time)` через append-only ledger; відсутня final 1m ціна залишає зобов'язання unresolved до її появи. Ця ціна маркується `estimated_prior_final_1m_close`, не точною MEXC valuation. Public endpoint не гарантує атомарний snapshot pagination або документовану семантику `collectCycle` при зміні розкладу, тому невідповідність перевірок блокує equity і потребує повторного reconciliation; full exchange parity не доведено.
## S10C/F1–F2: fees і REST book fill (2026-10-15)

Нормалізовані public rules зберігають `makerFeeRate`, `takerFeeRate`, `feeRateMode`, `feeRateType`, fee tiers і risk tiers у версіонованому raw hash. Зміна будь-якого поля raw rules, зокрема public fees, tiers або `state`, скидає ack; новий risk verdict не допускає виконання до повторного підтвердження поточної версії. Paper підтримує для нових fills лише спостережений public `NORMAL/BASE` тариф без tiered/leverage fee rates. Невідомий fee mode або неактивний contract не можна підтвердити як безпечний. Public ставка не є персональним тарифом рахунку.

Market і crossing limit є **оціненими taker fills**. Кожен fill записує depth event ID/version, ticker ID, rules hash/config version, обидві public ставки, застосовану taker rate/source, fee mode/type, slippage та limit model. Fee ledger проводить точно округлену Decimal суму цього fill через парні wallet/counteraccount записи. Старі fills не перераховуються після зміни public тарифу. Passive limit не отримує fill тільки через розміщення або дотик ціни: він лишається pending без моделі черги й без maker rebate.

REST book перевіряється на свіжість, версію, повні впорядковані сторони, ціновий tick та контрактний volume step. Повтор тієї самої версії з тими самими рівнями не створює нову ліквідність; та сама версія з іншими рівнями або регрес версії позначається stale до наступної нової версії. Для market модель бере не більш як 10% кожного видимого рівня; ціна включає scenario slippage, а максимальне погіршення від best quote обмежене `2 × slippage_bps` (один allowance для видимого руху книги, один для сценарію). За браком видимого обсягу market стає `PARTIALLY_FILLED_CANCELED` або `NO_LIQUIDITY`. Limit виконується тільки за ціною після slippage, що не порушує його межу; решта лишається pending. Це не відтворює queue, приховану ліквідність, latency або реальне біржове виконання.

## S10D/F4: tier-aware сценарна оцінка ліквідації (2026-10-15)

Для BTC_USDT модель приймає тільки чинні підтверджені public `riskLimitMode=CUSTOM`, `riskLimitType=BY_VOLUME`, впорядковані `riskLimitCustom` (`level/maxVol/mmr/imr/maxLeverage`) та підтримуваний public fee mode. `maxVol` — **contracts**, а не USDT notional; tier обирається за сумарною кількістю контрактів відкритої позиції: на межі діє поточний tier, після межі — наступний. Для pending partial fill перевіряється вже виконана кількість плюс залишок. Tier version є hash усіх raw rules; зміна tiers скидає ack. Невідомі, суперечливі, прострочені або не підтверджені правила дають `liquidation_estimate=null` і блокують новий entry (`RISK_TIER_UNKNOWN` або gate правил). Зменшення позиції лишається можливим лише через чинний risk gate і перевірений ринок.

Сценарна equality для mark `P`, кількості base units `Q`, середнього входу `E` і `r=mmr+takerFeeRate+liquidationFeeRate`: `isolated_cash + side × (P−E) × Q = P × Q × r`, де `isolated_cash=posted_margin+posted_funding`, `side=+1` LONG / `−1` SHORT. Звідси обчислюється модельна гранична ціна; protective tick використовує ту саму нерівність перед SL/TP. Funding, уже проведений у wallet/ledger, входить до isolated collateral один раз і не віднімається вдруге з глобального equity. Entry fee вже списана з wallet і не віднімається вдруге з isolated collateral; майбутні taker close та liquidation fees враховуються в `r` до їх фактичного проведення. При partial reduce залишкові margin/funding розподіляються пропорційно. Публічна ставка funding і оцінена історична settlement price залишаються модельними cash flows.

API позначає результат `liquidation_provenance.model=model_estimate`, подає tier/rules version, mark event, UTC/config version і припущення. Це **сценарна оцінка**, а не MEXC `liquidatePrice`: приватний position risk level, ADL, персональні fees, fee discounts, margin adjustment та точна біржова формула не підтверджені. Відсутність числової оцінки не є доказом безпечності позиції.

## S10E/F5a: процесний PostgreSQL gate (2026-10-16)

На тимчасових PostgreSQL 18 і 16.9 з двома API й окремими worker процесами перевірено одночасний повтор команди: один intent/verdict/order/fill на ключ у портфелі й той самий збережений результат. Аварійний вихід після Paper commit до фіналізації рішення бота відновлюється без другого fill. Для кожного з окремих paper портфелів wallet дорівнював сумі ledger postings; `backtest_run` не змінювався. Під kill pending скасовано, новий вхід заблоковано, а close тільки з підтвердженим свіжим market context пройшов risk gate як reduce-only; stale close не виконався. Це доказ на контрольованих даних, не тривала перевірка public MEXC stream або точності fills. Деталі та межі — `docs/IMPLEMENTATION.md` і `docs/evidence/paper-process-faults-2026-10-16.json`.

## S10F/F5b: real-public Paper browser QA (2026-10-16)

На окремому портфелі `S10F real-public manual QA` три **ручні** Paper open виконано за чинними public WS ticker і REST depth MEXC; кожен open і close мав `risk.outcome=ALLOW`, estimated fill із `public_contract_rules_taker=0.0004`, версіями правил і REST книги. Вхід не був сигналом бота. Після кожного close не лишилося відкритої тестової позиції, global kill switch відновлено в `true`; сукупно 6 intent/verdict/order/fill, 10 ledger rows, 0 незбалансованих postings. Public fee не дорівнює персональному тарифу, REST fill і liquidation tier залишаються модельними.

Browser/API route для історії позицій після виправлення async виклику повертає liquidation estimate/provenance для відкритої позиції, а для закритої `liquidation_estimate=null`, `status=unknown`, `reason=POSITION_CLOSED`; closed estimate не домислюється за новим mark. UI показує applied fee source/rate, rules/depth version, `model_estimate`/tier/mark/assumptions. Refresh і перезапуск API/Web не стерли journal. За 375 px відкриту позицію можна було переглянути й закрити через звичайну mobile дію без горизонтального прокручування. Джерело public snapshots і причинні IDs зафіксовано у `docs/evidence/paper-browser-qa-2026-10-16.json`; довгий ресурсний sample — `docs/evidence/paper-public-soak-2026-10-16.jsonl`.

30-хв public soak завершився без нового funding settlement: це не перевірка на 8-годинній межі. Хвилинні market samples усі `ok`, але 2-секундний зріз виявив 10 коротких `ticker_stale` при source age трохи понад чинний TTL 5 с; risk preflight відхиляє такі нові intent як `DATA_STALE` за контрактом, runtime manual submit саме в ці миті не виконувався. Alert outbox при цьому накопичував повторні market-stale transitions. Потрібен окремий quiet public run для калібрування freshness/alert hysteresis із доведенням fail-closed при справжньому WS disconnect; цей критерій ще відкритий.

Виявлено й виправлено `GET /portfolios/{id}/positions` 500: route має await `position_json(session, row, funding_resolved)`; `liquidation_provenance` тепер доступний у history API. Закрита позиція має `status=unknown`, `reason=POSITION_CLOSED` замість безпричинної оцінки. Browser evidence і межі — `docs/evidence/paper-browser-qa-2026-10-16.json`.

## S10G: operational entry gate (2026-10-16)

Новий Paper open і повторна перевірка pending limit потребують `trading` heartbeat зі статусом `idle` та віком менше `worker_stale_seconds` (локально 20 с); інакше зберігається risk verdict `WORKER_STALE` без ордера/fill. Kill switch перевіряється першим. Reduce-only close не залежить від worker heartbeat, але й далі потребує свіжого quality/ticker/depth/rules; на WS disconnect close лишається `DATA_STALE`. Trading heartbeat стає `degraded`, якщо ingestion task завершився, не було успішного WS ticker у межах `worker_stale_seconds` або paper/bot loop не завершував успішний tick у цій межі. Це перевірка прогресу компонентів, а не доказ кожної protective дії; кожен fill окремо проходить market/risk gate.

Інтеграційний fixture і дві API OS-процеси на ізольованій PostgreSQL показали `DATA_STALE` без ордера після injected WS disconnect, `WORKER_STALE` без ордера за свіжого market quality і heartbeat age 21 с, `ALLOW`/`reduce_only=true` для kill close на свіжих даних. Після Compose rebuild та міграції `f7c6482e5a01` readiness `ok`, kill switch `true`. Контрольовані snapshots не є вигаданими public samples; public sampling окремо read-only.

## S10H: bounded process faults (2026-10-16)

На окремій тимчасовій PostgreSQL із реальними незалежними API та `run_worker` процесами, але синтетичним loopback-only public feed, зависання paper tick або bot tick робило heartbeat `degraded`; незавершений, але завислий ingestion task теж стає `degraded` після доданої перевірки останнього WS ticker. При тестовій конфігурації heartbeat 1 с / stale 4 с entry дістав `WORKER_STALE` без нового order/fill приблизно за 4–5 с. Окреме блокування рядка heartbeat у PostgreSQL привело до stale readiness і того самого risk verdict без блокування Paper транзакцій. Pending limit при повторній перевірці під `WORKER_STALE` отримав `RISK_CANCELED` без fill; після recovery потрібен новий намір. Під kill reduce-only close зі свіжим ринком мав `ALLOW` і fill, а під stale ingestion close мав `DATA_STALE` без order. Детальні UTC/durations, outbox confirmation і restart dedupe — `docs/evidence/paper-worker-faults-2026-10-16.json`. Це контрольований локальний fault, не public MEXC SLA чи біржова execution parity.

S11/N4c (2026-10-18): окремий default-off test-only `ModelPaperService` перевіряє sealed synthetic forecast/proposal, artifact/approval та execution policy, після чого передає `OrderRequest` лише в чинний `PaperService.submit()`. Кількість задає Decimal policy, а не модель; округлення/contract bounds перевіряються перед intent, risk повторно перевіряє kill, worker, market/rules/funding і broker fill. Disposable SQLite fixture дав 1 intent/verdict/order/fill, повтор і restart не додали ордер, `UNKNOWN` зупинив повтор; відмови не дали нового fill/order. Production gate default-off і відхиляє synthetic approval; жодного runtime scheduler/API route немає. Користувач дозволив майбутні автоматичні тільки Paper-угоди після real model forward evaluation та чинного Risk Engine; зараз data-use/forward/Stage 8 gates блокують activation. [Evidence](evidence/s11-n4c-model-paper-fixture-2026-10-18.json).
