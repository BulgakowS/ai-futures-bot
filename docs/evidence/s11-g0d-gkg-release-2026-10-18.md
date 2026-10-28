# S11/G0d — один GDELT 2.1 GKG release, read-only discovery

Перевірка: **2026-10-18, 19:45–19:47 UTC**. Джерела: [GDELT Data](https://gdeltproject.org/data.html) (GKG 2.0 оновлюється кожні 15 хв), [опис GDELT 2.0 release lists](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/), [офіційний GKG 2.1 codebook](https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf), [Terms of Use](https://gdeltproject.org/about.html#termsofuse). Перші дві сторінки вже перевірив root через gstack `/browse` 2026-10-18; тут codebook завантажено як read-only file probe. Це **один** release, не backfill і не forward coverage test.

`lastupdate.txt` вказав `20261018194500.gkg.csv.zip`, **3,313,906 bytes**, MD5 `c009c9ce717287c2111d33feafb7d8f6`. Рівно один HTTPS GET цього архіву з timeout 15 s і лімітом 8 MB; розмір і MD5 збіглися. Локальний SHA-256: `13894340e4ff5433f469dae3503c22a58b6f1fb6974c0d354c92f3a51c4fbb25`; manifest **не містить SHA-256**, тому його не можна називати перевіреним за manifest. ZIP має один TSV payload 10,230,843 bytes. Сирий архів/codebook тільки у `/tmp`, у repo лише ці агрегати й [sanitized JSON](s11-g0d-gkg-release-2026-10-18.json).

## Схема за GKG 2.1 codebook

Codebook визначає один документ на TSV рядок, 27 полів, без header; фактичні **732/732** рядки мають рівно 27 полів. Нумерація нижче 1-based. `V2.1DATE` усіх рядків дорівнює `20261018194500`; це не доказ, що кожний документ став доступним у момент публікації. Усі 732 мають `V2SOURCECOLLECTIONIDENTIFIER=1` (WEB).

| № | Поле codebook | Статус для можливого числового BTC aggregate |
|---:|---|---|
| 1 | `GKGRECORDID` | Лише тимчасова перевірка дублів; не зберігати як article ID |
| 2 | `V2.1DATE` | Дата джерела за codebook, однакова в цьому файлі; окремо потрібен локальний `first_seen_at` |
| 3 | `V2SOURCECOLLECTIONIDENTIFIER` | Перевірка типу колекції |
| 4 | `V2SOURCECOMMONNAME` | Не зберігати publisher metadata |
| 5 | `V2DOCUMENTIDENTIFIER` | Не зберігати URL/цитату |
| 6–7 | `V1COUNTS`, `V2.1COUNTS` | Не використовувати для BTC article count |
| 8 | `V1THEMES` | Точний код `ECON_BITCOIN` для підрахунку рядків |
| 9 | `V2ENHANCEDTHEMES` | Позиції у тексті не потрібні |
| 10–11 | `V1LOCATIONS`, `V2ENHANCEDLOCATIONS` | Не потрібні |
| 12–13 | `V1PERSONS`, `V2ENHANCEDPERSONS` | Не потрібні |
| 14–15 | `V1ORGANIZATIONS`, `V2ENHANCEDORGANIZATIONS` | Не потрібні; exact `bitcoin` у №14 тут не трапився |
| 16 | `V1.5TONE` | Перший із 7 numeric components — document tone; агрегувати лише по BTC theme rows |
| 17–18 | `V2.1ENHANCEDDATES`, `V2GCAM` | Не потрібні для мінімального source contract |
| 19–22 | `V2.1SHARINGIMAGE`, `V2.1RELATEDIMAGES`, `V2.1SOCIALIMAGEEMBEDS`, `V2.1SOCIALVIDEOEMBEDS` | Не зберігати URL |
| 23 | `V2.1QUOTATIONS` | Не зберігати цитати |
| 24 | `V2.1ALLNAMES` | Не зберігати names/offsets; exact `bitcoin` тут не трапився |
| 25–27 | `V2.1AMOUNTS`, `V2.1TRANSLATIONINFO`, `V2EXTRASXML` | Не потрібні; не зберігати raw metadata |

У цьому одному release: `ECON_BITCOIN` у **1/732** рядків (інші перевірені exact `bitcoin` в organizations/allnames — 0); усі 732 tone parse як 7 numeric components, середній tone BTC subset **−0.695410292072323** (n=1). Це GKG theme-classified document count і середній *загальний тон документа*, не кількість усіх Bitcoin статей, не BTC-specific sentiment у реченні, не нормалізована `TimelineVol` intensity і не сигнал ціни. Нуль theme rows у майбутньому успішно отриманому release можна записати як 0; відсутній/зіпсований release лишається `unknown`.

## Придатність і рішення

**Кандидат на окремий source v2, без заміни G1/G2 зараз.** Кожен GKG release дає явний 15-хвилинний batch і достатні GDELT-derived numeric fields для `ECON_BITCOIN` count, denominator `GKG rows` та mean document tone. Це може уникнути неоднозначних пропущених DOC `TimelineVol` buckets, але **один** batch не доводить сталість випусків, BTC coverage, стабільність теми/схеми, latency, revisions або predictive lift. 1 BTC row показує потенційну розрідженість навіть при повному release stream. Порівняння з DOC `Volume Intensity` некоректне без окремого протоколу; старі G1 numeric revisions, G2 feature hashes і N5a freeze не можна переіменувати на GKG.

За цим sample передача одного zip кожні 15 хв коштувала б **3,313,906 bytes/slot**, ≈**318,134,976 bytes/day (303.4 MiB/day)** або ≈**9.54 GB/30 days** за сталої величини; це екстраполяція, не квота чи вимір розподілу розмірів. Розпакований TSV — 10,230,843 bytes/slot. Для candidate forward collector потрібно читати тільки `lastupdate`/офіційний master list із bounded rate, перевіряти monotonic 15m batch ID та size/MD5, фіксувати UTC `first_seen_at` фактичного отримання, SHA-256 payload, source/config version, causal scan ID і статус кожного очікуваного слота. `available_at=max(batch_at,first_seen_at)`; пізній release/корекція не переписує вже sealed anchor. У 4h anchor перевіряти всі 16 slot statuses, рахувати `WAIT` на missing/error/late, не підставляти нуль. `V2.1DATE` не замінює локальний час доступності. Потрібен окремий source-compatible dataset/model feature contract, якщо stream буде обрано.

Внутрішня [GDELT eligibility decision](../GDELT_SOURCE.md) дозволяє дослідницьке використання **власних числових derived fields GDELT**; цей кандидат зберігав би лише власні aggregate counts/tone/coverage. Article title, document URL, full text, quotations, publisher metadata, image URL і raw archive до repo/DB не потрапляють. Для публікації результатів: “Data source: GDELT Project” із посиланням на `https://gdeltproject.org/`. Окремо перевірити, що остаточний набір полів лишається в межах цього рішення.

**Наступний критерій:** окремий обмежений forward план без corpus backfill має показати всі очікувані 15m release IDs, фактичну затримку, gaps/дублі/корекції, BTC zero-vs-missing і bandwidth на репрезентативному періоді; лише після цього вирішувати про source v2 і змінювати G1/G2/N5a. Stage 9 лишається `collect-only`; модель, DB, Paper і Live не змінювалися.
