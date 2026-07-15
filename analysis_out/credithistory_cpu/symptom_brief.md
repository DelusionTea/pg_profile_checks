# Symptom Investigation Brief

symptom: high_cpu
symptom_title: Высокая утилизация CPU БД
report_count: 2
confirmed_causes: 0
suspected_causes: 3
possible_causes: 3

## Reports
- prom1: credithistory_prom1.html
  interval: 2026-07-06 21:00:02+03 .. 2026-07-07 21:00:02+03 (24.0 h)
- prom2: credithistory_prom2.html
  interval: 2026-07-07 21:00:02+03 .. 2026-07-08 21:00:01+03 (24.0 h)

## Possible causes
### [suspected] Доминирующие SQL по CPU (pg_stat_kcache) (cpu.dominant_queries)
Несколько запросов потребляют большую долю user/system CPU за интервал.
- reports: prom1, prom2
- evidence:
  - [prom1] Топ CPU: sum_cpu_time=121.6s, user_time_pct=17.8%
  - [prom1] hex=4aeabfa00fd66418: SELECT pgse_profile.take_sample()
  - [prom1] #2: sum_cpu_time=96.3s hex=790639d3aaede809
  - [prom1] #3: sum_cpu_time=85.8s hex=a822446855e79f13
  - [prom2] Топ CPU: sum_cpu_time=123.7s, user_time_pct=17.9%
- confirm:
  - EXPLAIN (ANALYZE, BUFFERS) для топ-3 SQL по sum_cpu_time из отчёта
  - Сопоставить pg_stat_statements (calls, mean_exec_time) с pg_stat_kcache (sum_cpu_time)
  - Проверить, совпадает ли пик CPU на OS с интервалом отчёта pg_profile
- refute:
  - Если sum_cpu_time топ-запросов <5% интервала — CPU уходит не в SQL (фон, autovacuum, checkpoint)
  - Сравнить топ CPU между двумя периодами: запрос исчез из топа — не корневая причина текущего пика

### [suspected] Checkpoint / bgwriter / IO wait в kernel CPU (cpu.checkpoint_bgwriter)
Высокий system_time, checkpoint write, maxwritten_clean — kernel CPU на запись.
- reports: prom1, prom2
- evidence:
  - [prom1] checkpoints_req=3, checkpoint_write_time=11518.9s
  - [prom2] checkpoints_req=4, checkpoint_write_time=11835.0s
- confirm:
  - cluster_stats: checkpoints_req, checkpoint_write_time, maxwritten_clean
  - Сопоставить system_time_pct в top_rusage с WAL/checkpoint метриками
- refute:
  - checkpoints_req низкий и checkpoint_write_time мал — не checkpoint

### [suspected] Autovacuum / analyze во время нагрузки (cpu.autovacuum_pressure)
Bloat, stale vacuum, высокий mods/dead_pct — autovacuum конкурирует за CPU.
- reports: prom1, prom2
- evidence:
  - [prom1] Bloat: credithistory.t_repl_agglock_auditevent dead_pct=50.0%
  - [prom2] Bloat: pgse_profile.sample_stat_tables dead_pct=15.541335217131541%
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

### [possible] Высокий объём вызовов (CPU × calls) (cpu.high_call_volume)
Умеренное mean_exec_time при очень большом calls даёт высокий суммарный CPU.
- confirm:
  - Проверить calls и total_exec_time в top_statements для топ CPU запросов
  - Найти источник частых вызовов (ORM N+1, polling, отсутствие pool)
- refute:
  - Если calls низкий при высоком CPU — причина не в частоте, а в тяжёлом плане/функции

## Action plan
- [подтвердить cpu.dominant_queries] EXPLAIN (ANALYZE, BUFFERS) для топ-3 SQL по sum_cpu_time из отчёта
- [подтвердить cpu.dominant_queries] Сопоставить pg_stat_statements (calls, mean_exec_time) с pg_stat_kcache (sum_cpu_time)
- [подтвердить cpu.dominant_queries] Проверить, совпадает ли пик CPU на OS с интервалом отчёта pg_profile
- [опровергнуть cpu.dominant_queries] Если sum_cpu_time топ-запросов <5% интервала — CPU уходит не в SQL (фон, autovacuum, checkpoint)
- [опровергнуть cpu.dominant_queries] Сравнить топ CPU между двумя периодами: запрос исчез из топа — не корневая причина текущего пика
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
- [подтвердить cpu.high_call_volume] Проверить calls и total_exec_time в top_statements для топ CPU запросов
- [подтвердить cpu.high_call_volume] Найти источник частых вызовов (ORM N+1, polling, отсутствие pool)
