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
