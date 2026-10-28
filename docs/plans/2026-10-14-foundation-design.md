# S02 Foundation — затверджений обсяг

Backend — модульний Python-пакет `ai_futures` у `services/api`; FastAPI API та окремий async trading worker користуються одним контрактом SQLAlchemy `Database.sessions` і Alembic migrations. PostgreSQL — runtime persistence; SQLite тільки у тестах. Redis — health dependency, не джерело балансу. AppState seed створюється рівно раз, з `paper` і ввімкненим kill switch, атомарно з audit/outbox. Торгових рахунків чи угод seed не створює.

Readiness перевіряє DB, міграції, Redis і persisted heartbeat worker. Liveness перевіряє лише процес. Кожен HTTP-запит має UUID request ID; будь-яка мутація вимагає Bearer bootstrap secret з `.env`. Secret не передається frontend. Публікується лише API на `127.0.0.1`; DB/Redis внутрішні. Live config не приймається.

Compose використовує ARM64-сумісні образи; dev override дає hot reload. Makefile стандартизує setup/test/lint/up/down/smoke. Перевірки: UTC/Decimal, safe config, auth, міграції, atomic bootstrap, readiness failure і реальний Docker restart persistence. S06 додасть web, S07 — опціональний AI; поточний S02 не означає приймання повного Foundation roadmap.
