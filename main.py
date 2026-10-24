import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import secrets
import time
import urllib.parse
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

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


def format_banner_node(line: str) -> str:
    """
    Добавляет понятные стрелки вниз ⬇️ на перекладины-разделители,
    чтобы пользователи сразу понимали, что серверы нужно выбирать ниже.
    """
    if not line or "@127.0.0.1:0" not in line:
        return line
    parts = line.split("#", 1)
    lbl = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
    if "амстердам" in lbl.lower():
        return make_banner("⚡ ── [ амстердам • 4k ] ── ⬇️")
    elif "шлюз" in lbl.lower() or "рф" in lbl.lower():
        return make_banner("🛡️ ── [ шлюзы рф • 25ms ] ── ⬇️")
    elif "резерв" in lbl.lower() or "ес" in lbl.lower():
        return make_banner("🌍 ── [ резерв • ес / сша ] ── ⬇️")
    return line


# Эталонный кластер NoVPN (22 проверенных узла с тремя логическими секциями)
DEDICATED_NODES = [
    make_banner("⚡ ── [ амстердам • 4k ] ── ⬇️"),
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=faster.lizeg.ru&sid=50#%F0%9F%87%B3%F0%9F%87%B1%20novpn%20%E2%80%A2%20Direct%20Ultra%20%28Amsterdam%29",
    "vless://e4b52c01-79b8-4d5c-9c98-1f5e8c187e02@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=1cdevelopment.ru&sid=50#%F0%9F%87%AA%F0%9F%87%BA%20novpn%20%E2%80%A2%20Cloudflare%20Shield%20%28WARP%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=www.gazeta-business.ru&sid=50#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20Retail%20Edge%20%28X5%20Group%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=1cdevelopment.ru&sid=50#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20Business%20%281C%20Enterprise%29",
    "vless://29e6cdd5-db74-43a9-870f-a74e630a4faf@186.246.47.218:443?security=reality&encryption=none&pbk=QZ-K_mgnEvdfSPzZ4fCokkeDAXH91zO1hq4S4QNOtF8&headerType=none&fp=qq&spx=%2F&type=tcp&flow=xtls-rprx-vision&sni=de.lizeg.ru&sid=50#%F0%9F%87%A9%F0%9F%87%AA%20novpn%20%E2%80%A2%20Alternative%20%28Lizeg%29",
    make_banner("🛡️ ── [ шлюзы рф • 25ms ] ── ⬇️"),
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@31.177.111.104:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20Smart%20Ape%20%28L3%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@82.202.158.28:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=keycdn.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20SberCloud",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@188.225.62.66:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20TimeWeb%20%28Fast%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@217.199.254.18:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=keycdn.com&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20TimeWeb%20%28LTE%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@90.156.218.83:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20VK%20Cloud%20%28gRPC%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@89.208.230.49:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=gazeta-business.ru&sid=50&flow=xtls-rprx-vision#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20VK%20Cloud%20%28X5%29",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@91.240.86.30:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=grpc&sni=www.gazeta-business.ru&sid=50&serviceName=ads.x5.ru&mode=gun#%F0%9F%87%B7%F0%9F%87%BA%20novpn%20%E2%80%A2%20JSC%20IOT",
    make_banner("🌍 ── [ резерв • ес / сша ] ── ⬇️"),
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@152.53.236.22:443?security=reality&encryption=none&pbk=7_3QW1h6XgI2rZ9bI_d8b3h7Z7n_u9X0&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.cloudflare.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%A9%F0%9F%87%AA%20Relay%20%E2%80%A2%20Germany",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@212.87.222.10:8443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.nokia.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%AB%F0%9F%87%AE%20Relay%20%E2%80%A2%20Finland",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@86.54.28.75:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.tradingview.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%B3%F0%9F%87%B1%20Relay%20%E2%80%A2%20Netherlands",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@143.246.219.99:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.vodafone.co.uk&sid=50&flow=xtls-rprx-vision#%F0%9F%87%AC%F0%9F%87%A7%20Relay%20%E2%80%A2%20United%20Kingdom",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@145.223.68.58:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=www.cloudflare.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%AB%F0%9F%87%B7%20Relay%20%E2%80%A2%20France",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@93.89.220.37:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=ibm.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%B8%F0%9F%87%AA%20Relay%20%E2%80%A2%20Sweden",
    "vless://699f307f-c4e2-4994-8401-15e7e6d8c0b4@169.40.42.168:443?security=reality&encryption=none&pbk=-tePObR3oZwGAUOb5kqTYkNWl6rtUKl0RFuzuu06wgw&headerType=none&fp=qq&spx=%2F&type=tcp&sni=yahoo.com&sid=50&flow=xtls-rprx-vision#%F0%9F%87%BA%F0%9F%87%B8%20Relay%20%E2%80%A2%20United%20States",
]

# Кэш актуального флота
current_fleet_nodes = list(DEDICATED_NODES)

# MTProto Proxy для быстрого подключения Telegram
MTPROTO_LINK = "tg://proxy?server=tg.asdqwemary.de5.net&port=8443&secret=eea27a33c5bed5ad8351b5b0eac0632c8a79616e6465782e7275"
MTPROTO_HTTP_LINK = "https://t.me/proxy?server=tg.asdqwemary.de5.net&port=8443&secret=eea27a33c5bed5ad8351b5b0eac0632c8a79616e6465782e7275"

UPSTREAM_FLEET_URL = os.getenv(
    "UPSTREAM_FLEET_URL",
    "https://sub.asdqwemary.de5.net:2053/sub/4305d56015dfcf2b27d4d495865a8326?raw=1"
)
UPSTREAM_GATEWAY_SECRET = os.getenv(
    "UPSTREAM_GATEWAY_SECRET",
    "novpn_sec_8f29c41d7e3a90b561c284e7f309d8aa"
)

DEFAULT_MAX_DEVICES = 5
DEVICE_INACTIVE_TTL_DAYS = 30
PROVISIONAL_DEVICE_TTL_HOURS = 2  # Однократные тесты / случайные пробы живут не более 2 часов без подтверждения (sync_count >= 2)


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
    max_devices = Column(Integer, default=DEFAULT_MAX_DEVICES, nullable=True)


