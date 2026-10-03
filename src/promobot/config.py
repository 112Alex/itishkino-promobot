import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


@dataclass
class Settings:
    environment: str
    mode: str
    database: Path
    branch: dict
    promoters: dict[int, str]
    admin: int
    token: str = ""
    crm_url: str = "https://itishkino.s20.online"
    crm_email: str = ""
    crm_key: str = ""
    proxy: str | None = None
    timeout: float = 20
    interval: float = .25
    max_attempts: int = 6
    retry_base: float = 5
    retry_cap: float = 1800
    age_min: int = 3
    age_max: int = 18
    name_limit: int = 50
    comment_limit: int = 1500
    retention_days: int = 7
    backup_path: Path = Path("var/backups")
    backup_keep: int = 14
    administrators: dict[int, str] = field(default_factory=dict)

    @classmethod
    def load(cls):
        e = os.environ
        try:
            cfg = json.loads(Path(e.get("CONFIG_PATH", "config/mock.json")).read_text())
            s = cls(e.get("ENVIRONMENT", "test"), e.get("CRM_MODE", "mock"),
                    Path(e.get("DATABASE_PATH", "var/test/bot.sqlite3")), cfg["branch"],
                    {int(k): v for k, v in cfg["promoters"].items()},
                    int(e.get("ADMIN_TELEGRAM_ID", "0")), e.get("TELEGRAM_BOT_TOKEN", ""),
                    e.get("CRM_BASE_URL", "https://itishkino.s20.online"),
                    e.get("CRM_EMAIL", ""), e.get("CRM_API_KEY", ""),
                    e.get("TELEGRAM_PROXY") or None,
                    float(e.get("HTTP_TIMEOUT", "20")), float(e.get("CRM_REQUEST_INTERVAL", ".25")),
                    int(e.get("MAX_ATTEMPTS", "6")), float(e.get("RETRY_BASE", "5")),
                    float(e.get("RETRY_CAP", "1800")), cfg.get("age_min", 3), cfg.get("age_max", 18),
                    cfg.get("name_limit", 50), cfg.get("comment_limit", 1500),
                    int(e.get("INBOX_RETENTION_DAYS", "7")), Path(e.get("BACKUP_PATH", "var/backups")),
                    int(e.get("BACKUP_KEEP", "14")))
            s.administrators = {int(k): v for k, v in cfg.get("administrators", {}).items()}
        except (OSError, ValueError, KeyError, TypeError):
            raise ConfigurationError("Не удалось прочитать настройки; проверьте локальный env и JSON") from None
        s.validate()
        return s

    def validate(self):
        if self.environment not in {"test", "prod"} or self.mode not in {"mock", "real"}:
            raise ConfigurationError("ENVIRONMENT: test/prod; CRM_MODE: mock/real")
        if self.admin <= 0 or any(i <= 0 or b != self.branch["key"] for mapping in (self.promoters, self.administrators) for i, b in mapping.items()):
            raise ConfigurationError("Укажите числовые ID владельца и привязку промоутеров")
        if self.environment == "prod" and self.mode != "real":
            raise ConfigurationError("Для prod требуется real; тесты запускайте отдельно")
        if self.interval < .25 or self.max_attempts < 1 or self.timeout <= 0 or self.backup_keep < 1:
            raise ConfigurationError("Неверные таймауты/лимиты")
        u = urlsplit(self.crm_url)
        if self.mode == "real" and (u.scheme != "https" or u.username or u.password or u.query or u.fragment):
            raise ConfigurationError("CRM_BASE_URL должен быть HTTPS без секретов")

    def require_real_contract(self):
        b = self.branch
        required = ["verified_contract", "archive_visibility_verified", "contacts_verified",
                    "unassigned_verified", "communication_verified"]
        ids = ["crm_id", "pipeline_id", "source_id", "technical_user_id"]
        initial = b.get("initial_unassigned") is True and b.get("status_id") is None
        if not initial:
            ids.append("status_id")
        if not self.crm_email or not self.crm_key:
            raise ConfigurationError("CRM не настроена: установите секреты локально")
        if not all(b.get(k) is True for k in required) or not all(isinstance(b.get(k), int) and b[k] > 0 for k in ids):
            raise ConfigurationError("Реальная запись закрыта: подтвердите контракт и ID по инструкции приёмки")
        if not str(b.get("request_field", "")).startswith("custom_") or not b.get("source_name"):
            raise ConfigurationError("Проверьте источник и системное имя custom-поля")

    def is_admin(self, uid):
        return uid == self.admin or uid in self.administrators

    def authorized(self, uid):
        return self.is_admin(uid) or uid in self.promoters


def guard_permissions(settings, env_path, config_path):
    # Mock CLI without credentials can use synthetic data on a shared workspace.
    # Runtime/real credentials require a filesystem that actually enforces modes.
    if not (settings.token or settings.crm_key or settings.mode == "real"):
        return
    for path in (Path(env_path), Path(config_path)):
        if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o027:
            raise ConfigurationError("Файл настроек/секретов не защищён. Используйте 600 или root:promobot 640 на файловой системе с рабочими Unix-правами")
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(settings.database.parent, 0o700)
    if stat.S_IMODE(settings.database.parent.stat().st_mode) & 0o077:
        raise ConfigurationError("Каталог БД не защищён: нужны реально действующие права 700")
    if settings.database.exists() and stat.S_IMODE(settings.database.stat().st_mode) & 0o077:
        raise ConfigurationError("БД не защищена: перенесите её в закрытый каталог и установите права 600")
