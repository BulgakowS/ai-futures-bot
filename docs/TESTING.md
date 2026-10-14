# Стратегія тестування

Тести мають доводити фінансові інваріанти, відмовостійкість і реальну інтеграцію; моки потрібні для швидких детермінованих тестів, але не заміняють перевірку MEXC public data. Жоден результат тесту не слід записувати як успішний до його фактичного запуску.

## Рівні

| Рівень | Що перевіряє | Приклад доказу |
|---|---|---|
| Unit/property | Decimal math, ledger balance, state machine, risk rules, indicators | тести на інваріанти й крайові значення |
| Contract | нормалізація MEXC, JSON AI, API/OpenAPI, event schema | збережені sanitized fixtures і schema validation |
| Integration | DB transactions/outbox, Redis restart, paper broker, worker recovery | тест із реальними локальними залежностями |
| End-to-end | живі дані → рішення → risk → paper fill → UI | ручний та автоматизований маршрут |
| Fault injection | WS gap, timeout, 429/5xx, DB outage, stale data, unknown order | відсутність нових входів і дублювання |
| Backtest correctness | no look-ahead, repeatability, costs, funding | golden runs із dataset checksum |
| Security | секрети, auth, Live default-off, mode isolation | негативні тести та огляд логів |

## Обов'язкові інваріанти

1. Немає fill без ордера, ордера без чинного risk verdict, risk verdict без intent.
2. Повтор команди/event/fill не змінює фінансовий результат удруге.
3. Сума ledger entries у кожній транзакції дорівнює нулю; позиція й баланс відновлюються після перезапуску.
4. Paper path не викликає приватний MEXC API; Live path недоступний за замовчуванням.
5. Неповні/застарілі дані та невідомий ордер блокують нові входи.
6. Бот A не може змінити портфель B; backtest не змінює Paper; Live не використовує paper ledger.
7. Зміна майбутніх ринкових даних не змінює минулі рішення в backtest.
8. AI timeout/schema error/budget stop дає WAIT, а не виконання.

## Сценарії для приймання етапів

Матриця тестів має посилатися на ID етапу з [roadmap](ROADMAP.md). Для кожного критерію записувати команду/маршрут, середовище, dataset або реальний ринок, очікуваний і фактичний результат, дату, відповідального та артефакт. Інтеграційний тест живих даних перевіряє timestamp і рух ціни, але не очікує конкретної ціни. Торговий end-to-end сценарій може використати контрольовану подію для гарантованого сигналу; окремо підтверджується живий канал.

## Продуктивність і тривала робота

На етапі 1–2 виміряти baseline CPU/RAM, затримку від market event до risk verdict, розмір черг і поведінку при кількох ботах. На етапі 8 встановити пороги за реальними вимірами й перевірити довгий paper soak та backup/restore. Невідомі пороги не називати SLA.
