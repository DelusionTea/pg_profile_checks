# Stable PROD Analysis Brief

min_stability_ratio: 1.0
report_count: 2
stable_findings_count: 22
tuning_recommendations_count: 17
ephemeral_findings_count: 3

## Reports
- prod1: pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html
  server: tsldd-pprb01138.delta.sbrf.ru
  interval: 2026-08-11 16:30:02+03 .. 2026-08-12 09:00:02+03 (16.5 h)
  findings_in_report: 75
- prod2: pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html
  server: tsldd-pprb01138.delta.sbrf.ru
  interval: 2026-08-12 16:00:02+03 .. 2026-08-13 08:30:02+03 (16.5 h)
  findings_in_report: 66

## Stable tuning recommendations (sorted by problem severity)
### [critical] Idle in transaction / нет таймаута
- tuning_rule_id: tune_idle_in_transaction
- finding_ids: sessions.high_idle_in_transaction, sessions.idle_timeout_disabled
- stability: 2/2 (100%) in prod1, prod2
- change_safety: cautious | change_impact: medium
- example: statecontracts: idle_in_transaction_time=1372155.5s (threshold 3600s), idle_in_transaction_session_timeout=0
- problem: Долгие idle in transaction сессии держат snapshots, блокируют vacuum
- GUC `idle_in_transaction_session_timeout` → set_nonzero [safety=cautious, impact=medium] current: prod1=0, prod2=0
  rationale: Стабильный idle in transaction без таймаута — классический production risk.
  postgres_pro: Postgres Pro / PostgreSQL: 60s–300s типично для OLTP. Защита от утечки snapshots и bloat.
- actions:
  - Исправить application: COMMIT/ROLLBACK, pool settings
  - Найти источник в pg_stat_activity
  - Установить idle_in_transaction_session_timeout
  - Исправить application connection pooling / missing COMMIT
  - Найти источник зависших транзакций в pg_stat_activity

### [high] Память / cache hit
- tuning_rule_id: tune_memory
- finding_ids: cache.low_table_hit_ratio
- stability: 2/2 (100%) in prod1, prod2
- change_safety: restart_required | change_impact: high
- example: postgres.pgse_profile.sample_statements: table hit_pct=94.92% (threshold 95.0%)
- problem: Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
- GUC `shared_buffers` → increase [safety=restart_required, impact=high] current: prod1=504320, prod2=537600
  rationale: Низкий cache hit при стабильном паттерне — кандидат на увеличение буферов.
  postgres_pro: Postgres Pro: shared_buffers ~25% RAM (политика банка). Требует restart.
- GUC `effective_cache_size` → review_increase [safety=safe, impact=low] current: prod1=1512960, prod2=1512960
  rationale: Помогает планировщику при низком blks_hit_pct.
  postgres_pro: Подсказка планировщику (~50–75% RAM). Не выделяет память — безопасно для reload.
- GUC `work_mem` → review_decrease_or_role_level [safety=risky, impact=high] current: prod1=16384, prod2=16384
  rationale: Стабильный риск OOM при высоком work_mem и connections.
  postgres_pro: work_mem × параллельные sorts = риск OOM. Postgres Pro: задавать на уровне роли для тяжёлых запросов.
- actions:
  - Connection pooler (PgBouncer)
  - Таблицы с высоким physical read
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

### [high] Длительная запись checkpoint / bgwriter
- tuning_rule_id: tune_checkpoints_write
- finding_ids: checkpoints.high_write_time, checkpoints.high_write_time_per_hour, checkpoints.maxwritten_clean
- stability: 2/2 (100%) in prod1, prod2
- change_safety: restart_required | change_impact: high
- example: Checkpoint write time: 38898.3s over 16.5h interval (threshold 300s)
- problem: Длительная запись checkpoint увеличивает IO latency и может влиять на
- GUC `checkpoint_completion_target` → review_increase [safety=safe, impact=low] current: prod1=0.7, prod2=0.7
  rationale: Длинный checkpoint write time стабильно на PROD.
  postgres_pro: Растягивание checkpoint (0.7–0.9) снижает latency spikes.
- GUC `shared_buffers` → review_increase [safety=restart_required, impact=high] current: prod1=504320, prod2=537600
  rationale: Bgwriter не успевает при maxwritten_clean.
  postgres_pro: maxwritten_clean часто связан с давлением на buffer pool.
- actions:
  - Проверить IO latency диска, iostat
  - Согласовать max_wal_size / checkpoint_timeout
  - Проверить производительность диска (IOPS, latency)
  - Оценить checkpoint_completion_target
  - Снизить write-нагрузку или распределить её во времени

