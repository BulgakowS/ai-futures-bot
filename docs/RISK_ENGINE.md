# Risk Engine

Risk Engine — незалежний обов'язковий gate між будь-яким джерелом наміру (правила, AI, UI, replay) і broker adapter. Він не приймає пояснення AI як доказ допустимості угоди. Результат перевірки: `ALLOW`, `ADJUST` або `REJECT` із machine-readable кодами, розрахованим допустимим розміром, snapshot ID і версією правил.

## Порядок перевірок

1. Глобальний kill switch, стан бота/портфеля та дозвіл режиму; Live має окремий server-side gate.
2. Якість і вік ринкових даних, доступність інструмента, повнота правил контракту, часовий drift.
3. Idempotency/deduplication, поточні pending orders, конфлікт із відкритою позицією.
4. Біржові precision, min/max size, notional, step/tick, margin mode та підтримувані типи ордерів.
5. Достатність вільної маржі з резервом на комісію/прослизання; максимальне плече.
6. Ризик на угоду за відстанню до stop і найгіршим сценарієм, сукупна експозиція, кількість позицій, корельовані інструменти.
7. Денний realized + unrealized loss, максимальний drawdown, circuit breaker після серії відмов/помилок/аномальної волатильності.
8. Валідність SL/TP та перевірка, що reduce-only/close не збільшує ризик.

Перший `REJECT` фіксує основну причину; журнал може містити всі порушення. `ADJUST` дозволений тільки для зменшення розміру в межах контрактних правил; змінений намір проходить повторну перевірку. Ліміти користувача мають бути конфігуровані з безпечними початковими значеннями після узгодження профілю; наведені в UI числа не можна приймати як серверні ліміти.

## Захисні стани

- `DATA_STALE`: нові входи заборонено. Відкриту позицію показати з позначкою невідомої поточної оцінки; захисні дії виконувати лише за підтвердженим каналом.
- `WORKER_STALE`: новий Paper open/pending fill заборонено, коли heartbeat відсутній, не `idle` або старший за `worker_stale_seconds`; reduce-only close проходить окрему перевірку свіжого ринку.
- `BROKER_UNKNOWN`: після невідомого статусу ордера заборонити нові ордери в портфелі до reconciliation.
- `CIRCUIT_OPEN`: нові входи заборонені до виконання задокументованого правила скидання.
- `KILL_SWITCH`: негайно заборонити нові входи й скасувати pending orders за наявності брокера; закриття позицій — окрема, явно перевірена процедура, яка не повинна відкривати нову експозицію.

## Live gate

Локальний вибір exchange credential для бота є лише неактивною чернеткою. Навіть active credential та `selection_valid=true` повертають `live_allowed=false`; Paper scheduler, Risk Engine і broker не читають цей вибір. Ротація/revoke роблять pinned selection непридатним. Відсутність окремого перевіреного shared Live risk/execution/reconciliation gate лишає Live забороненим.

Перед майбутнім Live потрібні: окреме середовище та облік; серверний feature gate default-off; ключ з мінімальними дозволами й без withdrawal; перевірений exchange adapter; підтверджені правила інструмента; ліміти втрат/плеча/позицій; аварійна зупинка; reconciliation після рестарту; алерти; тривалий paper soak; незалежний review; окрема явна згода власника на ввімкнення. UI-перемикач або наявність API-ключа самі по собі не відкривають Live.

Історичне S15 уточнення 2026-10-20 (замінене S23): користувацька попередня умова для можливого Live — MEXC-issued account+symbol підтвердження 0% maker **для API trades** і свіжий strict account fee snapshot із maker=0; taker може бути ненульовим. Окремий pure assessment не є server-side Live gate, не перевіряє справжність підтвердження і не відкриває торгівлю. Підтвердження зараз відсутнє. Числові max position (USDT або % equity), TP/SL (%) та leverage для майбутнього pilot ще треба визначити; без них та решти Live gate реальний ордер не допускається.

Історичне S17 уточнення (замінене S23): перед account/API fee assessment окремо потрібен свіжий exact-symbol public zero-zone proxy shortlist; його flags не є підтвердженням account fee. Майбутній автоматичний вибір за liquidity/spread має відмовляти на stale/unknown і поки не має числових критеріїв. Max position pilot рахується як **повний notional exposure включно з leverage**, не виділена маржа. Server-side ліміти TP/SL (%) і leverage ще не задані; чинний Live gate не змінено.

S23 чинна вимога (2026-10-21): майбутній вибір охоплює всі поточні active/API-allowed USDT-M perpetual, включно зі stock/TradFi. Ні належність до зони 0 комісій, ні письмове підтвердження 0% maker, ні нульова ставка не є Live умовою. Кожен новий Live entry має використовувати Post Only maker intent; захисний reduce-only exit може бути taker і мусить мати відповідний budget за свіжою account-specific maker/taker ставкою. Cost-aware Risk Engine має враховувати fee, spread, slippage, funding, повний notional exposure, pending intents та перевірену depth/liquidity; stale/unknown snapshot блокує entry. Post Only та reduce-only execution/reconciliation ще не реалізовані чи перевірені; загальний server-side Live gate залишається закритим. Історичні S14/S15/S17/S19 pure модулі не є його чинними блокерами.

