# yandex-messenger-cli

Консольный клиент **Яндекс.Мессенджера** (messenger.360.yandex.ru) на чистом Python stdlib —
без зависимостей, без токенов и без браузерной автоматизации. Авторизация переиспользует
куки уже залогиненного профиля Firefox.

Умеет: читать историю, отправлять текст, отвечать с цитатой, править и удалять свои сообщения,
принимать и отправлять вложения, работать с тредами.

Протокол официально не документирован и снят реверсом веб-клиента. Основной источник —
[conarti/yandex-messenger-mcp](https://github.com/conarti/yandex-messenger-mcp) (TypeScript, MCP-сервер);
здесь то же самое, но в виде четырёх файлов на stdlib. Несколько мест уточнены живыми
проверками — они отмечены ниже.

## Установка

Зависимостей нет, нужен Python 3.9+ и залогиненный в Яндекс профиль Firefox.

```bash
git clone https://github.com/dmitry-j-mikhin/yandex-messenger-cli
cd yandex-messenger-cli
python3 yamb_config.py --init
```

`--init` сам находит профили Firefox, показывает все контейнеры с залогиненной сессией Яндекса
(с логинами, чтобы было понятно, какой брать), достаёт uid из кук и guid — из WS-метода `whoami`:

```
$ python3 yamb_config.py --init
найдено несколько сессий Яндекса, уточни какую брать:
  [ivan@example.com]  --profile ~/.mozilla/firefox/xxxx.default-release --container 0
  [ivan.work@corp.ru] --profile ~/.mozilla/firefox/xxxx.default-release --container 7

$ python3 yamb_config.py --init --container 7
записано в ~/.config/yandex-messenger/config.json
```

Конфиг (профиль, контейнер, guid, uid) лежит в `~/.config/yandex-messenger/config.json`
с правами 600; путь переопределяется переменной `YAMB_CONFIG`. В самом коде никаких
аккаунтных констант нет.

## Использование

```bash
# история
python3 yamb_read.py <partnerGuid|chatId> [limit]

# тред: по родителю или по готовому thread_id
python3 yamb_read.py --thread <parentChatId|guid> <parent_timestamp> [limit]
python3 yamb_read.py --thread <thread_id> [limit]

# отправка, ответ с цитатой, файл, правка, удаление
python3 yamb_send.py <partnerGuid|chatId> "<текст>" [reply_to_timestamp]
python3 yamb_send.py <partnerGuid|chatId> --file <путь> [--as-file]
python3 yamb_send.py <partnerGuid|chatId> --edit <timestamp> "<новый текст>"
python3 yamb_send.py <partnerGuid|chatId> --delete <timestamp>

# кто это по guid + настоящий chat_id; скачивание вложения
python3 yamb_api.py <partnerGuid>
python3 yamb_api.py --download <file_id> [куда]
```

`partnerGuid` — guid прямо из адреса чата `messenger.360.yandex.ru/#/user/<guid>`.
У любой команды есть `--dry-run`: собрать и показать кадр, ничего не отправляя.

Пример вывода читалки:

```
[2026-08-05 15:35:17] Я:
    📎 image: screenshot.png, 231 КБ  file_id=disk/<uuid>
[2026-08-05 16:13:14] Иван Иванов: 'посмотри логи'
    ↳ в ответ на [2026-08-05 15:35:17] Я: None
[2026-08-05 16:12:37] Я: 'обсудим отдельно'
    🧵 тред: 2 сообщ., последнее 2026-08-05 16:13:15
       110/0/<guid>_<guid>_1785935557582088
```

## Файлы

| файл | назначение |
|---|---|
| `yamb_config.py` | конфиг и автоопределение аккаунта (`--init`) |
| `yamb_read.py` | WebSocket-транспорт, кодек кадров, история, треды |
| `yamb_send.py` | `push`: текст, ответы, вложения, правка, удаление |
| `yamb_api.py` | HTTP registry-API: пользователи, чаты, загрузка/скачивание файлов, подписка на тред |

## Как это работает

**Транспорт** — xiva WebSocket `wss://push.yandex.ru/v2/subscribe/websocket`, handshake только
по кукам. Кадр: `0x01` + MessagePack-заголовок `[serviceIndex, seq, method]` + `0x05` + 11 нулей +
UTF-8 JSON. Конец заголовка ищется разбором длин msgpack, а не поиском байта `0x05`: при `seq=5`
байт счётчика совпадает с разделителем и наивный скан режет заголовок не там.

**Чтение** — метод `history`. **Отправка** — метод `push` с конвертом
`{ClientTransportId:{XivaSubscriptionId}, UserAgent, Meta:{Origin:27}, ClientMessage:{…}}`.
`XivaSubscriptionId` берётся из операционного текст-кадра `subscribed`, который сервер шлёт
сразу после handshake, — выдумывать его нельзя, с чужим id сообщение уходит в никуда.
`PayloadId` (uuid4) — ключ серверной дедупликации: повтор с тем же id даёт `DUPLICATE(8)`,
а не второе сообщение. Ретраев нет намеренно: `push` необратим.

Одна форма `Plain` покрывает почти всё, и различают её поля:

| что | форма |
|---|---|
| отправка | `Plain{ChatId, PayloadId, Text}` |
| ответ | `+ ForwardedMessageRefs[{ChatId, Timestamp}]` и `ForwardedMessageStyles[{Quote}]` |
| пересылка | то же, но **без** `Quote` |
| правка | `Plain{ChatId, Timestamp, Text}` — метка цели + контент |
| удаление | `Plain{ChatId, Timestamp}` — метка цели **без** контента |

Метки времени (16 цифр, микросекунды) уходят на провод **числом**: строка на этом месте даёт
`BACKEND_CALL_ERROR`.

**Треды.** Серверного «создания треда» не существует: `thread_id` вычисляется на клиенте из
`{ChatId родителя, Timestamp сообщения}` (`110/0/<guid>_<guid>_<ts>` для лички, `100/…` для
группы, `101/…` для канала), а на сервере тред материализуется первым `push` в этот id.
Читается тред как обычный чат — `history` по `thread_id`, родитель приезжает внутри ответа
полем `ThreadParentMessage`.

**Вложения.** Отправка — три шага по HTTP: `upload_to_disk` → PUT сырых байт → `add_files`,
и только потом `push` с `FileInfo`. Скачивание — URL строит клиент из `FileInfo.Id2`:
`https://files.messenger.yandex.ru/file_shortterm/<Id2>?attach=true`.

## Уточнения, снятые живыми проверками

- **Порядок guid в id приватного чата не фиксирован.** Встречаются оба (`<partner>_<me>` и
  `<me>_<partner>`); все наблюдавшиеся живые чаты ложатся на пару, отсортированную по
  возрастанию. Неверная строка не даёт внятной ошибки — сервер отвечает так, будто чата нет
  (`ENTITY_NOT_FOUND` на чтении, `NO_SUCH_CHAT` на отправке), поэтому кандидат сверяется
  с `get_chats_info`.
- **Оба registry-шага загрузки файла требуют `X-CSRF-TOKEN`** — иначе `403 bad_csrf_token`.
- **`file_shortterm` отвечает 302 на storage-хост, и идти за редиректом с сессионной кукой
  нельзя** — запрос виснет. Ссылка уже подписана, куку туда слать незачем.
- **`Id2` содержит `/`** (`<bucket>/<uuid>`) — гнать его целиком через `quote()` нельзя,
  `/` станет `%2F` и вернётся 404. Здесь вместо экранирования — валидация формы сегментов.
- **Единственный признак «у сообщения есть тред»** — `ServerMessageInfo.ThreadState`
  `{LastSeqNo, LastTsMcs}`. Списка тредов сервер не отдаёт. Счётчик не уменьшается при
  удалении сообщений из треда, так что это признак наличия, а не точное число.
- **Деривация thread_id — radix 10, а не radix 2.** В самом веб-клиенте написано
  `100+parseInt(prefix,2)`: совпадает с десятичным только на префиксах 0/1, а на бизнес-чате
  `2/…` даёт `NaN`. Обратный парсер там же использует radix 10 — он и авторитетен.
- **Файлы удалённого сотрудника не скачиваются** — `403 DiskUserBlockedError`. Сообщения
  при этом читаются нормально.
- **Удаление оставляет надгробие**: сообщение остаётся в `history` с
  `ServerMessageInfo.Deleted=true` и пустым контентом.

## Ограничения

- Тянется **одна страница** истории; глубже — курсором `read_history(..., max_timestamp=…)`
  (в CLI не выведен).
- Поиска по имени нет — нужен guid из адреса чата.
- Не реализованы: отправка голосовых и галерей (приём работает), реакции, маркеры прочтения,
  `create_private_chat` (в чат, которого никогда не было, отправить нельзя).
- Ломается, если сессия в контейнере разлогинится или Яндекс поменяет протокол.
- Только Firefox: куки читаются из `cookies.sqlite` (копия БД + WAL, так что браузер может
  быть запущен).

## Лицензия

[MIT](LICENSE).

## Дисклеймер

Неофициальный клиент, с Яндексом никак не связан. Протокол снят реверсом и может измениться
без предупреждения. Используйте на свой риск и в рамках правил сервиса.
