# Headless Qwen: как устроен, ограничения и подключение к pg_profile_checks

Документ для инженера, который поднимает Qwen на удалённом сервере (в том числе в банковском контуре) и подключает его к этому приложению. Источники: официальная документация Qwen (qwen.readthedocs.io), vLLM, Ollama, llama.cpp — сверка на 18 августа 2026.

Модель вызывается из UI (**Запросить Qwen**) или CLI: `python run_llm.py`. Диагностика ошибок и quality gate — в [LLM_QUALITY_RUNBOOK.md](LLM_QUALITY_RUNBOOK.md).

## 1. Что такое headless Qwen простыми словами

Qwen — семейство открытых моделей Alibaba. «Headless» значит: модель работает как HTTP-сервис без чат-окна. Приложение шлёт JSON на эндпоинт и получает JSON с текстом ответа.

Типичный контракт — OpenAI Chat Completions:

- `POST {base}/v1/chat/completions`
- тело: `model`, `messages` (`system` + `user`), `temperature`, `max_tokens`, `stream: false`
- ответ: `choices[0].message.content`

Это не отдельный продукт «Headless Qwen». Это обычный Qwen, поднятый одним из серверов:

| Сервер | Когда выбирать | Эндпоинт по умолчанию |
| --- | --- | --- |
| **vLLM** | GPU, продакшен, длинный контекст, много запросов | `http://127.0.0.1:8000/v1` |
| **SGLang** | То же, если в контуре уже принят SGLang | порт задаётся при запуске |
| **llama.cpp server** | CPU или одна GPU, GGUF, проще в закрытом контуре | `http://127.0.0.1:8080/v1` |
| **Ollama** | Быстрый локальный стенд | `http://127.0.0.1:11434/v1` |
| **Внутренний шлюз банка** | Модель уже отдаёт соседняя команда | URL и заголовки выдаёт владелец шлюза |

Приложение умеет два типа провайдера:

- `qwen_local` — прямой OpenAI-совместимый сервер, токен не обязателен.
- `qwen_gateway` — тот же контракт (или близкий), но токен из переменной окружения обязателен.

`dry_run` сеть не трогает: им проверяют сбор промпта на ноутбуке.

## 2. Как это выглядит в нашем приложении

Цепочка такая:

1. Сначала обычный анализ: `analyze_pgprofile.py` или UI. В каталоге появляются `brief.md`, `influence_summary.md` и остальные артефакты.
2. UI (**Запросить Qwen**) или `run_llm.py` собирает из этих файлов **prompt bundle** (пресеты `summary`, `tuning`, `detailed_rca`).
3. Клиент (`pgprofile_llm.py`) делает один POST через стандартный `urllib` — без `openai` SDK и без pip-зависимостей сверх уже имеющихся.
4. В тот же каталог пишутся `llm_request_*.json`, `llm_response_*.json`, `llm_*.md` с `trace_id`, плюс `llm_quality_*.json`. Если `publishable=false` (в том числе всегда для `dry_run`), CLI печатает `PUBLISH_BLOCKED`.

Что клиент реально отправляет:

- URL = `base_url` + `path` (по умолчанию `http://127.0.0.1:8000/v1` + `/chat/completions`)
- заголовки: `Content-Type: application/json`, `Accept: application/json`, `X-Trace-Id`, плюс опционально `Authorization: Bearer …` и `X-Client-Id`
- тело: `model`, `messages`, `temperature`, `max_tokens`, `stream: false`
- текст ответа читается по пути `choices.0.message.content`

Чего клиент **не** шлёт: `top_p`, `top_k`, `presence_penalty`, `enable_thinking`, `chat_template_kwargs`, `stream: true`. Для анализа отчётов это нормально: нужен короткий фактический ответ, а не «thinking»-трасса.

Конфиг: `llm_providers.yaml`. Токены в файл не кладутся. Побеждают переменные окружения:

| Переменная | Назначение |
| --- | --- |
| `PGPROFILE_LLM_PROVIDER` | какой провайдер взять (`qwen_local` / `qwen_gateway`) |
| `PGPROFILE_LLM_BASE_URL` | адрес API (`…/v1`, без `/chat/completions`) |
| `PGPROFILE_LLM_MODEL` | имя модели, **как его отдаёт сервер** в `/v1/models` |
| `PGPROFILE_LLM_TIMEOUT` | таймаут HTTP в секундах |
| `PGPROFILE_LLM_TOKEN` | токен для `qwen_gateway` (имя задаётся в `auth.token_env`) |

