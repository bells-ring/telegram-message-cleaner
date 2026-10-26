import asyncio
import configparser
import re
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.types import User, Chat, Channel, ReactionPaid, UpdateMessageReactions
from telethon.tl.functions.messages import SendReactionRequest, GetMessagesReactionsRequest
from telethon.errors import (
    FloodWaitError, MessageDeleteForbiddenError, ChatAdminRequiredError,
    SessionPasswordNeededError, MessageIdInvalidError, MsgIdInvalidError,
    MessageNotModifiedError, ReactionInvalidError, ChatWriteForbiddenError,
    UserBannedInChannelError, ChannelPrivateError, PeerIdInvalidError,
)
from getpass import getpass


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read('config.ini', encoding='utf-8')
    return cfg


def parse_whitelist(cfg):
    raw = cfg.get('whitelist', 'chats', fallback='')
    return [
        int(x) if x.lstrip('-').isdigit() else x
        for x in raw.split(',') if x.strip()
    ]


def parse_msg_whitelist_entries(cfg):
    raw = cfg.get('message_whitelist', 'messages', fallback='')
    return [x.strip() for x in raw.split(',') if x.strip()]


async def resolve_msg_whitelist(client, entries):
    result = {}
    for entry in entries:
        m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', entry)
        if m:
            chat_id = int('-100' + m.group(1))
            msg_id = int(m.group(2))
            result.setdefault(chat_id, set()).add(msg_id)
            continue

        m = re.match(r'https?://t\.me/([a-zA-Z][^/]+)/(\d+)$', entry)
        if m:
            username = m.group(1)
            msg_id = int(m.group(2))
            try:
                entity = await client.get_entity(username)
                chat_id = tl_utils.get_peer_id(entity)
                result.setdefault(chat_id, set()).add(msg_id)
            except Exception as e:
                print(f"  [!] message_whitelist: не удалось разрешить @{username}: {e}")
            continue

        if ':' in entry:
            parts = entry.split(':', 1)
            if parts[0].lstrip('-').isdigit() and parts[1].isdigit():
                result.setdefault(int(parts[0]), set()).add(int(parts[1]))
                continue

        print(f"  [!] message_whitelist: не распознан формат '{entry}'")

    return result


def fmt(d):
    e = d.entity
    name = (
        getattr(e, 'title', None)
        or ' '.join(filter(None, [getattr(e, 'first_name', ''), getattr(e, 'last_name', '')]))
        or '?'
    )
    username = getattr(e, 'username', None)
    s = f"id:{d.id}"
    if d.id != e.id:
        s += f"  uid:{e.id}"
    if username:
        s += f"  @{username}"
    s += f"  {name}"
    if bool(getattr(e, 'bot', False)):
        s += "  [бот]"
    return s


def fmt_entity(e):
    name = (
        getattr(e, 'title', None)
        or ' '.join(filter(None, [getattr(e, 'first_name', ''), getattr(e, 'last_name', '')]))
        or '?'
    )
    username = getattr(e, 'username', None)
    s = f"uid:{e.id}"
    if username:
        s += f"  @{username}"
    s += f"  {name}"
    return s


