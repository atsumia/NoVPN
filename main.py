import asyncio
import base64
import html
import logging
import os
import re
import secrets
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NoVPN")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Список ID администраторов (по умолчанию ваш ID 8622456469)
RAW_ADMINS = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "8622456469"))
ADMIN_IDS = set()
for item in RAW_ADMINS.split(","):
    cleaned = item.strip()
    if cleaned.isdigit():
        ADMIN_IDS.add(int(cleaned))
if not ADMIN_IDS:
    ADMIN_IDS.add(8622456469)

RAW_APP_URL = os.getenv("APP_URL", "https://novpn-production.up.railway.app").strip().rstrip("/")
if not RAW_APP_URL.startswith("http://") and not RAW_APP_URL.startswith("https://"):
    APP_URL = f"https://{RAW_APP_URL}"
else:
    APP_URL = RAW_APP_URL

PORT = int(os.getenv("PORT", 8000))

RAW_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./vpn_pool.db")
if RAW_URL.startswith("postgres://"):
    DATABASE_URL = RAW_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif RAW_URL.startswith("postgresql://") and "+asyncpg" not in RAW_URL:
    DATABASE_URL = RAW_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = RAW_URL

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


def make_banner(text_label: str) -> str:
    encoded = urllib.parse.quote(text_label)
    return f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:0?security=none&encryption=none&type=tcp&headerType=none#{encoded}"


def sanitize_node(line: str) -> str:
    """
    Очищает и нормализует дескрипторы узлов, полностью скрывая
    любые упоминания хостинг-провайдеров (Smart Ape, TimeWeb, SberCloud, VK Cloud, Lizeg, Nova)
    и форматируя разделы в соответствии со стандартами 'Архитектуры ширмы'.
    """
    if not line or not line.startswith("vless://"):
        return line

    parts = line.split("#", 1)
    base_url = parts[0]

    # Если это баннер-разделитель (127.0.0.1:0), обновляем его оформление
    if "@127.0.0.1:0" in base_url:
        old_label = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        if "амстердам" in old_label.lower():
            return make_banner("⚡ ─ [ амстердам • 4k direct ] ─")
        elif "шлюз" in old_label.lower() or "рф" in old_label.lower():
            return make_banner("🛡️ ─ [ шлюзы рф • ширма 25ms ] ─")
        elif "резерв" in old_label.lower() or "ес" in old_label.lower():
            return make_banner("🌍 ─ [ резерв • ес / сша ] ─")
        return line

    remark = ""
    # 1. Амстердам 4K Direct (Высокоскоростной кластер)
    if "186.246.47.218" in base_url:
        if "sni=faster.lizeg.ru" in base_url:
            remark = "🇳🇱 novpn • Direct Ultra 4K (Amsterdam #1)"
        elif "sni=1cdevelopment.ru" in base_url:
            remark = "🇳🇱 novpn • Direct Business 1C (Amsterdam #2)"
        elif "sni=www.gazeta-business.ru" in base_url:
            remark = "🇳🇱 novpn • Direct Retail Edge (Amsterdam #3)"
        elif "sni=de.lizeg.ru" in base_url:
            remark = "🇳🇱 novpn • Direct Stealth Net (Amsterdam #4)"
        elif "e4b52c01" in base_url:
            remark = "🇪🇺 novpn • Cloudflare Shield 4K (WARP)"
        else:
            remark = "🇳🇱 novpn • Direct Ultra 4K (Amsterdam)"
    # 2. Архитектура ширмы (Экранирующие шлюзы РФ)
    elif "31.177.111.104" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 01 (Москва L3)"
    elif "82.202.158.28" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 02 (Москва Vision)"
    elif "188.225.62.66" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 03 (СПб Fast)"
    elif "217.199.254.18" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 04 (Москва LTE)"
    elif "90.156.218.83" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 05 (Москва gRPC)"
    elif "89.208.230.49" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 06 (Москва Retail)"
    elif "91.240.86.30" in base_url:
        remark = "🛡️ novpn • Ширма Шлюз 07 (РФ Резерв)"
    # 3. Резервные релеи
    elif any(ip in base_url for ip in ("152.53.236.22", "178.105.168.171")):
        remark = "🌍 novpn • Резерв DE (Франкфурт)"
    elif any(ip in base_url for ip in ("212.87.222.10", "83.219.97.18")):
        remark = "🌍 novpn • Резерв FI (Хельсинки)"
    elif any(ip in base_url for ip in ("86.54.28.75", "208.67.106.154")):
        remark = "🌍 novpn • Резерв NL (Амстердам)"
    elif any(ip in base_url for ip in ("143.246.219.99", "46.250.240.80")):
        remark = "🌍 novpn • Резерв UK (Лондон)"
    elif any(ip in base_url for ip in ("145.223.68.58", "51.77.217.70")):
        remark = "🌍 novpn • Резерв FR (Париж)"
    elif any(ip in base_url for ip in ("93.89.220.37", "150.241.79.149")):
        remark = "🌍 novpn • Резерв SE/EE (Северная Европа)"
    elif any(ip in base_url for ip in ("169.40.42.168", "169.40.42.35")):
        remark = "🌍 novpn • Резерв US (США)"
    else:
        # Для любых динамических узлов полностью вычищаем имена провайдеров
        existing_remark = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        cleaned = re.sub(
            r"(?i)(smart\s*ape|sbercloud|timeweb|vk\s*cloud|jsc\s*iot|lizeg|nova)",
            "Secure Edge",
            existing_remark
        )
        if not cleaned:
            cleaned = "novpn • Secure Node"
        return f"{base_url}#{urllib.parse.quote(cleaned)}"

    return f"{base_url}#{urllib.parse.quote(remark)}"


