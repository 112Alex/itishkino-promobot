# Перенос на Mac Mini через Tailscale без установки клиента в основную ОС

Инструкция рассчитана на Linux/DietPi amd64 на Mac Mini, первую установку бота и текущую локальную версию 0.2.1 в Docker. Tailscale на сервере уже работает. Команды ниже выполняет владелец; серверный доступ ещё нужно проверить. На основной машине текущий бот продолжает работать до шага окончательного переноса.

## 1. Доступ из терминала

Запустите официальный Tailscale в отдельном контейнере с userspace networking. Он предоставляет локальный SOCKS5-прокси, не создаёт TUN-интерфейс основной ОС и не меняет её маршруты/DNS. Happ остаётся подключением основной машины; доступ контейнера в интернет всё равно зависит от её сети. [Официальная документация](https://tailscale.com/docs/concepts/userspace-networking).

На основном ПК:

```sh
docker run -d --name tailscale-ssh \
  --restart unless-stopped \
  -p 127.0.0.1:1055:1055 \
  -v tailscale-ssh-state:/var/lib/tailscale \
  --entrypoint tailscaled \
  tailscale/tailscale:stable \
  --tun=userspace-networking \
  --state=/var/lib/tailscale/tailscaled.state \
  --socket=/tmp/tailscaled.sock \
  --socks5-server=0.0.0.0:1055

docker exec -it tailscale-ssh \
  tailscale --socket=/tmp/tailscaled.sock up \
  --hostname=promobot-admin-pc \
  --accept-dns=false --accept-routes=false
```

Откройте выданную ссылку и войдите в аккаунт Tailscale, имеющий доступ к Mac Mini. Если tailnet требует одобрения устройства, разрешите новый `promobot-admin-pc` в Machines. Auth key, пароль Tailscale и SSH-ключ в репозиторий не нужны. При повторном подключении:

```sh
docker start tailscale-ssh
docker exec tailscale-ssh tailscale --socket=/tmp/tailscaled.sock status
```

На текущем основном ПК уже есть OpenBSD `nc`, `ssh` и `scp`; `nc` поддерживает SOCKS5. Добавьте в свой `~/.ssh/config` (не заменяйте существующие записи):

```sshconfig
Host school-mini
    HostName TAILSCALE_IP
    User SSH_USER
    Port 22
    ProxyCommand nc -X 5 -x 127.0.0.1:1055 %h %p
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Замените `TAILSCALE_IP` на адрес Mac Mini из Machines, `SSH_USER` на существующего пользователя сервера. Если SSH использует другой порт — замените 22. При обычном OpenSSH остаётся ваш пароль/ключ; доступ Tailscale не создаёт автоматически Linux-пользователя. При включённом Tailscale SSH также должны разрешать доступ правила tailnet. Не отключайте проверку SSH host key.

```sh
ssh school-mini
```

Передача файлов работает через тот же alias:

```sh
scp /путь/к/файлу school-mini:~/
```

Если соединение не удалось: проверьте `tailscale status`, одобрение устройства, доступ к порту SSH и маршрут Docker через Happ. MagicDNS основной ОС не требуется, поскольку используется IP.

## 2. Подготовка сервера — пока локальный бот работает

В SSH на Mac Mini:

```sh
uname -m
cat /etc/os-release
docker version
docker compose version
```

Нужны `x86_64`, Linux и Compose 2.20+. Если Docker ещё не установлен, поставьте его через `dietpi-software` либо [официальную инструкцию Debian](https://docs.docker.com/engine/install/debian/). Следующие команды предполагают, что SSH-пользователь имеет доступ к Docker; при необходимости используйте `sudo docker`.

Проверьте, что Docker volume будет на SSD:

```sh
docker info --format '{{.DockerRootDir}}'
findmnt -T "$(docker info --format '{{.DockerRootDir}}')"
lsblk -o NAME,ROTA,TYPE,MOUNTPOINTS
```

Размещение исходников на SSD само по себе не переносит туда Docker volume. Перенос существующего Docker data-root — отдельная операция; не меняйте его, пока не выяснили, какие другие контейнеры работают.

Получите проект и подготовьте закрытый каталог для передачи:

```sh
git clone https://github.com/112Alex/itishkino-promobot.git ~/itishkino-promobot
umask 077
mkdir -p ~/promobot-transfer
chmod 700 ~/promobot-transfer
```

На основном ПК, из каталога проекта:

```sh
scp outputs/itishkino-promobot-0.2.1-linux-amd64.tar.gz \
    outputs/SHA256SUMS school-mini:~/promobot-transfer/
```

На сервере:

```sh
cd ~/promobot-transfer
sha256sum -c SHA256SUMS
docker load -i itishkino-promobot-0.2.1-linux-amd64.tar.gz
```

## 3. Передать настройки и проверить серверную сеть

На основном ПК, из каталога проекта:

```sh
umask 077
transfer_dir="$HOME/.config/itishkino-promobot/transfer"
mkdir -p "$transfer_dir"
chmod 700 "$transfer_dir"
docker compose cp bot:/run/promobot/.env "$transfer_dir/.env"
docker compose cp bot:/run/promobot/config.json "$transfer_dir/config.json"
chmod 600 "$transfer_dir/.env" "$transfer_dir/config.json"
scp "$transfer_dir/.env" "$transfer_dir/config.json" school-mini:~/promobot-transfer/
```

Эти файлы — действующие Docker-настройки с контейнерными путями. Не заменяйте их примерами и не берите старую локальную БД из `.env` основного проекта: теперь рабочая база находится в Docker volume.

На сервере:

```sh
cd ~/itishkino-promobot
mkdir -p runtime
chmod 700 runtime
sudo install -o 10001 -g 10001 -m 0600 ~/promobot-transfer/.env runtime/.env
sudo install -o 10001 -g 10001 -m 0600 ~/promobot-transfer/config.json runtime/config.json
docker compose run --rm -e DATABASE_PATH=/tmp/preflight/bot.sqlite3 bot network-check
```

Диагностика использует временную SQLite в `/tmp`, поэтому место для переноса основной базы остаётся свободным. Она не отправляет Telegram-сообщения, не делает polling и не создаёт лидов. Успех: `crm: ok`, `telegram: ok`, `webhook_present: false`.

**Happ на основном ПК не даёт Mac Mini доступ к Telegram.** На сервере должен быть собственный рабочий выход. Если проверка Telegram не проходит, сначала настройте его VPN/Amnezia или Telegram SOCKS/HTTP proxy. Ссылка Amnezia не является значением `TELEGRAM_PROXY`; нужен отдельный VPN-клиент. До исправления сети оставьте локальный бот работающим.

## 4. Остановить локальный бот и перенести финальную базу

Этот шаг выполняйте только после успешной проверки серверной сети. На основном ПК, из каталога проекта:

```sh
docker compose stop bot
docker compose run --rm bot backup
```

Команда напечатает путь наподобие `/data/backups/20261005T204000179041Z.sqlite3`. Подставьте точное имя своей новой копии:

```sh
docker compose cp bot:/data/backups/ИМЯ-КОПИИ.sqlite3 "$transfer_dir/bot.sqlite3"
chmod 600 "$transfer_dir/bot.sqlite3"
scp "$transfer_dir/bot.sqlite3" school-mini:~/promobot-transfer/
```

Остановленный контейнер `bot` остаётся доступен для `compose cp`. Backup-команда использует тот же volume. Финальная копия сохраняет заявки, черновики, offset Telegram, права промоутеров и приглашения. Не копируйте отдельно живой `bot.sqlite3` без WAL.

## 5. Импорт базы на сервере — только первая установка

На сервере, из каталога проекта, импортируйте копию от UID 10001. Файл открывается исключительно на создание: уже существующую базу команда не перезапишет.

```sh
cd ~/itishkino-promobot
docker compose run --rm -T --entrypoint python bot -c '
import os, sys, shutil, sqlite3
p = "/data/prod/bot.sqlite3"
f = os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
try:
    shutil.copyfileobj(sys.stdin.buffer, f)
finally:
    f.close()
c = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
try:
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
finally:
    c.close()
print("Backup imported; integrity ok")
' < ~/promobot-transfer/bot.sqlite3
```

Если файл уже существует, не удаляйте его вслепую: проверьте, был ли импорт уже выполнен или на сервере есть другой рабочий экземпляр. При ошибке целостности бот не запускайте; выясните причину и передайте проверенную копию заново.

## 6. Запустить и проверить

На сервере:

```sh
docker compose run --rm bot network-check
docker compose run --rm bot doctor
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=30 bot
```

Откройте бота и проверьте `/start`, список промоутеров и сохранённые заявки. Заполненную тестовую анкету подтверждайте только если хотите настоящую запись CRM. Контейнер имеет `restart: unless-stopped`; на сервере проверьте автозапуск Docker и настройте [ежедневные копии](docker.md#backup). `down -v` удаляет volume и данные.

Одновременно с этим токеном работает один бот. После запуска серверного экземпляра локальную копию оставьте остановленной. Для отката сначала остановите серверный бот (`docker compose stop bot` на сервере), затем включайте локальный. Если сервер уже принимал новые заявки, перед откатом нужен его свежий backup — старую локальную базу нельзя считать актуальной.

После окончания SSH-сеанса локальный Tailscale-прокси можно остановить:

```sh
docker stop tailscale-ssh
```

Бот на Mac Mini от этого продолжает работать: этот контейнер нужен только для вашего административного подключения.

## Если нужен доступ совсем без контейнера

[Tailscale SSH Console](https://tailscale.com/docs/features/tailscale-ssh/tailscale-ssh-console) работает в браузере. Требует административной роли tailnet и включённого Tailscale SSH на сервере. Для обычного `ssh`/`scp` из терминала используйте Docker-вариант выше.
