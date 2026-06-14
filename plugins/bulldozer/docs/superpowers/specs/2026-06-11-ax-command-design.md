# cdp.py `ax` — accessibility text snapshot + ref-bridge (issue #185)

**Дата:** 2026-06-11 · **Ветка:** `bulldozer/feat/185-149-188-ax-ref-bridge` · **Скоуп:** один PR

## 1. Проблема

При drive/look-верификации текстовый снапшот страницы — самый дешёвый канал ground
truth, но штатной команды нет. Обходные пути врут или дороги: `js
"document.body.innerText"` теряет роли и состояния (split-тест: 79.6% точности,
галлюцинации «enabled» на disabled-кнопке), `html` — на порядок дороже токенов,
скриншот — 1338–1600 токенов/страницу и OCR-промахи на длинных страницах (94.4%
sonnet, **74.1% haiku**). Запрос «посмотри через accessibility api» (сессия
b04aa3fe, рождение issue #185) штатно невыполним — это cdp.py wall по доктрине
drive SKILL.md.

## 2. Эмпирическая база

### 2.1 Протокол (живое репро, CfT 149.0.7827.54, эфемерные лейны)

- `Accessibility.getFullAXTree` работает **без** `Accessibility.enable`
  одношотово на свежем ws-соединении (enable несёт page-wide perf-cost — не используем).
- Дерево **строго per-frame**: контент iframe не входит в дерево родителя.
  Same-process дети (включая `srcdoc` и `about:blank`) перечислены в
  `Page.getFrameTree` и достаются `getFullAXTree({frameId})` на том же
  соединении. True OOPIF (другой site) в `getFrameTree` невидим и живёт
  отдельным таргетом `type=iframe` в `/json`.
- `queryAXTree` требует якорный nodeId и матчит имя exact — поэтому
  `assert --ax-role` отложен (см. §9).
- Реальные страницы — тысячи узлов (3310 на живой вкладке), шум ~70%
  (ignored / пустые generic / InlineTextBox / StaticText-дубли имени родителя).
- Параметр `depth` поддержан.

### 2.2 Сплит-тест формата (3 страницы × 4 условия, замороженные оракулы)

| Условие | sonnet (54 Q) | haiku (54 Q) | ~токены/страницу |
|---|---|---|---|
| ax Playwright-формат | **100%** | **96.3%** | 217–595 |
| ax упрощённый | 98.1% | 92.6% | 193–500 |
| innerText | 79.6% (галлюцинации состояний) | — | 102–161 |
| screenshot full-page | 94.4% (OCR-промахи) | 74.1% | 1338–1600 |

Выводы: формат — Playwright-parity (модели знают его из обучения; haiku парсит
его ЛУЧШЕ упрощённого); скриншот у дешёвых моделей разваливается, ax — нет;
маршрутизация «текст → ax, визуальное → screenshot» подтверждена данными.

### 2.3 Сплит-тест ref-bridge (живые клики, независимый грейдинг по журналу страницы)

| Метрика | R: `[ref]` + click-by-ref | S: CSS-селекторы |
|---|---|---|
| Задачи | **21/21** | 20/21 (клик не в ту кнопку + ложный рапорт успеха) |
| Tool-calls / время | 45 / 101с | 64 (+42%) / 156с (+54%) |
| Селекторные промахи 1-й попытки | 0 | у 3 из 6 агентов |

Вывод: мост — в v1. Селекторный путь работает в 95%, но даёт режим «тихой лжи
верификатора» и дороже по вызовам/времени. Прототип: `/0/.aitemp/ax-split-test/ax_bridge.py`.

### 2.4 Логи использования (bulldozer-look.log, 30 дней)

`js` 2155 · `navigate` 1820 · `screenshot` 811 · `console` 549 · `click` 500 ·
`wait` 310 · `assert` 292 · `fill` 75. Классификация js-выражений: **53% (1180
из 2236 за всю историю) — DOM-щупанье селекторами** (`querySelector`/
`getElementById`) — ровно та работа, которую устраняет ref-мост. Отсюда
ref-поверхность v1: click + fill + js + assert (интеракции со следами в
логах) + key (3 KeyboardEvent-хака = латентный спрос; fill не умеет сабмитить)
+ hover (следов нет, но hover JS-ом и не хакается — `:hover` синтетикой не
триггерится; hover-меню типичны для дашбордов нашего домена; конвейер готов)
+ scoped `ax --ref` (дешёвый re-check виджета в fix-verify цикле).
+ drag двумя лёгкими путями (mouse-серия для pointer-based библиотек +
JS-DataTransfer для нативного HTML5 — спайк 2.5; тяжёлый trusted-путь
Playwright не нужен).

### 2.5 Спайк ref-механизмов (живой лейн, test-page оракулы)

`fill --ref` (resolveNode → callFunctionOn: value + input/change события —
оракул-флаги страницы сработали), `js --ref` (биндинг `el`), предикат
видимости/actionable по ref (occluded-кнопка корректно распознана), протухание
(после reload `DOM.resolveNode` → «Node with given id does not belong to the
document» — чистый fail-loud сигнал). Вторым заходом: `key --ref Enter` после
`fill --ref` реально сабмитит форму (журнал keydown + submit-обработчик
сработали); `hover` через `Input.dispatchMouseEvent mouseMoved` честно
триггерит CSS `:hover` (tooltip display none→block) — с уроком: hover-таргеты
часто не имеют AX-ref (div без роли) → команде нужен и селекторный путь;
scoped-снапшот по backendDOMNodeId работает (рендер поддерева виджета).
Третьим заходом — drag: mouse-серия (down → 5×move(buttons=1) → up) дотащила
pointer-based элемент в зону (`__pointerDropped:true` — покрывает
dnd-kit/SortableJS/слайдеры/canvas) и предсказуемо НЕ родила `dragstart` на
нативном HTML5 DnD; JS-синтез `DragEvent` с настоящим `new DataTransfer()`
(`dragstart→dragenter→dragover→drop→dragend`) доставил payload через
`dataTransfer` (`__html5Dropped:"payload-42"`). Полный Playwright-путь
(DragManager: инжект + `Input.setInterceptDrags` + `dragIntercepted` +
`dispatchDragEvent`, подтверждено deepwiki) НЕ нужен — два лёгких пути
закрывают оба класса. Четвёртым заходом (триаж открытых issues): **AX-дерево
видит сквозь shadow DOM, включая `closed`-roots** (кнопки в open И closed
shadow попали в снапшот с ref, `click --ref` кликнул обе — журнал страницы
подтвердил; querySelector к closed-root не способен в принципе) — решает
класс A issue #172; **Esc mid-drag** (down → move → dispatchKeyEvent Escape →
release) отменил перетаскивание (`esc-cancel` в журнале) — кейс issue #149.
Пятым заходом (верификация чинимости поглощённых issues): **#188
репродуцирован байт-в-байт** (UnicodeEncodeError в print результата `cmd_js`,
суррогат от slice эмодзи) и фикс-проба `errors=replace` даёт exit 0; **#149 проверен на
ЖИВОМ приложении** (VRHOT TTS, cert-pin лейн на их self-signed :8081, их
собственный e2e-рецепт synthetic-WAV): decode РАБОТАЕТ на automation-лейне
(`trim.ready` за 706мс, canvas 606×100 — их «0-width в headless» блокер item 2
НЕ воспроизводится с `--window-size`), mouse-серия реально тянет границу
wavesurfer-региона (их индикатор `trim.info`: 3.0с → 2.0с), Esc mid-drag
доставлен достоверно (нативное закрытие `<dialog>`); **#172 honest negative:
canvas в shadow НЕ имеет AX-узла вовсе** (0 узлов в сыром дереве — canvas без
fallback-контента невидим для accessibility) — ax-пирсинг shadow работает
только для СЕМАНТИЧЕСКИХ элементов. Шестым заходом (панель №3, «третий путь»
вместо `--pierce-shadow`): ОДИН вызов `DOM.getDocument(depth:-1, pierce:true)`
даёт карту всех shadow-хостов с типом (`{17:'open', 19:'closed'}` на
двух-root фикстуре), хосты чисто коррелируются с AX-узлами по
backendDOMNodeId → рендер помечает их `[shadow=open|closed]`; пойман нюанс:
host — безымянный generic, дефолт-фильтр должен делать для него исключение.
12/12 механизмов + 3 issue-кейса эмпирически закрыты, 1 честно сужен.

