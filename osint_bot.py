#!/usr/bin/env python3
# RamsEye OSINT v9.1 — SECURITY FIX

import telebot
import subprocess
import os
import requests
import html
import logging
import threading
import time
import sqlite3
import re
import whois
from datetime import datetime
from telebot import types
from urllib.parse import quote
import phonenumbers
from phonenumbers import carrier, geocoder
from bs4 import BeautifulSoup

# ========== КОНФИГ ==========
TOKEN = os.environ.get('TELEGRAM_TOKEN')
bot = telebot.TeleBot(TOKEN)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

task_semaphore = threading.Semaphore(2)
user_step = {}

# ========== АВТОПИНГ ==========
def self_ping():
    url = "https://ramseye-bot.onrender.com"
    while True:
        time.sleep(300)
        try:
            requests.get(url, timeout=10)
            print("✅ Автопинг: сервер активен")
        except Exception as e:
            print(f"❌ Автопинг ошибка: {e}")

threading.Thread(target=self_ping, daemon=True).start()

# ========== ЛОГИ ==========
logging.basicConfig(filename='osint_bot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('searches.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS searches
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER, username TEXT, query_type TEXT, query TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def save_search(user_id, username, query_type, query):
    try:
        conn = sqlite3.connect('searches.db')
        c = conn.cursor()
        c.execute("INSERT INTO searches (user_id, username, query_type, query, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (user_id, username, query_type, query, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except:
        pass

# ========== БЕЗОПАСНЫЙ ВЫЗОВ КОМАНД ==========
def run_cmd_safe(cmd_args, timeout=180):
    """Безопасный вызов subprocess (без shell=True)"""
    try:
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.TimeoutExpired:
        return "⏰ Таймаут 180 секунд"
    except Exception as e:
        logging.error(f"CMD Error: {e}")
        return f"⚠️ Ошибка: {str(e)}"

def run_cmd_background(chat_id, cmd_args, title, user_id, username, query_type, query):
    with task_semaphore:
        try:
            bot.send_message(chat_id, f"🔍 Поиск по {title}... (очередь свободна)")
            result = run_cmd_safe(cmd_args)
            save_search(user_id, username, query_type, query)
            send_long_message(chat_id, result, title)
        except Exception as e:
            logging.error(f"Background error: {e}")
            bot.send_message(chat_id, f"❌ Ошибка: {str(e)}")

def send_long_message(chat_id, text, title):
    if len(text) > 4000:
        fname = f"{title}_{int(time.time())}.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(text)
        with open(fname, "rb") as f:
            bot.send_document(chat_id, f, caption=f"📊 Отчёт: {title}")
        os.remove(fname)
    else:
        try:
            safe_text = html.escape(text)
            bot.send_message(chat_id, f"✅ <b>{title}</b>:\n<pre>{safe_text}</pre>", parse_mode='HTML')
        except:
            bot.send_message(chat_id, f"✅ {title}:\n\n{text[:4000]}")

# ========== ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ ==========
def validate_nick(nick):
    return re.match(r'^[a-zA-Z0-9_]{3,32}$', nick) is not None

def validate_email(email):
    return re.match(r'^[^@]+@[^@]+\.[^@]+$', email) is not None

def validate_phone(phone):
    return re.match(r'^(\+7|8|7)?[\d]{10,11}$', phone.replace('+', '').replace('-', '').replace(' ', '')) is not None

def validate_ip(ip):
    return re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip) is not None

def validate_domain(domain):
    return re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}$', domain) is not None

# ========== ОСНОВНЫЕ МОДУЛИ ==========
def analyze_phone(phone):
    phone_clean = re.sub(r'[^0-9+]', '', phone)
    if phone_clean.startswith('8'):
        phone_clean = '+7' + phone_clean[1:]
    if not phone_clean.startswith('+'):
        phone_clean = '+' + phone_clean
    result = f"📱 <b>Анализ номера {phone_clean}</b>\n\n"
    try:
        num = phonenumbers.parse(phone_clean, None)
        country = geocoder.description_for_number(num, "ru")
        oper = carrier.name_for_number(num, "en")
        result += f"🌍 Страна: {country}\n📡 Оператор: {oper if oper else 'Не определён'}\n"
    except:
        result += "⚠️ Не удалось определить оператора\n"
    result += f"\n🔍 <b>Поиск по номеру:</b>\n• <a href='https://www.google.com/search?q={phone_clean}'>Google</a>\n• <a href='https://vk.com/search?c[phone]={phone_clean}'>VK</a>"
    return result

