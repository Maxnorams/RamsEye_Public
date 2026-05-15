#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════╗
# ║         RamsEye OSINT v7.1 — FULL PRODUCTION                    ║
# ║  Maigret+AI | GitHub | TG | VK | все модули полные              ║
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
import dns.resolver
import asyncio
import whois
import json
import socket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from telebot import types
from flask import Flask
from holehe import core
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from bs4 import BeautifulSoup

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КОНФИГ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKEN      = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID   = int(os.environ.get('ADMIN_ID', '0'))
GROQ_KEY   = os.environ.get('GROQ_API_KEY')
SHODAN_KEY = os.environ.get('SHODAN_API_KEY')
RENDER_URL = os.environ.get('RENDER_URL', '')

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

bot     = telebot.TeleBot(TOKEN)
session = requests.Session()
session.headers.update({'User-Agent': 'RamsEye-OSINT/7.1'})

task_semaphore = threading.Semaphore(3)
user_step: dict = {}
_step_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('ramseye.log', encoding='utf-8')
    ]
)
log = logging.getLogger('RamsEye')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FLASK KEEP-ALIVE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)

@app.route('/')
def health():
    return f"RamsEye OSINT v7.1 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200

threading.Thread(
    target=lambda: app.run(host='0.0.0.0', port=8080),
    daemon=True
).start()

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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  АВТОРИЗАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_allowed(uid: int) -> bool:
    return ADMIN_ID != 0 and uid == ADMIN_ID

def auth_check(obj) -> bool:
    uid = obj.from_user.id if hasattr(obj, 'from_user') else obj
    return is_allowed(uid)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СОСТОЯНИЯ (thread-safe)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def set_step(uid: int, step: str):
    with _step_lock:
        user_step[uid] = step

def get_step(uid: int):
    with _step_lock:
        return user_step.get(uid)

def clear_step(uid: int):
    with _step_lock:
        user_step.pop(uid, None)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ВАЛИДАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ОТПРАВКА РЕЗУЛЬТАТОВ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_result(chat_id: int, text: str, title: str):
    if len(text) > 3800:
        safe_title = re.sub(r'[^a-zA-Z0-9_]', '_', title)
        fname = f"/tmp/{safe_title}_{int(time.time())}.txt"
        try:
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(text)
            with open(fname, 'rb') as f:
                bot.send_document(chat_id, f, caption=f"📊 {title}")
        except Exception as e:
            log.error(f"send_result file: {e}")
            bot.send_message(chat_id, f"❌ Ошибка отправки: {e}")
        finally:
            if os.path.exists(fname):
                os.remove(fname)
    else:
        try:
            bot.send_message(
                chat_id,
                f"<b>📊 {html.escape(title)}</b>\n<pre>{html.escape(text)}</pre>",
                parse_mode='HTML'
            )
        except Exception as e:
            log.error(f"send_result msg: {e}")
            bot.send_message(chat_id, text[:4000])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GROQ AI — главный движок анализа
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = """Ты — RamsEye AI, персональный OSINT-ассистент аналитика Maxnorams.

РОЛЬ И КОНТЕКСТ:
Ты работаешь в составе профессиональной команды кибербезопасности.
Твой пользователь — опытный OSINT-аналитик, специализирующийся на расследованиях
по открытым источникам: корпоративная разведка, верификация личностей,
анализ цифрового следа, расследование мошенничества.

ТВОИ КОМПЕТЕНЦИИ:
1. Анализ и интерпретация результатов инструментов:
   Maigret, Holehe, Shodan, Maltego, theHarvester, SpiderFoot,
   Recon-ng, OSINT Framework, Censys, Fofa, ZoomEye
2. Разведка по открытым источникам:
   - Анализ никнеймов, email, телефонов, IP, доменов
   - Поиск цифрового следа и кросс-платформенных связей
   - Анализ метаданных документов и изображений (EXIF)
   - Исследование инфраструктуры (DNS, WHOIS, ASN, BGP)
   - Анализ сертификатов SSL и субдоменов
3. Разведка по социальным сетям (SOCMINT):
   - ВКонтакте, Telegram, Instagram, Twitter/X, LinkedIn, TikTok
   - Анализ паттернов активности и временных зон
   - Поиск связанных аккаунтов по аватарам, стилю, лексике
4. Технический анализ:
   - Интерпретация Shodan-баннеров и CVE
   - Анализ открытых портов и сервисов
   - Оценка инфраструктуры цели
5. Аналитика и отчётность:
   - Построение графов связей между идентификаторами
   - Временны́е шкалы активности
   - Составление структурированных досье
   - Оценка достоверности и источников

СТИЛЬ РАБОТЫ:
- Отвечай структурированно: заголовки, списки, чёткие выводы
- Используй профессиональную терминологию OSINT/CYBERSEC
- Давай конкретные следующие шаги расследования
- Указывай уровень достоверности (высокий/средний/низкий)
- Предлагай альтернативные векторы поиска если основной не дал результатов
- При анализе данных всегда выделяй: факты, предположения, рекомендации

ЭТИКА И ОГРАНИЧЕНИЯ:
- Работаешь исключительно с публично доступными данными
- Не помогаешь с действиями, нарушающими законодательство
- Не участвуешь в преследовании, сталкинге или доксинге частных лиц
- Все расследования предполагают законный профессиональный контекст

ФОРМАТ ОТВЕТОВ:
Для стратегии расследования:
  🎯 Цель | 🔍 Векторы поиска | 📊 Приоритеты | ⚠️ Риски

Для анализа данных:
  ✅ Подтверждённые факты | 🔶 Предположения | ❓ Требует проверки

Для технического анализа:
  🖥 Инфраструктура | 🔓 Уязвимости | 📡 Сервисы | 🗺 Связи

Язык ответов: русский, технические термины на английском где принято."""

