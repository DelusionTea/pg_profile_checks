# Stable PROD Analysis Brief

min_stability_ratio: 1.0
report_count: 2
stable_findings_count: 23
tuning_recommendations_count: 14
ephemeral_findings_count: 4

## Reports
- prom1: counteragent_prom1.html
  server: pslod-pprb00796.cloud.omega.sbrf.ru
  interval: 2026-07-01 05:00:02+03 .. 2026-07-01 21:00:01+03 (16.0 h)
  findings_in_report: 80
- prom2: counteragent_prom2.html
  server: pslod-pprb00796.cloud.omega.sbrf.ru
  interval: 2026-07-04 10:00:02+03 .. 2026-07-04 23:00:02+03 (13.0 h)
  findings_in_report: 85

## Stable tuning recommendations (sorted by problem severity)
### [critical] Idle in transaction / нет таймаута
- tuning_rule_id: tune_idle_in_transaction
- finding_ids: sessions.high_idle_in_transaction, sessions.idle_timeout_disabled
- stability: 2/2 (100%) in prom1, prom2
- change_safety: cautious | change_impact: medium
- example: counteragent: idle_in_transaction_time=953878.0s (threshold 3600s), idle_in_transaction_session_timeout=0
- problem: Долгие idle in transaction сессии держат snapshots, блокируют vacuum
- GUC `idle_in_transaction_session_timeout` → set_nonzero [safety=cautious, impact=medium] current: prom1=0, prom2=0
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
- finding_ids: cache.low_hit_ratio, cache.low_table_hit_ratio, memory.work_mem_connections_risk
- stability: 2/2 (100%) in prom1, prom2
- change_safety: restart_required | change_impact: high
- example: counteragent: blks_hit_pct=84.96% (threshold 99.0%)
- problem: Низкий blks_hit_pct — данные часто читаются с диска. Проверьте размер
- GUC `shared_buffers` → increase [safety=restart_required, impact=high] current: prom1=1026816, prom2=1026816
  rationale: Низкий cache hit при стабильном паттерне — кандидат на увеличение буферов.
  postgres_pro: Postgres Pro: shared_buffers ~25% RAM (политика банка). Требует restart.
- GUC `effective_cache_size` → review_increase [safety=safe, impact=low] current: prom1=3145728, prom2=3145728
  rationale: Помогает планировщику при низком blks_hit_pct.
  postgres_pro: Подсказка планировщику (~50–75% RAM). Не выделяет память — безопасно для reload.
- GUC `work_mem` → review_decrease_or_role_level [safety=risky, impact=high] current: prom1=16384, prom2=16384
  rationale: Стабильный риск OOM при высоком work_mem и connections.
  postgres_pro: work_mem × параллельные sorts = риск OOM. Postgres Pro: задавать на уровне роли для тяжёлых запросов.
- actions:
  - Connection pooler (PgBouncer)
  - Таблицы с высоким physical read
  - Увеличить shared_buffers (ориентир 25% RAM, с учётом политики банка)
  - Проверить effective_cache_size для планировщика
  - Найти таблицы с высоким physical read

### [high] Длительная запись checkpoint / bgwriter
- tuning_rule_id: tune_checkpoints_write
- finding_ids: checkpoints.high_write_time, checkpoints.high_write_time_per_hour, checkpoints.maxwritten_clean
- stability: 2/2 (100%) in prom1, prom2
- change_safety: restart_required | change_impact: high
- example: Checkpoint write time: 22768.8s over 16.0h interval (threshold 300s)
- problem: Длительная запись checkpoint увеличивает IO latency и может влиять на
- GUC `checkpoint_completion_target` → review_increase [safety=safe, impact=low] current: prom1=0.5, prom2=0.5
  rationale: Длинный checkpoint write time стабильно на PROD.
  postgres_pro: Растягивание checkpoint (0.7–0.9) снижает latency spikes.
- GUC `shared_buffers` → review_increase [safety=restart_required, impact=high] current: prom1=1026816, prom2=1026816
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
- stability: 2/2 (100%) in prom1, prom2
- change_safety: risky | change_impact: high
- example: counteragent/as_admin: mean=57670.1ms, max=191210.5ms, total=1730.1s, calls=30
  select 0, count(t0.DESTINATIONCLIENT_ENTITYID) c2, t0.DESTINATIONCLIENT_ENTITYID c3 from counteragent.T_FEEDBACKWAITI..
