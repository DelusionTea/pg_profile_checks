# Release checklist: A / B / C готов

Перед релизом или выкладкой на стенд банка. Этапы — продуктовые (план), не путать с вариантами размещения модели A/B/C в [QWEN_HEADLESS.md](QWEN_HEADLESS.md) §4.

Автопроверка без живого Qwen:

```bash
python scripts/check_smoke.py          # CLI, e2e, oracle — не замена UI-матрицы
python scripts/check_smoke.py --full   # перед релизом UI: девять сценариев
python scripts/check_contract_backcompat.py
```

`--exit-code` у `analyze_pgprofile.py` не смотрит на oracle. Для CI по качеству:

```bash
python analyze_pgprofile.py … --output-dir ./out/ --exit-code-quality
# или отдельно:
python run_quality.py --output-dir ./out/ --exit-code
```

`--exit-code` (находки health/diff) и `--exit-code-quality` (fail oracle/quality) независимы: молча менять CI health-check нельзя.

---

## A готов — таблица влияния + экспорт + UI

Цель этапа: пара и серия прогонов дают таблицу влияния, Confluence-wiki и UI это показывают. Qwen не обязателен.

### Авто

- [ ] `python scripts/check_pair_influence_case.py` — контракт пары, CSV, wiki, oracle не `fail`.
- [ ] `python scripts/check_e2e.py` — пара + серия на трёх HTML из `resources/`.
- [ ] `python scripts/check_contract_backcompat.py` — старые `run_comparison` / `settings_diff` открываются, новые поля — надмножество.
- [ ] `python scripts/validate_contract_fixtures.py`
- [ ] `python scripts/validate_series_influence_cases.py`

Опционально: `python scripts/check_report_cases.py` с тремя HTML (входит в `check_smoke.py --full`).

### Глазами в UI (http://127.0.0.1:8090)

- [ ] Два отчёта, сценарий **«Сравнение двух отчётов (diff)»**: блок «что лучше/хуже», таблица влияния в wiki, пилюля **oracle**.
- [ ] Три НТ + симптом `high_cpu`, сценарий **«Несколько прогонов НТ»**: `influence_summary_series` в wiki, вкладка **Качество**.
- [ ] Параметр без известной связи с метрикой не получает выдуманный `affected_metric` (confidence `low`, impact `neutral`).
- [ ] `workload_match` виден в compare-insights.

### Артефакты в `output_dir`

Пара: `influence_table.json` / `.csv`, `influence_summary.md` / `.wiki`, `run_comparison.json`, `settings_diff.json`, `oracle_report.json`, `quality_report.json`.

Серия: `influence_table_series.json` / `.csv`, `influence_summary_series.md` / `.wiki`, `nt_runs.json`, `nt_runs_confluence.wiki`.

### Известный пробел

Нет. Фильтры `probable`/`proven` и warning низкой сопоставимости — в compare-insights (P2).

---

## B готов — one-click Headless Qwen

Цель этапа: один клик собирает бандл из артефактов и ходит в `dry_run` / `qwen_local` / `qwen_gateway`. Основной ввод оператор не печатает.

### Авто (без модели)

- [ ] `python scripts/check_llm_provider_layer.py`
- [ ] `python scripts/check_llm_ui.py`
- [ ] `python scripts/check_llm_policy.py`
- [ ] `python scripts/check_e2e.py` — ветка `qwen_dry` + заглушки local/gateway.
- [ ] `python run_llm.py --list-providers` — виден `dry_run` (default) и два Qwen.

### Глазами в UI

- [ ] После pg_profile-анализа есть блок **Headless Qwen**.
- [ ] Задача `summary`, провайдер `dry_run`, **Запросить Qwen**: статус готово, **«нельзя публиковать»**, ответ копируется.
- [ ] `trace_id` есть в `llm_response_summary.json` и в UI.
- [ ] Ошибка без brief / без токена шлюза — нормализованное сообщение, не HTTP 500.
- [ ] JVM-режим кнопку не показывает.