# Специальный промпт для Maigret+AI анализа
MAIGRET_AI_PROMPT = """Ты — OSINT-аналитик высшего уровня. Тебе предоставлены сырые результаты Maigret по никнейму.

ТВОЯ ЗАДАЧА — провести глубокий анализ по следующему плану:

1. ИНВЕНТАРИЗАЦИЯ АККАУНТОВ
   - Перечисли ВСЕ найденные платформы со ссылками
   - Сгруппируй по категориям: соцсети / форумы / профессиональные / gaming / dating / прочее
   - Отметь платформы с высокой вероятностью активности

2. АНАЛИЗ ЦИФРОВОГО СЛЕДА
   - Какие платформы говорят о профессии / интересах / локации?
   - Есть ли паттерны в именовании аккаунтов (цифры, символы, вариации ника)?
   - Оцени "возраст" цифрового следа по набору платформ

3. ПОИСК СВЯЗЕЙ И КРОСС-ИДЕНТИФИКАЦИЯ
   - Какие платформы могут содержать реальное имя / фото / email?
   - Где вероятнее всего найти дополнительные идентификаторы?
   - Есть ли вариации ника которые стоит проверить дополнительно?

4. ОЦЕНКА ДОСТОВЕРНОСТИ
   - Высокая достоверность (уникальный ник на редкой платформе)
   - Средняя (распространённый ник, требует верификации)
   - Низкая (возможное совпадение имён)

5. СЛЕДУЮЩИЕ ШАГИ РАССЛЕДОВАНИЯ
   - Конкретные действия для углубления поиска
   - Какие инструменты применить дальше (Holehe, Shodan, reverse image search)
   - На каких платформах искать email / телефон

6. СВОДНЫЙ ПОРТРЕТ ЦЕЛИ
   - Вероятная сфера деятельности
   - Географические маркеры если есть
   - Уровень цифровой активности (высокий/средний/низкий)

Отвечай на русском. Будь конкретным — никакой воды. Каждый пункт должен содержать реальные выводы на основе данных."""

def ask_groq(question: str, context: str = "", custom_system: str = "") -> str:
    if not GROQ_KEY:
        return "⚠️ GROQ_API_KEY не задан"

    system = custom_system if custom_system else SYSTEM_PROMPT
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "user", "content": f"Контекст:\n{context}"})
        messages.append({"role": "assistant", "content": "Понял, учту контекст."})
    messages.append({"role": "user", "content": question})

    try:
        r = session.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 2000
            },
            timeout=40
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.error(f"Groq {r.status_code}: {r.text[:200]}")
        return f"❌ Groq API ошибка: {r.status_code}"
    except Exception as e:
        log.error(f"ask_groq: {e}")
        return f"❌ Groq ошибка: {e}"

def groq_thread(chat_id: int, question: str):
    send_result(chat_id, ask_groq(question), "RAMSEYE AI")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIGRET — subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def maigret_lookup(username: str) -> str:
    try:
        result = subprocess.run(
            ["maigret", username, "--no-color", "--timeout", "30"],
            capture_output=True,
            text=True,
            timeout=160
        )
        output = result.stdout or result.stderr or "Нет вывода"

        lines = [f"👤 Maigret: {username}\n"]
        found_count = 0
        for line in output.splitlines():
            stripped = line.strip()
            if "[+]" in stripped or "Found" in stripped.lower():
                lines.append(f"✅ {stripped}")
                found_count += 1
            elif "[-]" in stripped and found_count == 0:
                pass
        if found_count == 0:
            lines.append("❌ Аккаунты не найдены")
        else:
            lines.append(f"\n📊 Найдено аккаунтов: {found_count}")

        return '\n'.join(lines[:50])
    except subprocess.TimeoutExpired:
        return "⏰ Maigret: таймаут 160 сек"
    except FileNotFoundError:
        return "❌ Maigret не установлен: pip install maigret"
    except Exception as e:
        log.error(f"maigret_lookup: {e}")
        return f"❌ Ошибка Maigret: {e}"

