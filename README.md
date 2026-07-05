# pg_profile_checks

Набор Python-скриптов для анализа HTML-отчётов [pg_profile](https://github.com/zubkov-andrei/pg_profile).

Скрипты читают данные прямо из HTML-файла: отчёт pg_profile — это одностраничное приложение, внутри которого в JavaScript-объекте `const data={...}` лежат все метрики, настройки и тексты запросов. Внешняя база данных не нужна.

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
        ├── confluence_stub.wiki          → Confluence (таблицы)
        └── confluence_prompt.txt         → gigacli (текст, опционально)
                    │
                    ▼
            confluence_page.wiki → страница в Confluence
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
```

---

## Скрипты

| Скрипт | Назначение |
|--------|------------|
| `compare_settings.py` | Сравнение **Defined settings** между двумя отчётами (НТ и ПРОМ) |
| `check_report.py` | Health-check **одного** отчёта (checkpoints, WAL, sessions, SQL и др.) |
| `compare_runs.py` | Сравнение **метрик** двух тестовых прогонов |
| `compare_nt_prod.py` | **НТ vs ПРОМ**: gate по настройкам + WAL, DML, SQL по параметрам |
| `analyze_pgprofile.py` | **Оркестратор**: все анализы + JSON + brief + Confluence |
| `merge_confluence.py` | Сборка `confluence_stub.wiki` + ответ ИИ → `confluence_page.wiki` |

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
| `--exit-code-settings-only` | Exit `1` только при расхождении настроек |

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

## 5. Оркестратор `analyze_pgprofile.py`

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
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--report` | HTML для health-check (и первая среда для settings diff) |
| `--config` | Пороги (default: `thresholds.yaml`) |
| `--compare-run` | Второй HTML для сравнения прогонов |
| `--run-a-id`, `--run-b-id` | Метки прогонов |
| `--compare-settings` | Второй HTML для diff настроек (требует `--report`) |
| `--settings-a-id`, `--settings-b-id` | Метки сред (default: `NT` / `PROD`) |
| `--output-dir` | **Обязательный** каталог результатов |
| `--confluence-title` | Заголовок страницы Confluence (auto по умолчанию) |
| `--min-change-pct`, `--top-n` | Параметры compare_runs |
| `--exit-code` | Exit `1`, если есть находки / расхождения |

### Файлы в `--output-dir`

| Файл | Кто создаёт | Назначение |
|------|-------------|------------|
| `health_check.json` | Python | Health-check (если `--report`) |
| `run_comparison.json` | Python | Сравнение прогонов (если `--compare-run`) |
| `settings_diff.json` | Python | Diff настроек (если `--compare-settings`) |
| `findings.json` | Python | Все находки (combined) |
| `advisor.json` | Python | Находки + рекомендации из `knowledge/` |
| `brief.md` | Python | Краткий brief |
| `summary_prompt.txt` | Python | Промпт для LLM (`prompts/analyst.md` + brief) |
| `confluence_stub.wiki` | Python | Шапка + таблица находок (Wiki Markup) |
| `confluence_prompt.txt` | Python | Промпт для gigacli (Wiki Markup) |
| `confluence_body.wiki` | **ИИ** | Текстовые разделы (резюме, рекомендации) |
| `confluence_page.wiki` | `merge_confluence.py` | Готовая страница |

---

## 6. Публикация в Confluence (GigaIDE + gigacli)

Схема: **Python — точные таблицы**, **ИИ — только narrative** в Confluence Wiki Markup.

### Шаг 1. Генерация

```bash
python analyze_pgprofile.py \
  --report load_test.html \
  --output-dir ./analysis_out/ \
  --confluence-title "НТ sprint-42: pg_profile"
```

`confluence_stub.wiki` можно вставить в Confluence **сразу** — таблица находок уже готова.

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

### Сравнить два прогона

```bash
python compare_runs.py run_v1.html run_v2.html \
  --run-a-id sprint_42 --run-b-id sprint_43 \
  --only dml,queries,cluster
```

### Полный отчёт для Confluence

```bash
python analyze_pgprofile.py --report nt.html --output-dir ./analysis_out/
# → gigacli + merge_confluence.py (см. раздел 5)
```

### CI (без LLM)

```bash
python compare_settings.py nt.html prod.html --exit-code
python check_report.py report.html --exit-code
python analyze_pgprofile.py --report report.html --output-dir ./out/ --exit-code
```

Аудит и пайплайны опираются на Python и `findings.json`, не на gigacli.

---

## Структура проекта

```
pg_profile_checks/
├── compare_settings.py      # CLI: настройки НТ vs ПРОМ
├── compare_runs.py          # CLI: метрики двух прогонов
├── check_report.py          # CLI: health-check одного отчёта
├── compare_nt_prod.py       # CLI: валидация НТ vs ПРОМ
├── analyze_pgprofile.py     # CLI: оркестратор
├── merge_confluence.py      # CLI: stub + body → confluence_page.wiki
├── pgprofile_parser.py      # Парсинг HTML → JSON
├── pgprofile_health.py      # Логика health-check
├── pgprofile_compare.py     # Логика сравнения прогонов
├── pgprofile_nt_prod.py     # НТ vs ПРОМ: settings gate + отчёт
├── pgprofile_findings.py    # warnings → finding IDs
├── pgprofile_advisor.py     # knowledge/ → рекомендации
├── pgprofile_confluence.py  # Confluence Wiki Markup
├── pgprofile_output.py      # JSON output helpers
├── knowledge/               # YAML playbook (offline)
├── prompts/                 # Промпты для gigacli
├── thresholds.yaml
├── thresholds_relaxed.yaml
├── requirements.txt
├── resources/               # Примеры HTML
└── README.md
```

---

## Ограничения

- Только HTML-отчёты pg_profile в текущем формате (`const data={...}`).
- `compare_settings.py` — только **Defined settings**. Параметр, явно заданный на одной среде и дефолтный на другой, попадёт в «Only in NT» / «Only in PROD» — это ожидаемо.
- Пороги в `thresholds.yaml` — ориентир; подбирайте под свою нагрузку.
- ИИ (gigacli) — опционально, только для оформления текста; цифры и таблицы — из Python.
