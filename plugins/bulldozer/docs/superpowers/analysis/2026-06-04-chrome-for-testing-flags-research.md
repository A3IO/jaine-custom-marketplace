# Как убрать плашку «unsupported command-line flag» для JAINE Browser lanes

## 0. Контекст и ключевое ограничение (прочитать первым)

JAINE Browser = **отдельный instance того же самого бандла** `/Applications/Google Chrome.app`, что и личный daily Chrome Криса (verified: `launch.sh` строка 69 и `tests/conftest.py` строка 48 указывают на один и тот же путь `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`). Изоляция между «daily» и «lane» сейчас держится **только** на разных `--user-data-dir` и `--remote-debugging-port`, НЕ на разных бинарниках.

**Из этого следует жёсткое ограничение:** Chrome managed policies применяются **per-application-bundle** (`com.google.Chrome`) — подтверждено независимо исходниками Chromium (`policy_loader_mac.mm` читает политики только из preference-домена, совпадающего с bundle id; verdict: confirmed). Значит **любая политика, выставленная на `com.google.Chrome`, заденет ОБА instance** — и тестовый, и личный daily-браузер. Это центральный факт для ранжирования ниже: вариант через policy "грязный" именно потому, что не изолируется по bundle, пока daily и lane — один бандл.

`launch.sh` уже параметризует `CHROME_BIN` (env, строка 69: `CHROME_BIN="${CHROME_BIN:-/Applications/...}"`), `CDP_PORT`, profile dir, headless. Подмена бинарника **только для lanes** (с нетронутым daily Chrome) — это одна env-переменная. Это делает варианты с отдельным браузером тривиальными и одновременно решает проблему policy-scope.

---

## 1. Прямой ответ: варианты убрать плашку, ранжированные

### Вариант A — Отдельный бинарник Chrome for Testing (РЕКОМЕНДУЕТСЯ)

**Что делает:** CfT — это отдельный бандл Chrome (≈170 МБ на macOS, свой app bundle, отдельный bundle id), без auto-update, версионируемый под тестирование. Запускается через `CHROME_BIN`. Поскольку это **другой бандл и другой бинарник**, любые testing-флаги и любая policy, которую вы захотите к нему применить, **физически не могут задеть личный Chrome** — проблема per-bundle scope из §0 исчезает.

**Scope:** личный Chrome не затронут (другой бандл). ✅

**Про саму плашку — честно:** официальная документация CfT **не утверждает**, что инфобар «unsupported command-line flag» в CfT подавляется иначе, чем в обычном Chrome (это явный dead-end в исследовании — «не подтверждено независимо», что CfT сам по себе прячет плашку). НО: плашка глушится флагом `--enable-automation` (см. вариант B ниже — это подтверждено исходниками), а CfT — штатное место, где `--enable-automation` и подобные testing-флаги ожидаемы и не выглядят чужеродно. Комбинация **CfT + `--enable-automation`** даёт чистый старт без плашки и без риска для daily Chrome.

**Версии/совместимость:** macOS `mac-arm64` поддерживается официально (наряду с mac-x64/linux64/win). CDP полностью поддержан (CfT — Blink-based). `--use-fake-ui-for-media-stream` поддержан. Per-commit билды доступны с марта 2025.

**Команды/шаги:**
```bash
# установка конкретной версии (pin под себя; stable/beta/dev/canary или точная версия)
npx @puppeteer/browsers install chrome@stable
# вернёт путь вида:
#   chrome/mac_arm-<ver>/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing

# запуск lane через уже готовую параметризацию:
CHROME_BIN="/path/.../Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
CDP_PORT=9334 LOOK_PROFILE_DIR=/0/.jaine/.browser/profile-vad \
./skills/look/scripts/launch.sh --headful <url>
```

**Статус подтверждения:** существование, arm64, CDP, fake-media, отсутствие auto-update — confirmed (официальные источники). «CfT сам прячет плашку» — **не подтверждено независимо** (dead-end); полагаться надо на `--enable-automation`.

---

### Вариант B — Флаг `--enable-automation` (самый дешёвый; работает на ЛЮБОМ бинарнике)

**Что делает:** `--enable-automation` — официальный catch-all для автоматизации. Подтверждено независимым источником (chrome-launcher `chrome-flags-for-tools.md`, который трассирует эффект в исходники Chromium): он **подавляет ровно 4 инфобара, включая `ShowBadFlagsPrompt`** — а это и есть плашка «You are using an unsupported command-line flag». Дословно: *"avoids showing these 4 infobars: ShowBadFlagsPrompt, GoogleApiKeysInfoBarDelegate, ObsoleteSystemInfoBarDelegate, LacrosButterBar"*.

