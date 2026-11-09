# Trader Skill для Т‑Инвестиций

Российская функциональная адаптация всех **11 тематических навыков** [trader-skill](https://github.com/jakeschincariol/trader-skill) и дополнительный координатор **`trader-tinvest`**. Пакет содержит 12 папок навыков, рабочие офлайн-CLI, контракты, искусственные примеры и тесты. Денежные расчёты используют Python 3.9+ и стандартную библиотеку.

Текущий этап — перенос и проверка функций на файлах. Сбор реальных источников, фактические ответы T‑Invest MCP и их полная нормализация требуют отдельной интеграционной проверки. Подключение счёта, выбор стратегии и реальные операции при установке не выполняются. `trader-broker` оформляет только черновики; проверка тестов не устанавливает прибыльность.

## Состав и применение

Пути CLI указаны от корня репозитория. Для каждого навыка схема и пример находятся по ссылке на его `SKILL.md`.

| Навык | Функция в адаптации | CLI |
|---|---|---|
| [trader-copycat](skills/trader-copycat/SKILL.md) | Публичные исторические составы, сопоставление ISIN/UID, сравнение полных снимков, цели top-N | `skills/trader-copycat/holdings.py` |
| [trader-news](skills/trader-news/SKILL.md) | Нормализация сохранённых новостей MCP/RSS/Atom, связь с инструментами, дедупликация | `skills/trader-news/scripts/news.py` |
| [trader-budget](skills/trader-budget/SKILL.md) | Распределение своих денег по явным весам, целым лотам, ограничениям и издержкам | `skills/trader-budget/size.py` |
| [trader-pelosi](skills/trader-pelosi/SKILL.md) | Новые и исправленные публичные раскрытия российских эмитентов и владельцев, даты и диапазоны | `skills/trader-pelosi/scripts/disclosures.py` |
| [trader-analyst](skills/trader-analyst/SKILL.md) | Сопоставимые опубликованные показатели МСФО/РСБУ, маржа и рост | `skills/trader-analyst/scripts/facts.py` |
| [trader-screener](skills/trader-screener/SKILL.md) | Фильтрация и ранжирование по проверяемым метрикам с объяснением отказов | `skills/trader-screener/scripts/screen.py` |
| [trader-risk](skills/trader-risk/SKILL.md) | Совместный офлайн-контроль BUY/SELL, дневного убытка, идентичности и явных лимитов | `skills/trader-risk/scripts/check.py` |
| [trader-broker](skills/trader-broker/SKILL.md) | Повторный риск-контроль и журнал подготовки параметров `DRAFT_ONLY` | `skills/trader-broker/scripts/ticket.py` |
| [trader-tax](skills/trader-tax/SKILL.md) | ФИФО/RUB по записанным операциям, комиссии, исторические курсы, корзины и годы | `skills/trader-tax/scripts/lots.py` |
| [trader-alerts](skills/trader-alerts/SKILL.md) | Ценовые условия, переходы состояния и интервалы биржи из переданных данных | `skills/trader-alerts/scripts/alerts.py` |
| [trader-rebalance](skills/trader-rebalance/SKILL.md) | Отклонения от согласованных целей; продажи своих бумаг и покупки на текущие деньги | `skills/trader-rebalance/rebalance.py` |
| [trader-tinvest](skills/trader-tinvest/SKILL.md) | Координация модулей, общие денежные проверки, нормализация и необязательные исследовательские инструменты | `skills/trader-tinvest/scripts/` |

Сохранение названий означает соответствие задач, а не неизменённое копирование американских интеграций. `trader-pelosi` заменяет предмет мониторинга публичными раскрытиями эмитентов РФ; реестр сделок российских политиков не заявляется. `trader-tax` не переносит US wash sale и не является полным налоговым движком РФ. Подробности: [статус переноса](docs/migration-status.md), [общий контракт](docs/porting-contract.md), [разбор исходника](skills/trader-tinvest/references/upstream-review.md).

## Установка всего пакета

Сохраняйте **все 12 папок рядом в одном каталоге навыков**. Тематические модули используют общие файлы `trader-tinvest/scripts`; установка одного `trader-risk` или `trader-budget` без координатора не является установкой всего пакета.

В Codex можно передать установщику:

```text
$skill-installer Установи весь пакет из https://github.com/LDK28/trader-skill, все 12 путей:
skills/trader-tinvest, skills/trader-copycat, skills/trader-news, skills/trader-budget,
skills/trader-pelosi, skills/trader-analyst, skills/trader-screener, skills/trader-risk,
skills/trader-broker, skills/trader-tax, skills/trader-alerts, skills/trader-rebalance.
Сохрани их соседями в одном каталоге навыков и проверь доступность общих скриптов.
```

Альтернатива для установки в проект: разместить эти папки из `skills/` в `.agents/skills/` целевого проекта. Само наличие `skills/` в клонированном репозитории позволяет запускать CLI, но не гарантирует обнаружение навыков каждым клиентом. После установки проверьте 12 имён в списке навыков; если изменения не появились, перезапустите клиент. В Codex используются упоминания `$trader-news`, `$trader-tax` и т. п.; в ChatGPT выбирайте доступный навык через `@`. [Официальная документация](https://learn.chatgpt.com/docs/build-skills).

Для передачи исходников без секретов предусмотрен архив:

```bash
python3 scripts/package_skills.py --output .local/trader-skill-ru.zip
```

Архив сохраняет папки навыков и общие зависимости. В ChatGPT Work с доступной средой файлов и Python его можно распаковать для работы с CLI. Если ваш интерфейс Skills поддерживает импорт такого многомодульного ZIP, используйте его импорт и проверьте, что установлены все 12 папок и сохранены соседские зависимости. Поддержка конкретного интерфейса импорта требует проверки; загрузка архива как вложения сама по себе не подтверждает установку навыков.

Упаковка не публикует проект в универсальном каталоге и не переносит MCP-подключения, права или авторизацию счёта. Локальные навыки, workspace Skills и плагины имеют отдельные процедуры установки и доступа. [Официальное описание границ](https://learn.chatgpt.com/docs/enterprise/skills).

## Первый запуск без счёта

Из корня репозитория можно проверить искусственные примеры:

```bash
python3 skills/trader-copycat/holdings.py skills/trader-copycat/references/example.json
python3 skills/trader-budget/size.py skills/trader-budget/references/example.json
python3 skills/trader-analyst/scripts/facts.py skills/trader-analyst/references/reports.example.json
python3 skills/trader-tax/scripts/lots.py report skills/trader-tax/examples/transactions.synthetic.json
python3 skills/trader-pelosi/scripts/disclosures.py index skills/trader-pelosi/examples/disclosures.synthetic.json
python3 skills/trader-risk/scripts/check.py skills/trader-risk/references/example.json --as-of 2026-01-12T07:00:00Z
python3 skills/trader-broker/scripts/ticket.py skills/trader-broker/references/example.json --as-of 2026-01-12T07:00:00Z
```

`--as-of` предназначен для воспроизведения примера, а не освежения старого снимка счёта. Остальные CLI показывают параметры через `--help`. Новости, оповещения и раскрытия возвращают состояние для последующих проверок; сохраняйте его по контракту конкретного модуля. Расписание и отправка уведомлений не создаются автоматически.

Примеры запросов после установки:

```text
$trader-tinvest Покажи состав пакета и выполни искусственный пример распределения бюджета. Укажи, какие данные не проверены у брокера.
$trader-analyst Сопоставь переданные опубликованные отчёты одного эмитента, сохрани единицы и стандарты учёта.
$trader-risk Проверь весь предоставленный нормализованный пакет. Покажи отдельные результаты бюджета, своих бумаг и дневного контроля.
$trader-tax Рассчитай ФИФО-реестр по фактическим операциям за указанный год; налоговую базу и ставки не угадывай.
```

Выходы модулей не соединяются добавлением флага `verified:true`: переходы между ними требуют сверки идентификаторов, единиц, источников и времени. `NORMALIZED_OBSERVATIONS`, `ANALYZED`, `PASS_OFFLINE`, `DRAFT_ONLY` и `SIMULATED` обозначают разные этапы. Параметры и ограничения — в [общем контракте](docs/porting-contract.md) и контрактах навыков.

## Личные параметры и последующее подключение

[policy.json](skills/trader-tinvest/references/policy.json) — публичный пример с `profile_kind: "example"` и `requires_explicit_adoption:true`. Он не устанавливает риск нового пользователя. Согласованные параметры можно сохранить в `skills/trader-tinvest/references/policy.local.json`, исключённом из Git. При использовании всего собственного остатка остаются расходы на комиссии, обязательные платежи и округление до лотов. Демонстрационный порог 50% не является универсальной рекомендацией; дневной контроль прекращает новые предложения, а не закрывает позиции и не гарантирует максимальный убыток.

Подключение реальных источников и счёта — отдельный этап по запросу пользователя. Официальный сервер — [T‑Invest MCP](https://invest-public-api.tbank.ru/mcp), транспорт Streamable HTTP; для аналитики достаточно read-only доступа. Токен вводится в настройки подключения, вне чата и репозитория. Документированный метод API не означает наличие такого инструмента в текущем MCP-сеансе: нужно проверять фактическую схему и ответы. Установка этого пакета не меняет настройки счёта и не включает торговлю. [Инструкция Т‑Банка](https://developer.tbank.ru/invest/mcp/agents-connect/coding-agents).

Полноценная интеграция также требует подтверждённых собственных рассчитанных остатков, блокировок, доступности, лотов и оценки полной стоимости. Покупки не финансируются ожидаемой выручкой продаж. Фьючерсное ГО не равно полной оплате позиции; заёмные бумаги и плечо текущим cash-only контуром не поддерживаются.

## Необязательные исследовательские инструменты координатора

Сохранены отдельные инструменты: [REST-нормализация](skills/trader-tinvest/references/broker-normalization.md), [протокол стратегии](skills/trader-tinvest/references/strategy-research.md), [бэктест пробоя](skills/trader-tinvest/references/backtest-breakout.md), [подготовка истории MOEX](skills/trader-tinvest/references/moex-history.md), [исторические горизонты](skills/trader-tinvest/references/research-horizons.md). Их наличие не выбирает стратегию и не запускает исследование при установке. MOEX-загрузчик делает публичные GET только при отдельном запуске; replay работает с сохранёнными файлами. История сохраняет статус непроверенной, а успешная симуляция не устанавливает преимущество.

## Проверка и лицензия

```bash
python3 -m unittest discover -s tests -v
```

Тесты используют искусственные данные, не требуют сети, токенов и брокерского счёта. Они проверяют наблюдаемое поведение расчётных модулей; фактическое подключение, качество рыночных данных и прибыльность проверяются отдельно. В этой фазе не проводятся операции со счётом или исследование новой стратегии.

База переноса upstream: [`5189e79b9a480755a3733ac9713f5353981dbe18`](https://github.com/jakeschincariol/trader-skill/tree/5189e79b9a480755a3733ac9713f5353981dbe18). Уведомления об исходном проекте и его MIT-лицензии сохранены в [NOTICE.md](NOTICE.md) и [third_party/trader-skill/LICENSE](third_party/trader-skill/LICENSE). Перенос функций описан отдельно от готовности российских интеграций.
