#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════╗
# ║          RamsEye OSINT v6.0 — PROFESSIONAL EDITION              ║
# ║     Optimized | Parallel | PDF Reports | Shodan | DNS | EXIF    ║
# ╚══════════════════════════════════════════════════════════════════╝

import telebot
import subprocess
import os
import re
import html
import time
import logging
import threading
import requests
import json
import socket
import dns.resolver
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from telebot import types
from flask import Flask

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КОНФИГ — все секреты через env, никаких хардкодов
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKEN       = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID    = int(os.environ.get('ADMIN_ID', '0'))        # ← env, не хардкод
GROQ_KEY    = os.environ.get('GROQ_API_KEY')
SHODAN_KEY  = os.environ.get('SHODAN_API_KEY')            # опционально
RENDER_URL  = os.environ.get('RENDER_URL', '')

GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ИНИЦИАЛИЗАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
bot = telebot.TeleBot(TOKEN)

# Общая HTTP-сессия (connection pool, User-Agent)
session = requests.Session()
session.headers.update({
    'User-Agent': 'RamsEye-OSINT/6.0 (Professional)',
    'Accept': 'application/json'
})

# Семафор: не более 3 тяжёлых задач одновременно
task_semaphore = threading.Semaphore(3)

# Состояние пользователя (thread-safe)
_step_lock = threading.Lock()
user_step: dict = {}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('ramseye.log', encoding='utf-8')
    ]
)
log = logging.getLogger('RamsEye')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FLASK KEEP-ALIVE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)

@app.route('/')
def health():
    return f"RamsEye OSINT v6.0 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

def self_ping():
    if not RENDER_URL:
        return
    while True:
        time.sleep(280)
        try:
            session.get(RENDER_URL, timeout=10)
            log.info("Автопинг OK")
        except Exception as e:
            log.warning(f"Автопинг ошибка: {e}")

threading.Thread(target=self_ping, daemon=True).start()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  АВТОРИЗАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_allowed(user_id: int) -> bool:
    if ADMIN_ID == 0:
        log.warning("ADMIN_ID не задан — бот открыт всем!")
        return False
    return user_id == ADMIN_ID

def auth_check(message_or_call):
    """Декоратор-обёртка для проверки доступа."""
    uid = (message_or_call.from_user.id
           if hasattr(message_or_call, 'from_user')
           else message_or_call)
    return is_allowed(uid)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  УПРАВЛЕНИЕ СОСТОЯНИЕМ (thread-safe)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def set_step(uid: int, step: str):
    with _step_lock:
        user_step[uid] = step

def get_step(uid: int) -> str | None:
    with _step_lock:
        return user_step.get(uid)

def clear_step(uid: int):
    with _step_lock:
        user_step.pop(uid, None)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ВАЛИДАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def validate_nick(v: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9_]{3,32}$', v))

def validate_email(v: str) -> bool:
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', v))

def validate_ip(v: str) -> bool:
    try:
        parts = v.split('.')
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
    except Exception:
        return False

def validate_domain(v: str) -> bool:
    return bool(re.match(
        r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$', v
    ))

def validate_phone(v: str) -> bool:
    clean = re.sub(r'[\s\-\(\)\+]', '', v)
    return bool(re.match(r'^[78]?\d{10}$', clean))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  УТИЛИТЫ ОТПРАВКИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_result(chat_id: int, text: str, title: str):
    """Умная отправка: короткое — сообщением, длинное — файлом."""
    if len(text) > 3800:
        fname = f"/tmp/{title}_{int(time.time())}.txt"
        try:
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(text)
            with open(fname, 'rb') as f:
                bot.send_document(chat_id, f, caption=f"📊 {title}")
        except Exception as e:
            log.error(f"send_result file error: {e}")
            bot.send_message(chat_id, f"❌ Ошибка отправки: {e}")
        finally:
            if os.path.exists(fname):
                os.remove(fname)
    else:
        try:
            escaped = html.escape(text)
            bot.send_message(
                chat_id,
                f"<b>📊 {html.escape(title)}</b>\n<pre>{escaped}</pre>",
                parse_mode='HTML'
            )
        except Exception as e:
            log.error(f"send_result msg error: {e}")
            bot.send_message(chat_id, text[:4000])