- problem: Запросы с высоким mean/max/total execution time — кандидаты на оптимизацию
- GUC `work_mem` → review_increase_role_level [safety=risky, impact=high] current: prom1=16384, prom2=16384
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
- finding_ids: checkpoints.high_requested_ratio, checkpoints.high_requested_count, checkpoints.high_requested_per_hour
- stability: 2/2 (100%) in prom1, prom2
- change_safety: cautious | change_impact: medium
- example: Requested checkpoints: 181/185 (97.8%), threshold 30%
- problem: Большая доля requested checkpoints означает, что WAL заполняется быстрее,
- GUC `max_wal_size` → increase [safety=cautious, impact=medium] current: prom1=16384, prom2=16384
  rationale: Малый max_wal_size — частая причина requested checkpoints при стабильной write-нагрузке.
  postgres_pro: Postgres Pro / PostgreSQL: увеличение max_wal_size снижает частоту requested checkpoints.
- GUC `checkpoint_completion_target` → review_increase [safety=safe, impact=low] current: prom1=0.5, prom2=0.5
  rationale: Сглаживает IO-пики при длинных checkpoint write.
  postgres_pro: Значение 0.7–0.9 растягивает checkpoint во времени (PostgreSQL docs).
- actions:
  - Сравнить WAL MB/h между периодами (compare_nt_prod / compare_runs)
  - Проверить wal-heavy SQL в pg_profile
  - Увеличить max_wal_size (часто 1–4 GB+ для OLTP под нагрузкой)
  - Проверить wal_buffers_full и общий объём WAL за интервал
  - Сопоставить с checkpoint_timeout — не уменьшать timeout вместо max_wal_size

### [high] Autovacuum не успевает / bloat
- tuning_rule_id: tune_autovacuum_bloat
- finding_ids: autovacuum.table_high_dead_pct, autovacuum.table_high_mods_pct
- stability: 2/2 (100%) in prom1, prom2
- change_safety: cautious | change_impact: medium
- example: counteragent.counteragent.t_clientfeaturemapping: dead_pct=53.6%, n_dead=165979, last_autovacuum=never
- problem: Высокий процент мёртвых строк — autovacuum не успевает или заблокирован
- GUC `autovacuum_naptime` → decrease [safety=cautious, impact=medium] current: prom1=10, prom2=10
  rationale: Высокий naptime замедляет запуск vacuum на «горячих» таблицах.
  postgres_pro: Для OLTP часто 15–60 с. Уменьшение ускоряет реакцию на bloat, но повышает фоновый IO.
- GUC `autovacuum_vacuum_cost_delay` → decrease [safety=cautious, impact=medium] current: prom1=10, prom2=10
  rationale: Autovacuum «ползёт» при высоком cost_delay.
  postgres_pro: Снижение cost_delay ускоряет vacuum ценой IO (PostgreSQL cost-based vacuum delay).
- GUC `autovacuum_vacuum_cost_limit` → increase [safety=cautious, impact=medium] current: prom1=1000, prom2=1000
  rationale: Низкий limit — типичная причина медленного autovacuum.
  postgres_pro: Увеличение cost_limit даёт vacuum больший IO-бюджет за цикл.
- GUC `maintenance_work_mem` → increase [safety=cautious, impact=medium] current: prom1=3354624, prom2=3354624
  rationale: Ускоряет очистку при стабильном bloat.
  postgres_pro: Больше maintenance_work_mem ускоряет VACUUM и CREATE INDEX (PostgreSQL docs).
- actions:
  - Проверить idle in transaction (блокирует vacuum)
  - Per-table autovacuum для hot tables
  - Проверить idle in transaction и lock waits
  - Настроить per-table autovacuum при необходимости
  - Рассмотреть VACUUM (ANALYZE) в окно обслуживания

