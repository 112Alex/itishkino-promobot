# Docker на Intel Mac Mini 2011 / DietPi

## Подготовка

Установите Docker через `dietpi-software` (или [официальную инструкцию Debian](https://docs.docker.com/engine/install/debian/)). Нужен Compose 2.20+. Проверьте `docker version` и `docker compose version`. Если пользователь ещё не имеет доступа к Docker, выполняйте команды через `sudo`.

На Mac Mini установите Linux/DietPi amd64 и Docker Engine. Подготовленный образ рассчитан на Linux x86_64. Склонируйте публичный [репозиторий](https://github.com/112Alex/itishkino-promobot) в `/opt/itishkino-promobot` либо каталог своего пользователя; GitHub-токен не требуется.

Создайте `runtime/.env` и `runtime/config.json` по README. Файлы содержат секреты/права доступа; права 600, владелец UID/GID 10001. Каталог `runtime` — 700. Примеры запуска рассчитаны на обычную Linux-файловую систему с действующими Unix-правами. На некоторых внешних накопителях chmod не работает: храните runtime на системном диске.

Для локального `/mnt/storage`, где chmod не действует, разместите настройки отдельно. Compose поддерживает `PROMOBOT_RUNTIME_DIR` — это каталог файлов, а не переменная внутри контейнера:

```sh
sudo install -d -o 10001 -g 10001 -m 0700 /opt/itishkino-promobot-runtime
sudo install -o 10001 -g 10001 -m 0600 ~/.config/itishkino-promobot/.env /opt/itishkino-promobot-runtime/.env
sudo install -o 10001 -g 10001 -m 0600 ~/.config/itishkino-promobot/users-production.json /opt/itishkino-promobot-runtime/config.json
sudoedit /opt/itishkino-promobot-runtime/.env
export PROMOBOT_RUNTIME_DIR=/opt/itishkino-promobot-runtime
```

В этой копии env замените `DATABASE_PATH`, `CONFIG_PATH`, `BACKUP_PATH` на контейнерные пути из следующего раздела. Перед каждой командой Compose задавайте тот же `PROMOBOT_RUNTIME_DIR`, либо добавьте его в окружение своей оболочки. Исходные рабочие файлы не редактируйте для контейнерного запуска. На сервере с ext4 можно использовать обычный `./runtime` без этой переменной.

Приложение читает `.env` внутри контейнера, а не через `env_file` Compose. Секреты не попадают в образ. Контейнер работает без root, без дополнительных capabilities, с read-only корнем. `/data` — постоянный volume, `/tmp` — временная память. Входящие порты не публикуются.

## Готовый образ и ограничения ресурсов

Перенесите архив `itishkino-promobot-0.2.1-linux-amd64.tar.gz` и `SHA256SUMS` из каталога `outputs` текущего компьютера. На сервере, в каталоге с архивом:

```sh
sha256sum -c SHA256SUMS
docker load -i itishkino-promobot-0.2.1-linux-amd64.tar.gz
docker image inspect itishkino-promobot:0.2.1-amd64 --format '{{.Os}}/{{.Architecture}}'
```

Результат архитектуры: `linux/amd64`. После загрузки Compose использует этот образ без скачивания и сборки. Если архива нет, сборка и экспорт выполняются на другом Linux-компьютере командой `sh scripts/build-image.sh`. Собирать на Mac Mini не обязательно.

Compose задаёт 256 МБ RAM, 0,75 CPU и 64 процесса/потока; временный каталог ограничен 16 МБ. Один Python-процесс, SQLite без отдельного сервера БД; лог Docker ограничен тремя файлами по 10 МБ. Эти пределы относятся к боту, а операционной системе, Docker и VPN также нужна память. Постоянный volume `/data` размещайте на SSD; WAL и надёжность записи SQLite сохранены. На вашем Mac Mini производительность ещё нужно измерить на месте.

## Перенос с текущего компьютера

Скопируйте защищённые настройки из `~/.config/itishkino-promobot/` по SSH: `.env` и `users-production.json`. На сервере положите их в `runtime/.env` и `runtime/config.json`. В env замените только пути:

```dotenv
DATABASE_PATH=/data/prod/bot.sqlite3
CONFIG_PATH=/run/promobot/config.json
BACKUP_PATH=/data/backups
```

В `.env` также должны остаться `ADMIN_TELEGRAM_IDS` с ID Олега и Амира. Владелец указан отдельно в `ADMIN_TELEGRAM_ID`. Токен, email, API-ключ и реальные роли сохраняются. Промоутеры, добавленные в меню, хранятся в БД: переносите её, чтобы сохранить доступ. Старые промоутеры из JSON импортируются однократно при первом личном событии после обновления. Локальную копию бота обязательно остановите перед запуском серверной.

Если текущий бот работает в Docker, рабочая БД находится в volume. Подробный порядок с финальным backup после остановки и импортом без перезаписи: [перенос через Tailscale](tailscale-transfer.md). Для текущей локальной Docker-копии выполните `docker compose stop bot`, затем `docker compose run --rm bot backup`, скопируйте новую копию командой `docker compose cp bot:/data/backups/ИМЯ-КОПИИ.sqlite3 /защищённый/каталог/` и передайте её по SSH. Локальный `promobot --env .env backup` относится к прежнему запуску вне Docker и не копирует рабочий Docker volume.

Следующий вариант через `compose cp` допустим только при первой установке, когда целевого файла ещё нет:

```sh
docker compose create bot
docker compose cp /путь/к/копии.sqlite3 bot:/data/prod/bot.sqlite3
docker compose run --rm --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --entrypoint sh bot -c 'chown 10001:10001 /data/prod/bot.sqlite3 && chmod 600 /data/prod/bot.sqlite3'
docker compose run --rm bot network-check
docker compose run --rm bot doctor
docker compose up -d --no-build
```

`create` не запускает polling. Копировать нужно проверенную SQLite backup, а не отдельно живой `bot.sqlite3` без WAL. Для нового запуска без заявок шаг копирования пропускается. Идентификатор окружения, Telegram-бота и филиала должен остаться прежним.

## VPN и первая локальная проверка

Сначала включите VPN в операционной системе текущего компьютера, оставьте `TELEGRAM_PROXY=` пустым и выполните из контейнера:

```sh
docker compose run --rm bot network-check
```

Проверка не создаёт CRM-записей, не отправляет Telegram-сообщений и не забирает обновления. Успех: `crm: ok`, `telegram: ok`, `webhook_present: false`. Эта команда проверяет доступ именно из Docker: некоторые настольные VPN не маршрутизируют Docker bridge. Отчёт не доказывает выход через конкретный VPN. Если проверка не прошла, разберём маршруты/proxy перед polling.

После остановки прежней копии запустите `docker compose up -d --no-build`; все три администратора отправляют `/start`. В меню добавьте тестового промоутера по ID; для username проверьте приглашение, его `/start` и подтверждение. Проверка подключения сама по себе не создаёт тестовую карточку CRM.

Для следующего этапа нужен экспорт клиента из Amnezia с указанием протокола и версии. Ссылки `vpn://...`/`amnezia://...` не являются URL HTTP/SOCKS и не вставляются в `TELEGRAM_PROXY`. Конфигурация содержит приватный ключ: храните её отдельно от репозитория/образа. AWG 2 и AWG 3.1 требуют различной поддержки; после получения экспорта подбирается отдельный VPN-сервис и проверяются маршруты. Текущий архив содержит бот, а VPN-сервис пока не собран и не настроен.


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
