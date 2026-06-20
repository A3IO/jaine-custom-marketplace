# jaine-media — Agent Development Guide (грабли + решения)

> Перенесено из спайков `/tmp/jaine-gems-spike/` (исходный R&D в `/0/SANDBOX/ASSISTS`).
> Цель файла: **не наступать на старые грабли** при сборке. Читать ПЕРЕД работой.

## Что строим

MCP-плагин для Claude Code: **media understanding через Gemini** + media-обработка.
Claude не принимает видео и не слышит аудио — Gemini принимает оба. Плагин — мост.
«Продолжать говорить с тем же видео» = переиспользование `fileUri` (content-кэш).

**Scope = media-SUPERTOOL** (решение Криса «всё сразу»), 5 tools:

| tool | делает | зависимость | статус |
|------|--------|-------------|--------|
| `analyze_media(path\|paths, question, detail, max_tokens, model, language, fps, history)` | Gemini see/hear → ответ; multi-video сравнение; multi-turn беседа; finish_reason | httpx (чисто) | ✅ prod (диск-кэш, central log, multi-video #202, multi-turn #206, finish_reason) |
| `extract_frame(path, timecode, window, step)` | таймкод → ОКНО ±window png-кадров в `<sha8>/frames/` (Read нативно) | ffmpeg | ✅ Phase 2 (loop-closure live-verified) |
| `prepare_media(path, height, start, end)` | compress (size) / trim (явный, репортит dropped) до fit; `media.fits()` config-based | ffmpeg | ✅ Phase 3 |
| `fetch_media(url, max_height, prepare)` | URL/YouTube → workspace-файл (SSRF-guard, quality-cap, fit-check→prepare) | yt-dlp (+ffmpeg) | ✅ Phase 4 (unit; live по одобрению) |
| `list_models()` | живой каталог flash/pro generateContent (id, preview, limits) — выбор `model` без 404-перебора | httpx (чисто) | ✅ #202 (live models.list; + analyze 404→`available_models`) |

Общая **рабочая папка** на медиа-сессию: видео(симлинк)+ответы Gemini+извлечённые кадры рядом.

## Принятые решения дизайна

- **Модели (ЭВАЛ 2026-06-19, `reference/*_eval.py`, на реальной RU-речи 45с+36с):** ВСЕ актуальные flash/pro-модели Gemini **СЛЫШАТ аудио** (в т.ч. встроенное в видео) и локализуют таймкоды ~±0.5с. Прежнее «слышит только 2.5-flash» — **артефакт учёта токенов**, НЕ глухота: 2.5-семья выносит отдельную `AUDIO`-модальность (1440/1152 токенов), а 3.x **сворачивают аудио в `VIDEO`-токены** (`audio_tokens=0`, но транскрибируют верно). Глухих моделей НЕТ → audio-deaf warning **удалён** (`audio_tokens` ненадёжен как сигнал глухоты). 2.0-семья снята upstream (404). Дефолты (`DEFAULT_MODELS` в `server.py`) — **стабильные, НЕ preview**: analyze+locate = `gemini-2.5-flash` (слышит, чистый вывод без преамбулы, осмысленный `audio_tokens`). **Locate-таймкоды: НИ У КОГО нет надёжного edge** — мнимое суб-секундное преимущество `gemini-2.5-flash-lite` (MAE 0.1 на orig, n=1) ОПРОВЕРГНУТО на n=3 (`locate_revalidate.py`): это артефакт выравнивания GT (десятичные ответы помогают при GT на X.5, вредят при целом GT — на video C lite дал 0.42 против 0.00 у целочисленных). Locate в принципе ±0.5-1с; чинит **±окно extract_frame**, не выбор модели. ⚠️ Синтетик-вспышки 0.5с ненадёжны для тонкой timecode-оценки (теряются при ~1fps-семплинге Gemini — video B сломал все модели MAE 5-10); для эвалов брать длинные вспышки / реальный контент.
- **`mediaResolution: MEDIA_RESOLUTION_HIGH`** — даёт +качество (goat-эвал +36%), принят API. ⚠️ 2.0-семья на него 404'ит (но она и так снята).
- **Tool params (analyze_media) — РЕАЛИЗОВАНО:** `detail` (brief/normal/full → {512/2048/8192} maxOutputTokens) + `max_tokens` override (`effective = max_tokens or MAP[detail]`, дефолт normal); `model` override; `language` (мягкий стир, дефолт = язык вопроса, с carve-out «при транскрипции сохраняй язык оригинала»); `fps` (`videoMetadata.fps`); `session_id` — RESERVED (v1 stateless). (Спайк хардкодил 900 — обрезалось.)
- **Multi-turn:** v1 stateless re-query (переиспользует диск-кэш fileUri). Stateful (Gemini держит историю) — `session_id` зарезервирован в сигнатуре + ключ `sessions` в `cache.json`, без миграции включится в Phase 2+.
- **fps** (`videoMetadata.fps`) — рычаг точности таймкодов; в эвале default fps хватало (fps=5 НЕ улучшил, у 2.5-flash-lite даже ухудшил 0.1→0.5).
- **⚠️ Thinking-модели жрут output-бюджет:** 2.5-pro / 3.x-preview / 3.5-flash при низком `max_tokens` (40-80) возвращают ПУСТО (thinking съел бюджет). Для эвалов/коротких ответов давать ≥512-2048 токенов.
- **fetch_media (Phase 4) — 4 граблі:** (1) **SSRF-guard ОБЯЗАТЕЛЕН** (`agent/fetch.py` `validate_url`): yt-dlp качает что угодно (`file://`, localhost, `169.254.169.254` cloud-metadata) → до загрузки: только http(s) + резолв хоста, блок private/loopback/link-local/reserved (`is_blocked_ip`, fail-closed). Валидация ПЕРВЫМ шагом, до tool-check. ⚠️ Покрыт только INITIAL host — **redirect (302→internal) и DNS-rebind НЕ покрыты** (yt-dlp сам резолвит+следует 30x; реальный фикс = сетевая изоляция/прокси, осознанно вне scope). Claim в docstring сужен честно (review F1, личный тул + доверенные URL; технический фикс отклонён Крисом по proportionality). (2) **Качество на ЗАГРУЗКЕ**, не после: yt-dlp format-cap `height<=max_height` (720p деф) — не тащить 4K/часы; fit-check→prepare лишь backstop. (3) **Graceful**: нет yt-dlp/ffmpeg → структурная ошибка (`has_tool`). (4) **temp→hash→workspace**: sha неизвестен до загрузки → temp-dir → hash готового → move в `workspace/<sha8>/source<ext>` → единый путь. **Реальную загрузку гонять ТОЛЬКО по явному указанию Криса + benign-URL** (outward-facing); unit мокают yt-dlp.
- **prepare_media + fit-check (Phase 3):** лимиты Gemini **НЕ хардкодить в логику** — дрейфуют как model catalog. `media.fits()` берёт лимит из env `JAINE_MEDIA_MAX_FILE_MB` (дефолт 2000 = Files API ~2GB); реактивный backstop = структурная API-ошибка analyze_media если оценка устарела. Порядок митигаций: «слишком длинное» → сначала **fps** (param analyze, без потери контента), потом **trim** (last resort, ЯВНЫЕ start/end + репорт `dropped`, никогда не молча); «слишком большое» → **compress** (downscale resolution/bitrate, контент цел, падает fidelity). `fits()` экспонирован для переиспользования в Phase 4 (fetch→fit-check→auto-prepare, DRY). E2E: 62MB hevc → 4.11MB @720p, fits.
- **Central tool log (заменил per-workspace `answers.jsonl`):** все 4 тула пишут JSONL-строку в `~/.claude/logs/jaine-media.jsonl` (стабильный путь, переживает cache-wipe; env `JAINE_MEDIA_LOG`; ротация stdlib `RotatingFileHandler` 5MB×3; best-effort) — `agent/tool_log.py` `log_tool(tool, ok, *, digest=sha8, **fields)`. Хранит полный `answer` (анализ дог-фуда нужен текст). Формат выбран survey'ем (хуки=pipe-kv, но media-текст с `\|`/`\n` ломает pipe-kv → JSONL парсится `json.loads`). **conftest autouse изолирует тест-лог в tmp** (иначе pytest сорит в реальный лог). Анализ: `jq 'select(.tool=="analyze_media" and .ok==false)'`.
- **finish_reason детект (дог-фуд-баг b09485a0):** `_parse_response` → `(text, audio, finish_reason)`. Усечённый (`MAX_TOKENS`), SAFETY-блок (нет кандидата → `BLOCKED:<reason>` из `promptFeedback`), пустой (`EMPTY`) ответ **больше НЕ молчаливый success** — result несёт `finish_reason` + `complete:bool` + `note` с подсказкой. Ловит 3 боли одним сигналом (вкл. старую гочу «thinking-модель при низком max_tokens возвращает ПУСТО» = `MAX_TOKENS`). Логируется.
- **Multi-video (#202, нативный):** `analyze_media(paths=[a,b,...])` — несколько клипов = несколько file-частей в ОДИН Gemini-запрос, **полное разрешение каждого** (лучше ffmpeg-hstack, который ужимает оба). `_collect_targets` валидирует (пусто/>10/несуществующий → структурная ошибка); back-compat: одиночный `path` даёт прежнюю форму result. Модуль `agent.paths` импортируется как `agent_paths` (чтобы публичный параметр `paths=[...]` его НЕ затенял — review-фикс); `analyze_media` берёт возврат `workspace.prepare` (он и каталог создаёт, и симлинкует), не `agent_paths.workspace_dir`. Live: различил 2 клипа (текст+голос vs цветные экраны).
- **Multi-turn беседа (#206, stateless `history`-param):** `analyze_media(history=[{role,text,paths?}], question)` — продолжать обсуждать медиа накопительно, подмешивать РАЗНЫЕ видео по ходу. **КОНСЕНСУС 4 источников (consult-панель codex+grok+agy, офиц. Gemini docs, goat `video_analyze`, agy на 3.5-flash): Gemini multi-turn STATELESS/client-side — историю держит ВЫЗЫВАЮЩИЙ (Claude), сервер только реплеит её в `contents`. Server-side `session_id`-state = АНТИ-ПАТТЕРН** (десинхрон с историей Claude / concurrency / opacity — отвергнут единодушно; `session_id`-параметр оставлен мёртвым, докстринг честно говорит «use history»). `_build_request_body(history=...)` строит user/model turns, media едет в том turn где видео добавлено (`_resolve_history` резолвит `paths`→FileRef через `get_or_upload`, **re-upload протухших 48h fileUri**). Follow-up без нового файла (`continued:true`) — медиа в истории. Live: turn2 помнит «Привет мир» из turn1. Token-accumulation (видео ~250-300 ток/сек) — забота вызывающего (fps↓/сжатие).
- **mediaResolution — эмпирика (reference/media-resolution-tokens.md, замер 2026-06-19):** docs Google про 2.5 ОПРОВЕРГНУТЫ. Реально: 2.5 `default=MEDIUM=HIGH` (~263 ток/кадр), только `LOW` экономит (~71); 3.x ИНВЕРТИРОВАН — `default=LOW=MEDIUM` (~85), `HIGH` ~289 (3.4×, для OCR). Так что HIGH на 2.5 «бесплатен», на 3.x — дорог. Модель = главный рычаг скорости: 3.5-flash default ≈3× дешевле/быстрее 2.5-flash. **«goat был быстрее» = короткие клипы, НЕ mediaResolution.** При смене дефолт-модели — перезамерить `probe_mediaresolution.py`.

## 🚧 ГРАБЛИ (каждая проверена живьём)

1. **Кэш — на ДИСКЕ (✅ Phase 1.1).** goat-версия держала `_URI_CACHE` в памяти; MCP stdio-сервер умирает с сессией. Переписано: `<data_dir>/cache.json` (`sha256 → {uri,name,mime_type,expires_at,state}`), lazy-load + atomic `os.replace`, резерв ключа `sessions` под будущий stateful. data_dir резолвится в `server/agent/paths.py` (`JAINE_MEDIA_DATA_DIR` → `CLAUDE_PLUGIN_DATA` → `.aitemp/jaine-media`). Переживает рестарт (48h окно).
2. **`${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` резолвятся ТОЛЬКО для УСТАНОВЛЕННОГО плагина**, НЕ для MCP, добавленного через `claude mcp add` (local config). → Для dev-итераций используем local MCP config с **АБСОЛЮТНЫМИ путями** к `plugins/jaine-media/server`. `.mcp.json` в плагине (с `${CLAUDE_PLUGIN_ROOT}`) — для будущей установки через marketplace.
3. **`${CLAUDE_PLUGIN_ROOT}` в поле `command` бажен** (CC issue #9354) — использовать только в `args`.
4. **stdout — ТОЛЬКО JSON-RPC.** Любой `print`/лог в stdout ломает MCP-протокол молча. Все логи → stderr.
5. **Anti-npx (твой инцидент: 38 серверов 3.8ГБ от per-session spawn).** Запуск bundled: `uv run --frozen --offline server.py`. Депы ставить ОДИН раз через SessionStart-hook в `${CLAUDE_PLUGIN_DATA}/.venv` (committed `uv.lock`), launch без сети. НЕ `uvx`/`npx` (re-download, version drift, cache bloat 150ГБ, supply-chain). Наш video-сервер idle-дёшев (нет upstream-соединения, в отличие от channel-плагинов) → proliferation не страшна.
6. **Free Gemini-ключа ДОСТАТОЧНО** (video understanding + Files API). Но free rate-limit (429/503) на burst → при многих запросах **sequential + retry/backoff**. `fps=5` тяжелее → throttle быстрее. Ротация по `GEMINI_FREE_KEYS` (4 ключа) для прода.
7. **env-passthrough ключа:** `.mcp.json` → `"env": {"GEMINI_API_KEY": "${GEMINI_API_KEY}"}`. CC разворачивает из shell при старте (у Криса secrets.env загружен в shell через conf.d). Ключ НЕ писать в конфиг открытым.
8. **`~/.jaine/` защищён глобальным PreToolUse-хуком (нельзя писать программно). `./.jaine/` (project-local) и `.aitemp/` — СВОБОДНЫ.** Рабочую папку плагина дефолтить в `${CLAUDE_PLUGIN_DATA}/workspace/<sha>/`; локально — `.aitemp/`. НЕ `~/.jaine`.
9. **ffmpeg без drawtext** (homebrew-сборка без libfreetype) — для синтетических тест-видео используй `color`+`concat` (смена цвета), НЕ `drawtext`. ffmpeg 8.1.1 / ffprobe / yt-dlp — verified на машине (/opt/homebrew, ~/.local/bin).
10. **extraction `gemini_files.py` тривиален:** зависимость только `httpx` + 2 хелпера из адаптера (`DEFAULT_GEMINI_BASE_URL`, `is_native_gemini_base_url`) → они в `server/agent/gemini_native_adapter.py` (минимальный stub, не весь goat-адаптер).
11. **Gemini недетерминирован в языке ответа** — форсить «ответь на русском» в промпте, если нужен RU.
12. **MCP reload:** `/reload-plugins` подхватывает изменения MCP-сервера в живую сессию (полный релогин не нужен). Но изменения кода сервера требуют переподключения сервера (рестарт процесса).
13. **jaine-plugins = worktree-доктрина.** Плагин на ветке `jaine-media/main` (worktree), НЕ на main-хабе. `create-plugin.sh` ПУШИТ к A3IO + триггерит CI (outward!) — публиковать только по явной команде через `publish-plugin.sh`.
14. **git-hook: прямые коммиты в `jaine-media/main` ЗАПРЕЩЕНЫ** (PR-only doctrine). Коммить в feature-ветку `jaine-media/feat/*` (хук сам предлагает manual `git checkout -b jaine-media/feat/...`). Текущая работа на `jaine-media/feat/initial-scaffold` (локально, без push). НЕ обходить `--no-verify` (это safeguard, не препятствие). Мёрдж feat→main — через PR (`create-pr.sh`) при созревании/публикации.

## Спайк-результаты (де-рисковано, см. `reference/`)

- **Gemini ядро:** upload работает, аудио слышится (1440 audio-токенов, дословная RU-транскрипция), content-кэш reuse (cached_before false→true по sha256, разные пути → тот же fileUri), HIGH ок, free-ключ ок.
- **MCP обвязка:** bundled uv `--frozen --offline` стартует (no npx), handshake ок, env-passthrough ок.
- **Живой E2E:** `analyze_media` вызван из реального Claude Code на двух видео (sample + «Спор трёх людей.MP4» — кириллица+пробелы+заглавный `.MP4` в пути отработали).
- **Таймкоды:** `reference/timecode_eval.py` + `timecode_test.mp4` (color-flash GT). 3-flash-preview 4/4 ±0.5с default; 2.5-flash 3/4 default, 4/4 с fps=5.
- **Концепт-демо:** `reference/demo.html` (визуальное объяснение потока).

## Открытые вопросы (решить при сборке)

1. ✅ РЕШЕНО — глухих нет, все модели слышат; analyze+locate = стабильный `gemini-2.5-flash` (locate доуточнить в Phase 2). См. «Принятые решения → Модели».
2. ✅ РЕШЕНО — `{512/2048/8192}`, дефолт normal.
3. ✅ РЕШЕНО — `session_id` зарезервирован в сигнатуре; v1 stateless; ключ `sessions` в `cache.json` под будущее.
4. ✅ РЕШЕНО — опц. `language`, дефолт = язык вопроса (мягкий стир + carve-out для транскриптов).
5. 🔲 Phase 4 — `fetch_media` ОТДЕЛЬНЫМ tool'ом (URL→локальный путь→analyze); решение Криса. При сборке: fit-check «влезает в Gemini» → авто-вызов `prepare_media`.
6. ✅ РЕШЕНО — `<data_dir>/workspace/<sha8>/`: `source<ext>` симлинк (не копия) + `frames/` (extract_frame). Лог Q→A переехал в central tool log (`~/.claude/logs/jaine-media.jsonl`, см. «Принятые решения»), per-workspace `answers.jsonl` удалён.

## Dev workflow

- **Текущий код:** `server/server.py` (live analyze_media), `server/agent/` (gemini_files + stub-adapter), `server/pyproject.toml` + `uv.lock`.
- **Venv:** `uv sync` в `server/` (или SessionStart bootstrap для прода).
- **Запуск/проба:** local MCP config (`claude mcp add jaine-media` с абсолютными путями) → `/reload-plugins` → tool `mcp__jaine-media__analyze_media`.
- **Тесты:** TDD при доведении (стек: pytest). Тест-видео-генерация — `color`+`concat` (не drawtext).
- **Публикация:** ТОЛЬКО по явной команде → `./scripts/publish-plugin.sh jaine-media` (push A3IO + CI).
