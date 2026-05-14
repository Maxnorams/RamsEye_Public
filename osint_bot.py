#!/usr/bin/env python3
# RamsEye OSINT v6.1 — COMPLETE (Library Mode, Full Prompt)

import telebot
import os
import re
import html
import time
import logging
import threading
import requests
import json
import dns.resolver
import asyncio
import socket
import whois
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from telebot import types
from flask import Flask
from maigret import Maigret
from holehe import check_email
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

# ========== КОНФИГ ==========
TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '0'))
GROQ_KEY = os.environ.get('GROQ_API_KEY')
SHODAN_KEY = os.environ.get('SHODAN_API_KEY')
RENDER_URL = os.environ.get('RENDER_URL', '')

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

bot = telebot.TeleBot(TOKEN)
session = requests.Session()
session.headers.update({'User-Agent': 'RamsEye-OSINT/6.1'})

task_semaphore = threading.Semaphore(3)
user_step = {}
_step_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('RamsEye')

# ========== FLASK + AUTOPING ==========
app = Flask(__name__)

@app.route('/')
def health():
    return f"RamsEye OSINT v6.1 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

def self_ping():
    if not RENDER_URL:
        return
    while True:
        time.sleep(280)
        try:
            session.get(RENDER_URL, timeout=10)
            log.info("Автопинг OK")
        except:
            pass

threading.Thread(target=self_ping, daemon=True).start()