### 2.6 Границы выборки (честно)

n=2–3 на ячейку, 3 синтетические страницы, оракулы автора, модели sonnet/haiku.
Достаточно для выбора формата и маршрутизации; финальная валидация — dogfood на
живом session-viewer (план, не спека).

## 3. Команда `ax`

```
cdp.py [--target SEL] ax [--max-nodes N] [--raw]
```

- **Канал:** websocket-only. Без websocket-client: `ERROR: ax requires
  websocket-client (CDP Accessibility domain)` на stderr + exit 1 (прецедент `html`).
- **Соединение:** ОДНО ws-соединение на весь вызов (прецедент `cmd_console`):
  `Page.getFrameTree` → для main и каждого same-process child-фрейма
  `Accessibility.getFullAXTree({frameId})` → один `DOM.getDocument(depth:-1,
  pierce:true)` для карты shadow-хостов (backendNodeId → open|closed; спайк
  §2.5 заход 6). Приём строго через `_recv_for_id` (id-matched; событийные
  кадры пропускаются). БЕЗ `Accessibility.enable`.
- **Таб:** `get_tab(TARGET)` ровно один раз (AST-гард `test_no_unpinned_tab_resolution`).
- **`--max-nodes N`:** лимит отрендеренных строк-узлов, дефолт 500, `0` = без лимита.
- **`--raw`:** отключает ВСЕ фильтры рендера (включая InlineTextBox); ignored-узлы
  рендерятся с маркером `[ignored]`. Диагностический эскейп: возвращает в т.ч.
  aria-hidden контент.

