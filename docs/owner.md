# Установка и работа владельца

## Перед запуском

Используйте уже созданного рабочего бота; если его нет, создайте через официальный **@BotFather**, команда `/newbot`. Токен храните в локальном env-файле, не в чате/репозитории. Уже существующий тестовый бот остаётся отдельным. Все промоутеры и владелец один раз отправляют `/start`. Узнайте числовые ID командой `/id`, впишите первоначальных администраторов в `ADMIN_TELEGRAM_IDS` через запятую. `ADMIN_TELEGRAM_ID` необязателен, для диагностики при пустом значении используется первый ID CSV. Промоутеры добавляются из меню «Админское меню → Промоутеры» по ID или username с подтверждением после `/start`. Их роли сохраняются в SQLite.

В Alfa CRM создайте отдельного технического пользователя. Минимальные права: модуль v2api; чтение филиала, источников, этапов/воронки, лидов/клиентов/архива и коммуникаций; создание лида и комментария; чтение созданного результата. Только нужный филиал. Права удаления и изменения карточек приложению не нужны. Не назначайте технического пользователя ответственным автоматически; проверьте CRM-автоматику. Email/API key берутся из профиля, вводятся локально.

Точные параметры выясняются по [инструкции CRM](integration.md). Пример `config/production.example.json` оставляет неизвестные ID пустыми. Перенесите его в `/etc/promobot/prod.json`; реальные значения не угадывайте. Флаги `*_verified` разрешают запись и устанавливаются только по результатам приёмки контракта, а не чтобы скрыть ошибку запуска.

## Debian 12 / DietPi

Ниже команды для выполнения владельцем на сервере. Они здесь подготовлены, на вашем Mac Mini не выполнялись.

```sh
sh scripts/server-preflight.sh
sudo apt update
sudo apt install python3.11 python3.11-venv ca-certificates
sudo useradd --system --home-dir /var/lib/promobot --shell /usr/sbin/nologin promobot
sudo install -d -o root -g promobot -m 0750 /opt/promobot /etc/promobot
sudo install -d -o promobot -g promobot -m 0700 /var/lib/promobot/test /var/lib/promobot/prod
```

Перенесите исходники в `/opt/promobot` (без `.venv`, `.env*`, БД, кешей и копий). Создайте venv на самом сервере, не копируйте локальное окружение:

```sh
cd /opt/promobot
sudo python3.11 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.lock
sudo .venv/bin/python -m pip install --no-deps .
```

Зафиксированы прямые и транзитивные зависимости, включая тестовые; installer пакета использует зафиксированный setuptools из `pyproject.toml`. Не включайте uv в зависимости бота: он использован только для локальной проверки Python 3.11.

Создайте env из примера, затем отредактируйте файл локальным редактором:

```sh
sudo install -o root -g promobot -m 0640 .env.example /etc/promobot/test.env
sudoedit /etc/promobot/test.env
sudo install -o root -g promobot -m 0640 config/mock.json /etc/promobot/test.json
sudoedit /etc/promobot/test.json
```

Для systemd допустимы права `root:promobot 0640`; для личного файла — `0600`. JSON содержит разрешения доступа, также защищён. Параметры тестового env:

```dotenv
ENVIRONMENT=test
CRM_MODE=mock
DATABASE_PATH=/var/lib/promobot/test/bot.sqlite3
CONFIG_PATH=/etc/promobot/test.json
BACKUP_PATH=/var/lib/promobot/test/backups
```

Укажите отдельно токен, admin ID, при необходимости проверенный `TELEGRAM_PROXY`. Для рабочего env используйте `ENVIRONMENT=prod`, `CRM_MODE=real`, отдельный токен, `/var/lib/promobot/prod/bot.sqlite3`, `/etc/promobot/prod.json`, отдельные копии. БД привязана к окружению, боту, режиму CRM, адресу CRM и филиалу; переключать mock-БД в real нельзя.

Подключение Telegram через AWG требует [отдельной настройки сети](vpn.md). Бот работает без root/CAP_NET_ADMIN; HTTPX CRM не использует Telegram proxy и игнорирует глобальные proxy-переменные.

## Диагностика и systemd

```sh
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/test.env doctor
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/test.env doctor --telegram-check
sudo install -m 0644 deploy/promobot@.service /etc/systemd/system/
sudo install -m 0644 deploy/promobot-backup@.service deploy/promobot-backup@.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/promobot@.service /etc/systemd/system/promobot-backup@.service /etc/systemd/system/promobot-backup@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now promobot@test.service promobot-backup@test.timer
sudo systemctl status promobot@test.service
sudo journalctl -u promobot@test.service -n 50
```

`doctor --telegram-check` отправляет одно диагностическое сообщение владельцу: сначала `/start`. Без флага внешнего Telegram-сообщения нет. Обычный `doctor` читает состояние inbox/очередей, возраст старейшей задачи, последнее успешное polling, проверяет CRM login; проверка полного контракта — отдельно. Не включайте DEBUG сторонних HTTP/Telegram-библиотек: их сообщения могут содержать URL с токеном.

