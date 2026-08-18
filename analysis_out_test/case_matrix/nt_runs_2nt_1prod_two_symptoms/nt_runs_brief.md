# NT Multi-Run Analysis Brief

symptoms: high_cpu, high_wal
reports: nt1, nt2

## Reports
- nt1: pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html (2026 07 31 09 00 .. 2026 08 01 00)
- nt2: pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html (2026 08 11 16 30 .. 2026 08 12 09)

## PROD baseline reports
- prod1: pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html (2026 08 12 16 00 .. 2026 08 13 08)

## Symptom: Высокая утилизация CPU БД (high_cpu)
### Confirmed causes
- [cpu.dominant_queries] Доминирующие SQL по CPU (pg_stat_kcache)
  - [nt1] Топ CPU: sum_cpu_time=23229.9s, user_time_pct=31.9%
  - [nt1] hex=e2a55c68949e5d1e: select v1_0.object_id from statecontracts.t_value v1_0 where v1_0.inn is not nul…
  - [nt1] #2: sum_cpu_time=14123.0s hex=d5e326dff67d955a
### Suspected causes
- [cpu.high_call_volume] Высокий объём вызовов (CPU × calls)
  - [nt1] Высокий объём: calls=144,990,757, total_exec_time=17811.6s, mean=0.12ms hex=d5e326dff67d955a
  - [nt1] Высокий объём: calls=144,990,682, total_exec_time=12702.9s, mean=0.09ms hex=31df6528995548cf
- [cpu.checkpoint_bgwriter] Checkpoint / bgwriter / IO wait в kernel CPU
  - [nt1] checkpoints_req=56, checkpoint_write_time=25187.0s
  - [nt2] checkpoints_req=39, checkpoint_write_time=38898.3s
- [cpu.autovacuum_pressure] Autovacuum / analyze во время нагрузки
  - [nt1] Bloat: pgse_profile.sample_statements dead_pct=10.15296960859762%
  - [nt2] Bloat: pgse_profile.sample_stat_tables dead_pct=16.536570289395474%

## Symptom: Высокая генерация WAL (high_wal)
### Confirmed causes
- [wal.high_generation_rate] Высокая скорость генерации WAL (wal_bytes)
  - [nt1] WAL generation ≈ 19972.4 MB/h
  - [nt2] WAL generation ≈ 17965.0 MB/h
- [wal.buffers_full] Переполнение wal_buffers
  - [nt1] wal_buffers_full=932205
  - [nt2] wal_buffers_full=664448
### Suspected causes
- [wal.wal_heavy_queries] WAL-heavy SQL
  - [nt1] WAL-heavy SQL: wal_bytes=106 GB hex=d5e326dff67d955a
  - [nt2] WAL-heavy SQL: wal_bytes=106 GB hex=d5e326dff67d955a
- [wal.high_dml_tables] Write-heavy таблицы (DML volume)
  - [nt1] DML table statecontracts.t_value: ins=145000000 upd=None del=None
  - [nt2] DML table statecontracts.t_value: ins=145000000 upd=None del=None
- [wal.checkpoint_pressure] Checkpoint pressure (requested checkpoints)
  - [nt1] checkpoints_req=56
  - [nt2] checkpoints_req=39

## Settings change impact (pairwise)
### nt1 → nt2
Между прогонами nt1 → nt2 изменены настройки; ниже — вероятное влияние на метрики (корреляция, не доказательство причинности):
- checkpoint_completion_target: 0.5 → 0.7 (increased); уверенность: possible
  • Более высокий checkpoint_completion_target растягивает checkpoint во времени и сглаживает IO spikes.
  • В этом сравнении: checkpoint_write_time +54.4%, checkpoint_sync_time -27.0%, checkpoints_req -30.4% — направление согласуется с ожидаемым эффектом настройки
- huge_pages: — → on (changed); уверенность: possible
  • В этом сравнении: blk_read_time +6.7% — направление согласуется с ожидаемым эффектом настройки
- max_wal_size: 8192 (64.0 MB) → 12288 (96.0 MB) (increased); уверенность: possible
  • Увеличение max_wal_size снижает частоту requested checkpoints при высокой WAL generation.
  • В этом сравнении: checkpoints_req -30.4%, checkpoint_write_time +54.4%, wal_buffers_full -28.7% — направление согласуется с ожидаемым эффектом настройки

## NT vs PROD problem overlap
### Высокая утилизация CPU БД
- divergence_criticality: medium
- existing_on_prod: cpu.checkpoint_bgwriter, cpu.dominant_queries, cpu.high_call_volume
- nt_only: cpu.autovacuum_pressure
- critical_nt_only: none

### Высокая генерация WAL
- divergence_criticality: low
- existing_on_prod: wal.buffers_full, wal.checkpoint_pressure, wal.high_dml_tables, wal.high_generation_rate, wal.wal_heavy_queries
- nt_only: none
- critical_nt_only: none

## NT vs PROD divergence summary
- nt1 vs prod1: settings_valid=false, performance_warnings=42, critical_settings=9
- nt2 vs prod1: settings_valid=false, performance_warnings=36, critical_settings=6