def run_tool_bg(chat_id: int, cmd: list, title: str):
    """Запуск CLI-инструмента в фоне с семафором."""
    def _worker():
        with task_semaphore:
            bot.send_message(chat_id, f"⏳ {title} запущен...")
            t0 = time.time()
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=200
                )
                out = r.stdout or r.stderr or "Нет вывода"
                elapsed = time.time() - t0
                out += f"\n\n⏱ Время: {elapsed:.1f} сек"
                send_result(chat_id, out, title)
            except subprocess.TimeoutExpired:
                bot.send_message(chat_id, f"⏰ {title}: таймаут 200 сек")
            except Exception as e:
                log.error(f"run_tool_bg {title}: {e}")
                bot.send_message(chat_id, f"❌ {title}: {e}")
    threading.Thread(target=_worker, daemon=True).start()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: IP-РАЗВЕДКА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_ip_info(ip: str) -> str:
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
            f"📮 Почтовый индекс: {d.get('postal', 'Н/Д')}",
            f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>Карта</a>",
        ]
        # Проверка на Tor/VPN/Proxy (бесплатный эндпоинт)
        try:
            r2 = session.get(f"https://ipinfo.io/{ip}/privacy", timeout=5)
            priv = r2.json()
            lines.append(f"🕵️ VPN: {priv.get('vpn', '?')} | Proxy: {priv.get('proxy', '?')} | Tor: {priv.get('tor', '?')}")
        except Exception:
            pass
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"get_ip_info: {e}")
        return f"⚠️ Ошибка IP-запроса: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: DNS-РАЗВЕДКА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def dns_recon(domain: str) -> str:
    lines = [f"🔍 <b>DNS-разведка: {html.escape(domain)}</b>\n"]
    record_types = ['A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA']

    for rtype in record_types:
        try:
            answers = dns.resolver.resolve(domain, rtype, lifetime=5)
            vals = [str(r) for r in answers]
            lines.append(f"<b>{rtype}:</b> {', '.join(vals)}")
        except Exception:
            lines.append(f"<b>{rtype}:</b> —")

    # Субдомены через crt.sh
    lines.append("\n<b>📜 Субдомены (crt.sh):</b>")
    try:
        r = session.get(
            f"https://crt.sh/?q=%.{domain}&output=json", timeout=15
        )
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
    except Exception as e:
        lines.append(f"  ⚠️ crt.sh ошибка: {e}")

    # Обратный DNS
    lines.append("\n<b>🔄 Обратный DNS (A-записи):</b>")
    try:
        answers = dns.resolver.resolve(domain, 'A', lifetime=5)
        for r in answers:
            ip = str(r)
            try:
                hostname = socket.gethostbyaddr(ip)[0]
                lines.append(f"  {ip} → {hostname}")
            except Exception:
                lines.append(f"  {ip} → (нет PTR)")
    except Exception:
        lines.append("  —")

    return '\n'.join(lines)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: SHODAN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def shodan_lookup(ip: str) -> str:
    if not SHODAN_KEY:
        return "⚠️ SHODAN_API_KEY не задан в env"
    try:
        r = session.get(
            f"https://api.shodan.io/shodan/host/{ip}",
            params={"key": SHODAN_KEY}, timeout=15
        )
        if r.status_code != 200:
            return f"❌ Shodan: {r.status_code} — {r.text[:200]}"
        d = r.json()
        lines = [
            f"🔭 <b>Shodan: {ip}</b>",
            f"🏢 Организация: {d.get('org', 'Н/Д')}",
            f"🌍 Страна: {d.get('country_name', 'Н/Д')}",
            f"🖥 ОС: {d.get('os', 'Не определена')}",
            f"📅 Последнее обновление: {d.get('last_update', 'Н/Д')}",
            f"\n<b>📡 Открытые порты:</b>",
        ]
        for item in d.get('data', [])[:10]:
            port = item.get('port', '?')
            transport = item.get('transport', 'tcp')
            product = item.get('product', '')
            version = item.get('version', '')
            banner = item.get('banner', '')[:80].replace('\n', ' ')
            lines.append(f"  • {port}/{transport} {product} {version} — {banner}")

        vulns = d.get('vulns', {})
        if vulns:
            lines.append(f"\n<b>⚠️ Уязвимости CVE ({len(vulns)}):</b>")
            for cve in list(vulns.keys())[:5]:
                lines.append(f"  🔴 {cve}")
            if len(vulns) > 5:
                lines.append(f"  ... и ещё {len(vulns) - 5}")

        return '\n'.join(lines)
    except Exception as e:
        log.error(f"shodan_lookup: {e}")
        return f"⚠️ Shodan ошибка: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: WHOIS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def whois_lookup(domain: str) -> str:
    try:
        import whois as pywhois
        w = pywhois.whois(domain)
        lines = [f"🌐 <b>WHOIS: {html.escape(domain)}</b>\n"]
        fields = {
            'Регистратор': w.registrar,
            'Создан': w.creation_date,
            'Истекает': w.expiration_date,
            'Обновлён': w.updated_date,
            'Организация': w.org,
            'Страна': w.country,
            'Email': w.emails,
            'Серверы имён': w.name_servers,
            'Статус': w.status,
        }
        for label, val in fields.items():
            if val:
                if isinstance(val, list):
                    val = val[0] if len(val) == 1 else ', '.join(str(v) for v in val[:3])
                lines.append(f"<b>{label}:</b> {html.escape(str(val)[:200])}")
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"whois_lookup: {e}")
        return f"❌ WHOIS ошибка: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: GOOGLE DORKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def generate_dorks(query: str) -> str:
    dorks = [
        f'"{query}" filetype:pdf',
        f'"{query}" filetype:xls OR filetype:xlsx',
        f'"{query}" filetype:doc OR filetype:docx',
        f'site:vk.com "{query}"',
        f'site:ok.ru "{query}"',
        f'site:t.me "{query}"',
        f'site:github.com "{query}"',
        f'site:linkedin.com "{query}"',
        f'site:instagram.com "{query}"',
        f'"{query}" inurl:admin',
        f'"{query}" intext:password',
        f'"{query}" site:pastebin.com',
    ]
    result = f"🕸 <b>Google Dorks: {html.escape(query)}</b>\n\n"
    for d in dorks:
        url = f"https://www.google.com/search?q={quote(d)}"
        result += f"• <a href='{url}'>{html.escape(d)}</a>\n"
    return result

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: EXIF из фото
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def extract_exif(file_path: str) -> str:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

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
            elif tag in ('Make', 'Model', 'DateTime', 'Software',
                         'Artist', 'Copyright', 'ImageDescription',
                         'GPSInfo', 'XPAuthor', 'XPComment'):
                lines.append(f"<b>{tag}:</b> {html.escape(str(value)[:100])}")

        # GPS координаты
        if gps_info:
            def to_decimal(dms, ref):
                d, m, s = dms
                decimal = float(d) + float(m)/60 + float(s)/3600
                if ref in ['S', 'W']:
                    decimal = -decimal
                return round(decimal, 6)

            try:
                lat = to_decimal(gps_info['GPSLatitude'], gps_info['GPSLatitudeRef'])
                lon = to_decimal(gps_info['GPSLongitude'], gps_info['GPSLongitudeRef'])
                lines.append(f"\n📍 <b>GPS координаты:</b>")
                lines.append(f"Lat: {lat}, Lon: {lon}")
                lines.append(f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>Открыть на карте</a>")
            except Exception:
                lines.append("📍 GPS-данные есть, но не удалось расшифровать")

        return '\n'.join(lines)
    except ImportError:
        return "⚠️ Установи Pillow: pip install Pillow"
    except Exception as e:
        log.error(f"extract_exif: {e}")
        return f"⚠️ EXIF ошибка: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: GROQ AI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = """Ты — RamsEye AI, профессиональный OSINT-ассистент компании по кибербезопасности.
Твои задачи:
- Анализ данных из открытых источников
- Поиск связей между никами, email, IP, доменами
- Объяснение методов OSINT-расследований
- Помощь в интерпретации результатов инструментов (Maigret, Shodan, DNS)
- Составление стратегии расследования

Ты работаешь строго в рамках закона и этики OSINT.
Отвечай структурированно, детально, на русском языке.
Если запрос неоднозначен — уточни цель исследования."""

def ask_groq(question: str, context: str = "") -> str:
    if not GROQ_KEY:
        return "⚠️ GROQ_API_KEY не задан"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "user", "content": f"Контекст:\n{context}"})
        messages.append({"role": "assistant", "content": "Понял, учту контекст."})
    messages.append({"role": "user", "content": question})

    try:
        r = session.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={"model": GROQ_MODEL, "messages": messages,
                  "temperature": 0.6, "max_tokens": 1500},
            timeout=30
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.error(f"Groq {r.status_code}: {r.text[:200]}")
        return f"❌ Groq API ошибка: {r.status_code}"
    except Exception as e:
        log.error(f"ask_groq: {e}")
        return f"❌ Groq ошибка: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: КЛАСТЕРИЗАЦИЯ СВЯЗЕЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cluster_data(data_text: str) -> str:
    prompt = (
        "Проанализируй данные OSINT-расследования. Найди и структурируй:\n"
        "1. Все идентификаторы (ники, email, телефоны, IP, домены)\n"
        "2. Связи между ними\n"
        "3. Временну́ю шкалу если есть даты\n"
        "4. Вероятные выводы\n"
        "5. Рекомендации что проверить дальше\n\n"
        f"Данные:\n{data_text}"
    )
    return ask_groq(prompt)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДУЛЬ: ДОСЬЕ (параллельный запуск)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_dossier(target: str, chat_id: int):
    """
    Автоопределение типа цели + параллельный запуск всех
    подходящих инструментов + итоговый AI-анализ.
    """
    def _worker():
        with task_semaphore:
            t0 = time.time()
            bot.send_message(chat_id, f"📂 Сбор досье для <b>{html.escape(target)}</b>...", parse_mode='HTML')

            tasks = {}

            # Определяем тип и набор задач
            is_email  = validate_email(target)
            is_ip     = validate_ip(target)
            is_domain = validate_domain(target) and not is_ip
            is_nick   = validate_nick(target)

            with ThreadPoolExecutor(max_workers=5) as ex:
                if is_nick:
                    bot.send_message(chat_id, "👤 Maigret...")
                    tasks['MAIGRET'] = ex.submit(
                        subprocess.run,
                        ["maigret", "--txt", target, "--timeout", "30"],
                        capture_output=True, text=True, timeout=120
                    )

                if is_email:
                    bot.send_message(chat_id, "📧 Holehe...")
                    tasks['HOLEHE'] = ex.submit(
                        subprocess.run,
                        ["holehe", target, "--only-used"],
                        capture_output=True, text=True, timeout=120
                    )

                if is_ip:
                    bot.send_message(chat_id, "🌐 IP-разведка + Shodan...")
                    tasks['IP']     = ex.submit(get_ip_info, target)
                    tasks['SHODAN'] = ex.submit(shodan_lookup, target)

                if is_domain:
                    bot.send_message(chat_id, "🔍 DNS + WHOIS...")
                    tasks['DNS']   = ex.submit(dns_recon, target)
                    tasks['WHOIS'] = ex.submit(whois_lookup, target)

                # Дорки для всего
                tasks['DORKS'] = ex.submit(generate_dorks, target)

                # Собираем результаты
                results = {}
                for name, future in tasks.items():
                    try:
                        res = future.result(timeout=150)
                        if hasattr(res, 'stdout'):  # subprocess.CompletedProcess
                            results[name] = res.stdout or res.stderr or "Нет вывода"
                        else:
                            results[name] = str(res)
                    except Exception as e:
                        results[name] = f"❌ Ошибка: {e}"

            if not results:
                bot.send_message(chat_id, "❌ Не удалось определить тип данных.")
                return

            # AI-анализ собранных данных
            bot.send_message(chat_id, "🧠 AI анализирует связи...")
            summary_input = "\n\n".join(
                f"[{name}]\n{text[:800]}" for name, text in results.items()
                if name != 'DORKS'
            )
            ai_summary = ask_groq(
                f"Цель: {target}\nПроанализируй результаты OSINT-сбора и дай сводный отчёт:",
                context=summary_input
            )

            # Формируем итоговый файл
            elapsed = time.time() - t0
            header = (
                f"╔══════════════════════════════════════╗\n"
                f"║   RAMSEYE OSINT v6.0 — ДОСЬЕ         ║\n"
                f"╚══════════════════════════════════════╝\n"
                f"Цель:    {target}\n"
                f"Дата:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Время:   {elapsed:.1f} сек\n"
                f"{'='*40}\n\n"
            )

            body = ""
            for name, text in results.items():
                body += f"\n{'='*40}\n[{name}]\n{'='*40}\n{text}\n"

            footer = f"\n{'='*40}\n[AI АНАЛИЗ]\n{'='*40}\n{ai_summary}\n"

            full_report = header + body + footer

            fname = f"/tmp/dossier_{re.sub(r'[^a-zA-Z0-9]', '_', target)}_{int(time.time())}.txt"
            try:
                with open(fname, 'w', encoding='utf-8') as f:
                    f.write(full_report)
                with open(fname, 'rb') as f:
                    bot.send_document(
                        chat_id, f,
                        caption=f"📂 Досье: {target} | {elapsed:.1f} сек"
                    )
                # Отдельно отправляем AI-вывод как сообщение
                bot.send_message(
                    chat_id,
                    f"🧠 <b>AI-вывод по цели {html.escape(target)}:</b>\n\n{ai_summary[:3500]}",
                    parse_mode='HTML'
                )
            except Exception as e:
                log.error(f"dossier send: {e}")
                bot.send_message(chat_id, f"❌ Ошибка отправки досье: {e}")
            finally:
                if os.path.exists(fname):
                    os.remove(fname)

    threading.Thread(target=_worker, daemon=True).start()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МЕНЮ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add("🔍 OSINT SEARCH", "🧠 RAMSEYE AI")
    m.add("📂 DOSSIER", "ℹ️ ПОМОЩЬ")
    return m

def tools_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("👤 MAIGRET",      callback_data="maigret"),
        types.InlineKeyboardButton("📧 HOLEHE",       callback_data="holehe"),
        types.InlineKeyboardButton("🌐 IP-ПРОБИВ",    callback_data="ip"),
        types.InlineKeyboardButton("🔭 SHODAN",       callback_data="shodan"),
        types.InlineKeyboardButton("🔍 DNS",          callback_data="dns"),
        types.InlineKeyboardButton("🌍 WHOIS",        callback_data="whois"),
        types.InlineKeyboardButton("🕸 DORKS",        callback_data="dorks"),
        types.InlineKeyboardButton("🧠 CLUSTER",      callback_data="cluster"),
        types.InlineKeyboardButton("📷 EXIF-ФОТО",    callback_data="exif"),
        types.InlineKeyboardButton("❌ ЗАКРЫТЬ",      callback_data="close"),
    )
    return m

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ОБРАБОТЧИКИ TELEGRAM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    bot.send_message(
        message.chat.id,
        "🦾 <b>RAMSEYE OSINT v6.0 — PROFESSIONAL</b>\n\n"
        "🔍 <b>OSINT SEARCH</b> — Maigret, Holehe, IP, Shodan, DNS, WHOIS, Dorks, EXIF\n"
        "🧠 <b>RAMSEYE AI</b> — Llama 4 Scout (анализ, стратегия, связи)\n"
        "📂 <b>DOSSIER</b> — параллельный сбор + AI-анализ по любой цели\n"
        "ℹ️ <b>ПОМОЩЬ</b> — справка\n\n"
        "👇 Выбери действие",
        parse_mode='HTML', reply_markup=main_menu()
    )
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
        "maigret": "👤 Введи ник (только латиница/цифры/_):",
        "holehe":  "📧 Введи email:",
        "ip":      "🌐 Введи IP-адрес:",
        "shodan":  "🔭 Введи IP для Shodan:",
        "dns":     "🔍 Введи домен (example.com):",
        "whois":   "🌍 Введи домен для WHOIS:",
        "dorks":   "🕸 Введи запрос для Google Dorks:",
        "cluster": "🧠 Вставь данные для анализа связей\n(ники, email, IP через перенос строки):",
        "exif":    "📷 Отправь фотографию (как файл, не сжатое):",
    }
    prompt = prompts.get(call.data, f"Введи данные для {call.data}:")
    bot.answer_callback_query(call.id, f"Выбран: {call.data}")
    clear_step(call.from_user.id)
    set_step(call.from_user.id, call.data)
    bot.send_message(call.message.chat.id, prompt)

@bot.message_handler(content_types=['photo', 'document'])
def on_media(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    uid = message.from_user.id
    step = get_step(uid)

    if step == "exif":
        clear_step(uid)
        bot.send_message(message.chat.id, "📷 Извлекаю EXIF...")
        try:
            # Принимаем и document (файл без сжатия) и photo
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
            log.error(f"EXIF handler: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            if 'fpath' in locals() and os.path.exists(fpath):
                os.remove(fpath)
    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())

@bot.message_handler(func=lambda m: True, content_types=['text'])
def on_text(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return

    text = message.text or ""
    uid  = message.from_user.id
    uname = message.from_user.username or str(uid)
    step = get_step(uid)

    # ── Обработка ожидаемого ввода ───────────────────────────────────
    if step:
        if text == "/cancel":
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
            return

        clear_step(uid)

        if step == "maigret":
            if validate_nick(text):
                run_tool_bg(message.chat.id, ["maigret", "--txt", text, "--timeout", "30"], "MAIGRET")
            else:
                bot.send_message(message.chat.id, "❌ Некорректный ник (a-z, 0-9, _, 3-32 символа)")

        elif step == "holehe":
            if validate_email(text):
                run_tool_bg(message.chat.id, ["holehe", text, "--only-used"], "HOLEHE")
            else:
                bot.send_message(message.chat.id, "❌ Некорректный email")

        elif step == "ip":
            if validate_ip(text):
                t0 = time.time()
                result = get_ip_info(text)
                bot.send_message(
                    message.chat.id,
                    f"{result}\n\n⏱ {time.time()-t0:.2f} сек",
                    parse_mode='HTML'
                )
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP (0-255 в каждом октете)")

        elif step == "shodan":
            if validate_ip(text):
                bot.send_message(message.chat.id, "🔭 Запрос к Shodan...")
                result = shodan_lookup(text)
                bot.send_message(message.chat.id, result, parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP")

        elif step == "dns":
            if validate_domain(text):
                bot.send_message(message.chat.id, "🔍 DNS-разведка...")
                def _dns():
                    result = dns_recon(text)
                    send_result(message.chat.id, result, "DNS")
                threading.Thread(target=_dns, daemon=True).start()
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
            bot.send_message(message.chat.id, result,
                             parse_mode='HTML', disable_web_page_preview=True)

        elif step == "cluster":
            bot.send_message(message.chat.id, "🧠 Анализирую связи...")
            def _cluster():
                result = cluster_data(text)
                send_result(message.chat.id, result, "CLUSTER")
            threading.Thread(target=_cluster, daemon=True).start()

        elif step == "dossier":
            run_dossier(text, message.chat.id)

        elif step == "groq":
            bot.send_chat_action(message.chat.id, 'typing')
            bot.send_message(message.chat.id, "🤔 Думаю...")
            def _groq():
                answer = ask_groq(text)
                send_result(message.chat.id, answer, "RAMSEYE AI")
            threading.Thread(target=_groq, daemon=True).start()

        elif step == "exif":
            bot.send_message(message.chat.id, "📷 Отправь фото как файл (не сжатое)")
            set_step(uid, "exif")  # восстанавливаем ожидание

        return

    # ── Кнопки главного меню ─────────────────────────────────────────
    if text == "🔍 OSINT SEARCH":
        bot.send_message(message.chat.id, "🔍 <b>Выбери инструмент:</b>",
                         parse_mode='HTML', reply_markup=tools_menu())

    elif text == "🧠 RAMSEYE AI":
        set_step(uid, "groq")
        bot.send_message(message.chat.id, "🧠 Задай вопрос (или /cancel):")

    elif text == "📂 DOSSIER":
        set_step(uid, "dossier")
        bot.send_message(message.chat.id,
            "📂 Введи цель:\n"
            "• Ник → Maigret\n"
            "• Email → Holehe\n"
            "• IP → IP-пробив + Shodan\n"
            "• Домен → DNS + WHOIS\n"
            "• Любой → Google Dorks + AI-анализ")

    elif text == "ℹ️ ПОМОЩЬ":
        bot.send_message(message.chat.id,
            "<b>📖 RamsEye OSINT v6.0 — PROFESSIONAL</b>\n\n"
            "<b>Инструменты:</b>\n"
            "👤 Maigret — поиск ника по 500+ соцсетям\n"
            "📧 Holehe — проверка email по сервисам\n"
            "🌐 IP — геолокация, ISP, VPN/Proxy/Tor\n"
            "🔭 Shodan — открытые порты, CVE, баннеры\n"
            "🔍 DNS — A/MX/TXT/NS + субдомены crt.sh\n"
            "🌍 WHOIS — регистратор, даты, контакты\n"
            "🕸 Dorks — 12 Google Dorks по запросу\n"
            "🧠 Cluster — AI-анализ связей между данными\n"
            "📷 EXIF — GPS и метаданные из фото\n"
            "📂 Dossier — всё выше параллельно + AI-отчёт\n\n"
            "Доступ только для авторизованного пользователя.\n"
            "/cancel — отмена ввода",
            parse_mode='HTML')

    elif text == "/cancel":
        if get_step(uid):
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
        else:
            bot.send_message(message.chat.id, "Нет активного ожидания.")

    else:
        bot.send_message(message.chat.id, "Используй кнопки меню 👇",
                         reply_markup=main_menu())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАПУСК
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == '__main__':
    print("🦾 RamsEye OSINT v6.0 — PROFESSIONAL EDITION")
    print("=" * 50)
    print(f"ADMIN_ID : {'✅ задан' if ADMIN_ID else '❌ НЕ ЗАДАН — бот не запустится'}")
    print(f"GROQ     : {'✅' if GROQ_KEY else '❌ не задан'}")
    print(f"SHODAN   : {'✅' if SHODAN_KEY else '⚠️  не задан (опционально)'}")
    print(f"RENDER   : {RENDER_URL or '⚠️  не задан (автопинг отключён)'}")
    print("=" * 50)

    if not ADMIN_ID:
        print("❌ FATAL: задайте ADMIN_ID в переменных окружения")
        exit(1)
    if not TOKEN:
        print("❌ FATAL: задайте TELEGRAM_TOKEN в переменных окружения")
        exit(1)

    while True:
        try:
            log.info("Polling started")
            bot.polling(none_stop=True, interval=0, timeout=60)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)