def maigret_thread(chat_id: int, username: str):
    send_result(chat_id, maigret_lookup(username), "MAIGRET")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIGRET + AI — глубокий анализ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def maigret_ai_analysis(username: str, chat_id: int):
    bot.send_message(chat_id, "🔍 Шаг 1/3: Maigret собирает аккаунты...")
    raw_output = maigret_lookup(username)

    platforms = []
    urls = []
    for line in raw_output.splitlines():
        if "✅" in line:
            platforms.append(line.strip())
            url_match = re.search(r'https?://\S+', line)
            if url_match:
                urls.append(url_match.group())

    bot.send_message(
        chat_id,
        f"✅ Шаг 1/3: Maigret завершён. Найдено платформ: {len(platforms)}"
    )

    bot.send_message(chat_id, "🧠 Шаг 2/3: AI анализирует цифровой след...")

    context = f"""НИКНЕЙМ ДЛЯ АНАЛИЗА: {username}

ПОЛНЫЙ ВЫВОД MAIGRET:
{raw_output}

НАЙДЕННЫЕ ПЛАТФОРМЫ ({len(platforms)}):
{chr(10).join(platforms) if platforms else 'Не найдено'}

НАЙДЕННЫЕ URL ({len(urls)}):
{chr(10).join(urls[:30]) if urls else 'Не найдено'}"""

    ai_result = ask_groq(
        f"Проведи полный OSINT-анализ никнейма '{username}' по предоставленным данным Maigret.",
        context=context,
        custom_system=MAIGRET_AI_PROMPT
    )

    bot.send_message(chat_id, "📋 Шаг 3/3: Формирую итоговый отчёт...")

    report = (
        f"╔══════════════════════════════════════╗\n"
        f"║   RAMSEYE OSINT v7.1 — MAIGRET+AI    ║\n"
        f"╚══════════════════════════════════════╝\n"
        f"Никнейм: {username}\n"
        f"Дата:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Найдено: {len(platforms)} платформ\n"
        f"{'='*40}\n\n"
        f"[СЫРЫЕ ДАННЫЕ MAIGRET]\n"
        f"{'='*40}\n"
        f"{raw_output}\n\n"
        f"{'='*40}\n"
        f"[AI АНАЛИЗ ЦИФРОВОГО СЛЕДА]\n"
        f"{'='*40}\n"
        f"{ai_result}"
    )

    return report

def maigret_ai_thread(chat_id: int, username: str):
    try:
        report = maigret_ai_analysis(username, chat_id)
        send_result(chat_id, report, f"MAIGRET+AI_{username}")
    except Exception as e:
        log.error(f"maigret_ai_thread: {e}")
        bot.send_message(chat_id, f"❌ Ошибка Maigret+AI: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOLEHE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def holehe_lookup_async(email: str) -> dict:
    try:
        return await core.check_email(email)
    except Exception as e:
        log.error(f"holehe async: {e}")
        return {}

def holehe_lookup(email: str) -> str:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(holehe_lookup_async(email))
        loop.close()

        found = []
        for service, data in result.items():
            if data.get('rateLimit') or not data.get('exists'):
                continue
            found.append(f"✅ {service}")

        if not found:
            return "❌ Аккаунты не найдены"
        return f"📧 Найденные сервисы ({len(found)}):\n" + '\n'.join(found[:30])
    except Exception as e:
        log.error(f"holehe_lookup: {e}")
        return f"❌ Ошибка Holehe: {e}"

def holehe_thread(chat_id: int, email: str):
    send_result(chat_id, holehe_lookup(email), "HOLEHE")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IP-РАЗВЕДКА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_ip_info(ip: str) -> str:
    try:
        r = session.get(f"https://ipinfo.io/{ip}/json", timeout=10)
        d = r.json()
        loc = d.get('loc', '0,0').split(',')
        lat, lon = loc[0], loc[1]
        lines = [
            f"🌐 IP: {ip}",
            f"🏳 Страна: {d.get('country', 'Н/Д')}",
            f"🏙 Город: {d.get('city', 'Н/Д')}",
            f"🏢 Регион: {d.get('region', 'Н/Д')}",
            f"📡 ISP: {d.get('org', 'Н/Д')}",
            f"🌍 Хостнейм: {d.get('hostname', 'Н/Д')}",
            f"📮 Индекс: {d.get('postal', 'Н/Д')}",
            f"🗺 Карта: https://maps.google.com/?q={lat},{lon}",
        ]
        try:
            r2 = session.get(f"https://ipinfo.io/{ip}/privacy", timeout=5)
            priv = r2.json()
            lines.append(
                f"🕵️ VPN: {priv.get('vpn','?')} | "
                f"Proxy: {priv.get('proxy','?')} | "
                f"Tor: {priv.get('tor','?')}"
            )
        except Exception:
            pass
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"get_ip_info: {e}")
        return f"⚠️ Ошибка IP: {e}"