Также уважаются стандартные переменные Python/`urllib`:

- TLS внутреннего УЦ: `SSL_CERT_FILE`, `SSL_CERT_DIR`
- прокси банка: `https_proxy` / `http_proxy` / `no_proxy`

## 3. Ограничения и нюансы (из официальных docs)

### 3.1. Контекст — главный практический лимит

«Модель на 128K» не значит, что сервер примет 128K токенов. Лимит задаёт **сервер** (`--max-model-len` у vLLM, `--ctx-size` у llama.cpp, `num_ctx` / `OLLAMA_CONTEXT_LENGTH` у Ollama). KV-кэш ест VRAM линейно от длины.

Наше приложение режет промпт по **символам**, не по токенам: по умолчанию `max_chars=60000` (~15–25K токенов в зависимости от русского текста и таблиц). Если контекст сервера 4K–8K, запрос упадёт с ошибкой длины, даже если клиент «уложился».

Особо Ollama: без настройки контекст зависит от VRAM (часто 4K на картах < 24 ГиБ). Для наших бандлов этого мало. Нужно явно поднять контекст.

vLLM при OOM рекомендует уменьшить `--max-model-len` и/или `--gpu-memory-utilization` (по умолчанию 0.9). Для Qwen3 без YaRN разумный потолок ~40K позиций; YaRN включают только если реально нужны длинные тексты — на коротких он может ухудшить качество.

Qwen3.6-35B-A3B: нативный контекст 262144, рекомендуемый минимум для thinking — 128K, стандартный деплой в карточке модели — 8 GPU. Для нашего CLI это избыточно; достаточно меньшей dense/Instruct модели.

### 3.2. Thinking vs Instruct

У Qwen3 по умолчанию модель «думает» в блоках `<think>…</think>`. Это:

- тратит `max_tokens` на внутренние рассуждения;
- кладёт мысль либо в `content`, либо в нестандартное поле `reasoning_content` (если сервер запущен с `--reasoning-parser qwen3`);
- **несовместимо** с чистым OpenAI-контрактом.

Наш клиент читает только `message.content`. Если сервер отдаёт мысль в `reasoning_content`, а `content` пустой — получите `LLMResponseError: no text at response_path`.

Для анализа pg_profile нужен Instruct / non-thinking:

- Qwen3-Instruct-2507 — только non-thinking, отдельных флагов не нужно.
- Обычный Qwen3: на сервере `--chat-template` без thinking **или** в запросе `chat_template_kwargs.enable_thinking=false` (наш клиент это пока не шлёт — проще отключить на стороне сервера).
- Рекомендуемые sampling для Instruct: `temperature=0.7`, `top_p=0.8`. У нас в yaml стоит `0.2` — это сознательно ниже, чтобы меньше выдумывать цифры. Менять можно флагом `--temperature`.

Не ставьте greedy (`temperature=0`) на thinking-модели: у Qwen это даёт деградацию.

### 3.3. Имя модели должно совпасть один в один

В теле запроса поле `model` — не «человеческое имя», а идентификатор, который вернул `GET /v1/models`. Примеры:

- vLLM: `Qwen/Qwen3-8B` или локальный путь
- Ollama: `qwen3:8b`
- llama.cpp: часто имя файла GGUF
- шлюз банка: внутренний алиас (`qwen2.5-72b-instruct` и т.п.)

Несовпадение обычно даёт HTTP 404/400.

### 3.4. Безопасность сервера

vLLM прямо пишет: `--api-key` / `VLLM_API_KEY` закрывает только пути `/v1`, `/v2`, `/inference`. Эндпоинт `/invocations` и ряд операционных (`/pause`, `/update_weights`, …) остаются без ключа. Для банка:

- не публиковать порт модели в открытую сеть;
- слушать `127.0.0.1` и ставить reverse proxy (nginx/Envoy), который пускает **только** `/v1/chat/completions` и `/v1/models`;
- TLS и аутентификацию делать на прокси, а не надеяться на `--api-key`.

Ollama на `localhost` аутентификации не требует; ключ в SDK — заглушка. Если Ollama торчит наружу — это дыра.

llama.cpp: `--host` по умолчанию `127.0.0.1`, есть `--api-key`.

Между нодами vLLM трафик по умолчанию не шифруется — кластер только в изолированном сегменте.

### 3.5. Сеть, TLS, прокси, таймауты