### Вариант размещения модели (из [QWEN_HEADLESS.md](QWEN_HEADLESS.md))

Это **не** этапы A/B/C продукта, а куда ходит провайдер. Для «B готов» достаточно `dry_run` + прогон заглушки. Боевой контур — по месту:

| Вариант | Когда | Проверка |
| --- | --- | --- |
| **A** приложение и модель на одном хосте | `qwen_local`, `http://127.0.0.1:8000/v1` | `run_llm.py --check-connection --provider qwen_local` |
| **B** GPU в VLAN | `PGPROFILE_LLM_BASE_URL=https://…/v1` | то же + TLS/`no_proxy` |
| **C** шлюз банка | `qwen_gateway` + `PGPROFILE_LLM_TOKEN` | `--list-providers` → `token=set`, затем `--check-connection` |

Чеклист приёмки на удалённом сервере — [QWEN_HEADLESS.md §8](QWEN_HEADLESS.md).

---

## C готов — oracle и quality gate

Цель этапа: каждый анализ имеет отчёт качества; ответ модели не публикуется, если структура/claims не сходятся с таблицей.

### Авто

- [ ] `python scripts/check_oracle.py`
- [ ] `python scripts/check_oracle_statistical.py`
- [ ] `python scripts/check_llm_validate.py`
- [ ] `python scripts/check_quality_report.py`
- [ ] `python run_quality.py --output-dir analysis_out_test/e2e/pair`

### Глазами в UI

- [ ] Пилюля **oracle** (`pass` / `warning` / `fail`) кликабельна → вкладка **Качество**.
- [ ] Trail confidence: зачем строка стала `low` (много GUC, нет связи с метрикой, шум серии).
- [ ] После Qwen: quality score, `publishable`. `dry_run` всегда блокирует публикацию.
- [ ] На тройке `10.3.81.94` серийный oracle **не fail** из‑за полярности `checkpoint_completion_target` / `checkpoint_write_time`. Warning по огромному Δ допустим.

### Правила публикации LLM

`publishable = (quality != fail) and not dry_run`. CLI печатает `PUBLISH_BLOCKED`. Claims только из influence/findings; `proven` на строке `probable` — fail.

---

## Совместимость со старыми артефактами

Гарантии `scripts/check_contract_backcompat.py`:

1. Каталог только с legacy `run_comparison.json` и `settings_diff.json` (без `contract` / `workload_match` / influence) **не роняет** UI summary.
2. Текущие JSON — **надмножество** legacy-ключей: `run_a`, `run_b`, `summary.significant_count` и т.д. не удалялись.
3. Свежая пара ещё отдаёт в summary `influence` и `oracle`.

Фикстуры: `resources/contract_fixtures/legacy_*.json`.

Не обещаем, что каталог health-check 2024 года сам по себе вырастет в `influence_table.json` — таблица появляется только при `--compare-run` + `--compare-settings` или `--nt-reports`.

---

## Минимальный smoke перед релизом

```bash
python scripts/check_smoke.py          # CLI/контракты/e2e, без девяти UI-сценариев
python scripts/check_smoke.py --full   # обязательно перед релизом UI
```

Короткий smoke **копирует** `e2e/series` → `case_matrix/nt_runs_3nt_one_symptom`, если матрицы ещё нет. Это **не** замена `check_report_cases.py`: перед выкладкой UI нужен `--full` (девять сценариев через `ui/analysis_runner`).

Порядок короткого smoke (все offline, кроме in-process заглушки Qwen на `127.0.0.1`):

1. knowledge + JSON-контракты + серийные фикстуры
2. пара реальных отчётов и backcompat
3. LLM unit (validate, UI job, policy, provider layer)
4. E2E пара / серия / dry_run + local/gateway stub
5. oracle + quality на получившихся каталогах

`--full` добавляет `check_report_cases.py` (девять UI-сценариев). Не считайте e2e-копию серии этой матрицей.

Выход `0` и строка `SMOKE_PASSED` — можно релизить этап C на стенд без живого Qwen. Боевой Qwen подтверждают отдельно по варианту A/B/C размещения.