### S22 ізольований Live pilot preview (2026-10-20)

`evaluate_live_pilot_preview()` — pure, нез'єднана з брокером перевірка **одного нового entry** за S21 `LivePilotDraft`. Вона вимагає config version, UTC now/максимальний вік, source ID і UTC час для equity, daily loss, повних positions/pending books, кандидата та валідованих правил/ціни кожного символу. Відсутній, невідомий чи stale компонент означає deny. Snapshot має бути спільним і reconciled; виклик з конкурентним оновленням без атомарного резервування не є trading gate.

Наявна експозиція кожної позиції та pending entry рахується окремо як `remaining_contracts × contract_size × max(reference_price, recorded_price)`; кандидат — так само з entry price. Зустрічні напрямки не нетуються. `pending_entries` містить **лише невиконаний залишок** після partial fill; виконані контракти належать `positions`. Невідомий залишок/стан ордера, неповний перелік або неможливість reconciliation мають `EXPOSURE_INCOMPLETE`/`ORDER_STATE_UNKNOWN`, а не уявний нуль. Кількість пар — число distinct symbols у positions + pending + кандидаті; дві заявки однієї пари займають один слот, але обидві додають notional. `PCT_EQUITY` — значення `5` означає 5% позитивного свіжого equity. Daily loss — уже обчислений перевіреним caller невід'ємний USDT loss за визначеним ним UTC day boundary; `loss >= cap` блокує entry. Розрахунок Decimal без округлення до лімітів, перевищення строго `>`; leverage перевіряється для кандидата й наявної експозиції проти draft і validated instrument max. Для LONG SL нижче entry, TP вище; для SHORT навпаки. SL distance не може перевищити налаштований %, TP distance не може бути меншою.

Preview не підтверджує точність джерела/контрактних правил або повноту account snapshot, не резервує конкурентні intents, не перевіряє maker eligibility, liquidity/spread, margin/fees, precision/step/tick, ліквідацію чи весь Stage 8/10 Live gate. Навіть позитивний `preview_passed` завжди має `live_allowed=false`; цей модуль не викликається Paper worker, Live broker або exchange.

### S24 ізольований Post Only cost preview (2026-10-21)

`evaluate_live_cost_preview()` є окремою pure оцінкою одного entry і не підключений до S22, Paper, worker, broker чи біржі. S21 `LivePilotDraft` тепер вимагає `max_round_trip_cost_pct` без default: додатне Decimal число менше 100, де `0.5` означає **0.5% повного entry notional**, а не частку `0.5`. Кожне нове збереження створює наступну версію JSON. Старий payload без цього поля повертає `COST_CONFIG_INVALID`; його треба явно оновити, щоб запустити cost preview.

Для exact symbol потрібні UTC/source ID та свіжі authenticated account maker/taker ставки, а також валідовані bid/ask і виконувана глибина щонайменше на **повний** кандидатний notional. Caller окремо подає невід'ємні `exit_slippage_pct` і `funding_reserve_pct`; їхніх числових defaults немає. Ставки fee подаються як частка (`0.0001` = `0.01%`), spread і reserves — у відсоткових одиницях. Консервативна оцінка у %: `100 × (max(maker_rate, 0) + max(taker_rate, 0)) + 200 × (ask−bid)/(ask+bid) + exit_slippage_pct + funding_reserve_pct`. Повний bid–ask spread додається один раз. Негативні ставки/rebate не зменшують бюджет; exit оцінюється за taker ставкою. Межа рівна ліміту проходить, перевищення дає `ROUND_TRIP_COST_OVER_BUDGET`.

Відсутній/невалідний draft, input, версія або clock, stale/future fee/market, чужий symbol, невідоме підтвердження/ринок, crossed quote, замала liquidity чи entry без точного `POST_ONLY` дають причинні deny codes (`*_MISSING`, `*_UNKNOWN`, `*_STALE`, `EVIDENCE_SYMBOL_MISMATCH`, `LIQUIDITY_INSUFFICIENT`, `POST_ONLY_REQUIRED`). `preview_passed=true` все одно повертає `live_allowed=false`. Caller мусить окремо довести справжність authenticated fee, нормалізацію depth units, безперервність книги, придатність slippage/funding reserve та зв'язок з реальним intent; ця функція не робить attestation і не створює ордера. Наступний крок — verified live source/reconciliation, shared atomic risk gate і решта Stage 8/10 перед будь-яким дозволом Live.

## Докази приймання

Property tests на `never_execute_without_risk_verdict`, `never_increase_exposure_when_halted`, `no_duplicate_order_for_idempotency_key`; сценарії stale data, insufficient margin, leverage breach, денний збиток, drawdown, кореляція, orphan order, restart та kill switch. Результати мають містити відхилений intent і код причини.

## Поточний paper gate S04 (2026-10-15)

