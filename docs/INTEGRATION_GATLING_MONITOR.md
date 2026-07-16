# Встраивание UI pg_profile в Gatling Monitor

Автономный UI уже работает в `pg_profile_checks` (`python ui/server.py`).  
Этот документ — как перенести тот же фронт и контракт API в [gatling-monitor](https://github.com/), чтобы на рабочей машине не поднимать отдельный Python HTTP-сервер.

**Важно:** логику анализа на Java не переписывать. Spring только принимает upload и вызывает:

```text
{python} {project-dir}/analyze_pgprofile.py … --output-dir {work-dir}/{session}/out
```

Либо эквивалент через `python -c` / модульный вызов — главное, чтобы `cwd` был `project-dir` (нужны `knowledge/`, `prompts/`, `thresholds.yaml`).

---

## 1. Скопировать статику

Из `pg_profile_checks/ui/web/`:

| Источник | Куда в gatling-monitor |
|----------|------------------------|
| `css/pgprofile.css` | `src/main/resources/static/css/pgprofile.css` |
| `js/app.js` | `src/main/resources/static/js/pgprofile.js` |
| разметка `index.html` | `src/main/resources/templates/pg_profile.html` |

`gatling-base.css` и `mascot.png` в мониторе уже есть (`static/css/style.css`, `static/img/mascot.png`) — не дублировать.  
В шаблоне подключать:

```html
<link rel="stylesheet" th:href="@{/css/style.css}">
<link rel="stylesheet" th:href="@{/css/pgprofile.css}">
<script th:inline="javascript">
  window.API_BASE = /*[[@{/pg-profile}]]*/ '';
</script>
<script th:src="@{/js/pgprofile.js}"></script>
```

В `pg_profile.html` заменить пути `/css/…`, `/img/…`, `/js/…` на `th:href` / `th:src` Thymeleaf.  
Маскот: `th:src="@{/img/mascot.png}"`.

Если API будет под префиксом `/pg-profile`, в `app.js` уже есть `window.API_BASE` — Java должна отдавать те же относительные пути:

- `GET  {API_BASE}/api/symptoms`
- `POST {API_BASE}/api/analyze`
- `GET  {API_BASE}/api/sessions/{id}/wiki|prompt|brief|zip`

Либо смонтировать API на корне (`/api/...`) и оставить `API_BASE = ""`.

---

## 2. Навигация

В `templates/index.html` и `templates/info.html` в блок `.header-nav` добавить:

```html
<a th:href="@{/pg-profile}" class="nav-link">Профиль</a>
```

В `pg_profile.html` — активный пункт «Профиль», ссылки на `/` и `/info`.

---

## 3. Конфиг

`application.yml` или внешний `config/application.yml` рядом с JAR:

```yaml
spring:
  servlet:
    multipart:
      max-file-size: 50MB
      max-request-size: 200MB

pgprofile:
  python: python3
  # или полный путь к .venv: /path/to/pg_profile_checks/.venv/bin/python
  project-dir: /absolute/path/to/pg_profile_checks
  work-dir: ${java.io.tmpdir}/pgprofile-ui
  timeout-seconds: 300
```

На рабочей машине должны быть:

- JDK для запуска монитора (как сейчас);
- клон `pg_profile_checks` + уже установленный `PyYAML` (как для CLI);
- **без** новых pip-пакетов для UI.

---

## 4. Java-слой (зеркало `ui/server.py`)

Создать:

- `controller/PgProfileController.java` — `GET /pg-profile` → шаблон; REST как в таблице выше;
- `service/PgProfileAnalysisService.java` — temp dirs, `ProcessBuilder`, выбор артефактов, ZIP.

Пример вызова CLI (сценарий «несколько НТ»):

```text
{python} analyze_pgprofile.py
  --nt-reports a.html b.html
  --nt-label old_app --nt-label new_before_guc
  --symptoms high_cpu,high_wal
  --prod-reports p1.html p2.html
  --output-dir /tmp/pgprofile-ui/<session>/out
```

Правила:

- `ProcessBuilder.directory(projectDir)`;
- не принимать произвольную shell-команду от клиента — только allowlist аргументов;
- таймаут из `pgprofile.timeout-seconds`;
- stderr при ненулевом exit → JSON `{"error": "..."}`;
- приоритет wiki/prompt как в `ui/analysis_runner.py` (`WIKI_PRIORITY`, `PROMPT_PRIORITY`).

Контракт `POST /api/analyze` (multipart):

- поле `meta` — JSON:

```json
{
  "scenario": "nt_runs|symptom|health|stable_prod|nt_prod|auto",
  "symptoms": ["high_cpu"],
  "reports": [
    {"filename": "a.html", "env": "NT", "label": "old_app", "order": 0}
  ],
  "query_hex": null,
  "query_id": null,
  "query_text": null,
  "confluence_title": null
}
```

- поля `file` — HTML в том же порядке, что `meta.reports`.

Ответ 200:

```json
{
  "session_id": "uuid",
  "scenario": "nt_runs",
  "wiki": "nt_runs_confluence.wiki",
  "prompt": "summary_prompt.txt",
  "brief": "nt_runs_brief.md",
  "summary": {},
  "wiki_text": "...",
  "prompt_text": "...",
  "brief_text": "..."
}
```

ZIP: содержимое `out/` + `README_AI.txt` (текст как `AI_USAGE` в `analysis_runner.py`).

Ориентир по логике сборки флагов CLI: [`ui/analysis_runner.py`](../ui/analysis_runner.py) (`build_namespace`).

---

## 5. Проверка

```bash
# CLI жив
cd /path/to/pg_profile_checks
source .venv/bin/activate
python analyze_pgprofile.py --help

# монитор
java -jar gatling-monitor-1.0.0.jar \
  --spring.config.additional-location=file:./config/

# браузер
open http://localhost:8088/pg-profile
```

Прогнать кейс из `resources/` (например 2× prom + симптом `high_cpu`) — получить wiki и ZIP.

---

## 6. Что не трогать

- `pgprofile_*.py`, `knowledge/`, `prompts/` — только вызов CLI;
- SSH / генераторы / авто-refresh монитора — ортогонально;
- Spring Security по-прежнему нет: страница доступна всем, кто достучится до порта (как Монитор).

---

## 7. Standalone после встраивания

`python ui/server.py` можно оставить для локальной разработки анализа без Java.  
На рабочей машине с монитором — опционально не использовать.
