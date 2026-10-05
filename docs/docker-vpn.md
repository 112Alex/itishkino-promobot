# Telegram через AmneziaWG 2.0 в Docker

Это отдельный opt-in сервис. Бот остаётся в своей Docker-сети, его `TELEGRAM_PROXY` переопределяется на `socks5://vpn:1080`. HTTPX-клиент CRM продолжает обращаться напрямую. VPN не использует host networking и не меняет маршруты, DNS или Tailscale хоста. SOCKS не публикуется на портах сервера и разрешает только TCP CONNECT к порту 443.

Поддерживается native AWG2 `.conf` с IPv4 Address, IPv4 full-tunnel AllowedIPs и хотя бы одним IPv4 DNS. Параметры обфускации и ключи сохраняются. IPv6 выключен только внутри VPN-контейнера; IPv6 DNS из смешанного списка пропускается. AWG3 не поддерживается. Ссылка `vpn://` не является значением `TELEGRAM_PROXY`.

## Образ

На основной машине из корня репозитория:

```sh
sh scripts/build-vpn-image.sh
```

Получаются `outputs/itishkino-promobot-vpn-awg2-1-linux-amd64.tar.gz` и `outputs/VPN-SHA256SUMS`. Сборка требует интернета. Go-клиент v0.2.19 и tools v1.0.20260618 собраны из официальных репозиториев с проверкой полного SHA; базовые образы зафиксированы digest. Финальный образ не содержит компиляторов или пользовательской конфигурации.

Передайте оба файла на сервер тем же `scp`, которым переносили образ бота. В каталоге передачи:

```sh
sha256sum -c VPN-SHA256SUMS
docker load -i itishkino-promobot-vpn-awg2-1-linux-amd64.tar.gz
```

## Настройки и запуск только VPN

На сервере, root, из каталога проекта. `transfer/awg0.conf` — закрытый native-файл из вашей конфигурации:

```sh
install -o root -g root -m 600 transfer/awg0.conf runtime/awg0.conf
python3 scripts/prepare-vpn-dns.py runtime/awg0.conf runtime/vpn-resolv.conf
test -c /dev/net/tun
docker compose -f compose.yaml -f compose.vpn.yaml up -d --no-build vpn
docker compose -f compose.yaml -f compose.vpn.yaml ps vpn
docker compose -f compose.yaml -f compose.vpn.yaml logs --tail=10 vpn
docker compose -f compose.yaml -f compose.vpn.yaml run --rm \
  -e DATABASE_PATH=/tmp/preflight/bot.sqlite3 bot network-check
```

Если `/dev/net/tun` отсутствует, остановитесь и проверьте `modprobe tun` и поддержку TUN ядром DietPi. Контейнер требует устройства TUN и NET_ADMIN/NET_RAW только в собственной сети. SETUID/SETGID нужны Dante для непривилегированных workers. `privileged` и доступ к Docker socket ему не нужны.

Healthcheck подтверждает локальную SOCKS negotiation, **не handshake с VPN-сервером**. Готовность для бота подтверждает `network-check`: `telegram_proxy_configured: true`, `crm_proxy: false`, `crm: ok`, `telegram: ok`, `webhook_present: false`. Проверка использует временную базу и не запускает polling/отправку сообщений. Если VPN недоступен, firewall не разрешает прямой выход HTTPS/DNS через eth0. Ошибки запуска содержат только этап, без конфигурации и адресов.

Если `telegram: unavailable`, выполните безопасную диагностику без пересборки образа:

```sh
docker compose -f compose.yaml -f compose.vpn.yaml exec -T vpn \
  python3 - < scripts/vpn-diagnose.py
```

Она показывает возраст handshake, счётчики трафика и этап соединения SOCKS/TLS/HTTPS. Запрос идёт к публичному корню Telegram API без токена; сообщения не отправляются. `handshake_age_seconds: null`, растущий `sent_bytes` и нулевой `received_bytes` означают, что VPN-сессия пока не установлена. `socks_reply_code: 2` означает запрет правилами SOCKS; коды 3–5 — недоступную сеть/узел или отказ соединения. `connection: ok` подтверждает HTTPS через прокси; при таком результате и ошибке bot network-check отдельно проверяются настройки токена/API. Вывод не содержит ключей, VPN endpoint и URL с токеном.

## После переноса основной базы

Перенесите актуальную SQLite по [инструкции миграции](tailscale-transfer.md#4-остановить-локальный-бот-и-перенести-финальную-базу). Одновременно должен работать один polling-экземпляр.

После импорта используйте **оба** Compose-файла во всех командах:

```sh
docker compose -f compose.yaml -f compose.vpn.yaml run --rm bot doctor
docker compose -f compose.yaml -f compose.vpn.yaml up -d --no-build
docker compose -f compose.yaml -f compose.vpn.yaml logs --tail=30 bot
```

При недоступном VPN `TELEGRAM_PROXY` не убирайте ради автоматического обхода. CRM и очередь данных остаются в отдельном контейнере. Для изменения конфигурации перезапустите только VPN с `--force-recreate vpn`, затем повторите network-check. Выключить этот сервис можно командой `docker compose -f compose.yaml -f compose.vpn.yaml stop vpn`; это не изменит SSH/Tailscale хоста.

## Проверка реализации

Парсер тестируется на сохранении AWG-параметров, IPv4/IPv6 DNS и отказе от неподдерживаемых сетевых конфигураций. Перед эксплуатацией конкретной конфигурации нужны Docker smoke test, network-check и проверка отсутствия прямого выхода при отказе туннеля. Локальная проверка не подтверждает доступность VPN endpoint из сети школы.

Локальная проверка 2026-10-06: образ linux/amd64 собран, TUN и AWG setconf работают, SOCKS negotiation проходит, расход памяти около 25 МБ. При выключенном AWG-интерфейсе и восстановленном физическом default route прямой TCP/443 блокируется. CRM network-check проходит; VPN отправляет пакеты, но ответов и свежего handshake в локальной сети пока нет, поэтому Telegram через этот VPN ещё не подтверждён. На сервере требуется повторить проверку. Native-шаблон Amnezia содержит DNS placeholders: до передачи они заменяются адресами из того же import; пустые неактивные I-параметры пропускаются только при передаче в awg setconf.

[Официальные исходники AWG Go](https://github.com/amnezia-vpn/amneziawg-go/tree/v0.2.19), [tools](https://github.com/amnezia-vpn/amneziawg-tools/tree/v1.0.20260618), [настройка Dante](https://www.inet.no/dante/doc/1.4.x/config/server.html).
