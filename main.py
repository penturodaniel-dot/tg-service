"""
TG Account Service — аналог whatsapp_service но для Telegram аккаунта
Использует Telethon (MTProto) для работы с реальным аккаунтом (не ботом)

API:
  GET  /status              — статус подключения
  POST /auth/send_code      — отправить SMS код на номер
  POST /auth/sign_in        — войти по коду (+ пароль 2FA если нужен)
  POST /auth/sign_out       — выйти (сбросить сессию)
  POST /send                — отправить текстовое сообщение
  POST /send_media          — отправить медиафайл (base64)
  GET  /contact/{user_id}   — получить инфо о контакте
  GET  /health              — health check
"""

import os
import logging
import asyncio
import base64
import io
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PasswordHashInvalidError, PhoneNumberBannedError,
    FloodWaitError
)
from telethon.tl.types import (
    InputPeerUser, User, MessageMediaPhoto, MessageMediaDocument,
    UserStatusOnline, UserStatusOffline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Конфиг ───────────────────────────────────────────────────────────────────
# API_ID и API_HASH могут приходить из env ИЛИ из запроса (из СРМки)
_API_ID_ENV   = int(os.getenv("TG_API_ID", "0"))
_API_HASH_ENV = os.getenv("TG_API_HASH", "")
API_SECRET  = os.getenv("API_SECRET", "changeme")
MAIN_APP    = os.getenv("MAIN_APP_URL", "").rstrip("/")
MAIN_SECRET = os.getenv("WA_WEBHOOK_SECRET", "changeme")
PORT        = int(os.getenv("PORT", "8000"))
SESSION_DIR = os.getenv("SESSION_DIR", "/app/.tg_session")

# Динамические — могут быть установлены из СРМки через /auth/send_code
_dynamic_api_id:   int = 0
_dynamic_api_hash: str = ""

def get_api_id() -> int:
    return _dynamic_api_id or _API_ID_ENV

def get_api_hash() -> str:
    return _dynamic_api_hash or _API_HASH_ENV

# ── Состояние ────────────────────────────────────────────────────────────────
_client: TelegramClient | None = None
_status = "disconnected"   # disconnected | awaiting_code | awaiting_2fa | connected
_phone  = None             # номер телефона текущей сессии
_phone_hash = None         # hash от send_code_request
_me: User | None = None    # инфо о себе
_handlers_registered = False  # гвардия против двойной регистрации хендлеров


def _session_file() -> str:
    os.makedirs(SESSION_DIR, exist_ok=True)
    return os.path.join(SESSION_DIR, "account.session")


def _has_session() -> bool:
    return os.path.exists(_session_file())


def _get_client(session=None) -> TelegramClient:
    return TelegramClient(
        session or _session_file(),
        get_api_id(), get_api_hash(),
        system_version="4.16.30-vxCUSTOM"
    )


# ── Webhook в основное приложение ─────────────────────────────────────────────
async def notify_main(event: str, data: dict):
    if not MAIN_APP:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"{MAIN_APP}/tg/webhook",
                json={"event": event, "data": data},
                headers={"X-TG-Secret": MAIN_SECRET}
            )
    except Exception as e:
        log.warning(f"[TG] notify_main error: {e}")


