import asyncio
import base64
import html
import logging
import os
import re
import urllib.parse
import uuid
from datetime import datetime
import ipaddress

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NoVPN")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
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
    ping_ms = Column(Integer, default=999)
    is_alive = Column(Boolean, default=True)
    last_checked = Column(DateTime, default=datetime.utcnow)


async def init_db():
    logger.info("[DB] Инициализация структуры базы данных...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[DB] База данных готова.")


# Базы, созданные специально для обхода ТСПУ и белых списков в РФ
PRIORITY_RU_SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-all.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
]

SECONDARY_SOURCES = [
    "https://raw.githubusercontent.com/yaney01/telegram-collector/main/protocols/reality",
    "https://raw.githubusercontent.com/soroushmirzaei/telegram-configs-collector/main/protocols/reality",
    "https://raw.githubusercontent.com/soroushmirzaei/telegram-configs-collector/main/protocols/hysteria2",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Splitted-By-Protocol/reality.txt",
]

TELEGRAM_CHANNELS = [
    "igareq",
    "vpn_free_russia",
    "VLESS_REALITY",
    "DirectVPN",
    "vless_configs",
    "reality_free",
    "v2ray_outlinefree",
    "vpn_fail_ru",
]

RU_WHITE_DOMAINS = (
    "yandex.ru",
    "ya.ru",
    "vk.com",
    "vk.ru",
    "mail.ru",
    "dzen.ru",
    "gosuslugi.ru",
    "ozon.ru",
    "wildberries.ru",
    "tinkoff.ru",
    "sberbank.ru",
    "kinopoisk.ru",
    "rutube.ru",
    "rambler.ru",
    "wb.ru",
    "2gis.ru",
)

BLOCKED_IP_PREFIXES = (
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.",
    "104.22.", "104.23.", "104.24.", "104.25.", "104.26.", "104.27.",
    "104.28.", "172.64.", "172.65.", "172.66.", "172.67.",
    "151.101.",
)


def parse_proxy_uri(uri: str, is_ru_priority_source: bool = False) -> dict | None:
    uri = uri.strip()
    if not uri or uri.startswith("#"):
        return None
    try:
        parsed = urllib.parse.urlparse(uri)
        scheme = parsed.scheme.lower()

        # Разрешаем только протоколы, способные пробить ТСПУ в РФ
        if scheme not in ("vless", "hy2", "hysteria2"):
            return None

        host = parsed.hostname
        if not host:
            return None

        # Отсекаем заблокированные в РФ пулы Cloudflare Anycast и Fastly
        if any(host.startswith(p) for p in BLOCKED_IP_PREFIXES):
            return None

        query_params = urllib.parse.parse_qs(parsed.query)
        port = parsed.port or 443

        sni = query_params.get("sni", [""])[0].lower()
        has_ru_sni = any(d in sni for d in RU_WHITE_DOMAINS) or sni.endswith(".ru")

        if scheme == "vless":
            security = query_params.get("security", [""])[0].lower()
            pbk = query_params.get("pbk", [""])[0]
            flow = query_params.get("flow", [""])[0].lower()
            net_type = query_params.get("type", ["tcp"])[0].lower()

            # Исключаем устаревшие WebSocket/gRPC без Reality
            if security != "reality" and not pbk and "vision" not in flow:
                return None

        return {
            "uri": uri,
            "scheme": scheme,
            "host": host,
            "port": port,
            "sni": sni,
            "has_ru_sni": has_ru_sni,
            "is_ru_priority": is_ru_priority_source,
        }
    except Exception:
        return None


async def check_node_tcp(host: str, port: int, timeout: float = 2.5) -> int | None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        conn = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(conn, timeout=timeout)
        latency = int((loop.time() - start) * 1000)
        writer.close()
        await writer.wait_closed()
        return latency
    except Exception:
        return None


