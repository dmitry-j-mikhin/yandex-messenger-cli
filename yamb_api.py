#!/usr/bin/env python3
"""
HTTP registry-API Яндекс.Мессенджера (§3.1) на чистом stdlib.

POST на https://yandex.ru/messenger/api/registry/api/, тело — multipart/form-data
с единственным полем `request` = JSON `{method, params}`; авторизация — те же куки,
что у yamb_read.py. Ответ обёрнут в `{status, data}`.

Read-методы идут на голой куке; registry-МУТАЦИИ требуют заголовка `X-CSRF-TOKEN`
(csrf-token/ отдаёт `{token}` голым, без обёртки).

Нужен здесь ради одного: узнать НАСТОЯЩИЙ ChatId приватного чата. Порядок guid в нём
не «собеседник + я», как можно решить по одному примеру, а пара, сложившаяся при создании
чата; наблюдались оба порядка. Гадать нельзя — по неверной строке чат не находится
(`ENTITY_NOT_FOUND` на чтении, `NO_SUCH_CHAT` на отправке), поэтому кандидат сверяется
с `get_chats_info`.
"""
import http.client, json, mimetypes, os, re, sys, time, urllib.error, urllib.parse, urllib.request, uuid

import yamb_config
from yamb_read import load_cookie_jar, my_guid

API_URL = "https://yandex.ru/messenger/api/registry/api/"
CSRF_URL = "https://yandex.ru/messenger/api/registry/csrf-token/"
# приватный хост вложений: file_shortterm требует той же сессии, что и API
FILE_PRIVATE_HOST = "files.messenger.yandex.ru"
UPLOAD_TRIES = 5   # попыток загрузки файла (каждая с новым upload_url)


def _cookie_header():
    return "; ".join(f"{k}={v}" for k, v in load_cookie_jar().items())


def _headers(cookie):
    return {
        "Cookie": cookie,
        "Accept": "application/json",
        "Referer": "https://yandex.ru/chat",
        "User-Agent": "Mozilla/5.0",
    }


def csrf_token(cookie=None):
    cookie = cookie or _cookie_header()
    req = urllib.request.Request(CSRF_URL, data=b"", method="POST", headers=_headers(cookie))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["token"]


