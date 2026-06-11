# pg_profile_checks

Набор Python-скриптов для анализа HTML-отчётов [pg_profile](https://github.com/zubkov-andrei/pg_profile).

Скрипты читают данные прямо из HTML-файла: отчёт pg_profile — это одностраничное приложение, внутри которого в JavaScript-объекте `const data={...}` лежат все метрики, настройки и тексты запросов. Внешняя база данных не нужна.

## Что умеет проект

| Скрипт | Назначение |
|--------|------------|
| `compare_settings.py` | Сравнивает **Defined settings** между двумя отчётами (НТ и ПРОМ) |
| `check_report.py` | Анализирует **один** отчёт и выводит предупреждения о возможных проблемах производительности |
| `compare_runs.py` | Сравнивает **метрики производительности** двух тестовых прогонов (DML, checkpoints, SQL и др.) |

---

## Требования

- Python 3.10+
- Для `compare_settings.py` — только стандартная библиотека Python
- Для `check_report.py` и `compare_runs.py` — дополнительно PyYAML (см. `requirements.txt`)

---

## Установка

```bash
cd pg_profile_checks

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

> `compare_settings.py` можно запускать и без venv — ему не нужны внешние пакеты.  
> `check_report.py` требует PyYAML из `requirements.txt`.

---

## 1. Сравнение настроек НТ и ПРОМ

### Запуск

```bash
python compare_settings.py NT.html PROD.html
```

Или с именованными аргументами:

```bash
python compare_settings.py \
  --nt resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html \
  --prod /path/to/prod_report.html
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--verbose` | Не обрезать длинные значения настроек (например, `archive_command`) |
| `--exit-code` | Вернуть код выхода `1`, если есть расхождения (удобно для CI) |

### Как это работает

1. Из каждого HTML извлекается JSON-объект `data`.
2. Берётся датасет `datasets.settings` — список всех параметров PostgreSQL.
3. Оставляются только **Defined settings** — параметры, где `defined_val=true` (явно заданные, не дефолтные). Обычно их ~100–150 из ~550.
4. Строятся словари `{имя_параметра: значение}`.
5. Сравнение идёт по **объединению имён** — разное количество настроек в файлах допустимо.

### Что выводится

- **Таблица DIFFER** — параметр есть в обоих файлах, но значения разные (колонки NT и PROD).
- **Only in NT** — параметр явно задан только на тесте.
- **Only in PROD** — параметр явно задан только на проде.
- Совпадающие параметры не показываются, только счётчик в Summary.

### Пример вывода

```
pg_profile Settings diff (Defined settings only)
NT: pgprofile_srv=...  (121 settings)  server=10.55.51.82  ...
PROD: pgprofile_prod_...  (118 settings)  ...

Found 3 difference(s):

Setting            | NT (test) | PROD
-------------------+-----------+-----
max_connections    | 700       | 500
autovacuum_naptime | 10        | 60

Only in NT (1):
  some_param = value

Summary: 2 differ, 1 only NT, 0 only PROD, 118 identical (hidden)
```

### Коды выхода

| Код | Значение |
|-----|----------|
| `0` | Расхождений нет (или `--exit-code` не указан) |
| `1` | Есть расхождения (только с `--exit-code`) |
| `2` | Ошибка: файл не найден, не удалось распарсить HTML |

---

## 2. Проверка одного отчёта на предупреждения

### Запуск

```bash
python check_report.py report.html
```

С примером из `resources/`:

```bash
python check_report.py \
  resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--config thresholds.yaml` | Файл с порогами (по умолчанию `thresholds.yaml` в корне проекта) |
| `--only checkpoints,sessions,wal` | Запустить только выбранные категории |
| `--verbose` | Показывать полный текст SQL в предупреждениях о запросах |
| `--exit-code` | Вернуть код `1`, если есть предупреждения |

### Категории проверок

| Категория (`--only`) | Что проверяется |
|----------------------|-----------------|
| `checkpoints` | Requested checkpoints, время записи checkpoint, bgwriter |
| `queries` | Медленные SQL-запросы (mean/max/total execution time) |
| `autovacuum` | GUC autovacuum + таблицы с bloat / без vacuum |
| `wal` | Переполнение wal_buffers, генерация WAL, max_wal_size |
| `cache` | Cache hit ratio, время чтения с диска, temp files |
| `sessions` | Idle in transaction, откаты, fatal/killed sessions |
| `memory` | work_mem × max_connections, отключённые таймауты |
| `io` | Seq scan, heap reads, WAL-heavy запросы, неиспользуемые индексы |
| `locks` | Deadlocks |

### Как это работает

1. Из HTML извлекается JSON `data`.
2. Загружаются пороги из `thresholds.yaml`.
3. Собирается контекст отчёта: метрики кластера, статистика БД, запросы, настройки, таблицы.
4. Запускаются проверки по каждой категории.
5. Результат группируется и выводится в консоль с уровнями `[WARN]` и `[CRIT]`.

### Настройка порогов

Все пороги задаются в `thresholds.yaml` без изменения кода. Основные секции:

```yaml
checkpoints:
  max_requested_ratio: 0.30      # доля requested checkpoints (0.0–1.0)
  max_write_time_sec: 300

queries:
  max_mean_exec_ms: 1000
  max_max_exec_ms: 5000

sessions:
  max_idle_in_transaction_sec: 3600
  warn_on_disabled_idle_timeout: true

# ... и другие секции — см. thresholds.yaml
```

Для тестирования есть `thresholds_relaxed.yaml` с завышенными порогами (ожидается 0 предупреждений).

### Пример вывода

```
pg_profile health check
Server: tvldd-pprb06733.delta.sbrf.ru
Interval: 2026-03-13 12:30:02+03 .. 2026-03-14 03:30:02+03 (15.0 h)
Report: pgprofile_srv=10_55_51_82_...

== Checkpoints (5) ==
[CRIT] Requested checkpoints: 45/64 (70.3%), threshold 30%
[WARN] Checkpoint write time: 13576.6s over 15.0h interval (threshold 300s)

== Sessions and transactions (2) ==
[CRIT] taxes: idle_in_transaction_time=1112045.7s (threshold 3600s), idle_in_transaction_session_timeout=0

== Slow queries (5) ==
[WARN] postgres/postgres: mean=6461.9ms, max=18142.9ms, total=193.9s, calls=30
  SELECT pgse_profile.take_sample()

Summary: 34 warning(s) (3 critical, 31 warning)
```

### Коды выхода

| Код | Значение |
|-----|----------|
| `0` | Предупреждений нет |
| `1` | Есть предупреждения (только с `--exit-code`) |
| `2` | Ошибка: файл/конфиг не найден, ошибка парсинга |

---

## 3. Сравнение метрик двух тестовых прогонов

### Запуск

```bash
python compare_runs.py RUN_A.html RUN_B.html --run-a-id "прогон_6h" --run-b-id "прогон_24h"
```

Пример с отчётом из `resources/`:

```bash
python compare_runs.py \
  resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html \
  resources/pgprofile_srv=10_55_51_82_from=2026_03_13_12_30_to=2026_03_14_03.html \
  --run-a-id baseline \
  --run-b-id baseline
```

### Флаги

| Флаг | Описание |
|------|----------|
| `--run-a-id` | **Обязательный** RunId первого прогона (подпись колонки в таблице) |
| `--run-b-id` | **Обязательный** RunId второго прогона |
| `--only` | Секции: `cluster`, `wal`, `dml`, `tables`, `queries`, `sessions`, `cache` |
| `--min-change-pct` | Минимальное изменение в % для попадания в вывод (default: 5) |
| `--top-n` | Макс. строк в секциях tables/queries (default: 15) |
| `--verbose` | Полный текст SQL в сравнении запросов |
| `--exit-code` | Exit 1 при значимых расхождениях |

### Как это работает

1. Из каждого HTML загружаются метрики (`dbstat`, `cluster_stats`, `wal_stats`, `top_statements`, `top_tables` и др.).
2. В шапке выводятся RunId, интервал отчёта и длительность в часах.
3. Если длительность прогонов **различается**, появляется предупреждение:
   ```
   [!] Время прогонов отличается на 18.0 часов (6.0 h vs 24.0 h)
   ```
4. Для накопительных счётчиков (DML, calls, checkpoints, WAL) показываются **абсолютные значения** и нормализация **«/час»** (при разной длительности — всегда).
5. Для процентов (`blks_hit_pct`) и средних времён (`mean_exec_time`) — только абсолютное сравнение.

### Секции сравнения

| Секция | Что сравнивается |
|--------|------------------|
| `dml` | INSERT/UPDATE/DELETE/COMMIT/ROLLBACK/FETCH по каждой БД |
| `cluster` | Checkpoints, checkpoint write time, bgwriter |
| `wal` | WAL bytes, wal_buffers_full и др. |
| `tables` | DML и seq/index scan по таблицам (top по delta) |
| `queries` | Top SQL: total_time, calls, mean/max time (top по delta) |
| `sessions` | Сессии, idle in transaction, rollbacks |
| `cache` | Cache hit %, block read/write time |

### Пример вывода

```
pg_profile run comparison
Run A [прогон_6h]: report_6h.html | 2026-03-13 00:00 .. 06:00 (6.0 h) | server=...
Run B [прогон_24h]: report_24h.html | 2026-03-12 00:00 .. 2026-03-13 00:00 (24.0 h) | server=...

[!] Время прогонов отличается на 18.0 часов (6.0 h vs 24.0 h)

== DML by database (3 rows) ==
Metric              | прогон_6h       | прогон_24h      | Delta
--------------------+-----------------+-----------------+--------
taxes.INSERT        | 43.50M (7.25M/h)| 174.13M (7.26M/h)| +300.0%
```

### Коды выхода

| Код | Значение |
|-----|----------|
| `0` | Значимых расхождений нет |
| `1` | Есть расхождения (только с `--exit-code`) |
| `2` | Ошибка: файл не найден, ошибка парсинга |

---

## Структура проекта

```
pg_profile_checks/
├── compare_settings.py      # CLI: сравнение настроек НТ vs ПРОМ
├── compare_runs.py          # CLI: сравнение метрик двух прогонов
├── check_report.py          # CLI: проверка одного отчёта
├── pgprofile_parser.py      # Парсинг HTML → JSON data
├── pgprofile_health.py      # Логика проверок одного отчёта
├── pgprofile_compare.py     # Логика сравнения двух прогонов
├── thresholds.yaml          # Пороги по умолчанию
├── thresholds_relaxed.yaml  # Пороги для тестов (все завышены)
├── requirements.txt         # PyYAML
├── resources/               # Примеры HTML-отчётов
└── README.md
```

---

## Типичные сценарии

### Перед релизом: сравнить конфигурацию НТ и ПРОМ

```bash
python compare_settings.py nt_report.html prod_report.html --exit-code
```

### После нагрузочного теста: найти проблемы в отчёте

```bash
python check_report.py load_test_report.html --exit-code
```

### Сравнить два прогона нагрузочного теста

```bash
python compare_runs.py run_v1.html run_v2.html \
  --run-a-id "sprint_42" \
  --run-b-id "sprint_43" \
  --only dml,queries,cluster
```

### Проверить только checkpoints и сессии

```bash
python check_report.py report.html --only checkpoints,sessions
```

### Использование в CI

```bash
# Упасть, если настройки НТ и ПРОМ различаются
python compare_settings.py nt.html prod.html --exit-code

# Упасть, если в отчёте есть предупреждения
python check_report.py report.html --config thresholds.yaml --exit-code
```

---

## Ограничения

- Скрипты работают только с HTML-отчётами pg_profile в текущем формате (JSON в `const data=...`).
- `compare_settings.py` сравнивает только **Defined settings**, а не все ~550 параметров. Параметр, явно заданный на одной среде и оставшийся дефолтным на другой, попадёт в «Only in NT» / «Only in PROD» — это ожидаемое поведение.
- Пороги в `thresholds.yaml` нужно подбирать под вашу нагрузку и инфраструктуру; значения по умолчанию — ориентир, а не универсальный стандарт.