class ProxyServer(Base):
    __tablename__ = "proxy_servers"
    id = Column(Integer, primary_key=True)
    uri = Column(String, unique=True)
    protocol = Column(String)
    tier = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserDevice(Base):
    """
    Таблица авторизованных устройств пользователя.
    Лимит: до 5 устройств на обычного пользователя.
    Для администратора (роль 'admin') — ПОЛНЫЙ БЕЗЛИМИТ.
    """
    __tablename__ = "user_devices"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    sub_token = Column(String, index=True, nullable=False)
    device_fp = Column(String, index=True, nullable=False)
    device_label = Column(String, nullable=True)
    sync_count = Column(Integer, default=1)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


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
    {
        "telegram_id": 9990000002,
        "username": "parents",
        "first_name": "Мама и Папа",
        "sub_token": "82389f987a17b4d6b631871685c9dce2",
        "is_active": 1,
        "role": "user",
        "max_devices": 2,
        "sub_sync_count": 0,
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
            ("max_devices", "INTEGER DEFAULT 5"),
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
                max_dev = udata.get("max_devices", DEFAULT_MAX_DEVICES)
                res = await conn.execute(text(f"SELECT id, sub_sync_count FROM users WHERE telegram_id = {udata['telegram_id']} LIMIT 1;"))
                row = res.fetchone()
                if row:
                    curr_syncs = row[1] or 0
                    new_syncs = max(curr_syncs, udata.get("sub_sync_count", 0))
                    await conn.execute(text(
                        f"UPDATE users SET sub_token = '{udata['sub_token']}', is_active = {udata['is_active']}, role = '{udata['role']}', sub_sync_count = {new_syncs}, max_devices = {max_dev}, username = '{udata['username']}', first_name = '{udata['first_name']}' "
                        f"WHERE telegram_id = {udata['telegram_id']};"
                    ))
                else:
                    t_res = await conn.execute(text(f"SELECT id, sub_sync_count FROM users WHERE sub_token = '{udata['sub_token']}' LIMIT 1;"))
                    t_row = t_res.fetchone()
                    if t_row:
                        curr_syncs = t_row[1] or 0
                        new_syncs = max(curr_syncs, udata.get("sub_sync_count", 0))
                        await conn.execute(text(
                            f"UPDATE users SET telegram_id = {udata['telegram_id']}, is_active = {udata['is_active']}, role = '{udata['role']}', sub_sync_count = {new_syncs}, max_devices = {max_dev}, username = '{udata['username']}', first_name = '{udata['first_name']}' "
                            f"WHERE sub_token = '{udata['sub_token']}';"
                        ))
                    else:
                        init_syncs = udata.get("sub_sync_count", 0)
                        await conn.execute(text(
                            f"INSERT INTO users (telegram_id, username, first_name, sub_token, is_active, role, sub_sync_count, max_devices) "
                            f"VALUES ({udata['telegram_id']}, '{udata['username']}', '{udata['first_name']}', '{udata['sub_token']}', {udata['is_active']}, '{udata['role']}', {init_syncs}, {max_dev});"
                        ))
            except Exception as e:
                logger.error(f"[DB Seed Error] {e}")

        for admin_id in ADMIN_IDS:
            try:
                await conn.execute(text(f"UPDATE users SET is_active = 1, role = 'admin', username = 'none', first_name = 'none' WHERE telegram_id = {admin_id};"))
            except Exception:
                pass

        # Устраняем дубликаты пользователей по sub_token если они возникли
        try:
            await conn.execute(text("DELETE FROM users WHERE id NOT IN (SELECT MAX(id) FROM users GROUP BY sub_token);"))
        except Exception:
            pass

        # Миграция схемы: добавление sync_count в user_devices если отсутствует
        try:
            await conn.execute(text("ALTER TABLE user_devices ADD COLUMN sync_count INTEGER DEFAULT 1;"))
        except Exception:
            pass

        # Очистка синтетических тестовых записей устройств и паразитных админских привязок
        try:
            await conn.execute(text("DELETE FROM user_devices WHERE device_label LIKE '%iPhone 14 Pro%' OR device_label LIKE '%Happ 3.2.0%' OR device_label LIKE '%v2rayNG%';"))
            await conn.execute(text("DELETE FROM user_devices WHERE sub_token != '4305d56015dfcf2b27d4d495865a8326' AND (device_label LIKE '%Apple iPhone%' OR device_label LIKE '%iPhone%' OR device_fp LIKE '%2606221809551%');"))
        except Exception:
            pass

    logger.info(f"[DB] База данных готова и синхронизирована. Активных пользователей: {len(KNOWN_FLEET_USERS)}")


UPSTREAM_STATS_URL = os.getenv(
    "UPSTREAM_STATS_URL",
    "https://sub.asdqwemary.de5.net:2053/admin/stats"
)
APPLE_MODELS = {
    # iPhone 16
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    # iPhone 15
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    # iPhone 14
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    # iPhone 13
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,5": "iPhone 13",
    "iPhone14,4": "iPhone 13 mini",
    # iPhone 12
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone13,2": "iPhone 12",
    "iPhone13,1": "iPhone 12 mini",
    # iPhone 11
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    # iPhone SE
    "iPhone12,8": "iPhone SE (2nd gen)",
    "iPhone14,6": "iPhone SE (3rd gen)",
    # iPhone X / XS / XR
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    "iPhone10,3": "iPhone X",
    "iPhone10,6": "iPhone X",
    # iPhone 8
    "iPhone10,1": "iPhone 8",
    "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,5": "iPhone 8 Plus",
}


def parse_device_badge(model: str = "", os_name: str = "", ua: str = "", raw_title: str = "") -> str:
    """
    Интеллектуальный парсер оборудования, ОС и клиента в премиальный бейдж.
    Примеры:
    - 📱 iPhone 14 Pro • iOS 18.0 (Happ 4.13.0)
    - 💻 Windows PC • Windows 11 (Happ 4.1.1)
    - 🤖 Android Device • Android (Happ 4.7.1)
    - 💻 Windows PC • Windows (v2raytun)
    """
    model = (model or "").strip()
    os_name = (os_name or "").strip()
    ua = (ua or "").strip()
    raw_title = (raw_title or "").strip()

    client_app = ""
    ua_lower = ua.lower()

    if "happ/" in ua_lower:
        m = re.search(r"Happ/([0-9.]+)", ua, re.IGNORECASE)
        client_app = f"Happ {m.group(1)}" if m else "Happ"
        if not os_name:
            if "/windows" in ua_lower:
                os_name = "Windows"
            elif "/ios" in ua_lower:
                os_name = "iOS"
            elif "/android" in ua_lower:
                os_name = "Android"
            elif "/macos" in ua_lower or "/darwin" in ua_lower:
                os_name = "macOS"
    elif "v2rayng/" in ua_lower:
        m = re.search(r"v2rayNG/([0-9.]+)", ua, re.IGNORECASE)
        client_app = f"v2rayNG {m.group(1)}" if m else "v2rayNG"
        if not os_name:
            os_name = "Android"
    elif "v2rayn" in ua_lower:
        client_app = "v2rayN"
        if not os_name:
            os_name = "Windows"
    elif "v2raytun" in ua_lower:
        client_app = "v2raytun"
        if not os_name:
            if "windows" in ua_lower:
                os_name = "Windows"
            elif "ios" in ua_lower:
                os_name = "iOS"
            elif "android" in ua_lower:
                os_name = "Android"
    elif "hiddify" in ua_lower:
        m = re.search(r"Hiddify/([0-9.]+)", ua, re.IGNORECASE)
        client_app = f"Hiddify {m.group(1)}" if m else "Hiddify"
    elif "streisand" in ua_lower:
        client_app = "Streisand"
        if not os_name:
            os_name = "iOS"
    elif "sing-box" in ua_lower:
        client_app = "sing-box"
    elif "shadowrocket" in ua_lower:
        client_app = "Shadowrocket"
        if not os_name:
            os_name = "iOS"
    elif "clash" in ua_lower:
        client_app = "Clash"

    if not client_app and raw_title:
        m = re.search(r"\(([^)]+)\)", raw_title)
        if m:
            client_app = m.group(1)

    hardware = ""
    icon = "📱"

    clean_model_key = model.split()[0] if model else ""
    if clean_model_key in APPLE_MODELS:
        hardware = APPLE_MODELS[clean_model_key]
        icon = "📱"
        if not os_name:
            os_name = "iOS"
    elif "iphone" in model.lower():
        hardware = model
        icon = "📱"
        if not os_name:
            os_name = "iOS"
    elif "ipad" in model.lower() or model.startswith("iPad"):
        hardware = "Apple iPad"
        icon = "📱"
        if not os_name:
            os_name = "iPadOS"
    elif "amethyst" in model.lower():
        if "mac" in os_name.lower() or "darwin" in os_name.lower():
            hardware = "Apple Mac"
            icon = "💻"
            os_name = os_name or "macOS"
        else:
            hardware = "Windows PC"
            icon = "💻"
            os_name = os_name or "Windows"
    elif model.startswith("SM-"):
        hardware = f"Samsung Galaxy ({model})"
        icon = "🤖"
        os_name = os_name or "Android"
    elif model.lower().startswith("pixel"):
        hardware = f"Google {model}"
        icon = "🤖"
        os_name = os_name or "Android"
    elif model:
        hardware = model
        if "windows" in os_name.lower() or "pc" in model.lower():
            icon = "💻"
        elif "android" in os_name.lower():
            icon = "🤖"
        elif "ios" in os_name.lower() or "iphone" in model.lower():
            icon = "📱"

    if not hardware:
        raw_clean = re.sub(r"^[^\w]+", "", raw_title).strip()
        raw_clean = re.sub(r"\(.*?\)", "", raw_clean).strip()
        if "v2rayng" in ua_lower or "android" in raw_clean.lower() or "android" in os_name.lower() or "android" in ua_lower:
            hardware = "Android Device"
            icon = "🤖"
            os_name = os_name or "Android"
        elif "windows" in raw_clean.lower() or "windows" in os_name.lower() or "windows" in ua_lower:
            hardware = "Windows PC"
            icon = "💻"
            os_name = os_name or "Windows"
        elif "mac" in raw_clean.lower() or "macos" in os_name.lower():
            hardware = "Apple Mac"
            icon = "💻"
            os_name = os_name or "macOS"
        elif "iphone" in raw_clean.lower() or "ios" in os_name.lower() or "ios" in ua_lower:
            hardware = "Apple iPhone"
            icon = "📱"
            os_name = os_name or "iOS"
        elif raw_clean:
            hardware = raw_clean
            icon = "🌐"
        else:
            hardware = "VPN Client"
            icon = "🌐"

    if not os_name:
        if hardware == "Windows PC":
            os_name = "Windows"
        elif hardware == "Android Device":
            os_name = "Android"
        elif hardware in ("Apple iPhone", "Apple iPad"):
            os_name = "iOS"
        elif hardware == "Apple Mac":
            os_name = "macOS"

    parts = []
    if os_name:
        parts.append(os_name)
    if client_app:
        parts.append(f"({client_app})")

    meta = " ".join(parts).strip()
    if meta:
        return f"{icon} {hardware} • {meta}"
    return f"{icon} {hardware}"


def format_time_ago(dt: datetime | None) -> str:
    """
    Форматирует время последней активности в относительном времени по MSK (UTC+3).
    """
    if not dt:
        return "Неизвестно"
    now_utc = datetime.utcnow()
    diff = now_utc - dt
    total_seconds = int(diff.total_seconds())

    msk_dt = dt + timedelta(hours=3)
    now_msk = now_utc + timedelta(hours=3)
    time_str = msk_dt.strftime("%H:%M MSK")

    if total_seconds < 60:
        return "только что"
    if total_seconds < 3600:
        mins = max(1, total_seconds // 60)
        return f"{mins} мин. назад"
    if msk_dt.date() == now_msk.date():
        return f"Сегодня, {time_str}"
    if msk_dt.date() == (now_msk - timedelta(days=1)).date():
        return f"Вчера, {time_str}"
    return msk_dt.strftime("%d.%m.%Y, %H:%M MSK")


def label_quality_score(label: str) -> int:
    """Оценка информативности бейджа устройства (модели, версии)."""
    score = 0
    l = (label or "").lower()
    if re.search(r"iphone\s*\d+", l):
        score += 25
    elif "apple iphone" in l:
        score += 5
    elif "samsung" in l or "pixel" in l:
        score += 20
    if re.search(r"\d+\.\d+", l):
        score += 10
    return score


def get_device_identity_key(dev_label: str, dev_fp: str = "", raw_ua: str = "") -> str:
    """
    Возвращает канонический идентификатор физического оборудования.
    Исключает дублирование одного и того же компьютера или телефона при смене дней,
    разнице в User-Agent (например, при синке с VPS и прямом запросе в Railway).
    """
    combined = f"{dev_label or ''} {dev_fp or ''} {raw_ua or ''}".lower()

    m_hwid = re.search(r"hwid[:/]([a-zA-Z0-9_-]+)", combined)
    if m_hwid:
        return f"hwid:{m_hwid.group(1)}"

    for h in ("2608211947507", "2606221809551"):
        if h in combined:
            return f"hwid:{h}"

    if "windows" in combined or " pc" in combined or "/windows" in combined:
        return "platform:windows_pc"
    if "iphone" in combined or "ios" in combined or "/ios" in combined:
        return "platform:apple_iphone"
    if "ipad" in combined or "ipados" in combined:
        return "platform:apple_ipad"
    if "android" in combined or "/android" in combined:
        return "platform:android_device"
    if "mac" in combined or "darwin" in combined or "/macos" in combined:
        return "platform:apple_mac"
    if "linux" in combined:
        return "platform:linux_pc"

    return f"label:{dev_label.strip()}"


async def deduplicate_user_devices(session: AsyncSession, sub_token: str | None = None):
    """
    Устраняет дублирование слотов устройств (одинаковые платформы/компьютеры/телефоны за разные дни),
    оставляя единственную наиболее точную запись с самым свежим временем активности.
    Также очищает случайные однократные тесты (sync_count <= 1 старше 2 часов) для ВСЕХ пользователей.
    """
    # 1. Системная очистка однократных тестов / случайных кликов (sync_count <= 1 старше PROVISIONAL_DEVICE_TTL_HOURS)
    provisional_cutoff = datetime.utcnow() - timedelta(hours=PROVISIONAL_DEVICE_TTL_HOURS)
    await session.execute(
        delete(UserDevice).where(
            (UserDevice.sync_count <= 1) | (UserDevice.sync_count.is_(None)),
            UserDevice.last_seen < provisional_cutoff
        )
    )

    # 2. TTL очистка неактивных устройств старше DEVICE_INACTIVE_TTL_DAYS (30 дней)
    cutoff_30d = datetime.utcnow() - timedelta(days=DEVICE_INACTIVE_TTL_DAYS)
    await session.execute(
        delete(UserDevice).where(UserDevice.last_seen < cutoff_30d)
    )

    admin_tokens = [u.get("sub_token") for u in KNOWN_FLEET_USERS if u.get("role") == "admin" or u.get("id") in ADMIN_IDS]
    admin_tokens = [t for t in admin_tokens if t]

    # 3. Жесткая зачистка паразитного админского айфона с других пользователей
    await session.execute(
        delete(UserDevice).where(
            ~UserDevice.sub_token.in_(admin_tokens),
            (
                UserDevice.device_label.like("%Apple iPhone%") |
                UserDevice.device_label.like("%iPhone%") |
                UserDevice.device_fp.like("%2606221809551%") |
                UserDevice.device_fp.like("%2608211947507%")
            )
        )
    )

    # 4. Выборка целевых устройств для дедупликации
    query = select(UserDevice)
    if sub_token:
        query = query.where(UserDevice.sub_token == sub_token)
    res = await session.execute(query)
    all_devs = res.scalars().all()

    # Группируем по (sub_token, canonical_identity_key)
    grouped: dict[tuple[str, str], list[UserDevice]] = defaultdict(list)
    for dev in all_devs:
        key = (dev.sub_token, get_device_identity_key(dev.device_label or "", dev.device_fp or ""))
        grouped[key].append(dev)

    for (tok, ident), dev_list in grouped.items():
        if len(dev_list) <= 1:
            continue

        # Выбираем лучший экземпляр: приоритет детальному бейджу ("iPhone 13" > "Apple iPhone") и свежему last_seen
        best_dev = max(
            dev_list,
            key=lambda d: (
                label_quality_score(d.device_label or ""),
                d.last_seen or datetime.min
            )
        )

        max_last_seen = max((d.last_seen for d in dev_list if d.last_seen), default=datetime.utcnow())
        min_first_seen = min((d.first_seen for d in dev_list if d.first_seen), default=datetime.utcnow())
        best_dev.last_seen = max_last_seen
        best_dev.first_seen = min_first_seen

        total_syncs = sum(getattr(d, "sync_count", 1) or 1 for d in dev_list)
        best_dev.sync_count = max(best_dev.sync_count or 1, total_syncs)

        # Сохраняем HWID в device_fp если он был в одном из дубликатов
        for d in dev_list:
            if d.id != best_dev.id and d.device_fp and "hwid:" in d.device_fp and "hwid:" not in (best_dev.device_fp or ""):
                best_dev.device_fp = d.device_fp

        # Удаляем остальные дублирующие записи
        for d in dev_list:
            if d.id != best_dev.id:
                await session.delete(d)

    await session.commit()


vps_telemetry_cache = {}


async def sync_upstream_telemetry():
    """
    Синхронизация телеметрии и реального счетчика обращений с сервером VPS.
    Берет точные данные из /admin/stats и сопоставляет с пользователями в Telegram.
    Защищено секретным токеном X-Novpn-Gateway-Secret.
    """
    global vps_telemetry_cache
    try:
        headers = {"X-Novpn-Gateway-Secret": UPSTREAM_GATEWAY_SECRET}
        async with aiohttp.ClientSession() as session:
            async with session.get(UPSTREAM_STATS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    vps_users = data.get("users", {})
                    vps_telemetry_cache = vps_users
                    async with AsyncSessionLocal() as session_db:
                        for token, v_data in vps_users.items():
                            v_fetch = v_data.get("fetch_count", 0)
                            res = await session_db.execute(select(User).where(User.sub_token == token))
                            u = res.scalars().first()
                            if u:
                                if (u.sub_sync_count or 0) < v_fetch:
                                    u.sub_sync_count = v_fetch

                                # Синхронизируем устройства пользователя с VPS
                                v_devices = v_data.get("devices", {})
                                is_user_admin = (u.role == "admin") or (u.telegram_id in ADMIN_IDS)
                                for dev_key, dev_info in v_devices.items():
                                    raw_ua = dev_info.get("ua", "")
                                    raw_title = dev_info.get("title", "")
                                    last_seen_str = dev_info.get("last_seen", "")
                                    dev_ip = dev_info.get("ip", "")

                                    # Отфильтровываем внутренний шлюз Railway и пустые записи
                                    if "NoVPN-Railway-Gateway" in raw_ua or "NoVPN-Railway-Gateway" in raw_title:
                                        continue
                                    if not raw_ua and not raw_title:
                                        continue

                                    # Защита: админские устройства не должны попадать к обычным пользователям
                                    if not is_user_admin:
                                        if dev_ip == "85.93.1.38" or "2606221809551" in raw_ua or "2608211947507" in raw_ua or "Happ/4.13.0/ios" in raw_ua:
                                            continue

                                    badge = parse_device_badge(model="", os_name="", ua=raw_ua, raw_title=raw_title)
                                    vps_fp = f"vps:{dev_key}:{hashlib.sha256(raw_ua.encode('utf-8')).hexdigest()[:8]}"
                                    target_ident = get_device_identity_key(badge, vps_fp, raw_ua)

                                    dt_last = None
                                    if last_seen_str:
                                        try:
                                            dt_last = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
                                        except Exception:
                                            dt_last = datetime.utcnow()
                                    else:
                                        dt_last = datetime.utcnow()

                                    # Ищем существующее устройство пользователя
                                    existing_res = await session_db.execute(
                                        select(UserDevice).where(UserDevice.sub_token == token)
                                    )
                                    u_devs = existing_res.scalars().all()
                                    dev_obj = None
                                    for d in u_devs:
                                        d_ident = get_device_identity_key(d.device_label or "", d.device_fp or "")
                                        if d_ident == target_ident or d.device_fp == vps_fp or (badge and d.device_label == badge):
                                            dev_obj = d
                                            break

                                    vps_sync_count = int(dev_info.get("sync_count", 1) or 1)
                                    if dev_obj:
                                        if label_quality_score(badge) > label_quality_score(dev_obj.device_label or ""):
                                            dev_obj.device_label = badge
                                        if dt_last and (not dev_obj.last_seen or dev_obj.last_seen < dt_last):
                                            dev_obj.last_seen = dt_last
                                        dev_obj.sync_count = max(getattr(dev_obj, "sync_count", 1) or 1, vps_sync_count)
                                    else:
                                        new_dev = UserDevice(
                                            user_id=u.id,
                                            sub_token=token,
                                            device_fp=vps_fp,
                                            device_label=badge,
                                            sync_count=vps_sync_count,
                                            first_seen=dt_last,
                                            last_seen=dt_last,
                                        )
                                        session_db.add(new_dev)
                        await session_db.commit()
                        await deduplicate_user_devices(session_db)
                    logger.info(f"[Telemetry] Синхронизирована телеметрия с VPS: {len(vps_users)} подписок.")
    except Exception as e:
        logger.warning(f"[Telemetry] Ошибка опроса статистики VPS: {e}")


async def sync_upstream_fleet():
    global current_fleet_nodes
    try:
        headers = {
            "User-Agent": "NoVPN-Railway-Gateway/2.0",
            "X-Novpn-Gateway-Secret": UPSTREAM_GATEWAY_SECRET
        }
        async with aiohttp.ClientSession() as session:
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
                        current_fleet_nodes = [format_banner_node(l.strip()) for l in lines if l.strip()]
                        logger.info(f"[Fleet] Обновлен флот с VPS: {len(current_fleet_nodes)} строк ({len(vless_nodes)} нод).")
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
        "• <code>/links</code> — готовые ссылки подписок для раздачи\n"
        "• <code>/users</code> — список пользователей и устройств\n"
        "• <code>/approve &lt;tg_id&gt;</code> — выдать доступ\n"
        "• <code>/ban &lt;tg_id&gt;</code> — заблокировать доступ\n"
        "• <code>/clear_devices &lt;tg_id&gt;</code> — сбросить слоты устройств"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="👥 Список пользователей", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔗 Ссылки подписок (раздача)", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu"))
    await callback.message.edit_text(text=stats_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    await callback.answer()


async def build_users_list_message(session: AsyncSession) -> str:
    """
    Формирует премиальный карточный интерфейс со структурированным деревом устройств (├, │, └).
    """
    await deduplicate_user_devices(session)
    res = await session.execute(select(User).order_by(User.sub_sync_count.desc(), User.id.asc()))
    users = res.scalars().all()

    header = "👥 <b>Действующие пользователи NoVPN:</b>\n"
    cards = []

    for u in users:
        is_admin_user = (u.role == "admin") or (u.telegram_id in ADMIN_IDS)
        status_tag = "👑 <i>ADMIN</i>" if is_admin_user else ("🟢 <i>АКТИВЕН</i>" if u.is_active == 1 else "⏳ <i>ОЖИДАНИЕ</i>")
        name = html.escape(u.first_name or u.username or "none")
        uname = f" (@{html.escape(u.username)})" if u.username else ""
        syncs = u.sub_sync_count or 0

        # Получаем зарегистрированные слоты устройств из базы данных
        dev_res = await session.execute(
            select(UserDevice).where(UserDevice.sub_token == u.sub_token).order_by(UserDevice.last_seen.desc())
        )
        u_devices = dev_res.scalars().all()

        user_quota = getattr(u, "max_devices", None) or DEFAULT_MAX_DEVICES
        quota_str = f"<b>{len(u_devices)} (Безлимит)</b>" if is_admin_user else f"<b>{len(u_devices)}/{user_quota}</b>"

        card_lines = [
            f"👤 <b>{name}</b>{uname} {status_tag}",
            f"   ├ 🆔 <code>{u.telegram_id}</code>",
            f"   ├ 🔄 Трафик: <b>{syncs} syncs</b>",
            f"   ├ 📱 Слоты: {quota_str}",
        ]

        if u_devices:
            for idx, dev in enumerate(u_devices):
                is_last_dev = (idx == len(u_devices) - 1)
                dev_prefix = "   └" if is_last_dev else "   ├"
                dev_sub_prefix = "      └" if is_last_dev else "   │  └"

                dev_title = html.escape(dev.device_label or "📱 Устройство")
                time_ago = format_time_ago(dev.last_seen)
                status_suffix = "" if (getattr(dev, "sync_count", 1) or 1) >= 2 else " ⏳ <i>(на проверке)</i>"
                card_lines.append(f"{dev_prefix} {dev_title}{status_suffix}")
                card_lines.append(f"{dev_sub_prefix} ⏱ <i>{time_ago}</i>")
        else:
            card_lines.append("   └ 💤 <i>Устройства еще не подключены</i>")

        cards.append("\n".join(card_lines))

    separator = "\n\n" + "─" * 28 + "\n\n"
    body = separator.join(cards)

    footer = (
        f"\n\n{'─' * 28}\n"
        f"📊 <b>Всего пользователей:</b> {len(users)}\n"
        f"⚙️ <i>Сброс слотов:</i> <code>/clear_devices &lt;id&gt;</code>\n"
        f"🚫 <i>Блокировка:</i> <code>/ban &lt;id&gt;</code>"
    )

    return f"{header}\n{body}{footer}"


@router.callback_query(F.data == "adm_list_users")
async def callback_admin_list_users(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    # Подтягиваем свежую телеметрию с VPS в реальном времени
    await sync_upstream_telemetry()

    async with AsyncSessionLocal() as session:
        text = await build_users_list_message(session)

    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔗 Получить ссылки подписок", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔄 Обновить список", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    except Exception:
        pass
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


@router.message(Command("clear_devices"))
async def cmd_clear_devices(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/clear_devices &lt;telegram_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == target_id))
        target_user = res.scalar_one_or_none()
        if not target_user:
            await message.answer(f"Пользователь с ID <code>{target_id}</code> не найден.", parse_mode=ParseMode.HTML)
            return

        target_token = target_user.sub_token
        await session.execute(
            delete(UserDevice).where(UserDevice.sub_token == target_token)
        )
        await session.commit()

        # Также сбрасываем устройства на апстрим сервере VPS
        try:
            async with aiohttp.ClientSession() as vps_sess:
                headers = {"X-Novpn-Gateway-Secret": UPSTREAM_GATEWAY_SECRET}
                await vps_sess.get(
                    f"https://sub.asdqwemary.de5.net:2053/admin/devices/{target_token}/clear",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=4)
                )
        except Exception as e:
            logger.warning(f"[Clear Devices] Ошибка сброса на VPS: {e}")

    uname = html.escape(target_user.first_name or target_user.username or 'Пользователь')
    await message.answer(
        f"✅ <b>Слоты устройств сброшены!</b>\n\n"
        f"Пользователь: <b>{uname}</b> (ID: <code>{target_id}</code>)\n"
        f"Теперь пользователь может заново привязать свои устройства (до {DEFAULT_MAX_DEVICES} слотов) без смены ссылки.",
        parse_mode=ParseMode.HTML
    )


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
        "• <code>/links</code> — готовые ссылки подписок для раздачи\n"
        "• <code>/users</code> — список пользователей и устройств\n"
        "• <code>/approve &lt;tg_id&gt;</code> — выдать доступ\n"
        "• <code>/ban &lt;tg_id&gt;</code> — заблокировать доступ\n"
        "• <code>/clear_devices &lt;tg_id&gt;</code> — сбросить слоты устройств"
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="👥 Список пользователей", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔗 Ссылки подписок (раздача)", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    await message.answer(text=stats_text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())


@router.message(Command("users"))
async def cmd_users(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await sync_upstream_telemetry()
    async with AsyncSessionLocal() as session:
        text = await build_users_list_message(session)

    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔗 Получить ссылки подписок", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔄 Обновить список", callback_data="adm_list_users"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    await message.answer(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)


def build_all_links_message(users: list[User]) -> str:
    lines = ["📋 <b>Все действующие ссылки подписок NoVPN:</b>\n"]
    for u in users:
        if not u.sub_token:
            continue
        u_name = html.escape(u.first_name or u.username or "none")
        u_handle = f" (@{html.escape(u.username)})" if u.username and u.username != "none" else ""
        sub_url = f"{APP_URL}/sub/{u.sub_token}"
        happ_url = f"happ://add/{sub_url}"
        is_admin = (u.role == "admin") or (u.telegram_id in ADMIN_IDS)
        role_label = " 👑 <i>ADMIN</i>" if is_admin else ""

        lines.append(
            f"👤 <b>{u_name}</b>{u_handle}{role_label} [ID: <code>{u.telegram_id}</code>]\n"
            f"🔗 <code>{sub_url}</code>\n"
            f"⚡ Happ 1-click: <code>{happ_url}</code>\n"
        )
    lines.append("────────────────────────────")
    lines.append("<i>💡 Нажмите на любую ссылку в рамке чтобы скопировать.</i>")
    return "\n".join(lines)


def build_single_user_link_card(user: User) -> str:
    u_name = html.escape(user.first_name or user.username or "none")
    u_handle = f" (@{html.escape(user.username)})" if user.username and user.username != "none" else ""
    sub_url = f"{APP_URL}/sub/{user.sub_token}"
    happ_url = f"happ://add/{sub_url}"
    is_admin = (user.role == "admin") or (user.telegram_id in ADMIN_IDS)
    role_label = " 👑 <i>ADMIN</i>" if is_admin else (" 🟢 <i>АКТИВЕН</i>" if user.is_active == 1 else " ⏳ <i>ОЖИДАНИЕ</i>")
    user_quota = getattr(user, "max_devices", None) or DEFAULT_MAX_DEVICES
    quota_str = "Безлимит" if is_admin else f"до {user_quota} устройств"

    return (
        f"🔑 <b>Подписка NoVPN • {u_name}</b>{u_handle}{role_label}\n"
        f"🆔 ID: <code>{user.telegram_id}</code> | Слоты: <b>{quota_str}</b>\n\n"
        f"🔗 <b>Универсальная ссылка подписки:</b>\n"
        f"<code>{sub_url}</code>\n\n"
        f"⚡ <b>Быстрый импорт в 1 клик (Happ):</b>\n"
        f"<a href=\"{happ_url}\">👉 Нажмите для добавления в Happ</a>\n"
        f"Код ссылки Happ: <code>{happ_url}</code>\n\n"
        f"✈️ <b>Telegram MTProto (без сторонних приложений):</b>\n"
        f"<a href=\"{MTPROTO_HTTP_LINK}\">👉 Подключить прокси в Telegram</a>\n\n"
        f"<i>📋 Инструкция для отправки пользователю:</i>\n"
        f"1. Установите приложение <b>Happ</b> (доступно в App Store и Google Play).\n"
        f"2. Откройте ссылку подписки в браузере или нажмите ссылку быстрого импорта.\n"
        f"3. Все 22 скоростных сервера подключатся автоматически."
    )


@router.message(Command("links"))
@router.message(Command("sublinks"))
async def cmd_admin_links(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.is_active == 1).order_by(User.id.asc()))
        users = res.scalars().all()

    kb = InlineKeyboardBuilder()
    for u in users:
        u_name = u.first_name or u.username or f"ID {u.telegram_id}"
        kb.row(types.InlineKeyboardButton(text=f"👤 {u_name}", callback_data=f"adm_link_user:{u.id}"))
    kb.row(types.InlineKeyboardButton(text="📋 Скопировать ВСЕ ссылки разом", callback_data="adm_links_all"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))

    text = (
        "🔗 <b>Панель раздачи подписок NoVPN</b>\n\n"
        "Выберите пользователя для получения готовой карточки с ссылкой и инструкцией, "
        "или нажмите <b>Скопировать ВСЕ ссылки</b> для общего списка:"
    )
    await message.answer(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.callback_query(F.data == "adm_sub_links")
async def callback_admin_sub_links(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.is_active == 1).order_by(User.id.asc()))
        users = res.scalars().all()

    kb = InlineKeyboardBuilder()
    for u in users:
        u_name = u.first_name or u.username or f"ID {u.telegram_id}"
        kb.row(types.InlineKeyboardButton(text=f"👤 {u_name}", callback_data=f"adm_link_user:{u.id}"))
    kb.row(types.InlineKeyboardButton(text="📋 Скопировать ВСЕ ссылки разом", callback_data="adm_links_all"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))

    text = (
        "🔗 <b>Панель раздачи подписок NoVPN</b>\n\n"
        "Выберите пользователя для получения готовой карточки с ссылкой и инструкцией, "
        "или нажмите <b>Скопировать ВСЕ ссылки</b> для общего списка:"
    )
    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "adm_links_all")
