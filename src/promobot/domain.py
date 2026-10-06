import html
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import phonenumbers


class InputError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def moscow(value, seconds=True):
    pattern = "%d.%m.%Y %H:%M:%S МСК" if seconds else "%d.%m.%Y %H:%M"
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Europe/Moscow")).strftime(pattern)


def clean(value, multiline=False):
    return "".join(c for c in value if not unicodedata.category(c).startswith("C") or
                   (multiline and c == "\n")).strip()


def name(value, limit=200):
    value = clean(value)
    if not value or len(value) > limit or not any(c.isalpha() for c in value):
        raise InputError(f"Введите имя текстом, до {limit} символов")
    return value


def phone(value):
    value = clean(value)
    if not value.startswith("+") and not re.fullmatch(r"[\d\s()\-]+", value):
        raise InputError("Введите телефон, например +7 (999) 123-45-67")
    try:
        parsed = phonenumbers.parse(value, "RU" if not value.startswith("+") else None)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except (ValueError, phonenumbers.NumberParseException):
        raise InputError("Неверный формат телефона. Проверьте код страны и цифры") from None


def username(value):
    value = clean(value)
    value = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/", "", value, flags=re.I).lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
        raise InputError("Введите @username или https://t.me/username (5–32 символа)")
    return "@" + value.lower()


def age(value, settings):
    if value == "Не уточнили":
        return None
    if not re.fullmatch(r"\d{1,3}", value.strip()):
        raise InputError("Возраст — число полных лет или «Не уточнили»")
    n = int(value)
    if not settings.age_min <= n <= settings.age_max:
        raise InputError(f"Уточните возраст: ожидается {settings.age_min}–{settings.age_max}. Можно «Не уточнили»")
    return n


def title(data):
    return " ".join([data["parent"]] + [f'{c["name"]} {c["age"] if c["age"] is not None else "возраст не уточнили"}' for c in data["children"]])


def contact_keys(data):
    return set(["phone:" + p for p in data.get("phones", [])] +
               ["telegram:" + u.lower() for u in data.get("usernames", [])])


def family(data):
    return "\n".join(f'{i + 1}. {c["name"]} — {c["age"] if c["age"] is not None else "возраст не уточнили"}'
                     for i, c in enumerate(data["children"]))


def review(data, branch):
    return (f'Родитель: {data["parent"]}\nДети:\n{family(data)}\n'
            f'Контакты: {", ".join(data.get("phones", []) + data.get("usernames", []))}\n'
            f'Мессенджеры: {", ".join(data.get("messengers", [])) or "не указаны"}\n'
            f'Связь: {data["preference"]}\nКомментарий: {data.get("comment") or "нет"}\n'
            f'Заголовок: {data.get("title") or title(data)}\n'
            f'{branch["name"]} / {branch["pipeline_name"]} / {branch["status_name"]}\n'
            f'Источник: {branch.get("source_name") or "не настроен"}')


def crm_payload(request, settings):
    d, b = request["data"], settings.branch
    heading = d.get("title") or title(d)
    if len(heading) > settings.name_limit or len(d["parent"]) > 50:
        raise InputError("Слишком длинный заголовок/имя родителя для CRM")
    if d.get('crm_format') == 2:
        contact = {'Только писать': 'писать', 'Только звонить': 'звонить', 'Можно оба способа': 'писать/звонить'}
        try:
            preference = contact[d['preference']]
        except KeyError:
            raise InputError('Выберите способ связи: писать, звонить или писать/звонить') from None
        note = f'{moscow(request["confirmed_at"], seconds=False)}\n{preference}'
    else:
        # Keep old queued payloads stable, including ambiguous writes already sent to CRM.
        note = (f'{d["preference"]}: {", ".join(d.get("messengers", [])) or "мессенджер не указан"}\n'
                f'{d.get("comment", "")}\nРодитель: {d["parent"]}\n{family(d)}\n'
                f'Контакты: {", ".join(d.get("phones", []) + d.get("usernames", []))}\n'
                f'Заявка {request["id"]}; подтверждена {moscow(request["confirmed_at"])}')
    payload = {"name": html.escape(heading), "legal_name": html.escape(d["parent"]),
            "legal_type": 1, "is_study": 0, "branch_ids": [b["crm_id"]],
            "lead_status_ids": [b["status_id"]], "lead_source_id": b["source_id"],
            "assigned_id": None, "teacher_ids": [], "phone": d.get("phones", []),
            "web": ["https://t.me/" + u[1:] for u in d.get("usernames", [])],
            "note": html.escape(note), b["request_field"]: request["id"]}
    if b.get("initial_unassigned") is True:
        del payload["lead_status_ids"]
        payload["pipeline_id"] = b["pipeline_id"]
    return payload


def communication(request):
    data = request['data']
    if data.get('crm_format') != 2:
        return html.escape(data['comment']) + f'\n[promobot:{request["id"]}]'
    details = [data['comment']] if data.get('comment') else []
    details += [f'Родитель: {data["parent"]}', 'Дети:', family(data),
                'Контакты: ' + ', '.join(data.get('phones', []) + data.get('usernames', [])),
                'Мессенджеры: ' + (', '.join(data.get('messengers', [])) or 'не указаны'),
                'Связь: ' + data['preference']]
    return html.escape('\n'.join(details))


def telegram_chunks(text, limit=3500):
    # Telegram offsets use UTF-16. Keep astral characters together and leave room for the page heading.
    pages, current, units = [], [], 0
    for character in text:
        size = 2 if ord(character) > 0xffff else 1
        if units + size > limit:
            pages.append(''.join(current))
            current, units = [], 0
        current.append(character)
        units += size
    return pages + [''.join(current)]