- Клиент — синхронный HTTP, `stream=false`. Пока модель пишет ответ, сокет ждёт. Для `tuning`/`detailed_rca` 180–240 с часто мало; поднимайте `timeout_sec` / `PGPROFILE_LLM_TIMEOUT`.
- Повторы: HTTP 408, 425, 429, 500, 502, 503, 504. 401/403 не ретраятся.
- Корпоративный MITM-прокси: положите цепочку УЦ в `SSL_CERT_FILE`. Без этого будет `LLMTransportError` на TLS.
- Если Qwen в том же VLAN, а весь HTTPS ходит в прокси — добавьте хост модели в `no_proxy`.
- Скачивание весов с Hugging Face из банка часто закрыто. Модель качают на разрешённой машине (`huggingface-cli download` или ModelScope: `VLLM_USE_MODELSCOPE=true`) и заносят внутрь. На инференс-сервере: `HF_HUB_OFFLINE=1`.

### 3.6. Версии движка

| Модель | Минимум vLLM (официально) |
| --- | --- |
| Qwen3 | `>= 0.8.5`, лучше `>= 0.9.0` (парсер `qwen3`) |
| Qwen3.6 | `>= 0.19.0` |

Старый vLLM модель может загрузить и упасть на первом thinking-запросе.

### 3.7. Что приложение сознательно не умеет

- стриминг токенов;
- разбор `reasoning_content`;
- tool calling;
- мультимодальный вход (картинки/видео Qwen3.5/3.6).

## 4. Какой вариант подключения выбрать

```
Ноутбук инженера ──анализ──► каталог analysis_out
                                    │
                                    ▼
                    UI «Запросить Qwen»  /  python run_llm.py
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        qwen_local            qwen_local              qwen_gateway
        127.0.0.1             GPU-хост в VLAN         банковский API
        (модель на           (nginx + TLS             (токен из env)
         той же машине)       только /v1)
```

- **A.** Приложение и модель на одном сервере → `qwen_local`, `base_url: http://127.0.0.1:8000/v1`.
- **B.** Приложение на одном хосте, GPU с моделью на другом → `qwen_local` с `PGPROFILE_LLM_BASE_URL=https://qwen.nt.internal/v1` (прокси на GPU-хосте) **или** сразу `qwen_gateway`, если прокси требует Bearer.
- **C.** Готовый LLM-шлюз банка → только `qwen_gateway`. URL, имя модели, заголовок токена, при необходимости `request_style`/`response_path` — у владельца шлюза.

## 5. Из UI приложения

При старте `ui/server.py` читает `llm_providers.yaml` и делает короткий probe живого провайдера (`ui.probe_on_startup`, таймаут `ui.probe_timeout_sec`). Блок **Headless Qwen** и вкладка **Качество** видны только если probe ответил. `dry_run`, ошибка соединения, `--skip-llm-probe` или `ui.probe_on_startup: false` скрывают оба.

1. Задача: `summary` / `tuning` / `detailed_rca`.
2. Провайдер берётся из yaml (`default_provider`) или `--llm-provider` / `PGPROFILE_LLM_PROVIDER` при старте UI.
3. **Запросить Qwen** — статусы `в очереди` → `выполняется` → `готово` или `ошибка`. При провале quality gate статус **«нельзя публиковать»**; копировать текст можно.
4. Ответ, `trace_id` и файлы `llm_request_*.json` / `llm_response_*.json` / `llm_*.md` / `llm_quality_*.json` попадают в ZIP сессии. Вкладка **Качество** обновляется после ответа.
5. Бейдж **policy none** — сейчас фильтрации нет. Включение без правки кода: `PGPROFILE_LLM_POLICY=bank_redact` или `active:` в `llm_policy.yaml`.

JVM-режим кнопку не показывает: бандл собирается из артефактов pg_profile.

Перезапустите UI после обновления кода: `.venv/bin/python ui/server.py`.

## 6. Пошаговое подключение на удалённом сервере

Ниже — вариант B (самый частый в банке): GPU-сервер с vLLM + машина с этим репозиторием. Вариант A — те же шаги, но `base_url` остаётся `127.0.0.1`.

### Шаг 0. Что спросить у владельца модели / ИБ

Без этих ответов дальше гадание:

1. Хост и порт, HTTP или HTTPS.
2. Точный path: `/v1/chat/completions` или другой.
3. Имя модели для поля `model`.
4. Нужен ли Bearer / другой заголовок, имя переменной с токеном.
5. Есть ли клиентский заголовок (`X-Client-Id`, `X-Api-Key`).
6. Обязательный корпоративный прокси и пакет УЦ.
7. Лимит контекста и таймаут шлюза (часто 60 с — нам мало).
8. Где лежит JSON ответа, если это не `choices[0].message.content`.

