import asyncio, os, configparser, re
from datetime import datetime, timezone
from telethon import TelegramClient, utils as tl_utils
from telethon.tl.types import User, Chat, Channel
from telethon.errors import FloodWaitError, MessageDeleteForbiddenError, ChatAdminRequiredError

_config = configparser.ConfigParser()
_config.read('config.ini', encoding='utf-8')

API_ID   = _config.getint('settings', 'api_id')
API_HASH = _config.get('settings', 'api_hash')
BATCH    = _config.getint('settings', 'batch')
DELAY    = _config.getfloat('settings', 'delay')

_raw_chats = _config.get('whitelist', 'chats', fallback='')
WHITELIST = [
    int(x) if x.lstrip('-').isdigit() else x
    for x in _raw_chats.split(',') if x.strip()
]

_raw_msgs = _config.get('message_whitelist', 'messages', fallback='')
_MSG_WHITELIST_ENTRIES = [x.strip() for x in _raw_msgs.split(',') if x.strip()]

async def resolve_msg_whitelist(client):
    result = {}
    for entry in _MSG_WHITELIST_ENTRIES:
        m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', entry)
        if m:
            chat_id = int('-100' + m.group(1))
            msg_id  = int(m.group(2))
            result.setdefault(chat_id, set()).add(msg_id)
            continue
        m = re.match(r'https?://t\.me/([a-zA-Z][^/]+)/(\d+)$', entry)
        if m:
            username = m.group(1)
            msg_id   = int(m.group(2))
            try:
                entity  = await client.get_entity(username)
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
        or '—'
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
        or '—'
    )
    username = getattr(e, 'username', None)
    s = f"uid:{e.id}"
    if username:
        s += f"  @{username}"
    s += f"  {name}"
    return s

def parse_date(prompt):
    s = input(prompt).strip()
    if not s:
        return None
    for fmt_str in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt_str).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    print(f"  [!] Формат не тот: нужно ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ, введено '{s}'")
    return None

def ask_dates():
    print("\nФильтр по датам UTC (Enter - пропустить):")
    min_date = parse_date("  Не раньше (ДД.ММ.ГГГГ [ЧЧ:ММ]): ")
    max_date = parse_date("  Не позже  (ДД.ММ.ГГГГ [ЧЧ:ММ]): ")
    if min_date and max_date and min_date > max_date:
        print("  [!] Начало позже конца - даты игнорирую")
        return None, None
    return min_date, max_date

def matches_type(d, selected):
    e = d.entity
    if 1 in selected and isinstance(e, User):                        return True
    if 2 in selected and isinstance(e, Chat):                        return True
    if 3 in selected and isinstance(e, Channel) and e.megagroup:     return True
    if 4 in selected and isinstance(e, Channel) and not e.megagroup: return True
    return False

async def delete(client, entity, ids, label):
    total = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        while True:
            try:
                await client.delete_messages(entity, chunk)
                total += len(chunk)
                print(f"  [{label}] {total}/{len(ids)}", end='\r')
                await asyncio.sleep(DELAY)
                break
            except FloodWaitError as e:
                print(f"\n  FloodWait {e.seconds + 2}с...")
                await asyncio.sleep(e.seconds + 2)
            except (MessageDeleteForbiddenError, ChatAdminRequiredError):
                print(f"\n  [{label}] нет прав, пропускаю")
                return total
            except Exception as e:
                print(f"\n  [{label}] ошибка: {e}, пропускаю")
                return total
    return total

async def run_deletion(client, found):
    if not found:
        print("\n[!] Ничего не нашлось.")
        return

    print(f"\n{'─'*60}")
    print(f"  Чатов: {len(found)}  |  Сообщений: {sum(len(i) for _, i, _ in found)}")
    print(f"{'─'*60}")

    raw = input("\nИсключить номера (через запятую) или Enter: ").strip()
    if raw:
        skip = {int(x) for x in raw.split(',') if x.strip().isdigit()}
        found = [(e, ids, n) for k, (e, ids, n) in enumerate(found, 1) if k not in skip]
        print(f"  Осталось: {len(found)} чатов, {sum(len(i) for _, i, _ in found)} сообщений")

    if not found:
        print("Нечего удалять.")
        return

    confirm = input("\n[?] Удаляем? [yes/N]: ")
    if confirm.strip().lower() != 'yes':
        return

    grand = 0
    for entity, ids, label in found:
        grand += await delete(client, entity, ids, label)
        print()

    print(f"\n[+] Готово, удалено: {grand}")