async def callback_admin_links_all(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.is_active == 1).order_by(User.id.asc()))
        users = res.scalars().all()

    text = build_all_links_message(users)
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="👤 Выбрать по пользователю", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_link_user:"))
async def callback_admin_link_user(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    user_db_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.id == user_db_id))
        user = res.scalar_one_or_none()

    if not user or not user.sub_token:
        await callback.answer("Пользователь не найден или токен отсутствует.", show_alert=True)
        return

    text = build_single_user_link_card(user)
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="✈️ MTProto прокси в Telegram", url=MTPROTO_HTTP_LINK))
    kb.row(types.InlineKeyboardButton(text="📱 Сбросить устройства пользователя", callback_data=f"adm_clear_dev_act:{user.telegram_id}"))
    kb.row(types.InlineKeyboardButton(text="🔙 К списку пользователей", callback_data="adm_sub_links"))
    kb.row(types.InlineKeyboardButton(text="🔙 В админку", callback_data="admin_dashboard"))
    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_clear_dev_act:"))
async def callback_admin_clear_device_act(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    target_tg_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == target_tg_id))
        target_user = res.scalar_one_or_none()
        if not target_user:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return

        target_token = target_user.sub_token
        await session.execute(
            delete(UserDevice).where(UserDevice.sub_token == target_token)
        )
        await session.commit()

        # Также сбрасываем устройства на апстрим сервере VPS
        try:
            async with aiohttp.ClientSession() as vps_sess:
                headers = {"X-Novpn-Gateway-Secret": UPSTREAM_GATEWAY_SECRET}
                await vps_sess.get(
                    f"https://sub.asdqwemary.de5.net:2053/admin/devices/{target_token}/clear",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=4)
                )
        except Exception as e:
            logger.warning(f"[Clear Devices] Ошибка сброса на VPS: {e}")

    await callback.answer(f"✅ Слоты устройств для ID {target_tg_id} успешно сброшены!", show_alert=True)


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

        old_token = user.sub_token
        new_token = generate_secure_token()
        user.sub_token = new_token
        user.last_seen_at = datetime.utcnow()
        if old_token:
            await session.execute(delete(UserDevice).where(UserDevice.sub_token == old_token))
        await session.commit()
        await session.refresh(user)
        logger.info(f"[Security] Пользователь ID={user.telegram_id} перевыпустил токен и сбросил привязки девайсов.")

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
        BotCommand(command="links", description="🔗 Ссылки подписок (раздача)"),
        BotCommand(command="users", description="👥 Список пользователей"),
        BotCommand(command="clear_devices", description="📱 Сбросить устройства пользователя"),
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


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)


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