def call(method, params=None, csrf=False, cookie=None):
    """Вызывает registry-метод и отдаёт распакованный `data`."""
    cookie = cookie or _cookie_header()
    boundary = "----" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="request"\r\n\r\n'
        + json.dumps({"method": method, "params": params or {}})
        + f"\r\n--{boundary}--\r\n"
    ).encode()
    headers = _headers(cookie)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    if csrf:
        headers["X-CSRF-TOKEN"] = csrf_token(cookie)
    req = urllib.request.Request(API_URL, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            env = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # тело несёт {status:error, data:{code,text,source}} — без него 403 не диагностируется
        raise RuntimeError(f"registry {method}: HTTP {e.code} {e.read().decode()[:300]}") from None
    if env.get("status") != "ok":
        data = env.get("data") or {}
        raise RuntimeError(f"registry {method}: {data.get('code')} {data.get('text', '')}".strip())
    return env.get("data")


def user_info(guid):
    """`get_users_data` по guid: display_name, признак удалённого аккаунта, место работы."""
    users = (call("get_users_data", {"guids": [guid]}) or {}).get("users") or []
    return users[0] if users else None


def user_display_name(guid):
    u = user_info(guid) or {}
    emp = u.get("employee_info") or {}
    return emp.get("display_name") or u.get("display_name") or u.get("public_name") or guid


def chat_info(chat_id):
    """`get_chats_info`; None, если чата с таким id нет."""
    chats = (call("get_chats_info", {"chat_id": chat_id}) or {}).get("chats") or []
    return chats[0] if chats else None


def private_chat_id(partner_guid, me=None):
    """
    Настоящий ChatId приватного чата с partner_guid.

    Порядок guid в паре не выводится из ролей — сверяем обоих кандидатов с сервером.
    Наблюдения ложатся на сортировку по возрастанию, поэтому она проверяется первой,
    но правилом это не считаем: победитель тот, что подтверждён `get_chats_info`.
    """
    me = me or my_guid()
    if partner_guid == me:
        return f"{me}_{me}"
    a, b = sorted((partner_guid, me))
    for cid in (f"{a}_{b}", f"{b}_{a}"):
        if chat_info(cid):
            return cid
    return None


# --- треды -------------------------------------------------------------------

def join_thread(thread_id, cookie=None):
    """
    Подписка на тред (`{chat_member}`). Это НЕ создание: тред материализуется первым
    сообщением в него, а join лишь делает участника подписанным на обновления.
    Registry-мутация -> нужен CSRF.
    """
    return call("join_to_thread", {"thread_id": thread_id}, csrf=True, cookie=cookie)


def leave_thread(thread_id, cookie=None):
    """Отписка от треда (`{chat_member}`). Сам тред и его сообщения остаются."""
    return call("leave_thread", {"thread_id": thread_id}, csrf=True, cookie=cookie)


# --- вложения ---------------------------------------------------------------

# Id2 имеет форму `<bucket>/<uuid>` — СО СЛЭШЕМ ВНУТРИ. Целиком его через quote() гнать нельзя
# ('/' станет %2F и сервер ответит 404), но и подставлять как есть небезопасно: `../..` увёл бы
# URL на чужой путь. Поэтому не экранируем, а валидируем форму — что прошло, то URL-безопасно.
_FILE_ID_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_file_id(file_id):
    if not file_id:
        raise ValueError("file_id пуст: скачивать нечего")
    for seg in file_id.split("/"):
        if seg in (".", "..") or not _FILE_ID_SEGMENT.match(seg):
            raise ValueError(f"file_id недопустимой формы: {file_id!r} (ожидается <bucket>/<uuid>)")


def attachment_url(file_id, size=None):
    """
    URL вложения. На проводе у рефа нет ни url, ни бакета — только FileInfo.Id2,
    так что адрес целиком строит клиент.

    size (SMALL/SMALL48/MIDDLE2048/ORIGINAL) — превью картинки; без него — скачивание файла.
    ⚠️ живьём этот путь ресайз игнорирует и отдаёт оригинал независимо от size.
    """
    _check_file_id(file_id)
    base = f"https://{FILE_PRIVATE_HOST}/file_shortterm/{file_id}"
    return f"{base}?size={size}" if size else f"{base}?attach=true"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не глотаем: за ним нужно идти уже БЕЗ сессионной куки."""
    def redirect_request(self, *args):
        return None


def download_attachment(file_id, dest, size=None, cookie=None):
    """
    Качает вложение в dest (файл или каталог).

    `file_shortterm` отвечает 302 на storage-хост, и идти за редиректом автоматически нельзя:
    urllib утащил бы туда куку сессии — запрос виснет, а сама кука storage-хосту не нужна
    (ссылка уже подписана). Поэтому 302 снимаем сами и качаем по Location голым запросом.

    Имя файла авторитетно берут у FileInfo.Name (снаружи); Content-Disposition — только
    фоллбэк: наблюдались расхождения между ними.
    """
    cookie = cookie or _cookie_header()
    req = urllib.request.Request(attachment_url(file_id, size), headers=_headers(cookie))
    opener = urllib.request.build_opener(_NoRedirect)
    location, data, cd = None, None, ""
    try:
        with opener.open(req, timeout=60) as r:
            data = r.read()                       # прямой ответ байтами (редиректа не было)
            cd = r.headers.get("Content-Disposition") or ""
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise RuntimeError(f"file_shortterm: HTTP {e.code} {e.read()[:200]}") from None
        location = e.headers.get("Location")

    if data is None:
        if not location:
            raise RuntimeError("file_shortterm вернул редирект без Location")
        with urllib.request.urlopen(
            urllib.request.Request(location, headers={"User-Agent": "Mozilla/5.0"}), timeout=120
        ) as r:
            data = r.read()
            cd = r.headers.get("Content-Disposition") or ""
    if os.path.isdir(dest):
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)', cd)
        name = urllib.request.unquote(m.group(1)) if m else file_id.replace("/", "_")
        dest = os.path.join(dest, os.path.basename(name))
    with open(dest, "wb") as f:
        f.write(data)
    return dest, len(data)


def _put_bytes(upload_url, data, content_type, cookie):
    """PUT сырых байт -> заголовок Location. Редирект не глотаем: Location и есть результат."""
    req = urllib.request.Request(upload_url, data=data, method="PUT", headers={
        "Cookie": cookie,
        "Content-Type": content_type,
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            location = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        # 3xx на PUT: сам редирект и несёт нужный Location
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers.get("Location")
    if not location:
        raise RuntimeError("PUT не вернул заголовок Location")
    return location


def upload_file(chat_id, path, name=None, cookie=None):
    """
    Загрузка способом Disk, 3 шага:
      1. upload_to_disk {files:[{name, upload_id, size, chat_id}]} -> upload_url
      2. PUT сырых байт на upload_url -> заголовок Location
      3. add_files {chat_id, files:[{location}]} -> file_id
    Отдаёт (file_id, name, size) — их кладут в FileInfo сообщения.
    """
    cookie = cookie or _cookie_header()
    name = name or os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    size = len(data)

    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # Шаги 1-2 повторяем с новым upload_url: 12.10 сервер выдавал адрес на хосте, которого
    # нет в DNS (uploader93klg.disk.yandex.net - NXDOMAIN), или рвал соединение посреди PUT.
    # До add_files в чате ничего не появляется, так что повтор безопасен.
    for attempt in range(1, UPLOAD_TRIES + 1):
        # оба registry-шага загрузки — мутации: без X-CSRF-TOKEN сервер отдаёт 403 bad_csrf_token
        d = call("upload_to_disk", {"files": [
            {"name": name, "upload_id": str(uuid.uuid4()), "size": size, "chat_id": chat_id}
        ]}, csrf=True, cookie=cookie)
        upload_url = ((d.get("files") or [{}])[0]).get("upload_url")
        if not upload_url:
            raise RuntimeError(f"upload_to_disk без upload_url: {json.dumps(d, ensure_ascii=False)[:300]}")
        try:
            location = _put_bytes(upload_url, data, ctype, cookie)
            break
        except (OSError, http.client.HTTPException) as e:  # URLError - подкласс OSError
            if attempt == UPLOAD_TRIES:
                raise
            host = urllib.parse.urlparse(upload_url).hostname
            print(f"upload: попытка {attempt} на {host} не удалась ({type(e).__name__}: {e}), "
                  f"повтор через {attempt * 3} с", file=sys.stderr)
            time.sleep(attempt * 3)

    d = call("add_files", {"chat_id": chat_id, "files": [{"location": location}]},
             csrf=True, cookie=cookie)
    file_id = ((d.get("files") or [{}])[0]).get("id")
    if not file_id:
        raise RuntimeError(f"add_files без file_id: {json.dumps(d, ensure_ascii=False)[:300]}")
    return file_id, name, size


if __name__ == "__main__":
    yamb_config.friendly_errors()
    import sys
    if sys.argv[1] == "--download":
        dest = sys.argv[3] if len(sys.argv) > 3 else "."
        path, n = download_attachment(sys.argv[2], dest)
        print(f"скачано {n} байт -> {path}")
    else:
        guid = sys.argv[1]
        print("user:", json.dumps(user_info(guid), ensure_ascii=False, indent=1))
        print("chat_id:", private_chat_id(guid))
