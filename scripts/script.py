import os
from dotenv import load_dotenv
import telebot
import sqlite3
from datetime import datetime

# Загружаем переменные из .env
load_dotenv()

# Получаем данные
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

if not BOT_TOKEN:
    raise ValueError("Ошибка: Не установлена переменная окружения TELEGRAM_BOT_TOKEN")

MAX_BOOKINGS_PER_MONTH = 3

bot = telebot.TeleBot(BOT_TOKEN)

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db():
    """Возвращает соединение с БД. Файл создаётся автоматически."""
    conn = sqlite3.connect("bookings.db")
    conn.row_factory = sqlite3.Row  # Позволяет обращаться к столбцам по имени
    return conn

def init_db():
    """Создаёт таблицы, если их ещё нет."""
    conn = get_db()
    cur = conn.cursor()

    # Таблица слотов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, -- Формат: YYYY-MM-DD
            time TEXT NOT NULL, -- Формат: HH:MM
            is_booked INTEGER DEFAULT 0,
            booked_by INTEGER,
            FOREIGN KEY(booked_by) REFERENCES users(tg_id)
        )
    """)

    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT DEFAULT 'client'
        )
    """)

    # Таблица лимитов записей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS booking_limits (
            tg_id INTEGER,
            year_month TEXT, -- Формат: YYYY-MM
            count INTEGER DEFAULT 0,
            PRIMARY KEY (tg_id, year_month),
            CHECK (count >= 0) -- Гарантирует неотрицательный счётчик
        )
    """)

    conn.commit()
    conn.close()


def register_user(tg_id, username):
    """Добавляет пользователя в БД, если его там нет."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (tg_id, username) VALUES (?, ?)", (tg_id, username))
    conn.commit()
    conn.close()


def get_role(tg_id):
    """Определяет роль: админ или клиент."""
    if tg_id == ADMIN_ID:
        return "admin"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row["role"] if row else "client"


def check_and_increment_limit(tg_id, slot_date_str):
    """
    Проверяет лимит записей для пользователя в заданном месяце.
    Если лимит не превышен, увеличивает счётчик и возвращает (True, новый_счётчик).
    Если превышен, возвращает (False, текущий_счётчик).
    """
    conn = get_db()
    cur = conn.cursor()

    try:
        # Извлекаем год-месяц из строки даты
        slot_date = datetime.strptime(slot_date_str, "%Y-%m-%d")
        target_month = slot_date.strftime("%Y-%m")  # "2026-04-28" -> "2026-04"

        # Получаем текущий счётчик
        cur.execute("SELECT count FROM booking_limits WHERE tg_id=? AND year_month=?", (tg_id, target_month))
        limit_row = cur.fetchone()
        current_count = limit_row["count"] if limit_row else 0

        if current_count >= MAX_BOOKINGS_PER_MONTH:
            conn.close() # Закрываем соединение, если лимит превышен
            return False, current_count

        # Обновляем или вставляем счётчик
        if limit_row:
            cur.execute("UPDATE booking_limits SET count=count+1 WHERE tg_id=? AND year_month=?", (tg_id, target_month))
        else:
            cur.execute("INSERT INTO booking_limits (tg_id, year_month, count) VALUES (?, ?, 1)", (tg_id, target_month))

        conn.commit()
        conn.close()
        return True, current_count + 1

    except ValueError: # Если формат даты неверный
        conn.close()
        return False, -1 # Индикатор ошибки формата
    except sqlite3.Error as e: # Обработка ошибок SQLite
        print(f"Ошибка SQLite в check_and_increment_limit: {e}")
        conn.close()
        return False, -1 # Индикатор ошибки БД