### 3.1 Грамматика вывода (ИНВАРИАНТ)

Первая строка stdout — verdict-заголовок (анти-усечение: harness режет хвост):

```
AX_OK nodes=<int> shown=<int> frames=<int>[ truncated=1]
```

регекс: `^AX_OK nodes=\d+ shown=\d+ frames=\d+( truncated=1)?$`.
`nodes` — сырые узлы по всем обойдённым фреймам; `shown` — отрендеренные
строки-узлы (text-строки включены; строки `frame:` и маркер усечения НЕ
считаются); `frames` — число обойдённых фреймов (main + same-process дети).

Далее тело. Узел:

```
- <role> ["<name>"][ [attr]…][ [ref=N]][: <value>]
```

- отступ 2 пробела на уровень глубины; текстовые узлы — `- text: <содержимое>`;
- `<name>`: whitespace схлопнут в одиночные пробелы (включая переводы строк),
  внутренние двойные кавычки заменяются одинарными, длина ≤ 200 символов
  (далее `…`). То же для `<value>`;
- атрибуты из properties: `[level=N]`, `[disabled]`, `[checked]`/`[checked=mixed]`,
  `[expanded]`, `[selected]`, `[required]`, `[readonly]`, `[multiline]`,
  `[invalid=…]`. Отсутствие `[disabled]` = элемент включён (зафиксировать в
  SKILL.md — урок haiku-сплита);
- `[ref=N]` — ВСЕГДА на интерактивных ролях (button, link, checkbox, textbox,
  combobox, option, menuitem, radio, switch, tab, slider, searchbox), где N =
  `backendDOMNodeId` узла. Паритет с Playwright MCP;
- `[shadow=open|closed]` — на узлах shadow-хостов (по карте из
  `DOM.getDocument(pierce)`, корреляция по backendDOMNodeId): агент видит
  маршрут прямо в снапшоте — семантика внутри уже в дереве; несемантическое
  (canvas) в open-host → `--js` с `.shadowRoot`, в closed-host → скриншот
  (панель №3, «третий путь» вместо `--pierce-shadow`);
- `RootWebArea` без имени не рендерится (дети с уровня 0); с именем — рендерится.

Child-фреймы — секциями после дерева main:

```

frame: <url>
- <дерево фрейма с отступа 0>
```

Усечение: при достижении лимита рендер останавливается, в теле — строка-маркер
`… [truncated: shown <M> of <total> nodes — re-run with --max-nodes 0]`,
в заголовке — `truncated=1`. Усечение может резать ветку посередине — задокументировано.

**stderr:** только tool-ошибки и WARN. OOPIF: если суммарное число узлов роли
`Iframe` по обойдённым фреймам больше числа обойдённых child-фреймов, печатается
`WARN: N out-of-process iframe(s) not included` (best-effort детект; срабатывание
проверено на true-OOPIF, несрабатывание — на srcdoc/about:blank/same-origin).
Exit-код: 0 при успехе (включая truncated и WARN), 1 при transport/CDP-ошибке.

### 3.2 Рендерер (чистая функция)

`_render_ax_tree(frame_node_lists, max_nodes, raw)` — без I/O, тестируется на
синтетике. Реконструкция: индекс по `nodeId`, корни = узлы без `parentId` (или с
parentId вне индекса), обход по `childIds` с защитой от циклов. Дефолт-фильтры
(порядок применения, пропуск узла НЕ скрывает его детей — они рендерятся на
уровне пропущенного):

1. `ignored == true`;
2. роль `InlineTextBox` (layout-дубликат StaticText);
3. роли `generic`/`none`/`presentation` без имени — ИСКЛЮЧЕНИЕ: узел-shadow-host
   выживает с маркером `[shadow=…]` (маркер и есть информация; иначе фильтр
   съел бы безымянный generic-host — спайк §2.5 заход 6);
4. `StaticText` с пустым именем или именем, равным имени родителя
   (имя родителя уже показано; риск съесть легитимный дубль признан — эскейп `--raw`).

## 4. Ref-мост: `click/fill/js/assert/key/hover/drag --ref` + scoped `ax --ref`

Общие правила всех ref-веток:

