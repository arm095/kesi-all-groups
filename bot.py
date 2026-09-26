import telebot
from telebot import types
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re
import time
import threading
import schedule
import os
from flask import Flask
import hashlib

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ.get("TOKEN")
# ==============================

if not TOKEN:
    print("Ошибка: не указан TOKEN")
    exit(1)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

user_groups = {}          # chat_id -> группа
user_pages = {}
all_users = set()         # все, кто когда-либо писал боту
last_changes_hash = None  # хеш последних изменений

@app.route('/')
def home():
    return "Bot is alive!"

# ==================== ПАРСИНГ ====================

def get_all_groups():
    url = "https://college-edu.ru/stud/raspisanie/"
    try:
        r = requests.get(url, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except:
        return []

    groups = []
    for h2 in soup.find_all("h2"):
        text = h2.get_text(strip=True)
        if text.startswith("Группа "):
            groups.append(text.replace("Группа ", "").strip())
    return groups

def get_changes():
    """Возвращает текст изменений или None, если их нет"""
    url = "https://college-edu.ru/stud/raspisanie/"
    try:
        r = requests.get(url, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except:
        return None

    # Ищем блок изменений
    text = soup.get_text("\n", strip=True)
    
    # Ищем начало и конец блока изменений
    start_markers = ["Изменения в расписании", "ИЗМЕНЕНИЯ В РАСПИСАНИИ"]
    end_markers = ["ДГД 302", "Группа ДГД", "Расписание занятий ·"]

    start_idx = -1
    for marker in start_markers:
        start_idx = text.find(marker)
        if start_idx != -1:
            break

    if start_idx == -1:
        return None

    # Обрезаем от начала изменений
    changes_text = text[start_idx:]

    # Ищем конец блока (начало списка групп)
    for marker in end_markers:
        end_idx = changes_text.find(marker)
        if end_idx != -1 and end_idx > 50:
            changes_text = changes_text[:end_idx]
            break

    changes_text = changes_text.strip()

    # Если слишком коротко — считаем, что изменений нет
    if len(changes_text) < 40:
        return None

    return changes_text

def get_schedule(group_name):
    url = "https://college-edu.ru/stud/raspisanie/"
    try:
        r = requests.get(url, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        return None, f"Ошибка загрузки сайта: {e}"

    group_h2 = None
    for h2 in soup.find_all("h2"):
        if group_name in h2.get_text():
            group_h2 = h2
            break
    if not group_h2:
        return None, "Группа не найдена"

    schedule_dict = {}
    for div in group_h2.find_next_siblings("div"):
        lines = [l.strip() for l in div.get_text("\n", strip=True).split("\n") if l.strip()]
        if not lines:
            continue
        day = lines[0]
        pairs = []
        i = 1
        while i < len(lines):
            if re.match(r"^\d+$", lines[i]):
                pair_num = lines[i]
                time_str = lines[i+1] if i+1 < len(lines) else ""
                subject = lines[i+2] if i+2 < len(lines) else ""
                teacher = lines[i+3] if i+3 < len(lines) else ""
                room = ""
                next_idx = i + 4
                if next_idx < len(lines) and ("ауд" in lines[next_idx].lower() or "сдо" in lines[next_idx].lower()):
                    room = lines[next_idx]
                    i = next_idx + 1
                else:
                    i = next_idx
                pairs.append({
                    "num": pair_num,
                    "time": time_str,
                    "subject": subject,
                    "teacher": teacher,
                    "room": room
                })
            else:
                i += 1
        schedule_dict[day] = pairs
    return schedule_dict, group_name

def format_day(day_name, pairs):
    if not pairs:
        return f"<b>{day_name}</b>\n🎉 Пар нет"

    text = f"<b>{day_name}</b>\n"
    for p in pairs:
        room = f"  ·  {p['room']}" if p['room'] else ""
        text += f"\n<b>{p['num']} пара</b>  {p['time']}\n"
        text += f"📚 {p['subject']}\n"
        text += f"👤 {p['teacher']}{room}\n"
    return text.strip()

def get_day_schedule(group_name, target="today"):
    sched, gname = get_schedule(group_name)
    if sched is None:
        return gname

    today = datetime.now().date()
    target_date = today + timedelta(days=1) if target == "tomorrow" else today
    target_str = target_date.strftime("%d.%m.%y")

    for day_key, pairs in sched.items():
        if target_str in day_key:
            title = "📅 Сегодня" if target == "today" else "📅 Завтра"
            header = f"{title}\nГруппа: <b>{gname}</b>\n{'─' * 22}\n"
            return header + format_day(day_key, pairs)

    return f"На {'сегодня' if target == 'today' else 'завтра'} пар не найдено."

def get_week_schedule(group_name):
    sched, gname = get_schedule(group_name)
    if sched is None:
        return gname

    if not sched:
        return "Расписание на неделю пока пустое."

    text = f"📆 <b>Расписание на неделю</b>\nГруппа: <b>{gname}</b>\n{'─' * 22}\n\n"
    day_order = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    sorted_days = sorted(
        sched.items(),
        key=lambda x: next((i for i, d in enumerate(day_order) if x[0].startswith(d)), 99)
    )

    for day_key, pairs in sorted_days:
        text += format_day(day_key, pairs) + "\n\n"

    return text.strip()

# ==================== КЛАВИАТУРЫ ====================

def make_groups_keyboard(page=0, per_page=8):
    groups = get_all_groups()
    if not groups:
        return None

    markup = types.InlineKeyboardMarkup(row_width=1)
    start = page * per_page
    end = start + per_page
    page_groups = groups[start:end]

    for g in page_groups:
        btn_text = g if len(g) < 40 else g[:37] + "..."
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"grp:{g}"))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page-1}"))
    if end < len(groups):
        nav.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"page:{page+1}"))
    if nav:
        markup.row(*nav)

    return markup

