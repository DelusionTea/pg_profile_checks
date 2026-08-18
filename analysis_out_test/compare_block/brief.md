# pg_profile Analysis Brief

Source type: `health_check`

- Server: pslod-pprb00823.cloud.omega.sbrf.ru
- Interval: 2026-07-06 21:00:02+03 .. 2026-07-07 21:00:02+03 (24.0 h)
- Report: credithistory_prom1.html

Total findings: 56

## [WARNING] High checkpoint write time per hour
- **ID:** `checkpoints.high_write_time_per_hour`
- **Message:** Checkpoint write time: 480.0s/hour (threshold 60s/hour)
- **Recommendation:** Нормализованное время записи checkpoint за час высокое — устойчивое IO-давление
от checkpoints, а не только длинный единичный интервал отчёта.
- **Actions:**
  - Поднять checkpoint_completion_target (0.7–0.9) для сглаживания записи
  - Согласовать max_wal_size и checkpoint_timeout с объёмом WAL
  - Проверить latency диска (iostat) в окнах checkpoint

## [WARNING] Long total checkpoint write time
- **ID:** `checkpoints.high_write_time`
- **Message:** Checkpoint write time: 11518.9s over 24.0h interval (threshold 300s)
- **Recommendation:** Длительная запись checkpoint увеличивает IO latency и может влиять на
время отклика запросов во время checkpoint.
- **Actions:**
  - Проверить производительность диска (IOPS, latency)
  - Оценить checkpoint_completion_target
  - Снизить write-нагрузку или распределить её во времени

## [WARNING] Bgwriter interrupts (maxwritten_clean)
- **ID:** `checkpoints.maxwritten_clean`
- **Message:** Bgwriter interrupts (maxwritten_clean): 1028 (threshold 100)
- **Recommendation:** Bgwriter часто прерывается из-за max_write_pages — буферы сбрасываются
медленнее, чем backend их загрязняет.
- **Actions:**
  - Увеличить shared_buffers при низком cache hit
  - Настроить bgwriter (в новых версиях часто autotune)
  - Проверить checkpoint и WAL pressure

## [WARNING] Slow SQL queries detected
- **ID:** `queries.slow_execution`
- **Message:** postgres/postgres: mean=2796.9ms, total=134.2s, calls=48
  SELECT pgse_profile.take_sample()
- **Recommendation:** Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
плана, индексов или переписывания SQL.
- **Actions:**
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)
  - Оценить work_mem при temp spills

## [WARNING] Slow SQL queries detected
- **ID:** `queries.slow_execution`
- **Message:** credithistory/as_admin: total=82.7s, calls=874845
  update credithistory.t_value set sys_lastchangedate=$1,metricvalue=$2,rquid=$3,sourcedatetime=$4 where object_id=$5
- **Recommendation:** Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
плана, индексов или переписывания SQL.
- **Actions:**
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)
  - Оценить work_mem при temp spills

## [WARNING] Slow SQL queries detected
- **ID:** `queries.slow_execution`
- **Message:** credithistory/as_admin: total=90.5s, calls=876478
  SELECT txtype, changeeventmask, aggtype, ownerid, migrationstatus, silock, siversion, guid, version FROM credithistor...
- **Recommendation:** Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
плана, индексов или переписывания SQL.
- **Actions:**
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)
  - Оценить work_mem при temp spills

## [WARNING] Slow SQL queries detected
- **ID:** `queries.slow_execution`
- **Message:** credithistory/as_admin: total=65.2s, calls=875008
  select v1_0.object_id from credithistory.t_value v1_0 where v1_0.ucpid is not null and v1_0.ucpid=$1 and v1_0.metrict...
- **Recommendation:** Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
плана, индексов или переписывания SQL.
- **Actions:**
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)
  - Оценить work_mem при temp spills