async def collect_dialog(client, entity, is_private=False, min_date=None, max_date=None, msg_whitelist=None):
    try:
        chat_id = tl_utils.get_peer_id(entity)
        wl_ids  = msg_whitelist.get(chat_id, set()) if msg_whitelist else set()

        kwargs = {'from_user': 'me'}
        if max_date:
            kwargs['offset_date'] = max_date

        if not is_private:
            probe = await client.get_messages(entity, limit=1, **kwargs)
            if not probe:
                return [], 0

        ids = []
        skipped = 0
        async for m in client.iter_messages(entity, **kwargs):
            if min_date and m.date < min_date:
                break
            if m.id in wl_ids:
                skipped += 1
            else:
                ids.append(m.id)
        return ids, skipped
    except Exception:
        return [], 0

async def source_session(client, msg_whitelist):
    while True:
        print("\nЧто сканируем (можно несколько через запятую):")
        print("  0 - назад")
        print("  1 - личные сообщения")
        print("  2 - малые группы")
        print("  3 - супергруппы")
        print("  4 - публикации от имени каналов")
        print("  5 - конкретные чаты")

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
                    ids, skipped = await collect_dialog(
                        client, entity,
                        is_private=isinstance(entity, User),
                        min_date=min_date, max_date=max_date,
                        msg_whitelist=msg_whitelist
                    )
                    label = fmt_entity(entity)
                    skipped_str = f"  [whitelist: {skipped}]" if skipped else ""
                    if ids:
                        found.append((entity, ids, label))
                        print(f"  {len(found):>3}.  {label}: {len(ids)} сообщений{skipped_str}")
                    else:
                        print(f"        {label}: 0 сообщений{skipped_str}")
                except Exception as e:
                    print(f"  [!] '{target}' не вышло: {e}")
            await run_deletion(client, found)
            continue

        bot_filter = None
        if 1 in selected:
            print("\nКого смотрим в личках:\n  0 - назад\n  1 - только люди\n  2 - только боты\n  3 - всех")
            bf = input("Выбор: ").strip()
            if bf == '0': continue
            if bf == '1': bot_filter = False
            elif bf == '2': bot_filter = True

        min_date, max_date = ask_dates()

        print("\n[*] Сканирую...\n")
        found = []

        async for d in client.iter_dialogs():
            if not matches_type(d, selected): continue
            if d.id in WHITELIST or getattr(d.entity, 'username', None) in WHITELIST:
                print(f"  [whitelist]  {fmt(d)}")
                continue
            if d.is_user and bot_filter is not None:
                if bool(getattr(d.entity, 'bot', False)) != bot_filter: continue

            ids, skipped = await collect_dialog(
                client, d.entity,
                is_private=isinstance(d.entity, User),
                min_date=min_date, max_date=max_date,
                msg_whitelist=msg_whitelist
            )
            if ids:
                label = fmt(d)
                found.append((d.entity, ids, label))
                skipped_str = f"  [whitelist: {skipped}]" if skipped else ""
                print(f"  {len(found):>3}.  {len(ids):>5} сообщ.  {label}{skipped_str}")

        await run_deletion(client, found)

async def main():
    async with TelegramClient('session', API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"\n[+] Привет, {me.first_name}  (uid:{me.id})")

        msg_whitelist = await resolve_msg_whitelist(client)
        if msg_whitelist:
            total = sum(len(v) for v in msg_whitelist.values())
            print(f"  message_whitelist: {total} сообщ. в {len(msg_whitelist)} чатах")

        while True:
            print("\nЧто делаем:\n  1 - удалить сообщения\n  2 - выход")
            src = input("Выбор: ").strip()

            if src == '1':
                await source_session(client, msg_whitelist)
            elif src == '2':
                print("Пока.")
                break
            else:
                print("Нет такого варианта.")

asyncio.run(main())