# AI-ready README

Индекс для агента. Открой **один** файл из таблицы ниже. Корневой `README.md` — для человека; не копируй его в контекст.

Готово, когда: выбран ровно один playbook, изменены только его швы, зелены его тесты.

## Два продукта

| Режим | Вход | Код | Не смешивать с |
| --- | --- | --- | --- |
| PG | HTML pg_profile (`const data={...}`) | корневые `pgprofile_*.py`, `ui/analysis_runner.py` | JVM YAML |
| JVM | папка АС: `resources.yaml` + `jvm-config.*` | `jvmcheck_runtime/`, `ui/jvm_runner.py` | HTML pg_profile |

UI: `ui/server.py` → http://127.0.0.1:8090/ — переключатель PG / JVM.

## Указатель

| Задача | Файл | Открывать ещё |
| --- | --- | --- |
| Формат конфигов АС, новые поля YAML, «добавить систему», файлы → ссылки Bitbucket/Git | [system-config.md](system-config.md) | только файлы из его таблицы швов |
| Дерево JVM (рестарты, GC, SLA памяти, wiki) | код: `jvmcheck_runtime/src/jvmcheck/diagnostic_tree.py`, playbook: `jvmcheck_runtime/knowledge/jvm_diagnostic_tree.yaml` | тесты ниже |
| Пороги / правила JVM | `jvmcheck_runtime/thresholds_jvm.yaml`, `jvmcheck_runtime/knowledge/` | — |
| PG health / findings / Confluence | корневой `README.md` разделы «Быстрый старт» и «Структура» **точечно**, не целиком | `scripts/check_smoke.py` |
| Порядок отчётов, период отчёта, даты | швы: `parse_report_period` / `sort_reports_by_date` в `pgprofile_parser.py`, `order_reports_by_date` в `analyze_pgprofile.py`, `session_from_request` в `ui/analysis_runner.py` | `scripts/check_report_order.py` |
| План действий в wiki (стабильные / общие проблемы) | шов: `_stable_prod_action_plan_wiki` / `_stable_prod_now_actions` в `pgprofile_confluence.py`; знания: `knowledge/prod_tuning.yaml` (`operational`, `guc_recommendations`), `knowledge/recommendations.yaml` (`actions`) | `scripts/check_action_plan.py` |
| Содержательность wiki по сценариям PG (проблема, критичность, действие; без дублей) | швы: `_health_now_actions`, `_symptom_now_actions`, `_nt_prod_now_actions`, `_collapse_finding_rows`, вердикты в `pgprofile_confluence.py`; `build_nt_runs_confluence_wiki` в `pgprofile_nt_runs.py`; карточки `_build_findings_ui` в `ui/analysis_runner.py` | `scripts/check_pg_output.py`, `scripts/check_confluence_ux.py` |
| Шаги PG-UI: сценарий, загрузка нескольких отчётов | `ui/web/index.html` (`#scenario-panel` — шаг 00), `ui/web/js/app.js` (`selectedScenario`, `effectiveScenario`, `usesReportColumns`, `addFiles`) | `scripts/check_pg_ui_flow.py` |
| Эталон DML с ПРОМ (max insert/update/delete) | `pgprofile_dml_etalon.py`; адаптеры: `run_pipeline` / `session_from_request`; UI: option `dml_etalon` | `scripts/check_dml_etalon.py` |
| LLM / Qwen | `docs/QWEN_HEADLESS.md` | `docs/LLM_QUALITY_RUNBOOK.md` |
| Релиз / CI | `docs/RELEASE_CHECKLIST.md` | `scripts/check_smoke.py` |

## Как работать

1. Сопоставь задачу строке указателя. Открой этот файл.
2. Меняй только швы из его таблицы. Парсеры оставляют тот же тип наружу (`PodResourcesBudget`, `Dict[str, List[str]]`).
3. Тест — из того же файла. Не зелёный → не готово.
4. После правок `ui/*.py`, `ui/web/**` перезапусти `.venv/bin/python ui/server.py`.
5. Коммит только если пользователь попросил.

## Инварианты

- Python 3.10+, UI без новых pip-пакетов (stdlib + PyYAML).
- LLM по умолчанию `dry_run`. Цифры считает Python, не модель.
- Память JVM: SLA 80%; до 80% больше 30 дней — не критично, limit не поднимать. Ровно 30 дней ещё в окне.
- Wiki «Выкатывать JAVA_TOOL_OPTIONS *Да*» только если `pause_copyable_allowed` (рост отклика пользователя + GC p95 выше 250 мс).
- Не выдумывать G1-флаги. Не копировать G1, если причина — CPU throttle / RSS / живые объекты.
- Не коммитить секреты. Токен Qwen только `PGPROFILE_LLM_TOKEN`.
- Сценарий PG выбирается в шаге 00 и не зависит от «Расширенных настроек». Дефолт — `auto`. В анализ идут все загруженные отчёты; до одного файла список режет только явный `health`.
- Порядок отчётов PG — всегда по периоду из данных отчёта (`report_start1`), не по порядку загрузки и не по имени файла. Метки едут вместе со своим отчётом. Пара `--report` / `--compare-run` не переставляется: базу выбрал вызывающий.

## Тесты (минимум)

```bash
.venv/bin/python scripts/check_jvm_diagnostic_tree.py
.venv/bin/python scripts/check_jvm_tree_cases.py
.venv/bin/python scripts/check_report_order.py
.venv/bin/python scripts/check_action_plan.py
.venv/bin/python scripts/check_pg_output.py
.venv/bin/python scripts/check_pg_ui_flow.py
.venv/bin/python scripts/check_dml_etalon.py
.venv/bin/python scripts/check_smoke.py
```

После HTML/JS/Python UI: перезапуск `ui/server.py`, в браузере http://127.0.0.1:8090/

## Замороженные контракты

Не ломай без явной задачи: JSON influence/oracle/quality, `--exit-code`, default LLM `dry_run`, GUC→метрика только из hints, порог GC p95 = 250 мс, SLA памяти 80% / 30 дней.
