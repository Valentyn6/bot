# Архітектурний план: фінанси, життєві цілі та щоденні звички

## Поточна структура проєкту

У репозиторії є дві окремі реалізації:

- `fitness_bot/` — поточний бот, який запускається через `python -m fitness_bot`;
- `Нова папка/` — окрема шарова реалізація із SQLAlchemy та власною архітектурою.

Активний файл у Visual Studio — `fitness_bot/bot.py`, тому цей план насамперед стосується поточного бота. Реалізації не слід змішувати без окремого рішення.

## 1. Архітектура роутерів

Зараз основний роутер створюється безпосередньо у `fitness_bot/bot.py`:

```python
router = Router()
```

Він підключається до `Dispatcher` у функції `main()`:

```python
dispatcher = Dispatcher()
dispatcher.include_router(router)
await dispatcher.start_polling(bot)
```

Файл `fitness_bot/handlers/commands.py` також містить `Router`, але фактично він не підключений до `Dispatcher`. Тому переносити туди нову логіку без додаткового рефакторингу не варто.

### Рекомендація

Створити окремі модулі:

```text
fitness_bot/
├── finance/
│   ├── __init__.py
│   ├── router.py
│   ├── states.py
│   ├── service.py
│   └── monobank.py
├── goals/
│   ├── __init__.py
│   ├── router.py
│   ├── states.py
│   └── service.py
```

У `bot.py` підключати окремі роутери:

```python
dispatcher.include_router(finance_router)
dispatcher.include_router(goals_router)
dispatcher.include_router(router)
```

Або спочатку основний роутер, а потім модульні. Важливо, щоб callback-дані мали власні namespace:

```text
finance:menu
finance:sync
goals:create
habits:check
```

Це запобіжить конфліктам з існуючими callback-ами типу `food`, `profile` і `home`.

## 2. База даних і міграції

У поточному боті використовується `aiosqlite`.

Підключення відбувається в `Database.connect()`:

```python
self.conn = await aiosqlite.connect(self.path)
self.conn.row_factory = aiosqlite.Row
await self.conn.executescript(SCHEMA)
```

Початкова схема знаходиться у змінній `SCHEMA` в `fitness_bot/db.py`.

Після створення таблиць запускаються ручні міграції:

```python
await self._migrate_accounts()
await self._migrate_activity_log()
await self._migrate_food_log()
await self._migrate_profiles()
```

Міграції перевіряють `PRAGMA table_info(...)` і додають відсутні колонки через `ALTER TABLE`.

### Рекомендація

Не додавати всі нові поля до `telegram_accounts` або `profiles`. Краще створити окремі таблиці:

```text
monobank_accounts
finance_transactions
finance_categories
goals
habits
habit_logs
```

Приблизний зв’язок:

```text
telegram_accounts
		│
		├── monobank_accounts
		├── goals
		└── habits
				  │
				  └── habit_logs
```

Для транзакцій потрібен унікальний індекс за Monobank ID:

```text
UNIQUE(monobank_account_id, mono_transaction_id)
```

Це захистить від дублювання під час повторної синхронізації.

Існуючі таблиці та записи змінювати деструктивно не потрібно. Нові таблиці можна додати до `SCHEMA`, а індекси й нові поля — окремими ідемпотентними міграціями в `Database.connect()`.

Перед міграцією бажано створити резервну копію файлу:

```text
fitness_bot.sqlite3
```

## 3. FSM-стани

У поточному `fitness_bot/bot.py` використовується один клас:

```python
class InputFlow(StatesGroup):
	profile_value = State()
	food_meal = State()
	food_product = State()
	food_quantity = State()
	food_edit_quantity = State()
	weight = State()
	food_name = State()
	food_macros = State()
	food_unit = State()
	import_workout = State()
```

У `Нова папка/` є інші FSM-класи, але вони належать до окремої реалізації.

### Рекомендація

Не розширювати `InputFlow` станами фінансів і цілей. Створити окремі класи:

```python
class FinanceTokenFlow(StatesGroup):
	token = State()


class CashFlow(StatesGroup):
	amount = State()
	category = State()
	description = State()


class GoalFlow(StatesGroup):
	title = State()
	description = State()
	target_value = State()
	target_date = State()


class HabitFlow(StatesGroup):
	name = State()
	frequency = State()
	reminder_time = State()
```

Унікальні імена класів і станів запобігають конфліктам. Додатково кожен початок нового сценарію повинен виконувати:

```python
await state.clear()
```

Або очищати тільки власну FSM-групу, якщо в майбутньому з’являться паралельні сценарії.

## 4. Головне меню

У поточному боті використовується `InlineKeyboardMarkup`, а не Reply-клавіатура.

Головне меню реалізовано у функції:

```python
async def home_markup(user_id: int) -> InlineKeyboardMarkup:
```

Зараз у ньому є:

- `🍽 Раціон`;
- `⚖️ Вага`;
- `📈 Графіки`;
- `🩺 Здоров’я`;
- `👤 Профіль`;
- `⋯ Ще`.

### Рекомендація

Додати нові розділи безпосередньо до `home_markup()`:

```text
💳 Фінанси та Monobank
🎯 Цілі та звички
```

Наприклад:

```text
🍽 Раціон       ⚖️ Вага
💳 Фінанси      🎯 Цілі та звички
📈 Графіки      🩺 Здоров’я
👤 Профіль      ⋯ Ще
```

Основні callback-и:

```text
finance
goals
```

Внутрішні кнопки:

```text
finance:accounts
finance:sync
finance:cash
finance:categories
goals:list
goals:create
goals:habits
goals:stats
```

Існуючі кнопки переходу на головне меню вже використовують callback `home`, тому нові модулі повинні повертатися до нього через спільну функцію меню.

## 5. Фонові задачі

У поточному проєкті немає:

- `APScheduler`;
- `asyncio.create_task(...)`;
- періодичного polling;
- системи фонового запуску дайджестів;
- окремого lifecycle для scheduler.

Зараз бот просто запускає:

```python
await dispatcher.start_polling(bot)
```

### Рекомендація для першої версії

Почати без фонових задач:

- ручна кнопка «Синхронізувати Monobank»;
- ручне створення готівкової операції;
- ручна відмітка звички;
- ручний перегляд статистики.

Після стабілізації функціоналу можна додати scheduler. Його потрібно запускати і коректно зупиняти разом із ботом, а не створювати нескінченну задачу всередині handler-а.

Для Monobank автоматична синхронізація має бути polling-задачею з обмеженням частоти запитів. Для нагадувань також знадобляться:

- часова зона користувача;
- час нагадування;
- статус активності нагадувань;
- захист від повторної відправки.

## Запропонований план реалізації

### Етап 1. Зафіксувати цільову кодову базу

Поточний активний бот — `fitness_bot/`. Папка `Нова папка/` має іншу архітектуру і не повинна змішуватися з ним без окремого рішення.

### Етап 2. Спроєктувати структуру даних

Додати таблиці:

```text
monobank_accounts
finance_transactions
finance_categories
goals
habits
habit_logs
```

За потреби окремо:

```text
cash_transactions
```

Але готівкові операції можна зберігати в `finance_transactions` з полем:

```text
source = 'monobank' | 'cash' | 'manual'
```

### Етап 3. Додати міграції

Модифікувати:

```text
fitness_bot/db.py
```

Додати:

- створення нових таблиць;
- індекси;
- методи створення, оновлення та отримання записів;
- дедуплікацію Monobank-транзакцій;
- перевірку належності даних конкретному користувачу.

### Етап 4. Створити модуль фінансів

Нові файли:

```text
fitness_bot/finance/__init__.py
fitness_bot/finance/router.py
fitness_bot/finance/states.py
fitness_bot/finance/service.py
fitness_bot/finance/monobank.py
```

Функції:

- додавання токена;
- видалення токена;
- отримання рахунків;
- ручна синхронізація;
- перегляд операцій;
- фільтрація за категоріями;
- ручне введення готівки;
- фінансовий підсумок.

Для HTTP-запитів, імовірно, знадобиться додати `httpx` до `requirements.txt`.

Токен Monobank не можна зберігати у відкритому тексті повідомлень або логів. Також потрібно передбачити його видалення та заміну.

### Етап 5. Створити модуль цілей і звичок

Нові файли:

```text
fitness_bot/goals/__init__.py
fitness_bot/goals/router.py
fitness_bot/goals/states.py
fitness_bot/goals/service.py
```

Функції:

- створення, редагування та завершення цілі;
- встановлення дедлайну;
- прогрес цілі;
- створення щоденної звички;
- відмітка виконання;
- статистика за період;
- серія послідовних виконань.

### Етап 6. Підключити роутери

Змінити `fitness_bot/bot.py`:

```python
dispatcher.include_router(router)
dispatcher.include_router(finance_router)
dispatcher.include_router(goals_router)
```

Потрібно стежити, щоб handler-и нових модулів не перехоплювали існуючі повідомлення та callback-и раціону.

### Етап 7. Оновити меню

Змінити `fitness_bot/bot.py` у функціях `home_markup()` і, за потреби, `home_text()`.

Додати кнопки:

```text
💳 Фінанси та Monobank
🎯 Цілі та звички
```

### Етап 8. Додати scheduler після MVP

Окремий модуль, наприклад:

```text
fitness_bot/scheduler.py
```

Він може відповідати за:

- регулярну синхронізацію Monobank;
- щоденні нагадування;
- вечірній дайджест;
- нагадування про невиконані звички.

На першому етапі scheduler рекомендовано залишити вимкненим.

### Етап 9. Додати тести

Потрібно перевірити:

- міграцію існуючої бази без втрати даних;
- ізоляцію даних різних користувачів;
- дедуплікацію Monobank-транзакцій;
- FSM-переходи;
- створення й завершення цілей;
- повторну відмітку звички;
- обробку помилок API.

Перед реалізацією потрібно підтвердити ключовий вибір: новий функціонал додаємо саме до поточного `fitness_bot/`, чи переносимо його в архітектуру `Нова папка/`? Рекомендований варіант для збереження поточного працюючого бота — `fitness_bot/`.