def decrement_limit_on_cancellation(tg_id, slot_date_str):
    """
    Уменьшает счётчик записей при отмене брони.
    Возвращает True, если счётчик успешно уменьшен.
    """
    conn = get_db()
    cur = conn.cursor()

    try:
        # Извлекаем год-месяц из строки даты
        slot_date = datetime.strptime(slot_date_str, "%Y-%m-%d")
        target_month = slot_date.strftime("%Y-%m")

        # Уменьшаем счётчик, но не ниже 0 (CHECK в таблице тоже помогает)
        cur.execute("""
            UPDATE booking_limits
            SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END
            WHERE tg_id = ? AND year_month = ?
        """, (tg_id, target_month))

        conn.commit()
        affected_rows = cur.rowcount
        conn.close()

        # Если затронута строка, значит счётчик был уменьшен
        return affected_rows > 0

    except ValueError: # Если формат даты неверный
        conn.close()
        return False
    except sqlite3.Error as e: # Обработка ошибок SQLite
        print(f"Ошибка SQLite в decrement_limit_on_cancellation: {e}")
        conn.close()
        return False


# --- ОБРАБОТЧИКИ КОМАНД ---

@bot.message_handler(commands=["start"])
def cmd_start(message):
    tg_id = message.from_user.id
    username = message.from_user.username or "unknown"
    register_user(tg_id, username)
    role = get_role(tg_id)

    text = "👋 Добро пожаловать в систему записи!\n"
    if role == "admin":
        text += "🔹 Вы вошли как АДМИН\n"
        text += "Доступные команды:\n"
        text += "/addslot <дата> <время> – добавить слот (напр. /addslot 2026-06-10 14:00)\n"
        text += "/bookings – посмотреть все записи\n"
        text += "/delete <id_слота> – удалить слот или запись\n"
        # text += "/broadcast <текст> – рассылка всем пользователям (реализуйте сами)"
    else:
        text += f"🔹 Вы вошли как КЛИЕНТ (лимит: {MAX_BOOKINGS_PER_MONTH} действия/мес)\n"
        text += "/slots – посмотреть свободные слоты\n"
        text += "/book <id> – записаться на слот (тратит 1 попытку)\n"
        text += "/cancel <id> – отменить запись (НЕ тратит попытку)\n"
        text += "/mybookings – мои записи"

    bot.reply_to(message, text)


@bot.message_handler(commands=["slots"])
def cmd_slots(message):
    if get_role(message.from_user.id) != "client":
        bot.reply_to(message, "⛔ Эта команда только для клиентов.")
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, date, time FROM slots WHERE is_booked = 0 ORDER BY date, time")
        rows = cur.fetchall()
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении слотов: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при загрузке слотов. Попробуйте позже.")
        conn.close()
        return

    conn.close()

    if not rows:
        bot.reply_to(message, "📭 Свободных слотов пока нет.")
        return

    msg = "📅 Свободные слоты:\n"
    for r in rows:
        msg += f"🔹 ID: {r['id']} | {r['date']} в {r['time']}\n"
    msg += f"\nДля записи: /book <ID> | Для отмены: /cancel <ID>\n(Лимит: {MAX_BOOKINGS_PER_MONTH}/мес)"
    bot.reply_to(message, msg)