# ========== АВТОРИЗАЦИЯ ==========
def is_allowed(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

def auth_check(obj):
    uid = obj.from_user.id if hasattr(obj, 'from_user') else obj
    return is_allowed(uid)

# ========== СОСТОЯНИЯ ==========
def set_step(uid, step):
    with _step_lock:
        user_step[uid] = step

def get_step(uid):
    with _step_lock:
        return user_step.get(uid)

def clear_step(uid):
    with _step_lock:
        user_step.pop(uid, None)

# ========== ВАЛИДАЦИЯ ==========
def validate_nick(v): return bool(re.match(r'^[a-zA-Z0-9_]{3,32}$', v))
def validate_email(v): return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', v))
def validate_ip(v):
    try:
        parts = v.split('.')
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
    except:
        return False
def validate_domain(v):
    return bool(re.match(r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$', v))

# ========== ОТПРАВКА РЕЗУЛЬТАТОВ ==========
def send_result(chat_id, text, title):
    if len(text) > 3800:
        fname = f"/tmp/{title}_{int(time.time())}.txt"
        try:
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(text)
            with open(fname, 'rb') as f:
                bot.send_document(chat_id, f, caption=f"📊 {title}")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Ошибка: {e}")
        finally:
            if os.path.exists(fname):
                os.remove(fname)
    else:
        try:
            bot.send_message(chat_id, f"<b>📊 {html.escape(title)}</b>\n<pre>{html.escape(text)}</pre>", parse_mode='HTML')
        except:
            bot.send_message(chat_id, text[:4000])

# ========== MAIGRET (БИБЛИОТЕКА) ==========
def maigret_lookup(username):
    try:
        m = Maigret()
        result = m.search(username, timeout=30)
        if not result:
            return "❌ Ничего не найдено"
        lines = [f"👤 <b>Результаты для {username}</b>\n"]
        for site, data in result.items():
            if data.get('status', {}).get('exists'):
                url = data.get('url', '')
                if url:
                    lines.append(f"• {site}: <a href='{url}'>{url}</a>")
        return '\n'.join(lines[:30])
    except Exception as e:
        return f"❌ Ошибка Maigret: {e}"

# ========== HOLEHE (БИБЛИОТЕКА) ==========
async def holehe_async(email):
    try:
        return await check_email(email)
    except:
        return {}

def holehe_lookup(email):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(holehe_async(email))
        loop.close()
        found = []
        for service, data in result.items():
            if data.get('rateLimit') or not data.get('exists'):
                continue
            found.append(f"✅ {service}")
        if not found:
            return "❌ Аккаунты не найдены"
        return "📧 <b>Найденные сервисы:</b>\n" + '\n'.join(found[:30])
    except Exception as e:
        return f"❌ Ошибка Holehe: {e}"

# ========== IP ИНФО ==========
def get_ip_info(ip):
    try:
        r = session.get(f"https://ipinfo.io/{ip}/json", timeout=10)
        d = r.json()
        loc = d.get('loc', '0,0').split(',')
        lat, lon = loc[0], loc[1]
        lines = [
            f"🌐 <b>IP: {ip}</b>",
            f"🏳 Страна: {d.get('country', 'Н/Д')}",
            f"🏙 Город: {d.get('city', 'Н/Д')}",
            f"🏢 Регион: {d.get('region', 'Н/Д')}",
            f"📡 ISP: {d.get('org', 'Н/Д')}",
            f"🌍 Хостнейм: {d.get('hostname', 'Н/Д')}",
            f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>Карта</a>",
        ]
        try:
            r2 = session.get(f"https://ipinfo.io/{ip}/privacy", timeout=5)
            priv = r2.json()
            lines.append(f"🕵️ VPN: {priv.get('vpn', '?')} | Proxy: {priv.get('proxy', '?')} | Tor: {priv.get('tor', '?')}")
        except:
            pass
        return '\n'.join(lines)
    except Exception as e:
        return f"⚠️ Ошибка: {e}"

# ========== DNS РАЗВЕДКА ==========
def dns_recon(domain):
    lines = [f"🔍 <b>DNS-разведка: {html.escape(domain)}</b>\n"]
    record_types = ['A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA']
    for rtype in record_types:
        try:
            answers = dns.resolver.resolve(domain, rtype, lifetime=5)
            vals = [str(r) for r in answers]
            lines.append(f"<b>{rtype}:</b> {', '.join(vals)}")
        except:
            lines.append(f"<b>{rtype}:</b> —")
    lines.append("\n<b>📜 Субдомены (crt.sh):</b>")
    try:
        r = session.get(f"https://crt.sh/?q=%.{domain}&output=json", timeout=15)
        subs = set()
        for entry in r.json():
            name = entry.get('name_value', '')
            for sub in name.split('\n'):
                sub = sub.strip().lstrip('*.')
                if sub.endswith(domain) and sub != domain:
                    subs.add(sub)
        if subs:
            lines.extend([f"  • {s}" for s in sorted(subs)[:20]])
        else:
            lines.append("  Субдомены не найдены")
    except:
        lines.append("  ⚠️ crt.sh ошибка")
    return '\n'.join(lines)

# ========== SHODAN ==========
def shodan_lookup(ip):
    if not SHODAN_KEY:
        return "⚠️ SHODAN_API_KEY не задан"
    try:
        r = session.get(f"https://api.shodan.io/shodan/host/{ip}", params={"key": SHODAN_KEY}, timeout=15)
        if r.status_code != 200:
            return f"❌ Shodan: {r.status_code}"
        d = r.json()
        lines = [f"🔭 <b>Shodan: {ip}</b>", f"🏢 Организация: {d.get('org', 'Н/Д')}", f"🌍 Страна: {d.get('country_name', 'Н/Д')}", f"🖥 ОС: {d.get('os', 'Не определена')}", "\n<b>📡 Открытые порты:</b>"]
        for item in d.get('data', [])[:10]:
            lines.append(f"  • {item.get('port')}/{item.get('transport')} {item.get('product', '')} {item.get('version', '')}")
        vulns = d.get('vulns', {})
        if vulns:
            lines.append(f"\n<b>⚠️ Уязвимости CVE ({len(vulns)}):</b>")
            for cve in list(vulns.keys())[:5]:
                lines.append(f"  🔴 {cve}")
        return '\n'.join(lines)
    except Exception as e:
        return f"⚠️ Shodan ошибка: {e}"

# ========== WHOIS ==========
def whois_lookup(domain):
    try:
        w = whois.whois(domain)
        lines = [f"🌐 <b>WHOIS: {html.escape(domain)}</b>\n"]
        fields = {'Регистратор': w.registrar, 'Создан': w.creation_date, 'Истекает': w.expiration_date, 'Организация': w.org, 'Страна': w.country, 'Email': w.emails, 'Серверы имён': w.name_servers}
        for label, val in fields.items():
            if val:
                lines.append(f"<b>{label}:</b> {html.escape(str(val)[:200])}")
        return '\n'.join(lines)
    except Exception as e:
        return f"❌ WHOIS ошибка: {e}"

# ========== GOOGLE DORKS ==========
def generate_dorks(query):
    dorks = [
        f'"{query}" filetype:pdf', f'"{query}" filetype:xls OR filetype:xlsx', f'"{query}" filetype:doc OR filetype:docx',
        f'site:vk.com "{query}"', f'site:ok.ru "{query}"', f'site:t.me "{query}"', f'site:github.com "{query}"',
        f'site:linkedin.com "{query}"', f'site:instagram.com "{query}"', f'"{query}" inurl:admin',
        f'"{query}" intext:password', f'"{query}" site:pastebin.com'
    ]
    result = f"🕸 <b>Google Dorks: {html.escape(query)}</b>\n\n"
    for d in dorks:
        result += f"• <a href='https://www.google.com/search?q={quote(d)}'>{html.escape(d)}</a>\n"
    return result

# ========== EXIF ==========
def extract_exif(file_path):
    try:
        img = Image.open(file_path)
        exif_data = img._getexif()
        if not exif_data:
            return "📷 EXIF-данные отсутствуют"
        lines = ["📷 <b>EXIF-данные:</b>\n"]
        gps_info = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'GPSInfo':
                for gps_tag_id, gps_val in value.items():
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_info[gps_tag] = gps_val
            elif tag in ('Make', 'Model', 'DateTime', 'Software', 'Artist', 'Copyright', 'ImageDescription'):
                lines.append(f"<b>{tag}:</b> {html.escape(str(value)[:100])}")
        if gps_info:
            lines.append(f"\n📍 <b>GPS координаты:</b>")
            try:
                def to_decimal(dms, ref):
                    d, m, s = dms
                    dec = float(d) + float(m)/60 + float(s)/3600
                    if ref in ['S', 'W']:
                        dec = -dec
                    return round(dec, 6)
                lat = to_decimal(gps_info['GPSLatitude'], gps_info['GPSLatitudeRef'])
                lon = to_decimal(gps_info['GPSLongitude'], gps_info['GPSLongitudeRef'])
                lines.append(f"Lat: {lat}, Lon: {lon}")
                lines.append(f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>Открыть на карте</a>")
            except:
                lines.append("📍 GPS-данные есть, но не удалось расшифровать")
        return '\n'.join(lines)
    except Exception as e:
        return f"⚠️ EXIF ошибка: {e}"

# ========== GROQ AI С ПРОМТОМ ==========
SYSTEM_PROMPT = """Ты — RamsEye AI, профессиональный OSINT-ассистент.
Твои задачи:
- Анализ данных из открытых источников
- Поиск связей между никами, email, IP, доменами
- Объяснение методов OSINT-расследований
- Помощь в интерпретации результатов инструментов (Maigret, Shodan, DNS)
- Составление стратегии расследования

Ты работаешь строго в рамках закона и этики OSINT.
Отвечай структурированно, детально, на русском языке.
Если запрос неоднозначен — уточни цель исследования."""

def ask_groq(question, context=""):
    if not GROQ_KEY:
        return "⚠️ GROQ_API_KEY не задан"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "user", "content": f"Контекст:\n{context}"})
        messages.append({"role": "assistant", "content": "Понял, учту контекст."})
    messages.append({"role": "user", "content": question})
    try:
        r = session.post(GROQ_URL, headers={"Authorization": f"Bearer {GROQ_KEY}"},
                         json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.6, "max_tokens": 1500}, timeout=30)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        return f"❌ Groq API ошибка: {r.status_code}"
    except Exception as e:
        return f"❌ Groq ошибка: {e}"

# ========== КЛАСТЕРИЗАЦИЯ ==========
def cluster_data(data_text):
    return ask_groq(f"Проанализируй данные OSINT-расследования. Найди и структурируй: идентификаторы, связи между ними, временную шкалу, выводы, рекомендации. Данные:\n{data_text}")

# ========== INLINE-МЕНЮ ==========
def tools_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("👤 MAIGRET", callback_data="maigret"),
        types.InlineKeyboardButton("📧 HOLEHE", callback_data="holehe"),
        types.InlineKeyboardButton("🌐 IP-ПРОБИВ", callback_data="ip"),
        types.InlineKeyboardButton("🔭 SHODAN", callback_data="shodan"),
        types.InlineKeyboardButton("🔍 DNS", callback_data="dns"),
        types.InlineKeyboardButton("🌍 WHOIS", callback_data="whois"),
        types.InlineKeyboardButton("🕸 DORKS", callback_data="dorks"),
        types.InlineKeyboardButton("🧠 CLUSTER", callback_data="cluster"),
        types.InlineKeyboardButton("📷 EXIF-ФОТО", callback_data="exif"),
        types.InlineKeyboardButton("❌ ЗАКРЫТЬ", callback_data="close"),
    )
    return m