- N = `backendDOMNodeId` из снапшота `ax`; канал websocket-only (fail-loud);
- **протухание** (навигация/перерисовка): любой `DOM.resolveNode`/
  `scrollIntoViewIfNeeded`/`getBoxModel`-фейл по backendNodeId →
  `REF_STALE: ref N not resolvable — re-run ax for fresh refs` на **stdout**
  (единый verdict-маркер для всех ref-команд) + exit 1, действие не выполняется;
- для `click`/`assert`/`hover` `--ref` замещает единственный позиционник
  (SELECTOR): `--ref` + SELECTOR одновременно → usage-ошибка. Для `fill`
  `--ref` замещает ТОЛЬКО SELECTOR — позиционник VALUE остаётся обязательным
  (точная матрица — §6). Для `js` EXPR обязателен всегда, `--ref` лишь добавляет
  якорь-биндинг `el`. `key` — ref-only (см. 4.5), `ax --ref` — модификатор
  скоупа снапшота (см. 4.8), `drag` принимает ОДНОРОДНУЮ пару адресаторов:
  два селектора ИЛИ `--ref N --to-ref M`; смешение селектора с ref →
  usage-ошибка (R1-F3, см. 4.7);
- **ref-ы валидны из ЛЮБОГО same-process фрейма** (R1-F2,
  спайк-верифицировано §2.5): DOM-домен page-wide — `resolveNode`/`getBoxModel`
  по backendNodeId дочернего фрейма работают с сессии родителя, координаты
  BoxModel уже скомпонованы в viewport родителя (клик по ref child-кнопки
  дошёл, фокус подтверждён). OOPIF-ref-ов не существует (OOPIF не в снапшоте);
- селекторные режимы существующих команд НЕ меняются ни в одном байте поведения;
- **конвенция stdout (R1-F4):** успех-подтверждения — строчные, паритет с
  существующими `clicked BUTTON (trusted)`/`filled INPUT`: `clicked <TAG>
  (trusted, ref=N)` · `filled <TAG>` · `hovered <TAG>[ (ref=N)]` ·
  `pressed <KEY> (ref=N)` · `dragged <src> -> <dst> (mouse|html5)`; машинные
  отказы/вердикты — CAPS-маркеры (`REF_STALE`, `CLICK_REF_NOT_HITTABLE`,
  `HOVER_NOT_HITTABLE`, `DRAG_NOT_HITTABLE`, `DRAG_CANCELLED`, `ASSERT_*`,
  `AX_OK`). Все строки — в structural-тестах.

### 4.1 `click --ref N`

Одно соединение: `DOM.getDocument` → `DOM.scrollIntoViewIfNeeded({backendNodeId})`
→ `DOM.getBoxModel({backendNodeId})` → **hit-test гейт** (R1-F1:
`DOM.resolveNode` → `Runtime.callFunctionOn` с точкой-в-цель семантикой
селекторного `click`: `elementFromPoint(center)` === el или `el.contains(hit)`;
проверка occluded спайк-верифицирована §2.5) → центр content-quad →
`Input.dispatchMouseEvent` press+release (**всегда trusted**). NOT hittable
(скрыт/перекрыт/вне viewport после scroll) → `CLICK_REF_NOT_HITTABLE: ref N
(hidden/occluded) — refusing dispatch` на stdout + exit 1, клик НЕ выполняется;
untrusted-fallback НЕТ by design — в отличие от селекторного `click` (ref-путь
— верификационный, тихий клик в окклюдер был бы ложью). Успех:
`clicked <TAG> (trusted, ref=N)`.

### 4.2 `fill --ref N VALUE`

`DOM.resolveNode({backendNodeId})` → `Runtime.callFunctionOn(objectId)`:
`this.value = VALUE` + dispatch `input`/`change` (bubbles) — байт-в-байт
семантика селекторного `fill` (включая `<select>` через value). Вывод:
`filled <TAG>` (паритет с селекторным режимом).

### 4.3 `js --ref N 'EXPR'`

`DOM.resolveNode` → `Runtime.callFunctionOn`: EXPR исполняется с биндингом
`el` (= элемент), результат returnByValue как у `js`. Генерический эскейп —
заменяет селекторное DOM-щупанье (53% js-трафика по логам) одним вызовом.

### 4.4 `assert --ref N [--visible|--actionable] [--stable MS] [--timeout S]`

Та же поллинг-механика и flap-диагностика, что у селекторного `assert`
(стабильность ≥ `--stable` мс), но предикат заякорен на элемент:
`DOM.scrollIntoViewIfNeeded` один раз до цикла → `Runtime.callFunctionOn` с
телом `_VISIBLE_PRED_JS`-семантики (`--actionable` добавляет
disabled+hit-test — спайк подтвердил распознавание occluded). Дефолт без
флагов = «узел резолвится» (présence-аналог). `ASSERT_PASS`/`ASSERT_FAIL` —
те же маркеры; протухший ref в ходе поллинга = `REF_STALE` + exit 1.