def whatsapp_check(phone):
    phone_clean = re.sub(r'[^0-9]', '', phone)
    if phone_clean.startswith('8'):
        phone_clean = '7' + phone_clean[1:]
    elif not phone_clean.startswith('7'):
        phone_clean = '7' + phone_clean
    try:
        resp = requests.get(f"https://api.whatsapp.com/send/?phone={phone_clean}", timeout=10)
        if "This phone number is not on WhatsApp" in resp.text:
            return "❌ Номер НЕ зарегистрирован в WhatsApp"
        return f"✅ Номер активен в WhatsApp\n🔗 https://wa.me/{phone_clean}"
    except:
        return "⚠️ Не удалось проверить"

def darknet_search(query):
    results = []
    try:
        url = f"https://ahmia.fi/search/?q={quote(query)}"
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            if '.onion' in link['href']:
                results.append(link['href'])
                if len(results) >= 5:
                    break
    except:
        pass
    if results:
        return "💀 <b>Найдено в даркнете:</b>\n" + "\n".join(results)
    return "🌑 Не найдено в публичных индексах"

def generate_dorks(query):
    q = quote(query)
    dorks = [
        f'"{query}" filetype:pdf', f'"{query}" filetype:xls',
        f'site:vk.com "{query}"', f'site:ok.ru "{query}"',
        f'site:t.me "{query}"', f'site:github.com "{query}"'
    ]
    result = f"🔍 <b>Google Dorks для: {html.escape(query)}</b>\n\n"
    for d in dorks[:8]:
        result += f"• <a href='https://www.google.com/search?q={quote(d)}'>{html.escape(d)}</a>\n"
    return result

def get_ip_info(ip):
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=10)
        data = resp.json()
        if data.get('status') == 'success':
            return f"🌐 <b>IP: {ip}</b>\n📍 {data.get('country', 'Н/Д')}\n🏙 {data.get('city', 'Н/Д')}\n📡 {data.get('isp', 'Н/Д')}\n🗺️ <a href='https://maps.google.com/?q={data.get('lat')},{data.get('lon')}'>Карта</a>"
        return f"❌ Не удалось получить информацию для IP {ip}"
    except:
        return f"⚠️ Ошибка при запросе IP {ip}"

def whois_lookup(domain):
    try:
        w = whois.whois(domain)
        result = f"🌐 <b>WHOIS: {domain}</b>\n\n"
        result += f"📅 Создан: {w.creation_date}\n"
        result += f"⏰ Истекает: {w.expiration_date}\n"
        result += f"🏢 Организация: {w.org or 'Не указана'}\n"
        result += f"📧 Email: {w.emails or 'Не указан'}\n"
        return result
    except Exception as e:
        return f"❌ Ошибка WHOIS: {str(e)}"

# ========== GROQ NEURO ==========
def ask_groq(question):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"❌ Ошибка API: {response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

def process_groq_question(message):
    if message.text == "/cancel":
        bot.send_message(message.chat.id, "Отменено.")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    bot.send_message(message.chat.id, "🤔 Думаю... (Llama 3.3 70B)")
    answer = ask_groq(message.text)
    if len(answer) > 4000:
        fname = f"groq_{int(time.time())}.txt"
        with open(fname, "w") as f:
            f.write(answer)
        with open(fname, "rb") as f:
            bot.send_document(message.chat.id, f, caption="🧠 Ответ Groq")
        os.remove(fname)
    else:
        bot.send_message(message.chat.id, f"🧠 <b>Llama 3.3:</b>\n\n{answer}", parse_mode='HTML')

# ========== INLINE-МЕНЮ ==========
def tools_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("👤 Ник (Maigret)", callback_data="maigret"),
        types.InlineKeyboardButton("📧 Почта (Holehe)", callback_data="holehe"),
        types.InlineKeyboardButton("📱 Телефон", callback_data="phone"),
        types.InlineKeyboardButton("🌐 IP-пробив", callback_data="ip"),
        types.InlineKeyboardButton("💀 Darknet", callback_data="darknet"),
        types.InlineKeyboardButton("🕸 Google Dorks", callback_data="dorks"),
        types.InlineKeyboardButton("📱 WhatsApp", callback_data="whatsapp"),
        types.InlineKeyboardButton("🌍 WHOIS", callback_data="whois"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data="close")
    )
    return markup

