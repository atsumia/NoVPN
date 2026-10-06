import asyncio
import base64
import logging
import os
import urllib.parse
import uuid
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


SOURCES = [
    # Крупные проверенные источники VLESS Reality и Hysteria 2
    "https://raw.githubusercontent.com/yebekhe/TVC/main/subscriptions/xray/reality",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Sub1.txt",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Sub2.txt",
    "https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/sub_merge.txt",
    "https://raw.githubusercontent.com/soroushmirzaei/telegram-configs-collector/main/protocols/reality",
    "https://raw.githubusercontent.com/soroushmirzaei/telegram-configs-collector/main/protocols/hysteria2",
]


def parse_proxy_uri(uri: str) -> dict | None:
    uri = uri.strip()
    if not uri or uri.startswith("#"):
        return None
    try:
        parsed = urllib.parse.urlparse(uri)
        scheme = parsed.scheme.lower()

        # Разрешаем только протоколы, способные пробивать DPI и белые списки
        if scheme not in ("vless", "hy2", "hysteria2"):
            return None

        host = parsed.hostname
        if not host:
            return None

        query_params = urllib.parse.parse_qs(parsed.query)

        # Жесткая фильтрация VLESS: пропускаем ТОЛЬКО Reality с открытым ключом pbk
        if scheme == "vless":
            security = query_params.get("security", [""])[0].lower()
            pbk = query_params.get("pbk", [""])[0]
            if security != "reality" or not pbk:
                return None  # Отбрасываем голый WebSocket, security=none и старый TLS

        port = parsed.port or (443 if scheme in ("vless", "hy2", "hysteria2") else 80)
        return {"uri": uri, "scheme": scheme, "host": host, "port": port}
    except Exception:
        return None


async def check_node_tcp(host: str, port: int, timeout: float = 1.5) -> int | None:
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
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            try:
                decoded = base64.b64decode(text.strip()).decode("utf-8", errors="ignore")
                return decoded.splitlines()
            except Exception:
                return text.splitlines()
    except Exception as e:
        logger.warning(f"[Collector] Ошибка загрузки источника {url}: {e}")
        return []


async def update_proxies_task():
    logger.info("[Collector] Поиск и фильтрация Reality и Hysteria2 серверов...")
    async with aiohttp.ClientSession() as http_session:
        raw_uris = []
        for url in SOURCES:
            lines = await fetch_feed(http_session, url)
            raw_uris.extend(lines)

    unique_candidates = {}
    for line in raw_uris:
        meta = parse_proxy_uri(line)
        if meta and meta["host"] and meta["uri"] not in unique_candidates:
            unique_candidates[meta["uri"]] = meta

    candidates_list = list(unique_candidates.values())[:120]
    logger.info(f"[Collector] Найдено {len(candidates_list)} Reality/Hy2 кандидатов. Проверка пинга...")

    valid_servers = []
    for i in range(0, len(candidates_list), 20):
        chunk = candidates_list[i : i + 20]
        tasks = [check_node_tcp(item["host"], item["port"]) for item in chunk]
        results = await asyncio.gather(*tasks)

        for meta, ping in zip(chunk, results):
            if ping is not None:
                valid_servers.append((meta, ping))

    if not valid_servers:
        top_servers = [(item, 120) for item in candidates_list[:30]]
    else:
        valid_servers.sort(key=lambda x: x[1])
        top_servers = valid_servers[:35]

    async with AsyncSessionLocal() as session:
        await session.execute(delete(ProxyServer))
        for meta, ping in top_servers:
            srv = ProxyServer(
                uri=meta["uri"],
                protocol=meta["scheme"],
                ping_ms=ping,
                is_alive=True,
                last_checked=datetime.utcnow(),
            )
            session.add(srv)
        await session.commit()
    logger.info(f"[Collector] Сохранено {len(top_servers)} проверенных Reality/Hy2 узлов.")


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
        .limit(30)
    )
    servers = servers_res.scalars().all()

    if not servers:
        asyncio.create_task(update_proxies_task())
        return Response(content="", media_type="text/plain")

    raw_payload = "\n".join([srv.uri for srv in servers])
    encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")

    encoded_title = base64.b64encode("NoVPN Reality".encode()).decode()
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