### Шаг 1. Поднять модель (если поднимаете сами)

На GPU-сервере, веса уже лежат локально:

```bash
export HF_HUB_OFFLINE=1
export HF_HOME=/opt/models/huggingface

# Instruct, без thinking. Подставьте свой путь и число GPU.
vllm serve /opt/models/Qwen3-8B-Instruct \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.85 \
  --api-key "$VLLM_API_KEY"
```

Прокси (на том же хосте) должен слушать внутренний TLS и проксировать **только**:

- `GET  /v1/models`
- `POST /v1/chat/completions`

Наружу vLLM напрямую не открывать.

Ollama, если GPU-стенд простой:

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_CONTEXT_LENGTH=32768 ollama serve
ollama run qwen3:8b
# API: http://127.0.0.1:11434/v1/chat/completions, model=qwen3:8b
```

llama.cpp:

```bash
./llama-server -m /opt/models/qwen3-8b-instruct.gguf \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 32768 --jinja \
  --api-key "$LLAMA_API_KEY"
```

Проверка с GPU-хоста:

```bash
curl -sS http://127.0.0.1:8000/v1/models
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -d '{"model":"REPLACE_ME","messages":[{"role":"user","content":"ping"}],"max_tokens":8,"stream":false}'
```

В ответе должен быть непустой `choices[0].message.content`. Запомните точное `id` модели из `/v1/models`.

### Шаг 2. С машины приложения проверить сеть

Подставьте URL прокси/шлюза:

```bash
# без прокси, если хост внутренний
export no_proxy="qwen.nt.internal,10.0.0.0/8"
# при необходимости
export SSL_CERT_FILE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
export https_proxy=http://proxy.bank.internal:3128

curl -sv --max-time 20 https://qwen.nt.internal/v1/models
```

Ожидание: HTTP 200 или 401 (сервер жив, не хватает ключа). Таймаут / TLS verify failed / 403 от прокси — чинить сеть, не приложение.

### Шаг 3. Прописать провайдер

Не коммитьте боевые URL с токенами. На сервере либо правьте локальную копию yaml, либо обойдитесь env.

Прямой vLLM/llama.cpp/Ollama:

```bash
export PGPROFILE_LLM_PROVIDER=qwen_local
export PGPROFILE_LLM_BASE_URL=https://qwen.nt.internal/v1
export PGPROFILE_LLM_MODEL='Qwen/Qwen3-8B-Instruct'   # как в /v1/models
export PGPROFILE_LLM_TIMEOUT=300
```

Если локальный vLLM требует `--api-key`, тип всё равно `qwen_local`, но в yaml добавьте блок `auth` по образцу `qwen_gateway` (или переключитесь на `qwen_gateway` с тем же `base_url`).

Шлюз банка:

```bash
export PGPROFILE_LLM_PROVIDER=qwen_gateway
export PGPROFILE_LLM_BASE_URL=https://llm-gateway.internal/api/v1
export PGPROFILE_LLM_MODEL=qwen2.5-72b-instruct
export PGPROFILE_LLM_TOKEN='…секрет из сейфа, не в git…'
export PGPROFILE_LLM_TIMEOUT=300
```

Если шлюз ждёт не `messages`, а одно поле `prompt`:

```yaml
# в локальном llm_providers.yaml у qwen_gateway
request_style: prompt
response_path: choices.0.message.content   # или data.text — как скажет владелец
```

Проверка, что приложение видит конфиг:

```bash
cd /path/to/pg_profile_checks
.venv/bin/python run_llm.py --list-providers
```

У `qwen_gateway` колонка token должна быть `PGPROFILE_LLM_TOKEN=set`, не `MISSING`.

### Шаг 4. Preflight — отделяем сеть от промпта

```bash
.venv/bin/python run_llm.py --check-connection --provider qwen_local
```

Успех:

```
CONNECTION_OK
```

Типичные отказы:

| Сообщение | Что чинить |
| --- | --- |
| `LLMConfigError: token is expected in env …` | не экспортирован `PGPROFILE_LLM_TOKEN` |
| `LLMAuthError` HTTP 401/403 | неверный токен, не тот заголовок, IP не в allowlist |
| `LLMTimeoutError` | мало `timeout_sec`, шлюз режет, модель не влезла в GPU |
| `LLMTransportError: connection failed` | DNS, firewall, прокси, `no_proxy` |
| `LLMTransportError: HTTP 404` | неверный `path` или имя модели |
| `LLMResponseError: no text at response_path` | шлюз кладёт текст в другое поле; thinking ушёл в `reasoning_content` |
| TLS / certificate verify failed | `SSL_CERT_FILE` на внутренний УЦ |

### Шаг 5. Собрать бандл без вызова модели

Нужен уже посчитанный каталог анализа (`brief.md` и т.д.):

```bash
.venv/bin/python run_llm.py \
  --output-dir analysis_out \
  --task summary \
  --print-bundle
