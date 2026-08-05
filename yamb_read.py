#!/usr/bin/env python3
"""
Читатель Яндекс.Мессенджера (messenger.360.yandex.ru) на чистом stdlib.

Авторизация: куки залогиненного профиля/контейнера Firefox, домен .yandex.ru.
Транспорт: xiva WebSocket wss://push.yandex.ru/v2/subscribe/websocket (cookie-only handshake).
Чтение: WS-метод `history` с ChatId приватного чата.

Всё, что привязано к человеку и машине (профиль, контейнер, guid, uid), лежит в
yamb_config (~/.config/yandex-messenger/config.json), а не в коде — см. `yamb_config.py --init`.

Протокол снят реверсом веб-клиента (см. conarti/yandex-messenger-mcp) и HAR-записи.
"""
import os, re, ssl, socket, json, base64, hashlib, struct, sqlite3, shutil, tempfile, sys

import yamb_config

SERVICE = "messenger-prod"
WS_HOST = "push.yandex.ru"
WS_PORT = 443
WS_PATH = "/v2/subscribe/websocket"


# Идентичность читается из конфига ЛЕНИВО, функциями, а не константами модуля:
# импорт обязан проходить и без конфига — этим модулем пользуется сам `yamb_config --init`.
def my_guid():
    return yamb_config.require("my_guid")

def my_uid():
    return yamb_config.require("my_uid")

# --- 1. куки залогиненного профиля/контейнера Firefox ---
def load_cookie_jar(profile=None, container_id=None):
    profile = profile or yamb_config.require("firefox_profile")
    container_id = container_id if container_id is not None else yamb_config.require("container_id")

    con = yamb_config.open_cookies(profile)  # копия БД: Firefox может быть запущен
    where, params = yamb_config.container_clause(container_id)
    rows = con.execute(
        "SELECT name, value, host FROM moz_cookies "
        f"WHERE {where} "
        "AND (host = '.yandex.ru' OR host = 'push.yandex.ru' OR host = 'yandex.ru')",
        params,
    ).fetchall()
    con.close()
    # dedup по имени: при дублях берём последнее (обычно свежее)
    jar = {}
    for name, value, host in rows:
        jar[name] = value
    return jar

def load_cookies():
    return "; ".join(f"{k}={v}" for k, v in load_cookie_jar().items())