def main_menu_keyboard(has_group=False):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("📅 Сегодня", "📅 Завтра")
    markup.add("📆 Неделя", "⚠️ Изменения")
    if has_group:
        markup.add("🔄 Сменить группу")
    else:
        markup.add("📋 Выбрать группу")
    return markup

# ==================== КОМАНДЫ ====================

@bot.message_handler(commands=['start', 'help'])
def start(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    has_group = chat_id in user_groups

    text = (
        "Привет! Я бот с расписанием <b>всех групп</b> колледжа КЭСИ 📚\n\n"
        "Сначала выбери свою группу."
    )
    if has_group:
        text += f"\n\nТекущая группа: <b>{user_groups[chat_id]}</b>"

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=main_menu_keyboard(has_group))

    if not has_group:
        bot.send_message(chat_id, "Выбери группу:", reply_markup=make_groups_keyboard(0))

@bot.message_handler(commands=['группа', 'group'])
@bot.message_handler(func=lambda m: m.text in ["📋 Выбрать группу", "🔄 Сменить группу"])
def choose_group(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    user_pages[chat_id] = 0
    bot.send_message(chat_id, "Выбери свою группу:", reply_markup=make_groups_keyboard(0))

@bot.callback_query_handler(func=lambda call: call.data.startswith("page:"))
def page_callback(call):
    page = int(call.data.split(":")[1])
    user_pages[call.message.chat.id] = page
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=make_groups_keyboard(page)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("grp:"))
def group_selected(call):
    group = call.data[4:]
    chat_id = call.message.chat.id
    user_groups[chat_id] = group
    all_users.add(chat_id)

    bot.answer_callback_query(call.id, f"Выбрана группа: {group}")
    bot.edit_message_text(
        f"✅ Группа установлена:\n<b>{group}</b>\n\nТеперь можешь смотреть расписание.",
        chat_id,
        call.message.message_id,
        parse_mode="HTML"
    )
    bot.send_message(chat_id, "Меню:", reply_markup=main_menu_keyboard(True))

@bot.message_handler(commands=['сегодня', 'today'])
@bot.message_handler(func=lambda m: m.text == "📅 Сегодня")
def today_cmd(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    if chat_id not in user_groups:
        bot.send_message(chat_id, "Сначала выбери группу!", reply_markup=make_groups_keyboard(0))
        return
    bot.send_chat_action(chat_id, 'typing')
    result = get_day_schedule(user_groups[chat_id], "today")
    bot.send_message(chat_id, result, parse_mode="HTML")

@bot.message_handler(commands=['завтра', 'tomorrow'])
@bot.message_handler(func=lambda m: m.text == "📅 Завтра")
def tomorrow_cmd(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    if chat_id not in user_groups:
        bot.send_message(chat_id, "Сначала выбери группу!", reply_markup=make_groups_keyboard(0))
        return
    bot.send_chat_action(chat_id, 'typing')
    result = get_day_schedule(user_groups[chat_id], "tomorrow")
    bot.send_message(chat_id, result, parse_mode="HTML")

@bot.message_handler(commands=['неделя', 'week'])
@bot.message_handler(func=lambda m: m.text == "📆 Неделя")
def week_cmd(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    if chat_id not in user_groups:
        bot.send_message(chat_id, "Сначала выбери группу!", reply_markup=make_groups_keyboard(0))
        return
    bot.send_chat_action(chat_id, 'typing')
    result = get_week_schedule(user_groups[chat_id])
    bot.send_message(chat_id, result, parse_mode="HTML")

@bot.message_handler(commands=['изменения', 'changes'])
@bot.message_handler(func=lambda m: m.text == "⚠️ Изменения")
def changes_cmd(message):
    chat_id = message.chat.id
    all_users.add(chat_id)
    bot.send_chat_action(chat_id, 'typing')
    changes = get_changes()
    if changes:
        bot.send_message(chat_id, f"⚠️ <b>Изменения в расписании</b>\n\n{changes}", parse_mode="HTML")
    else:
        bot.send_message(chat_id, "Сейчас изменений в расписании нет.")

# ==================== АВТОМАТИКА ====================

def check_and_send_changes():
    global last_changes_hash

    changes = get_changes()
    if not changes:
        return

    current_hash = hashlib.md5(changes.encode()).hexdigest()

    if current_hash != last_changes_hash:
        last_changes_hash = current_hash
        text = f"⚠️ <b>Появились изменения в расписании!</b>\n\n{changes}"

        for chat_id in list(all_users):
            try:
                bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception:
                all_users.discard(chat_id)

def send_daily():
    if not user_groups:
        return
    for chat_id, group in list(user_groups.items()):
        try:
            result = get_day_schedule(group, "tomorrow")
            bot.send_message(chat_id, "🔔 <b>Расписание на завтра</b>\n\n" + result, parse_mode="HTML")
        except Exception:
            pass

def run_scheduler():
    # Проверка изменений каждые 20 минут
    schedule.every(20).minutes.do(check_and_send_changes)
    # Расписание на завтра в 20:00
    schedule.every().day.at("20:00").do(send_daily)

    # Первая проверка сразу при запуске
    check_and_send_changes()

    while True:
        schedule.run_pending()
        time.sleep(30)

def run_bot():
    bot.infinity_polling()

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
