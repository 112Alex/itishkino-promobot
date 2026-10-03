# Docker на DietPi

## Подготовка

Установите Docker через `dietpi-software` (или [официальную инструкцию Debian](https://docs.docker.com/engine/install/debian/)). Нужен Compose 2.20+. Проверьте `docker version` и `docker compose version`. Если пользователь ещё не имеет доступа к Docker, выполняйте команды через `sudo`.

Склонируйте приватный репозиторий в `/opt/itishkino-promobot` либо каталог своего пользователя. Для приватного GitHub понадобится ваш обычный SSH-доступ или вход через `gh auth login`. Не включайте GitHub-токен в env бота.

Создайте `runtime/.env` и `runtime/config.json` по README. Файлы содержат секреты/права доступа; права 600, владелец UID/GID 10001. Каталог `runtime` — 700. Примеры запуска рассчитаны на обычную Linux-файловую систему с действующими Unix-правами. На некоторых внешних накопителях chmod не работает: храните runtime на системном диске.

Приложение читает `.env` внутри контейнера, а не через `env_file` Compose. Секреты не попадают в образ. Контейнер работает без root, без дополнительных capabilities, с read-only корнем. `/data` — постоянный volume, `/tmp` — временная память. Входящие порты не публикуются.

## Перенос с текущего компьютера

Скопируйте защищённые настройки из `~/.config/itishkino-promobot/` по SSH: `.env` и `users-production.json`. На сервере положите их в `runtime/.env` и `runtime/config.json`. В env замените только пути:

```dotenv
DATABASE_PATH=/data/prod/bot.sqlite3
CONFIG_PATH=/run/promobot/config.json
BACKUP_PATH=/data/backups
```

Токен, email, API-ключ, владелец и реальные роли сохраняются. Локальную копию бота обязательно остановите перед запуском серверной.

Если уже принимали реальные анкеты, сохраните БД. Остановите локальную копию, затем выполните локально `promobot --env .env backup` и передайте полученную SQLite-копию по SSH. Перед первым запуском восстановите её в volume контейнера:

```sh
docker compose build
docker compose create bot
docker compose cp /путь/к/копии.sqlite3 bot:/data/prod/bot.sqlite3
docker compose run --rm --user 0 --entrypoint sh bot -c 'chown 10001:10001 /data/prod/bot.sqlite3 && chmod 600 /data/prod/bot.sqlite3'
docker compose run --rm bot doctor
docker compose up -d
```

`create` не запускает polling. Копировать нужно проверенную SQLite backup, а не отдельно живой `bot.sqlite3` без WAL. Для нового запуска без заявок шаг копирования пропускается. Идентификатор окружения, Telegram-бота и филиала должен остаться прежним.

## VPN

Для `TELEGRAM_PROXY` доступен HTTP/SOCKS-proxy. Сервис на сервере должен быть достижим из Docker bridge, например `socks5://host.docker.internal:1080`. Proxy, слушающий только `127.0.0.1` хоста, из bridge недоступен: используйте адрес Docker bridge и ограничьте доступ firewall. Не открывайте proxy в интернет.

AWG и маршруты в образ не входят и автоматически не меняются. При прямой доступности Telegram оставьте поле пустым. Для существующей AWG-схемы см. [инструкцию](vpn.md). CRM использует прямое подключение даже при заданном Telegram proxy.

## Backup

```sh
docker compose run --rm bot backup
docker compose ps
```

Копии находятся в `/data/backups`. Команда сообщает имя проверенного файла. Его можно забрать наружу:

```sh
mkdir -p backups
chmod 700 backups
docker compose cp bot:/data/backups/ИМЯ-КОПИИ.sqlite3 backups/
chmod 600 backups/*.sqlite3
```

Храните копии также на другом диске/компьютере. В env `BACKUP_KEEP=14` ограничивает число копий внутри volume.

Ежедневный backup можно добавить в crontab пользователя с доступом к Docker (`crontab -e`). Замените путь на фактический:

```cron
15 3 * * * cd /opt/itishkino-promobot && /usr/bin/docker compose run --rm -T bot backup >> /var/log/promobot-backup.log 2>&1
```

Для обычного пользователя выберите доступный ему закрытый log-файл, например `~/promobot-backup.log`, а не `/var/log`.

## Обновление и остановка

```sh
docker compose run --rm bot backup
git pull --ff-only
docker compose build
docker compose up -d
docker compose logs --tail=50 bot
```

При миграции приложение дополнительно сохраняет копию БД. `restart: unless-stopped` запускает сервис после перезагрузки Docker, если вы не остановили его вручную. Проверьте `systemctl is-enabled docker` на сервере.

Остановка: `docker compose down`. Не используйте `down -v`, если данные ещё нужны. Не запускайте одновременно Docker и прежний systemd-сервис `promobot@prod` с тем же токеном. Не масштабируйте `bot` более чем до одного экземпляра.