### [high] Переполнение wal_buffers
- tuning_rule_id: tune_wal_buffers
- finding_ids: wal.buffers_full
- stability: 2/2 (100%) in prom1, prom2
- change_safety: cautious | change_impact: medium
- example: wal_buffers_full: 23440729 (threshold 1000)
- problem: wal_buffers переполняется — процессы ждут освобождения WAL buffer.
- GUC `wal_buffers` → increase [safety=cautious, impact=medium] current: prom1=2048, prom2=2048
  rationale: Процессы ждут освобождения WAL buffer — стабильный симптом нехватки.
  postgres_pro: Postgres Pro: при высоком wal_buffers_full увеличьте wal_buffers (единицы 8kB).
- actions:
  - Проверить пики WAL generation и batch DML
  - Увеличить wal_buffers (единицы 8kB в pg_profile settings)
  - Проверить пики WAL generation

### [medium] WAL/checkpoint (общее)
- tuning_rule_id: tune_generic_wal
- finding_ids: wal.backend_writes_high
- stability: 2/2 (100%) in prom1, prom2
- change_safety: cautious | change_impact: medium
- example: Backend buffers written (314566620) exceed checkpoint buffers written (34553877)
- problem: Review WAL generation and checkpoint configuration.
- GUC `max_wal_size` → review [safety=cautious, impact=medium] current: prom1=16384, prom2=16384
  rationale: Стабильная WAL-аномалия без точного sub-rule.
  postgres_pro: Review WAL metrics against Postgres Pro tuning guide.
- actions:
  - Check max_wal_size and wal_buffers

### [medium] Cache or I/O read finding
- tuning_rule_id: advise.cache.high_read_time
- finding_ids: cache.high_read_time
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: counteragent: blk_read_time=196673.6s (threshold 60s)
- problem: Review cache hit ratios and disk read patterns.
- actions:
  - Identify tables with high physical reads

### [medium] Cache or I/O read finding
- tuning_rule_id: advise.cache.temp_usage
- finding_ids: cache.temp_usage
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: counteragent: temp usage detected (temp_bytes=699 MB, temp_files=8)
- problem: Review cache hit ratios and disk read patterns.
- actions:
  - Identify tables with high physical reads

### [medium] I/O pattern finding
- tuning_rule_id: advise.io.high_heap_reads
- finding_ids: io.high_heap_reads
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: counteragent.counteragent.t_processednotificationcandidate: heap_blks_read=198321587
- problem: Review query plans and table access patterns.
- actions:
  - Use EXPLAIN and pg_profile top I/O sections

### [medium] WAL-heavy SQL query
- tuning_rule_id: advise.io.wal_heavy_query
- finding_ids: io.wal_heavy_query
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: counteragent/as_admin: wal=522.0GB
  update counteragent.t_transaction set sys_lastchangedate=$1,rquid=$2,validto=$3 where object_id=$4
- problem: Запрос генерирует много WAL — типично для массовых INSERT/UPDATE.
- actions:
  - Оптимизировать batch DML
  - Проверить fillfactor и индексы на целевых таблицах

### [medium] Отключены statement/lock timeout
- tuning_rule_id: tune_timeouts
- finding_ids: memory.lock_timeout_zero, memory.statement_timeout_zero
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: lock_timeout=0 (no protection against lock waits)
- problem: Без lock_timeout приложение может бесконечно ждать блокировку,
- GUC `statement_timeout` → set_nonzero [safety=safe, impact=low] current: prom1=0, prom2=0
  rationale: Защита от runaway queries без restart.
  postgres_pro: Рекомендуется role-level timeout (30s–300s OLTP). Postgres Pro Enterprise — через ALTER ROLE.
- GUC `lock_timeout` → set_nonzero [safety=safe, impact=low] current: prom1=0, prom2=0
  rationale: Без lock_timeout приложение может ждать блокировку бесконечно.
  postgres_pro: lock_timeout 5–30s для OLTP предотвращает каскадные ожидания.
- actions:
  - Установить lock_timeout для OLTP workload
  - Установить statement_timeout для application roles
  - Отдельный лимит для batch/ETL при необходимости

### [low] Unused index
- tuning_rule_id: advise.io.unused_index
- finding_ids: io.unused_index
- stability: 2/2 (100%) in prom1, prom2
- change_safety: safe | change_impact: low
- example: Unused index counteragent.counteragent.t_transaction.i_transaction_rquid: size=1206 MB
- problem: Неиспользуемый индекс замедляет INSERT/UPDATE/DELETE и vacuum без пользы для чтения.
- actions:
  - Подтвердить отсутствие использования на длинном интервале
  - Удалить индекс в согласованное окно