def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add("🔍 OSINT SEARCH", "🧠 RAMSEYE AI")
    m.add("📂 DOSSIER", "ℹ️ ПОМОЩЬ")
    return m

# ========== DOSSIER (ПАРАЛЛЕЛЬНЫЙ) ==========
def run_dossier(target, chat_id):
    with task_semaphore:
        bot.send_message(chat_id, f"📂 Сбор досье для {html.escape(target)}...")
        results = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            if validate_nick(target):
                results['MAIGRET'] = ex.submit(maigret_lookup, target)
            if validate_email(target):
                results['HOLEHE'] = ex.submit(holehe_lookup, target)
            if validate_ip(target):
                results['IP'] = ex.submit(get_ip_info, target)
                results['SHODAN'] = ex.submit(shodan_lookup, target)
            if validate_domain(target):
                results['DNS'] = ex.submit(dns_recon, target)
                results['WHOIS'] = ex.submit(whois_lookup, target)
            results['DORKS'] = ex.submit(generate_dorks, target)

            output = {}
            for name, f in results.items():
                try:
                    output[name] = f.result(timeout=150)
                except Exception as e:
                    output[name] = f"❌ Ошибка: {e}"

        bot.send_message(chat_id, "🧠 AI анализирует связи...")
        ai_summary = ask_groq(f"Цель: {target}\nПроанализируй результаты OSINT-сбора:", context="\n\n".join([f"[{name}]\n{text[:1000]}" for name, text in output.items()]))

        report = f"Цель: {target}\nДата: {datetime.now()}\n\n" + "\n\n".join([f"[{name}]\n{text}" for name, text in output.items()]) + f"\n\n[AI АНАЛИЗ]\n{ai_summary}"
        send_result(chat_id, report, f"Досье: {target}")