def ip_thread(chat_id: int, ip: str):
    send_result(chat_id, get_ip_info(ip), "IP-ПРОБИВ")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS-РАЗВЕДКА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def dns_recon(domain: str) -> str:
    lines = [f"🔍 DNS-разведка: {domain}\n"]
    for rtype in ['A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA']:
        try:
            answers = dns.resolver.resolve(domain, rtype, lifetime=5)
            vals = [str(r) for r in answers]
            lines.append(f"{rtype}: {', '.join(vals)}")
        except Exception:
            lines.append(f"{rtype}: —")

    lines.append("\n📜 Субдомены (crt.sh):")
    try:
        r = session.get(
            f"https://crt.sh/?q=%.{domain}&output=json", timeout=15
        )
        subs = set()
        for entry in r.json():
            for sub in entry.get('name_value', '').split('\n'):
                sub = sub.strip().lstrip('*.')
                if sub.endswith(domain) and sub != domain:
                    subs.add(sub)
        if subs:
            lines.extend([f"  • {s}" for s in sorted(subs)[:20]])
        else:
            lines.append("  Субдомены не найдены")
    except Exception as e:
        lines.append(f"  ⚠️ crt.sh ошибка: {e}")

    return '\n'.join(lines)

def dns_thread(chat_id: int, domain: str):
    send_result(chat_id, dns_recon(domain), "DNS")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SHODAN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def shodan_lookup(ip: str) -> str:
    if not SHODAN_KEY:
        return "⚠️ SHODAN_API_KEY не задан в env"
    try:
        r = session.get(
            f"https://api.shodan.io/shodan/host/{ip}",
            params={"key": SHODAN_KEY},
            timeout=15
        )
        if r.status_code != 200:
            return f"❌ Shodan: {r.status_code}"
        d = r.json()
        lines = [
            f"🔭 Shodan: {ip}",
            f"🏢 Организация: {d.get('org', 'Н/Д')}",
            f"🌍 Страна: {d.get('country_name', 'Н/Д')}",
            f"🖥 ОС: {d.get('os', 'Не определена')}",
            f"📅 Обновлено: {d.get('last_update', 'Н/Д')}",
            "\n📡 Открытые порты:",
        ]
        for item in d.get('data', [])[:10]:
            lines.append(
                f"  • {item.get('port')}/{item.get('transport')} "
                f"{item.get('product', '')} {item.get('version', '')}"
            )
        vulns = d.get('vulns', {})
        if vulns:
            lines.append(f"\n⚠️ Уязвимости CVE ({len(vulns)}):")
            for cve in list(vulns.keys())[:5]:
                lines.append(f"  🔴 {cve}")
            if len(vulns) > 5:
                lines.append(f"  ... и ещё {len(vulns) - 5}")
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"shodan_lookup: {e}")
        return f"⚠️ Shodan ошибка: {e}"

def shodan_thread(chat_id: int, ip: str):
    send_result(chat_id, shodan_lookup(ip), "SHODAN")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WHOIS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def whois_lookup(domain: str) -> str:
    try:
        w = whois.whois(domain)
        lines = [f"🌐 WHOIS: {domain}\n"]
        fields = {
            'Регистратор': w.registrar,
            'Создан':      w.creation_date,
            'Истекает':    w.expiration_date,
            'Обновлён':    w.updated_date,
            'Организация': w.org,
            'Страна':      w.country,
            'Email':       w.emails,
            'DNS-серверы': w.name_servers,
            'Статус':      w.status,
        }
        for label, val in fields.items():
            if val:
                if isinstance(val, list):
                    val = ', '.join(str(v) for v in val[:3])
                lines.append(f"{label}: {str(val)[:200]}")
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"whois_lookup: {e}")
        return f"❌ WHOIS ошибка: {e}"

def whois_thread(chat_id: int, domain: str):
    send_result(chat_id, whois_lookup(domain), "WHOIS")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GOOGLE DORKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
    result = f"🕸 Google Dorks: {query}\n\n"
    for d in dorks:
        url = f"https://www.google.com/search?q={quote(d)}"
        result += f"• {d}\n  {url}\n\n"
    return result

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GITHUB SECRETS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def github_secrets(query: str) -> str:
    try:
        search_query = f"{query} password OR api_key OR token OR secret"
        url = f"https://api.github.com/search/code?q={quote(search_query)}"
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'RamsEye-OSINT'
        }
        resp = session.get(url, headers=headers, timeout=15)

        if resp.status_code == 403:
            return "⚠️ GitHub API rate limit. Подожди минуту или добавь GITHUB_TOKEN в env."
        if resp.status_code == 422:
            return "❌ Слишком короткий запрос для GitHub поиска."
        if resp.status_code != 200:
            return f"❌ GitHub API ошибка: {resp.status_code}"

        items = resp.json().get('items', [])[:15]
        if not items:
            return f"❌ Секреты по запросу '{query}' не найдены"

        lines = [f"🔑 GitHub секреты: '{query}'\n"]
        for item in items:
            repo = item['repository']['full_name']
            path = item['path']
            html_url = item['html_url']
            lines.append(f"• {repo}\n  📄 {path}\n  🔗 {html_url}\n")

        return '\n'.join(lines)
    except Exception as e:
        log.error(f"github_secrets: {e}")
        return f"⚠️ GitHub ошибка: {e}"

