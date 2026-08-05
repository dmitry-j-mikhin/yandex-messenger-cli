#!/usr/bin/env python3
"""
Конфиг клиента: всё, что привязано к конкретному человеку и машине, живёт ЗДЕСЬ,
а не в коде — чтобы сами скрипты можно было публиковать как есть.

Файл: ~/.config/yandex-messenger/config.json (путь переопределяется переменной YAMB_CONFIG).

    {
      "firefox_profile": "~/.mozilla/firefox/xxxxxxxx.default-release",
      "container_id": "7",          # userContextId контейнера с залогиненным Яндексом; "0" = без контейнера
      "my_guid": "<guid>",          # мой guid в мессенджере
      "my_uid": "<uid>"             # числовой uid паспорта
    }

Заполняется автоматически: `python3 yamb_config.py --init`. uid берётся из кук,
guid — из WS-метода `whoami`, профиль и контейнер определяются перебором.

В коде НЕТ значений по умолчанию для этих полей: неверный чужой guid молча уводит
запросы не в тот чат, а пустой конфиг с внятной ошибкой — нет.
"""
import json, os, sqlite3, shutil, sys, tempfile

CONFIG_ENV = "YAMB_CONFIG"
DEFAULT_CONFIG = "~/.config/yandex-messenger/config.json"
FIELDS = ("firefox_profile", "container_id", "my_guid", "my_uid")

_cache = None


def config_path():
    return os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(required=True):
    """Читает конфиг (с кэшем). required=False -> {} вместо ошибки, если файла нет."""
    global _cache
    if _cache is None:
        path = config_path()
        if not os.path.exists(path):
            if required:
                raise RuntimeError(
                    f"нет конфига {path} — запусти: python3 yamb_config.py --init")
            return {}
        with open(path, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def require(key):
    value = load().get(key)
    if not value:
        raise RuntimeError(
            f"в конфиге {config_path()} не заполнено поле {key!r} — "
            "запусти: python3 yamb_config.py --init")
    return value


def friendly_errors():
    """
    Ставится в CLI-точках входа: RuntimeError здесь — это почти всегда подсказка человеку
    («нет конфига», «сессия не залогинена»), и трейсбек её только прячет. Прочие исключения
    печатаются как обычно. На импорт модуля как библиотеки не влияет.
    """
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, RuntimeError):
            print(f"ошибка: {exc}", file=sys.stderr)
        else:
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


def save(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)  # в конфиге нет секретов, но есть идентичность
    global _cache
    _cache = data
    return path


# --- автоопределение ---------------------------------------------------------

def container_clause(container_id):
    """
    SQL-условие по контейнеру для moz_cookies.originAttributes.
    Контейнер 0 — это «без контейнера»: у таких кук userContextId в атрибутах ОТСУТСТВУЕТ,
    а не равен нулю, поэтому для него условие обратное, а не LIKE '%userContextId=0%'.
    """
    if str(container_id) in ("0", ""):
        return "originAttributes NOT LIKE '%userContextId=%'", ()
    return "originAttributes LIKE ?", (f"%userContextId={container_id}%",)


def open_cookies(profile):
    """Копия cookies.sqlite (+WAL/SHM), чтобы читать при запущенном Firefox."""
    src = os.path.join(os.path.expanduser(profile), "cookies.sqlite")
    if not os.path.exists(src):
        raise RuntimeError(f"в профиле нет cookies.sqlite: {src}")
    tmp = tempfile.mkdtemp()
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, os.path.join(tmp, "cookies.sqlite" + ext))
    return sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))


def find_profiles():
    root = os.path.expanduser("~/.mozilla/firefox")
    if not os.path.isdir(root):
        return []
    return sorted(os.path.join(root, d) for d in os.listdir(root)
                  if os.path.exists(os.path.join(root, d, "cookies.sqlite")))