@bot.callback_query_handler(func=lambda call: True)
def tools_callback(call):
    if call.data == "close":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Меню закрыто")
        return
    
    bot.answer_callback_query(call.id, f"Выбрано: {call.data}")
    bot.send_message(call.message.chat.id, f"🔎 Введи данные для {call.data}:")
    user_step[call.from_user.id] = call.data

# ========== ГЛАВНОЕ МЕНЮ ==========
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🔍 OSINT Search", "🧠 Нейросеть")
    markup.add("📊 История", "ℹ️ Помощь")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "🛰 <b>RamsEye OSINT v9.1</b>\n\n"
        "🔍 <b>OSINT Search</b> — все инструменты в подменю\n"
        "🧠 <b>Нейросеть</b> — Llama 3.3 70B\n"
        "📊 <b>История</b> — последние поиски\n"
        "ℹ️ <b>Помощь</b> — справка\n\n"
        "👇 Выбери действие",
        parse_mode='HTML', reply_markup=main_menu())
    logging.info(f"Юзер {message.from_user.id} запустил ботаe(message.chat.id, "📭 История пуста")
    elif t == "ℹ️ Помощь":
        bot.send_message(message.chat.id,
            "<b>📖 RamsEye OSINT Bot v9.1</b>\n\n"
            "🔍 OSINT Search — ник, почта, телефон, IP, даркнет, дорки, WhatsApp, WHOIS\n"
            "🧠 Нейросеть — Llama 3.3 70B (Groq)\n\n"
            "Все данные хранятся локально.\n"
            "🔒 Добавлена валидация ввода, безопасный вызов команд",
            parse_mode='HTML')
    elif t == "/cancel":
        if uid in user_step:
            clear_step(uid)
            bot.send_message(message.chat.id, "Отменено.")
        else:
            bot.send_message(message.chat.id, "Нет активного ожидания.")
    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    print("🚀 RamsEye OSINT v9.1 — FINAL")
    print("=" * 40)
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=60)
        except Exception as e:
            logging.error(f"Ошибка: {e}")
            time.sleep(5)    except subprocess.TimeoutExpired:
        return "⏰ Таймаут 180 секунд"
    except Exception as e:
        logging.error(f"CMD Error: {e}")
        return f"⚠️ Ошибка: {str(e)}"

def run_cmd_background(chat_id, cmd, title, user_id, username, query_type, query):
    with task_semaphore:
        try:
            bot.send_message(chat_id, f"🔍 Поиск по {title}... (очередь свободна)")
            result = run_cmd(cmd)
            save_search(user_id, username, query_type, query)
            send_long_message(chat_id, result, title)
        except Exception as e:
            logging.error(f"Background error: {e}")
            bot.send_message(chat_id, f"❌ Ошибка: {str(e)}")

def send_long_message(chat_id, text, title):
    if len(text) > 4000:
        fname = f"{title}_{int(time.time())}.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(text)
        with open(fname, "rb") as f:
            bot.send_document(chat_id, f, caption=f"📊 Отчёт: {title}")
        os.remove(fname)
    else:
        try:
            safe_text = html.escape(text)
            bot.send_message(chat_id, f"✅ <b>{title}</b>:\n<pre>{safe_text}</pre>", parse_mode='HTML')
        except:
            bot.send_message(chat_id, f"✅ {title}:\n\n{text[:4000]}")

# ========== ОСНОВНЫЕ МОДУЛИ ==========
def analyze_phone(phone):
    phone_clean = re.sub(r'[^0-9+]', '', phone)
    if phone_clean.startswith('8'):
        phone_clean = '+7' + phone_clean[1:]
    if not phone_clean.startswith('+'):
        phone_clean = '+' + phone_clean
    result = f"📱 <b>Анализ номера {phone_clean}</b>\n\n"
    try:
        num = phonenumbers.parse(phone_clean, None)
        country = geocoder.description_for_number(num, "ru")
        oper = carrier.name_for_number(num, "en")
        result += f"🌍 Страна: {country}\n📡 Оператор: {oper if oper else 'Не определён'}\n"
    except:
        result += "⚠️ Не удалось определить оператора\n"
    result += f"\n🔍 <b>Поиск по номеру:</b>\n• <a href='https://www.google.com/search?q={phone_clean}'>Google</a>\n• <a href='https://vk.com/search?c[phone]={phone_clean}'>VK</a>"
    return result