```

Смотрите размер: `Prompt size: N chars`. Если N больше того, что тянет контекст сервера — уменьшите `--max-chars` или увеличьте контекст модели.

### Шаг 6. Боевой вызов

```bash
.venv/bin/python run_llm.py \
  --output-dir analysis_out \
  --task summary \
  --provider qwen_local
```

В каталоге появятся:

- `llm_request_summary.json` — что ушло (без секрета токена в теле)
- `llm_response_summary.json` — сырой ответ, latency, `trace_id`
- `llm_summary.md` — текст для человека
- `llm_quality_summary.json` — score, checks, `publishable`
- обновлённые `oracle_report.json` и `quality_report.json` (слой `llm`)

Другие пресеты: `--task tuning`, `--task detailed_rca` (больше входных секций, дольше генерация).

Повторить тем же `trace_id` на стороне шлюза можно по заголовку `X-Trace-Id`.

### Шаг 7. Минимальный набор на systemd-хосте приложения

Файл окружения, например `/etc/pgprofile-checks/llm.env` (права 0600):

```bash
PGPROFILE_LLM_PROVIDER=qwen_gateway
PGPROFILE_LLM_BASE_URL=https://llm-gateway.internal/api/v1
PGPROFILE_LLM_MODEL=qwen2.5-72b-instruct
PGPROFILE_LLM_TIMEOUT=300
PGPROFILE_LLM_TOKEN=replace-me
SSL_CERT_FILE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
no_proxy=llm-gateway.internal,qwen.nt.internal
```

```bash
set -a && source /etc/pgprofile-checks/llm.env && set +a
.venv/bin/python run_llm.py --check-connection
```

## 7. Рекомендуемые модели под эту задачу

Для разбора отчётов pg_profile (факты, таблицы, русский язык) достаточно Instruct, не мультимодального гиганта:

- стенд / одна GPU 24 ГиБ: Qwen3-8B-Instruct или Qwen3-14B AWQ/GPTQ;
- рабочий контур: Qwen3-32B-Instruct или Qwen3-30B-A3B-Instruct-2507;
- если банк уже отдаёт 72B через шлюз — используйте его, клиенту всё равно.

Не берите Qwen3.5/3.6 ради картинок: приложение картинки не шлёт, а деплой требует много GPU.

## 8. Чеклист приёмки на удалённом сервере

Продуктовые этапы **A/B/C готов** (таблица влияния, one-click Qwen, quality gate) и команда `python scripts/check_smoke.py` — в [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md). Ниже — только приёмка **боевого** эндпоинта модели.

1. `curl /v1/models` с той же машины, где крутится `run_llm.py`, возвращает ожидаемое имя.
2. `--list-providers` показывает нужный URL и `token=set` (если шлюз).
3. `--check-connection` печатает `CONNECTION_OK` за разумное время (< таймаута).
4. `--print-bundle` по реальному `analysis_out` укладывается в контекст сервера.
5. `--task summary` пишет `llm_*` файлы и `llm_quality_*.json`. Для `dry_run` ожидается `PUBLISH_BLOCKED`. Боевой ответ опирается на цифры из brief, не выдумывает прогоны.
6. В логах шлюза находится `X-Trace-Id` из артефакта.
7. Порт модели не виден с пользовательских сегментов, только `/v1` через прокси.

## 9. Источники

- [Qwen docs: vLLM](https://qwen.readthedocs.io/en/latest/deployment/vllm.html)
- [Qwen3 GitHub (деплой vLLM/SGLang/Ollama/llama.cpp)](https://github.com/QwenLM/Qwen3)
- [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/openai_compatible_server.html)
- [vLLM security: ограничения `--api-key`](https://docs.vllm.ai/en/stable/usage/security.html)
- [Qwen3.6-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Ollama OpenAI compatibility](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx)
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
