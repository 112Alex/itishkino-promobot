> Эта инструкция описывает прежний план AWG 2.0 для Linux/systemd. Для запуска в Docker используйте [отдельный AWG2/SOCKS-сервис](docker-vpn.md). План ниже не применяется автоматически. AWG 3.1 нельзя считать совместимым с клиентом AWG 2.0. [Официальный FAQ](https://docs.amnezia.org/faq/).

# DietPi / Debian 12: AWG 2.0 для Telegram Bot API

## Состояние

Сеть реального Mac Mini не проверена. Ссылка пользователя ранее определена как `amnezia-awg2`: это определение формата, не handshake. Приватные значения отсутствуют в проекте. Ни одна команда ниже здесь не запускалась с реальной VPN-конфигурацией. Installer бота не меняет маршруты.

Выбрана обслуживаемая схема: **бот и Alfa CRM работают в основной сети; только aiogram использует SOCKS5 proxy, расположенный в отдельном network namespace с default route через AWG**. Это позволяет обращаться к `api.telegram.org` по имени, не закрепляя один IP. Host SSH и маршрут CRM остаются прежними. Прокси приложения Telegram/MTProto для этого не подходит.

Вариант с namespace подготовлен как отдельные проверяемые шаги: адреса линка, native-конфигурация, DNS, маршрут endpoint и служба proxy зависят от реального сервера. До выполнения этих шагов `TELEGRAM_PROXY` оставьте пустым и не объявляйте разделение сети готовым.

## Версии и источник

Для исходной AWG2-конфигурации кандидаты: `amneziawg-go v0.2.19` и `amneziawg-tools v1.0.20260618`. Они взяты из официальных тегов, а не случайного установщика. Наличие `S3/S4`, диапазонов `H1–H4` и `I1–I5` сверяется в [UAPI go v0.2.19](https://github.com/amnezia-vpn/amneziawg-go/blob/v0.2.19/device/uapi.go) и [парсере tools v1.0.20260618](https://github.com/amnezia-vpn/amneziawg-tools/blob/v1.0.20260618/src/config.c). Это проверка исходного формата; сборка и работа этой пары на вашем ядре ещё не выполнены. Более новые версии добавляют AWG3-параметры; нельзя подменять ими пользовательскую AWG2-конфигурацию.

У go-версии [go.mod](https://github.com/amnezia-vpn/amneziawg-go/blob/v0.2.19/go.mod) требует Go 1.24.4. Стандартный Go из Debian 12 может быть старее: проверьте `go version`. Если нужна отдельная сборочная toolchain, скачивайте её с [официальной страницы Go](https://go.dev/dl/), выбирайте архитектуру по `uname -m`, сверяйте опубликованный SHA256. Не заменяйте системный Python и не ставьте GUI AmneziaVPN на сервер ради userspace.

После проверки архитектуры и инструментов, в отдельном каталоге сборки:

```sh
git clone https://github.com/amnezia-vpn/amneziawg-go.git
git -C amneziawg-go checkout --detach v0.2.19
git -C amneziawg-go rev-parse HEAD
# Запишите полный SHA в локальный акт сборки; go.sum проверяет зависимости.
(cd amneziawg-go && go mod verify && make)
git clone https://github.com/amnezia-vpn/amneziawg-tools.git
git -C amneziawg-tools checkout --detach v1.0.20260618
git -C amneziawg-tools rev-parse HEAD
make -C amneziawg-tools/src
```

Сверьте полный SHA с официальным тегом GitHub перед сборкой. Публичные подписанные checksum для каждой сборки не подтверждены здесь: собственный `sha256sum` сохраняет идентичность собранных файлов, но сам по себе не подтверждает происхождение. Если доступна официальная checksum выпуска, сравните с ней. После ревью сборки установите `amneziawg-go`, `awg`, `awg-quick` в `/usr/local/bin`, root:root 0755. Не создавайте замены `/usr/bin/wg`.

## Предварительные проверки и native-файл

```sh
sh scripts/server-preflight.sh
ip route show
ip -6 route show
# Вывод сетевых маршрутов проверять локально, не публиковать вместе с конфигурацией.
```

Проверьте RAM/CPU/ядро, `/dev/net/tun`, SSH, свободное место и действующие сетевые службы. Обеспечьте локальную консоль/второй SSH-сеанс для отката.

Предпочтительный способ получить native `.conf`: экспортировать AWG2-конфигурацию через официальный клиент AmneziaVPN на доверенном устройстве, затем перенести защищённым способом. Пользовательскую `vpn://` не передавайте сайту/поиску, не вставляйте в shell-аргумент. Универсальный декодер proprietary-ссылок здесь не объявляется совместимым. Форматы описаны в [официальной документации](https://docs.amnezia.org/documentation/supported-configuration-formats/).

Сохраните файл `/etc/amnezia/amneziawg/awg0.conf` с правами root:root 0600. Сохраните исходные `PrivateKey`, `PublicKey`, `PresharedKey`, Endpoint, AllowedIPs, Address, DNS, `Jc/Jmin/Jmax`, `S1–S4`, `H1–H4`, `I1–I5`. Не дополняйте отсутствующие параметры значениями из примеров. Создайте отдельную рабочую копию для namespace: `Table=off`, удалите route/DNS hooks, SaveConfig; DNS перенесите в отдельный namespace resolv.conf. Правки обфускации не делаются.

```sh
sudo python3 scripts/check-awg-config.py /etc/amnezia/amneziawg/awg0.conf
```

Проверка не печатает значения и не подключается к сети. Она отвергает неизвестные ключи/hooks и незакрытые права файла. `awg-quick strip` запускайте только после проверки файла, не печатайте stripped output: он содержит приватный ключ. Не используйте `awg showconf`/`show all dump` в отчётах.

## Проверка TUN и разделения сети

Шаблон `deploy/awg-userspace@.service` запускает `amneziawg-go -f awg0` и root-helper `scripts/promobot-awg-configure`, применяющий native-параметры через `awg setconf`. Он поднимает адреса интерфейса, **не устанавливает host default route и не меняет DNS**. Только эта сетевой служба имеет CAP_NET_ADMIN/CAP_NET_RAW. Установите helper root:root 0755 в `/usr/local/libexec/promobot-awg-configure`, unit — в `/etc/systemd/system`. Сначала `systemd-analyze verify`, затем пробный start, без enable до ручной приёмки. MTU из native-файла необходимо отдельно установить после проверки локального пути; helper не угадывает MTU.

Для раздельной маршрутизации примените следующие шаги в сетевой службе/отдельном root-helper, после локальной проверки адресов. Это пример плана, адреса подсети линка подбираются по таблице сервера:

1. Создать namespace `promo-vpn` и veth-пару `promo-host`/`promo-ns`; второй конец перенести внутрь. Выбрать не пересекающуюся локальную /30, например **только после проверки отсутствия конфликта** `10.203.0.0/30`: host `.1`, namespace `.2`. Поднять loopback и veth. IPv4 forwarding всего host не нужен для связи с proxy на соседнем veth.
2. Userspace AWG-процесс запускается в основной сети: его внешний UDP-сокет обращается к Endpoint через исходный host route. После конфигурации переместить **TUN-интерфейс**, а не процесс, в `promo-vpn`: `ip link set awg0 netns promo-vpn`. FD TUN остаётся у userspace-процесса. Это необходимо подтвердить на конкретном ядре/userspace-сборке: если процесс закрывает TUN или сокет после перемещения, остановиться и скорректировать службу, не менять host default route.
3. В namespace проверить Address, применить проверенный native MTU, включить интерфейс. Для предоставленного `AllowedIPs=0.0.0.0/0` установить `ip -n promo-vpn route replace default dev awg0`. Если экспорт содержит только ограниченные AllowedIPs, не расширять их молча — сначала выяснить допустимую конфигурацию. У veth остаётся только непосредственно подключённая /30; **не задавать namespace default через veth**, чтобы отказ VPN не дал обход туннеля.
4. При IPv6 default в VPN — настроить native IPv6 адрес, маршрут `::/0` и проверить HTTPS IPv6. Если IPv6 в туннеле отсутствует, отключить IPv6 **внутри namespace**, не на host, и проверить отсутствие обхода. Не полагаться на то, что DNS всегда вернёт IPv4.
5. `/etc/netns/promo-vpn/resolv.conf` должен использовать DNS, доступный через VPN. `ip netns exec` применяет этот файл; для systemd proxy нужны `NetworkNamespacePath=/run/netns/promo-vpn` и `BindReadOnlyPaths=/etc/netns/promo-vpn/resolv.conf:/etc/resolv.conf`. DNS host остаётся прежним. Проверьте namespace DNS через повторные запросы `api.telegram.org`, отсутствие прямого DNS-трафика и fallback через veth.
6. SOCKS5 proxy (например Debian `dante-server`) запускается только в namespace, слушает namespace veth IP:1080, разрешает client только с host veth IP. Исходящие соединения привязать к `awg0`, запретить внешний bind/UDP/BIND и служебные журналы с URL. Не создавать proxy на публичном адресе. Конкретный config/права пользователя Dante сверяются с установленной версией; Bot API требует TCP CONNECT. Namespace default/kill switch обеспечивает выход через AWG; proxy может разрешать CONNECT по имени без одноразовой фиксации Telegram IP.
7. В env бота установить `TELEGRAM_PROXY=socks5://ПРОВЕРЕННЫЙ_NS_IP:1080`. `AiohttpSession(proxy=...)` применяет proxy только к aiogram. Проверить `doctor --telegram-check` из-под `promobot`, в том же systemd-окружении. `httpx` для CRM остаётся в основной сети, `trust_env=False`, без proxy.

Сетевой helper должен при старте создавать namespace/veth идемпотентно, при stop удалять только свои объекты. Сохраните root-owned параметры в отдельном файле, не зашивайте секреты в unit. Шаблон AWG unit сам по себе не создаёт namespace/proxy: до исполнения этого плана сеть не готова. После установки сделайте override ExecStartPost с проверенным namespace-helper, добавьте namespace/proxy units с After/Requires и Restart=on-failure. Установить enable только после успешного пробного старта/остановки. При переносе интерфейса ExecStopPost должен удалять его через `ip -n promo-vpn link delete awg0`, а не host `ip link delete`; скорректируйте override.

## Приёмка и откат

- `awg show awg0 latest-handshakes` (без dump) должен показать свежий handshake; public key в этом выводе тоже не публикуйте без необходимости. При TUN в namespace awg control socket остаётся у host userspace-процесса.
- Из namespace: DNS и HTTPS `https://api.telegram.org` (401/404 без токена подтверждает только TLS/доступность, не авторизацию). Реальную авторизацию и сообщение владельцу проверяет doctor, без токена в argv/журнале curl.
- Из основной сети: CRM HTTPS напрямую; подтвердить маршруты/раздельный egress. Проверить IPv4/IPv6 и повтор после смены DNS-адресов.
- Остановить AWG: proxy не должен выйти через host route; бот сохраняет отложенные уведомления. CRM worker остаётся жив и может доставлять сохранённые заявки.
- Запустить AWG/proxy снова, проверить recovery; перезагрузить DietPi и проверить restart/autostart, SSH и один polling.
- Откат: убрать `TELEGRAM_PROXY` у тестового бота, остановить proxy и namespace/AWG units, удалить только выделенные veth/namespace; host default routes и DNS должны совпадать с исходными. Рабочего бота при недоступном Telegram держать с сохранённой очередью до решения связи.

[Официальная инструкция userspace](https://docs.amnezia.org/documentation/instructions/install-amneziawg-go/) объясняет установку, но выполнение команды установки не доказывает AWG2-совместимость и split routing. Раздача VPN другим устройствам в проект не входит.