# Эталонный кластер NoVPN (22 проверенных узла с тремя логическими секциями)
DEDICATED_NODES = [
    make_banner("⚡ ─ [ амстердам • 4k direct ] ─"),
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=faster.lizeg.ru&sid=50#%E2%9A%A1%20novpn%20%E2%80%A2%20Direct%20Ultra%204K%20%28Amsterdam%20%231%29",
    "vless://e4b52c01-79b8-4d5c-9c98-1f5e8c187e02@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=1cdevelopment.ru&sid=50#%F0%9F%87%AA%F0%9F%87%BA%20novpn%20%E2%80%A2%20Cloudflare%20Shield%204K%20%28WARP%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=www.gazeta-business.ru&sid=50#%E2%9A%A1%20novpn%20%E2%80%A2%20Direct%20Retail%20Edge%20%28Amsterdam%20%233%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=1cdevelopment.ru&sid=50#%E2%9A%A1%20novpn%20%E2%80%A2%20Direct%20Business%201C%20%28Amsterdam%20%232%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=de.lizeg.ru&sid=50#%E2%9A%A1%20novpn%20%E2%80%A2%20Direct%20Stealth%20Net%20%28Amsterdam%20%234%29",
    make_banner("🛡️ ─ [ шлюзы рф • ширма 25ms ] ─"),
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@31.177.111.104:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2001%20%28%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20L3%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@82.202.158.28:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=keycdn.com&sid=50&flow=xtls-rprx-vision#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2002%20%28%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20Vision%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@188.225.62.66:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2003%20%28%D0%A1%D0%9F%D0%B1%20Fast%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@217.199.254.18:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=keycdn.com&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2004%20%28%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20LTE%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@90.156.218.83:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2005%20%28%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20gRPC%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@89.208.230.49:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=gazeta-business.ru&sid=50&flow=xtls-rprx-vision#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2006%20%28%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20Retail%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@91.240.86.30:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%9B%A1%EF%B8%8F%20novpn%20%E2%80%A2%20%D0%A8%D0%B8%D1%80%D0%BC%D0%B0%20%D0%A8%D0%BB%D1%8E%D0%B7%2007%20%28%D0%A0%D0%A4%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%29",
    make_banner("🌍 ─ [ резерв • ес / сша ] ─"),
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@152.53.236.22:443?security=reality&encryption=none&pbk=7_3QW1h6XgI2rZ9bI_d8b3h7Z7n_u9X0&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.cloudflare.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20DE%20%28%D0%A4%D1%80%D0%B0%D0%BD%D0%BA%D1%84%D1%83%D1%80%D1%82%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@212.87.222.10:8443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.nokia.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20FI%20%28%D0%A5%D0%B5%D0%BB%D1%8C%D1%81%D0%B8%D0%BD%D0%BA%D0%B8%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@86.54.28.75:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.tradingview.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20NL%20%28%D0%90%D0%BC%D1%81%D1%82%D0%B5%D1%80%D0%B4%D0%B0%D0%BC%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@143.246.219.99:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.vodafone.co.uk&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20UK%20%28%D0%9B%D0%BE%D0%BD%D0%B4%D0%BE%D0%BD%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@145.223.68.58:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.cloudflare.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20FR%20%28%D0%9F%D0%B0%D1%80%D0%B8%D0%B6%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@93.89.220.37:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=ibm.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20EE%20%28%D0%A2%D0%B0%D0%BB%D0%BB%D0%B8%D0%BD%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@169.40.42.168:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=yahoo.com&sid=50&flow=xtls-rprx-vision#%F0%9F%8C%8D%20novpn%20%E2%80%A2%20%D0%A0%D0%B5%D0%B7%D0%B5%D1%80%D0%B2%20US%20%28%D0%9D%D1%8C%D1%8E-%D0%99%D0%BE%D1%80%D0%BA%29",
]

# Кэш актуального флота
current_fleet_nodes = list(DEDICATED_NODES)

# MTProto Proxy для быстрого подключения Telegram
MTPROTO_LINK = "tg://proxy?server=tg.asdqwemary.de5.net&port=8443&secret=eea27a33c5bed5ad8351b5b0eac0632c8a79616e6465782e7275"
MTPROTO_HTTP_LINK = "https://t.me/proxy?server=tg.asdqwemary.de5.net&port=8443&secret=eea27a33c5bed5ad8351b5b0eac0632c8a79616e6465782e7275"

UPSTREAM_FLEET_URL = os.getenv(
    "UPSTREAM_FLEET_URL",
    "https://sub.asdqwemary.de5.net/sub/4305d56015dfcf2b27d4d495865a8326?raw=1"
)


class User(Base):
    """
    Таблица пользователей NoVPN.
    Каждый telegram_id строго изолирован.
    is_active = 0 (Ожидает подтверждения администратора), 1 (Одобрен / Активен).
    """
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    sub_token = Column(String, unique=True, index=True, nullable=True)
    is_active = Column(Integer, default=0)
    role = Column(String, default="user")
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    last_sub_sync = Column(DateTime, nullable=True)
    sub_sync_count = Column(Integer, default=0)


class ProxyServer(Base):
    __tablename__ = "proxy_servers"
    id = Column(Integer, primary_key=True)
    uri = Column(String, unique=True)
    protocol = Column(String)
    tier = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)


KNOWN_FLEET_USERS = [
    {
        "telegram_id": 8622456469,
        "username": "none",
        "first_name": "none",
        "sub_token": "4305d56015dfcf2b27d4d495865a8326",
        "is_active": 1,
        "role": "admin",
        "sub_sync_count": 107,
    },
    {
        "telegram_id": 6614500398,
        "username": "kiryuha",
        "first_name": "Кирилл",
        "sub_token": "ae42247fbc3271c58d7016fa4f571a4c",
        "is_active": 1,
        "role": "user",
        "sub_sync_count": 4,
    },
    {
        "telegram_id": 7808666968,
        "username": "vova",
        "first_name": "Вова",
        "sub_token": "4b24c649cbbecf424887e4c449cd6264",
        "is_active": 1,
        "role": "user",
        "sub_sync_count": 1,
    },
    {
        "telegram_id": 1055426403,
        "username": "maximius",
        "first_name": "Максим",
        "sub_token": "f86bdeb76cffbd34e45e3def8ccbf478",
        "is_active": 1,
        "role": "vip",
        "sub_sync_count": 1,
    },
    {
        "telegram_id": 9990000001,
        "username": "babushka",
        "first_name": "Бабушка",
        "sub_token": "ec02c7c499ea67bb2f1d074582720fe8",
        "is_active": 1,
        "role": "user",
        "sub_sync_count": 13,
    },
]


def generate_secure_token() -> str:
    """Генерация криптографически стойкого токена подписки."""
    return secrets.token_hex(16)


async def init_db():
    logger.info("[DB] Инициализация базы данных и синхронизация пользователей...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        new_columns = [
            ("username", "VARCHAR"),
            ("first_name", "VARCHAR"),
            ("sub_token", "VARCHAR"),
            ("is_active", "INTEGER DEFAULT 0"),
            ("role", "VARCHAR DEFAULT 'user'"),
            ("last_seen_at", "TIMESTAMP"),
            ("last_sub_sync", "TIMESTAMP"),
            ("sub_sync_count", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in new_columns:
            try:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col_name} {col_type};"))
            except Exception:
                try:
                    await conn.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};"))
                except Exception:
                    pass

        # Синхронизация действующих пользователей и их существующих ключей
        for udata in KNOWN_FLEET_USERS:
            try:
                res = await conn.execute(text(f"SELECT id, sub_sync_count FROM users WHERE telegram_id = {udata['telegram_id']} LIMIT 1;"))
                row = res.fetchone()
                if row:
                    curr_syncs = row[1] or 0
                    new_syncs = max(curr_syncs, udata.get("sub_sync_count", 0))
                    await conn.execute(text(
                        f"UPDATE users SET sub_token = '{udata['sub_token']}', is_active = {udata['is_active']}, role = '{udata['role']}', sub_sync_count = {new_syncs} "
                        f"WHERE telegram_id = {udata['telegram_id']};"
                    ))
                else:
                    t_res = await conn.execute(text(f"SELECT id, sub_sync_count FROM users WHERE sub_token = '{udata['sub_token']}' LIMIT 1;"))
                    t_row = t_res.fetchone()
                    if t_row:
                        curr_syncs = t_row[1] or 0
                        new_syncs = max(curr_syncs, udata.get("sub_sync_count", 0))
                        await conn.execute(text(
                            f"UPDATE users SET telegram_id = {udata['telegram_id']}, is_active = {udata['is_active']}, role = '{udata['role']}', sub_sync_count = {new_syncs} "
                            f"WHERE sub_token = '{udata['sub_token']}';"
                        ))
                    else:
                        init_syncs = udata.get("sub_sync_count", 0)
                        await conn.execute(text(
                            f"INSERT INTO users (telegram_id, username, first_name, sub_token, is_active, role, sub_sync_count) "
                            f"VALUES ({udata['telegram_id']}, '{udata['username']}', '{udata['first_name']}', '{udata['sub_token']}', {udata['is_active']}, '{udata['role']}', {init_syncs});"
                        ))
            except Exception as e:
                logger.error(f"[DB Seed Error] {e}")

        for admin_id in ADMIN_IDS:
            try:
                await conn.execute(text(f"UPDATE users SET is_active = 1, role = 'admin' WHERE telegram_id = {admin_id};"))
            except Exception:
                pass

    logger.info(f"[DB] База данных готова и синхронизирована. Активных пользователей: {len(KNOWN_FLEET_USERS)}")