### 4.5 `key --ref N KEY`

`DOM.focus({backendNodeId})` → `Input.dispatchKeyEvent` rawKeyDown[+char]+keyUp.
KEY ∈ таблица в cdp.py: `Enter` (char `\r` — implicit form submission, спайк:
форма реально сабмитится), `Escape`, `Tab`, `ArrowDown`/`ArrowUp` (расширяемая
мапа windowsVirtualKeyCode/code/key). Неизвестный KEY → usage-ошибка со
списком поддержанных. Закрывает дыру «`fill` ставит value, но не может
отправить форму» (3 KeyboardEvent-хака в логах). Глобального `key KEY` без
`--ref` НЕТ — неявный фокус недетерминирован.

### 4.6 `hover SELECTOR | hover --ref N` (новая команда)

Селекторный путь: measure cmd_click (scrollIntoView → rect → центр) →
`Input.dispatchMouseEvent mouseMoved`. Ref-путь: scrollIntoViewIfNeeded →
getBoxModel → центр → mouseMoved. **Hit-test гейт перед dispatch (R2-F2):**
та же точка-в-цель проверка, что у `click` — not hittable →
`HOVER_NOT_HITTABLE: <адресатор> (hidden/occluded)` + exit 1, события не
диспатчатся (untrusted-аналога у hover не существует — отказ единственно
честен). Спайк: честно триггерит CSS `:hover` (tooltip появился). Селекторный
путь обязателен: hover-таргеты часто не имеют AX-ref (div-меню без ARIA-роли —
урок спайка). Hover-состояние живёт до следующего mouse-события — после
`hover` агент снимает `ax`/скриншот и видит раскрытое меню. Websocket-only
(Input-домен), `(CDP only)` в Quick Reference. Успех: `hovered <TAG>[ (ref=N)]`.

### 4.7 `drag SRC_SEL DST_SEL | drag --ref N --to-ref M` `[--html5 | --cancel]` (новая команда)

Флаги взаимоисключающие (`--cancel` — только mouse-путь; `--cancel`+`--html5`
→ usage-ошибка). **Hit-test гейт обоих концов перед mouse-путём (R2-F2):**
src и dst проверяются той же точка-в-цель семантикой; отказ —
`DRAG_NOT_HITTABLE: src|dst <адресатор>` + exit 1, ничего не диспатчится.
`--html5`-путь гейта не требует (JS-события доставляются элементам напрямую).

Два адресатора (как у hover — drag-участники почти никогда не имеют AX-ref:
безымянные div) и два механизма выбором ЯВНОГО флага (cdp.py-принцип: no
heuristics):

- **дефолт (mouse-серия, trusted):** центр src → `mousePressed` → 5×`mouseMoved
  (buttons=1)` интерполяцией → `mouseReleased` в центре dst. Покрывает
  pointer-based DnD-библиотеки (dnd-kit, SortableJS, react-beautiful-dnd),
  слайдеры, canvas. На нативном `draggable=true` НЕ работает (dragstart не
  рождается из синтетической mouse-серии — спайк) → подсказка в выводе
  «try --html5» при нулевом эффекте недетектируема, поэтому просто
  документируется в SKILL.md;
- **`--html5` (JS-синтез, untrusted):** `new DataTransfer()` +
  `DragEvent dragstart→dragenter→dragover→drop→dragend` через
  `Runtime.callFunctionOn` — payload реально проходит через `dataTransfer`
  (спайк). Ограничение: события `isTrusted=false` (приложения, проверяющие
  trust, не поведутся — задокументировано). Полный trusted-путь Playwright
  (`Input.setInterceptDrags` + `dispatchDragEvent`) сознательно НЕ строим —
  follow-up при первом реальном кейсе, где оба лёгких пути не сработали.

- **`--cancel`** (только mouse-путь): down → половина move-пути → `Escape`
  (dispatchKeyEvent) → release. Тестирует cleanup-листенеры отменённого
  драга — прямой кейс issue #149 (VRHOT minimap/playhead); спайк: страница
  получила `down → esc-cancel`, drop не случился.

Вывод: `dragged <src> -> <dst> (mouse|html5)` / `DRAG_CANCELLED <src> (esc)`;
`REF_STALE`/NOT_FOUND — fail-loud. Смешанная пара адресаторов (селектор + ref)
→ usage-ошибка (R1-F3; structural-тест).

### 4.8 `ax --ref N` (scoped snapshot)

