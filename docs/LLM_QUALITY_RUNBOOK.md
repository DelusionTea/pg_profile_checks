# Runbook: LLM, quality gate и oracle

Краткая диагностика, когда ответ Qwen не публикуется, job падает или пилюля **oracle** красная. Цифры и таблицы влияния всегда из Python; модель только комментирует уже посчитанное.

Связанные документы: [README — таблица влияния](../README.md#3a-таблица-влияния-guc--метрики), [настройка Qwen](QWEN_HEADLESS.md).

## 1. Где смотреть

| Где | Что |
| --- | --- |
| UI, пилюля **oracle** | `pass` / `warning` / `fail`. Клик открывает вкладку **Качество**. |
| UI, блок **Headless Qwen** | статус job, quality, «нельзя публиковать», текст ответа, `trace_id` |
| UI, вкладка **Качество** | слои oracle, trail confidence, gate публикации |
| `oracle_report.json` / `.md` | вердикт, причины, слои `rule_based` / `statistical` / `llm` |
| `quality_report.json` / `.md` | то же плюс `confidence_trail` и блок `llm` |
| `llm_job.json` | статус one-click job, `error`, `publishable`, `quality_score` |
| `llm_quality_<task>.json` | разбор ответа: score, checks, claims |
| `llm_request_<task>.json` | что ушло в модель (без токена) |
| `llm_response_<task>.json` | сырой ответ, latency, `trace_id` |

CLI:

```bash
python run_quality.py --output-dir analysis_out
python run_quality.py --output-dir analysis_out --json --exit-code
python run_llm.py --output-dir analysis_out --task summary --provider dry_run
```

`--exit-code` у `analyze_pgprofile.py` **не** смотрит на oracle/quality. Красный oracle не ломает CI health-check, пока явно не вызвать `--exit-code-quality` или `run_quality.py --exit-code`.

## 2. Quality gate для ответа модели

После любого вызова (UI или `run_llm.py`) ответ разбирается как JSON:

обязательные поля `verdict`, `summary`, `claims`, `recommendations`, `risks`, `missing_data`;
`verdict` ∈ `go` | `no-go` | `need-validation`.

| Score | Вердикт quality | Публикация |
| --- | --- | --- |
| ≥ 80, нет warning | `pass` | да, если это не `dry_run` |
| 50–79 или dry-run / warning-check | `warning` | нет при `dry_run`, иначе да |
| < 50 или structural/claim fail | `fail` | нет |

`publishable = (quality_verdict != fail) and not dry_run`.

В CLI при блоке печатается `PUBLISH_BLOCKED`. В UI статус **«нельзя публиковать»**, копировать текст всё равно можно — в Confluence его не несут как готовый вердикт.

Типичные fail-check'и в `llm_quality_*.json`:

| `id` | Смысл |
| --- | --- |
| `llm.structure` | ответ не JSON или нет обязательных полей |
| `llm.claim_grounded` | `subject` нет в `influence_table*.json` / findings |
| `llm.claim_overconfident` | модель написала `proven`, а строка таблицы — `probable` |
| `llm.publish` | итоговый запрет публикации |

## 3. Ошибки транспорта и конфигурации

Сообщение — класс из `pgprofile_llm.py`. В UI оно лежит в `llm_job.json` → `error`.

| Симптом | Класс / текст | Что делать |
| --- | --- | --- |
| Нет токена шлюза | `LLMConfigError: token is expected in env …` | `export PGPROFILE_LLM_TOKEN=…`, затем `--list-providers` → `token=set` |
| 401 / 403 | `LLMAuthError` | неверный токен, не тот заголовок, IP не в allowlist |
| Тишина дольше timeout | `LLMTimeoutError` | поднять `PGPROFILE_LLM_TIMEOUT`, проверить GPU OOM, таймаут шлюза |
| Сеть | `LLMTransportError: connection failed` | DNS, firewall, `https_proxy` / `no_proxy` |
| HTTP 404 | `LLMTransportError: HTTP 404` | неверный `path` или имя модели |
| Пустой ответ | `LLMResponseError: no text at response_path` | шлюз кладёт текст в другое поле; thinking ушёл в `reasoning_content` |
| Нет brief | job `fail`, `LLMBundleError` | сначала анализ; в каталоге должен быть `brief.md` или `nt_runs_brief.md` |
| Уже идёт запрос | UI: конфликт job | дождаться пилюли «готово» / «ошибка» |
| TLS | certificate verify failed | `SSL_CERT_FILE` на внутренний УЦ |

Preflight отдельно от промпта:

```bash
python run_llm.py --list-providers
python run_llm.py --check-connection --provider qwen_local
python run_llm.py --output-dir analysis_out --task summary --print-bundle
```

## 4. Oracle по таблице влияния

| Вердикт | Когда |
| --- | --- |
| `pass` | поля на месте, знак Δ и impact согласованы |
| `warning` | подозрительная величина (например `|Δ%| > 500`) при целой структуре |
| `fail` | дырявый контракт, NaN/inf, `proven` без изоляции, impact противоречит полярности метрики |

Слои:

- `rule_based` — полнота и знак Δ;
- `statistical` — серия (≥3 пары, stability, шум IQR);
- `llm` — quality ответа модели (пропускается, если модели не было).

**Не путать** `direction` в серийной строке (`increased` / `decreased` — как изменился GUC) с направлением метрики (`metric_direction`: `up` / `down` / `flat`). Oracle смотрит на метрику.

Серийный `impact` считается по доминантной метрике, поэтому на тройке `10.3.81.94` строка `checkpoint_completion_target` больше не получает `improved` при росте `checkpoint_write_time`. Если oracle всё же `fail` — вкладка **Качество**, поле `where`.

## 5. Типовые сценарии

### «Запросил Qwen — нельзя публиковать»

1. Провайдер `dry_run`? Так и должно быть: цепочка жива, в Confluence не несут.
2. Иначе откройте `llm_quality_summary.json` → `reasons` / `checks`.
3. Если `claim_grounded` — модель сослалась на параметр, которого нет в таблице. Перегенерируйте или поправьте extra-instructions: «только параметры из influence_table».

### Job сразу `ошибка`

1. В каталоге сессии нет brief — прогоните анализ ещё раз.
2. Выбран `qwen_gateway`, токена нет — подсказка провайдера в UI это показывает.
3. `llm_job.json` → `error.message`.

### Пилюля oracle `fail` после серии НТ

1. Вкладка **Качество** / `oracle_report.md` — конкретная строка (`where`).
2. Сверьте `impact` и `delta_pct` в `influence_table_series.json`.
3. Wiki и таблица влияния всё равно пригодны как **гипотеза** (`probable`), не как доказанная причинность.

### CLI напечатал `PUBLISH_BLOCKED`, код выхода 0

Это не сбой анализа. Quality gate отказал в публикации. Смотрите score и `llm_quality_*.json`. Для CI по качеству: `python analyze_pgprofile.py … --exit-code-quality` или `python run_quality.py --output-dir … --exit-code`.

## 6. Что уходит в модель

Бандл собирается из артефактов каталога (`brief`, influence summary, findings) плюс короткие extra-instructions из UI. Токен в промпт не попадает.

| Policy | Что происходит |
| --- | --- |
| `none` (yaml default) | Текст не меняется. В findings может быть SQL. |
| `bank_redact` | Маскирование по профилю в `llm_policy.yaml`. Включают на контуре (`PGPROFILE_LLM_POLICY`), не коммитят как default без решения ИБ. |

ZIP / gigacli этот gate не проходят: коллега отдаёт файлы вручную.

## 7. Что не чинить «на глаз»

- Не подставляйте параметр → метрику, если в `knowledge/guc_impact.yaml` / hints её нет: строка останется без `affected_metric`, confidence `low`.
- Не повышайте `evidence_type` до `proven` вручную в JSON.
- Токены не кладут в `llm_providers.yaml` и не коммитят.