**Побочки:** ставит `navigator.webdriver=true`, глушит password-save UI, extension-notification bubbles, отключает infobar-анимации, не авто-перезагружает на сетевых ошибках. Для VAD/voice-теста это безвредно. (Если `navigator.webdriver=true` мешает тестируемому коду — это единственный реальный минус.)

**Scope:** флаг — per-process, НЕ per-bundle. Передаётся только в argv конкретного запуска lane → **личный Chrome не затронут вообще**, даже на одном бандле. ✅ Это его главное преимущество перед policy.

**Команды/шаги:** добавить в массив `CHROME_ARGV` в `launch.sh` (строки 186-200), желательно под gate тестовой lane (по аналогии с тем, как там уже сделан `--insecure`/`--disable-web-security` — строки 204-206):
```bash
CHROME_ARGV+=(--enable-automation)
```

**ВАЖНАЯ ПОПРАВКА к исходному тезису задачи:** в raw-findings фигурировал тезис, что плашку прячет `--test-type`. Это **refuted** перекрёстной проверкой: `ShowBadFlagsPrompt` подавляет `--enable-automation`, а НЕ `--test-type`. В исходниках `bad_flags_prompt.cc` нет guard'а на `kTestType` для desktop — только `kEnableAutomation`. `--test-type` делает другое (не создаёт app-stubs на Mac, влияет на exit codes, отключает часть startup-сервисов), и его описание «2014 version of --enable-automation» — само по себе **не подтверждено независимо** (все источники сводятся к одному chrome-launcher doc). **Не используйте `--test-type` ради плашки — используйте `--enable-automation`.**

**Статус подтверждения:** `--enable-automation` прячет `ShowBadFlagsPrompt` — confirmed (refuted-проверка прямо это установила). `--test-type` прячет плашку — **refuted**.

---

### Вариант C — Policy `CommandLineFlagSecurityWarningsEnabled=Disabled` (НЕ рекомендуется при одном бандле)

**Что делает:** политика, выставленная в `Disabled`, **убирает security-warnings при запуске с «опасными» флагами**, включая искомую плашку. Семантика confirmed двумя независимыми источниками (Microsoft Edge реализует ту же Chromium-политику под тем же именем с той же семантикой; ADMX.help независимо документирует то же для Chrome).

**Почему НЕ рекомендуется в текущей конфигурации:** scope — **per-bundle `com.google.Chrome`** (confirmed исходниками Chromium). Пока daily и lane = один бандл, политика **заденет личный daily Chrome тоже**. Это прямо противоречит цели «daily Chrome untouched».

**Дополнительное ограничение (refuted-уточнение):** на macOS политика действует **только на управляемых устройствах** — через MDM, MCX-domain-join, **или enrollment в Chrome Enterprise Core**. Исходный тезис «только MDM или MCX» **refuted/неполон** — официальная страница Google добавляет третий путь (Chrome Enterprise Core), он не требует MDM-инфраструктуры. На **неуправляемом** консьюмерском Mac политика, выставленная через `defaults write com.google.Chrome ...`, по доктрине Google/Chromium — **testing-only, recommended-level**, и **не может форсить mandatory-политику** (confirmed: `defaults write` на macOS — только testing-метод, не production-эквивалент MDM-plist). Требуется рестарт браузера (не применяется динамически).

**Когда C становится приемлемым:** только в связке с вариантом A/D — т.е. когда lane = ОТДЕЛЬНЫЙ бандл (CfT `com.google.chrome.for.testing`, ungoogled `org.chromium.Chromium`, и т.п.). Тогда `defaults write <bundle-id-тестового-браузера> CommandLineFlagSecurityWarningsEnabled -bool false` затронет только его. Но в этом случае проще обойтись `--enable-automation` (B) и не возиться с managed-prefs вовсе.

**Команды (для справки, если когда-нибудь lane станет отдельным бандлом):**
```bash
# НЕ делать на com.google.Chrome — заденет daily!
defaults write <test-bundle-id> CommandLineFlagSecurityWarningsEnabled -bool false
# проверка применения:
#   открыть chrome://policy в этом браузере, нажать "Reload policies"
```

**Статус подтверждения:** Disabled убирает плашку — confirmed. «только MDM/MCX» — **refuted** (есть третий путь Chrome Enterprise Core). per-bundle scope — confirmed. `defaults write` как testing-only — confirmed.

---

### Вариант D — ungoogled-chromium / собственный билд

