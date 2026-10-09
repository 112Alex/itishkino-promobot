"""Deterministic single-message intake; free comment text is never interpreted."""
import re
import shlex

from .domain import InputError, age, clean, name, phone, title, username

EXAMPLE = 'Мария Игорь 6 Василий 8 +79991234567 @username\nКомментарий со второй строки'
INSTRUCTIONS = ('Отправьте семью одним сообщением:\n' + EXAMPLE +
                '\n\nПервое имя — родитель, затем дети и возраст, в конце телефон или @username. '
                'Имена из нескольких слов заключайте в двойные кавычки. '
                'Вместо неизвестного возраста можно написать ?. '
                'В конце первой строки можно указать писать, звонить или писать/звонить '
                'и MAX, Telegram, WhatsApp. Всё со второй строки — комментарий. '
                'Корректная заявка сразу сохраняется и отправляется в выбранный филиал.')


def parse_message(text, settings):
    # Normalize line endings before cleaning, preserving comment paragraphs verbatim.
    text = clean(text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' '), multiline=True)
    header, _, comment = text.partition('\n')
    if len(comment) > settings.comment_limit:
        raise InputError(f'Комментарий должен быть не длиннее {settings.comment_limit} символов.')
    try:
        tokens = shlex.split(header)
    except ValueError:
        raise InputError('Закройте двойные кавычки вокруг имени.') from None
    if len(tokens) < 3:
        raise InputError('Нужны имя родителя, имя ребёнка и его возраст.')

    def person(value, limit=200):
        if any(c.isdigit() for c in value) or value.startswith(('@', '+')) or '://' in value:
            raise InputError('Имя должно быть текстом. Например: Мария Игорь 6.')
        return name(value, limit)

    data = {'parent': person(tokens[0], 50), 'children': [], 'phones': [],
            'usernames': [], 'messengers': [], 'comment': comment.strip()}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(('+', '@')) or re.match(r'(?i)^(?:https?://)?(?:www\.)?t\.me/', token) or token[0].isdigit():
            break
        if index + 1 >= len(tokens):
            raise InputError(f'После имени «{token}» укажите возраст ребёнка.')
        child = person(token)
        years = age('Не уточнили' if tokens[index + 1] == '?' else tokens[index + 1], settings)
        data['children'].append({'name': child, 'age': years})
        index += 2
    if not data['children']:
        raise InputError('Добавьте хотя бы одного ребёнка с возрастом.')

    preferences = {'писать': 'Только писать', 'звонить': 'Только звонить',
                   'писать/звонить': 'Можно оба способа', 'звонить/писать': 'Можно оба способа'}
    messengers = {'max': 'MAX', 'макс': 'MAX', 'telegram': 'Telegram', 'телеграм': 'Telegram',
                  'whatsapp': 'WhatsApp', 'ватсап': 'WhatsApp'}
    while index < len(tokens):
        token, lower = tokens[index], tokens[index].lower().strip(',;')
        if lower in preferences:
            if 'preference' in data and data['preference'] != preferences[lower]:
                raise InputError('Укажите один способ связи: писать, звонить или писать/звонить.')
            data['preference'] = preferences[lower]
            index += 1
            continue
        if lower in messengers:
            if messengers[lower] not in data['messengers']:
                data['messengers'].append(messengers[lower])
            index += 1
            continue
        if token.startswith('@') or re.match(r'(?i)^(?:https?://)?(?:www\.)?t\.me/', token):
            value, field = username(token.rstrip(',;')), 'usernames'
            if 'Telegram' not in data['messengers']:
                data['messengers'].append('Telegram')
            index += 1
        elif re.fullmatch(r'[+\d()\-]+[,;]?', token):
            pieces = [token.rstrip(',;')]
            index += 1
            # Formatted phones such as 8 (999) 123-45-67 and +44 20 7946 0958.
            while index < len(tokens) and re.fullmatch(r'[\d()\-]+[,;]?', tokens[index]):
                if token.endswith((',', ';')) or re.fullmatch(r'\d{10,15}', tokens[index]):
                    break
                pieces.append(tokens[index].rstrip(',;'))
                ended = tokens[index].endswith((',', ';'))
                index += 1
                if ended:
                    break
            value, field = phone(' '.join(pieces)), 'phones'
        else:
            raise InputError(f'Не удалось распознать «{token}». Контакты пишите в первой строке, комментарий — со второй.')
        if value not in data[field]:
            if len(data[field]) >= 2:
                raise InputError('Можно указать до двух телефонов и двух Telegram-контактов.')
            data[field].append(value)
    if not (data['phones'] or data['usernames']):
        raise InputError('Добавьте телефон или @username в конец первой строки.')
    data.setdefault('preference', 'Можно оба способа' if data['phones'] else 'Только писать')
    if not data['phones'] and data['preference'] != 'Только писать':
        raise InputError('Для звонков нужен телефон. Укажите телефон или способ связи писать.')
    if not data['phones'] and any(m != 'Telegram' for m in data['messengers']):
        raise InputError('Для MAX и WhatsApp укажите телефон.')
    heading = title(data)
    if len(heading) > settings.name_limit:
        # Full names remain in the communication; only the CRM heading is shortened.
        data['title'] = heading[:settings.name_limit - 1].rstrip() + '…'
    return data
