#!/usr/bin/env python3
"""
Отправка сообщений в Яндекс.Мессенджер (messenger.360.yandex.ru) на чистом stdlib.

Транспорт и авторизация — те же, что у yamb_read.py (xiva WebSocket + куки Firefox из yamb_config).
Отправка: WS-метод `push`. Конверт:

    { ClientTransportId: {XivaSubscriptionId}, UserAgent, ClientSupportedFeatures,
      Meta: {Origin: <serviceId>},
      ClientMessage: { Plain: {ChatId, PayloadId, Text:{MessageText}}, LogData:{YandexUid} } }

XivaSubscriptionId — per-connection значение из операционного текст-кадра `subscribed`,
который сервер шлёт сразу после handshake. Самому его выдумывать нельзя: с пустым/чужим id
сообщение уходит в никуда.

PayloadId (uuid4) — ключ серверной дедупликации: повтор push с тем же id даёт DUPLICATE(8),
а не второе сообщение. Ретраев здесь НЕТ: push необратим.

Протокол снят реверсом веб-клиента (см. conarti/yandex-messenger-mcp).
"""
import json, os, struct, sys, uuid, base64

import yamb_config
from yamb_read import (
    load_cookie_jar, connect_ws, resolve_chat_id,
    encode_data_frame, decode_data_frame, fmt_ts,
)

USER_AGENT = "chats-web/3.22.0"
SERVICE_ID = 27            # Meta.Origin
SUPPORTED_FEATURES = 0     # битмаска; эфемерные сообщения не поддерживаем

# --- слой 3: commit-статус ответа на push ---
PUSH_STATUS = {
    0: "UNCOMMMITED", 1: "FULLY_COMMITTED", 2: "UNIPROXY_COMMITTED", 3: "FAILED",
    4: "NO_SUCH_CHAT", 5: "NOT_LEADING", 6: "FOREIGN_PARTITION", 7: "SENDER_NOT_IN_CHAT",
    8: "DUPLICATE", 9: "POSTPROC_COMMITTED", 10: "KIKIMR_WRITE_FAILED", 11: "MESSAGE_NOT_FOUND",
    12: "DEQUEUED_AFTER_ERROR", 13: "BAD_REQUEST", 14: "FILESHARE_FAILED", 15: "NO_PERMISSION",
    16: "CONFLICT", 17: "NO_SUCH_USER", 18: "THROTTLED", 19: "BANNED", 20: "BLACKLISTED",
    21: "NOT_FOUND", 22: "SPAM_DETECTED", 23: "RATE_LIMIT_EXCEEDED",
    24: "BLOCKED_BY_PRIVACY_SETTINGS", 25: "PUSH_UNAUTHORIZED",
}
# принято и повторять нельзя
COMMITTED = (1, 8)

# --- слой 1: транспортная ошибка из кадра PROXY_STATUS ---
TRANSPORT_ERROR = {
    0: "SUCCESS", 1: "PROTOCOL_ERROR", 2: "BACKEND_CALL_ERROR", 3: "INTERNAL_ERROR",
    4: "CORRUPTED_DATA_HEADER", 5: "BACKEND_NOT_FOUND", 6: "SERVICE_UNAVAILABLE",
    7: "TOO_MANY_REQUESTS", 8: "FRAME_TOO_LARGE",
}


def wait_subscribed(ws, tries=50):
    """Ждёт операционный текст-кадр `subscribed` и отдаёт его subscription-id."""
    for _ in range(tries):
        opcode, payload = ws.recv_frame()
        if opcode == 0x8:
            raise ConnectionError(f"сокет закрыт до subscribed: {payload[:200]!r}")
        if opcode == 0x9:
            ws.send(payload, opcode=0xA)
            continue
        if opcode == 0x1:
            try:
                op = json.loads(payload.decode())
            except Exception:
                continue
            if op.get("operation") == "subscribed":
                sid = op.get("subscription-id")
                if not sid:
                    raise RuntimeError(f"кадр subscribed без subscription-id: {op}")
                return sid
    raise RuntimeError("операционный кадр subscribed не пришёл")


def build_plain_text(chat_id, text, payload_id, reply_to=None, quote=None):
    """Вариант ClientMessage для текста. reply_to — Timestamp (мкс) цитируемого сообщения."""
    plain = {
        "ChatId": chat_id,
        "PayloadId": payload_id,
        "Text": {"MessageText": text},
    }
    if reply_to is not None:
        # метка цели уходит ЧИСЛОМ: строка на этом месте даёт BACKEND_CALL_ERROR
        plain["ForwardedMessageRefs"] = [{"ChatId": chat_id, "Timestamp": int(reply_to)}]
        if quote:
            # наличие Quote отличает reply от forward
            plain["ForwardedMessageStyles"] = [{"Quote": quote[:200]}]
    return {"Plain": plain}