**Что делает:** отдельный браузер (другой бандл `org.chromium.Chromium`), prebuilt arm64 доступен (Homebrew cask `ungoogled-chromium`, notarized Apple Developer ID как минимум до 2026-10-14, активно поддерживается).

**Scope:** другой бандл → личный Chrome не затронут. ✅

**Минусы конкретно под нашу цель:**
- ungoogled-chromium **сам по себе плашку не убирает** — наоборот, есть подтверждённый баг: он показывает «unsupported flag» предупреждение **даже для поддерживаемых флагов** (например `--enable-blink-features=MiddleClickAutoscroll`). То есть проблема плашки тут не решена «из коробки» — всё равно нужен `--enable-automation`.
- Высокий maintenance-burden: релизы идут в темпе upstream Chromium (новый milestone каждые 4 недели, с сентября 2026 — каждые 2 недели). Это постоянное обновление pin'а.
- CDP-паритет с обычным Chromium **не задокументирован явно** (вероятно идентичен как Blink-based, но — «не подтверждено независимо»).

**Собственный билд** (удалить код плашки) — официально единственный «гарантированный» способ убрать именно это предупреждение по словам Chromium-форума, но это абсурдный объём работы под данную задачу. Не рекомендуется.

**Статус подтверждения:** arm64 prebuilt + notarization — confirmed. Плашка для поддерживаемых флагов — confirmed (это баг ungoogled, аргумент ПРОТИВ). CDP-паритет — **не подтверждено независимо**.

---

### Чего делать НЕ надо (тупики)

- **`--disable-infobars`** — для обычного Chrome удалён ещё в **январе 2018** (исходный тезис «May 2019» — **refuted**, реальный commit Peter Kasting `d869ab3`). Для standard Chrome не работает. (Нюанс: в июне 2024 его переподключили для headless/CfT со scoped-поведением «прячет инфобары без кнопок» — commit `70c0208` — но плашку bad-flags это не покрывает целево; не закладывайтесь.)
- **`--test-type`** ради плашки — см. вариант B, refuted.
- **`--silent-debugger-extension-api`** — прячет другой инфобар (про `chrome.debugger` attach), не bad-flags.

---

## 2. Конкретная рекомендация для JAINE Browser lanes (пошагово)

Учитывая, что `launch.sh` уже параметризует `CHROME_BIN`, и что цель — daily Chrome нетронут:

**Рекомендуемая комбинация: CfT (отдельный бинарник) + `--enable-automation` (флаг).** CfT снимает per-bundle риск policy навсегда; `--enable-automation` гарантированно глушит плашку (это подтверждённый механизм, в отличие от «CfT сам прячет»). Policy (вариант C) не трогаем вовсе — она лишняя и опасна на общем бандле.

Если ставить CfT не хочется прямо сейчас — **минимальный шаг — только `--enable-automation` на текущем бандле**: флаг per-process, daily Chrome не заденет. Это работает уже сегодня, без нового бинарника. CfT добавить можно позже как чистоту изоляции.

**Шаги:**

1. **(Опционально, но рекомендуется) Поставить CfT под arm64:**
   ```bash
   npx @puppeteer/browsers install chrome@stable
   # запомнить путь к "Google Chrome for Testing.app/.../Google Chrome for Testing"
   ```
   Pin версию (записать в конфиг lane), чтобы не было дрейфа между прогонами — это и есть главный смысл CfT.

2. **Добавить `--enable-automation` в `launch.sh`**, под gate для тестовых lane (НЕ для daily 9333), по образцу уже существующего `--insecure`-gate (строки 154-179, 204-206). Daily-браузер (порт 9333) оставить без флага, чтобы `navigator.webdriver` и подавление password-UI не меняли поведение «человек смотрит». Например, добавить после блока `--insecure` (≈строка 206):
   ```bash
   if (( AUTOMATION )); then
     CHROME_ARGV+=(--enable-automation)
   fi
   ```
   где `AUTOMATION` выводится из нового `--automation`/`LOOK_AUTOMATION` env (по аналогии с `LOOK_HEADLESS`/`LOOK_INSECURE`), и так же, как `--insecure`, запрещён на порту 9333 / daily-профиле.

3. **Запуск VAD-lane** (headful обязателен — см. §3):
   ```bash
   CHROME_BIN="/path/.../Google Chrome for Testing" \
   CDP_PORT=9334 \
   LOOK_PROFILE_DIR=/0/.jaine/.browser/profile-vad \
   LOOK_AUTOMATION=1 \
   ./skills/look/scripts/launch.sh --headful <url>
   ```
   (Если CfT не ставили — просто опустить `CHROME_BIN`, всё остальное идентично. Флаг `--use-fake-ui-for-media-stream` — отдельно, см. §3, его в текущем argv НЕТ.)

