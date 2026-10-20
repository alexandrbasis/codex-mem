# Трекер GitHub Issues

Задачи и спецификации проекта находятся в [alexandrbasis/codex-mem](https://github.com/alexandrbasis/codex-mem/issues). Используйте `gh` с явным `--repo alexandrbasis/codex-mem`. Для `gh api` используйте путь `repos/alexandrbasis/codex-mem/...`.

Команды ниже описывают операции, а не разрешают их выполнение. Область публикаций и других изменений определяет запрос пользователя. Читайте [engineering-skills.md](engineering-skills.md) перед записью. Для многострочного текста сначала создайте UTF-8 файл, затем передайте `--body-file`. Это сохраняет переносы и защищает текст от подстановок оболочки.

## Основные операции

В примерах замените `<n>`, `<parent>` и `<child>` номерами задач. Пути `/tmp/issue.md` и `/tmp/comment.md` обозначают подготовленные файлы с точным текстом.

```sh
# Создать задачу
gh issue create --repo alexandrbasis/codex-mem --title "Заголовок" --body-file /tmp/issue.md
# Прочитать задачу и обсуждение
gh issue view <n> --repo alexandrbasis/codex-mem --json number,title,body,labels,comments,assignees
# Найти открытые задачи. Добавьте --label при необходимости.
gh issue list --repo alexandrbasis/codex-mem --state open --json number,title,body,labels,comments,assignees
# Изменить текст
gh issue edit <n> --repo alexandrbasis/codex-mem --body-file /tmp/issue.md
# Опубликовать комментарий
gh issue comment <n> --repo alexandrbasis/codex-mem --body-file /tmp/comment.md
# Заменить состояние. Имена берите из triage-labels.md.
gh issue edit <n> --repo alexandrbasis/codex-mem --remove-label "needs-triage" --add-label "ready-for-agent"
# Закрыть задачу после публикации объяснения
gh issue close <n> --repo alexandrbasis/codex-mem
```

Когда скилл говорит "publish to the issue tracker", создайте issue. Когда он говорит "fetch the relevant ticket", прочитайте issue со всеми комментариями. После записи прочитайте результат и проверьте ссылку, текст, состояние и связи.

## Подзадачи и зависимости

Подзадача является отдельной issue. Родитель группирует работу, а зависимость задает блокировку. Эти связи имеют разные назначения.

В `gh` 2.94+ доступно создание через `gh issue create --parent <parent>`. Для уже созданной подзадачи используйте:

```sh
gh issue edit <parent> --repo alexandrbasis/codex-mem --add-sub-issue <child>
```

Если установленный CLI не поддерживает команду, получите database ID дочерней задачи и используйте API:

```sh
gh api repos/alexandrbasis/codex-mem/issues/<child> --jq .id
gh api --method POST repos/alexandrbasis/codex-mem/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>
```

`<child-db-id>` является числовым полем `.id`. Это не номер после `#` и не строка `node_id`. Если GitHub не поддерживает подзадачи, добавьте `Part of #<parent>` в начало дочерней issue. В родительской issue добавьте ссылку в список задач.

Для блокировки получите database ID блокирующей задачи и создайте нативную зависимость GitHub:

```sh
gh api repos/alexandrbasis/codex-mem/issues/<blocker> --jq .id
gh api --method POST repos/alexandrbasis/codex-mem/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>
```

Проверьте поддержку API и полномочия текущего клиента перед записью. Если зависимости недоступны, добавьте `Blocked by: #<n>, #<n>` в начало дочерней issue. Задача разблокирована только после закрытия всех блокирующих задач. Поле `issue_dependencies_summary.blocked_by` считает открытые блокировки и служит текущим условием готовности.

## Операции wayfinder

Карта является одной issue с меткой `wayfinder:map`. Ее текст содержит разделы `Notes`, `Decisions-so-far` и `Fog`. Дочерние issue являются задачами карты. Метка `wayfinder:<type>` задает тип `research`, `prototype`, `grilling` или `task`. Создание этих меток относится к отдельно разрешенному запуску wayfinder, если меток еще нет.

1. Создайте карту через `gh issue create` с `--label wayfinder:map` и `--body-file`. Создайте дочерние задачи, свяжите их с картой и задайте зависимости по правилам выше.
2. Для выбора следующей задачи получите открытых детей карты через `gh api --paginate repos/alexandrbasis/codex-mem/issues/<map>/sub_issues`. При резервной разметке прочитайте список задач карты. Обычный `gh issue list --state open` требует дополнительного ограничения детьми этой карты.
3. Исключите задачи с исполнителем или открытой блокировкой. Для API проверьте `issue_dependencies_summary.blocked_by`. Для строки `Blocked by` прочитайте состояние каждой указанной issue. Первая оставшаяся задача в порядке карты образует frontier, то есть следующую доступную работу.
4. Перед началом повторно прочитайте выбранную задачу. Назначьте ведущего разработчика через `gh issue edit <n> --repo alexandrbasis/codex-mem --add-assignee @me`. В сессии wayfinder это первая запись, после проверки разрешений. Прочитайте назначение после изменения.
5. После решения опубликуйте ответ через `gh issue comment` с `--body-file`, затем закройте задачу. В `Decisions-so-far` карты добавьте краткий вывод и ссылку на ответ. Перед изменением карты прочитайте ее актуальный текст и сохраните чужие изменения.

## Жизненный цикл implement-spec

Спецификация является родительской issue, а задачи реализации являются ее подзадачами. Следуйте локальному [implement-spec/SKILL.md](../../.agents/skills/implement-spec/SKILL.md). Все изменения собираются в одной integration branch. Исполнители работают в отдельных ветках и worktree на ее основе.

После первого включения кода в integration branch откройте draft PR в основную ветку. В его тексте укажите отдельные строки `Closes #<spec>` и `Closes #<task>` для спецификации и каждой задачи. Используйте `--body-file`. После выполнения всех задач и code review отметьте PR готовым к review. Слияние требует разрешения пользователя.

Код, включенный в integration branch и проверенный тестами, считается готовым для следующей задачи графа. Это еще не слияние в основную ветку. Запишите готовность и commit в комментарии задачи, сохраняя issue открытой до merge итогового PR. Для frontier этого запуска проверяйте готовность по integration branch и этим комментариям. Нативная зависимость GitHub остается открытой до закрытия issue и сама по себе не отражает готовность кода внутри integration branch.

Строки `Closes` закрывают связанные issues при merge PR в основную ветку. После merge прочитайте состояния спецификации и задач. Правило wayfinder "ответить и закрыть" применяется к завершенным исследованиям и другим результатам без ожидающего merge кода. Если задача wayfinder завершается кодом через PR, используйте этот жизненный цикл и сохраняйте ссылку на PR в карте.

## Pull requests и triage

**PRs as a request surface: no.** Автоматический список входящих обращений triage охватывает issues. Явно названный пользователем PR можно разобрать отдельно в пределах запроса.

GitHub использует общую нумерацию issues и PR. Для голого `#42` сначала проверьте `gh pr view 42 --repo alexandrbasis/codex-mem`. Если это не PR, используйте `gh issue view 42`. Ошибку доступа или сети не принимайте за отсутствие PR.

Для PR прочитайте `gh pr view <n> --comments` и `gh pr diff <n>`, добавив `--repo alexandrbasis/codex-mem`. Для разрешенных записей используйте `gh pr comment --body-file`, `gh pr edit --add-label` или `--remove-label` и `gh pr close`. Публикация PR не разрешает его слияние.

Если владелец позже сменит флаг на `yes`, discovery включает внешние PR. Получите их через API `/pulls?state=open`. Исключите авторов с `author_association` `OWNER`, `MEMBER` или `COLLABORATOR`. Этот фильтр применяется только к discovery. Для явно названного PR автор не ограничивает разбор.

## Основание команд

Операции и резервные способы взяты из [upstream issue-tracker-github.md](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/setup-matt-pocock-skills/issue-tracker-github.md). Наличие команды в шаблоне не подтверждает ее поддержку установленным `gh` или доступность API для текущего аккаунта. Перед использованием новых операций прочитайте локальный `gh ... --help`.