# ========== ОБРАБОТЧИКИ TELEGRAM ==========
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    bot.send_message(message.chat.id,
        "🦾 <b>RAMSEYE OSINT v6.1</b>\n\n"
        "🔍 OSINT SEARCH — все инструменты\n"
        "🧠 RAMSEYE AI — Llama 4 Scout\n"
        "📂 DOSSIER — параллельный сбор + AI\n"
        "👇 Выбери действие",
        parse_mode='HTML', reply_markup=main_menu())
    log.info(f"Start: uid={message.from_user.id}")

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    if not auth_check(call):
        bot.answer_callback_query(call.id, "❌ Доступ запрещён")
        return
    if call.data == "close":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Закрыто")
        return
    prompts = {
        "maigret": "👤 Введи ник:", "holehe": "📧 Введи email:", "ip": "🌐 Введи IP:",
        "shodan": "🔭 Введи IP для Shodan:", "dns": "🔍 Введи домен:", "whois": "🌍 Введи домен:",
        "dorks": "🕸 Введи запрос:", "cluster": "🧠 Вставь данные для анализа:",
        "exif": "📷 Отправь фотографию (файлом)"
    }
    clear_step(call.from_user.id)
    set_step(call.from_user.id, call.data)
    bot.send_message(call.message.chat.id, prompts.get(call.data, "Введи данные:"))