## [WARNING] Slow SQL queries detected
- **ID:** `queries.slow_execution`
- **Message:** postgres/"sa-o00000003249": total=78.0s, calls=86139
  WITH locks AS ( SELECT pid, locktype, mode, relation, page, tuple, virtualxid, transactionid, classid, objid, objsubi...
- **Recommendation:** Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
плана, индексов или переписывания SQL.
- **Actions:**
  - Выполнить EXPLAIN (ANALYZE, BUFFERS) для топ-запросов
  - Проверить индексы и статистику (ANALYZE)
  - Оценить work_mem при temp spills

## [WARNING] High modified tuples ratio since analyze
- **ID:** `autovacuum.table_high_mods_pct`
- **Message:** credithistory.credithistory.t_repl_agglock_auditevent: mods_pct=100.0%, last_autoanalyze=never
- **Recommendation:** Высокий n_mod_since_analyze — статистика устаревает, планировщик может выбирать
плохие планы. Autovacuum analyze не успевает за DML.
- **Actions:**
  - Снизить autovacuum_analyze_scale_factor / threshold на горячих таблицах
  - Ускорить autovacuum (naptime, cost_delay/limit)
  - Выполнить ANALYZE в окно обслуживания при необходимости

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** credithistory.credithistory.t_value: dead_pct=13.9%, n_dead=1392350, last_autovacuum=2026-07-06 08:45:17.991906+03
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_kcache: dead_pct=13.6%, n_dead=13574, last_autovacuum=2026-07-05 15:00:11.233547+03
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_statements: dead_pct=12.2%, n_dead=12792, last_autovacuum=2026-07-05 21:30:14.77639+03
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_stat_tables: dead_pct=10.0%, n_dead=25061, last_autovacuum=2026-07-06 06:30:11.04073+03
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_kcache: stale autovacuum (54h ago), dead_pct=13.6%
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** credithistory.credithistory.t_value: stale autovacuum (36h ago), dead_pct=13.9%
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_statements: stale autovacuum (47h ago), dead_pct=12.2%
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] High dead tuples ratio on table
- **ID:** `autovacuum.table_high_dead_pct`
- **Message:** postgres.pgse_profile.sample_stat_tables: stale autovacuum (38h ago), dead_pct=10.0%
- **Recommendation:** Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
long transactions.
- **Actions:**
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

## [WARNING] WAL buffers frequently full
- **ID:** `wal.buffers_full`
- **Message:** wal_buffers_full: 7260 (threshold 1000)
- **Recommendation:** wal_buffers переполняется — процессы ждут освобождения WAL buffer.
Типичная рекомендация коммьюнити: увеличить wal_buffers (часто 64MB на busy systems).
- **Actions:**
  - Увеличить wal_buffers (единицы 8kB в pg_profile settings)
  - Проверить пики WAL generation

## [WARNING] Cache or I/O read finding
- **ID:** `cache.high_read_time`
- **Message:** credithistory: blk_read_time=117.1s (threshold 60s)
- **Recommendation:** Review cache hit ratios and disk read patterns.
- **Actions:**
  - Identify tables with high physical reads

## [WARNING] Low table I/O hit ratio
- **ID:** `cache.low_table_hit_ratio`
- **Message:** postgres.pgse_profile.sample_statements: table hit_pct=92.80% (threshold 95.0%)
- **Recommendation:** Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
холодный working set или неоптимальные планы/индексы.
- **Actions:**
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

## [WARNING] Low table I/O hit ratio
- **ID:** `cache.low_table_hit_ratio`
- **Message:** postgres.pgse_profile.sample_kcache: table hit_pct=92.02% (threshold 95.0%)
- **Recommendation:** Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
холодный working set или неоптимальные планы/индексы.
- **Actions:**
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

## [WARNING] Low table I/O hit ratio
- **ID:** `cache.low_table_hit_ratio`
- **Message:** postgres.pgse_profile.sample_stat_tables_total: table hit_pct=91.82% (threshold 95.0%)
- **Recommendation:** Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
холодный working set или неоптимальные планы/индексы.
- **Actions:**
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

## [WARNING] Low table I/O hit ratio
- **ID:** `cache.low_table_hit_ratio`
- **Message:** postgres.pgse_profile.sample_statements_total: table hit_pct=91.50% (threshold 95.0%)
- **Recommendation:** Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
холодный working set или неоптимальные планы/индексы.
- **Actions:**
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

## [WARNING] Low table I/O hit ratio
- **ID:** `cache.low_table_hit_ratio`
- **Message:** postgres.pgse_profile.last_stat_activity_count_srv2: table hit_pct=94.03% (threshold 95.0%)
- **Recommendation:** Горячие таблицы читаются с диска чаще ожидаемого — узкий buffer pool,
холодный working set или неоптимальные планы/индексы.
- **Actions:**
  - Найти top I/O tables в pg_profile и проверить индексы/partitioning
  - Оценить shared_buffers и effective_cache_size
  - EXPLAIN (ANALYZE, BUFFERS) для запросов к этим таблицам

## [CRITICAL] High idle in transaction time
- **ID:** `sessions.high_idle_in_transaction`
- **Message:** credithistory: idle_in_transaction_time=13277.7s (threshold 3600s), idle_in_transaction_session_timeout=0
- **Recommendation:** Долгие idle in transaction сессии держат snapshots, блокируют vacuum
и увеличивают bloat — одна из самых частых рекомендаций коммьюнити.
- **Actions:**
  - Установить idle_in_transaction_session_timeout
  - Исправить application connection pooling / missing COMMIT
  - Найти источник зависших транзакций в pg_stat_activity

... and 31 more findings


---

# pg_profile Analysis Brief

Source type: `run_comparison`

- Run A [before]: 24.0 h
- Run B [after]: 15.0 h
- **Interval mismatch:** 9.0 h difference — use /hour values