async def check_and_register_device(
    user: User, token: str, request: Request, ua_lower: str, db: AsyncSession
) -> tuple[bool, str]:
    """
    Проверяет и регистрирует устройство с распознаванием оборудования, ОС и клиента.
    - Для администратора (user.role == 'admin' или telegram_id in ADMIN_IDS): ПОЛНЫЙ БЕЗЛИМИТ.
    - Для обычных пользователей: квота до DEFAULT_MAX_DEVICES (5) слотов.
    - TTL автоочистка: удаляет неактивные слоты старше DEVICE_INACTIVE_TTL_DAYS (30 дней).
    - Защита от превью/тестов: запросы админа и роботов предпросмотра не занимают слоты пользователей.
    Возвращает (is_allowed, device_label).
    """
    user_agent = request.headers.get("User-Agent", "").strip()
    if "NoVPN-Railway-Gateway" in user_agent:
        return True, "Gateway"

    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()
    admin_key = request.query_params.get("admin_key", "")
    is_admin_header = (request.headers.get("X-Novpn-Admin", "") == "true") or (admin_key == "novpn2026")
    is_admin_user = (user.role == "admin") or (user.telegram_id in ADMIN_IDS)
    is_admin_hwid = ("2606221809551" in user_agent) or ("2608211947507" in user_agent) or ("2606221809551" in ua_lower) or ("2608211947507" in ua_lower)
    is_admin_ip = (client_ip == "85.93.1.38")
    is_admin_probe = is_admin_header or is_admin_hwid or is_admin_ip

    # Если админ тестирует, открывает или предпросматривает чужую ссылку подписки — НЕ занимаем слот пользователя!
    if is_admin_probe and not is_admin_user:
        logger.info(f"[Admin Inspector] Запрос админа (IP={client_ip}) к ссылке пользователя {user.telegram_id}. Слот устройства не резервируется.")
        return True, "Admin Inspector"

    # Отфильтровываем поисковых/превью роботов (Telegram preview, Safari preview, crawler bots)
    preview_crawlers = ("telegrambot", "applebot", "preview", "crawler", "spider", "whatsapp", "meta-externalagent")
    if any(b in ua_lower for b in preview_crawlers):
        return True, "Preview Bot"

    hwid = request.headers.get("X-Hwid", "").strip()
    device_model = request.headers.get("X-Device-Model", "").strip()
    device_os = request.headers.get("X-Device-Os", "").strip()

    # Если hwid нет в заголовках, пробуем достать из UA Happ: Happ/x.x.x/platform/hwid
    if not hwid and "happ/" in ua_lower:
        parts_ua = user_agent.split("/")
        if len(parts_ua) >= 4 and parts_ua[3].strip():
            hwid = parts_ua[3].strip()

    label = parse_device_badge(model=device_model, os_name=device_os, ua=user_agent)

    if hwid:
        fp = f"hwid:{hwid}"
    else:
        clean_ua = ua_lower.split("/")[0][:32]
        fp = "client:" + hashlib.sha256(clean_ua.encode("utf-8")).hexdigest()[:16]

    # 1. TTL автоочистка: удаляем неактивные подтвержденные устройства старше 30 дней
    # и однократные пробы (sync_count <= 1) старше PROVISIONAL_DEVICE_TTL_HOURS (2 часов)
    provisional_cutoff = datetime.utcnow() - timedelta(hours=PROVISIONAL_DEVICE_TTL_HOURS)
    cutoff_30d = datetime.utcnow() - timedelta(days=DEVICE_INACTIVE_TTL_DAYS)
    await db.execute(
        delete(UserDevice).where(
            UserDevice.sub_token == token,
            (
                (UserDevice.last_seen < cutoff_30d) |
                (((UserDevice.sync_count <= 1) | (UserDevice.sync_count.is_(None))) & (UserDevice.last_seen < provisional_cutoff))
            )
        )
    )

    # 2. Интеллектуальный поиск существующего слота устройства
    target_ident = get_device_identity_key(label, fp, user_agent)
    dev_res = await db.execute(select(UserDevice).where(UserDevice.sub_token == token))
    existing_list = dev_res.scalars().all()

    matching_dev = None
    for d in existing_list:
        d_ident = get_device_identity_key(d.device_label or "", d.device_fp or "")
        if d_ident == target_ident or d.device_fp == fp or (label and d.device_label == label):
            matching_dev = d
            break

    if matching_dev:
        matching_dev.sync_count = (matching_dev.sync_count or 1) + 1
        matching_dev.last_seen = datetime.utcnow()
        if label and label_quality_score(label) >= label_quality_score(matching_dev.device_label or ""):
            matching_dev.device_label = label
        if hwid and "hwid:" not in (matching_dev.device_fp or ""):
            matching_dev.device_fp = fp
        await db.commit()
        return True, matching_dev.device_label

    # 3. Если устройство новое: проверяем квоту для не-админов
    if not is_admin_user:
        count_res = await db.execute(
            select(func.count(UserDevice.id)).where(
                UserDevice.sub_token == token,
                ~(
                    ((UserDevice.sync_count <= 1) | (UserDevice.sync_count.is_(None))) &
                    (UserDevice.last_seen < provisional_cutoff)
                )
            )
        )
        active_count = count_res.scalar_one() or 0
        user_max_devices = getattr(user, "max_devices", None) or DEFAULT_MAX_DEVICES
        if active_count >= user_max_devices:
            logger.warning(
                f"[Device Limit] Превышен лимит устройств для user_id={user.telegram_id} "
                f"({active_count}/{user_max_devices}). Запрос от '{label}' перенаправлен в Honeypot."
            )
            return False, label

    # 4. Регистрируем новое устройство
    new_dev = UserDevice(
        user_id=user.id,
        sub_token=token,
        device_fp=fp,
        device_label=label,
        sync_count=1,
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
    )
    db.add(new_dev)
    await db.commit()
    logger.info(f"[Device Added] Новый слот устройства (1-я синхронизация) для user_id={user.telegram_id}: '{label}'")
    return True, label


