# План: углубление архитектуры (после P0–P3)

Исходный обзор: locality размазана в оркестрации, доменные module уже deep. Не трогаем `pgprofile_parser`, `pgprofile_compare`, `pgprofile_health`, `pgprofile_findings`, `pgprofile_advisor`, ядро `analyze_nt_runs` / `investigate_symptom`, `pgprofile_llm` + `llm_tasks`, `ui/llm_runner`.

Не меняем пользовательские контракты: JSON influence/oracle/quality, `--exit-code`, default LLM `dry_run`, GUC→метрика только из hints.

Порядок обязателен: каждый шаг зелёный до следующего.

Статус: **R1–R5 выполнены** (валидация скриптами после каждого шага).

```
R1 JVM seam ──► R2 quality snapshot ──► R3 session ──► R4 influence→wiki ──► R5 UiPayload
```

Приёмка шага: указанные скрипты + `py_compile` затронутых файлов. UI после Python/JS — перезапуск `ui/server.py`.

---

## R1 — JVM и PG: два module, два adapter

**Зачем.** `ui/analysis_runner.py` смешивает PG-сессию и JVM (~1100 строк). Баг GUC и баг флагов живут в одном файле.

**Сделать.**

- `ui/models.py`: `ReportMeta`, `AnalyzeRequest`, `AnalyzeResult`, `JvmAnalyzeRequest`.
- `ui/jvm_runner.py`: каталог проблем, `list_jvm_*`, `run_jvm_analysis`, last_input.
- `ui/analysis_runner.py`: только PG (`run_analysis`, `build_namespace`, `_build_summary`, zip).
- `ui/server.py`: PG из `analysis_runner`, JVM из `jvm_runner`.

Поведение UI (PG/JVM) не меняется.

**Приёмка.**

```bash
.venv/bin/python -m py_compile ui/models.py ui/jvm_runner.py ui/analysis_runner.py ui/server.py
.venv/bin/python -c "from ui.jvm_runner import list_jvm_problems, list_jvm_systems; assert list_jvm_problems(); print(list_jvm_systems()[:3])"
.venv/bin/python scripts/check_ui_nt_runs_case.py
.venv/bin/python scripts/check_llm_ui.py
```

В UI: переключатель PG/JVM, список АС, запуск demo JVM, обычный NT-анализ.

---

## R2 — один quality snapshot

**Зачем.** `write_oracle_report` всегда зовёт `write_quality_report`; `record_llm_quality` заново гоняет весь oracle. Trail иногда пересчитывает `recommend_series_confidence`.

**Сделать.**

- Один вход: `evaluate_quality(output_dir) -> snapshot` (oracle payload + trail + llm block).
- `write_oracle_report` / `write_quality_report` пишут файлы из snapshot, без второго `evaluate_output_dir`.
- После LLM: дописать слой llm в snapshot и перезаписать отчёты; rule/stat слои не пересчитывать, если influence JSON не менялся.
- Trail: брать `confidence_reasons` со строки; не звать `recommend_series_confidence`, если reasons уже есть (как сейчас для непустого списка).

Артефакты `oracle_report.json` / `quality_report.json` с теми же полями.

**Приёмка.**

```bash
.venv/bin/python scripts/check_oracle.py
.venv/bin/python scripts/check_oracle_statistical.py
.venv/bin/python scripts/check_quality_report.py
.venv/bin/python scripts/check_llm_validate.py
.venv/bin/python scripts/check_llm_ui.py
```

Пара `10.3.81.94`: вкладка Качество, после dry_run Qwen — llm слой без смены rule verdict.

---

## R3 — сессия анализа вместо «выучить Namespace»

**Зачем.** UI собирает `argparse.Namespace`. Multi-symptom есть только в UI.

**Сделать.**

- `pgprofile_session.py`: dataclass `AnalysisSession` с теми же атрибутами, что CLI Namespace.
- CLI: `validate_args` / `run_pipeline` принимают session (Namespace → session на входе).
- UI: `session_from_request` вместо ручной сборки Namespace; `run_pipeline(session)`.
- Multi-symptom fan-out остаётся в `run_analysis` (поведение то же); не тащим JVM в session.

Не раздуваем interface: никаких новых CLI-флагов.

**Приёмка.**

```bash
.venv/bin/python scripts/check_e2e.py
.venv/bin/python scripts/check_ui_nt_runs_case.py
.venv/bin/python analyze_pgprofile.py --help
```

CLI пара и серия НТ, UI `nt_runs` и health — те же артефакты.

---

## R4 — wiki серии читает influence payload

**Зачем.** `build_nt_runs_confluence_wiki` заново делает `load_settings` по HTML, хотя `influence_series.settings_table` уже есть. Два источника цифр.

**Сделать.**

- Расширить series `settings_table`: `rows` (изменившиеся) + `equal_rows` (без изменений), `run_labels` включая PROD, если они в `nt_runs`.
- Wiki: таблицы настроек только из payload; HTML не парсить второй раз для GUC.
- Метрики по-прежнему из `metrics_table`.

Набор GUC в wiki совпадает с тем, что было на тройке `10.3.81.94` (changed ∪ equal).

**Приёмка.**

```bash
.venv/bin/python scripts/validate_series_influence_cases.py
.venv/bin/python scripts/check_e2e.py
.venv/bin/python scripts/check_ui_nt_runs_case.py
```

Wiki: «Отличия настроек» и expand «Одинаковые настройки» заполнены; числа GUC не из второго парса HTML.

---

## R5 — compare view-model на сервере

**Зачем.** `app.js` заново считает mode, `score < 0.6`, список GUC и подсказки confidence.

**Сделать.**

- `_build_summary` кладёт `compare`: `mode`, `workload_weak`, `changed_params`, `confidence_hints` (уже посчитанные).
- `app.js` рендерит эти поля; фильтры/сортировка строк остаются в браузере (состояние формы).
- E2E проверяет JSON summary, не имена JS-функций (кроме того, что фильтры ещё есть).

**Приёмка.**

```bash
.venv/bin/python scripts/check_e2e.py
.venv/bin/python scripts/check_smoke.py
```

UI пара и серия: mode, warning нагрузки, список GUC, фильтры.

---

## Вне плана (не делать)

- Слияние pair и series *builders* в одну функцию (разные implementation, общий row schema уже есть).
- Перенос `app.js` фильтров на сервер (query params).
- Рефактор `pgprofile_confluence.py` целиком.
- Коммит — только по отдельной просьбе.