`ProtectSystem=strict` оставляет записи в `/var/lib/promobot` и `/tmp`; SQLite, WAL, backup находятся в разрешённом каталоге. Общий `/tmp` нужен для блокировки одного получателя на токен между test/prod; защита не распространяется на другую машину — второй polling на другом сервере запускать нельзя. `PrivateDevices=true` допустим боту: TUN использует отдельная VPN-служба.

После проверки тестового бота и реальной приёмки аналогично включите `promobot@prod` и backup@prod. Если используете отдельный proxy-unit, добавьте боту `After=`/`Wants=` на него. Не добавляйте `BindsTo`: при остановке VPN очередь CRM должна продолжать работать напрямую.

## Ошибки и безопасный повтор

В Telegram всем администраторам доступны `/problems`, `/request НОМЕР`, `/retry НОМЕР`. Детали включают контакты только в личном чате администратора. Совпадения нельзя превратить в автоматические дубли через retry. Проверяйте существующую карточку вручную; бот её не изменяет.

При `manual_review` выясните причину: недостоверный поиск, права, CRM-автоматика, неизвестный результат записи. После устранения причины `/retry` возобновит **сверку прежней операции**, ID заявки сохранится. Если исход create остаётся неизвестным и результата по ID нет, автоматический повтор create запрещён даже администратору. Такой случай закрывается владельцем вне автоматической доставки, после проверки CRM; локальная заявка сохраняется для расследования. Не редактируйте SQL-фазу на create.

Ошибки одного состояния агрегируются в одно уведомление на заявку для каждого администратора, успехи отправляются промоутеру. При полном отключении Telegram уведомления тоже ждут сети. Для заблокированных пользователем/отклонённых Telegram сообщений после исправления причины выполните локальную административную команду `outbox-retry`; она не затрагивает CRM.

## Копии, обновление, восстановление

Ежедневно в 02:30 МСК timer делает SQLite backup API, проверяет integrity_check и оставляет последние `BACKUP_KEEP` (по умолчанию 14) копий. Внеплановая копия:

```sh
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/prod.env backup
```

Не копируйте только `.db` работающего WAL. Копии содержат контакты: храните с правами 600, отдельную офлайн-копию на защищённом носителе. В mock для полного восстановления подставной CRM отдельно копируйте её БД через такой же backup-механизм; основной backup сохраняет бота, а не внешнюю CRM.

Перед обновлением: остановите службу, сделайте backup старой версией, замените код/зависимости, выполните тесты, запустите doctor/службу. SQL-миграции версионируются; перед миграцией существующей БД делается согласованная копия. Обновление не очищает черновики/очереди.

```sh
sudo systemctl stop promobot@prod
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/prod.env backup
# Перенос нового кода и установка зависимостей, затем:
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/prod.env doctor
sudo systemctl start promobot@prod
```

Проверка восстановления — только в новый файл:

```sh
sudo -u promobot /opt/promobot/.venv/bin/promobot --env /etc/promobot/prod.env restore --source /var/lib/promobot/prod/backups/ВЫБРАННАЯ.sqlite3 --target /var/lib/promobot/prod/restored.sqlite3
```

Для реального восстановления остановите службу, восстановите в новый файл, проверьте содержимое/doctor, измените `DATABASE_PATH`, затем включите один получатель. Копия старее последней подтверждённой операции может потерять свежие локальные заявки/offset; перед возобновлением нужно сверить последние CRM ID и операции. Не обещается exactly-once после отката БД к произвольно старой копии.

Завершённые payload inbox очищаются через `INBOX_RETENTION_DAYS` (7). Ключи событий сохраняются для защиты от повторов; незавершённые события/задачи и request ID не удаляются. Отдельный срок удаления семейных заявок пока не задан владельцем: автоматического удаления бизнес-данных нет. Настройте внешний монитор (например, проверка heartbeat без контактов и секретов), который сообщает по независимому каналу о пропаже сервера. Telegram через неисправный VPN не может обеспечить мгновенный алерт.

## Файловая система локального проекта

В текущем workspace `/mnt/storage` режим файлов остаётся 0777 даже после chmod: Unix-права здесь не обеспечиваются. Поэтому локальная CLI-приёмка выполнена только с вымышленными данными, без токенов и ключей. Для реальных секретов/контактов используйте защищённый каталог на файловой системе с рабочими правами (например ext4 на сервере); запуск с секретами отвергает открытые env/JSON/каталог БД. Созданные здесь шаблоны unit устанавливаются на сервер через `install -m 0644`, а env — через `install -m 0640`, как показано выше.

## Администраторы, имена и филиалы

Первоначальные администраторы задаются через `ADMIN_TELEGRAM_IDS` в env. Каждый может добавлять других через «Админское меню → Администраторы», задавать имена и редактировать имена промоутеров. Добавленные роли, имена и подписки хранятся в SQLite. Администраторы работают со всеми филиалами; «Мои филиалы для уведомлений» ограничивает только уведомления по заявкам. Общие сбои Telegram/сервиса приходят всем. Обход проверки дублей запрещён.

Подробности меню и обновления текущего Docker-сервера: [четыре филиала](franchise-upgrade.md).