FILE_SOURCE_DISK = 1  # FileInfo.Source: загрузка способом Disk


def build_file_info(file_id, name, size):
    return {"Id2": file_id, "Name": name, "Size": size, "Source": FILE_SOURCE_DISK}


def build_attachment(chat_id, payload_id, file_id, name, size, width=None, height=None):
    """
    Вложение: content-поле в Plain ровно одно — либо Image, либо MiscFile.
    Текст рядом с файлом не уезжает: подпись = отдельное сообщение.
    """
    if width and height:
        body = {"Image": {"Width": width, "Height": height,
                          "FileInfo": build_file_info(file_id, name, size)}}
    else:
        body = {"MiscFile": {"FileInfo": build_file_info(file_id, name, size)}}
    return {"Plain": {"ChatId": chat_id, "PayloadId": payload_id, **body}}


def build_edit(chat_id, timestamp, text):
    """
    Правка своего сообщения: тот же Plain с новым Text, но с Timestamp цели — это
    «переписать вот это», а не «отправить новое». Отличается от удаления только
    наличием content-поля. Правка пересобирает Plain целиком, поэтому всё, чего в ней
    нет (упоминания, цитата ответа), у сообщения СТИРАЕТСЯ.
    """
    return {"Plain": {"ChatId": chat_id, "Timestamp": int(timestamp),
                      "Text": {"MessageText": text}}}