4. **Проверить, что плашка ушла и daily не затронут:**
   - в lane-браузере убедиться, что инфобар «unsupported command-line flag» не появляется при старте;
   - открыть личный daily Chrome (порт 9333) и убедиться, что его поведение не изменилось (флаг туда не передаётся; если выбрали путь БЕЗ policy — гарантировано by construction);
   - при желании проверить флаги текущего процесса: `chrome://version` → строка Command Line.

5. **Тесты:** по доктрине плагина (`CLAUDE.md` → "Adding new commands — MANDATORY", и существующий `LOOK_DRY_RUN`) — добавить dry-run-проверку, что новый gate кладёт `--enable-automation` в `CHROME_ARGV` для lane и НЕ кладёт для порта 9333. `LOOK_DRY_RUN=1` печатает весь argv (строки 216-234) — это готовый seam для unit-теста, Chrome не запускается.

**Замечание о текущем состоянии vs план:** в действующем `launch.sh` **нет** `--use-fake-ui-for-media-stream` и нет gate для automation — это всё нужно добавить. Описанная в задаче VAD-lane с fake-media — пока **планируемая**, не отгруженная (grep по `cdp.py`/`launch.sh`/`conftest.py` подтверждает: флаги fake-media в коде отсутствуют).

---

## 3. Про `--use-fake-ui-for-media-stream` / `--use-fake-device-for-media-stream`: легитимность, безопасность, headless-нюанс

**Легитимность:** оба флага — штатные testing-инструменты, задокументированы в официальных WebRTC/Chromium-ресурсах именно под автоматические тесты без железа. `--use-fake-ui-for-media-stream` автоматически грантит `getUserMedia` без промпта; `--use-fake-device-for-media-stream` подаёт синтетический поток вместо живой камеры/микрофона (по умолчанию «green pac-man» видео и «boop boop» аудио). Можно и отклонять: `--use-fake-ui-for-media-stream=deny`. (confirmed, официальные источники)

**Безопасность — почему важно НЕ ставить на daily Chrome:** эти флаги **обходят обычные permission-промпты** на доступ к камере/микрофону. На повседневном браузере это privacy-риск (сайт получает медиа-доступ без ведома пользователя). Это ещё один аргумент за изоляцию VAD-lane от daily Chrome (отдельный профиль + флаг только в argv lane). Существует и подтверждённый запрос на user-visible warning именно для `--use-fake-ui-for-media-stream` — т.е. на daily-браузере он сам по себе может породить ту самую «unsupported flag» плашку (что снова решается `--enable-automation`, §1B). (confirmed)

**Headless-нюанс (критично для VAD — подтверждает требование задачи о headful):**
- `--use-fake-ui-for-media-stream` **не работает в headless** — контроль permission для медиа в headless-режиме не поддерживается (флаг работает в windowed/headful, в headless падает). (confirmed, официальный headless-dev тред)
- Это прямо обосновывает выбранный для VAD-lane **headful** режим. Дизайн-спека плагина (`2026-06-03-look-isolation-v2-design.md` §2) фиксирует тот же факт под другим углом: headless-браузер **не имеет output-устройства** → «functional verification yes, audible no». То есть для VAD нужен headful и потому, что без него fake-ui не активируется, и потому, что нет аудио-выхода.
- Если в каком-то сценарии всё же headless: подача медиа возможна только через `--use-fake-device-for-media-stream` + файлы `--use-file-for-fake-audio-capture` (WAV) / `--use-file-for-fake-video-capture` (Y4M); по умолчанию там синтетический «boop boop». Но permission-обход (`fake-ui`) в headless не работает — промпт нечем закрыть. Для VAD-теста с реальным media stream это означает: **остаёмся на headful** (как и требует условие задачи). (confirmed)
- Дополнительно: `--use-fake-device-for-media-stream` **не работает для screen capture** — только камера/микрофон. Для VAD (микрофон) это норма. (confirmed)

**Вывод по §3:** оба флага легитимны и безопасны *в изолированной lane*; на daily Chrome их ставить нельзя (privacy + плашка). Для VAD обязателен **headful** — иначе `--use-fake-ui-for-media-stream` не активируется И нет аудио-выхода. Эти флаги в текущем `launch.sh` отсутствуют — их надо добавить тем же gate-механизмом, что и `--enable-automation`/`--insecure`.

---

## 4. Источники

Подтверждено как минимум двумя независимыми источниками (confirmed) либо явно помечено иначе.