`PaperService.check()` записує verdict для кожного нового open/close та повторної перевірки pending limit перед broker fill. Broker додатково перевіряє ALLOW, зв'язок intent/verdict/action, reduce-only позицію, ticker/depth/rules snapshot IDs та актуальний `market_quality.trade_eligible`. Kill switch скасовує pending й блокує входи; close допускається за підтвердженого ринку і чинних правил. Втрата WS quality блокує також close і захисний tick до відновлення підтвердженого каналу. У поточному однокористувацькому paper backend немає приватного брокера чи стану `BROKER_UNKNOWN`; повне приймання roadmap 4 очікує боти, restart dedupe їхніх подій і ширшу fault-injection матрицю.

S10D/F4: новий Paper entry і pending залишок потребують однозначного public `CUSTOM/BY_VOLUME` tier за сукупними контрактами, з valid `mmr/imr/maxLeverage`; unknown → `RISK_TIER_UNKNOWN`, без order/fill. Tier leverage та `imr` обмежують плече разом із профілем. Зміна raw tier/rules потребує ack чинної версії; ack невідомого tier відхиляється. Існуюча позиція може пройти reduce-only close після ack нових правил, без перенесення старого fee/funding ledger. Protective tick застосовує спільну сценарну модель maintenance до SL/TP, а stale market/rules не створюють вигаданого close. Приватний risk level MEXC не використовується, тому цей gate не означає parity з біржею.

S10E/F5a (2026-10-16): ізольований PostgreSQL 16.9/18 multi-process тест підтвердив серіалізацію concurrent kill+open/close на стані risk gate. Після активного kill новий open мав `KILL_SWITCH`, pending limit — `KILL_CANCELED`; concurrent close отримав `ALLOW` із `reduce_only=true`. Bot close на свіжих quality/ticker/depth пройшов, на stale quality отримав `DATA_STALE` без fill. При одночасному open/kill можливий ALLOW тільки коли open завершився до активації kill; результат залежить від порядку транзакцій. Live gate не змінено. Evidence — `docs/evidence/paper-process-faults-2026-10-16.json`.

S10G–H (2026-10-16): окремий heartbeat gate у `PaperService.check()` виконується після kill і перед market quality для нового entry; age 21 с при threshold 20 с дав `WORKER_STALE` при свіжому ринку й без order. Injected WS disconnect за свіжого heartbeat дав `DATA_STALE` без order. Kill close на свіжому ринку мав `ALLOW`, `reduce_only=true`; kill entry мав `KILL_SWITCH`. Worker статус перевіряє завершення ingestion task, вік останнього успішного WS ticker та прогрес paper/bot tick. Процесні fault tests S10H показали `WORKER_STALE` без нового order/fill при кожному partial-loop stall і затримці heartbeat DB row; повторна перевірка pending limit скасувала його як `RISK_CANCELED`. Reduce-only close і далі вимагає свіжого market gate: під stale ingestion він мав `DATA_STALE`. Heartbeat не доводить кожну protective дію, а кожен fill усе одно проходить market/risk gate.

S11/N4c test-only Model→Paper adapter (2026-10-18) не має брокера чи exchange keys і не записує ордер напряму. Лише перевірений synthetic candidate із sealed causal hashes та Decimal execution policy передається в `PaperService.submit()`; позитивний fixture зафіксував `ALLOW` перед єдиним order/fill. Kill, stale quality, stale worker і stale rules дали чинний risk `REJECT` без order/fill; `WAIT`, unapproved/corrupt/expired artifact, contract bounds і `UNKNOWN` order блокувалися до нового виконання. Це не model promotion: production flag вимкнений; реальний forward/data-use та Stage 8 operational gates лишаються відкритими.

## S21 чернетка майбутніх Live pilot limits (2026-10-20)

`GET/PUT /api/v1/bots/{id}/live-pilot-settings` зберігає окремий від Paper `RiskProfile` append-only draft. Для цього Live pilot виняток із загального принципу безпечних початкових лімітів: **жодного числового default**; користувач заповнює всі поля в налаштуваннях конкретного бота. Відсутній/неповний/некоректний draft не зберігається й означає Live deny. Навіть повний draft повертає `live_allowed=false/LIVE_DISABLED` і не підключений до брокера, selector чи Risk Engine. Paper defaults і Paper start не змінено.

Per-pair та aggregate ліміти — повна сума notional exposure, включно з плечем, а не margin. Обидві межі повинні мати однакову одиницю (`USDT` або `PCT_EQUITY`), aggregate ≥ per-pair. Для `PCT_EQUITY` число `5` означає 5% від майбутньої окремої Live equity, не частку `0.05`; daily loss у цій одиниці має бути `0 < value < 100`. Max leverage у draft ≥1, TP/SL — `0 < pct < 100`. Це лише форма й локальна валідація, не підтверджені біржові або економічні межі. Майбутній gate має звіряти leverage з актуальними symbol rules, додавати pending intents до aggregate exposure, перевіряти daily loss у відокремленому Live ledger і fail closed на невідомій equity/правилах.
