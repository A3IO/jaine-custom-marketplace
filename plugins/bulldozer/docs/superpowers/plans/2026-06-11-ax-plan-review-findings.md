# Ревью плана 2026-06-11-ax-command-and-ref-bridge.md (сессия-автор спеки)

**Вердикт: механизмы верны, мержить план МОЖНО после правок ТЕСТОВЫХ секций и
Task 10.** Проверка: 4 верификатора (фиделити/болячки/полнота/консистентность,
35 находок) + личная перепроверка блокеров грепами. НЕ перепроектируй
механизмы — все clean: грамматика AX_OK, фильтры+shadow-исключение,
anti-enable AST, парсер-матрица, аддитивность, выпил счётчиков (все 4 точки),
dogfood-гейт, порядок задач, TDD-каденция.

## Блокеры (подтверждены лично)

1. **DRAG_NOT_HITTABLE отсутствует полностью** (0 вхождений): Task 8 говорит
   «hit-test gate both endpoints» прозой, но ни маркера `DRAG_NOT_HITTABLE:
   src|dst <адресатор>` в коде шага, ни e2e-теста (спека §4.7 + §6 требуют).
2. **test_drag_cancel_esc — оракул не существует**: ассерт ждёт `esc-cancel`
   в `window.__actions`, но фикстура Task 2 не имеет Escape-keydown листенера,
   пишущего его (паттерн — в /0/.aitemp/ax-split-test/shadow-esc.html).
3. **#187 Proposal B отсутствует** (0 вхождений): спека §5.1 требует блок
   «Shared или isolated — реши ДО первой команды» в начало look SKILL.md +
   сжатую форму во frontmatter description. Добавить в Task 10.
4. **Фикстура: таблица 2 строки вместо 15** (спека §6; e2e-вопрос «сколько
   строк» и сплит-протокол считают 15).
5. **REF_STALE e2e только для click** — File Map обещает «all ref commands»,
   спека §6: «для всех ref-команд» (click/fill/js/assert/key/hover/drag-ref).
6. **Task 5 «Expected: ALL PASS» ложно**: `test_all_commands_registered` с
   hover/key/drag в set красный до Tasks 6–8. Либо добавлять команды в set
   инкрементально по задачам, либо честно пометить ожидаемый RED.

## Системный класс (≈70% находок): тесты слабее контрактов

Спека §4: «Все строки — в structural-тестах». План проверяет подстроки
(`"hovered" in`, `"clicked" in` + `f"ref={ref}" in`), а не точные форматы.
Добавить структурные/e2e ассерты ПОЛНЫХ строк по конвенции §4:
`clicked <TAG> (trusted, ref=N)` · `hovered <TAG>[ (ref=N)]` ·
`pressed <KEY> (ref=N)` · `filled <TAG>` · `dragged <src> -> <dst> (mouse|html5)` ·
`DRAG_CANCELLED <src> (esc)` · точный текст маркера усечения · regex AX_OK.

## Пропущенные обязательные e2e (спека §6)

- click --ref по кнопке в CLOSED shadow root (фикстура есть — теста нет);
- click --ref по ref из ДОЧЕРНЕГО фрейма (фикстура есть — теста нет);
- hover --ref путь (есть только селекторный);
- drag --ref N --to-ref M (есть только селекторный);
- OOPIF WARN на stderr (счётная логика юнитом есть, поведение cmd_ax — нет);
- surrogate-половинка в ИМЕНИ AX-узла (есть только js-вариант);
- doc-тест: добавить проверку упоминания маркера `[shadow=` в drive SKILL.md.

## Мелочь

- `ws_mod` алиас в Task 5 → конвенция cdp.py `import websocket`;
- `time.sleep(1)` в test_ref_stale_after_reload → condition-based wait
  (testing doctrine);
- ref_val==0 edge в scoped ax (OS-валидные backendNodeId > 0, но guard дешёв);
- label у form-input в фикстуре (иначе key-тест матчится на чужой textbox);
- CLAUDE.md «Architecture table update» — сверх спеки; допустимо, но держи
  минимальным (без новых счётчиков!).

## Спека-уточнение (внесено автором спеки, коммит в ветке)

Семантика scoped `ax --ref`: `nodes=` = сырые узлы ДЕРЕВА ФРЕЙМА, где найден
узел; `shown=` = отрендеренные строки поддерева. План это и делает — теперь
запинено в §4.8, расхождение с §3.1 снято.