**Policy `CommandLineFlagSecurityWarningsEnabled`:**
- https://chromeenterprise.google/policies/command-line-flag-security-warnings-enabled/ (official; здесь же третий путь Chrome Enterprise Core — основание для refuted «только MDM/MCX»)
- https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/commandlineflagsecuritywarningsenabled (independent confirm семантики через Edge, та же Chromium-политика)
- https://admx.help/?Category=Chrome&Policy=Google.Policies.Chrome::CommandLineFlagSecurityWarningsEnabled (independent confirm для Chrome)
- https://github.com/ProfileManifests/ProfileManifests/blob/master/Manifests/ManagedPreferencesApplications/com.google.Chrome.plist (manifest, подтверждает существование ключа)

**macOS policy mechanics + per-bundle scope:**
- https://chromium.googlesource.com/chromium/src/+/refs/heads/main/components/policy/core/common/policy_loader_mac.mm (исходник: per-bundle чтение политик — основа всего §0)
- https://www.chromium.org/administrators/mac-quick-start/ (`defaults write` как testing-only; chrome://policy для проверки)

**`--enable-automation` / `--test-type` / `--disable-infobars`:**
- https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md (independent: `--enable-automation` прячет ShowBadFlagsPrompt; refute `--test-type`)
- https://chromium.googlesource.com/chromium/src/+/d869ab3350d8ebd95222b4a47adf87ce3d3214b1 (commit: `--disable-infobars` удалён Jan 2018 — refute «May 2019»)
- https://chromium.googlesource.com/chromium/src/+/70c0208350f09289a71efe8770cfcbd6f5ca8f76 (commit: переподключение `--disable-infobars` для headless/CfT, июнь 2024)
- https://chromium.googlesource.com/chromium/src/+/refs/heads/main/content/public/common/content_switches.cc (`--test-type` всё ещё валиден; нейтральное описание)

**Chrome for Testing:**
- https://developer.chrome.com/blog/chrome-for-testing (official; отдельный бандл, no auto-update, per-commit с марта 2025)
- https://googlechromelabs.github.io/chrome-for-testing/ (mac-arm64 поддержан)
- https://www.npmjs.com/package/@puppeteer/browsers (установка `npx @puppeteer/browsers install chrome@stable`)
- *Dead-end / не подтверждено независимо:* нет официального источника, что CfT сам подавляет плашку bad-flags иначе обычного Chrome.

**ungoogled-chromium:**
- https://github.com/ungoogled-software/ungoogled-chromium-macos (arm64 prebuilt, notarized)
- https://formulae.brew.sh/cask/ungoogled-chromium (Homebrew cask, notarization до 2026-10-14)
- https://github.com/ungoogled-software/ungoogled-chromium/issues/3136 (баг: плашка для поддерживаемых флагов — аргумент против)
- *Не подтверждено независимо:* CDP-паритет ungoogled ↔ standard Chromium.

**fake-media флаги + headless:**
- https://webrtc.github.io/webrtc-org/testing/ (official; `--use-fake-ui-for-media-stream` грантит getUserMedia)
- https://bugs.chromium.org/p/chromium/issues/detail?id=489092 (official; вариант `=deny`)
- https://groups.google.com/a/chromium.org/g/headless-dev/c/OVXsRrZwGwM (official; fake-ui НЕ работает в headless)
- https://bugs.chromium.org/p/chromium/issues/detail?id=372693 (official; fake-device не для screen capture)
- https://www.daily.co/blog/how-to-make-a-headless-robot-to-test-webrtc-in-your-daily-app/ (синтетический поток; `--use-file-for-fake-audio-capture` WAV)

**Локальная инфра (verified в этой сессии):**
- `/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/skills/look/scripts/launch.sh` — `CHROME_BIN` (стр. 69), `CDP_PORT` (стр. 16), headful/headless (стр. 99-203), `CHROME_ARGV` + `--insecure`-gate как образец (стр. 154-206), `LOOK_DRY_RUN` (стр. 216-234). Флагов fake-media и automation в argv НЕТ.
- `/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/tests/conftest.py` — `CHROME` const (стр. 48), идентичен дефолту `CHROME_BIN` → один бандл с daily.
- `/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/docs/superpowers/specs/2026-06-03-look-isolation-v2-design.md` — §2: headless ⇒ нет output-устройства, "functional verification yes, audible no".

---
*Research: 17 агентов (6 search haiku + 10 verify sonnet + 1 synth opus), 2026-06-04. Все claims перекрёстно проверены; refuted/unverified помечены.*