## Ephemeral findings (not in all reports)
- [warning] autovacuum.generic: 1/2 — prom1
- [warning] io.high_seq_scan: 1/2 — prom2
- [critical] sessions.abnormal_termination: 1/2 — prom2
- [warning] sessions.high_rollback_ratio: 1/2 — prom2


---

# Symptom Investigation Brief

symptom: high_cpu
symptom_title: Высокая утилизация CPU БД
report_count: 2
confirmed_causes: 1
suspected_causes: 3
possible_causes: 2

## Reports
- prom1: counteragent_prom1.html
  interval: 2026-07-01 05:00:02+03 .. 2026-07-01 21:00:01+03 (16.0 h)
- prom2: counteragent_prom2.html
  interval: 2026-07-04 10:00:02+03 .. 2026-07-04 23:00:02+03 (13.0 h)

## Possible causes
### [confirmed] Доминирующие SQL по CPU (pg_stat_kcache) (cpu.dominant_queries)
Несколько запросов потребляют большую долю user/system CPU за интервал.
- reports: prom1, prom2
- evidence:
  - [prom1] Топ CPU: sum_cpu_time=6050.2s, user_time_pct=9.7%
  - [prom1] hex=11a74fb9c1776a85: update counteragent.t_transaction set sys_lastchangedate=$1,rquid=$2,validto=$3 …
  - [prom1] #2: sum_cpu_time=5432.9s hex=a1fcd5d63d26f0a7
  - [prom1] #3: sum_cpu_time=5214.5s hex=541f3e835c8bf2f3
  - [prom2] Топ CPU: sum_cpu_time=6904.7s, user_time_pct=13.3%
- confirm:
  - EXPLAIN (ANALYZE, BUFFERS) для топ-3 SQL по sum_cpu_time из отчёта
  - Сопоставить pg_stat_statements (calls, mean_exec_time) с pg_stat_kcache (sum_cpu_time)
  - Проверить, совпадает ли пик CPU на OS с интервалом отчёта pg_profile
- refute:
  - Если sum_cpu_time топ-запросов <5% интервала — CPU уходит не в SQL (фон, autovacuum, checkpoint)
  - Сравнить топ CPU между двумя периодами: запрос исчез из топа — не корневая причина текущего пика

### [suspected] Высокий объём вызовов (CPU × calls) (cpu.high_call_volume)
Умеренное mean_exec_time при очень большом calls даёт высокий суммарный CPU.
- reports: prom1, prom2
- evidence:
  - [prom1] Высокий объём: calls=28,399,920, total_exec_time=77554.6s, mean=2.73ms hex=11a74fb9c1776a85
  - [prom1] Высокий объём: calls=39,694,024, total_exec_time=44526.3s, mean=1.12ms hex=541f3e835c8bf2f3
  - [prom1] Высокий объём: calls=24,696,855, total_exec_time=24938.1s, mean=1.01ms hex=9caf3ac0769402eb
  - [prom1] Высокий объём: calls=24,812,094, total_exec_time=17363.3s, mean=0.70ms hex=bd7c3f63db986839
  - [prom1] Высокий объём: calls=34,406,203, total_exec_time=11478.1s, mean=0.33ms hex=a1fcd5d63d26f0a7
- confirm:
  - Проверить calls и total_exec_time в top_statements для топ CPU запросов
  - Найти источник частых вызовов (ORM N+1, polling, отсутствие pool)
- refute:
  - Если calls низкий при высоком CPU — причина не в частоте, а в тяжёлом плане/функции

### [suspected] Checkpoint / bgwriter / IO wait в kernel CPU (cpu.checkpoint_bgwriter)
Высокий system_time, checkpoint write, maxwritten_clean — kernel CPU на запись.
- reports: prom1, prom2
- evidence:
  - [prom1] checkpoints_req=181, checkpoint_write_time=22768.8s
  - [prom2] checkpoints_req=154, checkpoint_write_time=21018.5s
- confirm:
  - cluster_stats: checkpoints_req, checkpoint_write_time, maxwritten_clean
  - Сопоставить system_time_pct в top_rusage с WAL/checkpoint метриками
- refute:
  - checkpoints_req низкий и checkpoint_write_time мал — не checkpoint

