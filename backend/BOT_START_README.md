# Приветствие по команде /start

- **Файл:** `telegram_welcome.py` → `WELCOME_MESSAGE` и кнопка PLAY.
- **bot.py** и **server_lite.py** отправляют только `WELCOME_MESSAGE` и кнопку PLAY.

# Описание бота (About) и поиск Telegram

Текст «About» хранится в коде и при каждом старте выставляется через API:

- **Файл:** `telegram_welcome.py` → `BOT_ABOUT_TEXT` (About, до 120 символов) и `BOT_DESCRIPTION` (полное описание в профиле).
- При старте **server_lite** и **bot** вызывают `setMyShortDescription` (About) и `setMyDescription` (Description) для default и ru.

Чтобы изменить текст — правь `BOT_ABOUT_TEXT` и/или `BOT_DESCRIPTION` в `telegram_welcome.py`, затем перезапусти бэкенд или выполни `python3 scripts/force-bot-start-fix.py`.

# Картинка бота (профиль)

Картинка для профиля бота хранится в проекте: **`public/bot-profile.png`**. В Telegram её нельзя выставить через API — только вручную в @BotFather: выбери бота → **Edit Bot** → **Edit Botpic** → загрузи файл `public/bot-profile.png`. Тогда описание и эта картинка будут всегда отображаться вместе в профиле.
