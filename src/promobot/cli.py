import argparse
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from .config import ConfigurationError, Settings, guard_permissions
from .crm import AlfaCRM, CRMError
from .demo import acceptance
from .dialog import Dialog
from .domain import now
from .mock import MockCRM
from .storage import Store
from .telegram import ProcessLock, Runtime, make_bot
from .worker import Worker


async def bootstrap(crm, settings):
    if settings.mode == "real" and (not settings.crm_email or not settings.crm_key):
        print("CRM не настроена. Внесите секреты локально в env с правами 600.")
        return False
    b = settings.branch
    report = {"mode": settings.mode, "read_only": True, "dictionaries": {}, "unknown": []}
    for resource in ("branch", "lead-status", "lead-source", "pipeline"):
        path = "/v2api/branch/index" if resource == "branch" else crm.path(resource + "/index")
        if resource != "branch" and not b.get("crm_id"):
            report["unknown"].append(f'{resource}: сначала укажите проверенный crm_id филиала')
            continue
        try:
            data = await crm.index(path)
            report["dictionaries"][resource] = [{k: item[k] for k in ("id", "name", "pipeline_id") if k in item} for item in data]
        except CRMError as exc:
            report["unknown"].append(f'{resource}: {exc.code}')
    if b.get("crm_id"):
        try:
            sample = await crm.index(crm.path("customer/index"), {"id": 887, "is_study": 2, "removed": 1})
            if len(sample) == 1 and sample[0]["id"] == 887:
                x = sample[0]
                report["card_887"] = {"lead_source_id": x.get("lead_source_id"), "branch_ids": x.get("branch_ids"),
                                      "lead_status_ids": x.get("lead_status_ids"),
                                      "custom_field_candidates": [k for k in x if k.startswith("custom_")],
                                      "contact_types": {k: type(x.get(k)).__name__ for k in ("phone", "web", "note")}}
            else:
                report["unknown"].append("Карточка 887 недоступна; источник/поле не определены")
        except CRMError as exc:
            report["unknown"].append("card_887: " + exc.code)
    report["unknown"] += ["Название custom-поля сопоставить в настройках CRM: значения карточки не публикуются",
                          "Архивная видимость, контакты, создание без назначения и автор коммуникации требуют ручной приёмки",
                          "Отчёт не включает и не изменяет персональные данные; никаких записей CRM не выполнено"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return not report["unknown"]


async def health(db, crm, s, telegram_check=False):
    report = {"mode": s.mode, "environment": s.environment}
    report["inbox"] = await db.query("SELECT state,count(*) AS count FROM inbox GROUP BY state")
    report["crm_queue"] = await db.query("SELECT state,count(*) AS count FROM jobs GROUP BY state")
    report["outbox"] = await db.query("SELECT state,count(*) AS count FROM outbox GROUP BY state")
    meta = await db.query("SELECT key,value FROM metadata WHERE key IN ('last_poll','telegram_network')")
    report.update({x["key"]: x["value"] for x in meta})
    old = await db.query("SELECT min(r.saved_at) AS oldest FROM requests r JOIN jobs j ON j.request_id=r.id WHERE j.state!='done'")
    report["oldest_task_seconds"] = int((datetime.now(timezone.utc)-datetime.fromisoformat(old[0]["oldest"])).total_seconds()) if old[0]["oldest"] else None
    try:
        await crm.login()
        report["crm"] = "доступна; права и контракт отдельно проверяются bootstrap/приёмкой"
    except CRMError as exc:
        report["crm"] = exc.code
    if telegram_check:
        if not s.token:
            report["telegram"] = "не настроен"
        else:
            bot = make_bot(s)
            try:
                await bot.get_me()
                # Explicit doctor flag authorizes this diagnostic message to the owner.
                await bot.send_message(s.admin, "Проверка promobot: уведомления владельцу доступны", parse_mode=None)
                report["telegram"] = "getMe и уведомление владельцу: успешно"
            except Exception:
                report["telegram"] = "проверка не пройдена; проверьте VPN/токен и /start владельца"
            finally:
                await bot.session.close()
    else:
        report["telegram"] = "не проверено; doctor --telegram-check после /start владельца"
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def async_main(args):
    load_dotenv(args.env, override=False)
    s = Settings.load()
    guard_permissions(s, args.env, os.environ.get("CONFIG_PATH", "config/mock.json"))
    db = await Store(s.database).open()
    mock = crm = bot = None
    try:
        if s.mode == "mock":
            mock = await MockCRM(s.database.with_name("mock-crm.sqlite3"), s).open()
        crm = AlfaCRM(s, mock.client() if mock else None)
        if args.command == "bootstrap":
            await bootstrap(crm, s)
        elif args.command == "doctor":
            await health(db, crm, s, args.telegram_check)
        elif args.command == "backup":
            destination = s.backup_path / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
            await db.backup(destination)
            for p in sorted(s.backup_path.glob("*.sqlite3"))[:-s.backup_keep]:
                p.unlink()
            print(f"Проверенная резервная копия: {destination.resolve()}")
        elif args.command == "restore":
            source = Path(args.source)
            target = Path(args.target)
            if target.exists() or not source.is_file():
                raise ConfigurationError("Восстановление только в новый файл из существующей копии")
            await Store.restore(source, target)
            print(f"Восстановлено в новый файл: {target.resolve()}")
        elif args.command == "outbox-retry":
            # Local admin tool, no contact access or CRM writes.
            await db.execute("UPDATE outbox SET state='pending',next_at=0 WHERE state='held'")
            print("Остановленные уведомления возвращены в очередь")
        elif args.command == "mock-server":
            if not mock:
                raise ConfigurationError("mock-server доступен только CRM_MODE=mock")
            from aiohttp import web
            runner = web.AppRunner(mock.application(), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", args.port).start()
                print(f"Mock CRM: http://127.0.0.1:{args.port}; данные только локально", flush=True)
                stopped = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stopped.set)
                await stopped.wait()
            finally:
                await runner.cleanup()
        elif args.command == "integration-test":
            with ProcessLock(s.database.with_suffix(".lock")):
                await acceptance(db, s, crm, args.allow_real_write)
        elif args.command == "run":
            if not s.token:
                raise ConfigurationError("Telegram не настроен: внесите токен локально")
            if s.mode == "real":
                s.require_real_contract()
            bot = make_bot(s)
            # aiogram validates token format; numeric prefix is enough to lock before networking.
            bot_id = str(bot.id)
            with ProcessLock(s.database.with_suffix(".lock")), ProcessLock(Path("/tmp") / f"promobot-{os.getuid()}" / f"bot-{bot_id}.lock"):
                await db.bind(s.environment, bot_id, s.branch, s.mode, s.crm_url)
                worker = Worker(db, s, crm)
                runtime = Runtime(db, s, bot, Dialog(db, s, bot_id), worker)
                runner = asyncio.create_task(runtime.run())
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, runner.cancel)
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
        else:
            raise ConfigurationError("Неизвестная команда")
    finally:
        if bot:
            await bot.session.close()
        if crm:
            await crm.close()
        if mock:
            await mock.close()
        await db.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for lib in ("httpx", "httpcore", "aiogram", "aiohttp"):
        logging.getLogger(lib).setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser(description="Промобот Айтишкино")
    p.add_argument("--env", default=".env.test", help="Путь к защищённому env-файлу")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("run", "bootstrap", "backup", "outbox-retry"):
        sub.add_parser(command)
    server = sub.add_parser("mock-server")
    server.add_argument("--port", type=int, default=8081)
    integration = sub.add_parser("integration-test")
    integration.add_argument("--allow-real-write", action="store_true", help="Явно разрешить создание одной вымышленной тестовой семьи в настроенной CRM")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--telegram-check", action="store_true", help="Отправить диагностическое сообщение владельцу")
    restore = sub.add_parser("restore")
    restore.add_argument("--source", required=True)
    restore.add_argument("--target", required=True)
    args = p.parse_args()
    try:
        asyncio.run(async_main(args))
    except ConfigurationError as exc:
        p.exit(2, str(exc) + "\n")
    except Exception:
        # Avoid exception repr/tracebacks containing HTTP URLs, tokens or contact bodies.
        p.exit(1, "Сервис остановлен: operation_failed. Проверьте настройки, место на диске и доступность сети. Очередь сохранена до последней успешной транзакции.\n")


if __name__ == "__main__":
    main()