### [high] Медленные / spill SQL (не GUC-only)
- tuning_rule_id: tune_queries
- finding_ids: queries.slow_execution
- stability: 2/2 (100%) in prod1, prod2
- change_safety: risky | change_impact: high
- example: statecontracts/as_admin: max=35536.1ms, calls=1
  select $3, t0.OBJECT_ID c2 from statecontracts.T_CONTRACTS t0 where t0.RQUID &lt;> $1 limit $2
- problem: Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
- GUC `work_mem` → review_increase_role_level [safety=risky, impact=high] current: prod1=16384, prod2=16384
  rationale: Temp spill стабильно на PROD — точечная настройка безопаснее глобальной.
  postgres_pro: temp_blks_written — кандидат на role-level work_mem, не глобальное увеличение.
- actions:
  - EXPLAIN (ANALYZE, BUFFERS) топ SQL
  - Индексы, статистика ANALYZE
  - Не менять GUC без анализа планов
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)

### [high] Частые requested checkpoints / давление WAL
- tuning_rule_id: tune_checkpoints_wal
- finding_ids: checkpoints.high_requested_ratio, checkpoints.high_requested_count
- stability: 2/2 (100%) in prod1, prod2
- change_safety: cautious | change_impact: medium
- example: Requested checkpoints: 39/46 (84.8%), threshold 30%
- problem: Большая доля requested checkpoints означает, что WAL заполняется быстрее,
- GUC `max_wal_size` → increase [safety=cautious, impact=medium] current: prod1=12288, prod2=12288
  rationale: Малый max_wal_size — частая причина requested checkpoints при стабильной write-нагрузке.
  postgres_pro: Postgres Pro / PostgreSQL: увеличение max_wal_size снижает частоту requested checkpoints.
- GUC `checkpoint_completion_target` → review_increase [safety=safe, impact=low] current: prod1=0.7, prod2=0.7
  rationale: Сглаживает IO-пики при длинных checkpoint write.
  postgres_pro: Значение 0.7–0.9 растягивает checkpoint во времени (PostgreSQL docs).
- actions:
  - Сравнить WAL MB/h между периодами (compare_nt_prod / compare_runs)
  - Проверить wal-heavy SQL в pg_profile
  - Увеличить max_wal_size (часто 1–4 GB+ для OLTP под нагрузкой)
  - Проверить wal_buffers_full и общий объём WAL за интервал
  - Сопоставить с checkpoint_timeout — не уменьшать timeout вместо max_wal_size

### [high] Давление на соединения
- tuning_rule_id: tune_connection_pressure
- finding_ids: sessions.connection_pressure
- stability: 2/2 (100%) in prod1, prod2
- change_safety: cautious | change_impact: high
- example: postgres: connection pressure sessions=3241 (589% of max_connections=550)
- problem: Высокая утилизация соединений или много idle-сессий — риск исчерпания
- GUC `max_connections` → review_decrease_with_pooler [safety=cautious, impact=high] current: prod1=550, prod2=550
  rationale: Лишние backends едят память и усложняют vacuum/locks.
  postgres_pro: После PgBouncer снижайте max_connections до разумного пика backends.
- actions:
  - Внедрить/проверить connection pooler
  - Найти утечки idle/idle in transaction в приложении
  - Внедрить/проверить PgBouncer (transaction pooling где возможно)
  - Снизить max_connections после появления pooler
  - Найти утечки соединений в приложении

### [high] Переполнение wal_buffers
- tuning_rule_id: tune_wal_buffers
- finding_ids: wal.buffers_full
- stability: 2/2 (100%) in prod1, prod2
- change_safety: cautious | change_impact: medium
- example: wal_buffers_full: 664448 (threshold 1000)
- problem: wal_buffers переполняется — процессы ждут освобождения WAL buffer.
- GUC `wal_buffers` → increase [safety=cautious, impact=medium] current: prod1=2048, prod2=2048
  rationale: Процессы ждут освобождения WAL buffer — стабильный симптом нехватки.
  postgres_pro: Postgres Pro: при высоком wal_buffers_full увеличьте wal_buffers (единицы 8kB).
- actions:
  - Проверить пики WAL generation и batch DML
  - Увеличить wal_buffers (единицы 8kB в pg_profile settings)
  - Проверить пики WAL generation