def whatsapp_check(phone):
    phone_clean = re.sub(r'[^0-9]', '', phone)
    if phone_clean.startswith('8'):
        phone_clean = '7' + phone_clean[1:]
    elif not phone_clean.startswith('7'):
        phone_clean = '7' + phone_clean
    try:
        resp = requests.get(f"https://api.whatsapp.com/send/?phone={phone_clean}", timeout=10)
        if "This phone number is not on WhatsApp" in resp.text:
            return "❌ Номер НЕ зарегистрирован в WhatsApp"
        return f"✅ Номер активен в WhatsApp\n🔗 https://wa.me/{phone_clean}"
    except:
        return "⚠️ Не удалось проверить"

def darknet_search(query):
    results = []
    try:
        url = f"https://ahmia.fi/search/?q={quote(query)}"
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for link in soup.find_all('a', href=True):
            if '.onion' in link['href']:
                results.append(link['href'])
                if len(results) >= 5: break
    except:
        pass
    if results:
        return "💀 <b>Найдено в даркнете:</b>\n" + "\n".join(results)
    return "🌑 Не найдено в публичных индексах"

def generate_dorks(query):
    q = quote(query)
    dorks = [
        f'"{query}" filetype:pdf', f'"{query}" filetype:xls',
        f'site:vk.com "{query}"', f'site:ok.ru "{query}"',
        f'site:t.me "{query}"', f'site:github.com "{query}"'
    ]
    result = f"🔍 <b>Google Dorks для: {html.escape(query)}</b>\n\n"
    for d in dorks[:8]:
        result += f"• <a href='https://www.google.com/search?q={quote(d)}'>{html.escape(d)}</a>\n"
    return result

def get_ip_info(ip):
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=10)
        data = resp.json()
        if data.get('status') == 'success':
            return f"🌐 <b>IP: {ip}</b>\n📍 {data.get('country')}\n🏙 {data.get('city')}\n📡 {data.get('isp')}"
        return f"❌ Не удалось получить информацию для IP {ip}"
    except:
        return f"⚠️ Ошибка при запросе IP {ip}"

# ========== GROQ NEURO ==========
def ask_groq(question):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"❌ Ошибка API: {response.status_code}\n{response.text[:200]}"
    except Exception as e:
        return f"❌ Ошибка подключения: {str(e)}"

def process_groq_question(message):
    if message.text == "/cancel":
        bot.send_message(message.chat.id, "Отменено.")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    bot.send_message(message.chat.id, "🤔 Думаю... (Llama 3.3 70B)")
    answer = ask_groq(message.text)
    if len(answer) > 4000:
        fname = f"groq_{int(time.time())}.txt"
        with open(fname, "w") as f: f.write(answer)
        with open(fname, "rb") as f: bot.send_document(message.chat.id, f, caption="🧠 Ответ Groq")
        os.remove(fname)
    else:
        bot.send_message(message.chat.id, f"🧠 <b>Llama 3.3 (Groq):</b>\n\n{answer}", parse_mode='HTML')

# ========== МЕНЮ ==========
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("👤 Ник (Maigret)", "📧 Почта (Holehe)")
    markup.add("📱 Телефон", "🌐 IP-пробив")
    markup.add("💀 Darknet", "🕸 Google Dorks")
    markup.add("📱 WhatsApp", "🧠 Llama 3.3")
    markup.add("📊 История", "ℹ️ Помощь")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "🛰 <b>OSINT v8.2 — Llama 3.3 Integrated</b>\n\n"
        "🔹 <b>Ник</b> (Maigret)\n🔹 <b>Почта</b> (Holehe)\n🔹 <b>Телефон / IP / WhatsApp</b>\n"
        "🔹 <b>Darknet / Dorks</b>\n🔹 <b>Llama 3.3 70B</b> (Groq)\n\nВыбери действие 👇",
        parse_mode='HTML', reply_markup=main_menu())
    logging.info(f"Юзер {message.from_user.id} запустил бота")