def parse_date(prompt):
    while True:
        s = input(prompt).strip()
        if not s:
            return None
        for fmt_str in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
            try:
                return datetime.strptime(s, fmt_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        print(f"  [!] Формат не тот: нужно ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ, введено '{s}'. Попробуй ещё раз или Enter для пропуска.")


def ask_dates():
    print("\nФильтр по датам UTC (Enter — пропустить):")
    while True:
        min_date = parse_date("  Не раньше (ДД.ММ.ГГГГ [ЧЧ:ММ]): ")
        max_date = parse_date("  Не позже  (ДД.ММ.ГГГГ [ЧЧ:ММ]): ")
        if min_date and max_date and min_date > max_date:
            print("  [!] Начало позже конца. Введи даты заново.")
            continue
        return min_date, max_date


def matches_type(d, selected):
    e = d.entity
    if 1 in selected and isinstance(e, User):
        return True
    if 2 in selected and isinstance(e, Chat):
        return True
    if 3 in selected and isinstance(e, Channel) and e.megagroup:
        return True
    if 4 in selected and isinstance(e, Channel) and not e.megagroup:
        return True
    return False


def pace_left(started, delay):
    # Пауза считается от начала запроса, а не от ответа: время ответа сервера входит в delay
    return max(0.0, delay - (time.monotonic() - started))


async def call_flood_safe(client, request, what):
    while True:
        try:
            return await client(request)
        except FloodWaitError as e:
            print(f"\n  FloodWait ({what}) {e.seconds + 2}с...")
            await asyncio.sleep(e.seconds + 2)


async def iter_history(client, entity, scan_delay, **kwargs):
    # iter_messages с продолжением после долгого FloodWait с того же места, а не с начала
    offset_id = 0
    while True:
        try:
            async for m in client.iter_messages(entity, offset_id=offset_id, wait_time=scan_delay, **kwargs):
                offset_id = m.id
                yield m
            return
        except FloodWaitError as e:
            print(f"\n  FloodWait (скан) {e.seconds + 2}с...")
            await asyncio.sleep(e.seconds + 2)
            if offset_id:
                kwargs.pop('offset_date', None)


async def delete(client, entity, ids, label, batch, delay):
    total = 0
    done = set()
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        retries = 0
        while retries < 3:
            started = time.monotonic()
            try:
                await client.delete_messages(entity, chunk)
                total += len(chunk)
                done.update(chunk)
                print(f"  [{label}] сообщения {total}/{len(ids)}", end='\r')
                await asyncio.sleep(pace_left(started, delay))
                break
            except FloodWaitError as e:
                wait = e.seconds + 2
                print(f"\n  FloodWait {wait}с...")
                await asyncio.sleep(wait)
            except (MessageDeleteForbiddenError, ChatAdminRequiredError):
                print(f"\n  [{label}] нет прав, пропускаю")
                return total, done
            except Exception as e:
                retries += 1
                print(f"\n  [{label}] ошибка ({retries}/3): {e}")
                if retries >= 3:
                    print(f"  [{label}] превышен лимит попыток, пропускаю чанк")
                    break
                await asyncio.sleep(2 * retries)
    return total, done


def reaction_state(r):
    """True — есть своя реакция, False — нет, None — Telegram прислал урезанные (min) данные."""
    if not r or not r.results:
        return False
    if any(rc.chosen_order is not None and not isinstance(rc.reaction, ReactionPaid) for rc in r.results):
        return True
    if any(p.my and not isinstance(p.reaction, ReactionPaid) for p in r.recent_reactions or ()):
        return True
    if r.min:
        return None
    return False


async def resolve_min_reactions(client, entity, ids):
    # Сомнительные включаем: лишний запрос дешевле пропущенной реакции
    mine = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            upd = await call_flood_safe(client, GetMessagesReactionsRequest(peer=entity, id=chunk), 'реакции')
        except Exception:
            mine.extend(chunk)
            continue
        states = {
            u.msg_id: reaction_state(u.reactions)
            for u in getattr(upd, 'updates', [])
            if isinstance(u, UpdateMessageReactions)
        }
        mine.extend(x for x in chunk if states.get(x) is not False)
    return mine


# Ошибки уровня чата: повтор на других сообщениях этого чата ничего не даст
CHAT_LEVEL_ERRORS = (
    ChatAdminRequiredError, ChatWriteForbiddenError, UserBannedInChannelError,
    ChannelPrivateError, PeerIdInvalidError,
)


async def unreact(client, entity, ids, label, delay):
    stats = {'ok': 0, 'noop': 0, 'gone': 0, 'fail': 0}
    chat_errors = 0
    for n, msg_id in enumerate(ids, 1):
        retries = 0
        while retries < 3:
            started = time.monotonic()
            try:
                # без reaction Telegram снимает все свои реакции с сообщения
                await client(SendReactionRequest(peer=entity, msg_id=msg_id))
                stats['ok'] += 1
                chat_errors = 0
                break
            except FloodWaitError as e:
                wait = e.seconds + 2
                print(f"\n  FloodWait {wait}с...")
                await asyncio.sleep(wait)
            except MessageNotModifiedError:
                stats['noop'] += 1
                chat_errors = 0
                break
            except (MessageIdInvalidError, MsgIdInvalidError):
                stats['gone'] += 1
                break
            except CHAT_LEVEL_ERRORS as e:
                stats['fail'] += 1
                chat_errors += 1
                if chat_errors >= 3:
                    print(f"\n  [{label}] {type(e).__name__} на 3 сообщениях подряд, пропускаю чат")
                    stats['fail'] += len(ids) - n
                    return stats
                break
            except ReactionInvalidError:
                stats['fail'] += 1
                break
            except Exception as e:
                retries += 1
                print(f"\n  [{label}] ошибка на {msg_id} ({retries}/3): {e}")
                if retries >= 3:
                    stats['fail'] += 1
                    break
                await asyncio.sleep(2 * retries)
        print(f"  [{label}] реакции {n}/{len(ids)}", end='\r')
        await asyncio.sleep(pace_left(started, delay))
    return stats


def count_str(mode, msg_ids, react_ids):
    if mode == 'msg':
        return f"{len(msg_ids):>5} сообщ."
    if mode == 'react':
        return f"{len(react_ids):>5} реакц."
    # Реакции на собственных сообщениях исчезнут вместе с ними
    own = set(msg_ids)
    return f"{len(msg_ids):>5} сообщ. + {sum(1 for x in react_ids if x not in own):>4} реакц."


def totals_str(mode, found):
    parts = []
    if mode in ('msg', 'both'):
        parts.append(f"Сообщений: {sum(len(m) for _, m, _, _ in found)}")
    if mode in ('react', 'both'):
        parts.append(f"Реакций: {sum(len(set(r) - set(m)) for _, m, r, _ in found)}")
    return '  |  '.join(parts)


async def run_deletion(client, found, opts, mode='msg'):
    if not found:
        print("\n[!] Ничего не нашлось.")
        return

    print(f"\n{'─'*60}")
    print(f"  Чатов: {len(found)}  |  {totals_str(mode, found)}")
    print(f"{'─'*60}")

    raw = input("\nИсключить номера (через запятую) или Enter: ").strip()
    if raw:
        skip = {int(x) for x in raw.split(',') if x.strip().isdigit()}
        found = [f for k, f in enumerate(found, 1) if k not in skip]
        print(f"  Осталось: {len(found)} чатов  |  {totals_str(mode, found)}")

    if not found:
        print("Нечего удалять.")
        return

    confirm = input("\n[?] Удаляем? [yes/N]: ")
    if confirm.strip().lower() != 'yes':
        return

    grand_msg = 0
    grand = {'ok': 0, 'noop': 0, 'gone': 0, 'fail': 0}
    for entity, msg_ids, react_ids, label in found:
        deleted = set()
        if msg_ids:
            n, deleted = await delete(client, entity, msg_ids, label, opts['batch'], opts['delay'])
            grand_msg += n
            print()
        # Реакции снимаем только там, где сообщение реально осталось
        todo = [x for x in react_ids if x not in deleted]
        if todo:
            st = await unreact(client, entity, todo, label, opts['react_delay'])
            for k in grand:
                grand[k] += st[k]
            print()

    print()
    if mode in ('msg', 'both'):
        print(f"[+] Удалено сообщений: {grand_msg}")
    if mode in ('react', 'both'):
        print(f"[+] Снято реакций: {grand['ok']}")
        extra = []
        if grand['noop']:
            extra.append(f"уже не было: {grand['noop']}")
        if grand['gone']:
            extra.append(f"сообщение удалено: {grand['gone']}")
        if grand['fail']:
            extra.append(f"не вышло: {grand['fail']}")
        if extra:
            print(f"    {', '.join(extra)}")


async def collect_dialog(client, entity, label, mode, opts, min_date=None, max_date=None, msg_whitelist=None):
    """-> (ids сообщений, ids сообщений с моей реакцией, сколько пропущено по whitelist)"""
    msg_ids, react_ids, skipped = [], [], set()
    try:
        chat_id = tl_utils.get_peer_id(entity)
    except Exception as e:
        print(f"  [!] {label}: {e}")
        return [], [], 0
    wl_ids = msg_whitelist.get(chat_id, set()) if msg_whitelist else set()
    base = {'offset_date': max_date} if max_date else {}

    if mode in ('msg', 'both'):
        # Серверный поиск по автору: первая же страница пустая — в чате нет моих сообщений
        try:
            async for m in iter_history(client, entity, opts['scan_delay'], from_user='me', **base):
                if max_date and m.date > max_date:
                    continue
                if min_date and m.date < min_date:
                    break
                if m.id in wl_ids:
                    skipped.add(m.id)
                else:
                    msg_ids.append(m.id)
        except Exception as e:
            print(f"  [!] {label}: скан сообщений прерван ({e}), найдено {len(msg_ids)}")

    if mode in ('react', 'both'):
        # Поиска по своим реакциям в API нет — листаем историю и смотрим флаг своей реакции
        unknown = []
        seen = 0
        try:
            async for m in iter_history(client, entity, opts['scan_delay'], **base):
                seen += 1
                if seen % 1000 == 0:
                    print(f"    ...{label}: просмотрено {seen}", end='\r', flush=True)
                if max_date and m.date > max_date:
                    continue
                if min_date and m.date < min_date:
                    break
                state = reaction_state(m.reactions)
                if state is False:
                    continue
                if m.id in wl_ids:
                    skipped.add(m.id)
                elif state:
                    react_ids.append(m.id)
                else:
                    unknown.append(m.id)
        except Exception as e:
            print(f"\n  [!] {label}: скан реакций прерван ({e}), найдено {len(react_ids) + len(unknown)}")
        if seen >= 1000:
            print(' ' * 100, end='\r')
        if unknown:
            react_ids += await resolve_min_reactions(client, entity, unknown)

    return msg_ids, react_ids, len(skipped)


async def source_session(client, msg_whitelist, whitelist, opts, mode='msg'):
    while True:
        print("\nЧто сканируем (можно несколько через запятую):")
        print("  0 - назад")
        print("  1 - личные сообщения")
        print("  2 - малые группы")
        print("  3 - супергруппы")
        print({
            'msg': "  4 - публикации от имени каналов",
            'react': "  4 - каналы",
            'both': "  4 - каналы (свои публикации + реакции)",
        }[mode])
        print("  5 - конкретные чаты")
        if mode != 'msg':
            print("  [i] Реакции ищутся перебором всей истории — сужай даты, иначе будет долго.")

        raw = input("Выбор: ").strip()
        if not raw or raw == '0':
            return

        try:
            selected = {int(x.strip()) for x in raw.split(',') if x.strip()}
        except ValueError:
            print("Что-то не то.")
            continue

        if not selected or not selected.issubset({1, 2, 3, 4, 5}):
            print("Нет такого варианта.")
            continue

        if 5 in selected:
            if len(selected) > 1:
                print("[!] Режим 5 нельзя комбинировать с другими.")
                continue
            raw_targets = input("Юзернеймы (без @) или ID через запятую (Enter - назад): ").strip()
            if not raw_targets:
                continue
            targets = [t.strip() for t in raw_targets.split(',') if t.strip()]
            min_date, max_date = ask_dates()
            print("\n[*] Сканирую...")
            found = []
            for target in targets:
                t = int(target) if target.lstrip('-').isdigit() else target
                try:
                    entity = await client.get_entity(t)
                    label = fmt_entity(entity)
                    msg_ids, react_ids, skipped = await collect_dialog(
                        client, entity, label, mode, opts,
                        min_date=min_date, max_date=max_date,
                        msg_whitelist=msg_whitelist
                    )
                    skipped_str = f"  [whitelist: {skipped}]" if skipped else ""
                    if msg_ids or react_ids:
                        found.append((entity, msg_ids, react_ids, label))
                        print(f"  {len(found):>3}.  {count_str(mode, msg_ids, react_ids)}  {label}{skipped_str}")
                    else:
                        print(f"        {count_str(mode, msg_ids, react_ids)}  {label}{skipped_str}")
                except Exception as e:
                    print(f"  [!] '{target}' не вышло: {e}")
            await run_deletion(client, found, opts, mode)
            continue

        bot_filter = None
        if 1 in selected:
            print("\nКого смотрим в личках:\n  0 - назад\n  1 - только люди\n  2 - только боты\n  3 - всех")
            bf = input("Выбор: ").strip()
            if bf == '0':
                continue
            if bf == '1':
                bot_filter = False
            elif bf == '2':
                bot_filter = True

        min_date, max_date = ask_dates()

        print("\n[*] Сканирую...\n")
        found = []

        async for d in client.iter_dialogs():
            if not matches_type(d, selected):
                continue
            if d.id in whitelist or getattr(d.entity, 'username', None) in whitelist:
                print(f"  [whitelist]  {fmt(d)}")
                continue
            if d.is_user and bot_filter is not None:
                if bool(getattr(d.entity, 'bot', False)) != bot_filter:
                    continue

            label = fmt(d)
            msg_ids, react_ids, skipped = await collect_dialog(
                client, d.entity, label, mode, opts,
                min_date=min_date, max_date=max_date,
                msg_whitelist=msg_whitelist
            )
            if msg_ids or react_ids:
                found.append((d.entity, msg_ids, react_ids, label))
                skipped_str = f"  [whitelist: {skipped}]" if skipped else ""
                print(f"  {len(found):>3}.  {count_str(mode, msg_ids, react_ids)}  {label}{skipped_str}")

        await run_deletion(client, found, opts, mode)


def show_qr(url):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print("  [!] Для QR в консоли: python -m pip install qrcode")
        print(f"  Ссылка (можно вставить в любой генератор QR): {url}")


async def login(client):
    await client.connect()
    if await client.is_user_authorized():
        return

    print("\nСессия не авторизована. Вход:\n  1 - по QR-коду (код в ТГ не нужен)\n  2 - по номеру телефона")
    if input("Выбор: ").strip() == '2':
        await client.start()
        return

    print("\nТелефон: Настройки → Устройства → Подключить устройство, отсканируй QR.")
    qr = await client.qr_login()
    while True:
        show_qr(qr.url)
        try:
            await qr.wait()
            return
        except asyncio.TimeoutError:
            print("\n  QR истёк, генерирую новый...")
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass("  Облачный пароль (2FA): "))
            return