def github_thread(chat_id: int, query: str):
    send_result(chat_id, github_secrets(query), "GITHUB SECRETS")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TELEGRAM ПАРСИНГ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def tg_parse(username: str) -> str:
    username = username.lstrip('@')
    try:
        url = f"https://t.me/{username}?embed=1"
        resp = session.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')

        lines = [f"📡 Telegram: @{username}\n"]

        title = soup.find('div', class_='tgme_page_title')
        lines.append(f"👤 Имя: {title.get_text(strip=True) if title else 'Не найдено'}")

        extra = soup.find('div', class_='tgme_page_extra')
        lines.append(f"🔗 Username: {extra.get_text(strip=True) if extra else 'Не найдено'}")

        desc = soup.find('div', class_='tgme_page_description')
        lines.append(f"📝 Описание: {desc.get_text(strip=True) if desc else 'Нет описания'}")

        if soup.find('div', class_='tgme_page_context_link'):
            lines.append("📌 Тип: Канал/Группа")
        else:
            lines.append("📌 Тип: Пользователь/Бот")

        counter = soup.find('div', class_='tgme_page_extra')
        if counter:
            lines.append(f"👥 Подписчики: {counter.get_text(strip=True)}")

        lines.append(f"\n🔗 Ссылка: https://t.me/{username}")
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"tg_parse: {e}")
        return f"⚠️ Ошибка парсинга Telegram: {e}"

def tg_thread(chat_id: int, username: str):
    send_result(chat_id, tg_parse(username), "TELEGRAM")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━#  VK ПАРСИНГ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def vk_parse(user_id: str) -> str:
    user_id = user_id.lstrip('@')
    try:
        url = f"https://m.vk.com/{user_id}"
        resp = session.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')

        lines = [f"📡 VK: {user_id}\n"]

        name_tag = soup.find('h1', class_='page_name') or \
                   soup.find('div', class_='pv_peer_name') or \
                   soup.find('h2', class_='op_header')
        lines.append(f"👤 Имя: {name_tag.get_text(strip=True) if name_tag else 'Не найдено'}")

        status = soup.find('div', class_='current_info') or \
                 soup.find('div', class_='pv_status')
        lines.append(f"💬 Статус: {status.get_text(strip=True) if status else 'Нет статуса'}")

        city = soup.find('div', class_='profile_city')
        if city:
            lines.append(f"🏙 Город: {city.get_text(strip=True)}")

        bdate = soup.find('div', class_='profile_info_row')
        if bdate:
            lines.append(f"📅 Инфо: {bdate.get_text(strip=True)[:100]}")

        lines.append(f"\n🔗 Ссылка: https://vk.com/{user_id}")
        return '\n'.join(lines)
    except Exception as e:
        log.error(f"vk_parse: {e}")
        return f"⚠️ Ошибка парсинга VK: {e}"

def vk_thread(chat_id: int, user_id: str):
    send_result(chat_id, vk_parse(user_id), "VK")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EXIF
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def extract_exif(file_path: str) -> str:
    try:
        img = Image.open(file_path)
        exif_data = img._getexif()
        if not exif_data:
            return "📷 EXIF-данные отсутствуют"

        lines = ["📷 EXIF-данные:\n"]
        gps_info = {}

        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'GPSInfo':
                for gps_tag_id, gps_val in value.items():
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_info[gps_tag] = gps_val
            elif tag in ('Make', 'Model', 'DateTime', 'Software',
                         'Artist', 'Copyright', 'ImageDescription'):
                lines.append(f"{tag}: {str(value)[:100]}")

        if gps_info:
            lines.append("\n📍 GPS координаты:")
            try:
                def to_decimal(dms, ref):
                    d, m, s = dms
                    dec = float(d) + float(m) / 60 + float(s) / 3600
                    if ref in ['S', 'W']:
                        dec = -dec
                    return round(dec, 6)

                lat = to_decimal(gps_info['GPSLatitude'],  gps_info['GPSLatitudeRef'])
                lon = to_decimal(gps_info['GPSLongitude'], gps_info['GPSLongitudeRef'])
                lines.append(f"Lat: {lat}, Lon: {lon}")
                lines.append(f"Карта: https://maps.google.com/?q={lat},{lon}")
            except Exception:
                lines.append("GPS-данные есть, но не удалось расшифровать")

        return '\n'.join(lines)
    except Exception as e:
        log.error(f"extract_exif: {e}")
        return f"⚠️ EXIF ошибка: {e}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КЛАСТЕРИЗАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cluster_data(data_text: str) -> str:
    prompt = (
        "Проанализируй данные OSINT-расследования. Найди и структурируй:\n"
        "1. Все идентификаторы (ники, email, телефоны, IP, домены)\n"
        "2. Связи между ними\n"
        "3. Временну́ю шкалу если есть даты\n"
        "4. Вероятные выводы\n"
        "5. Конкретные рекомендации что проверить дальше\n\n"
        f"Данные:\n{data_text}"
    )
    return ask_groq(prompt)