Поиск узла с `backendDOMNodeId == N` по main и same-process child-фреймам
(тот же frame-walk, что у полного `ax` — R1-F2: ref может жить в дочернем
фрейме) → рендер только поддерева из фрейма, где найден (те же фильтры/формат/
`--max-nodes`/`--raw`, та же грамматика заголовка; `frames=` сохраняет единую
семантику «число обойдённых фреймов» — при scoped-поиске это фреймы,
просмотренные до нахождения узла; `nodes=` = сырые узлы дерева фрейма, где
узел найден, `shown=` = отрендеренные строки поддерева — уточнение по ревью
плана, снимает неоднозначность с §3.1).
Узла нет ни в одном фрейме → `REF_STALE` + exit 1. Назначение: дешёвая
повторная проверка одного виджета в fix-verify цикле вместо полного снапшота.

## 5. Изменения документации

### 5.1 ax-first (двусторонние Decision Rules — НЕ «ax вместо всего»)

- **look SKILL.md:** Quick Reference — добавить `ax`/`hover`/`key`/`drag` и
  `--ref`-варианты с тегом `(CDP only)`; Decision Rules: «что на
  странице/состояния/текст → `ax`; layout/цвет/перекрытие/canvas/пиксели →
  `screenshot` (заменять ЗАПРЕЩЕНО)»; Fallback Matrix — строки новых команд
  (CDP-only); пояснение «нет `[disabled]` = enabled».
