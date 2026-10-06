import asyncio
import base64
import html
import logging
import os
import re
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NoVPN")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Домен на Railway
RAW_APP_URL = os.getenv("APP_URL", "https://novpn-production.up.railway.app").strip().rstrip("/")
if not RAW_APP_URL.startswith("http://") and not RAW_APP_URL.startswith("https://"):
    APP_URL = f"https://{RAW_APP_URL}"
else:
    APP_URL = RAW_APP_URL

PORT = int(os.getenv("PORT", 8000))

# Поддержка SQLite по умолчанию (для Railway без внешних баз) или PostgreSQL
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


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    sub_token = Column(String, unique=True, default=lambda: uuid.uuid4().hex)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProxyServer(Base):
    __tablename__ = "proxy_servers"
    id = Column(Integer, primary_key=True)
    uri = Column(String, unique=True)
    protocol = Column(String)
    tier = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    logger.info("[DB] Инициализация базы данных...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "postgresql" in DATABASE_URL:
            try:
                await conn.execute(text("ALTER TABLE proxy_servers ADD COLUMN IF NOT EXISTS tier INTEGER DEFAULT 3;"))
            except Exception:
                pass
    logger.info("[DB] База данных готова.")


# Базы обхода белых списков РФ
RU_WHITELIST_SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-SNI-RU-all.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-checked.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-all.txt",
]

RU_FALLBACK_SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
]

TELEGRAM_CHANNELS = [
    "igareq",
    "vpn_free_russia",
    "VLESS_REALITY",
    "vless_configs",
    "reality_free",
    "vpn_fail_ru",
]

RU_WHITE_DOMAINS = (
    "yandex.ru", "ya.ru", "vk.com", "vk.ru", "mail.ru", "dzen.ru",
    "gosuslugi.ru", "ozon.ru", "wildberries.ru", "wb.ru", "tinkoff.ru",
    "tbank.ru", "sberbank.ru", "sber.ru", "kinopoisk.ru", "rutube.ru",
    "rambler.ru", "2gis.ru", "megafon.ru", "mts.ru", "beeline.ru",
    "t2.ru", "tele2.ru", "avito.ru",
)

BLOCKED_IP_PREFIXES = (
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.",
    "104.22.", "104.23.", "104.24.", "104.25.", "104.26.", "104.27.",
    "104.28.", "104.29.", "104.30.", "104.31.", "172.64.", "172.65.",
    "172.66.", "172.67.", "151.101.",
)


def parse_proxy_uri(uri: str, source_type: str = "other") -> dict | None:
    uri = uri.strip()
    if not uri or uri.startswith("#"):
        return None
    try:
        parsed = urllib.parse.urlparse(uri)
        scheme = parsed.scheme.lower()
        if scheme not in ("vless", "hy2", "hysteria2"):
            return None

        host = parsed.hostname
        if not host or any(host.startswith(p) for p in BLOCKED_IP_PREFIXES):
            return None

        query_params = urllib.parse.parse_qs(parsed.query)
        net_type = query_params.get("type", ["tcp"])[0].lower()
        sni = query_params.get("sni", [""])[0].lower()
        is_ru_sni = any(d in sni for d in RU_WHITE_DOMAINS) or sni.endswith(".ru")

        if scheme == "vless":
            security = query_params.get("security", [""])[0].lower()
            pbk = query_params.get("pbk", [""])[0]
            if security != "reality" or not pbk:
                return None

        if source_type == "whitelist" or is_ru_sni:
            tier = 1
        elif source_type == "ru_fallback":
            tier = 2
        else:
            tier = 3

        return {
            "uri": uri,
            "scheme": scheme,
            "net_type": net_type,
            "tier": tier,
        }
    except Exception:
        return None


async def fetch_feed(session: aiohttp.ClientSession, url: str) -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text_data = await resp.text()
            try:
                decoded = base64.b64decode(text_data.strip()).decode("utf-8", errors="ignore")
                return decoded.splitlines()
            except Exception:
                return text_data.splitlines()
    except Exception:
        return []


async def fetch_telegram_channel(session: aiohttp.ClientSession, channel: str) -> list[str]:
    url = f"https://t.me/s/{channel}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            body = html.unescape(await resp.text())
            return re.findall(r"(?:vless|hy2|hysteria2)://[^\s<\"'#]+", body)
    except Exception:
        return []