def cluster_thread(chat_id: int, data_text: str):
    send_result(chat_id, cluster_data(data_text), "CLUSTER")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DOSSIER — параллельный сбор + AI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_dossier(target: str, chat_id: int):
    with task_semaphore:
        t0 = time.time()
        bot.send_message(chat_id, f"📂 Сбор досье для: {target}")

        futures = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            if validate_nick(target):
                bot.send_message(chat_id, "👤 Запускаю Maigret...")
                futures['MAIGRET'] = ex.submit(maigret_lookup, target)

            if validate_email(target):
                bot.send_message(chat_id, "📧 Запускаю Holehe...")
                futures['HOLEHE'] = ex.submit(holehe_lookup, target)

            if validate_ip(target):
                bot.send_message(chat_id, "🌐 IP + Shodan...")
                futures['IP']     = ex.submit(get_ip_info, target)
                futures['SHODAN'] = ex.submit(shodan_lookup, target)

            if validate_domain(target):
                bot.send_message(chat_id, "🔍 DNS + WHOIS...")
                futures['DNS']   = ex.submit(dns_recon, target)
                futures['WHOIS'] = ex.submit(whois_lookup, target)

            futures['DORKS'] = ex.submit(generate_dorks, target)

            output = {}
            for name, f in futures.items():
                try:
                    output[name] = f.result(timeout=200)
                except Exception as e:
                    output[name] = f"❌ Ошибка: {e}"

        if not output:
            bot.send_message(chat_id, "❌ Не удалось определить тип данных.")
            return

        bot.send_message(chat_id, "🧠 AI анализирует связи...")
        ai_summary = ask_groq(
            f"Цель: {target}\nПроанализируй результаты OSINT-сбора и дай сводный отчёт.",
            context="\n\n".join(
                f"[{name}]\n{text[:1000]}"
                for name, text in output.items()
                if name != 'DORKS'
            )
        )

        elapsed = time.time() - t0
        header = (
            f"╔══════════════════════════════════════╗\n"
            f"║      RAMSEYE OSINT v7.1 — ДОСЬЕ      ║\n"
            f"╚══════════════════════════════════════╝\n"
            f"Цель:  {target}\n"
            f"Дата:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Время: {elapsed:.1f} сек\n"
            f"{'='*40}\n\n"
        )
        body = "\n\n".join(
            f"{'='*40}\n[{name}]\n{'='*40}\n{text}"
            for name, text in output.items()
        )
        footer = (
            f"\n\n{'='*40}\n"
            f"[AI АНАЛИЗ]\n"
            f"{'='*40}\n{ai_summary}"
        )

        send_result(chat_id, header + body + footer, f"Досье_{target}")

        bot.send_message(
            chat_id,
            f"🧠 AI-вывод:\n\n{ai_summary[:3500]}"
        )

def dossier_thread(chat_id: int, target: str):
    run_dossier(target, chat_id)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МЕНЮ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def tools_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("👤 MAIGRET",        callback_data="maigret"),
        types.InlineKeyboardButton("🧠 MAIGRET+AI",     callback_data="maigret_ai"),
        types.InlineKeyboardButton("📧 HOLEHE",         callback_data="holehe"),
        types.InlineKeyboardButton("🌐 IP-ПРОБИВ",      callback_data="ip"),
        types.InlineKeyboardButton("🔭 SHODAN",         callback_data="shodan"),
        types.InlineKeyboardButton("🔍 DNS",            callback_data="dns"),
        types.InlineKeyboardButton("🌍 WHOIS",          callback_data="whois"),
        types.InlineKeyboardButton("🕸 DORKS",          callback_data="dorks"),
        types.InlineKeyboardButton("🧠 CLUSTER",        callback_data="cluster"),
        types.InlineKeyboardButton("🔑 GITHUB SECRETS", callback_data="github"),
        types.InlineKeyboardButton("📡 TELEGRAM",       callback_data="tg"),
        types.InlineKeyboardButton("📡 VK",             callback_data="vk"),
        types.InlineKeyboardButton("📷 EXIF-ФОТО",      callback_data="exif"),
        types.InlineKeyboardButton("❌ ЗАКРЫТЬ",        callback_data="close"),
    )
    return m