@bot.message_handler(content_types=['photo', 'document'])
def on_media(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    if get_step(message.from_user.id) == "exif":
        clear_step(message.from_user.id)
        bot.send_message(message.chat.id, "📷 Извлекаю EXIF...")
        try:
            if message.document:
                file_info = bot.get_file(message.document.file_id)
            else:
                file_info = bot.get_file(message.photo[-1].file_id)
            downloaded = bot.download_file(file_info.file_path)
            fpath = f"/tmp/exif_{int(time.time())}.jpg"
            with open(fpath, 'wb') as f:
                f.write(downloaded)
            result = extract_exif(fpath)
            bot.send_message(message.chat.id, result, parse_mode='HTML')
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            if os.path.exists(fpath):
                os.remove(fpath)
    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())

@bot.message_handler(func=lambda m: True, content_types=['text'])
def on_text(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    text = message.text or ""
    uid = message.from_user.id
    step = get_step(uid)

    if step:
        if text == "/cancel":
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
            return
        clear_step(uid)

        if step == "maigret":
            if validate_nick(text):
                result = maigret_lookup(text)
                send_result(message.chat.id, result, "MAIGRET")
            else:
                bot.send_message(message.chat.id, "❌ Некорректный ник")
        elif step == "holehe":
            if validate_email(text):
                result = holehe_lookup(text)
                send_result(message.chat.id, result, "HOLEHE")
            else:
                bot.send_message(message.chat.id, "❌ Некорректный email")
        elif step == "ip":
            if validate_ip(text):
                result = get_ip_info(text)
                bot.send_message(message.chat.id, result, parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP")
        elif step == "shodan":
            if validate_ip(text):
                result = shodan_lookup(text)
                bot.send_message(message.chat.id, result, parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP")
        elif step == "dns":
            if validate_domain(text):
                result = dns_recon(text)
                send_result(message.chat.id, result, "DNS")
            else:
                bot.send_message(message.chat.id, "❌ Некорректный домен")
        elif step == "whois":
            if validate_domain(text):
                result = whois_lookup(text)
                bot.send_message(message.chat.id, result, parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Некорректный домен")
        elif step == "dorks":
            result = generate_dorks(text)
            bot.send_message(message.chat.id, result, parse_mode='HTML', disable_web_page_preview=True)
        elif step == "cluster":
            bot.send_message(message.chat.id, "🧠 Анализирую связи...")
            result = cluster_data(text)
            send_result(message.chat.id, result, "CLUSTER")
        elif step == "exif":
            bot.send_message(message.chat.id, "📷 Отправь фото файлом")
            set_step(uid, "exif")
        return

    if text == "🔍 OSINT SEARCH":
        bot.send_message(message.chat.id, "🔍 <b>Выбери инструмент:</b>", parse_mode='HTML', reply_markup=tools_menu())
    elif text == "🧠 RAMSEYE AI":
        set_step(uid, "groq")
        bot.send_message(message.chat.id, "🧠 Задай вопрос (или /cancel):")
    elif text == "📂 DOSSIER":
        set_step(uid, "dossier")
        bot.send_message(message.chat.id, "📂 Введи цель (ник, email, IP, домен):")
    elif text == "ℹ️ ПОМОЩЬ":
        bot.send_message(message.chat.id,
            "<b>📖 RamsEye OSINT v6.1</b>\n\n"
            "👤 Maigret — поиск ника по 500+ соцсетям\n"
            "📧 Holehe — проверка email по сервисам\n"
            "🌐 IP — геолокация, ISP, VPN/Proxy/Tor\n"
            "🔭 Shodan — порты, CVE, баннеры\n"
            "🔍 DNS — A/MX/TXT/NS + субдомены\n"
            "🌍 WHOIS — регистратор, даты, контакты\n"
            "🕸 Dorks — 12 Google Dorks\n"
            "🧠 Cluster — AI-анализ связей\n"
            "📷 EXIF — GPS и метаданные из фото\n"
            "📂 Dossier — параллельный сбор + AI\n\n"
            "Доступ только для авторизованного пользователя.",
            parse_mode='HTML')
    elif text == "/cancel":
        if get_step(uid):
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
        else:
            bot.send_message(message.chat.id, "Нет активного ожидания.")
    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    print("🦾 RamsEye OSINT v6.1 — COMPLETE")
    if not ADMIN_ID or not TOKEN:
        print("❌ FATAL: задайте ADMIN_ID и TELEGRAM_TOKEN")
        exit(1)
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=60)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