@app.get("/")
async def root():
    # Заглушка для healthcheck Railway (полная маскировка от сканеров)
    return {"status": "ok"}


sub_rate_limit_history = defaultdict(list)
SUB_RATE_LIMIT_MAX = 30  # максимум запросов
SUB_RATE_LIMIT_WINDOW = 60  # за 60 секунд


@app.get("/sub/{token}")
async def get_subscription(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Основной шлюз выдачи подписки.
    Только для активных (is_active=1) пользователей.
    Включает защиту Уровня 3 (Anti-Scanner & Honeypot Tarpit) и защиту от флуда.
    """
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()
    admin_key = request.query_params.get("admin_key", "")
    admin_auth = request.headers.get("X-Novpn-Admin", "") == "true" or admin_key == "novpn2026"

    # Защита от DoS/DDoS флуда на эндпоинт подписки
    if not admin_auth:
        now_ts = time.time()
        req_times = [t for t in sub_rate_limit_history[client_ip] if now_ts - t < SUB_RATE_LIMIT_WINDOW]
        if len(req_times) >= SUB_RATE_LIMIT_MAX:
            logger.warning(f"[Rate Limit] IP={client_ip} превысил лимит запросов к /sub/{token[:6]}***")
            return Response(content="Rate limit exceeded. Please wait a minute.\n", status_code=429)
        req_times.append(now_ts)
        sub_rate_limit_history[client_ip] = req_times

    user_res = await db.execute(select(User).where(User.sub_token == token))
    user = user_res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="Subscription token not found or revoked")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Subscription is suspended or pending approval")

    user_agent = request.headers.get("User-Agent", "").strip()
    ua_lower = user_agent.lower()
    accept_header = request.headers.get("Accept", "").lower()
    raw_param = request.query_params.get("raw", "")

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

    # 5. Квотирование устройств (Device Limiter & TTL)
    allowed, dev_label = await check_and_register_device(user, token, request, ua_lower, db)
    if not allowed:
        # Лимит устройств исчерпан: тихий слив в Honeypot (первые 5 девайсов продолжают работать)
        limit_decoy = [
            make_banner("🛡️ ─ [ novpn • device limit reached ] ─"),
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?security=none#%F0%9F%94%92%20novpn%20%E2%80%A2%20Device%20Limit%20Reached%20%28Max%205%29",
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:80?security=none#%E2%9A%A0%EF%B8%8F%20Contact%20Admin%20In%20Telegram",
            "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:8080?security=none#%F0%9F%93%B1%20Use%20Reset%20To%20Reassign%20Slots",
        ]
        raw_decoy = "\n".join(limit_decoy)
        decoy_encoded = base64.b64encode(raw_decoy.encode("utf-8")).decode("utf-8")
        headers = {
            "Profile-Title": "novpn-limit",
            "Profile-Update-Interval": "24",
            "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0",
            "Content-Disposition": 'attachment; filename="novpn.txt"; filename*=UTF-8\'\'novpn',
        }
        return Response(content=decoy_encoded, media_type="text/plain; charset=utf-8", headers=headers)

    # 6. Легитимная выдача подписки для официальных клиентов
    user.last_sub_sync = datetime.utcnow()
    user.sub_sync_count = (user.sub_sync_count or 0) + 1
    await db.commit()

    device_model = request.headers.get("X-Device-Model", "").strip()
    device_os = request.headers.get("X-Device-Os", "").strip()
    if device_model or device_os or user_agent:
        cached = vps_telemetry_cache.setdefault(token, {"devices": {}})
        parsed_b = parse_device_badge(model=device_model, os_name=device_os, ua=user_agent)
        cached["devices"][parsed_b] = {
            "title": parsed_b,
            "ua": user_agent,
            "last_seen": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        }

    raw_nodes = [format_banner_node(n) for n in (current_fleet_nodes if current_fleet_nodes else DEDICATED_NODES)]
    raw_payload = "\n".join(raw_nodes)
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
    user = user_res.scalars().first()
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