- **look SKILL.md, начало + frontmatter (issue #187 Proposal B, doc-половина):**
  правило «Shared или isolated — реши ДО первой команды»: 9333 = живой браузер
  пользователя (куки/логины/co-browsing — и там `open`+`--target`, не
  `navigate` активной вкладки); своя задача агента (file://, localhost-превью,
  итерация UI) → изолированная lane. Acceptance #187: ни один существующий
  рецепт не меняет поведения. Proposal A (auto-lane механика) — НЕ здесь,
  остаётся в issue.
- **drive SKILL.md:** `ax` — дефолтный текстовый ground-truth канал
  (см.-примитив verify-core); правило wait-before-ax (снапшот после
  `navigate --wait`/`assert`, иначе intermediate state); `screenshot --bind`
  остаётся обязательным визуальным каналом (drive SKILL.md «screenshot as ground
  truth» для shadow/reactive не трогается); цепочка действия: `ax` → `click --ref`.
- **drive SKILL.md, РАСШИРИТЬ существующую секцию «Assert patterns for modern
  frameworks (dogfood #172)» (issue #172, doc-половина; новую секцию НЕ
  создавать — дубль)
  — трёхходовая маршрутизация shadow DOM:** СЕМАНТИЧЕСКИЕ элементы в shadow
  (кнопки/поля/заголовки) → `ax` + `assert/click --ref` (видит open И closed
  roots — для closed это ЕДИНСТВЕННЫЙ канал, `.shadowRoot` там null; спайк
  §2.5); НЕсемантические (canvas-класс) → AX-узла НЕТ вовсе (честный негатив
  §2.5) — остаётся `--js` с `.shadowRoot` (open-roots) и скриншот; reactive
  re-insert (Alpine x-if) — assert по реактивному state через `--js` (ref
  протухает при пересоздании узла — честный `REF_STALE`, не решение).
- **cdp.py `__doc__`:** usage-строки всех новых команд/веток.

### 5.2 Выпиливание дрейфующих счётчиков (решение Криса, конвенция global CLAUDE.md)

Убрать ВСЕ числовые счётчики команд из прозы: bulldozer CLAUDE.md («18 CDP
commands», «Command count: 19 total…» — оба уже дрейфуют), README.md («17 CDP
commands», «13/17 commands»), look SKILL.md заголовок «Quick Reference — 17
Commands» → «Quick Reference». Заменить формулировками без чисел («команды —
в `COMMANDS`; look-facing — в Quick Reference»). Соотношение fallback-каналов
выражать словами («большинство команд работают без websocket; CDP-only помечены
в Quick Reference»).

### 5.3 Surrogate-safe вывод (closes issue #188)

`cmd_js` падает с `UnicodeEncodeError` на суррогатной половинке (slice эмодзи
в JS-выражении) — а `ax` печатает страничный юникод массово и унаследовал бы
краш. Фикс общий, в одном месте: безопасная печать для всего cdp.py
(`sys.stdout.reconfigure(errors="replace")` в `main()` — или эквивалентный
sanitize-хелпер, если reconfigure недоступен), чтобы НИКАКОЙ страничный текст
не мог уронить пробу. Тесты: суррогатная половинка в js-результате (репро
#188) и в имени AX-узла → команда печатает replacement-символ и выходит 0.

## 6. Тесты

- **Юниты `_render_ax_tree`** (синтетические узлы, offline): фильтры по
  отдельности; StaticText-дубль в кнопке/ячейке; value/attrs/level; refs только
  на интерактивных; цикл `parentId` (терминация); multi-root; усечение
  mid-branch + маркер + `truncated=1`; `--raw` с `[ignored]`; кавычки и
  переводы строк в именах; OOPIF-warn счётная логика (тоже чистая функция).
- **Structural (test_cdp.py):** `cmd_ax` существует и зарегистрирован; AST:
  вызывает `Accessibility.getFullAXTree` и НЕ вызывает `Accessibility.enable`;
  `ax` добавлен в set `test_all_commands_registered`; `__doc__` содержит `ax` и
  все `--ref`-ветки и scoped `ax --ref`; websocket-only отказ; **пер-командная
  парсер-матрица (R2-F1 — генерик-правило ложно отвергало валидные грамматики):**
  `click/assert/hover` — `--ref` + любой позиционник → usage-ошибка;
  `fill --ref N VALUE` — ровно один позиционник VALUE (ноль или два → ошибка);
  `js --ref N EXPR` — EXPR обязателен; `key --ref N KEY` — KEY обязателен,
  неизвестный KEY → ошибка со списком; `drag` — однородная пара, смешение →
  ошибка, `--cancel`+`--html5` → ошибка; везде нечисловой ref → usage-ошибка.
- **E2e (test_e2e.py, jaine_browser):** новая AX-богатая фикстура
  `tests/fixtures/ax-page.html` (таблица 15 строк, disabled/checked/selected,
  aria-label кнопка, текстовое поле с событийными оракулами
  `dataset.inputFired/changeFired`, occluded-кнопка, журнал `window.__actions`):
  грамматика первой строки; роли/состояния/value в снапшоте; same-process
  iframe → секция `frame:`; `click --ref` по ref из живого снапшота → журнал
  страницы (протокол сплита 2.3 как тест); `fill --ref` → значение + оба
  события; `js --ref` → свойства элемента; `assert --ref --actionable` →
  PASS на чистой кнопке, FAIL на occluded; `key --ref Enter` → submit-оракул
  формы (`window.__submitted`); `hover` (оба пути) → CSS `:hover` tooltip
  виден в последующем `ax`; `ax --ref` → только поддерево виджета;
  `drag` (mouse-путь → pointer-zone оракул `__pointerDropped`; `--html5` →
  `__html5Dropped` c payload через DataTransfer; `--cancel` → Esc-оракул
  журнала: down→move→esc-cancel, drop ОТСУТСТВУЕТ — R2-F3); hit-test отказы
  на occluded-фикстуре: `CLICK_REF_NOT_HITTABLE`, `HOVER_NOT_HITTABLE`,
  `DRAG_NOT_HITTABLE` (R2-F2); `REF_STALE` после reload — для всех ref-команд. Фикстура дополняется hover-блоком (`:hover`-tooltip),
  формой с submit-оракулом, двумя drop-зонами (pointer-based + HTML5,
  паттерн спайковой drag.html) и shadow-блоком: open-root с canvas И кнопкой +
  closed-root с кнопкой (оракулы: кнопки в снапшоте с ref и кликаются,
  canvas отсутствует, хосты несут `[shadow=open|closed]`) — закрывает
  «closed-claim без оракула» (панель №3).
- **Структурный doc-тест** (панель №3): тест в духе `test_skill_prompts.py`
  на присутствие shadow-маршрутизации в drive SKILL.md (семантика→ax/ref,
  canvas→--js/screenshot, маркер `[shadow=…]`) — doc-гайданс под охраной,
  не дрейфует молча.
- Чеклист «Adding new commands — MANDATORY» (bulldozer CLAUDE.md) выполняется
  для обеих команд полностью.

## 7. Что НЕ меняется (гарантия «не сломать»)

Существующие команды, `launch.sh`, каналы, verify-core контракты — ноль правок
поведения. Изменения строго аддитивны: +`cmd_ax`, +`cmd_hover`, +`cmd_key`, +`cmd_drag`,
+`--ref`-ветки в `cmd_click`/`cmd_fill`/`cmd_js`/`cmd_assert` (каждая —
отдельный путь ДО существующей логики; селекторные пути байт-в-байт),
+4 записи `COMMANDS` (`ax`, `hover`, `key`, `drag`; `cmd_drag` — тоже новая
аддитивная команда), +фикстура, +тесты, +доки.

## 8. Не-цели v1

- Видимость/геометрия в ax-снапшоте (это `assert --ref --visible/--actionable`
  и hit-test `click`);
- полнота virtualized-списков (показывается отрендеренное — паритет со скриншотом);
- замена скриншота для визуальных проверок;
- trusted-путь HTML5-drag (Playwright DragManager: `Input.setInterceptDrags` +
  `dragIntercepted` + `dispatchDragEvent`) — два лёгких пути §4.7 закрывают
  оба класса DnD; тяжёлый строим только если в живом кейсе оба откажут
  (issue с этим обоснованием);
- file upload (`DOM.setFileInputFiles`), обработка JS-алертов
  (`Page.handleJavaScriptDialog`), back/forward, посимвольный ввод — сверено
  с полной поверхностью Playwright MCP: нулевые следы в логах (все 9
  «dialog»-хитов — DOM-`<dialog>`, который ax видит нативно ролью dialog);
  issue при первом реальном кейсе.

## 9. Follow-up issues (завести после мержа)

1. Полный OOPIF: enumerate `/json` `type=iframe` таргеты + рендер их деревьев секциями.
2. `assert --ax-role ROLE --name SUBSTR`: anchor через `DOM.getDocument`,
   клиентский substring-матч поверх exact-match `queryAXTree`.
3. Калибровка: ax-задачи в drive calibration-manifests; формат-A/B на живых
   продуктах в dogfood.
4. `--pierce-shadow` для селекторных команд (canvas-presence в open shadow без
   рукописного `--js`) — два независимых consult-вердикта NO-GO для v1
   (single-codex + панель №3: отдельная selector-capability на 5 путей,
   дублирует `--js`, closed roots всё равно не покрывает, hit-test слеп,
   перф в поллинге); потребность дополнительно снижена маркером
   `[shadow=open|closed]` (§3.1). Если делать — explicit opt-in, open roots
   only, и только по реальному dogfood-кейсу, где маркер+`--js` не хватило.

## 10. Раскатка

Один PR в `bulldozer/main` (ветка label-привязана к issues #185/#149/#188):
код + тесты + фикстура + доки + выпиливание счётчиков.

**Issue-связки PR** (триаж всех открытых bulldozer-issues 2026-06-11):

- **Closes #185** — сам ax + ref-мост;
- **Closes #149** — item 1 целиком (drag mouse-серией + `--cancel`
  Esc-mid-drag, key, hover), проверено на ИХ живом приложении (§2.5: граница
  wavesurfer-региона реально тянется, `trim.info` 3.0с→2.0с); item 2 НЕ
  воспроизводится на automation-лейне (decode ok, canvas 606×100) — при
  закрытии оставить комментарий с этой эмпирикой: фикс item 2 = «используй
  automation lane», doc-note вместо нового кода;
- **Closes #188** — surrogate-safe вывод (§5.3);
- **Addresses #187** — Proposal B (doc-половина, §5.1); Proposal A (auto-lane
  механика launch.sh) остаётся в issue;
- **Addresses #172** — класс A для СЕМАНТИЧЕСКИХ элементов в shadow (ax видит
  open+closed roots, `assert/click --ref` пирсят — спайк §2.5; closed-roots —
  единственный канал вообще); canvas-подкласс (их буквальный кейс wavesurfer)
  ax НЕ решает — AX-узла у canvas нет (честный негатив §2.5), маршрут остаётся
  `--js`/скриншот и фиксируется в расширяемой секции «Assert patterns for
  modern frameworks» drive SKILL.md (§5.1);
  класс B (reactive re-insert / MutationObserver) остаётся в issue.