def find_sessions(profile):
    """
    Все контейнеры профиля, где есть залогиненная сессия Яндекса.
    Отдаёт [(container_id, uid), ...] — uid вытаскивается из тех же кук.
    """
    con = open_cookies(profile)
    rows = con.execute(
        "SELECT originAttributes, name, value FROM moz_cookies "
        "WHERE host LIKE '%yandex.ru' "
        "AND name IN ('Session_id','yandex360session','yandex_login')"
    ).fetchall()
    con.close()

    found = {}
    for attrs, name, value in rows:
        cid = "0"
        for part in (attrs or "").lstrip("^").split("&"):
            if part.startswith("userContextId="):
                cid = part.split("=", 1)[1]
        entry = found.setdefault(cid, {"container_id": cid, "uid": None, "login": None})

        uid = None
        if name == "yandex360session" and value.isdigit():
            uid = value
        elif name == "Session_id" and "1|" in value:
            # Session_id вида `...:1|<uid>.<...>` — uid идёт сразу за разделителем
            tail = value.split("1|", 1)[1].split(".", 1)[0]
            if tail.isdigit():
                uid = tail
        elif name == "yandex_login" and value:
            entry["login"] = value  # только чтобы человек узнал свой аккаунт в списке

        if uid and not entry["uid"]:
            entry["uid"] = uid
    return [v for v in found.values() if v["uid"]]


def discover_identity(profile, container_id, uid):
    """
    Идентичность из WS-метода `whoami` -> (guid, uid). Подключиться без uid нельзя —
    сервер рвёт соединение сразу после handshake, поэтому uid для коннекта берётся из кук,
    а из ответа уже подтверждается.
    """
    import base64, os as _os
    # импорт здесь, а не сверху: yamb_read сам читает этот модуль
    import yamb_read as R

    cookie = "; ".join(f"{k}={v}" for k, v in R.load_cookie_jar(profile, container_id).items())
    if "Session_id" not in cookie:
        raise RuntimeError("в выбранном контейнере нет Session_id — сессия не залогинена")

    ws = R.connect_ws(cookie, uid=uid)
    try:
        seq = 1
        req = {"RequestId": base64.b16encode(_os.urandom(8)).decode().lower()}
        ws.send(R.encode_data_frame(seq, "whoami", req), opcode=0x2)
        for _ in range(200):
            op, payload = ws.recv_frame()
            if op == 0x9:
                ws.send(payload, opcode=0xA)
                continue
            if op != 0x2:
                continue
            try:
                ft, elems, body = R.decode_data_frame(payload)
            except Exception:
                continue
            if ft == 0x01 and len(elems) >= 2 and elems[1] == seq:
                info = (body or {}).get("UserInfo") or {}
                guid = info.get("Guid")
                if not guid:
                    raise RuntimeError(
                        f"whoami без UserInfo.Guid: {json.dumps(body, ensure_ascii=False)[:300]}")
                return guid, str(info.get("Uid") or uid)
        raise RuntimeError("ответ на whoami не пришёл")
    finally:
        ws.close()


def init(profile=None, container_id=None):
    profiles = [profile] if profile else find_profiles()
    if not profiles:
        raise RuntimeError("не найден профиль Firefox с cookies.sqlite")

    candidates = []
    for p in profiles:
        for s in find_sessions(p):
            if container_id is None or str(container_id) == s["container_id"]:
                candidates.append({"profile": p, **s})

    if not candidates:
        raise RuntimeError("не найдено ни одной залогиненной сессии Яндекса "
                           "(проверь профиль/контейнер)")
    if len(candidates) > 1:
        lines = "\n".join(
            f"  [{c['login'] or 'логин неизвестен'}] "
            f"--profile {c['profile']} --container {c['container_id']}"
            for c in candidates)
        raise RuntimeError("найдено несколько сессий Яндекса, уточни какую брать:\n" + lines)

    c = candidates[0]
    guid, uid = discover_identity(c["profile"], c["container_id"], c["uid"])
    data = {
        "firefox_profile": c["profile"].replace(os.path.expanduser("~"), "~", 1),
        "container_id": c["container_id"],
        "my_guid": guid,
        "my_uid": uid,
    }
    return save(data), data


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    if "--init" in argv:
        def opt(name):
            return argv[argv.index(name) + 1] if name in argv else None
        try:
            path, data = init(opt("--profile"), opt("--container"))
        except RuntimeError as e:
            # выбор аккаунта и незалогиненная сессия — это подсказка человеку, не сбой кода
            print(e, file=sys.stderr); sys.exit(2)
        print(f"записано в {path}:")
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"конфиг: {config_path()}")
        cfg = load(required=False)
        print(json.dumps(cfg, ensure_ascii=False, indent=2) if cfg
              else "не создан — запусти: python3 yamb_config.py --init")