Total findings: 94

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.buffers_backend`
- **Message:** buffers_backend
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.buffers_checkpoint`
- **Message:** buffers_checkpoint
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.checkpoint_sync_time`
- **Message:** checkpoint_sync_time
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.checkpoint_write_time`
- **Message:** checkpoint_write_time
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.checkpoints_req`
- **Message:** checkpoints_req
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.checkpoints_timed`
- **Message:** checkpoints_timed
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.maxwritten_clean`
- **Message:** maxwritten_clean
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.cluster.wal_size`
- **Message:** wal_size
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.wal.wal_buffers_full`
- **Message:** wal_buffers_full
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.wal.wal_bytes`
- **Message:** wal_bytes
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.wal.wal_records`
- **Message:** wal_records
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.wal.wal_sync`
- **Message:** wal_sync
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.wal.wal_write`
- **Message:** wal_write
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.INSERT`
- **Message:** Total.INSERT
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.UPDATE`
- **Message:** Total.UPDATE
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.DELETE`
- **Message:** Total.DELETE
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.COMMIT`
- **Message:** Total.COMMIT
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.ROLLBACK`
- **Message:** Total.ROLLBACK
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.Total.FETCH`
- **Message:** Total.FETCH
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.credithistory.INSERT`
- **Message:** credithistory.INSERT
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.credithistory.UPDATE`
- **Message:** credithistory.UPDATE
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.credithistory.DELETE`
- **Message:** credithistory.DELETE
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.credithistory.COMMIT`
- **Message:** credithistory.COMMIT
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.credithistory.FETCH`
- **Message:** credithistory.FETCH
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

## [WARNING] Metric changed between test runs
- **ID:** `run_compare.dml.postgres.INSERT`
- **Message:** postgres.INSERT
- **Recommendation:** Метрика изменилась между прогонами. При разной длительности интервалов
ориентируйтесь на значения /час, а не только на абсолютные счётчики.
- **Actions:**
  - Сравнить per-hour значения
  - Сопоставить с изменениями нагрузки или конфигурации

... and 69 more findings


---

# pg_profile Analysis Brief

Source type: `settings_diff`

- Run A: before
- Run B: after

Total findings: 26

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.archive_command`
- **Message:** archive_command
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.only_nt.archive_mode`
- **Message:** archive_mode
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.authentication_max_workers`
- **Message:** authentication_max_workers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.autovacuum_work_mem`
- **Message:** autovacuum_work_mem
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.cluster_name`
- **Message:** cluster_name
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.commit_timestamp_buffers`
- **Message:** commit_timestamp_buffers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.only_nt.disk_retry_count`
- **Message:** disk_retry_count
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.effective_cache_size`
- **Message:** effective_cache_size
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения
- **GUC note:** Подсказка планировщику о доступном кэше ОС. Не выделяет память.
- **Typical OLTP:** ~50-75% RAM estimate

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.force_failover_timeout`
- **Message:** force_failover_timeout
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.maintenance_work_mem`
- **Message:** maintenance_work_mem
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения
- **GUC note:** Влияет на скорость VACUUM, CREATE INDEX, ALTER TABLE.
- **Typical OLTP:** 256MB-2GB+

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.max_parallel_workers`
- **Message:** max_parallel_workers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.max_prepared_transactions`
- **Message:** max_prepared_transactions
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.only_nt.max_worker_processes`
- **Message:** max_worker_processes
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.only_nt.performance_insights.directory`
- **Message:** performance_insights.directory
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.primary_slot_name`
- **Message:** primary_slot_name
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.only_prod.sec_admin_default_auth`
- **Message:** sec_admin_default_auth
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.shared_buffers`
- **Message:** shared_buffers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения
- **GUC note:** Основной кэш страниц. Слишком низкий shared_buffers → low cache hit.
При shared_buffers ≳ 2GB планируйте HugePages (см. huge_pages).
- **Typical OLTP:** ~25% RAM (policy-dependent)

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.shared_memory_size`
- **Message:** shared_memory_size
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.shared_memory_size_in_huge_pages`
- **Message:** shared_memory_size_in_huge_pages
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.subtransaction_buffers`
- **Message:** subtransaction_buffers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.synchronous_standby_names`
- **Message:** synchronous_standby_names
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.transaction_buffers`
- **Message:** transaction_buffers
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [WARNING] PostgreSQL setting differs between environments
- **ID:** `settings.differ.version`
- **Message:** version
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [INFO] PostgreSQL setting differs between environments
- **ID:** `settings.differ.pg_conf_load_time`
- **Message:** pg_conf_load_time
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

## [INFO] PostgreSQL setting differs between environments
- **ID:** `settings.differ.pg_postmaster_start_time`
- **Message:** pg_postmaster_start_time
- **Recommendation:** Параметр явно задан (Defined) в одной среде и отличается от другой.
Убедитесь, что расхождение намеренное и соответствует политике банка.
- **Actions:**
  - Сверить с guc_guidance.yaml для данного параметра
  - Документировать обоснование расхождения

... and 1 more findings
