# Домен в BotFather (без туннеля)

Бот работает по **домену** https://tgplay.fun. Туннели не используются.

## Кнопка меню (PLAY)

При каждом старте бэкенд вызывает Telegram API (`setChatMenuButton`) и выставляет кнопку с текстом **PLAY** и URL мини-приложения. Текст задаётся в `backend/telegram_welcome.py` → `MENU_BUTTON_TEXT`.

### Если в списке чатов всё ещё «ОТКРЫТЬ»

В API у бота уже сохранён текст **PLAY** (проверка: `python3 scripts/set-menu-button-play.py`). Надпись **«ОТКРЫТЬ» в списке чатов** в части клиентов Telegram подставляется приложением по умолчанию и не берётся из API. Что можно попробовать:

1. **BotFather**  
   Открой [@BotFather](https://t.me/BotFather), отправь **`/setmenubutton`** → выбери бота → укажи URL `https://tgplay.fun` → когда попросит текст кнопки, введи **`PLAY`**.

2. **Bot Settings → Menu Button**  
   Бот → **Bot Settings** → **Menu Button** → **Configure menu button** → если есть поле **Button text**, укажи **`PLAY`**, URL — твой домен.

3. **В самом чате с ботом** кнопка рядом с полем ввода должна показывать PLAY (данные из API).

## В backend/.env

Обязательно указан только домен (не URL туннеля):

```
WEBAPP_URL=https://tgplay.fun
```

Других конфигураций для туннеля не требуется.
