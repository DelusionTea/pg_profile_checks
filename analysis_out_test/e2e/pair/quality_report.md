# Качество: WARNING

Публикация ответа LLM: нет ответа модели в этом прогоне.

## Слои

- `rule_based`: warning
- `statistical`: pass (пропущен)
- `llm`: pass (пропущен)

## Почему

- cache.postgres.blk_write_time Δ=1600.0% looks implausible

## Confidence

- `прогон` [unchanged]: medium → medium (probable). Confidence baseline for pair comparison without isolated causality proof.
- `commit_timestamp_buffers` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `pg_conf_load_time` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `pg_postmaster_start_time` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `shared_buffers` [unchanged]: medium → medium (probable). Confidence baseline for pair comparison without isolated causality proof.
- `shared_memory_size` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `shared_memory_size_in_huge_pages` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `subtransaction_buffers` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low
- `transaction_buffers` [downgrade]: medium → low (probable). unattributed; confidence downgraded to low

## LLM

Ответа модели нет — слой llm пропущен.