@bot.message_handler(commands=["book"])
def cmd_book(message):
    if get_role(message.from_user.id) != "client":
        return # Или отправить сообщение о недостатке прав

    try:
        slot_id = int(message.text.split(maxsplit=1)[1]) # maxsplit=1, чтобы не разбивать время с :
    except (IndexError, ValueError):
        bot.reply_to(message, "⚠️ Формат: /book <ID_слота>")
        return

    conn = get_db()
    cur = conn.cursor()
    tg_id = message.from_user.id

    try:
        # Проверяем слот
        cur.execute("SELECT is_booked, date FROM slots WHERE id=?", (slot_id,))
        row = cur.fetchone()

        if not row:
            bot.reply_to(message, "❌ Слот с таким ID не найден.")
            conn.close()
            return
        if row["is_booked"]:
            bot.reply_to(message, "❌ Этот слот уже занят.")
            conn.close()
            return

        # --- ПРОВЕРКА И УВЕЛИЧЕНИЕ ЛИМИТА ---
        success, new_count = check_and_increment_limit(tg_id, row["date"])
        if not success:
            if new_count == MAX_BOOKINGS_PER_MONTH:
                bot.reply_to(message, f"❌ Лимит записей на {row['date'][:7]} исчерпан ({MAX_BOOKINGS_PER_MONTH}/{MAX_BOOKINGS_PER_MONTH}).")
            else:
                bot.reply_to(message, "❌ Произошла ошибка при проверке лимита.")
            conn.close()
            return

        # --- ЗАПИСЬ НА СЛОТ ---
        cur.execute("UPDATE slots SET is_booked=1, booked_by=? WHERE id=?", (tg_id, slot_id))
        conn.commit()
        conn.close()

        remaining = MAX_BOOKINGS_PER_MONTH - new_count
        bot.reply_to(message, f"✅ Вы записаны на слот #{slot_id}\n📊 Осталось действий в {row['date'][:7]}: {remaining}")

    except sqlite3.Error as e:
        print(f"Ошибка SQLite при записи: {e}")
        conn.close()
        bot.reply_to(message, "❌ Произошла ошибка при попытке записи. Попробуйте позже.")


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if get_role(message.from_user.id) != "client":
        return # Или отправить сообщение о недостатке прав

    try:
        slot_id = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        bot.reply_to(message, "⚠️ Формат: /cancel <ID_слота>")
        return

    conn = get_db()
    cur = conn.cursor()
    tg_id = message.from_user.id

    try:
        # Проверяем, существует ли слот и записан ли на него пользователь
        cur.execute("SELECT is_booked, booked_by, date FROM slots WHERE id=?", (slot_id,))
        row = cur.fetchone()

        if not row:
            bot.reply_to(message, "❌ Слот с таким ID не найден.")
            conn.close()
            return

        if not row["is_booked"]:
            bot.reply_to(message, "❌ Этот слот и так свободен. Отменять нечего.")
            conn.close()
            return

        if row["booked_by"] != tg_id:
            bot.reply_to(message, "❌ Вы не записаны на этот слот.")
            conn.close()
            return

        # --- ОТМЕНЯЕМ ЗАПИСЬ ---
        cur.execute("UPDATE slots SET is_booked=0, booked_by=NULL WHERE id=?", (slot_id,))
        conn.commit()

        # --- ВОССТАНАВЛИВАЕМ ЛИМИТ ---
        limit_restored = decrement_limit_on_cancellation(tg_id, row["date"])

        conn.close()

        # Сообщение пользователю
        msg = f"✅ Бронь на слот #{slot_id} отменена."
        if limit_restored:
             # Пересчитываем оставшиеся попытки после восстановления
             # Для этого нужно снова запросить счётчик из БД
             conn_temp = get_db()
             cur_temp = conn_temp.cursor()
             target_month = row["date"][:7]
             cur_temp.execute("SELECT count FROM booking_limits WHERE tg_id=? AND year_month=?", (tg_id, target_month))
             temp_row = cur_temp.fetchone()
             current_count_after_restore = temp_row["count"] if temp_row else 0
             conn_temp.close()
             remaining_after_restore = MAX_BOOKINGS_PER_MONTH - current_count_after_restore
             msg += f"\n📊 Доступное действие возвращено. Осталось: {remaining_after_restore}/{MAX_BOOKINGS_PER_MONTH} в {target_month}."
        else:
             # Скорее всего, счётчик уже был 0 до отмены, или произошла ошибка БД
             msg += "\n⚠️ Действие не возвращено (возможно, лимит уже был восстановлен ранее)."
        bot.reply_to(message, msg)

    except sqlite3.Error as e:
        print(f"Ошибка SQLite при отмене: {e}")
        conn.close()
        bot.reply_to(message, "❌ Произошла ошибка при попытке отмены. Попробуйте позже.")