# ── Регистрируем обработчики входящих сообщений ───────────────────────────────
def _register_handlers(client: TelegramClient):
    global _handlers_registered
    if _handlers_registered:
        log.info("[TG] Handlers already registered, skipping duplicate registration")
        return
    _handlers_registered = True

    @client.on(events.MessageRead(inbox=False))
    async def on_read(event):
        """Получатель прочитал наши исходящие сообщения."""
        try:
            peer = await event.get_input_chat()
            entity = await client.get_entity(peer)
            if not isinstance(entity, User):
                return
            await notify_main("read", {
                "tg_user_id": str(entity.id),
                "max_id":     event.max_id,
            })
        except Exception as e:
            log.warning(f"[TG] on_read error: {e}")

    @client.on(events.NewMessage(outgoing=True))
    async def on_outgoing(event):
        """Исходящие сообщения — менеджер написал напрямую из Telegram."""
        if event.is_group or event.is_channel:
            return
        try:
            peer = await event.get_input_chat()
            entity = await client.get_entity(peer)
            if not isinstance(entity, User):
                return
        except Exception as e:
            log.warning(f"[TG] on_outgoing get_entity error: {e}")
            return

        user_id  = str(entity.id)
        username = getattr(entity, "username", None) or ""
        text     = event.message.text or event.message.message or "[медиафайл]"

        log.info(f"[TG] OUTGOING to {user_id} (@{username}): {text[:60]}")

        await notify_main("message", {
            "tg_user_id":   user_id,
            "username":     username,
            "sender_name":  "",
            "phone":        "",
            "body":         text,
            "has_media":    bool(event.message.media),
            "media_base64": None,
            "media_type":   "",
            "message_id":   event.message.id,
            "is_outgoing":  True,
        })

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if event.is_group or event.is_channel:
            return

        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        user_id   = str(sender.id)
        username  = getattr(sender, "username", None) or ""
        first     = getattr(sender, "first_name", "") or ""
        last      = getattr(sender, "last_name", "") or ""
        sender_name = f"{first} {last}".strip() or username or user_id
        phone_num = getattr(sender, "phone", None) or ""

        text = event.message.text or event.message.message or ""

        # Медиа
        media_base64 = None
        media_type   = None
        has_media    = bool(event.message.media)

        if has_media:
            try:
                buf = io.BytesIO()
                await client.download_media(event.message, file=buf)
                buf.seek(0)
                media_base64 = base64.b64encode(buf.read()).decode()
                # Определяем mime
                if isinstance(event.message.media, MessageMediaPhoto):
                    media_type = "image/jpeg"
                elif isinstance(event.message.media, MessageMediaDocument):
                    doc = event.message.media.document
                    for attr in doc.attributes:
                        pass
                    mime = getattr(doc, "mime_type", "application/octet-stream")
                    media_type = mime
                else:
                    media_type = "application/octet-stream"
                if not text:
                    text = "[медиафайл]"
            except Exception as e:
                log.warning(f"[TG] media download error: {e}")
                text = text or "[медиафайл]"

        log.info(f"[TG] MSG from {user_id} (@{username}): {text[:60]}")

        await notify_main("message", {
            "tg_user_id":  user_id,
            "username":    username,
            "sender_name": sender_name,
            "phone":       phone_num,
            "body":        text,
            "has_media":   has_media,
            "media_base64": media_base64,
            "media_type":  media_type,
            "message_id":  event.message.id,
        })


# ── FastAPI ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _status, _me
    if _has_session() and get_api_id() and get_api_hash():
        try:
            _client = _get_client()
            await _client.connect()
            if await _client.is_user_authorized():
                _me = await _client.get_me()
                _status = "connected"
                _register_handlers(_client)
                log.info(f"[TG] Auto-connected: @{_me.username} +{_me.phone}")
                await notify_main("ready", {
                    "user_id":  str(_me.id),
                    "username": _me.username or "",
                    "phone":    _me.phone or "",
                    "name":     f"{_me.first_name or ''} {_me.last_name or ''}".strip()
                })
            else:
                _status = "disconnected"
                await _client.disconnect()
                _client = None
        except Exception as e:
            log.error(f"[TG] Auto-connect error: {e}")
            _status = "disconnected"
    yield
    if _client:
        await _client.disconnect()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def auth_check(request: Request) -> bool:
    secret = request.headers.get("X-Api-Secret") or request.query_params.get("secret")
    return secret == API_SECRET


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/status")
async def status(request: Request):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    return {
        "status":          _status,
        "phone":           _me.phone if _me else None,
        "username":        _me.username if _me else None,
        "name":            f"{_me.first_name or ''} {_me.last_name or ''}".strip() if _me else None,
        "user_id":         str(_me.id) if _me else None,
        "has_credentials": bool(get_api_id() and get_api_hash()),
    }


