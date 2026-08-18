# План доработок после валидации (influence / oracle / Qwen / UI)

Исходный объём A/B/C закрыт: [PGPROFILE_ORACLE_QWEN_PLAN.md](PGPROFILE_ORACLE_QWEN_PLAN.md). Этот документ — **следующий контур**: чтобы таблица влияния не врала, UI не путал пару с серией, quality gate нельзя было обойти коротким словом, а коллеги понимали ZIP vs Qwen.

Не переоткрываем уже принятое:

- связь GUC→метрика **не выдумывается**, если нет hint в `knowledge/` / `PARAM_METRIC_HINTS`;
- `probable` ≠ причинность; `proven` только при изоляции (пара) или устойчивости серии;
- токен только из env; default LLM = `dry_run`;
- Python считает цифры, модель только комментирует;
- `--exit-code` оркестратора **не** начинает внезапно смотреть на oracle (CI не ломаем).

---

## Зафиксировать до кода (иначе каждая история разъедется)

| Тема | Решение в этом плане | Почему не иначе |
|------|----------------------|-----------------|
| Серия: `impact` vs `delta_pct` | `impact` и `delta_pct` считаются **по одной** доминантной метрике. Голос остальных correlated — в `pair_effects` / `notes`, не в `impact`. | Сейчас majority-vote и медиана разных множеств → ложный oracle fail на реальных отчётах. |
| Серия: поле `direction` | Контракт не ломаем: `direction` остаётся направлением **GUC** (`increased`/`decreased`). Добавляем `metric_direction`: `up`/`down`/`flat`. В UI/wiki подписываем обе оси. | Oracle уже игнорирует GUC-direction; путаница в таблице остаётся. |
| Пара: `evidence_count` | = 1, если строка атрибутирована, иначе 0. Число совпавших hint-метрик — в `notes`, не в «Наблюдений». В серии `evidence_count` = число пар, как сейчас. | Колонка выглядит как статистическое доказательство. |
| Пара: какая метрика | По-прежнему max \|Δ%\| **только среди hint-совпадений**. Не расширяем на «самую двинувшуюся метрику вообще». | Иначе вернёмся к выдуманной причинности. |
| Quality gate subject | Claim `subject` должен совпасть с параметром/метрикой после нормализации **целиком** (или как qualified `section.metric`). Подстрока `"wal"` ∈ `"wal_buffers"` — fail. Overclaim `proven` — по тому же ключу, что grounding. | Иначе `publishable=true` на размытых claims. |
| `--exit-code` | Поведение не меняем. Новый флаг `--exit-code-quality` (fail oracle/quality → 1). | Молча поменять CI нельзя. |
| Policy | Default остаётся `none`. В UI явная пометка: в модель может уйти SQL из findings. Включение `bank_redact` — решение контура, не кода. | Hard-block не согласовывали. |
| ZIP / внешний ИИ | Quality gate **только** у кнопки Qwen. ZIP — инструкция + файлы, без автопроверки ответа gigacli. | Не обещать коллегам то, чего нет. |

---

## Порядок (зависимости)

```
P0 корректность цифр ──► P0 UI mode ──► ужесточить тесты серии
         │
         ├──► P1 quality gate subject
         ├──► P1 evidence_count + подписи осей
         └──► P2 epic 3 UX (фильтры, workload warning, список GUC)
                    │
                    └──► P3 процесс: smoke --full в чеклист, коммит, флаги CI
```

P0 блокирует широкую раздачу серии НТ. P2 не блокирует wiki/Confluence.

---

## P0 — чтобы таблица и UI не врали

### P0.1 Согласовать серийный `impact` с доминантной метрикой

Файлы: `pgprofile_influence.py`, фикстуры series, `scripts/check_e2e.py`, `scripts/check_oracle.py`.

- Доминантная метрика как сейчас (частота hits).
- `delta_pct` = медиана Δ% **этой** метрики (уже так).
- `impact` = `expected_metric_impact(dominant, delta_pct)`, не majority по всем correlated.
- На тройке `10.3.81.94` строка `checkpoint_completion_target` больше не `improved` при росте `checkpoint_write_time`.
- E2E: серийный oracle **не fail** из‑за этой строки (другие warning допустимы).
- Снять `allow_fail` / послабления в `check_oracle*` / `check_quality_report` для этой причины.

Приёмка: `python scripts/check_e2e.py` и `python scripts/check_oracle.py` без оговорки «серия имеет право быть fail».

### P0.2 UI: не называть серию парой

Файл: `ui/web/js/app.js` (+ summary в `ui/analysis_runner.py`, если не хватает поля).

- `modeLabel` **не** из `workload_match.level` (это `high|medium|low`).
- Источник: `influence.type === "influence_table_series"` или `run_identity.mode` / `settings_table`.
- При серии снова видны блоки «Отличающиеся настройки по прогонам» и метрики по прогонам.
- Пилюля mode показывает `series`.

Приёмка в UI: три НТ + `high_cpu` → mode **series**, две доп. таблицы не пустые.

После правки Python/JS — перезапуск `ui/server.py` и refresh.

---

## P1 — quality gate и честная семантика колонок

### P1.1 Жёсткий grounding claims

`pgprofile_llm_validate.py` + `scripts/check_llm_validate.py`.