@bot.message_handler(commands=["mybookings"])
def cmd_mybookings(message):
    if get_role(message.from_user.id) != "client":
        return # Или отправить сообщение о недостатке прав

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT s.id, s.date, s.time FROM slots s WHERE s.booked_by=?", (message.from_user.id,))
        rows = cur.fetchall()
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении моих записей: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при загрузке ваших записей. Попробуйте позже.")
        conn.close()
        return

    conn.close()

    if not rows:
        bot.reply_to(message, "ostringstream> У вас пока нет записей.")
        return

    msg = "📋 Ваши записи:\n"
    for r in rows:
        msg += f"🔹 ID: {r['id']} | {r['date']} в {r['time']}\n"
    bot.reply_to(message, msg)


@bot.message_handler(commands=["addslot"])
def cmd_addslot(message):
    if get_role(message.from_user.id) != "admin":
        bot.reply_to(message, "⛔ Нет прав администратора.")
        return

    try:
        parts = message.text.split(maxsplit=2) # maxsplit=2, чтобы корректно обработать дату и время
        if len(parts) < 3:
             raise ValueError("Недостаточно аргументов")
        _, date_str, time_str = parts
        # Проверяем формат даты и времени
        datetime.strptime(date_str, '%Y-%m-%d')
        datetime.strptime(time_str, '%H:%M')
    except (ValueError, IndexError):
        bot.reply_to(message, "⚠️ Формат: /addslot YYYY-MM-DD HH:MM")
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO slots (date, time) VALUES (?, ?)", (date_str, time_str))
        conn.commit()
        bot.reply_to(message, f"✅ Слот добавлен: {date_str} {time_str}")
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при добавлении слота: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при добавлении слота.")
    finally:
        conn.close()


@bot.message_handler(commands=["bookings"])
def cmd_admin_bookings(message):
    if get_role(message.from_user.id) != "admin":
        return # Или отправить сообщение о недостатке прав

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT s.id, s.date, s.time, u.username
            FROM slots s
            LEFT JOIN users u ON s.booked_by = u.tg_id
            ORDER BY s.date, s.time
        """)
        rows = cur.fetchall()
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении всех записей: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при загрузке записей. Попробуйте позже.")
        conn.close()
        return

    conn.close()

    if not rows:
        bot.reply_to(message, "ostringstream> Записей пока нет.")
        return

    msg = "📊 Все слоты и записи:\n"
    for r in rows:
        status = f"👤 @{r['username']}" if r["username"] else "🟢 Свободен"
        msg += f"🔹 ID: {r['id']} | {r['date']} {r['time']} | {status}\n"
    bot.reply_to(message, msg)


@bot.message_handler(commands=["delete"])
def cmd_delete(message):
    if get_role(message.from_user.id) != "admin":
        return # Или отправить сообщение о недостатке прав

    try:
        slot_id = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        bot.reply_to(message, "⚠️ Формат: /delete <ID_слота>")
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        # Проверяем, есть ли бронь на слоте перед удалением
        cur.execute("SELECT booked_by, date FROM slots WHERE id=?", (slot_id,))
        row = cur.fetchone()
        if row and row["booked_by"]: # Если слот был забронирован
             # Здесь можно добавить логику: уменьшать лимит у пользователя или нет?
             # В данном случае, просто удаляем слот, не влияя на лимиты.
             # Если нужно уменьшать, нужно вызвать decrement_limit_on_cancellation
             # с данными пользователя row["booked_by"] и датой row["date"].
             pass

        cur.execute("DELETE FROM slots WHERE id=?", (slot_id,))
        conn.commit()
        if cur.rowcount > 0:
            bot.reply_to(message, f"🗑 Слот #{slot_id} удалён.")
        else:
            bot.reply_to(message, f"❌ Слот с ID #{slot_id} не найден.")
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при удалении слота: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при удалении слота.")
    finally:
        conn.close()


# --- ЗАПУСК ---
if __name__ == "__main__":
    print("🔄 Инициализация базы данных...")
    init_db()
    print("✅ БД готова. Запускаю бота...")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\n--- Бот остановлен пользователем. ---")
    except Exception as e:
        print(f"Критическая ошибка: {e}")