@app.post("/auth/send_code")
async def auth_send_code(request: Request):
    global _client, _status, _phone, _phone_hash
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)

    body = await request.json()
    phone = body.get("phone", "").strip()

    # Принимаем api_id/api_hash из запроса (из СРМки) или берём из env
    global _dynamic_api_id, _dynamic_api_hash
    if body.get("api_id"):
        try:
            _dynamic_api_id = int(body["api_id"])
        except Exception:
            pass
    if body.get("api_hash"):
        _dynamic_api_hash = str(body["api_hash"]).strip()

    if not phone:
        return JSONResponse({"error": "phone required"}, 400)
    if not get_api_id() or not get_api_hash():
        return JSONResponse({"error": "Укажите TG_API_ID и TG_API_HASH"}, 400)

    try:
        if _client:
            await _client.disconnect()
        _client = _get_client()
        await _client.connect()
        result = await _client.send_code_request(phone)
        _phone      = phone
        _phone_hash = result.phone_code_hash
        _status     = "awaiting_code"
        log.info(f"[TG] Code sent to {phone}")
        return {"ok": True, "message": f"Код отправлен на {phone}"}
    except PhoneNumberBannedError:
        return JSONResponse({"error": "Номер заблокирован в Telegram"}, 400)
    except FloodWaitError as e:
        return JSONResponse({"error": f"Подождите {e.seconds} секунд"}, 429)
    except Exception as e:
        log.error(f"[TG] send_code error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/auth/sign_in")
async def auth_sign_in(request: Request):
    global _client, _status, _me, _phone_hash
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)

    body = await request.json()
    code     = body.get("code", "").strip()
    password = body.get("password", "").strip()  # 2FA

    if not _client or not _phone:
        return JSONResponse({"error": "Сначала отправьте код"}, 400)

    try:
        if password:
            # 2FA
            await _client.sign_in(password=password)
        else:
            await _client.sign_in(_phone, code, phone_code_hash=_phone_hash)

        _me     = await _client.get_me()
        _status = "connected"
        _register_handlers(_client)
        log.info(f"[TG] Signed in: @{_me.username} +{_me.phone}")
        await notify_main("ready", {
            "user_id":  str(_me.id),
            "username": _me.username or "",
            "phone":    _me.phone or "",
            "name":     f"{_me.first_name or ''} {_me.last_name or ''}".strip()
        })
        return {"ok": True, "username": _me.username, "phone": _me.phone}

    except SessionPasswordNeededError:
        _status = "awaiting_2fa"
        return JSONResponse({"error": "2fa_required", "message": "Требуется пароль 2FA"}, 200)
    except PhoneCodeInvalidError:
        return JSONResponse({"error": "Неверный код"}, 400)
    except PasswordHashInvalidError:
        return JSONResponse({"error": "Неверный пароль 2FA"}, 400)
    except Exception as e:
        log.error(f"[TG] sign_in error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/auth/sign_out")
async def auth_sign_out(request: Request):
    global _client, _status, _me, _phone, _phone_hash, _handlers_registered
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    try:
        if _client:
            await _client.log_out()
            await _client.disconnect()
        import shutil
        if os.path.exists(SESSION_DIR):
            shutil.rmtree(SESSION_DIR, ignore_errors=True)
        _client = _status = _me = _phone = _phone_hash = None
        _handlers_registered = False
        _status = "disconnected"
        await notify_main("disconnected", {"reason": "sign_out"})
        return {"ok": True}
    except Exception as e:
        log.error(f"[TG] sign_out error: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/send")