UPSTREAM_STATS_URL = "https://sub.asdqwemary.de5.net/admin/stats"
vps_telemetry_cache = {}


async def sync_upstream_telemetry():
    """
    Синхронизация телеметрии и реального счетчика обращений с сервером VPS.
    Берет точные данные из /admin/stats и сопоставляет с пользователями в Telegram.
    """
    global vps_telemetry_cache
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(UPSTREAM_STATS_URL, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    vps_users = data.get("users", {})
                    vps_telemetry_cache = vps_users
                    async with AsyncSessionLocal() as session_db:
                        for token, v_data in vps_users.items():
                            v_fetch = v_data.get("fetch_count", 0)
                            res = await session_db.execute(select(User).where(User.sub_token == token))
                            u = res.scalar_one_or_none()
                            if u and (u.sub_sync_count or 0) < v_fetch:
                                u.sub_sync_count = v_fetch
                        await session_db.commit()
                    logger.info(f"[Telemetry] Синхронизирована телеметрия с VPS: {len(vps_users)} подписок.")
    except Exception as e:
        logger.warning(f"[Telemetry] Ошибка опроса статистики VPS: {e}")


async def sync_upstream_fleet():
    global current_fleet_nodes
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "NoVPN-Railway-Gateway/2.0"}
            async with session.get(UPSTREAM_FLEET_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.text()
                    lines = []
                    try:
                        decoded = base64.b64decode(data.strip()).decode("utf-8", errors="ignore")
                        lines = [x.strip() for x in decoded.splitlines() if x.strip()]
                    except Exception:
                        lines = [x.strip() for x in data.splitlines() if x.strip()]

                    vless_nodes = [l for l in lines if l.startswith("vless://")]
                    if len(vless_nodes) >= 10:
                        current_fleet_nodes = [sanitize_node(l) for l in lines]
                        logger.info(f"[Fleet] Обновлен и санитизирован флот с VPS: {len(current_fleet_nodes)} строк ({len(vless_nodes)} нод).")
    except Exception as e:
        logger.warning(f"[Fleet] Синхронизация флота с VPS временно недоступна ({e}). Используется локальный эталон.")


router = Router()


def build_main_menu(sub_token: str, is_admin: bool = False) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        types.InlineKeyboardButton(text="🍏 iOS (iPhone / iPad)", callback_data="guide_ios"),
        types.InlineKeyboardButton(text="🤖 Android", callback_data="guide_android"),
    )
    kb.row(
        types.InlineKeyboardButton(text="💻 Windows / Mac", callback_data="guide_pc"),
        types.InlineKeyboardButton(text="✈️ MTProto (Telegram)", url=MTPROTO_HTTP_LINK),
    )
    kb.row(
        types.InlineKeyboardButton(text="🌐 Открыть веб-кабинет", url=f"{APP_URL}/sub/{sub_token}"),
        types.InlineKeyboardButton(text="🔄 Сбросить ссылку", callback_data="reset_prompt"),
    )
    if is_admin:
        kb.row(types.InlineKeyboardButton(text="👑 Панель управления (Админ)", callback_data="admin_dashboard"))
    kb.row(
        types.InlineKeyboardButton(text="❓ О сервисе и защите от блокировок", callback_data="info_service")
    )
    return kb


def make_welcome_text(user: User, sub_link: str) -> str:
    name_display = html.escape(user.first_name or user.username or "друг")
    role_badge = " [Администратор 👑]" if user.telegram_id in ADMIN_IDS else ""
    return (
        f"👋 <b>Привет, {name_display}!</b>{role_badge}\n\n"
        "Добро пожаловать в <b>NoVPN</b> — приватный кластер для свободного доступа в сеть в условиях жестких блокировок ТСПУ и DPI.\n\n"
        "🛡️ <b>Ваш статус:</b> <code>🟢 Активна (Безлимит)</code>\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.telegram_id}</code>\n"
        f"🔄 <b>Синхронизаций:</b> <code>{user.sub_sync_count or 0}</code>\n\n"
        "🔗 <b>Ваша персональная ссылка подписки:</b>\n"
        f"<code>{sub_link}</code>\n\n"
        "<i>Выберите вашу платформу ниже для быстрой настройки в один клик:</i>"
    )