def image_size(path):
    """
    Ширина/высота картинки из заголовка файла (png/gif/jpeg/webp), без внешних зависимостей.
    None -> отправляем как обычный файл: Image без размеров UI рисует криво.
    """
    with open(path, "rb") as f:
        head = f.read(65536)
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack("!II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            if head[12:16] == b"VP8 ":
                return struct.unpack("<HH", head[26:30])
            if head[12:16] == b"VP8L":
                b = int.from_bytes(head[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if head[12:16] == b"VP8X":
                w = int.from_bytes(head[24:27], "little") + 1
                h = int.from_bytes(head[27:30], "little") + 1
                return w, h
        if head[:2] == b"\xff\xd8":  # JPEG: идём по маркерам до SOFn
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker, seg = head[i + 1], struct.unpack("!H", head[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack("!HH", head[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except Exception:
        pass
    return None


def build_delete(chat_id, timestamp):
    """
    Удаление своего сообщения: пустой Plain с Timestamp цели и БЕЗ content-поля.
    Именно отсутствие контента при наличии метки означает «удалить это», а не «отправить».
    Метка уходит числом (строка даёт BACKEND_CALL_ERROR).
    """
    return {"Plain": {"ChatId": chat_id, "Timestamp": int(timestamp)}}


def push(partner_or_chatid, client_message, dry_run=False):
    """Гоняет готовый вариант ClientMessage методом `push`. Ретраев нет: push необратим."""
    chat_id = resolve_chat_id(partner_or_chatid)

    jar = load_cookie_jar()
    if "Session_id" not in jar:
        raise RuntimeError("нет Session_id в куках выбранного контейнера — сессия не залогинена")
    yandex_uid = jar.get("yandexuid", "")
    cookie = "; ".join(f"{k}={v}" for k, v in jar.items())

    client_message = dict(client_message, LogData={"YandexUid": yandex_uid})

    if dry_run:
        return chat_id, {"DryRun": True, "ClientMessage": client_message}

    ws = connect_ws(cookie)
    try:
        subscription_id = wait_subscribed(ws)

        params = {
            "RequestId": base64.b16encode(os.urandom(8)).decode().lower(),
            "ClientTransportId": {"XivaSubscriptionId": subscription_id},
            "UserAgent": USER_AGENT,
            "ClientMessage": client_message,
            "Meta": {"Origin": SERVICE_ID},
            "ClientSupportedFeatures": SUPPORTED_FEATURES,
        }
        seq = 1
        ws.send(encode_data_frame(seq, "push", params), opcode=0x2)

        for _ in range(200):
            opcode, payload = ws.recv_frame()
            if opcode == 0x8:
                raise ConnectionError(f"сервер закрыл сокет: {payload[:200]!r}")
            if opcode == 0x9:
                ws.send(payload, opcode=0xA)
                continue
            if opcode != 0x2:
                continue
            try:
                ft, elems, pl = decode_data_frame(payload)
            except Exception:
                continue
            if ft == 0x02 and len(elems) >= 2 and elems[0] == seq:
                code = elems[1]
                raise RuntimeError(
                    f"транспортная ошибка: {TRANSPORT_ERROR.get(code, code)} ({code})"
                )
            if ft == 0x01 and len(elems) >= 2 and elems[1] == seq:
                return chat_id, pl
        raise TimeoutError("ответ на push не пришёл")
    finally:
        ws.close()


def send_text(partner_or_chatid, text, reply_to=None, quote=None, dry_run=False):
    chat_id = resolve_chat_id(partner_or_chatid)
    cm = build_plain_text(chat_id, text, str(uuid.uuid4()), reply_to, quote)
    return push(chat_id, cm, dry_run=dry_run)


def delete_message(partner_or_chatid, timestamp, dry_run=False):
    chat_id = resolve_chat_id(partner_or_chatid)
    return push(chat_id, build_delete(chat_id, timestamp), dry_run=dry_run)


def edit_message(partner_or_chatid, timestamp, text, dry_run=False):
    chat_id = resolve_chat_id(partner_or_chatid)
    return push(chat_id, build_edit(chat_id, timestamp, text), dry_run=dry_run)


def send_file(partner_or_chatid, path, as_file=False, dry_run=False):
    """
    Файл: сперва 3-шаговая загрузка (байты уходят по HTTP, не в сокет), потом push
    с FileInfo. Картинка едет как Image, если удалось снять размеры; иначе — как MiscFile.
    """
    from yamb_api import upload_file  # ленивый импорт: yamb_api тянет yamb_read
    chat_id = resolve_chat_id(partner_or_chatid)
    dims = None if as_file else image_size(path)

    if dry_run:
        cm = build_attachment(chat_id, str(uuid.uuid4()), "<file_id>", os.path.basename(path),
                              os.path.getsize(path), *(dims or (None, None)))
        return push(chat_id, cm, dry_run=True)

    file_id, name, size = upload_file(chat_id, path)
    cm = build_attachment(chat_id, str(uuid.uuid4()), file_id, name, size, *(dims or (None, None)))
    return push(chat_id, cm)


USAGE = """usage:
  yamb_send.py <partnerGuid|chatId> <текст> [reply_to_timestamp] [--dry-run]
  yamb_send.py <partnerGuid|chatId> --file <путь> [--as-file] [--dry-run]
  yamb_send.py <partnerGuid|chatId> --edit <timestamp> <новый текст> [--dry-run]
  yamb_send.py <partnerGuid|chatId> --delete <timestamp> [--dry-run]"""

if __name__ == "__main__":
    yamb_config.friendly_errors()
    flags = {"--dry-run", "--as-file"}
    dry = "--dry-run" in sys.argv
    as_file = "--as-file" in sys.argv
    args = [a for a in sys.argv[1:] if a not in flags]
    if len(args) < 2:
        print(USAGE); sys.exit(2)

    target, mode = args[0], args[1]
    if mode in ("--delete", "--file", "--edit"):
        need = 4 if mode == "--edit" else 3
        if len(args) < need:
            print(USAGE); sys.exit(2)
    if mode == "--delete":
        action = "удалено"
        chat_id, resp = delete_message(target, args[2], dry_run=dry)
    elif mode == "--edit":
        action = "исправлено"
        chat_id, resp = edit_message(target, args[2], args[3], dry_run=dry)
    elif mode == "--file":
        action = "отправлено"
        chat_id, resp = send_file(target, args[2], as_file=as_file, dry_run=dry)
    else:
        action = "отправлено"
        reply_to = args[2] if len(args) > 2 else None
        chat_id, resp = send_text(target, mode, reply_to=reply_to, dry_run=dry)

    print(f"ChatId: {chat_id}")
    if not resp:
        print("нет ответа"); sys.exit(1)
    if resp.get("DryRun"):
        print(json.dumps(resp["ClientMessage"], ensure_ascii=False, indent=1)); sys.exit(0)
    status = resp.get("Status")
    name = PUSH_STATUS.get(status, f"UNKNOWN({status})")
    info = resp.get("MessageInfo") or {}
    ts = info.get("TimestampMcs") or info.get("Timestamp")
    print(f"Status: {status} {name}")
    if ts:
        print(f"Timestamp: {ts} ({fmt_ts(ts)})  SeqNo: {info.get('SeqNo')}")
    if status not in COMMITTED:
        print("RAW:", json.dumps(resp, ensure_ascii=False)[:600])
        sys.exit(1)
    print(action)