### [suspected] Autovacuum / analyze во время нагрузки (cpu.autovacuum_pressure)
Bloat, stale vacuum, высокий mods/dead_pct — autovacuum конкурирует за CPU.
- reports: prom1, prom2
- evidence:
  - [prom1] Bloat: counteragent.t_clientfeaturemapping dead_pct=53.591401043550135%
  - [prom2] Bloat: pgse_profile.sample_statements dead_pct=16.300568821413986%
- confirm:
  - Проверить top_tbl_last_sample: dead_pct, mods_pct, last_autovacuum
  - pg_stat_activity: autovacuum workers в пик CPU
  - Логи autovacuum (log_autovacuum_min_duration)
- refute:
  - Нет таблиц с высоким dead_pct и autovacuum свежий — маловероятно

### [possible] Параллельные запросы / max_parallel_workers (cpu.parallel_workers)
Много parallel workers увеличивают суммарный CPU.
- confirm:
  - EXPLAIN: Gather / Parallel Seq Scan в топ SQL
  - Проверить max_parallel_workers_per_gather и load
- refute:
  - Планы без parallel nodes при высоком CPU — ищите другие причины

### [possible] JIT-компиляция запросов (cpu.jit_overhead)
JIT включён и топ SQL имеет заметный jit_total_time.
- confirm:
  - Проверить jit_* поля в top_statements для медленных/тяжёлых запросов
  - Сравнить plan time vs exec time; jit_generation_time в pg_profile
  - Тест с SET jit=off для подозрительного запроса на НТ
- refute:
  - jit_total_time отсутствует или << total_exec_time — JIT не доминирует

## Action plan
- [подтвердить cpu.dominant_queries] EXPLAIN (ANALYZE, BUFFERS) для топ-3 SQL по sum_cpu_time из отчёта
- [подтвердить cpu.dominant_queries] Сопоставить pg_stat_statements (calls, mean_exec_time) с pg_stat_kcache (sum_cpu_time)
- [подтвердить cpu.dominant_queries] Проверить, совпадает ли пик CPU на OS с интервалом отчёта pg_profile
- [опровергнуть cpu.dominant_queries] Если sum_cpu_time топ-запросов <5% интервала — CPU уходит не в SQL (фон, autovacuum, checkpoint)
- [опровергнуть cpu.dominant_queries] Сравнить топ CPU между двумя периодами: запрос исчез из топа — не корневая причина текущего пика
- [подтвердить cpu.high_call_volume] Проверить calls и total_exec_time в top_statements для топ CPU запросов
- [подтвердить cpu.high_call_volume] Найти источник частых вызовов (ORM N+1, polling, отсутствие pool)
- [опровергнуть cpu.high_call_volume] Если calls низкий при высоком CPU — причина не в частоте, а в тяжёлом плане/функции
- [подтвердить cpu.checkpoint_bgwriter] cluster_stats: checkpoints_req, checkpoint_write_time, maxwritten_clean
- [подтвердить cpu.checkpoint_bgwriter] Сопоставить system_time_pct в top_rusage с WAL/checkpoint метриками
- [опровергнуть cpu.checkpoint_bgwriter] checkpoints_req низкий и checkpoint_write_time мал — не checkpoint
- [подтвердить cpu.autovacuum_pressure] Проверить top_tbl_last_sample: dead_pct, mods_pct, last_autovacuum
- [подтвердить cpu.autovacuum_pressure] pg_stat_activity: autovacuum workers в пик CPU
- [подтвердить cpu.autovacuum_pressure] Логи autovacuum (log_autovacuum_min_duration)
- [опровергнуть cpu.autovacuum_pressure] Нет таблиц с высоким dead_pct и autovacuum свежий — маловероятно
- [подтвердить cpu.parallel_workers] EXPLAIN: Gather / Parallel Seq Scan в топ SQL
- [подтвердить cpu.parallel_workers] Проверить max_parallel_workers_per_gather и load
- [подтвердить cpu.jit_overhead] Проверить jit_* поля в top_statements для медленных/тяжёлых запросов
- [подтвердить cpu.jit_overhead] Сравнить plan time vs exec time; jit_generation_time в pg_profile
- [подтвердить cpu.jit_overhead] Тест с SET jit=off для подозрительного запроса на НТ