@router.message(CommandStart())
async def start_handler(message: types.Message, bot: Bot):
    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = res.scalar_one_or_none()

        is_new = False
        if not user:
            is_new = True
            user = User(
                telegram_id=user_id,
                username=message.from_user.username or "",
                first_name=message.from_user.first_name or "",
                sub_token=generate_secure_token() if is_admin else None,
                is_active=1 if is_admin else 0,
                role="admin" if is_admin else "user",
                created_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        else:
            user.last_seen_at = datetime.utcnow()
            if message.from_user.username:
                user.username = message.from_user.username
            if message.from_user.first_name:
                user.first_name = message.from_user.first_name
            if is_admin:
                user.is_active = 1
                user.role = "admin"
                if not user.sub_token:
                    user.sub_token = generate_secure_token()
            await session.commit()

        # ЕСЛИ ПОЛЬЗОВАТЕЛЬ НЕ ОДОБРЕН — ПОДПИСКУ НЕ ДАЕМ, ОТПРАВЛЯЕМ ЗАЯВКУ АДМИНУ ТОЛЬКО 1 РАЗ
        if not user.is_active and not is_admin:
            if user.role == "rejected":
                await message.answer(
                    "⛔ <b>Доступ отклонен</b>\n\nАдминистратор отклонил вашу заявку на подключение к NoVPN.",
                    parse_mode=ParseMode.HTML
                )
                return

            if is_new:
                pending_text = (
                    "🔒 <b>Доступ ограничен</b>\n\n"
                    "Сервис <b>NoVPN</b> является закрытым приватным кластером.\n"
                    "Ваша заявка направлена администратору на модерацию.\n\n"
                    "<i>Как только администратор подтвердит доступ, вам сюда сразу придёт уведомление и персональная ссылка на подключение.</i>"
                )
                kb = InlineKeyboardBuilder()
                kb.row(types.InlineKeyboardButton(text="🔄 Проверить статус заявки", callback_data="check_status"))
                await message.answer(text=pending_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())

                # Уведомляем администратора СТРОГО ОДИН РАЗ только при первичной регистрации:
                user_title = html.escape(message.from_user.full_name or "Пользователь")
                username_str = f"@{message.from_user.username}" if message.from_user.username else "отсутствует"
                for admin_id in ADMIN_IDS:
                    try:
                        admin_notice = (
                            "🔔 <b>Новая заявка на доступ в NoVPN!</b>\n\n"
                            f"👤 <b>Пользователь:</b> {user_title}\n"
                            f"🏷 <b>Username:</b> {username_str}\n"
                            f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n"
                            f"📅 <b>Время:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                        akb = InlineKeyboardBuilder()
                        akb.row(
                            types.InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_appr:{user_id}"),
                            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rej:{user_id}")
                        )
                        await bot.send_message(chat_id=admin_id, text=admin_notice, parse_mode=ParseMode.HTML, reply_markup=akb.as_markup())
                    except Exception as e:
                        logger.error(f"[Notify Admin Error] {e}")
            else:
                # Пользователь уже подал заявку и жмет /start повторно — админу спам НЕ шлем!
                already_text = (
                    "⏳ <b>Ваша заявка уже находится на рассмотрении!</b>\n\n"
                    "Администратор уже получил уведомление и рассматривает ваш запрос.\n"
                    "Пожалуйста, ожидайте — персональная ссылка придет автоматически сразу после одобрения."
                )
                kb = InlineKeyboardBuilder()
                kb.row(types.InlineKeyboardButton(text="🔄 Проверить статус заявки", callback_data="check_status"))
                await message.answer(text=already_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
            return

        # Пользователь активен (или админ)
        sub_link = f"{APP_URL}/sub/{user.sub_token}"
        kb = build_main_menu(user.sub_token, is_admin=is_admin)
        welcome_text = make_welcome_text(user, sub_link)
        await message.answer(text=welcome_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.callback_query(F.data == "check_status")
async def callback_check_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = res.scalar_one_or_none()

    if not user or not user.is_active:
        await callback.answer("⏳ Ваша заявка все еще находится на рассмотрении.", show_alert=True)
        return

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    kb = build_main_menu(user.sub_token, is_admin=user.telegram_id in ADMIN_IDS)
    welcome_text = make_welcome_text(user, sub_link)
    await callback.message.edit_text(text=welcome_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await callback.answer("✅ Доступ одобрен!")


@router.callback_query(F.data.startswith("adm_appr:"))
async def callback_admin_approve(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    applicant_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == applicant_id))
        applicant = res.scalar_one_or_none()
        if not applicant:
            await callback.answer("Пользователь не найден в базе данных.", show_alert=True)
            return

        already_active = applicant.is_active == 1
        applicant.is_active = 1
        applicant.role = "user"
        if not applicant.sub_token:
            applicant.sub_token = generate_secure_token()
        sub_token = applicant.sub_token
        applicant_name = applicant.first_name or str(applicant.telegram_id)
        await session.commit()

    status_note = "Подписка выдана, пользователю отправлено уведомление." if not already_active else "Пользователь уже был активирован ранее."
    await callback.message.edit_text(
        f"✅ <b>Заявка одобрена!</b>\n\n"
        f"Пользователь: <b>{html.escape(applicant_name)}</b>\n"
        f"ID: <code>{applicant_id}</code>\n"
        f"{status_note}",
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Доступ открыт!")

    # Отправляем уведомление заявителю ТОЛЬКО если это не повторный клик
    if not already_active:
        try:
            user_kb = build_main_menu(sub_token)
            approved_msg = (
                "🎉 <b>Ваша заявка одобрена!</b>\n\n"
                "Администратор открыл вам доступ к приватному кластеру <b>NoVPN</b>.\n\n"
                f"🔗 <b>Ваша персональная ссылка подписки:</b>\n"
                f"<code>{APP_URL}/sub/{sub_token}</code>\n\n"
                "<i>Выберите вашу платформу ниже для быстрой настройки в один клик:</i>"
            )
            await bot.send_message(chat_id=applicant_id, text=approved_msg, parse_mode=ParseMode.HTML, reply_markup=user_kb.as_markup())
        except Exception as e:
            logger.error(f"[Notify Applicant Error] {e}")


@router.callback_query(F.data.startswith("adm_rej:"))
async def callback_admin_reject(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    applicant_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == applicant_id))
        applicant = res.scalar_one_or_none()
        if applicant:
            applicant.is_active = 0
            applicant.role = "rejected"
            applicant.sub_token = None
            await session.commit()
            applicant_name = applicant.first_name or str(applicant_id)
        else:
            applicant_name = str(applicant_id)

    await callback.message.edit_text(
        f"❌ <b>Заявка отклонена.</b>\n\n"
        f"Пользователь: <b>{html.escape(applicant_name)}</b> (ID: <code>{applicant_id}</code>)",
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Заявка отклонена.")

    try:
        await bot.send_message(
            chat_id=applicant_id,
            text="⛔ <b>Заявка отклонена</b>\n\nАдминистратор отклонил ваш запрос на доступ к NoVPN.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


@router.callback_query(F.data == "admin_dashboard")
async def callback_admin_dashboard(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return

    # Синхронизируем телеметрию с VPS перед показом статистики
    await sync_upstream_telemetry()

    async with AsyncSessionLocal() as session:
        total_res = await session.execute(select(func.count(User.id)))
        total_count = total_res.scalar_one()

        active_res = await session.execute(select(func.count(User.id)).where(User.is_active == 1))
        active_count = active_res.scalar_one()

        pending_res = await session.execute(select(func.count(User.id)).where(User.is_active == 0))
        pending_count = pending_res.scalar_one()

        sync_res = await session.execute(select(func.sum(User.sub_sync_count)))
        total_syncs = sync_res.scalar_one() or 0

    stats_text = (
        "👑 <b>Панель администратора NoVPN</b>\n\n"
        "📊 <b>Статистика пользователей:</b>\n"
        f"• Всего зарегистрировано: <b>{total_count}</b>\n"
        f"• Активных подписок: <b>{active_count}</b>\n"
        f"• Ожидают подтверждения: <b>{pending_count}</b>\n"
        f"• Всего синхронизаций конфигураций: <b>{total_syncs}</b>\n\n"
        f"🌐 <b>Узлов в сети:</b> {len(current_fleet_nodes)}\n\n"
        "<i>Команды быстрого управления:</i>\n"
        "• <code>/approve &lt;tg_id&gt;</code> — выдать доступ\n"
        "• <code>/ban &lt;tg_id&gt;</code> — заблокировать доступ"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="👥 Список пользователей", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    await callback.message.edit_text(text=stats_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "adm_list_users")
async def callback_admin_list_users(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    # Подтягиваем свежую телеметрию с VPS в реальном времени
    await sync_upstream_telemetry()

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).order_by(User.sub_sync_count.desc(), User.id.asc()))
        users = res.scalars().all()

    lines = ["👥 <b>Действующие пользователи NoVPN:</b>\n"]
    for u in users:
        status_ico = "🟢" if u.is_active == 1 else "⏳"
        name = html.escape(u.first_name or u.username or "Пользователь")
        uname = f"(@{u.username})" if u.username else ""
        syncs = u.sub_sync_count or 0

        # Определяем активные клиенты из кэша телеметрии
        v_data = vps_telemetry_cache.get(u.sub_token, {})
        devices = v_data.get("devices", {})
        dev_badges = []
        if devices:
            for d_info in devices.values():
                d_title = d_info.get("title", "")
                if "iPhone" in d_title or "iOS" in d_title:
                    dev_badges.append("📱 iOS")
                elif "Windows" in d_title:
                    dev_badges.append("💻 Windows")
                elif "Android" in d_title:
                    dev_badges.append("🤖 Android")
                elif "v2ray" in d_title:
                    dev_badges.append("💻 v2ray")
                else:
                    dev_badges.append("🌐 VPN")

        dev_str = f" | {', '.join(sorted(set(dev_badges)))}" if dev_badges else ""
        lines.append(f"{status_ico} <b>{name}</b> {uname} — ID: <code>{u.telegram_id}</code>\n   ↳ 🔄 <b>{syncs} syncs</b>{dev_str}\n")

    lines.append("<i>Для блокировки используйте <code>/ban &lt;id&gt;</code></i>")
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    await callback.message.edit_text(text="\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.message(Command("approve"))
async def cmd_approve(message: types.Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/approve &lt;telegram_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == target_id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=target_id, sub_token=generate_secure_token(), is_active=1)
            session.add(user)
        else:
            user.is_active = 1
            if not user.sub_token:
                user.sub_token = generate_secure_token()
        sub_token = user.sub_token
        await session.commit()

    await message.answer(f"✅ Доступ для ID <code>{target_id}</code> успешно выдан!", parse_mode=ParseMode.HTML)
    try:
        user_kb = build_main_menu(sub_token)
        await bot.send_message(
            chat_id=target_id,
            text=f"🎉 <b>Вам открыт доступ к NoVPN!</b>\n\nСсылка подписки:\n<code>{APP_URL}/sub/{sub_token}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=user_kb.as_markup()
        )
    except Exception:
        pass


@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/ban &lt;telegram_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(parts[1])
    if target_id in ADMIN_IDS:
        await message.answer("Нельзя заблокировать администратора.")
        return
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == target_id))
        user = res.scalar_one_or_none()
        if user:
            user.is_active = 0
            user.sub_token = None
            await session.commit()
            await message.answer(f"🚫 Пользователь <code>{target_id}</code> заблокирован, подписка отозвана.", parse_mode=ParseMode.HTML)
        else:
            await message.answer("Пользователь не найден.")


@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ только для администратора.")
        return

    await sync_upstream_telemetry()
    async with AsyncSessionLocal() as session:
        total_res = await session.execute(select(func.count(User.id)))
        total_count = total_res.scalar_one()

        active_res = await session.execute(select(func.count(User.id)).where(User.is_active == 1))
        active_count = active_res.scalar_one()

        pending_res = await session.execute(select(func.count(User.id)).where(User.is_active == 0))
        pending_count = pending_res.scalar_one()

        sync_res = await session.execute(select(func.sum(User.sub_sync_count)))
        total_syncs = sync_res.scalar_one() or 0

    stats_text = (
        "👑 <b>Панель администратора NoVPN</b>\n\n"
        "📊 <b>Статистика пользователей:</b>\n"
        f"• Всего зарегистрировано: <b>{total_count}</b>\n"
        f"• Активных подписок: <b>{active_count}</b>\n"
        f"• Ожидают подтверждения: <b>{pending_count}</b>\n"
        f"• Всего синхронизаций конфигураций: <b>{total_syncs}</b>\n\n"
        f"🌐 <b>Узлов в сети:</b> {len(current_fleet_nodes)}\n\n"
        "<i>Команды быстрого управления:</i>\n"
        "• <code>/users</code> — список пользователей с устройствами\n"
        "• <code>/approve &lt;tg_id&gt;</code> — выдать доступ\n"
        "• <code>/ban &lt;tg_id&gt;</code> — заблокировать доступ"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="👥 Список пользователей", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await message.answer(text=stats_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())


@router.message(Command("users"))
async def cmd_users(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await sync_upstream_telemetry()
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).order_by(User.sub_sync_count.desc(), User.id.asc()))
        users = res.scalars().all()

    lines = ["👥 <b>Действующие пользователи NoVPN:</b>\n"]
    for u in users:
        status_ico = "🟢" if u.is_active == 1 else "⏳"
        name = html.escape(u.first_name or u.username or "Пользователь")
        uname = f"(@{u.username})" if u.username else ""
        syncs = u.sub_sync_count or 0

        v_data = vps_telemetry_cache.get(u.sub_token, {})
        devices = v_data.get("devices", {})
        dev_badges = []
        if devices:
            for d_info in devices.values():
                d_title = d_info.get("title", "")
                if "iPhone" in d_title or "iOS" in d_title:
                    dev_badges.append("📱 iOS")
                elif "Windows" in d_title:
                    dev_badges.append("💻 Windows")
                elif "Android" in d_title:
                    dev_badges.append("🤖 Android")
                elif "v2ray" in d_title:
                    dev_badges.append("💻 v2ray")
                else:
                    dev_badges.append("🌐 VPN")

        dev_str = f" | {', '.join(sorted(set(dev_badges)))}" if dev_badges else ""
        lines.append(f"{status_ico} <b>{name}</b> {uname} — ID: <code>{u.telegram_id}</code>\n   ↳ 🔄 <b>{syncs} syncs</b>{dev_str}\n")

    lines.append("<i>Для блокировки используйте <code>/ban &lt;id&gt;</code></i>")
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    await message.answer(text="\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())


@router.message(Command("status"))
async def cmd_status(message: types.Message):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()

    if not user or not user.is_active:
        await message.answer("🔒 <b>Ваш доступ ожидает подтверждения администратора.</b>", parse_mode=ParseMode.HTML)
        return

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    kb = build_main_menu(user.sub_token, is_admin=message.from_user.id in ADMIN_IDS)
    welcome_text = make_welcome_text(user, sub_link)
    await message.answer(text=welcome_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text_content = (
        "ℹ️ <b>О технологиях NoVPN</b>\n\n"
        "• <b>VLESS Reality + XTLS-Vision:</b> весь трафик маскируется под легитимный TLS-трафик крупных российских корпоративных порталов (1C, X5 Group, Газета). ТСПУ видит лишь обычные зашифрованные сессии к разрешенным сайтам.\n"
        "• <b>Амстердам 4K (1 Гбит/с Direct):</b> прямой высокоскоростной европейский кластер с алгоритмом BBR для просмотра 4K/8K контента и минимального пинга.\n"
        "• <b>Архитектура ширмы (Шлюзы РФ 25мс):</b> отечественные экранирующие шлюзы в Москве и СПб. Устройство взаимодействует исключительно с российскими узлами, полностью скрывая внешний маршрут и инфраструктуру от локальных провайдеров и систем анализа трафика.\n"
        "• <b>Защита Уровня 3 (L3 Cloaking & Anti-Scanner):</b> интеллектуальная фильтрация входящих соединений и honeypot-ловушки для аналитиков и автоматических сканеров, исключающие возможность реверс-инжиниринга сети.\n"
        "• <b>MTProto Fake-TLS:</b> специальный прокси для Telegram с маскировкой под Яндекс — мгновенная передача фото, видео и звонков."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await message.answer(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())


@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()

    if not user or not user.is_active:
        await message.answer("⛔ Доступ не активен.")
        return

    text_content = (
        "⚠️ <b>Перевыпуск персональной ссылки</b>\n\n"
        "Вы действительно хотите сбросить текущую ссылку подписки?\n\n"
        "• Старая ссылка будет мгновенно аннулирована.\n"
        "• Все подключенные ранее устройства перестанут синхронизироваться.\n"
        "• Вам будет выдан новый уникальный ключ."
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        types.InlineKeyboardButton(text="✅ Да, перевыпустить ключ", callback_data="do_reset_token"),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")
    )
    await message.answer(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())


@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()

    if not user or not user.is_active:
        await callback.answer("⛔ Доступ ограничен. Ожидайте подтверждения заявки.", show_alert=True)
        return

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    kb = build_main_menu(user.sub_token, is_admin=callback.from_user.id in ADMIN_IDS)
    welcome_text = make_welcome_text(user, sub_link)
    await callback.message.edit_text(text=welcome_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "guide_ios")
async def callback_guide_ios(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
    if not user or not user.is_active:
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    token = user.sub_token
    text_content = (
        "🍏 <b>Настройка NoVPN на Apple iOS (iPhone / iPad)</b>\n\n"
        "Для работы рекомендуется приложение <b>Happ</b> (или <b>Streisand</b>):\n\n"
        "1️⃣ Установите <b>Happ</b> или <b>Streisand</b> из App Store.\n"
        "2️⃣ Нажмите кнопку <b>«🚀 Добавить в Happ»</b> или <b>«⚡ Добавить в Streisand»</b> ниже — профиль добавится автоматически.\n"
        "3️⃣ Включите туннель переключателем в приложении.\n\n"
        "💡 <b>Рекомендация:</b> включите в приложении режим <i>«Bypass Russian Federation»</i> (раздельное туннелирование). "
        "В этом случае банки, Госуслуги и Ozon продолжат работать напрямую без задержек, "
        "а YouTube, Instagram и заблокированные сайты пойдут через наш гигабитный кластер в Амстердаме."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🚀 Добавить в Happ (1 клик)", url=f"{APP_URL}/open/happ?token={token}"))
    kb.row(types.InlineKeyboardButton(text="⚡ Добавить в Streisand (1 клик)", url=f"{APP_URL}/open/streisand?token={token}"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "guide_android")
async def callback_guide_android(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
    if not user or not user.is_active:
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    token = user.sub_token
    text_content = (
        "🤖 <b>Настройка NoVPN на Android</b>\n\n"
        "Поддерживаются любые клиенты с протоколом VLESS Reality:\n\n"
        "1️⃣ Установите <b>v2rayNG</b>, <b>Happ</b> или <b>Hiddify</b> из Google Play.\n"
        "2️⃣ Нажмите кнопку импорта ниже — конфигурация загрузится в одно касание.\n"
        "3️⃣ В настройках приложения выберите режим <i>«Обходить локальную сеть и РФ»</i>.\n"
        "4️⃣ Запустите подключение."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="📱 Добавить в v2rayNG (1 клик)", url=f"{APP_URL}/open/v2rayng?token={token}"))
    kb.row(types.InlineKeyboardButton(text="🚀 Добавить в Happ (1 клик)", url=f"{APP_URL}/open/happ?token={token}"))
    kb.row(types.InlineKeyboardButton(text="⚡ Добавить в Hiddify (1 клик)", url=f"{APP_URL}/open/hiddify?token={token}"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "guide_pc")
async def callback_guide_pc(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
    if not user or not user.is_active:
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    token = user.sub_token
    sub_link = f"{APP_URL}/sub/{token}"
    text_content = (
        "💻 <b>Настройка NoVPN на Windows / macOS / Linux</b>\n\n"
        "Рекомендуемый клиент: <b>Hiddify</b> (или <b>v2rayN</b>):\n\n"
        "1️⃣ Скачайте <b>Hiddify</b> с официального сайта или GitHub.\n"
        "2️⃣ Нажмите <b>«⚡ Добавить в Hiddify»</b> или вручную вставьте вашу ссылку:\n"
        f"<code>{sub_link}</code>\n"
        "3️⃣ Переключите режим проксирования в <i>VPN Mode</i>.\n"
        "4️⃣ Нажмите кнопку Connect."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="⚡ Добавить в Hiddify", url=f"{APP_URL}/open/hiddify?token={token}"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "reset_prompt")
async def callback_reset_prompt(callback: types.CallbackQuery):
    text_content = (
        "⚠️ <b>Перевыпуск персональной ссылки</b>\n\n"
        "Вы действительно хотите сбросить текущую ссылку подписки?\n\n"
        "• Старая ссылка будет мгновенно аннулирована.\n"
        "• Все подключенные ранее устройства перестанут синхронизироваться.\n"
        "• Вам будет выдан новый уникальный ключ.\n\n"
        "<i>Используйте эту функцию, если вы случайно отправили ссылку посторонним или в публичный чат.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        types.InlineKeyboardButton(text="✅ Да, перевыпустить ключ", callback_data="do_reset_token"),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")
    )
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "do_reset_token")
async def callback_do_reset_token(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
        if not user or not user.is_active:
            await callback.answer("Ошибка: доступ не активен.", show_alert=True)
            return

        new_token = generate_secure_token()
        user.sub_token = new_token
        user.last_seen_at = datetime.utcnow()
        await session.commit()
        await session.refresh(user)
        logger.info(f"[Security] Пользователь ID={user.telegram_id} перевыпустил токен подписки.")

    new_sub_link = f"{APP_URL}/sub/{new_token}"
    text_content = (
        "✅ <b>Ключ подписки успешно обновлен!</b>\n\n"
        "Предыдущая ссылка аннулирована и больше не действует. Ваша новая ссылка:\n"
        f"<code>{new_sub_link}</code>\n\n"
        "<i>Обновите подписку в ваших приложениях с помощью кнопок ниже:</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🚀 Добавить в Happ (iOS)", url=f"{APP_URL}/open/happ?token={new_token}"))
    kb.row(types.InlineKeyboardButton(text="📱 Добавить в v2rayNG (Android)", url=f"{APP_URL}/open/v2rayng?token={new_token}"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer("Ссылка успешно обновлена!", show_alert=True)


@router.callback_query(F.data == "info_service")
async def callback_info_service(callback: types.CallbackQuery):
    text_content = (
        "ℹ️ <b>О технологиях NoVPN</b>\n\n"
        "• <b>VLESS Reality + XTLS-Vision:</b> весь трафик маскируется под легитимный TLS-трафик крупных российских корпоративных порталов (1C, X5 Group, Газета). ТСПУ видит лишь обычные зашифрованные сессии к разрешенным сайтам.\n"
        "• <b>Амстердам 4K (1 Гбит/с Direct):</b> прямой высокоскоростной европейский кластер с алгоритмом BBR для просмотра 4K/8K контента и минимального пинга.\n"
        "• <b>Архитектура ширмы (Шлюзы РФ 25мс):</b> отечественные экранирующие шлюзы в Москве и СПб. Устройство взаимодействует исключительно с российскими узлами, полностью скрывая внешний маршрут и инфраструктуру от локальных провайдеров и систем анализа трафика.\n"
        "• <b>Защита Уровня 3 (L3 Cloaking & Anti-Scanner):</b> интеллектуальная фильтрация входящих соединений и honeypot-ловушки для аналитиков и автоматических сканеров, исключающие возможность реверс-инжиниринга сети.\n"
        "• <b>MTProto Fake-TLS:</b> специальный прокси для Telegram с маскировкой под Яндекс — мгновенная передача фото, видео и звонков."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    await callback.message.edit_text(text=text_content, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


async def cron_loop():
    while True:
        try:
            await sync_upstream_fleet()
            await sync_upstream_telemetry()
        except Exception as e:
            logger.error(f"[Cron Error] {e}")
        await asyncio.sleep(900)


async def setup_bot_commands(bot: Bot):
    """Настройка официального нативного меню команд Telegram (кнопка [Меню] внизу экрана)."""
    user_cmds = [
        BotCommand(command="start", description="Главное меню / Подписка"),
        BotCommand(command="status", description="Статус подключения"),
        BotCommand(command="help", description="Инструкция по настройке"),
        BotCommand(command="reset", description="Сбросить / обновить ключ"),
    ]
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning(f"[Commands] Ошибка установки общих команд: {e}")

    admin_cmds = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="admin", description="👑 Панель управления"),
        BotCommand(command="users", description="👥 Список пользователей"),
        BotCommand(command="help", description="Инструкция по настройке"),
        BotCommand(command="reset", description="Сбросить / обновить ключ"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning(f"[Commands] Ошибка установки команд для админа {admin_id}: {e}")


async def bot_polling_loop():
    if not BOT_TOKEN:
        logger.error("[Bot] BOT_TOKEN не задан! Бот не сможет обрабатывать сообщения.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_commands(bot)
    logger.info("[Bot] Telegram-бот запущен с нативным меню команд и ожидает пользователей...")

    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramConflictError:
            logger.warning("[Bot] Конфликт сессий бота. Ожидание 5 сек...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"[Bot Polling Error] {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await sync_upstream_fleet()
    await sync_upstream_telemetry()
    c_task = asyncio.create_task(cron_loop())
    b_task = asyncio.create_task(bot_polling_loop())
    yield
    c_task.cancel()
    b_task.cancel()


app = FastAPI(title="NoVPN Gateway & Auth Service", lifespan=lifespan)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


def render_web_landing(user: User, token: str) -> str:
    sub_url = f"{APP_URL}/sub/{token}"
    name = html.escape(user.first_name or user.username or "Пользователь")
    sync_count = user.sub_sync_count or 0

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>NoVPN • Личный кабинет</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #090d16;
      color: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
    }}
    .container {{
      max-width: 460px;
      width: 100%;
      background: #111827;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 24px;
      padding: 32px 24px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
      text-align: center;
    }}
    .badge-status {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 16px;
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      background: #10b981;
      border-radius: 50%;
      box-shadow: 0 0 10px #10b981;
    }}
    h1 {{
      font-size: 26px;
      font-weight: 800;
      letter-spacing: -0.5px;
      margin-bottom: 6px;
      color: #ffffff;
    }}
    .subtitle {{
      color: #94a3b8;
      font-size: 14px;
      margin-bottom: 24px;
    }}
    .info-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 24px;
      text-align: left;
    }}
    .info-card {{
      background: #1e293b;
      border-radius: 14px;
      padding: 12px 14px;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .info-card .label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #94a3b8;
      margin-bottom: 4px;
    }}
    .info-card .value {{
      font-size: 14px;
      font-weight: 700;
      color: #f8fafc;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .section-title {{
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #cbd5e1;
      margin: 20px 0 12px;
      text-align: left;
    }}
    .action-btn {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      width: 100%;
      background: #ffffff;
      color: #000000;
      font-weight: 700;
      font-size: 15px;
      padding: 14px 18px;
      border-radius: 14px;
      text-decoration: none;
      margin-bottom: 10px;
      transition: all 0.2s ease;
      box-shadow: 0 4px 12px rgba(255, 255, 255, 0.1);
    }}
    .action-btn:hover {{
      transform: translateY(-2px);
      box-shadow: 0 6px 16px rgba(255, 255, 255, 0.2);
    }}
    .action-btn.secondary {{
      background: #1e293b;
      color: #f1f5f9;
      border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow: none;
    }}
    .action-btn.secondary:hover {{
      background: #334155;
    }}
    .action-btn.telegram {{
      background: #0284c7;
      color: #ffffff;
      box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3);
    }}
    .link-box {{
      position: relative;
      background: #0b0f19;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 12px;
      padding: 12px;
      font-family: monospace;
      font-size: 12px;
      color: #94a3b8;
      word-break: break-all;
      margin-top: 14px;
      text-align: left;
      cursor: pointer;
    }}
    .copy-hint {{
      position: absolute;
      right: 10px;
      bottom: 8px;
      font-family: sans-serif;
      font-size: 11px;
      color: #38bdf8;
      font-weight: 600;
    }}
    .toast {{
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: #10b981;
      color: #ffffff;
      padding: 12px 24px;
      border-radius: 30px;
      font-weight: 700;
      font-size: 14px;
      box-shadow: 0 10px 25px rgba(16, 185, 129, 0.4);
      opacity: 0;
      transition: all 0.3s ease;
      z-index: 100;
    }}
    .toast.show {{
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }}
    footer {{
      margin-top: 24px;
      font-size: 12px;
      color: #64748b;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="badge-status">
      <div class="pulse-dot"></div>
      Подписка активна • Безлимит
    </div>
    <h1>NoVPN Cloud</h1>
    <div class="subtitle">Персональный шлюз с защитой от блокировок ТСПУ</div>

    <div class="info-grid">
      <div class="info-card">
        <div class="label">Пользователь</div>
        <div class="value">{name}</div>
      </div>
      <div class="info-card">
        <div class="label">Синхронизаций</div>
        <div class="value">{sync_count} раз</div>
      </div>
    </div>

    <div class="section-title">Быстрое подключение (1 клик)</div>
    <a href="{APP_URL}/open/happ?token={token}" class="action-btn">
      🚀 Добавить в Happ (iOS / Android)
    </a>
    <a href="{APP_URL}/open/v2rayng?token={token}" class="action-btn secondary">
      📱 Добавить в v2rayNG (Android)
    </a>
    <a href="{APP_URL}/open/streisand?token={token}" class="action-btn secondary">
      ⚡ Добавить в Streisand (iOS / macOS)
    </a>
    <a href="{APP_URL}/open/hiddify?token={token}" class="action-btn secondary">
      💻 Добавить в Hiddify (ПК / Mac)
    </a>
    <a href="{MTPROTO_HTTP_LINK}" class="action-btn telegram">
      ✈️ Telegram MTProto (в 1 клик)
    </a>

    <div class="section-title">Ссылка подписки</div>
    <div class="link-box" onclick="copyLink()">
      {sub_url}
      <div class="copy-hint" id="copyText">📋 Копировать</div>
    </div>

    <div style="margin-top: 14px;">
      <a href="{sub_url}?raw=1" style="color: #64748b; font-size: 13px; text-decoration: underline;">
        Скачать текстовый файл конфигурации (.txt)
      </a>
    </div>

    <footer>
      novpn cluster • XTLS-Vision + Reality • Amsterdam 1G
    </footer>
  </div>

  <div class="toast" id="toast">✅ Ссылка скопирована в буфер обмена!</div>

  <script>
    function copyLink() {{
      const link = "{sub_url}";
      navigator.clipboard.writeText(link).then(() => {{
        const toast = document.getElementById("toast");
        toast.classList.add("show");
        document.getElementById("copyText").innerText = "Скопировано!";
        setTimeout(() => {{
          toast.classList.remove("show");
          document.getElementById("copyText").innerText = "📋 Копировать";
        }}, 2500);
      }}).catch(() => {{
        prompt("Скопируйте ссылку:", link);
      }});
    }}
  </script>
</body>
</html>"""


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "NoVPN",
        "app_url": APP_URL,
        "active_fleet_nodes": len(current_fleet_nodes),
    }


@app.get("/sub/{token}")
async def get_subscription(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Основной шлюз выдачи подписки.
    Только для активных (is_active=1) пользователей.
    Включает защиту Уровня 3 (Anti-Scanner & Honeypot Tarpit).
    """
    user_res = await db.execute(select(User).where(User.sub_token == token))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Subscription token not found or revoked")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Subscription is suspended or pending approval")

    user_agent = request.headers.get("User-Agent", "").strip()
    ua_lower = user_agent.lower()
    accept_header = request.headers.get("Accept", "").lower()
    raw_param = request.query_params.get("raw", "")
    admin_key = request.query_params.get("admin_key", "")
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()

    # 1. Признаки официальных VPN-клиентов
    vpn_clients = (
        "happ", "v2ray", "hiddify", "sing-box", "shadowrocket",
        "nekobox", "karing", "streisand", "clash", "loon", "surge",
        "flclash", "foxray", "wireguard", "leaf", "dart", "okhttp",
        "stash", "v2box", "foxtray", "fair", "quantumult", "sub_update"
    )
    is_vpn_client = any(k in ua_lower for k in vpn_clients)

    # 2. Детекция сканеров, ботов, CLI инструментов и анализаторов (Уровень 3)
    scanner_keywords = (
        "curl", "python", "requests", "aiohttp", "urllib", "wget", "httpclient",
        "java", "go-http-client", "postman", "insomnia", "censys", "shodan",
        "zgrab", "nmap", "masscan", "nikto", "sqlmap", "burp", "headless",
        "phantom", "selenium", "puppeteer", "crawler", "spider", "scan", "bot"
    )
    is_scanner = any(k in ua_lower for k in scanner_keywords) or len(ua_lower) < 4

    # 3. Браузерный доступ (для веб-портала в 1 клик)
    force_raw = raw_param == "1"
    is_browser = (
        ("text/html" in accept_header or "mozilla" in ua_lower or "safari" in ua_lower or "chrome" in ua_lower)
        and not is_vpn_client
        and not is_scanner
        and not force_raw
    )

    if is_browser:
        html_page = render_web_landing(user, token)
        return HTMLResponse(content=html_page, status_code=200)

    # 4. УРОВЕНЬ 3: ЗАЩИТА ОТ СКАНЕРОВ И АНАЛИТИКОВ (HONEYPOT / TARPIT)
    admin_auth = request.headers.get("X-Novpn-Admin", "") == "true" or admin_key == "novpn2026"
    if (is_scanner or (not is_vpn_client and not force_raw)) and not admin_auth:
        logger.warning(
            f"[Security Level 3] Перехвачен сканер/аналитик: IP={client_ip}, UA='{user_agent}', Token={token[:6]}***"
        )
        decoy_nodes = [
            make_banner("🛡️ ─ [ novpn • security honeypot ] ─"),
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?security=none#%F0%9F%94%92%20novpn%20%E2%80%A2%20Direct%20CLI%20Access%20Prohibited",
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:80?security=none#%E2%9A%A0%EF%B8%8F%20Automated%20Scanner%20Detected",
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:8080?security=none#%F0%9F%93%B1%20Use%20Official%20App%20(Happ%20%2F%20v2rayNG)",
        ]
        raw_decoy = "\n".join(decoy_nodes)
        decoy_encoded = base64.b64encode(raw_decoy.encode("utf-8")).decode("utf-8")
        headers = {
            "Profile-Title": "novpn-decoy",
            "Profile-Update-Interval": "24",
            "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0",
            "Content-Disposition": 'attachment; filename="novpn.txt"; filename*=UTF-8\'\'novpn',
        }
        return Response(content=decoy_encoded, media_type="text/plain; charset=utf-8", headers=headers)

    # 5. Легитимная выдача подписки для официальных клиентов
    user.last_sub_sync = datetime.utcnow()
    user.sub_sync_count = (user.sub_sync_count or 0) + 1
    await db.commit()

    raw_nodes = list(current_fleet_nodes) if current_fleet_nodes else list(DEDICATED_NODES)
    clean_nodes = [sanitize_node(n) for n in raw_nodes]
    raw_payload = "\n".join(clean_nodes)
    encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")

    headers = {
        "Profile-Title": "novpn",
        "Profile-Update-Interval": "6",
        "Subscription-Userinfo": "upload=0; download=0; total=0; expire=1893456000",
        "Content-Disposition": 'attachment; filename="novpn.txt"; filename*=UTF-8\'\'novpn',
    }
    return Response(content=encoded, media_type="text/plain; charset=utf-8", headers=headers)


@app.get("/open/{client}")
async def redirect_to_client(client: str, token: str, db: AsyncSession = Depends(get_db)):
    user_res = await db.execute(select(User).where(User.sub_token == token))
    user = user_res.scalar_one_or_none()
    if not user or not user.is_active:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='background:#0b0f19;color:#fff;text-align:center;padding:50px;font-family:sans-serif;'>"
            "<h2>⚠️ Доступ ограничен или ссылка недействительна</h2><p>Пожалуйста, перейдите в Telegram-бот и проверьте статус доступа.</p>"
            "</body></html>",
            status_code=403 if user else 404,
        )

    sub_url = f"{APP_URL}/sub/{token}"
    if client == "happ":
        target = f"happ://add/{sub_url}"
    elif client == "v2rayng":
        target = f"v2rayng://install-config?url={sub_url}"
    elif client == "hiddify":
        target = f"hiddify://install-sub?url={sub_url}"
    elif client == "streisand":
        target = f"streisand://import/{sub_url}"
    elif client == "tgproxy":
        target = MTPROTO_LINK
    else:
        target = sub_url

    html_content = f"""<!DOCTYPE html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <title>NoVPN Connect</title>
    <meta http-equiv="refresh" content="0; url={target}">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
      body {{ margin: 0; padding: 0; background: #090d16; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
      .card {{ background: #111827; border: 1px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 36px 28px; max-width: 380px; width: 90%; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }}
      .logo {{ font-size: 24px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px; margin-bottom: 8px; }}
      .desc {{ color: #94a3b8; font-size: 14px; margin-bottom: 28px; line-height: 1.5; }}
      .btn {{ display: block; background: #ffffff; color: #000000; font-weight: 700; font-size: 16px; padding: 14px 20px; border-radius: 12px; text-decoration: none; transition: transform 0.15s ease; }}
      .btn:active {{ transform: scale(0.98); }}
    </style>
    <script>
      setTimeout(function() {{ window.location.href = "{target}"; }}, 80);
    </script>
  </head>
  <body>
    <div class="card">
      <div class="logo">novpn</div>
      <div class="desc">Перенаправление в приложение... Если оно не открылось автоматически, нажмите кнопку ниже:</div>
      <a href="{target}" class="btn">👉 Открыть приложение</a>
    </div>
  </body>
</html>"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    server_config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(server_config)
    asyncio.run(server.serve())