def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add("🔍 OSINT SEARCH", "🧠 RAMSEYE AI")
    m.add("📂 DOSSIER", "ℹ️ ПОМОЩЬ")
    return m

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ОБРАБОТЧИКИ TELEGRAM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    bot.send_message(
        message.chat.id,
        "🦾 <b>RAMSEYE OSINT v7.1</b>\n\n"
        "🔍 <b>OSINT SEARCH</b> — 13 инструментов разведки\n"
        "🧠 <b>RAMSEYE AI</b> — Llama 4 Scout\n"
        "📂 <b>DOSSIER</b> — параллельный сбор + AI-отчёт\n"
        "ℹ️ <b>ПОМОЩЬ</b> — справка\n\n"
        "👇 Выбери действие",
        parse_mode='HTML',
        reply_markup=main_menu()
    )
    log.info(f"Start: uid={message.from_user.id}")

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    if not auth_check(call):
        bot.answer_callback_query(call.id, "❌ Доступ запрещён")
        return
    if call.data == "close":
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
        bot.answer_callback_query(call.id, "Закрыто")
        return

    prompts = {
        "maigret":    "👤 Введи ник (a-z, 0-9, _, 3-32 символа):",
        "maigret_ai": "🧠 Введи ник для глубокого AI-анализа:",
        "holehe":     "📧 Введи email:",
        "ip":         "🌐 Введи IP-адрес:",
        "shodan":     "🔭 Введи IP для Shodan:",
        "dns":        "🔍 Введи домен (example.com):",
        "whois":      "🌍 Введи домен для WHOIS:",
        "dorks":      "🕸 Введи запрос для Google Dorks:",
        "cluster":    "🧠 Вставь данные для анализа связей:",
        "github":     "🔑 Введи запрос (например: openai или company_name):",
        "tg":         "📡 Введи username Telegram (без @):",
        "vk":         "📡 Введи ID или username VK:",
        "exif":       "📷 Отправь фотографию файлом (не сжатую):",
    }

    clear_step(call.from_user.id)
    set_step(call.from_user.id, call.data)
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        prompts.get(call.data, "Введи данные:")
    )

