# Конфиги АС (JVM)

Открывай этот файл, если задача про: папки АС, `resources.yaml`, `jvm-config`, список систем в UI, «добавить новую систему», drag-and-drop, **ссылки Bitbucket/Git вместо файлов**, новый формат YAML.

Готово, когда: парсеры по-прежнему отдают `PodResourcesBudget` и `Dict[str, list[str]]`; список АС и выбор контейнера работают; зелены проверки в конце.

Не читай корневой README. Не трогай `pgprofile_*.py`.

---

## Контракт сейчас

Каждая АС — **локальная папка**. Имя папки = имя в выпадающем списке UI.

```text
jvmcheck_runtime/resources/<AS_NAME>/
  resources.yaml      # обязателен (допустим .yml)
  jvm-config.txt      # желателен (.txt / .yaml / .yml)
  last_input.json     # пишет UI после прогона; не руками
```

Demo-АС (`DEMO_*`) лежат отдельно и всегда в списке:

```text
resources/jvm_demo/DEMO_CounterAgent/
resources/jvm_demo/DEMO_CreditHistory/
```

Корень рантайма: `JVMCHECK_ROOT` → иначе `jvmcheck_runtime/`. Код: `ui/jvm_runner.py` → `_detect_jvmcheck_root`.

Зарезервированные имена АС: `__new__`, `__root__`, `.`, `..`, любые `DEMO_*`. Путь в имени (`/`, `\`) запрещён. Функция: `normalize_jvm_system_name`.

---

## Два файла, два парсера, два объекта

| Файл на диске | Как узнать | Парсер | Объект наружу |
| --- | --- | --- | --- |
| resources | имя содержит `resource` / `resources` / `values`, суффикс `.yaml`/`.yml` | `parse_k8s_or_stand_yaml(text) -> PodResourcesBudget` | контейнеры с `name`, `pod_name`, request/limit MiB |
| jvm-config | имя содержит `jvm` / `java` / `tool` / `opts` / `options` | `parse_jvm_options_file(path) -> Dict[str, List[str]]` | ключ = **имя контейнера**, значение = флаги `-XX...` |

Имя контейнера в jvm-config **должно совпасть** с `container.name` из resources. Иначе флаги не пришьются.

Эталон форматов: `resources/jvm_demo/DEMO_CounterAgent/resources.yaml` и `jvm-config.txt`.

### Что должен уметь resources YAML

Парсер ест **оба** вида (один файл может быть любым):

1. Kubernetes Deployment/Pod: `spec.template.spec.containers[].name` + `.resources.requests/limits.memory`.
2. Stand-структура (как Finmonweb): секции `app.application.resources.requests.memory` и т.п.

Нужны как минимум memory request и limit у целевого контейнера. CPU желателен. `metadata.name` у workload → `pod_name` в UI.

Код: `jvmcheck_runtime/src/jvmcheck/parsers/k8s_yaml_parser.py`.

### Что должен уметь jvm-config

YAML:

```yaml
application:
  javaToolOptions: >
    -XX:+UseG1GC
    -XX:MaxRAMPercentage=70.0
```

Ключи флагов: `javaToolOptions`, `java_tool_options`, `javaOptions`, `JAVA_TOOL_OPTIONS`, `JAVA_OPTS`, `JAVA_TOOL_OPTS`.

`.txt` — секции `имяКонтейнера:` и строки флагов, начинающиеся с `-`.

Код: `jvmcheck_runtime/src/jvmcheck/parsers/custom_config_parser.py`.

---

## Цепочка вызова (не ломай порядок)

```text
UI список АС
  list_jvm_systems()                    ui/jvm_runner.py
    сканирует jvmcheck_runtime/resources/*/ + ключи DEMO_*

UI «добавить новую систему»
  POST /api/jvm/systems                 ui/server.py _handle_create_jvm_system
    create_jvm_system(name, paths)      ui/jvm_runner.py
      _classify_jvm_upload(filename)    по имени файла, не по содержимому
      пишет resources.yaml / jvm-config.* в папку АС

UI контейнеры
  list_jvm_containers(system)           ui/jvm_runner.py
    resolve файлы → parse_k8s_or_stand_yaml → список pod+container

Анализ
  run_jvm_analysis(req, upload_paths)
    _apply_jvm_uploads                  если пришли файлы — перезаписать
    resolve_system_input_files          jvmcheck_runtime/src/jvmcheck/input_resolver.py
    parse_k8s_or_stand_yaml
    parse_jvm_options_file
    evaluate_jvm_diagnostic_tree        дерево отсечения (другой файл)
```

`resolve_system_input_files(systems_root, system_name, resources_file, jvm_config_file) -> (Path, Path | None)`  
Сейчас всегда **локальные Path**. Парсеры читают текст с диска. Ссылки Bitbucket сюда ещё не входят.

Классификация upload (UI и create):

- resources: суффикс yaml/yml **и** в имени `resource` или `values`
- jvm: суффикс yaml/yml/txt **и** в имени `jvm` или `java`

Имена, которые всегда проходят: `resources.yaml`, `jvm-config.txt`.

---

## Таблица швов — меняй только это

| Что меняется | Файл | Функция |
| --- | --- | --- |
| Как найти файлы в папке АС | `jvmcheck_runtime/src/jvmcheck/input_resolver.py` | `resolve_system_input_files`, `_auto_select_*` |
| Формат resources YAML | `jvmcheck_runtime/src/jvmcheck/parsers/k8s_yaml_parser.py` | `parse_k8s_or_stand_yaml` |
| Формат jvm-config | `jvmcheck_runtime/src/jvmcheck/parsers/custom_config_parser.py` | `parse_jvm_options_file`, `parse_custom_jvm_options` |
| Список АС / создание папки / upload | `ui/jvm_runner.py` | `list_jvm_systems`, `create_jvm_system`, `_classify_jvm_upload`, `_apply_jvm_uploads`, `_resolve_jvm_input_files_for_system` |
| HTTP создать АС | `ui/server.py` | `POST /api/jvm/systems` → `_handle_create_jvm_system` |
| Выпадающий список, «добавить новую систему» | `ui/web/js/app.js` | `loadJvmSystems`, `createJvmSystemFromUi`, константа `__new__` |
| Подписи UI | `ui/web/index.html` | `#jvm-system-name`, `#jvm-new-system-panel` |
| CLI тех же файлов | `jvmcheck_runtime/src/jvmcheck/cli.py` | `--systems-root`, `--system-name`, `--resources-file`, `--jvm-config-file` |

Доменные типы **не менять без нужды**: `ContainerResources`, `PodResourcesBudget` в `jvmcheck_runtime/src/jvmcheck/models.py`.

Не сюда: `diagnostic_tree.py`, `pgprofile_*.py`, `ui/analysis_runner.py`.

---

## Ветка A — изменился формат локальных файлов

Цель: тот же `PodResourcesBudget` / map флагов из нового YAML/текста.

Шаги:

1. Положи **золотой** пример рядом с demo: скопируй новый файл в `resources/jvm_demo/DEMO_CounterAgent/` или во временный фикстурный путь в тесте. Не ломай старый demo, пока парсер не ест оба формата.
2. Расширь **парсер**, не resolver. Resolver по-прежнему возвращает Path.
3. Сохрани оба формата, если старые АС ещё в `jvmcheck_runtime/resources/` (Finmonweb, Finmonmob, SberratingWeb).
4. Если поменялись **имена файлов** — правь hints в `input_resolver.py` и `_classify_jvm_upload`.
5. Тест: парсер на золотом файле + `create_jvm_system` + `list_jvm_containers`.

Критерий: `parse_k8s_or_stand_yaml` на новом тексте даёт контейнер `application` (или реальное имя) с memory request/limit; `list_jvm_containers("DEMO_CounterAgent")` не пустой.

---

## Ветка B — вместо файлов ссылки Bitbucket/Git

Цель: папка АС хранит **указатель** (URL), а парсеры по-прежнему получают **локальный Path** с уже скачанным текстом.

Не скачивай внутри `parse_k8s_or_stand_yaml` / `parse_jvm_options_file`. Скачивание — отдельный шаг **перед** парсером.

### Предлагаемый указатель (ещё не реализован)

Файл `jvmcheck_runtime/resources/<AS_NAME>/source.yaml`:

```yaml
resources_url: https://bitbucket.example/projects/X/repos/Y/raw/deploy/resources.yaml?at=master
jvm_config_url: https://bitbucket.example/projects/X/repos/Y/raw/deploy/jvm-config.txt?at=master
```

Локальные `resources.yaml` / `jvm-config.txt` остаются валидны: если они есть на диске, URL не обязателен.

### Куда писать код

1. Новая функция рядом с resolver, например `materialize_system_input_files(...) -> tuple[Path, Path | None]` в `input_resolver.py` (или новый модуль `jvmcheck/input_fetch.py`).
2. Она: читает `source.yaml` если нет локальных файлов → качает в `jvmcheck_runtime/resources/<AS>/.cache/` (или temp) → возвращает Path на кэш.
3. `run_jvm_analysis` и `list_jvm_containers` вызывают materialize **вместо** голого `resolve_system_input_files`, либо resolve внутри вызывает materialize.
4. UI «добавить новую систему»: либо по-прежнему upload файлов, либо поле URL → запись `source.yaml` без сырого YAML в репозиторий.
5. Классификация upload не меняется для файлов. Новый тип: если пользователь вставил URL — не `_classify_jvm_upload`, а запись указателя.
6. Секреты: токен Bitbucket только из env (`BITBUCKET_TOKEN` или аналог), не в yaml и не в git.
7. Оффлайн/demo: `DEMO_*` всегда локальные, без сети.
8. Таймаут и ошибка сети → понятный `ValueError` на русском в UI, анализ не падает traceback-ом.

### Порядок реализации (один слайс за раз)

1. Тест: папка с `source.yaml` + fake HTTP (локальный файл, подменённый fetcher) → те же контейнеры, что у demo.
2. `materialize_*` + вызов из `list_jvm_containers` / `run_jvm_analysis`.
3. UI: поле URL или пункт «ссылка» рядом с «добавить новую систему».
4. Документ: одна строка в этом файле «указатель реализован: source.yaml».

Критерий: АС без локального `resources.yaml`, но с `source.yaml`, появляется в списке; контейнеры те же, что после ручной загрузки того же YAML; demo и Finmon* работают без сети.

---

## UI: добавить новую систему

Список: `GET /api/jvm/systems`. JS дописывает option `value="__new__"` текст «добавить новую систему».

Панель `#jvm-new-system-panel`: имя + dropzone + «Подгрузить файлы» → `POST /api/jvm/systems` (multipart: `system_name`, `jvm_file`).

После успеха JS выбирает созданную АС и грузит контейнеры.

Для ссылок Bitbucket: не подменяй `__new__`. Добавь поле URL на ту же панель и отдельное поле multipart или JSON `resources_url` / `jvm_config_url`.

---

## Тесты этой ветки

```bash
.venv/bin/python scripts/check_jvm_diagnostic_tree.py
# внутри: test_create_jvm_system, test_ui_contract (поле новой АС, __new__, POST /api/jvm/systems)
.venv/bin/python scripts/check_jvm_tree_cases.py
```

Новый fetcher: тест в `scripts/check_jvm_diagnostic_tree.py` с `tempfile` и **без живого Bitbucket**. Подмени download функцией, которая копирует demo YAML в кэш.

После UI-правок: перезапуск `.venv/bin/python ui/server.py`, страница http://127.0.0.1:8090/ — JVM → список АС → «добавить новую систему».

---

## Золотые файлы

| Назначение | Путь |
| --- | --- |
| K8s resources | `resources/jvm_demo/DEMO_CounterAgent/resources.yaml` |
| jvm-config YAML-like txt | `resources/jvm_demo/DEMO_CounterAgent/jvm-config.txt` |
| Stand resources (секции app/ingress) | `jvmcheck_runtime/resources/Finmonweb/resources.yaml` |
| Боевые АС в рантайме | `jvmcheck_runtime/resources/*/` |