### [high] Рост tablespace / объектов
- tuning_rule_id: tune_tablespace_growth
- finding_ids: io.table_growth
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts.statecontracts.t_value: table growth=21 GB (≥100MB)
- problem: Таблица заметно растёт за интервал отчёта — риск диска, vacuum lag и деградации планов.
- actions:
  - Проверить свободное место на томах
  - Архивирование / partitioning горячих растущих таблиц
  - Сопоставить с vacuum lag и unused indexes
  - Проверить DML-паттерн и возможность partitioning/архивации
  - Убедиться, что autovacuum успевает за ростом

### [medium] Высокая доля backend writes
- tuning_rule_id: tune_backend_writes
- finding_ids: wal.backend_writes_high
- stability: 2/2 (100%) in prod1, prod2
- change_safety: restart_required | change_impact: high
- example: Backend buffers written (21842039) exceed checkpoint buffers written (11698150)
- problem: Backend'ы сами пишут dirty buffers чаще ожидаемого — bgwriter/checkpoint
- GUC `checkpoint_completion_target` → review_increase [safety=safe, impact=low] current: prod1=0.7, prod2=0.7
  rationale: Backend writes растут, когда bgwriter/checkpoint не успевают.
  postgres_pro: Сглаживание checkpoint снижает пики dirty и долю backend writes.
- GUC `shared_buffers` → review_increase [safety=restart_required, impact=high] current: prod1=504320, prod2=537600
  rationale: Давление на buffer pool заставляет backends писать самим.
  postgres_pro: При maxwritten_clean / backend writes смотрите размер buffer pool.
- actions:
  - Проверить bgwriter_* и maxwritten_clean
  - Найти write-heavy SQL / batch DML
  - Проверить shared_buffers, bgwriter и checkpoint_completion_target
  - Сопоставить с maxwritten_clean и checkpoint write time
  - Оценить write-heavy SQL и batch DML

### [medium] Высокий seq scan
- tuning_rule_id: tune_io_planner
- finding_ids: io.high_seq_scan
- stability: 2/2 (100%) in prod1, prod2
- change_safety: cautious | change_impact: medium
- example: postgres.pg_catalog.pg_db_role_setting: seq_scan=3003316, idx_scan=121956, ratio=24.6
- problem: Много seq scan при низком idx_scan — возможно отсутствует индекс
- GUC `random_page_cost` → review_decrease_ssd [safety=cautious, impact=medium] current: prod1=2, prod2=2
  rationale: Стабильный seq scan может быть из-за planner costs.
  postgres_pro: На SSD random_page_cost 1.1–2 (PostgreSQL docs). Влияет на выбор index vs seq scan.
- GUC `effective_io_concurrency` → increase [safety=cautious, impact=low] current: prod1=300, prod2=300
  rationale: Низкий effective_io_concurrency на быстром storage.
  postgres_pro: SSD: 200+ (PostgreSQL 15+). Улучшает prefetch.
- actions:
  - EXPLAIN, индексы, ANALYZE
  - EXPLAIN проблемных запросов
  - Добавить индекс или обновить статистику (ANALYZE)

### [medium] Cache or I/O read finding
- tuning_rule_id: advise.cache.high_read_time
- finding_ids: cache.high_read_time
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts: blk_read_time=260.5s (threshold 60s)
- problem: Review cache hit ratios and disk read patterns.
- actions:
  - Identify tables with high physical reads

### [medium] One database dominates statement load
- tuning_rule_id: advise.db.statement_dominance
- finding_ids: db.statement_dominance
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts: dominates statement time 98.6% (total_exec_time=56520.08)
- problem: Одна БД кластера забирает львиную долю времени/вызовов statements —
- actions:
  - Сузить анализ top SQL / rusage к доминирующей БД
  - Проверить temp/JIT/WAL stats этой БД в statements_dbstats
  - Сверить нагрузку приложения и connection pool по БД

### [medium] I/O pattern finding
- tuning_rule_id: advise.io.high_heap_reads
- finding_ids: io.high_heap_reads
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts.statecontracts.t_contracts: heap_blks_read=4806511
- problem: Review query plans and table access patterns.
- actions:
  - Use EXPLAIN and pg_profile top I/O sections

### [medium] High vacuum/analyze operation count
- tuning_rule_id: advise.io.vacuum_ops_pressure
- finding_ids: io.vacuum_ops_pressure
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts.statecontracts.t_dspc_sys_config: vacuum_ops=151, analyze_ops=150
- problem: Много vacuum/analyze операций на таблице — высокая churn или агрессивные пороги;
- actions:
  - Проверить dead/mods % и per-table autovacuum settings
  - Снизить ненужный churn (idle in xact, частые мелкие UPDATE)
  - Согласовать окно обслуживания для тяжёлого VACUUM