async def main():
    cfg = load_config()
    api_id = cfg.getint('settings', 'api_id')
    api_hash = cfg.get('settings', 'api_hash')
    delay = cfg.getfloat('settings', 'delay')
    opts = {
        'batch': cfg.getint('settings', 'batch'),
        'delay': delay,
        'react_delay': cfg.getfloat('settings', 'react_delay', fallback=delay),
        'scan_delay': cfg.getfloat('settings', 'scan_delay', fallback=0.3),
    }
    whitelist = parse_whitelist(cfg)
    msg_entries = parse_msg_whitelist_entries(cfg)

    client = TelegramClient('session', api_id, api_hash)
    await login(client)
    async with client:
        me = await client.get_me()
        print(f"\n[+] Привет, {me.first_name}  (uid:{me.id})")

        msg_whitelist = await resolve_msg_whitelist(client, msg_entries)
        if msg_whitelist:
            total = sum(len(v) for v in msg_whitelist.values())
            print(f"  message_whitelist: {total} сообщ. в {len(msg_whitelist)} чатах")

        while True:
            print(
                "\nЧто делаем:\n  1 - удалить сообщения\n  2 - удалить реакции\n  3 - удалить сообщения и реакции"
                "\n  4 - выход\n  5 - завершить сессию удалятора и выйти"
            )
            src = input("Выбор: ").strip()

            if src == '1':
                await source_session(client, msg_whitelist, whitelist, opts)
            elif src == '2':
                await source_session(client, msg_whitelist, whitelist, opts, mode='react')
            elif src == '3':
                await source_session(client, msg_whitelist, whitelist, opts, mode='both')
            elif src == '4':
                print("Пока.")
                break
            elif src == '5':
                confirm = input("[?] Сессия будет завершена в Telegram, файл session.session удалён. Дальше нужен новый вход. [yes/N]: ")
                if confirm.strip().lower() != 'yes':
                    continue
                if await client.log_out():
                    print("[+] Сессия завершена, файл сессии удалён. Пока.")
                else:
                    print("[!] Telegram не принял выход. Заверши сессию вручную: Настройки → Устройства.")
                break
            else:
                print("Нет такого варианта.")


asyncio.run(main())