async def update_proxies_task():
    logger.info("[Collector] Сбор серверов...")
    candidates = {}

    async with aiohttp.ClientSession() as session:
        wl_tasks = [fetch_feed(session, url) for url in RU_WHITELIST_SOURCES]
        for res in await asyncio.gather(*wl_tasks, return_exceptions=True):
            if isinstance(res, list):
                for line in res:
                    meta = parse_proxy_uri(line, source_type="whitelist")
                    if meta and meta["uri"] not in candidates:
                        candidates[meta["uri"]] = meta

        fb_tasks = [fetch_feed(session, url) for url in RU_FALLBACK_SOURCES]
        for res in await asyncio.gather(*fb_tasks, return_exceptions=True):
            if isinstance(res, list):
                for line in res:
                    meta = parse_proxy_uri(line, source_type="ru_fallback")
                    if meta and meta["uri"] not in candidates:
                        candidates[meta["uri"]] = meta

        tg_tasks = [fetch_telegram_channel(session, ch) for ch in TELEGRAM_CHANNELS]
        for res in await asyncio.gather(*tg_tasks, return_exceptions=True):
            if isinstance(res, list):
                for line in res:
                    meta = parse_proxy_uri(line, source_type="tg")
                    if meta and meta["uri"] not in candidates:
                        candidates[meta["uri"]] = meta

    candidates_list = list(candidates.values())
    if not candidates_list:
        logger.warning("[Collector] Новые узлы не получены, оставляем базу.")
        return

    candidates_list.sort(key=lambda x: (x["tier"], 0 if x["net_type"] == "tcp" else 1))
    top_servers = candidates_list[:45]

    async with AsyncSessionLocal() as db_session:
        await db_session.execute(delete(ProxyServer))
        for item in top_servers:
            srv = ProxyServer(uri=item["uri"], protocol=item["scheme"], tier=item["tier"])
            db_session.add(srv)
        await db_session.commit()

    logger.info(f"[Collector] Успешно сохранено {len(top_servers)} серверов.")


router = Router()


@router.message(CommandStart())
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=user_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🚀 Добавить в Happ (1-клик)", url=f"{APP_URL}/open/happ?token={user.sub_token}"))
    kb.row(types.InlineKeyboardButton(text="⚡ Добавить в Hiddify", url=f"{APP_URL}/open/hiddify?token={user.sub_token}"))

    msg = (
        f"👋 <b>Добро пожаловать в NoVPN!</b>\n\n"
        f"Подписка оптимизирована для мобильных сетей РФ (обход белых списков).\n\n"
        f"🔗 <b>Ссылка на подписку</b>:\n"
        f"<code>{sub_link}</code>\n\n"
        f"💡 <b>Нажмите кнопку ниже для импорта:</b>"
    )
    await message.answer(text=msg, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup(), disable_web_page_preview=True)


async def cron_loop():
    while True:
        try:
            await update_proxies_task()
        except Exception as e:
            logger.error(f"[Collector Error] {e}")
        await asyncio.sleep(1800)


async def bot_polling_loop():
    if not BOT_TOKEN:
        logger.error("[Bot] BOT_TOKEN не задан в переменных окружения!")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("[Bot] Telegram-бот запущен и ожидает сообщений...")

    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramConflictError:
            logger.warning("[Bot] Конфликт сессий, повтор через 5 секунд...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"[Bot Error] {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск фоновых процессов при старте сервера FastAPI
    await init_db()
    c_task = asyncio.create_task(cron_loop())
    b_task = asyncio.create_task(bot_polling_loop())
    yield
    c_task.cancel()
    b_task.cancel()


app = FastAPI(title="NoVPN Gateway", lifespan=lifespan)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@app.get("/")
async def root():
    return {"status": "online", "service": "NoVPN", "app_url": APP_URL}


@app.get("/sub/{token}")
async def get_subscription(token: str, db: AsyncSession = Depends(get_db)):
    user_res = await db.execute(select(User).where(User.sub_token == token))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Token not found")

    servers_res = await db.execute(
        select(ProxyServer).order_by(ProxyServer.tier.asc()).limit(45)
    )
    servers = servers_res.scalars().all()

    if not servers:
        asyncio.create_task(update_proxies_task())
        return Response(content="", media_type="text/plain")

    raw_payload = "\n".join([srv.uri for srv in servers])
    encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")

    encoded_title = base64.b64encode("NoVPN WhiteList".encode()).decode()
    headers = {
        "profile-title": f"base64:{encoded_title}",
        "Subscription-Userinfo": "upload=0; download=0; total=107374182400; expire=0",
        "profile-update-interval": "1",
        "content-disposition": "attachment; filename=NoVPN.txt",
    }
    return Response(content=encoded, media_type="text/plain; charset=utf-8", headers=headers)


@app.get("/open/{client}")
async def redirect_to_client(client: str, token: str):
    sub_url = f"{APP_URL}/sub/{token}"
    target = f"happ://add/{sub_url}" if client == "happ" else f"hiddify://install-sub?url={sub_url}"

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>NoVPN Connect</title>
    <meta http-equiv="refresh" content="0; url={target}">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
  </head>
  <body style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px 20px; background: #0f172a; color: #f8fafc;">
    <h2>Открытие приложения Happ...</h2>
    <p><a href="{target}" style="color: #38bdf8; font-size: 16px; font-weight: bold;">👉 Нажмите сюда для импорта</a></p>
  </body>
</html>"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    server_config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(server_config)
    asyncio.run(server.serve())