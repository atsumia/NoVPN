import asyncio
import base64
import os
import urllib.parse
import uuid
from datetime import datetime

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import Depends, FastAPI, HTTPException, Response
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ==========================================
# 1. КОНФИГУРАЦИЯ И БАЗА ДАННЫХ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ==========================================
# 2. ПАРСЕР И ВАЛИДАТОР НОД (LIGHTWEIGHT)
# ==========================================
SOURCES = [
    "https://raw.githubusercontent.com/yebekhe/TelegramV2rayCollector/main/sub/normal/vless",
    "https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/Splitted/Vless.txt",
    "https://raw.githubusercontent.com/freefq/free/master/v2",
]


def parse_proxy_uri(uri: str) -> dict | None:
    uri = uri.strip()
    try:
        parsed = urllib.parse.urlparse(uri)
        scheme = parsed.scheme.lower()
        if scheme not in ("vless", "hy2", "hysteria2"):
            return None

        host = parsed.hostname
        port = parsed.port or (443 if scheme == "vless" else 80)
        query = urllib.parse.parse_qs(parsed.query)
        security = query.get("security", [""])[0]

        if scheme == "vless" and security not in ("reality", "tls"):
            return None

        return {"uri": uri, "scheme": scheme, "host": host, "port": port}
    except Exception:
        return None


async def check_node_tcp(host: str, port: int, timeout: float = 2.0) -> int | None:
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
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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


async def update_proxies_task():
    async with aiohttp.ClientSession() as http_session:
        raw_uris = []
        for url in SOURCES:
            lines = await fetch_feed(http_session, url)
            raw_uris.extend(lines)

    unique_candidates = {}
    for line in raw_uris:
        meta = parse_proxy_uri(line)
        if meta and meta["host"]:
            unique_candidates[meta["uri"]] = meta

    valid_servers = []
    candidates_list = list(unique_candidates.values())[:80]

    for i in range(0, len(candidates_list), 10):
        chunk = candidates_list[i : i + 10]
        tasks = [check_node_tcp(item["host"], item["port"]) for item in chunk]
        results = await asyncio.gather(*tasks)

        for meta, ping in zip(chunk, results):
            if ping is not None:
                valid_servers.append((meta, ping))

    if not valid_servers:
        return

    valid_servers.sort(key=lambda x: x[1])
    top_servers = valid_servers[:25]

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


# ==========================================
# 3. FASTAPI СЕРВЕР ПОДПИСОК
# ==========================================
app = FastAPI(title="VPN Subscription Gateway")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@app.get("/")
async def root():
    return {"status": "running"}


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
        .limit(15)
    )
    servers = servers_res.scalars().all()

    if not servers:
        return Response(content="", media_type="text/plain")

    raw_payload = "\n".join([srv.uri for srv in servers])
    encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")

    headers = {
        "Subscription-Userinfo": "upload=0; download=0; total=107374182400; expire=0",
        "profile-update-interval": "2",
        "content-disposition": "inline; filename=proxies.txt",
    }
    return Response(content=encoded, media_type="text/plain; charset=utf-8", headers=headers)


# ==========================================
# 4. TELEGRAM БОТ (AIOGRAM 3)
# ==========================================
router = Router()


def get_menu(token: str):
    kb = InlineKeyboardBuilder()
    sub_url = f"{APP_URL}/sub/{token}"
    kb.row(types.InlineKeyboardButton(text="⚡ Hiddify (1-клик)", url=f"hiddify://install-sub?url={sub_url}"))
    kb.row(types.InlineKeyboardButton(text="📱 v2rayNG (1-клик)", url=f"v2rayng://install-sub?url={sub_url}"))
    kb.row(types.InlineKeyboardButton(text="📋 Скопировать ссылку", callback_data="get_link"))
    return kb.as_markup()


@router.message(CommandStart())
async def start_handler(message: types.Message):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=message.from_user.id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

    sub_link = f"{APP_URL}/sub/{user.sub_token}"
    msg = (
        "🛡 **Персональный VPN-шлюз**\n\n"
        "Конфигурации серверов отбираются по протоколам VLESS Reality и Hysteria 2 "
        "и обновляются каждые 30 минут.\n\n"
        f"**Ваша ссылка подписки:**\n`{sub_link}`"
    )
    await message.answer(msg, parse_mode="Markdown", reply_markup=get_menu(user.sub_token))


@router.callback_query(lambda c: c.data == "get_link")
async def send_link(callback: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
        if user:
            sub_url = f"{APP_URL}/sub/{user.sub_token}"
            await callback.message.answer(
                f"Ссылка подписки (нажмите для копирования):\n`{sub_url}`",
                parse_mode="Markdown",
            )
    await callback.answer()


# ==========================================
# 5. ТОЧКА ВХОДА
# ==========================================
async def cron_loop():
    while True:
        try:
            await update_proxies_task()
        except Exception as e:
            print(f"[Collector Error] {e}")
        await asyncio.sleep(1800)


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in environment variables.")

    await init_db()
    asyncio.create_task(cron_loop())

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    server_config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(server_config)

    await asyncio.gather(
        dp.start_polling(bot),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())