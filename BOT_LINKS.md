# Ссылка, которая сразу открывает Mini App (не чат с ботом)

Чтобы по ссылке из канала или чата открывался именно плеер (Mini App), а не только чат с ботом, нужно настроить **Main Mini App** в BotFather.

## Шаги в BotFather

1. Открой [@BotFather](https://t.me/BotFather).
2. Отправь **`/mybots`** и выбери своего бота **@tgplayxbot**.
3. Зайди в **Bot Settings** → **Configure Mini App** (или **Main Mini App** / **Configure Bot** — пункт про Mini App).
4. Укажи URL приложения: **`https://tgplay.fun`** (тот же, что в кнопке PLAY).
5. Сохрани настройки.

## Ссылка для поста в канале

После настройки используй эту ссылку как гиперссылку в сообщениях:

**https://t.me/tgplayxbot?startapp**

При переходе по ней в Telegram откроется сразу Mini App (плеер), а не чат с ботом. Контекст Telegram сохраняется — логин и плейлисты работают.

### Варианты

- Без параметров: `https://t.me/tgplayxbot?startapp`
- С параметром (если понадобится в приложении): `https://t.me/tgplayxbot?startapp=channel` — значение `channel` придёт в приложение в `tgWebAppStartParam`.

Если в BotFather нет пункта «Main Mini App», ищи **Configure Mini App** или **Menu Button** и убедись, что указан URL `https://tgplay.fun`; тогда кнопка меню (PLAY) уже открывает приложение, а ссылка `?startapp` может заработать после обновления настроек бота.