@bot.message_handler(content_types=['photo', 'document'])
def on_media(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return

    if get_step(message.from_user.id) != "exif":
        bot.send_message(message.chat.id, "Используй кнопки меню 👇", reply_markup=main_menu())
        return

    clear_step(message.from_user.id)
    bot.send_message(message.chat.id, "📷 Извлекаю EXIF...")

    fpath = f"/tmp/exif_{int(time.time())}.jpg"
    try:
        file_info = (
            bot.get_file(message.document.file_id)
            if message.document
            else bot.get_file(message.photo[-1].file_id)
        )
        downloaded = bot.download_file(file_info.file_path)
        with open(fpath, 'wb') as f:
            f.write(downloaded)
        result = extract_exif(fpath)
        bot.send_message(message.chat.id, result)
    except Exception as e:
        log.error(f"EXIF handler: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    finally:
        if os.path.exists(fpath):
            os.remove(fpath)

@bot.message_handler(func=lambda m: True, content_types=['text'])
def on_text(message):
    if not auth_check(message):
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return

    text = message.text or ""
    uid  = message.from_user.id
    step = get_step(uid)

    # ── Ожидание ввода ───────────────────────────────────────────
    if step:
        if text == "/cancel":
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
            return

        clear_step(uid)

        if step == "maigret":
            if validate_nick(text):
                bot.send_message(message.chat.id, "⏳ Maigret запущен (до 160 сек)...")
                threading.Thread(
                    target=maigret_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный ник (a-z, 0-9, _, 3-32 символа)")

        elif step == "maigret_ai":
            if validate_nick(text):
                bot.send_message(message.chat.id, "🧠 Maigret+AI запущен (до 3 мин)...")
                threading.Thread(
                    target=maigret_ai_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный ник")
            return

        elif step == "holehe":
            if validate_email(text):
                bot.send_message(message.chat.id, "⏳ Holehe запущен...")
                threading.Thread(
                    target=holehe_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный email")

        elif step == "ip":
            if validate_ip(text):
                bot.send_message(message.chat.id, "⏳ IP-запрос...")
                threading.Thread(
                    target=ip_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP")

        elif step == "shodan":
            if validate_ip(text):
                bot.send_message(message.chat.id, "⏳ Shodan запрос...")
                threading.Thread(
                    target=shodan_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный IP")

        elif step == "dns":
            if validate_domain(text):
                bot.send_message(message.chat.id, "⏳ DNS-разведка...")
                threading.Thread(
                    target=dns_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный домен")

        elif step == "whois":
            if validate_domain(text):
                bot.send_message(message.chat.id, "⏳ WHOIS запрос...")
                threading.Thread(
                    target=whois_thread, args=(message.chat.id, text), daemon=True
                ).start()
            else:
                bot.send_message(message.chat.id, "❌ Некорректный домен")

        elif step == "dorks":
            result = generate_dorks(text)
            bot.send_message(
                message.chat.id, result,
                disable_web_page_preview=True
            )

        elif step == "cluster":
            bot.send_message(message.chat.id, "🧠 Анализирую связи...")
            threading.Thread(
                target=cluster_thread, args=(message.chat.id, text), daemon=True
            ).start()

        elif step == "github":
            bot.send_message(message.chat.id, "🔑 Поиск секретов на GitHub...")
            threading.Thread(
                target=github_thread, args=(message.chat.id, text), daemon=True
            ).start()

        elif step == "tg":
            bot.send_message(message.chat.id, "📡 Парсинг Telegram...")
            threading.Thread(
                target=tg_thread, args=(message.chat.id, text), daemon=True
            ).start()

        elif step == "vk":
            bot.send_message(message.chat.id, "📡 Парсинг VK...")
            threading.Thread(
                target=vk_thread, args=(message.chat.id, text), daemon=True
            ).start()

        elif step == "exif":
            bot.send_message(message.chat.id, "📷 Отправь фото файлом (не сжатое)")
            set_step(uid, "exif")
            return

        elif step == "dossier":
            bot.send_message(message.chat.id, "⏳ Запускаю сбор досье...")
            threading.Thread(
                target=dossier_thread, args=(message.chat.id, text), daemon=True
            ).start()

        elif step == "groq":
            bot.send_message(message.chat.id, "⏳ RamsEye AI думает...")
            threading.Thread(
                target=groq_thread, args=(message.chat.id, text), daemon=True
            ).start()

        return

    # ── Главное меню ─────────────────────────────────────────────
    if text == "🔍 OSINT SEARCH":
        bot.send_message(
            message.chat.id,
            "🔍 <b>Выбери инструмент:</b>",
            parse_mode='HTML',
            reply_markup=tools_menu()
        )

    elif text == "🧠 RAMSEYE AI":
        set_step(uid, "groq")
        bot.send_message(message.chat.id, "🧠 Задай вопрос (или /cancel):")

    elif text == "📂 DOSSIER":
        set_step(uid, "dossier")
        bot.send_message(
            message.chat.id,
            "📂 Введи цель:\n"
            "• Ник → Maigret\n"
            "• Email → Holehe\n"
            "• IP → IP-пробив + Shodan\n"
            "• Домен → DNS + WHOIS\n"
            "• Любой → Google Dorks + AI-анализ"
        )

    elif text == "ℹ️ ПОМОЩЬ":
        bot.send_message(
            message.chat.id,
            "<b>📖 RamsEye OSINT v7.1</b>\n\n"
            "👤 Maigret — поиск ника по сотням сайтов\n"
            "🧠 Maigret+AI — поиск + глубокий AI-анализ связей\n"
            "📧 Holehe — проверка email по сервисам\n"
            "🌐 IP — геолокация, ISP, VPN/Proxy/Tor\n"
            "🔭 Shodan — открытые порты, CVE\n"
            "🔍 DNS — A/MX/TXT/NS/SOA + субдомены\n"
            "🌍 WHOIS — регистратор, даты, контакты\n"
            "🕸 Dorks — 12 Google Dorks\n"
            "🧠 Cluster — AI-анализ связей по любым данным\n"
            "🔑 GitHub Secrets — поиск утечек ключей\n"
            "📡 Telegram — парсинг профиля/канала\n"
            "📡 VK — парсинг профиля\n"
            "📷 EXIF — GPS и метаданные из фото\n"
            "📂 Dossier — всё параллельно + AI-отчёт\n\n"
            "/cancel — отмена ввода",
            parse_mode='HTML'
        )

    elif text == "/cancel":
        if get_step(uid):
            clear_step(uid)
            bot.send_message(message.chat.id, "✅ Отменено.")
        else:
            bot.send_message(message.chat.id, "Нет активного ожидания.")

    else:
        bot.send_message(
            message.chat.id,
            "Используй кнопки меню 👇",
            reply_markup=main_menu()
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАПУСК
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == '__main__':
    print("🦾 RamsEye OSINT v7.1 — FULL PRODUCTION")
    print("=" * 50)
    print(f"ADMIN_ID  : {'✅ задан' if ADMIN_ID else '❌ НЕ ЗАДАН'}")
    print(f"TOKEN     : {'✅ задан' if TOKEN    else '❌ НЕ ЗАДАН'}")
    print(f"GROQ      : {'✅ задан' if GROQ_KEY else '❌ не задан'}")
    print(f"SHODAN    : {'✅ задан' if SHODAN_KEY else '⚠️  опционально'}")
    print(f"RENDER_URL: {RENDER_URL or '⚠️  не задан'}")
    print("=" * 50)

    if not ADMIN_ID or not TOKEN:
        print("❌ FATAL: задайте ADMIN_ID и TELEGRAM_TOKEN в переменных окружения")
        exit(1)

    while True:
        try:
            log.info("Polling started")
            bot.polling(none_stop=True, interval=0, timeout=60)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
