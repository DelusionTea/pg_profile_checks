# NT Multi-Run Analysis Brief

symptoms: high_cpu
reports: nt1, nt2, nt3

## Reports
- nt1: pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html (2026 07 31 09 00 .. 2026 08 01 00)
- nt2: pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html (2026 08 11 16 30 .. 2026 08 12 09)
- nt3: pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html (2026 08 12 16 00 .. 2026 08 13 08)

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

### nt2 → nt3
Между прогонами nt2 → nt3 изменены настройки; ниже — вероятное влияние на метрики (корреляция, не доказательство причинности):
- shared_buffers: 504320 (3940.0 MB) → 537600 (4200.0 MB) (increased); уверенность: possible
  • Увеличение shared_buffers повышает cache hit и снижает физические чтения (CPU/IO).
  • В этом сравнении: blks_read +26.9%, blk_read_time +19.9%, blk_write_time -26.2% — направление согласуется с ожидаемым эффектом настройки
