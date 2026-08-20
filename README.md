# pg_profile_checks

Набор Python-скриптов для анализа HTML-отчётов [pg_profile](https://github.com/zubkov-andrei/pg_profile).

Скрипты читают данные прямо из HTML-файла: отчёт pg_profile — это одностраничное приложение, внутри которого в JavaScript-объекте `const data={...}` лежат все метрики, настройки и тексты запросов. Внешняя база данных не нужна.

**Агенту:** не читай этот файл целиком. Индекс — [docs/ai/README.md](docs/ai/README.md). Конфиги АС и ссылки Bitbucket — [docs/ai/system-config.md](docs/ai/system-config.md).

## Архитектура

```
HTML-отчёт(ы) pg_profile
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  Python (детерминированно)                                │
│  compare_settings │ check_report │ compare_runs           │
│  analyze_pgprofile (оркестратор)                          │
│       │                                                   │
│       ├── thresholds.yaml      — пороги health-check      │
│       └── knowledge/*.yaml     — рекомендации (не скиллы) │
└───────────────────────────────────────────────────────────┘
        │
        ├── findings.json, advisor.json   → CI / аудит
        ├── influence_table*.json/.wiki   → влияние GUC на метрики (пара / серия)
        ├── oracle_report, quality_report → проверка таблицы и quality gate
        ├── confluence_stub.wiki          → Confluence (таблицы)
        └── confluence_prompt.txt         → gigacli (текст, опционально)
                    │
                    ▼
            confluence_page.wiki → страница в Confluence
            (опц.) llm_*         → Headless Qwen из UI или run_llm.py
```

**Важно:** `knowledge/` — это YAML-база знаний, которую читает Python (`pgprofile_advisor.py`). Это **не** скиллы для GigaCode/Cursor и **не** нужно подключать к gigacli вручную. Рекомендации уже попадают в `brief.md` и `confluence_*`.

---

## Быстрый старт

```bash
cd pg_profile_checks
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Проверка одного отчёта
python check_report.py resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html

# Полный pipeline: JSON + Confluence + промпт для gigacli
python analyze_pgprofile.py \
  --report resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html \
  --output-dir ./analysis_out/ \
  --exit-code

# UI (см. раздел ниже; нужен Python 3.10+)
.venv/bin/python ui/server.py
# → http://127.0.0.1:8090/
```

### JVM-режим на рабочей машине (self-contained)

Для работы вкладки JVM теперь достаточно **этой директории** `pg_profile_checks`: внутри репозитория есть встроенный рантайм `jvmcheck_runtime/` (код анализатора, knowledge, thresholds, resources).

Внешний `/path/to/jvmcheck` рядом с проектом **не обязателен**.

Если нужно использовать другой экземпляр `jvmcheck`, можно переопределить путь:

```bash
export JVMCHECK_ROOT=/path/to/jvmcheck
.venv/bin/python ui/server.py
```

Приоритет поиска:
1. `JVMCHECK_ROOT` (если задан),
2. `pg_profile_checks/jvmcheck_runtime`,
3. legacy-пути (`~/jvmcheck` и соседние директории).

### Инструкция пользователя: как подготовить данные для JVM

Ниже — практический формат, который приложение ожидает для корректного парсинга в режиме `JVM`.

#### 1) Куда добавлять папки АС

По умолчанию складывайте данные сюда:

```text
pg_profile_checks/jvmcheck_runtime/resources/
```

Для каждой АС создавайте отдельную папку. Имя папки будет показано в UI в списке `АС`.

Пример:

```text
pg_profile_checks/
  jvmcheck_runtime/
    resources/
      CounterAgent/
      CreditHistory/
```

#### 2) Какие файлы обязательны внутри папки АС

Минимально нужен ресурсный файл контейнера:

- `resources.yaml` (или `resources.yml`)

Рекомендуется также добавить JVM-конфиг:

- `jvm-config.txt` (можно `jvm-config.yaml` / `jvm-config.yml`)

Рекомендуемый шаблон:

```text
jvmcheck_runtime/resources/<AS_NAME>/
  resources.yaml
  jvm-config.txt
```

#### 3) Что должно быть в `resources.yaml`

Парсер ожидает Kubernetes-подобное описание контейнеров, где у контейнеров есть:

- `name`
- `resources.requests.memory`
- `resources.limits.memory`
- для явного выбора pod в CLI/UI желательно `metadata.name` у каждого workload/pod-манифеста

Именно из этого файла берётся список целей для выбора в JVM-режиме: `pod + container`.

#### 4) Что должно быть в `jvm-config.*`

Ожидается мапа по имени контейнера, например:

```yaml
application:
  javaToolOptions: >
    -XX:+UseContainerSupport
    -XX:+UseG1GC
    -XX:MaxRAMPercentage=70.0
```

Имя контейнера в `jvm-config` должно совпадать с именем контейнера из `resources.yaml`.

Если одинаковое имя контейнера встречается в нескольких pod (например, `istio-proxy`), для запуска CLI указывайте оба параметра: `--pod-name` и `--container-name`.

#### 5) Drag-and-drop в UI

В списке АС есть пункт **«добавить новую систему»**: имя папки + `resources.yaml` / `jvm-config.txt` → `POST /api/jvm/systems`.

При загрузке файл классифицируется по имени:

- ресурсный: имя содержит `resource` или `values` и расширение `.yaml/.yml`;
- JVM-конфиг: имя содержит `jvm` или `java` и расширение `.txt/.yaml/.yml`.

Чтобы избежать ошибок распознавания, используйте имена:

- `resources.yaml`
- `jvm-config.txt`

Загруженные файлы перезаписывают конфиг выбранной АС.

#### 6) Если хотите использовать внешний jvmcheck

Можно хранить данные вне репозитория и переключить источник:

```bash
export JVMCHECK_ROOT=/path/to/jvmcheck
```

Тогда структура должна быть аналогичной:

```text
/path/to/jvmcheck/
  src/jvmcheck/
  resources/<AS_NAME>/resources.yaml
  resources/<AS_NAME>/jvm-config.txt
  knowledge/
  thresholds_jvm.yaml
```

#### 7) Demo-данные

Demo-системы (`DEMO_*`) всегда видны в UI и находятся в:

```text
pg_profile_checks/resources/jvm_demo/
```

Их можно использовать как образец для своих АС.

---

## UI (локальный визард)

Веб-интерфейс поверх того же оркестратора `analyze_pgprofile.py`. Новых pip-зависимостей нет (только stdlib + уже установленный PyYAML). Визуальный стиль и маскот — как у Gatling Monitor.

### Запуск

Нужен **Python 3.10+** — тот же интерпретатор, которым уже запускаете CLI (`analyze_pgprofile.py`).  
Команда `python` на рабочей машине часто указывает на 2.x / 3.6 → ошибка вида `future feature annotations is not defined`.

```bash
cd pg_profile_checks

# предпочтительно: venv проекта (уже с PyYAML)
.venv/bin/python ui/server.py

# или явный python3 3.10+
python3 ui/server.py

# проверить версию
.venv/bin/python -V    # ожидается Python 3.10+
```

Откройте http://127.0.0.1:8090/

Опции:

```bash
.venv/bin/python ui/server.py --host 127.0.0.1 --port 8090
.venv/bin/python ui/server.py --llm-config llm_providers.yaml --llm-provider qwen_local
.venv/bin/python ui/server.py --skip-llm-probe
```

Адрес, модель, таймаут и `default_provider` — в `llm_providers.yaml`. Токен шлюза только из `PGPROFILE_LLM_TOKEN` при старте процесса (в yaml нельзя). По умолчанию `default_provider: dry_run`: при старте живой Qwen не зовётся, блок **Headless Qwen** и вкладка **Качество** скрыты. Чтобы показать их — `default_provider: qwen_local` (или `qwen_gateway` + токен) и успешный probe. `--skip-llm-probe` или `ui.probe_on_startup: false` скрывают те же блоки.

Если порт занят (`Address already in use`):

```bash
.venv/bin/python ui/server.py --port 8091
```

По умолчанию слушает только `127.0.0.1` (доступ с других машин нет). Для LAN: `--host 0.0.0.0` (авторизации нет).

### Что умеет

0. Шаг **00 · СЦЕНАРИЙ** — первый на странице, вне «Расширенных настроек». По умолчанию «Авто по числу файлов и меткам»: один отчёт → health-check, два и больше → полный анализ, отмеченные симптомы → расследование (≥2 НТ → серия прогонов). Под списком видно, что именно выберет «Авто». Сценарии «НТ vs ПРОМ», «Стабильные / общие проблемы» и «Несколько прогонов НТ» сами открывают в таблице колонки **Среда** и **Label** — метки им нужны.
1. Drag-and-drop нескольких HTML-отчётов pg_profile. Все загруженные отчёты идут в анализ; до одного файла список урезает только явно выбранный сценарий «Health-check одного отчёта» (тогда лишние строки помечены «пропуск»).
2. Для каждого файла: период отчёта (читается из самого отчёта), метка **НТ** / **ПРОМ**, label. Порядок вручную не задаётся: отчёты выстраиваются по периоду от раннего к позднему, поэтому «прогон 1» — это всегда самый ранний прогон, а дельты между соседними прогонами считаются в правильную сторону. Отчёт без распознанного периода уходит в конец списка и подписывается «период не найден».
3. Сценарии, доступные в шаге 00:
   - расследование проблемы(ам);
   - несколько прогонов НТ (+ опционально ПРОМ-baseline) — серийная таблица влияния GUC;
   - health-check одного отчёта;
   - стабильные проблемы ПРОМ;
   - НТ vs ПРОМ (gate);
   - сравнение двух отчётов (diff) — метрики + Defined settings + таблица влияния.
4. Проблемы из playbook (`high_cpu`, `high_memory`, `high_wal`, `slow_query`) — опционально. **Если ничего не отмечено** — полный health-check всего отчёта; при нескольких файлах — findings, общие для всех, и специфичные для отдельных отчётов. Если отмечены симптомы — точечное расследование (при нескольких — объединённый Confluence-текст).
5. Результат:
   - Wiki Markup для Confluence (копировать / скачать `.wiki`);
   - промпт для ИИ (gigacli пока не вызывается из UI);
   - brief;
   - вкладка **Качество** и блок **Headless Qwen** — только если при старте UI живой Qwen ответил (иначе скрыты);
   - ZIP со всем `analysis_out` сессии + `README_AI.txt`.

### Два пути к модели

1. **Кнопка «Запросить Qwen»** (или `run_llm.py`): Python собирает бандл, ответ проходит quality gate. `dry_run` всегда блокирует публикацию. Модель не анализирует HTML и не ходит в БД.
2. **ZIP** с `README_AI.txt`: файлы для gigacli / ChatGPT / другого чата. Внешняя модель не получает плагины UI, парсер отчётов и **не** проходит quality gate. В Confluence ответ несут сами.

Не обещаем, что внешняя модель «имеет все функции» кнопки Qwen. Policy по умолчанию `none`: SQL из findings может уйти в промпт; `bank_redact` — решение контура.

### Режим JVM checks

Переключатель в header `PG/JVM`:
   - пошаговое дерево отсечения: АС / pod / контейнер → подов на одном плече (плеч всегда два) → причина рестарта → растёт ли память (какой график, % за часы, heap used до/после GC) → CPU throttle и CPU % of limits с двух плеч → GC p95 → при необходимости времена отклика пользователя;
   - «Не знаю» допустимо: анализ запускается, копируемую `JAVA_TOOL_OPTIONS` не даём, пока критические узлы не закрыты;
   - нет пресетов и чекбоксов проблем: нельзя пропустить дерево;
   - heap/OldGen открываются при OOMKilled, Java OOME **или** если указали, что память растёт; OldGen used можно ввести в % или целым MiB;
   - блок проверки перед запуском (system/pod/container + ответы дерева);
   - drag-and-drop `resources/jvm-config` в секции «Дополнительно» для перезаписи конфигов выбранной АС;
   - встроенные demo АС и контейнеры видны всегда.
   - при неоднозначном имени контейнера (одинаковый контейнер в нескольких pod) CLI требует явный `--pod-name`.

Список симптомов берётся из `knowledge/symptom_playbook.yaml`.

Страница **Thresholds** (ссылка в header): таблицы порогов из `thresholds.yaml` — `/thresholds`.
У части параметров — справка «когда менять / для каких БД» из `knowledge/threshold_guidance.yaml`
(ориентир — документация Postgres Pro). Поиск и фильтр «только ситуативные» — на странице.

После анализа UI показывает summary pills (в том числе **oracle**), карточки findings, превью Wiki Markup и чеклист проверки.
Пилюля oracle кликабельна — открывает вкладку **Качество**. Confluence-страницы строятся по каркасу: вердикт → TOC → действия → сводка → детали в `{expand}`.

Для JVM-анализа в wiki добавляются:
- вердикт, можно ли копировать `JAVA_TOOL_OPTIONS`, «На основании» / «Перепроверьте» / ответы дерева;
- кандидаты флагов в `{expand}`, если копировать нельзя;
- цель (АС/pod/контейнер), ресурсы контейнера и текущие JVM-опции;
- copy/paste `jvm-config` только если ворота дерева закрыты и есть подтверждающая метрика.

SLA по памяти:
- `memory usage % limit` — фиксированный порог **80%** (`memory_sla_percent` в `jvm_diagnostic_tree.yaml`, согласован с `memory_limit_pressure_ratio: 0.80`).
- Если при текущей скорости рост до 80% займёт **больше 30 дней** (`memory_sla_not_critical_days`), проблема **не критична**: limit из-за этого роста не поднимаем.
- Скорость роста считайте **после стабильной динамики**, не со старта пода.
- Если GC очищает heap, но минимум после каждой следующей сборки выше — это не плато, проблема всё ещё есть.
- Иначе время до OOMKilled (100%) ≤ 24 ч по-прежнему срочное.
- Длинные паузы (от 16 с) без пользовательских симптомов: в wiki — доп. анализ (GC log / safepoint / cause), без копирования G1, пока нет боли пользователя, liveness или CPU-SLA на очистке.
- Смена настроек перезапускает под и обнуляет память: ретест роста после выкатки несопоставим с текущим прогоном.

Проверка согласованности knowledge (recommendations ↔ prod_tuning, guc_guidance ↔ guc_impact):

```bash
python scripts/check_knowledge_consistency.py
```

### Сессии и хранение файлов

Каждый запуск «Анализировать» создаёт каталог:

```text
{tempdir}/pgprofile_ui_sessions/<uuid>/
  uploads/     # загруженные HTML
  out/         # findings, wiki, prompt, brief, …
  meta.json
```

Путь к `tempdir` печатается при старте сервера (`sessions: …`). Обычно это `/tmp/...` или `/var/folders/...` на macOS.

**Автоочистка (по умолчанию):**

| Параметр | Default | Смысл |
|----------|---------|--------|
| `--session-ttl-hours` | `24` | удалять сессии старше N часов (`0` — выключить) |
| `--cleanup-interval-hours` | `1` | периодический сканер во время работы (`0` — только при старте и после analyze) |

Очистка срабатывает: при старте сервера, после каждого analyze, и по таймеру. Возраст считается по mtime каталога сессии / `meta.json` / `out`.

```bash
# хранить сессии 6 часов, чистить каждый час
python3.11 ui/server.py --session-ttl-hours 6

# без автоочистки
python3.11 ui/server.py --session-ttl-hours 0
```

Остановка сервера сама по себе файлы не удаляет — их заберёт следующий старт (если TTL > 0) или ручная очистка. Нужный результат лучше сразу скачать ZIP. В корне архива — `README_AI.txt`. ZIP — файлы, не плагины; quality gate только у кнопки Qwen (см. «Два пути к модели» выше).

Ручная очистка всего каталога:

```bash
rm -rf "$(python3 -c 'import tempfile; print(tempfile.gettempdir())')/pgprofile_ui_sessions"
```

### Одновременная работа нескольких человек

Сервер — `ThreadingHTTPServer`: запросы обрабатываются в разных потоках. У каждого анализа свой UUID и своя папка; чужие сессии не перезаписываются.

На практике:

- несколько анализов делят CPU и RAM одной машины;
- без `--host 0.0.0.0` доступен только localhost;
- авторизации нет: зная `session_id`, можно скачать чужой ZIP (UUID угадать сложно).

Для постоянного общего доступа удобнее позже встроить UI в Gatling Monitor (см. ниже).

### Встраивание в Gatling Monitor

Standalone UI можно перенести в уже развёрнутый Gatling Monitor (Java вызывает тот же CLI, без новых Python-библиотек на рабочей машине):

→ [docs/INTEGRATION_GATLING_MONITOR.md](docs/INTEGRATION_GATLING_MONITOR.md)

---

## Скрипты

| Скрипт | Назначение |
|--------|------------|
| `compare_settings.py` | Сравнение **Defined settings** между двумя отчётами (НТ и ПРОМ) |
| `check_report.py` | Health-check **одного** отчёта (checkpoints, WAL, sessions, SQL и др.) |
| `compare_runs.py` | Сравнение **метрик** двух тестовых прогонов |
| `compare_nt_prod.py` | **НТ vs ПРОМ**: gate по настройкам + WAL, DML, SQL по параметрам |
| `analyze_prod_stability.py` | **Несколько PROD-отчётов**: стабильные проблемы + GUC-рекомендации |
| `investigate_symptom.py` | **Расследование симптома**: CPU / память / WAL / медленный SQL |
| `analyze_nt_runs.py` | **Несколько НТ-прогонов**: симптомы + влияние GUC (+ опционально ПРОМ) |
| `analyze_pgprofile.py` | **Оркестратор**: все анализы + JSON + brief + Confluence + oracle |
| `run_llm.py` | **Headless Qwen**: бандл из артефактов → local/gateway/`dry_run` |
| `run_quality.py` | Сводный quality report (oracle + confidence trail + LLM gate) |
| `scripts/check_smoke.py` | **Smoke перед релизом** (A/B/C без живого Qwen) |
| `scripts/check_report_order.py` | Порядок отчётов по периоду: парсер, CLI, UI-адаптер |
| `scripts/check_action_plan.py` | План действий в wiki «Стабильные / общие проблемы» |
| `scripts/check_pg_ui_flow.py` | Шаг 00 «Сценарий» и загрузка нескольких отчётов в UI |
| `scripts/check_dml_etalon.py` | Эталон DML с ПРОМ: max insert/update/delete |
| `merge_confluence.py` | Сборка `confluence_stub.wiki` + ответ ИИ → `confluence_page.wiki` |
| `ui/server.py` | **UI**: локальный HTTP-визард (stdlib, без новых pip-пакетов) |

Все CLI-скрипты анализа поддерживают `--format json` и `-o` / `--output` (кроме `analyze_pgprofile`, который пишет в `--output-dir`).

---

## Требования и установка

- Python 3.10+
- `compare_settings.py` — только стандартная библиотека
- Остальные скрипты — PyYAML (`requirements.txt`)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 1. Сравнение настроек НТ и ПРОМ

### Запуск

```bash
python compare_settings.py NT.html PROD.html
```

```bash
python compare_settings.py \
  --nt resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html \
  --prod /path/to/prod_report.html
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--verbose` | Не обрезать длинные значения (например, `archive_command`) |
| `--format text\|json` | Формат вывода (по умолчанию `text`) |
| `-o`, `--output` | Записать результат в файл |
| `--run-a-id`, `--run-b-id` | Метки сред в JSON (по умолчанию `NT` / `PROD`) |
| `--exit-code` | Exit `1`, если есть расхождения (для CI) |

### Как это работает

1. Из каждого HTML извлекается JSON `data`.
2. Берётся `datasets.settings`, остаются только **Defined settings** (`defined_val=true`) — обычно ~100–150 из ~550 параметров.
3. Сравнение по **объединению имён** — разное количество настроек допустимо.

### Вывод

- **DIFFER** — параметр в обоих файлах, значения разные.
- **Only in NT** / **Only in PROD** — явно задан только на одной среде.
- Совпадающие параметры скрыты, показывается счётчик в Summary.

### JSON

```bash
python compare_settings.py nt.html prod.html --format json -o settings.json
```

### Коды выхода

| Код | Значение |
|-----|----------|
| `0` | Расхождений нет |
| `1` | Есть расхождения (с `--exit-code`) |
| `2` | Ошибка парсинга / файл не найден |

---

## 2. Проверка одного отчёта

### Запуск

```bash
python check_report.py report.html
python check_report.py resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--config thresholds.yaml` | Файл порогов (по умолчанию `thresholds.yaml`) |
| `--only checkpoints,sessions,wal` | Только выбранные категории |
| `--verbose` | Полный текст SQL в предупреждениях |
| `--format text\|json` | Формат вывода |
| `-o`, `--output` | Записать в файл |
| `--exit-code` | Exit `1`, если есть предупреждения |

### Категории (`--only`)

| Категория | Что проверяется |
|-----------|-----------------|
| `checkpoints` | Requested checkpoints, время записи, bgwriter |
| `queries` | Медленные SQL (mean/max/total time) |
| `autovacuum` | GUC + таблицы с bloat / без vacuum |
| `wal` | wal_buffers_full, генерация WAL, max_wal_size |
| `cache` | Cache hit ratio, disk read time, temp files |
| `sessions` | Idle in transaction, rollbacks, fatal/killed |
| `memory` | work_mem × max_connections, таймауты |
| `io` | Seq scan, heap reads, WAL-heavy запросы |
| `locks` | Deadlocks |

Пороги настраиваются в `thresholds.yaml` без изменения кода. Для тестов — `thresholds_relaxed.yaml`.

### JSON

```bash
python check_report.py report.html --format json -o health.json
```

### Пример вывода

```
pg_profile health check
Server: tvldd-pprb06733.delta.sbrf.ru
Interval: 2026-03-13 12:30:02+03 .. 2026-03-14 03:30:02+03 (15.0 h)

== Checkpoints (5) ==
[CRIT] Requested checkpoints: 45/64 (70.3%), threshold 30%
[WARN] Checkpoint write time: 13576.6s over 15.0h interval (threshold 300s)

Summary: 34 warning(s) (3 critical, 31 warning)
```

---

## 3. Сравнение двух тестовых прогонов

### Запуск

```bash
python compare_runs.py RUN_A.html RUN_B.html --run-a-id sprint42 --run-b-id sprint43
```

`--run-a-id` и `--run-b-id` **обязательны** — это подписи колонок в таблице.

### Флаги

| Флаг | Описание |
|------|----------|
| `--only cluster,wal,dml,tables,queries,sessions,cache` | Секции сравнения |
| `--min-change-pct` | Мин. изменение в % (default: 5) |
| `--top-n` | Макс. строк в tables/queries (default: 15) |
| `--verbose` | Полный текст SQL |
| `--format text\|json` | Формат вывода |
| `-o`, `--output` | Записать в файл |
| `--exit-code` | Exit `1` при значимых расхождениях |

### Особенности

- При **разной длительности** прогонов — предупреждение и значения **абсолют + /час**.
- Для процентов и средних времён — только абсолютное сравнение.

### JSON

```bash
python compare_runs.py a.html b.html --run-a-id v1 --run-b-id v2 --format json -o runs.json
```

---

## 3a. Таблица влияния GUC → метрики

`compare_runs.py` показывает, **что** изменилось в метриках. Таблица влияния отвечает на другой вопрос: **какие Defined settings сменились и с какими метриками это совпало**. Связь не выдумывается: если для параметра нет подсказки в `knowledge/guc_impact.yaml`, строка остаётся без `affected_metric`, `impact=neutral`, `confidence=low`.

Два режима.

### Пара прогонов (before → after)

Нужны оба флага: `--compare-run` (метрики) и `--compare-settings` (Defined settings). В UI это сценарий **«Сравнение двух отчётов (diff)»**.

```bash
python analyze_pgprofile.py \
  --report run_before.html \
  --compare-run run_after.html \
  --compare-settings run_after.html \
  --output-dir ./analysis_out/
```

Появляются `influence_table.json`, `influence_table.csv`, `influence_summary.md` и `influence_summary.wiki` (это человекочитаемое сравнение для Confluence).

### Серия НТ (2+ отчёта по порядку)

`--nt-reports` требует `--symptoms`. В UI: сценарий **«Несколько прогонов НТ»**, отчёты с меткой НТ, отмеченный симптом.

Порядок аргументов значения не имеет: серия выстраивается по периоду отчёта (`report_start1` из данных отчёта), метки `--nt-label` переезжают вместе со своими файлами. Если порядок пришлось поправить, в stderr печатается строка `note: --nt-reports: порядок отчётов выставлен по дате отчёта: ...`. Пара `--report` / `--compare-run` не переставляется — базу выбирает вызывающий, но при инверсии в stderr будет предупреждение.

```bash
python analyze_pgprofile.py \
  --nt-reports nt1.html nt2.html nt3.html \
  --nt-label nt1 --nt-label nt2 --nt-label nt3 \
  --symptoms high_cpu \
  --output-dir ./analysis_out/
```

Появляются `influence_table_series.json` / `.csv` и `influence_summary_series.md` / `.wiki` плюс `nt_runs_confluence.wiki`.

Тот же анализ без оркестратора: `python analyze_nt_runs.py nt1.html nt2.html --symptoms high_cpu`.

### Как читать confidence и evidence

| Поле | Значения | Смысл |
|------|----------|--------|
| `evidence_type` | `probable` | Совпадение во времени. Гипотеза, не причинность. |
| `evidence_type` | `proven` | Изолированный эффект. В паре: сменился **один** параметр и `workload_match ≥ 0.85`. В серии: ≥3 пары, `stability_score ≥ 0.7`, шум IQR/\|медиана Δ%\| < 1. |
| `confidence` | `low` / `medium` / `high` | Насколько можно опираться на строку. Много одновременных GUC, плохая сопоставимость нагрузки, нет связи с метрикой — понижение. |
| `impact` | `improved` / `degraded` / `neutral` | Полярность **метрики** (для `*time`, `blks_read`, `checkpoints_req` рост — ухудшение). |
| `workload_match_score` | 0…1 | Сопоставимость окон/серверов. Низкий балл — сравнивать осторожно. |

Правила, которые повторяются в `influence_summary*.md`:

- **PROVEN** — эффект устойчив; **PROBABLE** — только корреляция.
- Для `probable` нельзя утверждать причинность: нужен отдельный изолированный прогон.
- Числа только из `influence_table*.json`; модель и оператор не добавляют своих Δ.

В серийной строке `direction=increased|decreased` — как изменился **GUC**, не метрика. Не путать с `up`/`down` у метрик.

### Oracle и качество

После анализа пишутся `oracle_report.json` / `.md` и `quality_report.json` / `.md`.

- `pass` / `warning` / `fail` — согласованность таблицы (знак Δ, полнота полей, ложный `proven`).
- Вкладка UI **Качество** и `python run_quality.py --output-dir ./analysis_out/` показывают слои и trail «почему confidence стал low».
- `--exit-code` оркестратора **не** зависит от oracle. Для CI по качеству: `python analyze_pgprofile.py … --exit-code-quality` или `python run_quality.py --output-dir ./analysis_out/ --exit-code`.

Разбор красного oracle и blocked LLM — [docs/LLM_QUALITY_RUNBOOK.md](docs/LLM_QUALITY_RUNBOOK.md).

---

## 4. Валидация НТ vs ПРОМ (`compare_nt_prod.py`)

Сравнивает **метрики производительности** между отчётами НТ и ПРОМ, чтобы убедиться, что нагрузочный стенд отражает прод. Это **не** замена `compare_settings.py` — настройки проверяются автоматически как **предусловие**.

### Зачем

- Убедиться, что на НТ можно экспериментировать (настройки и профиль нагрузки близки к ПРОМ).
- Увидеть расхождения в **скорости WAL**, **объёме DML**, **параметрах SQL** (calls, mean/max time, wal_bytes).
- Если Defined settings расходятся — прогон помечается **невалидным** (красный баннер вверху).

### Запуск

```bash
python compare_nt_prod.py nt_report.html prod_report.html
```

```bash
python compare_nt_prod.py \
  --nt nt_report.html \
  --prod prod_report.html \
  --only wal,dml,queries \
  --min-change-pct 10 \
  --exit-code
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--only wal,dml,queries,...` | Секции (default: wal, dml, cluster, queries, sessions, cache, tables) |
| `--min-change-pct` | Мин. изменение в % (default: 5) |
| `--top-n` | Макс. SQL-запросов в детальном сравнении (default: 15) |
| `--verbose` | Полный текст SQL |
| `--no-color` | Без ANSI-цветов (для файла / CI) |
| `--format text\|json` | Формат вывода |
| `-o`, `--output` | Записать в файл |
| `--exit-code` | Exit `1` при расхождении настроек **или** метрик |
| `--exit-code-settings-only` | Exit `1` только при **критичных** расхождениях GUC |

**Классификация:** runtime-метаданные (`pg_conf_load_time`, `pg_postmaster_start_time`, `in_hot_standby`) не блокируют сравнение. Отличия по объёму WAL и DML — секция «Справочно», не «ПРОГОН НЕВАЛИДЕН». Предупреждения по производительности (mean/max time, cache) — отдельно.

### Структура отчёта

1. **Красный баннер** — если Defined settings НТ ≠ ПРОМ (`ПРОГОН НЕВАЛИДЕН`).
2. **Зелёный баннер** — если настройки совпадают.
3. Метаданные интервалов НТ и ПРОМ (всегда **+/час** для счётчиков).
4. **Краткая сводка** — валидность, число расхождений, вывод «можно экспериментировать».
5. **Расхождения настроек** (компактная таблица).
6. **WAL / скорость генерации** — `wal_bytes` с доп. строкой `MB/h`.
7. **DML операции** — INSERT/UPDATE/DELETE/COMMIT по БД.
8. **SQL по параметрам** — для каждого запроса: calls, total/mean/max time, wal_bytes, I/O.

### JSON

```bash
python compare_nt_prod.py nt.html prod.html --format json -o nt_prod.json
```

Поле `settings_valid: false` — метрикам нельзя доверять до выравнивания конфигурации.

### Confluence + gigacli (краткая сводка для ИИ)

```bash
python compare_nt_prod.py nt.html prod.html \
  --confluence-dir ./analysis_out/ \
  --confluence-title "Валидация НТ vs ПРОМ: sprint-42"
```

| Файл | Назначение |
|------|------------|
| `nt_prod_confluence_stub.wiki` | Красный `{warning}` при невалидных настройках + таблицы WAL/DML/SQL |
| `nt_prod_confluence_prompt.txt` | Промпт для gigacli — краткая интерпретация |
| `nt_prod_brief.md` | Данные для ИИ |

```bash
# gigacli → body
gigacli < analysis_out/nt_prod_confluence_prompt.txt > analysis_out/nt_prod_body.wiki

# итоговая страница
python merge_confluence.py analysis_out/nt_prod_confluence_stub.wiki \
  -b analysis_out/nt_prod_body.wiki \
  -o analysis_out/nt_prod_confluence_page.wiki
```

Через оркестратор:

```bash
python analyze_pgprofile.py \
  --report nt.html \
  --compare-prod prod.html \
  --output-dir ./analysis_out/
```

### Пример (невалидный прогон)

```
!!! ПРОГОН НЕВАЛИДЕН: Defined settings НТ и ПРОМ расходятся (3 отличий) !!!
  Сравнение метрик может вводить в заблуждение. Сначала выровняйте настройки.

== WAL / скорость генерации ==
Metric          | NT              | PROD            | Delta
wal_bytes       | 1.2G (80M/h)    | 900M (37.5M/h)  | -300M (-25.0%)
  wal throughput| 80.00 MB/h      | 37.50 MB/h      |
```

---

## 5. Стабильные проблемы на PROD (`analyze_prod_stability.py`)

Сравнивает **два и более** PROD-отчёта за разные периоды, находит health-check находки,
которые повторяются стабильно (по умолчанию — во **всех** отчётах), и выдаёт рекомендации
по изменению GUC с учётом `knowledge/prod_tuning.yaml` и общих рекомендаций Postgres Pro.

Каждая рекомендация маркируется **двумя осями**:

| Ось | Значения | Смысл |
|-----|----------|-------|
| **Критичность проблемы** | `critical`, `high`, `medium`, `warning` | Насколько серьёзен стабильный симптом |
| **Безопасность изменения** | `safe`, `cautious`, `risky`, `restart_required` | Насколько осторожно применять GUC |
| **Влияние изменения** | `low`, `medium`, `high` | Потенциальный эффект на нагрузку при ошибке |

### Запуск

```bash
# Два периода одной PROD-базы
python analyze_prod_stability.py \
  resources/counteragent_prom1.html \
  resources/counteragent_prom2.html \
  --label prom1 --label prom2

# Четыре отчёта: стабильность ≥50% периодов
python analyze_prod_stability.py \
  resources/counteragent_prom1.html resources/counteragent_prom2.html \
  resources/credithistory_prom1.html resources/credithistory_prom2.html \
  --min-stability 0.5
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--label NAME` | Метка отчёта (повторять для каждого файла, в том же порядке) |
| `--config PATH` | Пороги health-check (по умолчанию `thresholds.yaml`) |
| `--tuning PATH` | Правила GUC (по умолчанию `knowledge/prod_tuning.yaml`) |
| `--min-stability RATIO` | Доля отчётов, где finding должен встретиться (`1.0` = все) |
| `--show-ephemeral` | Показать нестабильные находки (не во всех отчётах) |
| `--format text\|json` | Формат вывода |
| `-o`, `--output` | Записать результат в файл |
| `--exit-code` | Exit `1`, если есть стабильные `critical` / `high` рекомендации |
| `--confluence-dir DIR` | `stable_prod_confluence_stub.wiki`, `stable_prod_confluence_prompt.txt`, `stable_prod_brief.md` |
| `--confluence-title` | Заголовок страницы Confluence |

### Confluence + gigacli

```bash
python analyze_prod_stability.py prom1.html prom2.html \
  --confluence-dir ./analysis_out/ \
  --confluence-title "PROD counteragent: стабильные проблемы"
```

Раздел «План действий: подтвердить → изменить → убедиться» даёт по каждой устойчивой проблеме (от critical к остальным) три шага: чем подтвердить проблему в текущих отчётах, что менять (GUC с текущими значениями по отчётам и риском изменения: SAFE / CAUTIOUS / RISKY / RESTART, плюс операционные действия), и по какому признаку убедиться — какой finding должен перестать воспроизводиться в новом отчёте. Список «Что сделать сейчас» выше даёт по одному действию на проблему, чтобы одна громкая находка не занимала весь блок.

| Файл | Назначение |
|------|------------|
| `stable_prod_confluence_stub.wiki` | Вердикт, чеклист, план действий, таблицы стабильных рекомендаций и GUC (Wiki Markup) |
| `stable_prod_confluence_prompt.txt` | Промпт для gigacli — резюме и план внедрения |
| `stable_prod_brief.md` | Данные для ИИ |

```bash
gigacli < analysis_out/stable_prod_confluence_prompt.txt > analysis_out/stable_prod_body.wiki

python merge_confluence.py analysis_out/stable_prod_confluence_stub.wiki \
  -b analysis_out/stable_prod_body.wiki \
  -o analysis_out/stable_prod_confluence_page.wiki
```

### JSON

```bash
python analyze_prod_stability.py prom1.html prom2.html \
  --format json -o stable_prod.json
```

Поле `recommendations[].combined_change_safety` и `combined_change_impact` — агрегат
по всем GUC в правиле (берётся наихудший случай).

---

## 5a. Расследование симптома (`investigate_symptom.py`)

Принимает **тип симптома** и один или несколько pg_profile HTML, возвращает список
**возможных причин** с уровнем уверенности и **план действий** для подтверждения/опровержения.

### Симптомы

| ID | Описание |
|----|----------|
| `high_cpu` | Высокая утилизация CPU БД |
| `high_memory` | Высокое потребление памяти БД |
| `high_wal` | Высокая генерация WAL |
| `slow_query` | Медленный запрос (нужен `--query-hex`, `--query-id` или `--query-text`) |

Статусы причин: **confirmed** (данные в отчёте), **suspected** (косвенные признаки),
**possible** (типичная причина без данных).

### Примеры

```bash
# Высокий CPU — два периода PROD
python investigate_symptom.py high_cpu \
  resources/counteragent_prom1.html resources/counteragent_prom2.html \
  --label prom1 --label prom2

# Высокий WAL
python investigate_symptom.py high_wal resources/counteragent_prom1.html

# Медленный запрос по hex ID или фрагменту SQL
python investigate_symptom.py slow_query resources/counteragent_prom1.html \
  --query-hex 11a74fb9c1776a85

python investigate_symptom.py slow_query resources/counteragent_prom1.html \
  --query-text "t_transaction set sys_lastchangedate"

# JSON
python investigate_symptom.py high_memory report.html --format json -o symptom.json

# Список симптомов
python investigate_symptom.py --list-symptoms
```

### Confluence + gigacli

```bash
python investigate_symptom.py high_cpu prom1.html prom2.html \
  --confluence-dir ./analysis_out/ \
  --confluence-title "PROD: высокий CPU"
```

| Файл | Назначение |
|------|------------|
| `symptom_confluence_stub.wiki` | Таблицы гипотез, evidence и план verify |
| `symptom_confluence_prompt.txt` | Промпт для gigacli — интерпретация и порядок проверок |
| `symptom_brief.md` | Данные для ИИ |

```bash
gigacli < analysis_out/symptom_confluence_prompt.txt > analysis_out/symptom_body.wiki

python merge_confluence.py analysis_out/symptom_confluence_stub.wiki \
  -b analysis_out/symptom_body.wiki \
  -o analysis_out/symptom_confluence_page.wiki
```

Playbook причин и шагов верификации: `knowledge/symptom_playbook.yaml`.

---

## 6. Оркестратор `analyze_pgprofile.py`

Запускает один или несколько анализов, обогащает находки рекомендациями из `knowledge/` и пишет артефакты в каталог.

### Запуск

```bash
# Только health-check
python analyze_pgprofile.py \
  --report load_test.html \
  --output-dir ./analysis_out/

# Health-check + сравнение прогонов + настройки НТ vs ПРОМ
python analyze_pgprofile.py \
  --report sprint42.html \
  --compare-run sprint43.html \
  --run-a-id sprint42 \
  --run-b-id sprint43 \
  --compare-settings prod.html \
  --settings-a-id NT \
  --settings-b-id PROD \
  --output-dir ./analysis_out/ \
  --exit-code

# Только стабильные проблемы PROD (2+ отчёта) + Confluence
python analyze_pgprofile.py \
  --stable-prod-reports resources/counteragent_prom1.html resources/counteragent_prom2.html \
  --stable-prod-label prom1 --stable-prod-label prom2 \
  --output-dir ./analysis_out/ \
  --confluence-title "PROD counteragent: tuning"

# Расследование симптома + Confluence
python analyze_pgprofile.py \
  --symptom high_cpu \
  --symptom-reports resources/counteragent_prom1.html resources/counteragent_prom2.html \
  --symptom-label prom1 --symptom-label prom2 \
  --output-dir ./analysis_out/ \
  --confluence-title "PROD: высокий CPU"

# Пара прогонов: метрики + settings → таблица влияния
python analyze_pgprofile.py \
  --report run_before.html \
  --compare-run run_after.html \
  --compare-settings run_after.html \
  --output-dir ./analysis_out/

# Серия НТ + симптомы → серийная таблица влияния
python analyze_pgprofile.py \
  --nt-reports nt1.html nt2.html nt3.html \
  --nt-label nt1 --nt-label nt2 --nt-label nt3 \
  --symptoms high_cpu \
  --output-dir ./analysis_out/
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--report` | HTML для health-check (и первая среда для settings diff) |
| `--config` | Пороги (default: `thresholds.yaml`) |
| `--compare-run` | Второй HTML для сравнения прогонов |
| `--run-a-id`, `--run-b-id` | Метки прогонов |
| `--compare-settings` | Второй HTML для diff настроек (требует `--report`) |
| `--nt-reports` | 2+ HTML НТ по порядку; требует `--symptoms` |
| `--nt-label` | Метка для каждого `--nt-reports` (повторять) |
| `--prod-reports` | PROD baseline для `--nt-reports` |
| `--symptoms` | Симптомы для серии НТ: `high_cpu,high_wal,...` |
| `--compare-prod` | PROD HTML для НТ vs ПРОМ (требует `--report` = НТ) |
| `--stable-prod-reports` | 2+ PROD HTML для анализа стабильных проблем |
| `--stable-prod-label` | Метка для каждого `--stable-prod-reports` |
| `--symptom` | Симптом: `high_cpu`, `high_memory`, `high_wal`, `slow_query` |
| `--symptom-reports` | 1+ HTML для расследования симптома |
| `--symptom-label` | Метка для каждого `--symptom-reports` |
| `--query-hex`, `--query-id`, `--query-text` | Целевой SQL (для `slow_query`) |
| `--playbook` | Playbook симптомов (`knowledge/symptom_playbook.yaml`) |
| `--min-stability` | Доля отчётов для «стабильной» находки (default: `1.0`) |
| `--tuning` | Правила GUC (`knowledge/prod_tuning.yaml`) |
| `--settings-a-id`, `--settings-b-id` | Метки сред (default: `NT` / `PROD`) |
| `--output-dir` | **Обязательный** каталог результатов |
| `--confluence-title` | Заголовок страницы Confluence (auto по умолчанию) |
| `--min-change-pct`, `--top-n` | Параметры compare_runs |
| `--exit-code` | Exit `1`, если есть находки / расхождения |
| `--exit-code-quality` | Exit `1`, если oracle/quality = `fail` (`--exit-code` не меняет) |

### Файлы в `--output-dir`

| Файл | Кто создаёт | Назначение |
|------|-------------|------------|
| `health_check.json` | Python | Health-check (если `--report`) |
| `run_comparison.json` | Python | Сравнение прогонов (если `--compare-run`) |
| `settings_diff.json` | Python | Diff настроек (если `--compare-settings`) |
| `influence_table.json` / `.csv` | Python | Влияние GUC на метрики (пара, если `--compare-run` + `--compare-settings`) |
| `influence_summary.md` / `.wiki` | Python | Краткое резюме влияния для Confluence |
| `influence_table_series.json` / `.csv` | Python | Влияние по серии (если `--nt-reports`) |
| `influence_summary_series.md` / `.wiki` | Python | Резюме серийного влияния |
| `nt_runs.json` | Python | Несколько НТ + симптомы + GUC |
| `nt_runs_confluence.wiki` | Python | Wiki: симптомы + влияние GUC |
| `oracle_report.json` / `.md` | Python | Проверка таблицы влияния |
| `quality_report.json` / `.md` | Python | Слои oracle + trail confidence + LLM gate |
| `findings.json` | Python | Все находки (combined) |
| `advisor.json` | Python | Находки + рекомендации из `knowledge/` |
| `brief.md` | Python | Краткий brief |
| `summary_prompt.txt` | Python | Промпт для LLM (`prompts/analyst.md` + brief) |
| `confluence_stub.wiki` | Python | Шапка: всё ли хорошо / что изменилось / что сделать; таблицы находок в Expand |
| `confluence_prompt.txt` | Python | Промпт для gigacli (Wiki Markup) |
| `stable_prod.json` | Python | Стабильные PROD-проблемы (если `--stable-prod-reports`) |
| `stable_prod_confluence_stub.wiki` | Python | Таблицы стабильных GUC-рекомендаций |
| `stable_prod_confluence_prompt.txt` | Python | Промпт gigacli для stable PROD |
| `stable_prod_brief.md` | Python | Brief для stable PROD |
| `symptom_investigation.json` | Python | Расследование симптома (если `--symptom`) |
| `symptom_confluence_stub.wiki` | Python | Таблицы гипотез и план verify |
| `symptom_confluence_prompt.txt` | Python | Промпт gigacli для симптома |
| `symptom_brief.md` | Python | Brief для симптома |
| `jvm_analysis.json` | Python | Findings/recommendations JVM-анализа + `tuning_target_snapshot` (pod/container/resources/JVM options) |
| `jvm_confluence.wiki` | Python | Wiki Markup для JVM-анализа (в шапке: ресурсы и JVM-настройки целевого контейнера) |
| `jvm_prompt.txt` | Python | Промпт ИИ для JVM-анализа |
| `jvm_brief.md` | Python | Brief JVM-анализа |
| `confluence_body.wiki` | **ИИ** | Текстовые разделы (резюме, рекомендации) |
| `confluence_page.wiki` | `merge_confluence.py` | Готовая страница |

---

## 7. Публикация в Confluence (GigaIDE + gigacli)

Схема: **Python — точные таблицы**, **ИИ — только narrative** в Confluence Wiki Markup.

### Шаг 1. Генерация

```bash
python analyze_pgprofile.py \
  --report load_test.html \
  --output-dir ./analysis_out/ \
  --confluence-title "НТ sprint-42: pg_profile"
```

`confluence_stub.wiki` можно вставить в Confluence **сразу**: сверху вердикт «всё ли хорошо», при сравнении — «что изменилось», затем план; длинные таблицы — в Expand.

### Шаг 2. gigacli (DeepSeek V4 Flash, 262k)

В GigaIDE откройте терминал gigacli и передайте промпт:

```text
Прочитай analysis_out/confluence_prompt.txt и выполни инструкции.
Ответ сохрани в analysis_out/confluence_body.wiki
```

Или через перенаправление (если поддерживается вашей версией gigacli):

```bash
gigacli < analysis_out/confluence_prompt.txt > analysis_out/confluence_body.wiki
```

Для JVM-режима UI используйте `jvm_prompt.txt` (если он есть в артефактах сессии):

```bash
gigacli < analysis_out/jvm_prompt.txt > analysis_out/jvm_body.wiki
```

**Не передавайте в gigacli:**
- HTML-отчёты pg_profile
- `knowledge/*.yaml` (уже учтено в brief)
- скиллы / плагины — **не требуются**

### Шаг 3. Сборка страницы

```bash
python merge_confluence.py analysis_out/confluence_stub.wiki \
  --body analysis_out/confluence_body.wiki \
  -o analysis_out/confluence_page.wiki
```

### Шаг 4. Вставка в Confluence

| Версия | Действие |
|--------|----------|
| Server / Data Center | **Вставить** → **Разметка Wiki** → содержимое `confluence_page.wiki` |
| Cloud | Вставить текст; при необходимости — Wiki Markup macro или плагин инстанса |

Макросы в выводе ИИ: `{warning}`, `{note}`, `{info}`, `{status}`, `{expand}` — см. [`prompts/analyst_confluence.md`](prompts/analyst_confluence.md).

### НТ vs ПРОМ: отдельная страница Confluence

```bash
python compare_nt_prod.py nt.html prod.html --confluence-dir ./analysis_out/
# → nt_prod_confluence_stub.wiki + nt_prod_confluence_prompt.txt

python merge_confluence.py analysis_out/nt_prod_confluence_stub.wiki \
  -b analysis_out/nt_prod_body.wiki \
  -o analysis_out/nt_prod_confluence_page.wiki
```

Промпт для ИИ: [`prompts/analyst_confluence_nt_prod.md`](prompts/analyst_confluence_nt_prod.md) — краткая сводка с вердиктом «можно экспериментировать на НТ».

### Альтернатива: текстовый отчёт без Confluence

Передайте gigacli файл `summary_prompt.txt` — ответ будет в обычном тексте/Markdown (`prompts/analyst.md`).

---

## База знаний `knowledge/`

| Файл | Содержимое | Кто читает |
|------|------------|------------|
| [`knowledge/recommendations.yaml`](knowledge/recommendations.yaml) | finding ID → рекомендация, actions, ссылки | **Python** (`pgprofile_advisor.py`) |
| [`knowledge/guc_guidance.yaml`](knowledge/guc_guidance.yaml) | Подсказки по GUC при diff настроек | **Python** |
| [`prompts/analyst.md`](prompts/analyst.md) | Промпт: текстовый отчёт | gigacli (через `summary_prompt.txt`) |
| [`prompts/analyst_confluence.md`](prompts/analyst_confluence.md) | Промпт: Confluence Wiki Markup | gigacli (через `confluence_prompt.txt`) |
| [`prompts/analyst_confluence_nt_prod.md`](prompts/analyst_confluence_nt_prod.md) | Промпт: НТ vs ПРОМ → Confluence | gigacli (через `nt_prod_confluence_prompt.txt`) |

Базу знаний обновляет DBA (раз в квартал) на машине с доступом к документации PostgreSQL, затем коммитит в git.

Добавление нового типа находки:
1. Порог / логика — `pgprofile_health.py` или `pgprofile_compare.py`
2. Стабильный ID — `pgprofile_findings.py` (`infer_rule_id`)
3. Рекомендация — новая запись в `knowledge/recommendations.yaml`

---

## Типичные сценарии

### Перед релизом: НТ vs ПРОМ (настройки + метрики)

```bash
python compare_nt_prod.py nt.html prod.html --exit-code
```

### Перед релизом: только настройки (НТ vs ПРОМ)

```bash
python compare_settings.py nt.html prod.html --exit-code
```

### После нагрузочного теста

```bash
python check_report.py load_test.html --exit-code
```

### Сравнить два прогона (метрики)

```bash
python compare_runs.py run_v1.html run_v2.html \
  --run-a-id sprint_42 --run-b-id sprint_43 \
  --only dml,queries,cluster
```

### Пара прогонов: таблица влияния GUC → метрики

```bash
python analyze_pgprofile.py \
  --report run_before.html \
  --compare-run run_after.html \
  --compare-settings run_after.html \
  --output-dir ./analysis_out/
python run_quality.py --output-dir ./analysis_out/
```

В UI: два HTML, сценарий «Сравнение двух отчётов (diff)».

### Серия НТ + симптом

```bash
python analyze_pgprofile.py \
  --nt-reports nt1.html nt2.html nt3.html \
  --nt-label nt1 --nt-label nt2 --nt-label nt3 \
  --symptoms high_cpu \
  --output-dir ./analysis_out/
```

### Headless Qwen (после анализа)

По умолчанию `dry_run` — без сети, проверка цепочки. Боевой провайдер: [docs/QWEN_HEADLESS.md](docs/QWEN_HEADLESS.md). Ошибки и quality gate: [docs/LLM_QUALITY_RUNBOOK.md](docs/LLM_QUALITY_RUNBOOK.md).

```bash
python run_llm.py --list-providers
python run_llm.py --output-dir ./analysis_out/ --task summary --print-bundle
python run_llm.py --output-dir ./analysis_out/ --task summary --provider dry_run
```

В UI после анализа: блок **Headless Qwen** → **Запросить Qwen**, если при старте `ui/server.py` провайдер из `llm_providers.yaml` ответил. `dry_run`, `--skip-llm-probe` или ошибка соединения скрывают блок и вкладку **Качество**.

### Полный отчёт для Confluence

```bash
python analyze_pgprofile.py --report nt.html --output-dir ./analysis_out/
# → gigacli + merge_confluence.py (см. раздел 7)
```

### CI (без живого Qwen)

Минимум CLI — [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) (этапы A/B/C):

```bash
python scripts/check_smoke.py
```

Перед релизом **UI** — полная матрица сценариев (три HTML из `resources/`). Копия `e2e/series` в `case_matrix/` этому не замена:

```bash
python scripts/check_smoke.py --full
```

Совместимость со старыми `run_comparison` / `settings_diff`:

```bash
python scripts/check_contract_backcompat.py
```

Точечный health-check без LLM:

```bash
python compare_settings.py nt.html prod.html --exit-code
python check_report.py report.html --exit-code
python analyze_pgprofile.py --report report.html --output-dir ./out/ --exit-code
```

Аудит и пайплайны опираются на Python и `findings.json`, не на gigacli. Oracle/quality в CI: `python analyze_pgprofile.py … --exit-code-quality` или `python run_quality.py --output-dir ./out/ --exit-code`.

---

## Структура проекта

```
pg_profile_checks/
├── compare_settings.py      # CLI: настройки НТ vs ПРОМ
├── compare_runs.py          # CLI: метрики двух прогонов
├── check_report.py          # CLI: health-check одного отчёта
├── compare_nt_prod.py       # CLI: валидация НТ vs ПРОМ
├── analyze_prod_stability.py # CLI: стабильные PROD-проблемы + GUC
├── investigate_symptom.py   # CLI: расследование симптома
├── analyze_pgprofile.py     # CLI: оркестратор
├── run_llm.py               # CLI: Headless Qwen
├── run_quality.py           # CLI: quality report
├── merge_confluence.py      # CLI: stub + body → confluence_page.wiki
├── pgprofile_parser.py      # Парсинг HTML → JSON
├── pgprofile_health.py      # Логика health-check
├── pgprofile_compare.py     # Логика сравнения прогонов
├── pgprofile_influence.py   # Таблица влияния GUC → метрики
├── pgprofile_oracle.py      # Проверка influence-таблиц
├── pgprofile_quality.py     # Сводный quality report
├── pgprofile_llm.py         # Провайдеры dry_run / qwen_local / qwen_gateway
├── pgprofile_nt_prod.py     # НТ vs ПРОМ: settings gate + отчёт
├── pgprofile_stable_prod.py # N PROD-отчётов: стабильность + tuning
├── pgprofile_symptoms.py    # Расследование симптомов (CPU/RAM/WAL/SQL)
├── pgprofile_findings.py    # warnings → finding IDs
├── pgprofile_advisor.py     # knowledge/ → рекомендации
├── pgprofile_confluence.py  # Confluence Wiki Markup
├── pgprofile_output.py      # JSON output helpers
├── knowledge/               # YAML playbook (offline)
│   ├── recommendations.yaml
│   ├── guc_guidance.yaml
│   ├── guc_impact.yaml      # ожидаемый эффект смены GUC (NT multi-run)
│   ├── prod_tuning.yaml     # finding → GUC tuning (PROD stability)
│   └── symptom_playbook.yaml # симптом → причины + verify steps
├── prompts/                 # Промпты для gigacli
├── ui/                      # Standalone UI (stdlib HTTP)
│   ├── server.py            # python ui/server.py → :8090
│   ├── analysis_runner.py   # UI → analyze_pgprofile.run_pipeline
│   └── web/                 # HTML/CSS/JS + mascot (стиль Gatling Monitor)
├── docs/
│   ├── ai/                  # индекс и playbook для агента (не читать корневой README целиком)
│   │   ├── README.md
│   │   └── system-config.md
│   ├── INTEGRATION_GATLING_MONITOR.md
│   ├── QWEN_HEADLESS.md     # настройка local/gateway Qwen
│   ├── LLM_QUALITY_RUNBOOK.md
│   └── RELEASE_CHECKLIST.md # A/B/C готов + smoke
├── llm_providers.yaml
├── analyze_nt_runs.py       # CLI: несколько НТ + симптомы + GUC impact
├── pgprofile_nt_runs.py
├── thresholds.yaml
├── thresholds_relaxed.yaml
├── requirements.txt
├── jvmcheck_runtime/         # встроенный JVM-рантайм (self-contained)
│   ├── src/jvmcheck/         # код JVM-анализатора
│   ├── resources/            # АС-папки и конфиги для JVM
│   ├── knowledge/            # база правил/рекомендаций JVM
│   └── thresholds_jvm.yaml   # JVM-пороги (в т.ч. SLA memory 80%)
├── resources/               # Примеры HTML
└── README.md
```

---

## Ограничения

- Только HTML-отчёты pg_profile в текущем формате (`const data={...}`).
- `compare_settings.py` — только **Defined settings**. Параметр, явно заданный на одной среде и дефолтный на другой, попадёт в «Only in NT» / «Only in PROD» — это ожидаемо.
- Пороги в `thresholds.yaml` — ориентир; подбирайте под свою нагрузку.
- Таблица влияния не приписывает метрику параметру без известной связи. `probable` ≠ причинность.
- ИИ (gigacli и Headless Qwen) — опционально. Цифры и таблицы — из Python. Qwen по умолчанию `dry_run` (сеть не трогает, публикацию блокирует quality gate).
- UI не вызывает gigacli сам: только отдаёт stub/wiki, prompt, ответ Qwen и ZIP; сессии в temp с TTL по умолчанию 24ч (см. раздел UI).
- `--exit-code` оркестратора не учитывает oracle/quality; для этого `--exit-code-quality` или `run_quality.py --exit-code`.
