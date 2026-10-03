# Промобот «Айтишкино — Преображенка»

Telegram-бот для записи семейных заявок в AlfaCRM. Промоутер или владелец отвечает на вопросы о родителе, контактах и детях, проверяет анкету и отправляет её в CRM. Одна семья создаёт одну карточку, а комментарий сохраняется отдельной коммуникацией от технического аккаунта.

Бот сохраняет черновики и очередь в SQLite, переживает перезапуск и сообщает о доставке отдельно от приёма анкеты. Если телефон или Telegram уже есть среди лидов, клиентов либо архивных карточек, заявка передаётся администратору для проверки. Существующие карточки бот не изменяет.

## Как добавить лида

Откройте [@itishkino_promo_bot](https://t.me/itishkino_promo_bot), отправьте `/start` и нажмите **«Добавить лида»**. Можно сразу отправить `/new`. Владелец из `ADMIN_TELEGRAM_ID` умеет добавлять лидов без отдельной роли промоутера и сохраняет административный доступ.

Введите имя родителя, телефон или Telegram, предпочтение связи и всех детей. Добавьте комментарий при необходимости, проверьте ответы и нажмите **«Отправить в CRM»**. «Заявка сохранена» подтверждает приём ботом; «Создано в CRM» подтверждает результат записи и проверки. «Мои заявки» показывает ваши отправленные анкеты. Подробности: [памятка](docs/promoter.md).

## Запуск на сервере через Docker

Нужны Linux, Docker Engine и Docker Compose 2.20+; на DietPi Docker можно поставить через `dietpi-software`. Открывать входящие порты для бота не требуется. Для рабочего бота используйте один экземпляр, включая локально запущенные копии.

Получите проект и перейдите в его каталог:

```sh
git clone https://github.com/112Alex/itishkino-promobot.git
cd itishkino-promobot
```

Подготовьте настройки:

```sh
mkdir -p runtime
cp .env.example runtime/.env
cp config/preobrazhenka.example.json runtime/config.json
chmod 700 runtime
chmod 600 runtime/.env runtime/config.json
nano runtime/.env
nano runtime/config.json
```

В `.env` заполните токен BotFather, свой числовой Telegram ID, email технического пользователя CRM и ключ v2api. В `runtime/config.json` добавьте ID промоутеров в `promoters`, дополнительных администраторов в `administrators`, например `"123456789": "preobrazhenka"`. Владельца в эти списки добавлять не нужно. Уведомления о проблемах получает владелец.

`config/preobrazhenka.example.json` содержит параметры **этого филиала этой CRM**, проверенные реальной записью 3 октября 2026. Для другой CRM используйте `config/production.example.json` и пройдите [приёмку](docs/integration.md). Не переносите флаги проверки на другой аккаунт/филиал автоматически.

Контейнер работает от UID/GID 10001. После редактирования настройте владельца файлов и запустите:

```sh
sudo chown 10001:10001 runtime/.env runtime/config.json
docker compose build
docker compose run --rm bot doctor
docker compose up -d
docker compose logs --tail=50 bot
```

Команда `doctor` проверяет подключение CRM; она не создаёт лидов и не отправляет сообщения Telegram. После запуска владелец, Олег и Максим должны открыть бота и нажать `/start`.

Для переноса настроек текущего рабочего аккаунта используйте свои защищённые env/JSON, а не заново заполненные примеры. Контейнерные пути из `.env.example` оставьте как указано. **Перед запуском на сервере остановите локальную копию этого Telegram-бота.** Порядок переноса данных, резервных копий и VPN: [Docker на DietPi](docs/docker.md).

База и резервные копии находятся в постоянном Docker volume `data`. Пересборка образа их сохраняет. `docker compose down` останавливает бот; **`down -v` удаляет данные**.

## Обслуживание

```sh
docker compose ps
docker compose logs --tail=100 bot
docker compose run --rm bot doctor
docker compose run --rm bot backup
docker compose restart bot
```

Копия SQLite создаётся штатным API базы данных, с проверкой целостности. [Инструкция Docker](docs/docker.md) описывает выгрузку копии, ежедневный backup и обновления. Telegram-proxy задаётся только через `TELEGRAM_PROXY`; запросы к CRM идут напрямую.

## Локальная разработка и mock

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
cp .env.mock.example .env.test
chmod 600 .env.test
.venv/bin/promobot --env .env.test doctor
.venv/bin/promobot --env .env.test integration-test
.venv/bin/python -m pytest -q
```

Mock не обращается к рабочей CRM. Без Telegram-токена ответы сохраняются только в локальном outbox. Для приёмки используйте отдельную БД, которую не запускаете потом с рабочим ботом. Тесты не создают карточки в реальной CRM. `requirements.lock` фиксирует зависимости разработки, `requirements.runtime.lock` — только рабочие зависимости образа.

## Документация

- [Docker: перенос, запуск и резервные копии](docs/docker.md)
- [Памятка пользователя бота](docs/promoter.md)
- [Работа владельца и установка без Docker](docs/owner.md)
- [Контракт AlfaCRM и приёмка](docs/integration.md)
- [Результат реальной проверки CRM](docs/crm-acceptance.md)
- [AWG 2.0 и маршрутизация](docs/vpn.md)
- [Тестирование](docs/testing.md)

## Как устроен проект

Python 3.11–3.12, aiogram 3, httpx и aiosqlite. `dialog.py` ведёт анкету; `storage.py` хранит события, черновики и очередь; `worker.py` проверяет совпадения, доставляет и сверяет карточку; `crm.py` работает с API; `telegram.py` принимает обновления и отправляет сохранённые ответы. SQLite использует WAL, foreign keys и `synchronous=FULL`.

Неопределённый результат записи сначала сверяется по номеру заявки; слепого повторного создания нет. Поиск дублей и создание — разные API-запросы: параллельное ручное создание менеджером оставляет окно гонки. Приёмку интерфейса CRM, серверной сети и перезагрузки DietPi нужно выполнить на месте. GitHub Actions проверяет Python 3.11/3.12 и сборку Docker.