async def send_message(request: Request):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)

    body = await request.json()
    to   = body.get("to")      # user_id или username
    text = body.get("message", "")
    if not to or not text:
        return JSONResponse({"error": "to and message required"}, 400)

    try:
        sent = await _client.send_message(int(to) if str(to).lstrip("-").isdigit() else to, text)
        return {"ok": True, "message_id": sent.id}
    except Exception as e:
        log.error(f"[TG] send error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.post("/send_media")
async def send_media(request: Request):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)

    body    = await request.json()
    to      = body.get("to")
    b64     = body.get("base64")
    mime    = body.get("mimetype", "image/jpeg")
    caption = body.get("caption", "")
    fname   = body.get("filename", "file.jpg")

    if not to or not b64:
        return JSONResponse({"error": "to and base64 required"}, 400)

    try:
        data = base64.b64decode(b64)
        buf  = io.BytesIO(data)
        buf.name = fname
        peer = int(to) if str(to).lstrip("-").isdigit() else to
        await _client.send_file(peer, buf, caption=caption, force_document=not mime.startswith("image/"))
        return {"ok": True}
    except Exception as e:
        log.error(f"[TG] send_media error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.get("/contact/{user_id}")
async def get_contact(request: Request, user_id: str):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)

    try:
        peer = int(user_id) if user_id.lstrip("-").isdigit() else user_id
        entity = await _client.get_entity(peer)
        photo_url = None
        try:
            buf = io.BytesIO()
            await _client.download_profile_photo(entity, file=buf)
            buf.seek(0)
            data = buf.read()
            if data:
                photo_url = f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
        except Exception:
            pass

        return {
            "ok":       True,
            "user_id":  str(entity.id),
            "username": getattr(entity, "username", None),
            "phone":    getattr(entity, "phone", None),
            "name":     f"{getattr(entity,'first_name','') or ''} {getattr(entity,'last_name','') or ''}".strip(),
            "about":    getattr(entity, "about", None),
            "photo_url": photo_url,
        }
    except Exception as e:
        log.error(f"[TG] get_contact error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.post("/mark_read")
async def mark_read(request: Request):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return JSONResponse({"error": "user_id required"}, 400)
    try:
        peer = int(user_id) if str(user_id).lstrip("-").isdigit() else user_id
        await _client.send_read_acknowledge(peer)
        return {"ok": True}
    except Exception as e:
        if "Could not find the input entity" in str(e):
            log.warning(f"[TG] mark_read: unknown entity {user_id}, skipping")
            return {"ok": True, "skipped": True}
        log.error(f"[TG] mark_read error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.delete("/message/{peer_id}/{message_id}")
async def delete_message(request: Request, peer_id: str, message_id: int):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)
    try:
        peer = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
        await _client.delete_messages(peer, [message_id])
        return {"ok": True}
    except Exception as e:
        log.error(f"[TG] delete_message error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.patch("/message/{peer_id}/{message_id}")
async def edit_message(request: Request, peer_id: str, message_id: int):
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, 400)
    try:
        peer = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
        await _client.edit_message(peer, message_id, text)
        return {"ok": True}
    except Exception as e:
        log.error(f"[TG] edit_message error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.get("/user_status/{user_id}")
async def get_user_status(request: Request, user_id: str):
    """Статус онлайн пользователя (online / recently / last_week / last_month / unknown)."""
    if not auth_check(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    if not _client or _status != "connected":
        return JSONResponse({"error": "Not connected"}, 503)
    try:
        peer   = int(user_id) if user_id.lstrip("-").isdigit() else user_id
        entity = await _client.get_entity(peer)
        st     = getattr(entity, "status", None)

        if isinstance(st, UserStatusOnline):
            return {"ok": True, "online": True,  "status": "online",      "last_seen": None}
        elif isinstance(st, UserStatusOffline):
            last = st.was_online.isoformat() if st.was_online else None
            return {"ok": True, "online": False, "status": "offline",     "last_seen": last}
        elif isinstance(st, UserStatusRecently):
            return {"ok": True, "online": False, "status": "recently",    "last_seen": None}
        elif isinstance(st, UserStatusLastWeek):
            return {"ok": True, "online": False, "status": "last_week",   "last_seen": None}
        elif isinstance(st, UserStatusLastMonth):
            return {"ok": True, "online": False, "status": "last_month",  "last_seen": None}
        else:
            return {"ok": True, "online": False, "status": "unknown",     "last_seen": None}
    except Exception as e:
        if "Could not find the input entity" in str(e):
            log.warning(f"[TG] get_user_status: unknown entity {user_id}, skipping")
            return {"ok": False, "online": False, "status": "unknown", "last_seen": None}
        log.error(f"[TG] get_user_status error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.get("/health")
async def health():
    return {"ok": True, "status": _status}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