# --- 2. минимальный WebSocket-клиент (RFC6455) ---
class WS:
    def __init__(self, host, port, path, cookie, origin="https://yandex.ru"):
        raw = socket.create_connection((host, port), timeout=20)
        ctx = ssl.create_default_context()
        self.s = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            f"Cookie: {cookie}\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "\r\n"
        )
        self.s.sendall(req.encode())
        resp = self._read_http_headers()
        status = resp.split("\r\n", 1)[0]
        if "101" not in status:
            raise RuntimeError(f"handshake failed: {status}\n{resp[:500]}")
        self.buf = b""

    def _read_http_headers(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self._pending = rest
        return head.decode("latin1")

    def _recv(self, n):
        while len(self._pending) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._pending += chunk
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def send(self, data, opcode=0x2):
        # клиент ОБЯЗАН маскировать (RFC6455 §5.3)
        fin_op = 0x80 | opcode
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", fin_op, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", fin_op, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", fin_op, 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.s.sendall(hdr + mask + masked)

    def recv_frame(self):
        b0, b1 = self._recv(2)
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._recv(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._recv(8))[0]
        mask = self._recv(4) if masked else None
        payload = self._recv(ln) if ln else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload

    def close(self):
        try: self.s.close()
        except Exception: pass

# --- 3. кодек кадров xiva ---
def enc_uint(v):
    if v < 0x80: return bytes([v])
    if v < 0x100: return bytes([0xcc, v])
    if v < 0x10000: return bytes([0xcd, v >> 8, v & 0xff])
    return b"\xce" + struct.pack("!I", v)

def enc_str(v):
    b = v.encode()
    if len(b) < 0x20: return bytes([0xa0 | len(b)]) + b
    if len(b) < 0x100: return bytes([0xd9, len(b)]) + b
    return bytes([0xda]) + struct.pack("!H", len(b)) + b

def encode_data_frame(seq, method, payload):
    header = bytes([0x93]) + enc_uint(0) + enc_uint(seq) + enc_str(method)
    data_section = bytes([0x05]) + b"\x00" * 11
    return bytes([0x01]) + header + data_section + json.dumps(payload).encode()

def _dec_elem(buf, off):
    tag = buf[off]
    if tag < 0x80: return tag, off + 1
    if tag == 0xcc: return buf[off+1], off + 2
    if tag == 0xcd: return struct.unpack("!H", buf[off+1:off+3])[0], off + 3
    if tag == 0xce: return struct.unpack("!I", buf[off+1:off+5])[0], off + 5
    if (tag & 0xe0) == 0xa0:
        ln = tag & 0x1f; return buf[off+1:off+1+ln].decode(), off + 1 + ln
    if tag == 0xd9:
        ln = buf[off+1]; return buf[off+2:off+2+ln].decode(), off + 2 + ln
    if tag == 0xda:
        ln = struct.unpack("!H", buf[off+1:off+3])[0]; return buf[off+3:off+3+ln].decode(), off + 3 + ln
    raise ValueError(f"bad tag {tag:#x} at {off}")

def decode_data_frame(buf):
    frame_type = buf[0]
    arity = buf[1] & 0x0f
    off = 2
    elems = []
    for _ in range(arity):
        v, off = _dec_elem(buf, off)
        elems.append(v)
    json_start = off + 12  # data-section prefix
    payload = json.loads(buf[json_start:].decode()) if json_start < len(buf) else None
    return frame_type, elems, payload

# --- 4. основной сценарий ---
def resolve_chat_id(partner_or_chatid):
    """
    guid собеседника -> ChatId приватного чата; готовый id (с `_` или `/`) — как есть.

    Порядок guid в паре наблюдался в ОБОИХ вариантах, поэтому кандидат сверяется с
    registry-API (yamb_api.private_chat_id). Если API недоступен — падаем на пару,
    отсортированную по возрастанию: ей отвечают все известные живые чаты.
    """
    if "_" in partner_or_chatid or "/" in partner_or_chatid:
        return partner_or_chatid
    try:
        from yamb_api import private_chat_id  # ленивый импорт: yamb_api сам тянет этот модуль
        cid = private_chat_id(partner_or_chatid, my_guid())
        if cid:
            return cid
    except Exception as e:
        print(f"warn: резолв ChatId через registry не удался ({e}), беру пару по сортировке",
              file=sys.stderr)
    return "_".join(sorted((partner_or_chatid, my_guid())))

# --- треды: thread_id — чистая производная от родителя, сети тут нет ---
# Тред — это чат, и серверного «создания треда» не существует: кнопка «Обсудить» ничего
# не шлёт, id вычисляется строковой функцией из {ChatId родителя, Timestamp сообщения},
# а на сервере тред материализуется первым push в этот id.
_PRIVATE_CHAT_RE = re.compile(r"^[0-9a-f-]{36}_[0-9a-f-]{36}$", re.I)
_NUMERIC_CHAT_RE = re.compile(r"^(\d+)/(\d+)/(.+)$")
_THREAD_ID_RE = re.compile(r"^1(\d\d)/(\d+)/(.+)_(\d{16})$")

# для каких префиксов родителя деривация определена: 0 — группа, 1 — канал
_THREADABLE = {"0", "1"}

def build_thread_id(chat_id, timestamp):
    """
    thread_id из чата-родителя и метки родительского сообщения:
      приватный `<guid>_<guid>`  -> `110/0/<guid>_<guid>_<ts>`
      группа    `0/0/<uuid>`     -> `100/0/<uuid>_<ts>`
      канал     `1/0/<uuid>`     -> `101/0/<uuid>_<ts>`

    Radix 10, НЕ radix 2: в самом веб-клиенте написано `100+parseInt(prefix,2)`, что совпадает
    с десятичным лишь на префиксах 0/1, а на бизнес-чате `2/…` даёт NaN. Обратный парсер там
    же использует radix 10 — он и авторитетен. Префиксы вне {0,1} тредов не получают, поэтому
    отказ здесь явный, а не молчаливая кривая строка.
    """
    ts = str(int(timestamp))
    if len(ts) != 16:
        raise ValueError(f"метка родителя должна быть 16-значной (мкс), получено {ts!r}")
    if _PRIVATE_CHAT_RE.match(chat_id):
        return f"110/0/{chat_id}_{ts}"
    m = _NUMERIC_CHAT_RE.match(chat_id)
    if not m:
        raise ValueError(f"{chat_id!r} не распознан как чат — тред не выводится")
    prefix, ns, rest = m.groups()
    if prefix not in _THREADABLE:
        raise ValueError(
            f"тред для чата с префиксом {prefix!r} недоступен: деривация определена "
            "только для групп (0) и каналов (1)")
    return f"{100 + int(prefix)}/{ns}/{rest}_{ts}"

def parse_thread_id(thread_id):
    """Обратный разбор -> (chatId родителя, метка) либо None, если это не thread_id."""
    m = _THREAD_ID_RE.match(thread_id)
    if not m:
        return None
    code, ns, rest, ts = m.groups()
    # 110 (code=='10') — приватный тред: родитель это `rest` целиком
    parent = rest if code == "10" else f"{int(code)}/{ns}/{rest}"
    return parent, ts

def is_thread_id(value):
    return bool(_THREAD_ID_RE.match(value))

def connect_ws(cookie, uid=None):
    """
    Открывает xiva-сокет. Порядок параметров запроса — как у веб-клиента.
    uid обязателен: без него сервер рвёт соединение сразу после handshake.
    """
    session = base64.b16encode(os.urandom(8)).decode().lower()
    session = "-".join(session[i:i+4] for i in range(0, 16, 4))
    from urllib.parse import urlencode
    q = urlencode({
        "service": f"{SERVICE}:version5*common+version5*main",
        "session": session,
        "client": "web_main",
        "user": uid or my_uid(),
    })
    return WS(WS_HOST, WS_PORT, f"{WS_PATH}?{q}", cookie)

def read_history(partner_or_chatid, limit=10, with_chat_data=False, max_timestamp=None):
    """
    Страница истории. with_chat_data просит `ChatDataFilter:{}` — только с ним у треда
    приезжает ThreadParentMessage. max_timestamp — курсор вглубь (строго старше метки).
    """
    chat_id = resolve_chat_id(partner_or_chatid)

    cookie = load_cookies()
    if "Session_id" not in cookie:
        raise RuntimeError("нет Session_id в куках выбранного контейнера — сессия не залогинена")

    ws = connect_ws(cookie)

    seq = 1
    req_id = base64.b16encode(os.urandom(8)).decode().lower()
    params = {"RequestId": req_id, "Limit": limit, "ChatId": chat_id}
    if with_chat_data:
        params["ChatDataFilter"] = {}
    if max_timestamp is not None:
        params["MaxTimestamp"] = int(max_timestamp)
    frame = encode_data_frame(seq, "history", params)
    ws.send(frame, opcode=0x2)

    # читаем кадры, пока не придёт наш DATA-ответ (elems[1] == seq)
    result = None
    for _ in range(200):
        opcode, payload = ws.recv_frame()
        if opcode == 0x8:  # close
            raise ConnectionError(f"server closed: {payload[:200]!r}")
        if opcode == 0x9:  # ping -> pong
            ws.send(payload, opcode=0xA); continue
        if opcode == 0x1:  # текстовый операционный кадр (ping/subscribed)
            try:
                op = json.loads(payload.decode())
                if op.get("operation") == "ping":
                    pass
            except Exception:
                pass
            continue
        if opcode == 0x2:  # binary DATA
            try:
                ft, elems, pl = decode_data_frame(payload)
            except Exception:
                continue
            if ft == 0x01 and len(elems) >= 2 and elems[1] == seq:
                result = pl
                break
    ws.close()
    return chat_id, result

def read_thread(parent_or_thread_id, parent_ts=None, limit=20):
    """
    Открывает тред как обычный чат — отдельного метода чтения нет. Отдаёт
    (thread_id, parent_message, messages, empty).

    ENTITY_NOT_FOUND(4) здесь = тред ещё НЕ материализован (пуст), а не отказ доступа
    (это был бы ACCESS_DENIED(2)): тред появляется на сервере только с первым push в него.
    """
    if parent_ts is None:
        thread_id = parent_or_thread_id
        if not is_thread_id(thread_id):
            raise ValueError(f"{thread_id!r} не thread_id — нужна ещё метка родителя")
    else:
        thread_id = build_thread_id(resolve_chat_id(parent_or_thread_id), parent_ts)

    _, resp = read_history(thread_id, limit, with_chat_data=True)
    if not resp:
        raise RuntimeError("нет ответа на history треда")
    if resp.get("Status") == 4:
        return thread_id, None, [], True

    parent, msgs = None, []
    for ch in resp.get("Chats") or []:
        raw = ch.get("ThreadParentMessage")
        if raw:
            parent = raw.get("ServerMessage", raw)
        msgs.extend(ch.get("Messages") or [])
    return thread_id, parent, msgs, False

def fmt_ts(mcs):
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(mcs)/1_000_000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(mcs)

def extract(msg):
    sm = msg.get("ServerMessage", {})
    info = sm.get("ServerMessageInfo", {})
    ts = info.get("Timestamp")
    frm = info.get("From")
    if isinstance(frm, dict):
        frm = frm.get("Guid") or frm.get("Uid")
    cm = sm.get("ClientMessage", {})
    text = None
    if isinstance(cm, dict):
        plain = cm.get("Plain")
        if isinstance(plain, dict):
            text = plain.get("MessageText") or plain.get("Text")
    # распаковка вложенного {'MessageText': ...} до строки
    for _ in range(3):
        if isinstance(text, dict):
            text = text.get("MessageText") or text.get("Text")
        else:
            break
    return ts, frm, text, sm

def attachments(sm):
    """
    Рефы вложений из тела Plain. На проводе у файла только {Id2, Name, Size, Source} —
    ни url, ни бакета; качать по Id2 умеет yamb_api.download_attachment.
    Content-поле в теле одно, но Gallery несёт пачку — отсюда список.
    """
    plain = (sm.get("ClientMessage") or {}).get("Plain") or {}
    refs = []

    def add(kind, holder):
        fi = (holder or {}).get("FileInfo") or {}
        if fi.get("Id2"):
            refs.append({"kind": kind, "file_id": fi["Id2"], "name": fi.get("Name"),
                         "size": fi.get("Size")})

    add("image", plain.get("Image"))
    add("file", plain.get("MiscFile"))
    add("voice", plain.get("Voice"))
    for item in (plain.get("Gallery") or {}).get("Items") or []:
        add("gallery_image", item.get("Image"))
    return refs

def print_message(m, indent=""):
    """Печать одного сообщения: текст/надгробие + вложения + цитаты ответов."""
    ts, frm, text, sm = extract(m)
    who = "Я" if frm == my_guid() else (frm or "?")
    atts = attachments(sm)
    # удалённое остаётся в истории надгробием: контента нет, метка и From на месте
    if sm.get("ServerMessageInfo", {}).get("Deleted"):
        body = "<удалено>"
    elif text is None:
        body = "" if atts else "<нетекстовое сообщение>"  # стикер/опрос — не разбираем
    else:
        body = repr(text)
    # LastEditTimestamp есть и у удалённых (удаление — та же правка), поэтому не у них
    edited = sm.get("ServerMessageInfo", {}).get("LastEditTimestamp") and body != "<удалено>"
    print(f"{indent}[{fmt_ts(ts)}] {who}: {body}{' (изменено)' if edited else ''}")
    for a in atts:
        kb = f", {a['size'] / 1024:.0f} КБ" if a.get("size") else ""
        print(f"{indent}    📎 {a['kind']}: {a.get('name') or '?'}{kb}  file_id={a['file_id']}")
    # единственный признак «у сообщения есть тред»: списка тредов сервер не отдаёт
    thread = sm.get("ServerMessageInfo", {}).get("ThreadState")
    if thread:
        n = thread.get("LastSeqNo")
        last = thread.get("LastTsMcs")
        parent_chat = ((sm.get("ClientMessage") or {}).get("Plain") or {}).get("ChatId")
        tail = f", последнее {fmt_ts(last)}" if last else ""
        print(f"{indent}    🧵 тред: {n} сообщ.{tail}")
        if parent_chat and ts:
            try:
                print(f"{indent}       {build_thread_id(parent_chat, ts)}")
            except ValueError:
                pass
    # ответ/пересылка: сервер разворачивает ForwardedMessageRefs в ForwardedMessages
    for fwd in sm.get("ForwardedMessages") or []:
        info = fwd.get("ServerMessageInfo", {})
        src = (info.get("From") or {}).get("DisplayName") or "?"
        payload = fwd.get("Payload") or {}
        quoted = (payload.get("Text") or {}).get("MessageText")
        print(f"{indent}    ↳ в ответ на [{fmt_ts(info.get('Timestamp'))}] {src}: {quoted!r}")
        # у пересланного тело лежит прямо в Payload, а не под ClientMessage.Plain
        for a in attachments({"ClientMessage": {"Plain": payload}}):
            print(f"{indent}        📎 {a['kind']}: {a.get('name') or '?'}  file_id={a['file_id']}")


USAGE = """usage:
  yamb_read.py <partnerGuid|chatId> [limit]
  yamb_read.py --thread <parentChatId|guid> <parent_timestamp> [limit]
  yamb_read.py --thread <thread_id> [limit]"""

if __name__ == "__main__":
    yamb_config.friendly_errors()
    if len(sys.argv) > 1 and sys.argv[1] == "--thread":
        rest = sys.argv[2:]
        if not rest:
            print(USAGE); sys.exit(2)
        if is_thread_id(rest[0]):
            tid, parent_ts = rest[0], None
            limit = int(rest[1]) if len(rest) > 1 else 20
        else:
            if len(rest) < 2:
                print(USAGE); sys.exit(2)
            tid, parent_ts = rest[0], rest[1]
            limit = int(rest[2]) if len(rest) > 2 else 20
        thread_id, parent, msgs, empty = read_thread(tid, parent_ts, limit)
        print(f"ThreadId: {thread_id}")
        parsed = parse_thread_id(thread_id)
        if parsed:
            print(f"родитель: чат {parsed[0]}, сообщение {parsed[1]}")
        if parent:
            print("\nродительское сообщение:")
            print_message({"ServerMessage": parent}, indent="  ")
        if empty:
            print("\nтред пуст (ENTITY_NOT_FOUND): либо в него ещё не писали, "
                  "либо все сообщения удалены")
            sys.exit(0)
        print(f"\nсообщений в треде: {len(msgs)}\n")
        for m in msgs[-limit:]:
            print_message(m)
        sys.exit(0)

    if len(sys.argv) < 2:
        print(USAGE); sys.exit(2)
    target = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    chat_id, resp = read_history(target, limit)
    print(f"ChatId: {chat_id}")
    if not resp:
        print("нет ответа"); sys.exit(1)
    if resp.get("Status") not in (None, 0, 1):
        print("Status:", resp.get("Status"), "RAW:", json.dumps(resp)[:400]);
    chats = resp.get("Chats") or []
    msgs = []
    for ch in chats:
        for m in ch.get("Messages", []):
            msgs.append(m)
    print(f"сообщений в ответе: {len(msgs)}\n")
    if not msgs:
        print("RAW keys:", list(resp.keys()))
        print(json.dumps(resp)[:600])
    for m in msgs[-limit:]:
        print_message(m)