# ========== ОБРАБОТЧИКИ ==========
def clear_step(user_id):
    if user_id in user_step: del user_step[user_id]

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    t = message.text
    uid = message.from_user.id
    uname = message.from_user.username or "unknown"
    
    # Ожидание ввода
    if uid in user_step:
        expected = user_step[uid]
        menu_commands = ["👤 Ник (Maigret)", "📧 Почта (Holehe)", "📱 Телефон", "🌐 IP-пробив",
                         "💀 Darknet", "🕸 Google Dorks", "📱 WhatsApp", "🧠 Llama 3.3", "📊 История", "ℹ️ Помощь"]
        if t in menu_commands:
            clear_step(uid)
        else:
            clear_step(uid)
            if expected == "maigret":
                threading.Thread(target=run_cmd_background, args=(message.chat.id, f"maigret --txt {t} --timeout 30", "Maigret", uid, uname, "nick", t)).start()
            elif expected == "holehe":
                threading.Thread(target=run_cmd_background, args=(message.chat.id, f"holehe {t} --only-used", "Holehe", uid, uname, "email", t)).start()
            elif expected == "phone": bot.send_message(message.chat.id, analyze_phone(t), parse_mode='HTML')
            elif expected == "ip": bot.send_message(message.chat.id, get_ip_info(t), parse_mode='HTML')
            elif expected == "darknet": bot.send_message(message.chat.id, darknet_search(t), parse_mode='HTML')
            elif expected == "dorks": bot.send_message(message.chat.id, generate_dorks(t), parse_mode='HTML', disable_web_page_preview=True)
            elif expected == "whatsapp": bot.send_message(message.chat.id, whatsapp_check(t), parse_mode='HTML')
            elif expected == "groq": process_groq_question(message)
            return

    # Команды меню
    if t == "👤 Ник (Maigret)": bot.send_message(message.chat.id, "🔎 Введи никнейм:"); user_step[uid] = "maigret"
    elif t == "📧 Почта (Holehe)": bot.send_message(message.chat.id, "📧 Введи email:"); user_step[uid] = "holehe"
    elif t == "📱 Телефон": bot.send_message(message.chat.id, "📱 Введи номер (+7...):"); user_step[uid] = "phone"
    elif t == "🌐 IP-пробив": bot.send_message(message.chat.id, "🌐 Введи IP:"); user_step[uid] = "ip"
    elif t == "💀 Darknet": bot.send_message(message.chat.id, "💀 Введи ник/email:"); user_step[uid] = "darknet"
    elif t == "🕸 Google Dorks": bot.send_message(message.chat.id, "🔎 Введи имя/ник:"); user_step[uid] = "dorks"
    elif t == "📱 WhatsApp": bot.send_message(message.chat.id, "📱 Введи номер (+7...):"); user_step[uid] = "whatsapp"
    elif t == "🧠 Llama 3.3": bot.send_message(message.chat.id, "🧠 Задай вопрос (или /cancel):"); user_step[uid] = "groq"
    elif t == "📊 История":
        conn = sqlite3.connect('searches.db')
        c = conn.cursor()
        c.execute("SELECT query_type, query, timestamp FROM searches WHERE user_id = ? ORDER BY id DESC LIMIT 10", (uid,))
        rows = c.fetchall()
        conn.close()
        if rows:
            hist = "<b>📜 Последние 10 поисков:</b>\n\n"
            for row in rows: hist += f"🔹 <b>{row[0]}</b>: {row[1][:30]}\n   ⏰ {row[2]}\n\n"
            bot.send_message(message.chat.id, hist, parse_mode='HTML')
        else: bot.send_message(message.chat.id, "📭 История пуста")
    elif t == "ℹ️ Помощь":
        bot.send_message(message.chat.id, "Выбери пункт меню.\nДля админа: /admin\nДанные локально.", parse_mode='HTML')
    elif t == "/cancel":
        if uid in user_step: clear_step(uid); bot.send_message(message.chat.id, "Ожидание ввода отменено.")
        else: bot.send_message(message.chat.id, "Нет активного ожидания.")
    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    print("🚀 OSINT BOT v8.2 — Llama 3.3 READY")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print("=" * 40)
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=60)
        except Exception as e:
            logging.error(f"Ошибка: {e}")
            time.sleep(5)
