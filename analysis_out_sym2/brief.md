# Symptom Investigation Brief

symptom: high_wal
symptom_title: Высокая генерация WAL
report_count: 1
confirmed_causes: 2
suspected_causes: 3
possible_causes: 1

## Reports
- counteragent_prom1: counteragent_prom1.html
  interval: 2026-07-01 05:00:02+03 .. 2026-07-01 21:00:01+03 (16.0 h)

## Possible causes
### [confirmed] Высокая скорость генерации WAL (wal_bytes) (wal.high_generation_rate)
wal_bytes / interval выше порога — общая write-нагрузка.
- reports: counteragent_prom1
- evidence:
  - [counteragent_prom1] WAL generation ≈ 123028.1 MB/h
- confirm:
  - wal_stats: wal_bytes, wal_bytes_per_sec за интервал
  - Сравнить WAL MB/h между периодами (compare_runs / compare_nt_prod)
- refute:
  - wal_bytes низкий за интервал — симптом мог быть кратковременным вне окна отчёта

### [confirmed] Переполнение wal_buffers (wal.buffers_full)
wal_buffers_full > 0 — процессы ждут WAL buffer.
- reports: counteragent_prom1
- evidence:
  - [counteragent_prom1] wal_buffers_full=23440729
- confirm:
  - wal_stats.wal_buffers_full
  - Увеличить wal_buffers на НТ и сравнить
- refute:
  - wal_buffers_full = 0 — буферы достаточны

### [suspected] WAL-heavy SQL (wal.wal_heavy_queries)
Топ SQL с высоким wal_bytes, wal_records, shared_blks_dirtied.
- reports: counteragent_prom1
- evidence:
  - [counteragent_prom1] WAL-heavy SQL: wal_bytes=522 GB hex=11a74fb9c1776a85
- confirm:
  - top_statements: wal_bytes, wal_fpi для топ запросов
  - EXPLAIN: массовые UPDATE/INSERT, TOAST, FPI
- refute:
  - Топ SQL не содержит wal_bytes — WAL от batch/DDL/replication

### [suspected] Write-heavy таблицы (DML volume) (wal.high_dml_tables)
top_tables с высоким n_tup_ins/upd/del генерируют WAL.
- reports: counteragent_prom1
- evidence:
  - [counteragent_prom1] DML table counteragent.t_repl_agglock_transaction: ins=5290969 upd=34407832 del=None
- confirm:
  - top_tables: n_tup_ins, n_tup_upd, n_tup_del
  - Сопоставить таблицы с wal-heavy SQL
- refute:
  - DML counters низкие — WAL не от OLTP DML

### [suspected] Checkpoint pressure (requested checkpoints) (wal.checkpoint_pressure)
Частые requested checkpoints из-за max_wal_size / write burst.
- reports: counteragent_prom1
- evidence:
  - [counteragent_prom1] checkpoints_req=181
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
- [подтвердить wal.high_generation_rate] wal_stats: wal_bytes, wal_bytes_per_sec за интервал
- [подтвердить wal.high_generation_rate] Сравнить WAL MB/h между периодами (compare_runs / compare_nt_prod)
- [опровергнуть wal.high_generation_rate] wal_bytes низкий за интервал — симптом мог быть кратковременным вне окна отчёта
- [подтвердить wal.buffers_full] wal_stats.wal_buffers_full
- [подтвердить wal.buffers_full] Увеличить wal_buffers на НТ и сравнить
- [опровергнуть wal.buffers_full] wal_buffers_full = 0 — буферы достаточны
- [подтвердить wal.wal_heavy_queries] top_statements: wal_bytes, wal_fpi для топ запросов
- [подтвердить wal.wal_heavy_queries] EXPLAIN: массовые UPDATE/INSERT, TOAST, FPI
- [опровергнуть wal.wal_heavy_queries] Топ SQL не содержит wal_bytes — WAL от batch/DDL/replication
- [подтвердить wal.high_dml_tables] top_tables: n_tup_ins, n_tup_upd, n_tup_del
- [подтвердить wal.high_dml_tables] Сопоставить таблицы с wal-heavy SQL
- [опровергнуть wal.high_dml_tables] DML counters низкие — WAL не от OLTP DML
- [подтвердить wal.checkpoint_pressure] cluster_stats: checkpoints_req/timed, checkpoint_write_time
- [подтвердить wal.checkpoint_pressure] max_wal_size vs WAL generation rate
- [опровергнуть wal.checkpoint_pressure] checkpoints_req низкий — checkpoint не лимитирует
- [подтвердить wal.small_max_wal_size] settings.max_wal_size vs wal MB/h
- [подтвердить wal.small_max_wal_size] Политика диска и replication
