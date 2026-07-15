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