### [medium] WAL-heavy SQL query
- tuning_rule_id: advise.io.wal_heavy_query
- finding_ids: io.wal_heavy_query
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: statecontracts/as_admin: wal=105.9GB, shared_blks_dirtied=5802883
  insert into statecontracts.t_value (chgcnt,date_,inn,sys_isdeleted,kpp,sys_lastchangedate,level_entityid,metrictype_e...
- problem: Запрос генерирует много WAL — типично для массовых INSERT/UPDATE.
- actions:
  - Оптимизировать batch DML
  - Проверить fillfactor и индексы на целевых таблицах

### [medium] Отключены statement/lock timeout
- tuning_rule_id: tune_timeouts
- finding_ids: memory.lock_timeout_zero, memory.statement_timeout_zero
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: lock_timeout=0 (no protection against lock waits)
- problem: Без lock_timeout приложение может бесконечно ждать блокировку,
- GUC `statement_timeout` → set_nonzero [safety=safe, impact=low] current: prod1=0, prod2=0
  rationale: Защита от runaway queries без restart.
  postgres_pro: Рекомендуется role-level timeout (30s–300s OLTP). Postgres Pro Enterprise — через ALTER ROLE.
- GUC `lock_timeout` → set_nonzero [safety=safe, impact=low] current: prod1=0, prod2=0
  rationale: Без lock_timeout приложение может ждать блокировку бесконечно.
  postgres_pro: lock_timeout 5–30s для OLTP предотвращает каскадные ожидания.
- actions:
  - Установить lock_timeout для OLTP workload
  - Установить statement_timeout для application roles
  - Отдельный лимит для batch/ETL при необходимости

### [low] Неиспользуемые индексы
- tuning_rule_id: tune_unused_index
- finding_ids: io.unused_index
- stability: 2/2 (100%) in prod1, prod2
- change_safety: safe | change_impact: low
- example: Unused index statecontracts.statecontracts.t_value.i_value_metrictype: size=1537 MB
- problem: Неиспользуемый индекс замедляет INSERT/UPDATE/DELETE и vacuum без пользы для чтения.
- actions:
  - Подтвердить отсутствие idx_scan на длинном интервале (не только один отчёт)
  - Удалить unused index в согласованное окно (снижает DML/vacuum cost)
  - Не удалять уникальные/FK-supporting индексы без проверки зависимостей
  - Подтвердить отсутствие использования на длинном интервале
  - Удалить индекс в согласованное окно

## Report-specific findings (not in all reports)
### Only in one report
#### prod1
- [warning] autovacuum.table_high_dead_pct: postgres.pgse_profile.sample_stat_tables: dead_pct=16.5%, n_dead=112895, last_autovacuum=never
- [warning] disk.tablespace_growth: Tablespace statecontracts: size_delta=135 GB (size=135 GB, threshold 500MB)
#### prod2
- [warning] autovacuum.table_many_dead_tuples: postgres.pgse_profile.sample_stat_tables: n_dead=14902, last_autovacuum=2026-08-12 11:00:20.624183+03


---

# Symptom Investigation Brief

symptom: high_wal
symptom_title: Высокая генерация WAL
report_count: 3
confirmed_causes: 2
suspected_causes: 3
possible_causes: 1

## Reports
- nt1: pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html
  interval: 2026-07-31 09:00:02+03 .. 2026-08-01 00:00:02+03 (15.0 h)
- prod1: pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html
  interval: 2026-08-11 16:30:02+03 .. 2026-08-12 09:00:02+03 (16.5 h)
- prod2: pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html
  interval: 2026-08-12 16:00:02+03 .. 2026-08-13 08:30:02+03 (16.5 h)

## Possible causes
### [confirmed] Высокая скорость генерации WAL (wal_bytes) (wal.high_generation_rate)
wal_bytes / interval выше порога — общая write-нагрузка.
- reports: nt1, prod1, prod2
- evidence:
  - [nt1] WAL generation ≈ 19972.4 MB/h
  - [prod1] WAL generation ≈ 17965.0 MB/h
  - [prod2] WAL generation ≈ 17457.9 MB/h
- confirm:
  - wal_stats: wal_bytes, wal_bytes_per_sec за интервал
  - Сравнить WAL MB/h между периодами (compare_runs / compare_nt_prod)
- refute:
  - wal_bytes низкий за интервал — симптом мог быть кратковременным вне окна отчёта

### [confirmed] Переполнение wal_buffers (wal.buffers_full)
wal_buffers_full > 0 — процессы ждут WAL buffer.
- reports: nt1, prod1, prod2
- evidence:
  - [nt1] wal_buffers_full=932205
  - [prod1] wal_buffers_full=664448
  - [prod2] wal_buffers_full=680648
- confirm:
  - wal_stats.wal_buffers_full
  - Увеличить wal_buffers на НТ и сравнить
- refute:
  - wal_buffers_full = 0 — буферы достаточны

### [suspected] WAL-heavy SQL (wal.wal_heavy_queries)
Топ SQL с высоким wal_bytes, wal_records, shared_blks_dirtied.
- reports: nt1, prod1, prod2
- evidence:
  - [nt1] WAL-heavy SQL: wal_bytes=106 GB hex=d5e326dff67d955a
  - [prod1] WAL-heavy SQL: wal_bytes=106 GB hex=d5e326dff67d955a
  - [prod2] WAL-heavy SQL: wal_bytes=106 GB hex=d5e326dff67d955a
- confirm:
  - top_statements: wal_bytes, wal_fpi для топ запросов
  - EXPLAIN: массовые UPDATE/INSERT, TOAST, FPI
- refute:
  - Топ SQL не содержит wal_bytes — WAL от batch/DDL/replication

### [suspected] Write-heavy таблицы (DML volume) (wal.high_dml_tables)
top_tables с высоким n_tup_ins/upd/del генерируют WAL.
- reports: nt1, prod1, prod2
- evidence:
  - [nt1] DML table statecontracts.t_value: ins=145000000 upd=None del=None
  - [prod1] DML table statecontracts.t_value: ins=145000000 upd=None del=None
  - [prod2] DML table statecontracts.t_value: ins=145000000 upd=None del=None
- confirm:
  - top_tables: n_tup_ins, n_tup_upd, n_tup_del
  - Сопоставить таблицы с wal-heavy SQL
- refute:
  - DML counters низкие — WAL не от OLTP DML

### [suspected] Checkpoint pressure (requested checkpoints) (wal.checkpoint_pressure)
Частые requested checkpoints из-за max_wal_size / write burst.
- reports: nt1, prod1, prod2
- evidence:
  - [nt1] checkpoints_req=56
  - [prod1] checkpoints_req=39
  - [prod2] checkpoints_req=30
- confirm:
  - cluster_stats: checkpoints_req/timed, checkpoint_write_time
  - max_wal_size vs WAL generation rate
- refute:
  - checkpoints_req низкий — checkpoint не лимитирует

### [possible] Малый max_wal_size (wal.small_max_wal_size)
max_wal_size ниже рекомендуемого при текущей write-нагрузке.
- confirm:
  - settings.max_wal_size vs wal MB/h
  - Политика диска и replication
- refute:
  - max_wal_size уже большой — причина в объёме записи, не в лимите

## Action plan
- (подтвердить wal.high_generation_rate) wal_stats: wal_bytes, wal_bytes_per_sec за интервал
- (подтвердить wal.high_generation_rate) Сравнить WAL MB/h между периодами (compare_runs / compare_nt_prod)
- (опровергнуть wal.high_generation_rate) wal_bytes низкий за интервал — симптом мог быть кратковременным вне окна отчёта
- (подтвердить wal.buffers_full) wal_stats.wal_buffers_full
- (подтвердить wal.buffers_full) Увеличить wal_buffers на НТ и сравнить
- (опровергнуть wal.buffers_full) wal_buffers_full = 0 — буферы достаточны
- (подтвердить wal.wal_heavy_queries) top_statements: wal_bytes, wal_fpi для топ запросов
- (подтвердить wal.wal_heavy_queries) EXPLAIN: массовые UPDATE/INSERT, TOAST, FPI
- (опровергнуть wal.wal_heavy_queries) Топ SQL не содержит wal_bytes — WAL от batch/DDL/replication
- (подтвердить wal.high_dml_tables) top_tables: n_tup_ins, n_tup_upd, n_tup_del
- (подтвердить wal.high_dml_tables) Сопоставить таблицы с wal-heavy SQL
- (опровергнуть wal.high_dml_tables) DML counters низкие — WAL не от OLTP DML
- (подтвердить wal.checkpoint_pressure) cluster_stats: checkpoints_req/timed, checkpoint_write_time
- (подтвердить wal.checkpoint_pressure) max_wal_size vs WAL generation rate
- (опровергнуть wal.checkpoint_pressure) checkpoints_req низкий — checkpoint не лимитирует
- (подтвердить wal.small_max_wal_size) settings.max_wal_size vs wal MB/h
- (подтвердить wal.small_max_wal_size) Политика диска и replication