- Совпадение subject: нормализованное равенство или полный qualified metric; **запретить** `in` по подстроке короче имени.
- `evidence_by_param` для overclaim искать тем же ключом, что grounding.
- Негативные тесты: subject `wal`, `read`, `buffers` при каталоге `shared_buffers` / `cache.postgres.blks_read` → не publishable.
- `proven` на `probable`-строке по-прежнему fail.

### P1.2 `evidence_count` и две оси direction

- Пара: `evidence_count` 0|1, как в таблице выше.
- Серия: без изменения смысла (число пар).
- Поле `metric_direction` в JSON; wiki/UI: «GUC» vs «метрика».
- Подпись колонки в UI не «Наблюдений», если это пары: «Пар» / «Есть связь».
- Backcompat: новые поля только добавляем, старые ключи не удаляем (`check_contract_backcompat.py`).

### P1.3 Не слать в Confluence заблокированный ответ «по привычке»

Не прятать copy (удобно отлаживать). В UI усилить: рядом с копированием «это не для Confluence, quality gate закрыт». ZIP README уже говорит это — проверить, что после **перезапуска UI** в архиве новый `README_AI.txt`.

---

## P2 — Epic 3, то что план A обещал в UI

Делать после P0.2, иначе фильтры сядут на неверную таблицу.

### P2.1 Сопоставимость нагрузки

- Пилюля `workload_match` уже есть.
- Если `level=low` или score &lt; 0.6 — `compare-warning`, как у «слишком много параметров».
- Текст: сравнивать осторожно, не трактовать Δ как эффект настройки.

### P2.2 Фильтры и сортировка таблицы влияния

- Фильтры: `impact`, `confidence`, `evidence_type` (probable / proven).
- Сортировка: \|Δ%\|, confidence.
- Короткий hint: probable = гипотеза, proven = изоляция/устойчивая серия.

### P2.3 Много GUC и причина confidence

- Рядом с «улучшилось/ухудшилось» список изменённых параметров (хотя бы top 15 + «и ещё N»).
- В compare-блоке 1–2 причины из `confidence_reasons` / trail, не только на вкладке Качество.

Критерий из исходного DoD: «в UI видны улучшения/ухудшения, сопоставимость и ограничения» — закрывается этим эпиком.

---

## P3 — процесс, CI, документация

### P3.1 Флаг качества для пайплайна

`analyze_pgprofile.py --exit-code-quality` или явное использование уже существующего `run_quality.py --exit-code` в README/чеклисте. Default `--exit-code` не трогать.

### P3.2 Smoke и фикстуры

- В [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md): перед релизом UI — `check_smoke.py --full`, не только короткий smoke.
- Не считать копирование `e2e/series` → `case_matrix/…` заменой матрицы UI.
- После P0.1 вернуть жёсткий assert «серия на трёх HTML не fail из‑за polarity».

### P3.3 Коллеги и ZIP

Уже начато (`README_AI.txt`, подсказки вкладок). Добить:

- перезапуск UI на стенде;
- 10 строк в README: «два пути: Qwen с gate / ZIP без gate»;
- не обещать, что внешняя модель «имеет все функции».

### P3.4 Git

Сейчас контур **не закоммичен**. Отдельный шаг по вашей команде: логичные коммиты (контракт+influence / oracle+quality / llm / ui / docs), без `analysis_out_test/` и `__pycache__`.

### P3.5 Policy на контуре банка

Не включать `bank_redact` в yaml default без решения ИБ. Задача: в UI hint + runbook «что уходит в модель».

---

## Явно не делаем в этом контуре

- Автопубликация в Confluence из Qwen.
- Quality gate на ответ gigacli/ChatGPT по ZIP.
- Расширение атрибуции «на любую метрику, которая сильнее всего двинулась».
- Стриминг / thinking / tool calling у Qwen.
- Смена default провайдера с `dry_run` на live.
- Фильтры JVM-режима и кнопка Qwen в JVM (бандл из pg_profile).

---

## Как проверять каждый кусок

| После | Команда | Глазами в UI |
|-------|---------|----------------|
| P0.1 | `python scripts/check_e2e.py` + `check_oracle.py` | Серия: нет fail polarity на `checkpoint_completion_target` |
| P0.2 | refresh после рестарта UI | Три НТ: mode series, таблицы настроек/метрик |
| P1.1 | `python scripts/check_llm_validate.py` | Qwen dry_run по-прежнему «нельзя публиковать» |
| P1.2 | `python scripts/check_pair_influence_case.py` | Колонка наблюдений = 0/1 на паре |
| P2 | сценарий diff + nt_runs | warning низкой сопоставимости, фильтры, список GUC |
| Перед стендом | `python scripts/check_smoke.py --full` | ZIP содержит новый README_AI |

---

## Оценка объёма (ориентир)

| Пакет | Суть | Порядок |
|-------|------|---------|
| P0 | серия consistent + UI mode | 1 сессия |
| P1 | gate + evidence_count + metric_direction | 1 сессия |
| P2 | epic 3 UX | 1–2 сессии |
| P3 | флаги, чеклист, коммит | коротко, когда скажете коммитить |

Рекомендуемый старт: **P0.1 затем P0.2** — без этого фильтры P2 и «зелёный oracle на серии» бессмысленны.

Дальше (не P0–P3): [ARCHITECTURE_DEEPENING_PLAN.md](ARCHITECTURE_DEEPENING_PLAN.md) — R1 JVM seam → R2 quality snapshot → R3 session → R4 influence→wiki → R5 UiPayload.