async def fetch_feed(session: aiohttp.ClientSession, url: str) -> list[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            try:
                decoded = base64.b64decode(text.strip()).decode("utf-8", errors="ignore")
                return decoded.splitlines()
            except Exception:
                return text.splitlines()
    except Exception:
        return []


async def fetch_telegram_channel(session: aiohttp.ClientSession, channel: str) -> list[str]:
    url = f"https://t.me/s/{channel}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            body = html.unescape(await resp.text())
            uris = re.findall(r"(?:vless|hy2|hysteria2)://[^\s<\"'#]+", body)
            return uris
    except Exception:
        return []


async def update_proxies_task():
    logger.info("[Collector] Сбор серверов из баз обхода блокировок РФ...")
    candidates = {}

    async with aiohttp.ClientSession() as session:
        ru_tasks = [fetch_feed(session, url) for url in PRIORITY_RU_SOURCES]
        ru_results = await asyncio.gather(*ru_tasks, return_exceptions=True)
        for res in ru_results:
            if isinstance(res, list):
                for line in res:
                    meta = parse_proxy_uri(line, is_ru_priority_source=True)
                    if meta and meta["uri"] not in candidates:
                        candidates[meta["uri"]] = meta

        sec_tasks = [fetch_feed(session, url) for url in SECONDARY_SOURCES]
        tg_tasks = [fetch_telegram_channel(session, ch) for ch in TELEGRAM_CHANNELS]
        other_results = await asyncio.gather(*sec_tasks, *tg_tasks, return_exceptions=True)
        for res in other_results:
            if isinstance(res, list):
                for line in res:
                    meta = parse_proxy_uri(line, is_ru_priority_source=False)
                    if meta and meta["uri"] not in candidates:
                        candidates[meta["uri"]] = meta

    candidates_list = list(candidates.values())
    logger.info(f"[Collector] Отфильтровано {len(candidates_list)} узлов без Cloudflare/Trojan.")

    if not candidates_list:
        logger.warning("[Collector] Новые узлы не получены, сохраняем существующую базу.")
        return

    check_pool = candidates_list[:200]
    valid_servers = []

    for i in range(0, len(check_pool), 30):
        chunk = check_pool[i : i + 30]
        ping_tasks = [check_node_tcp(item["host"], item["port"]) for item in chunk]
        results = await asyncio.gather(*ping_tasks)

        for meta, ping in zip(chunk, results):
            if ping is not None:
                score = ping
                if meta["is_ru_priority"]:
                    score -= 300
                if meta["has_ru_sni"]:
                    score -= 200
                valid_servers.append((meta, ping, score))

    if valid_servers:
        valid_servers.sort(key=lambda x: x[2])
        top_servers = [(item[0], item[1]) for item in valid_servers[:45]]
    else:
        top_servers = [(item, 90) for item in candidates_list[:35]]

    async with AsyncSessionLocal() as db_session:
        await db_session.execute(delete(ProxyServer))
        for meta, ping in top_servers:
            srv = ProxyServer(
                uri=meta["uri"],
                protocol=meta["scheme"],
                ping_ms=ping,
                is_alive=True,
                last_checked=datetime.utcnow(),
            )
            db_session.add(srv)
        await db_session.commit()
    logger.info(f"[Collector] База обновлена: сохранено {len(top_servers)} узлов для РФ.")


app = FastAPI(title="NoVPN Gateway")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@app.get("/")
async def root():
    return {"status": "online", "service": "NoVPN"}


@app.get("/sub/{token}")
async def get_subscription(token: str, db: AsyncSession = Depends(get_db)):
    user_res = await db.execute(select(User).where(User.sub_token == token))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Token not found")

    servers_res = await db.execute(
        select(ProxyServer)
        .where(ProxyServer.is_alive == True)
        .order_by(ProxyServer.ping_ms.asc())
        .limit(40)
    )
    servers = servers_res.scalars().all()

    if not servers:
        asyncio.create_task(update_proxies_task())
        return Response(content="", media_type="text/plain")

    raw_payload = "\n".join([srv.uri for srv in servers])
    encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")

    encoded_title = base64.b64encode("NoVPN Free".encode()).decode()
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
    if client == "happ":
        target = f"happ://add/{sub_url}"
    elif client == "hiddify":
        target = f"hiddify://install-sub?url={sub_url}"
    else:
        target = sub_url

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Подключение NoVPN</title>
    <meta http-equiv="refresh" content="0; url={target}">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
  </head>
  <body style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px 20px; background: #0f172a; color: #f8fafc;">
    <h2>Открытие в приложении...</h2>
    <p style="color: #94a3b8; font-size: 14px;">Если приложение не открылось автоматически:</p>
    <p><a href="{target}" style="color: #38bdf8; text-decoration: none; font-weight: bold; font-size: 16px;">👉 Нажмите сюда, чтобы открыть</a></p>
  </body>
</html>"""
    return HTMLResponse(content=html_content)


router = Router()


def get_menu(token: str):
    kb = InlineKeyboardBuilder()
    happ_url = f"{APP_URL}/open/happ?token={token}"
    hiddify_url = f"{APP_URL}/open/hiddify?token={token}"

    kb.row(types.InlineKeyboardButton(text="🚀 Добавить в Happ (1-клик)", url=happ_url))
    kb.row(types.InlineKeyboardButton(text="⚡ Добавить в Hiddify", url=hiddify_url))
    return kb.as_markup()


@router.message(CommandStart())
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    logger.info(f"[Bot] Запрос /start от {user_id}")

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == user_id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=user_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    msg = (
        f"👋 <b>Добро пожаловать в NoVPN!</b>\n\n"
        f"Ваша персональная подписка (VLESS Reality & Hysteria 2).\n\n"
        f"🔗 <b>Ссылка на подписку</b> (нажмите для копирования):\n"
        f"<code>{sub_link}</code>\n\n"
        f"<i>💡 Для быстрого импорта нажмите кнопку ниже:</i>"
    )

    await message.answer(
        text=msg,
        parse_mode=ParseMode.HTML,
        reply_markup=get_menu(user.sub_token),
        disable_web_page_preview=True,
    )


async def cron_loop():
    while True:
        try:
            await update_proxies_task()
        except Exception as e:
            logger.error(f"[Collector Error] {e}", exc_info=True)
        await asyncio.sleep(1800)


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
        raise ValueError("BOT_TOKEN is not set")

    await init_db()
    asyncio.create_task(cron_loop())

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("[Bot] Webhook сброшен, запуск...")

    server_config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(server_config)

    while True:
        try:
            await asyncio.gather(
                dp.start_polling(bot),
                server.serve(),
            )
            break
        except TelegramConflictError:
            logger.warning("[Bot] Временный конфликт сессий при перезапуске Render. Ожидание 5 сек...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())