НЕ взяты (с основанием): #186 (конвенция issue-филинга — другая тема),
#161 (рефактор conftest-фикстур — отдельная чистка, конфликт-риск),
#160 (launch.sh-гейты — этот PR launch.sh не трогает, §7), #184/#181/#133
(скилл check), #107 (consult), #99 (research), #171 (отдельная drive-фича).

**Имплементационный план ОБЯЗАН включать pre-merge dogfood-фазу** (доктрина
PR #101 — плагин ревьюит собственную разработку своими же скиллами):

1. **Адверсариальный цикл** — `/bulldozer:check` на имплементации (cdp.py
   diff + SKILL.md правки), минимум до вердикта без новых real-находок;
   находки — через `/receiving-code-review` дисциплину.
2. **Живой ax-first прогон** — полный цикл `ax` → `assert --ref` →
   `click/fill/key --ref` → `ax` на живом session-viewer (спайк W1.x —
   первичный потребитель issue) или эквивалентном живом дашборде, в
   изолированном лейне. Фиксируются: спотыкания агента о ref-цепочку,
   пригодность формата, реальные токен-затраты против скриншот-пути.
   Спотыкание о разрыв ax→действие = триггер пересмотра §4 ДО мержа.
3. **Вердикты dogfood — в PR-описание** (что прогнано, что найдено, что
   исправлено) — без этой секции PR не мержится.

После мержа: обновление кеша консьюмеров (`jaine-sync plugins update
bulldozer`), затем продолжительный dogfood в обычной работе (формат-A/B на
живых задачах — follow-up §9 п.3).
