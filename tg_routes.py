"""
tg_routes.py — маршруты Telegram аккаунта для добавления в main.py

Добавь в env основного сервиса:
  TG_SERVICE_URL     = https://tg-service-xxx.up.railway.app
  TG_API_SECRET      = тот_же_секрет
  TG_WEBHOOK_SECRET  = тот_же_секрет_что_в_WA_WEBHOOK_SECRET
"""

# Эти переменные уже должны быть в main.py:
# TG_SVC_URL    = os.getenv("TG_SERVICE_URL", "").rstrip("/")
# TG_SVC_SECRET = os.getenv("TG_API_SECRET", "changeme")
# TG_WH_SECRET  = os.getenv("TG_WEBHOOK_SECRET", "changeme")


# ── Хелпер вызова TG сервиса ──────────────────────────────────────────────────

async def tg_api(method: str, path: str, **kwargs) -> dict:
    if not TG_SVC_URL:
        return {"error": "TG_SERVICE_URL not configured"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await getattr(client, method)(
                f"{TG_SVC_URL}{path}",
                headers={"X-Api-Secret": TG_SVC_SECRET},
                **kwargs
            )
            return resp.json()
    except Exception as e:
        log.error(f"TG API error: {e}")
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK — принимает события от TG сервиса
# ══════════════════════════════════════════════════════════════════════════════

# @app.post("/tg/webhook")
async def tg_webhook(request: Request):
    secret = request.headers.get("X-TG-Secret", "")
    if secret != TG_WH_SECRET:
        log.warning(f"[TG webhook] Wrong secret")
        return JSONResponse({"error": "unauthorized"}, 401)

    try:
        body = await request.json()
    except Exception as e:
        log.error(f"[TG webhook] Bad JSON: {e}")
        return JSONResponse({"ok": True})

    event = body.get("event")
    data  = body.get("data", {})
    log.info(f"[TG webhook] event={event} keys={list(data.keys())}")

    try:
        if event == "ready":
            db.set_setting("tg_account_status", "connected")
            db.set_setting("tg_account_username", data.get("username", ""))
            db.set_setting("tg_account_phone", data.get("phone", ""))
            db.set_setting("tg_account_name", data.get("name", ""))
            log.info(f"[TG webhook] ready, @{data.get('username')} +{data.get('phone')}")

        elif event == "disconnected":
            db.set_setting("tg_account_status", "disconnected")
            db.set_setting("tg_account_username", "")

        elif event == "message":
            tg_user_id  = data.get("tg_user_id", "")
            username    = data.get("username", "")
            sender_name = data.get("sender_name") or username or tg_user_id
            raw_text    = data.get("body") or ""
            has_media   = data.get("has_media", False)
            media_b64   = data.get("media_base64")
            media_type  = data.get("media_type", "")
            phone_num   = data.get("phone", "")

            # Загружаем медиа на Cloudinary если есть
            media_url = None
            if media_b64 and not media_url:
                try:
                    import cloudinary
                    import cloudinary.uploader
                    import base64 as _b64
                    cld_url = db.get_setting("cloudinary_url") or os.getenv("CLOUDINARY_URL", "")
                    if cld_url:
                        cloudinary.config(cloudinary_url=cld_url)
                        mime = media_type or "image/jpeg"
                        data_uri = f"data:{mime};base64,{media_b64}"
                        result = cloudinary.uploader.upload(data_uri, folder="tg_media", resource_type="auto")
                        media_url = result.get("secure_url")
                    elif media_type and media_type.startswith("image/"):
                        media_url = f"data:{media_type};base64,{media_b64}"
                except Exception as e:
                    log.error(f"[TG webhook] Cloudinary upload error: {e}")

            if not raw_text and has_media:
                raw_text = "[фото]" if (media_type or "").startswith("image/") else "[файл]"
            text = (raw_text or "").strip() or "[сообщение]"

            # Создаём или находим диалог
            conv = db.get_or_create_tg_account_conversation(tg_user_id, sender_name, username, phone_num)
            is_new_conv = not conv.get("utm_source") and not conv.get("fbclid")

            # UTM трекинг по временному окну
            if is_new_conv:
                click_data = db.get_staff_click_recent_any(minutes=30)
                if click_data:
                    db.apply_utm_to_tg_conv(
                        conv["id"],
                        fbclid=click_data.get("fbclid"),
                        fbp=click_data.get("fbp"),
                        utm_source=click_data.get("utm_source"),
                        utm_medium=click_data.get("utm_medium"),
                        utm_campaign=click_data.get("utm_campaign"),
                        utm_content=click_data.get("utm_content"),
                        utm_term=click_data.get("utm_term"),
                    )
                    db.mark_staff_click_used(click_data["ref_id"])
                    conv = db.get_tg_account_conversation(conv["id"]) or conv
                    log.info(f"[TG webhook] UTM linked utm={click_data.get('utm_campaign')}")

            db.save_tg_account_message(conv["id"], tg_user_id, "visitor", text,
                                        media_url=media_url, media_type=media_type)
            db.update_tg_account_last_message(tg_user_id, text, increment_unread=True)
            log.info(f"[TG webhook] saved msg conv={conv['id']} from={tg_user_id}: {text[:50]}")

            # Уведомление менеджеру
            notify_chat = db.get_setting("notify_chat_id")
            bot2 = bot_manager.get_staff_bot()
            if notify_chat and bot2:
                try:
                    uname_str = f"@{username}" if username else tg_user_id
                    short = text[:80] + ("..." if len(text) > 80 else "")
                    await bot2.send_message(
                        int(notify_chat),
                        f"💬 TG аккаунт — новое сообщение\n"
                        f"👤 {sender_name} ({uname_str})\n"
                        f"✉️ {short}"
                    )
                except Exception as e:
                    log.warning(f"[TG webhook] notify error: {e}")

    except Exception as e:
        log.error(f"[TG webhook] error: {e}", exc_info=True)

    return JSONResponse({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# TG АККАУНТ — страница чатов
# ══════════════════════════════════════════════════════════════════════════════

# @app.get("/tg_account/chat", response_class=HTMLResponse)
async def tg_account_chat_page(request: Request, conv_id: int = 0, status_filter: str = "open"):
    user, err = require_auth(request, tab="tg_account_chat")
    if err: return err

    convs = db.get_tg_account_conversations(status=status_filter if status_filter != "all" else None)

    # Статус подключения
    tg_status   = db.get_setting("tg_account_status", "disconnected")
    tg_username = db.get_setting("tg_account_username", "")
    tg_phone    = db.get_setting("tg_account_phone", "")

    if tg_status == "connected":
        conn_badge = f'<div style="background:#052e16;border:1px solid #166534;border-radius:7px;padding:6px 12px;font-size:.8rem;color:#86efac;margin-bottom:8px">📱 Подключён · @{tg_username} · +{tg_phone}</div>'
    else:
        conn_badge = f'<div style="background:#2d0a0a;border:1px solid #7f1d1d;border-radius:7px;padding:6px 12px;font-size:.8rem;color:#fca5a5;margin-bottom:8px">⚠️ TG аккаунт не подключён → <a href="/tg_account/setup" style="color:#fca5a5;text-decoration:underline">Подключить</a></div>'

    # Вкладки статусов
    def tab(val, label):
        active = "background:var(--orange);color:#fff" if val == status_filter else "background:var(--bg3);color:var(--text3)"
        return f'<a href="/tg_account/chat?status_filter={val}" style="flex:1;text-align:center;padding:5px 0;border-radius:7px;font-size:.78rem;font-weight:600;text-decoration:none;{active}">{label}</a>'

    tabs_html = f'<div style="display:flex;gap:4px;margin-bottom:8px">{tab("open","Открытые")}{tab("closed","Закрытые")}{tab("all","Все")}</div>'

    # Список диалогов
    conv_items = ""
    for c in convs:
        cls = "conv-item active" if c["id"] == conv_id else "conv-item"
        t = (c.get("last_message_at") or c["created_at"])[:16].replace("T", " ")
        ucount = f'<span class="unread-num">{c["unread_count"]}</span>' if c.get("unread_count", 0) > 0 else ""
        dot = "🟢" if c["status"] == "open" else "⚫"
        src_badge = '<span class="source-badge source-fb">🔵 FB</span>' if c.get("fbclid") else \
                    ('<span class="source-badge source-tg">TG</span>' if c.get("utm_source") else \
                     '<span class="source-badge source-organic">organic</span>')
        utm_parts = []
        if c.get("utm_campaign"): utm_parts.append(f'<span class="utm-tag" title="Кампания">🎯 {c["utm_campaign"][:30]}</span>')
        if c.get("utm_content"):  utm_parts.append(f'<span class="utm-tag" style="background:#1a2a1a;color:#86efac">📌 {c["utm_content"][:20]}</span>')
        if c.get("utm_term"):     utm_parts.append(f'<span class="utm-tag" style="background:#1a1a2a;color:#a5b4fc">📂 {c["utm_term"][:20]}</span>')
        utm_line = '<div class="conv-meta" style="display:flex;flex-wrap:wrap;gap:3px;margin-top:2px">' + "".join(utm_parts) + '</div>' if utm_parts else ""

        conv_items += f"""<a href="/tg_account/chat?conv_id={c['id']}&status_filter={status_filter}"><div class="{cls}">
          <div class="conv-name"><span>{dot} {c['visitor_name']}</span>{ucount}</div>
          <div style="font-size:.75rem;color:var(--text3)">@{c.get('username') or '—'}</div>
          <div class="conv-preview">{c.get('last_message') or 'Нет сообщений'}</div>
          <div class="conv-time" style="display:flex;align-items:center;justify-content:space-between">{t} {src_badge}</div>
          {utm_line}</div></a>"""

    if not conv_items:
        conv_items = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:.85rem">Нет диалогов</div>'

    # Активный диалог
    chat_area = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text3);font-size:.9rem">Выберите диалог</div>'

    if conv_id:
        active_conv = db.get_tg_account_conversation(conv_id)
        if active_conv:
            db.mark_tg_account_conv_read(conv_id)
            msgs = db.get_tg_account_messages(conv_id)

            messages_html = ""
            for m in msgs:
                t = m["created_at"][11:16]
                if m.get("media_url") and (m.get("media_type") or "").startswith("image/"):
                    content_html = f'<img src="{m["media_url"]}" style="max-width:220px;max-height:220px;border-radius:8px;display:block;cursor:pointer" onclick="window.open(this.src)" />'
                elif m.get("media_url"):
                    content_html = f'<a href="{m["media_url"]}" target="_blank" style="color:#60a5fa">📎 Открыть файл</a>'
                else:
                    content_html = (m["content"] or "").replace("<", "&lt;")
                sender_label = ""
                if m["sender_type"] == "manager" and m.get("sender_name"):
                    sender_label = f'<div style="font-size:.68rem;color:var(--orange);margin-bottom:2px;text-align:right;opacity:.8">{m["sender_name"]}</div>'
                messages_html += f"""<div class="msg {m['sender_type']}" data-id="{m['id']}">
                  {sender_label}<div class="msg-bubble">{content_html}</div>
                  <div class="msg-time">{t}</div></div>"""

            # Шапка
            uname = f"@{active_conv['username']}" if active_conv.get("username") else active_conv.get("tg_user_id", "")
            fb_sent = active_conv.get("fb_event_sent")
            lead_btn = '<span class="badge-green">✅ Lead отправлен</span>' if fb_sent else \
                       f'<form method="post" action="/tg_account/send_lead" style="display:inline"><input type="hidden" name="conv_id" value="{conv_id}"/><button class="btn btn-sm" style="font-size:.73rem;background:#1e3a5f;border:1px solid #3b5998;color:#93c5fd">📤 Lead → FB</button></form>'
            status_color = "#34d399" if active_conv["status"] == "open" else "#ef4444"
            close_btn = f'<form method="post" action="/tg_account/close"><input type="hidden" name="conv_id" value="{conv_id}"/><button class="btn-gray btn-sm">✓ Закрыть</button></form>' if active_conv["status"] == "open" else \
                        f'<form method="post" action="/tg_account/reopen"><input type="hidden" name="conv_id" value="{conv_id}"/><button class="btn-orange btn-sm">↺ Открыть</button></form>'
            delete_btn = f'<button class="btn-gray btn-sm" style="color:var(--red);border-color:#7f1d1d" onclick="deleteTgConv({conv_id})">🗑</button>' if user and user.get("role") == "admin" else ""

            # UTM теги
            utm_tags = ""
            tags = []
            if active_conv.get("fbclid") or active_conv.get("utm_source") in ("facebook","fb"):
                tags.append('<span class="utm-tag" style="background:#1e3a5f;color:#60a5fa">🔵 Facebook</span>')
            elif active_conv.get("utm_source"):
                tags.append(f'<span class="utm-tag">{active_conv["utm_source"]}</span>')
            if active_conv.get("utm_campaign"): tags.append(f'<span class="utm-tag">🎯 {active_conv["utm_campaign"][:25]}</span>')
            if active_conv.get("utm_content"):  tags.append(f'<span class="utm-tag" style="background:#1a2a1a;color:#86efac">📌 {active_conv["utm_content"][:20]}</span>')
            if active_conv.get("utm_term"):     tags.append(f'<span class="utm-tag" style="background:#1a1a2a;color:#a5b4fc">📂 {active_conv["utm_term"][:20]}</span>')
            if active_conv.get("fbclid"):       tags.append('<span class="utm-tag badge-green">fbclid ✓</span>')
            if tags:
                utm_tags = '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">' + "".join(tags) + '</div>'

            # Кнопка звонка
            call_url = f"https://t.me/{active_conv['username']}" if active_conv.get("username") else f"tg://user?id={active_conv.get('tg_user_id','')}"
            call_btn = f'<a href="{call_url}" target="_blank" class="btn-gray btn-sm" style="display:inline-flex;align-items:center;gap:4px;padding:5px 10px;border-radius:7px;font-size:.74rem;border:1px solid var(--border);text-decoration:none">📞 Открыть в TG</a>'

            chat_area = f"""
            <div class="chat-header">
              <div style="display:flex;align-items:flex-start;gap:12px;flex:1">
                <div class="avatar">{'@'[0]}</div>
                <div style="flex:1">
                  <div style="font-weight:700;color:var(--text)">{active_conv['visitor_name']} <span style="color:{status_color};font-size:.72rem">●</span></div>
                  <div style="font-size:.78rem;color:var(--text3)">{uname}</div>
                  <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center">
                    {lead_btn} {call_btn}
                  </div>
                  {utm_tags}
                </div>
              </div>
              <div style="display:flex;gap:6px;flex-shrink:0">{close_btn} {delete_btn}</div>
            </div>
            <div class="chat-messages" id="tg-msgs">{messages_html}</div>
            <div class="chat-input">
              <div style="position:relative;flex:1">
                <textarea id="tg-inp" placeholder="Написать в Telegram... (Enter — отправить)"
                  style="width:100%;resize:none;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px 44px 10px 14px;color:var(--text);font-size:.9rem;font-family:inherit;min-height:44px;max-height:120px"
                  rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendTgMsg()}}"></textarea>
                <label style="position:absolute;right:10px;bottom:10px;cursor:pointer;opacity:.6" title="Отправить файл">
                  📎<input type="file" id="tg-file" style="display:none" onchange="sendTgFile(this)"/>
                </label>
              </div>
              <button class="btn-orange" onclick="sendTgMsg()">Отправить</button>
            </div>
            <script>
            const TG_CONV_ID = {conv_id};
            const msgBox = document.getElementById('tg-msgs');
            if(msgBox) msgBox.scrollTop = msgBox.scrollHeight;
            let lastTgId = (()=>{{const msgs=document.querySelectorAll('#tg-msgs .msg[data-id]');return msgs.length?msgs[msgs.length-1].dataset.id:0}})();
            async function sendTgMsg(){{
              const inp=document.getElementById('tg-inp');
              const text=inp.value.trim();
              if(!text)return;
              inp.value='';
              const r=await fetch('/tg_account/send',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'conv_id='+TG_CONV_ID+'&text='+encodeURIComponent(text)}});
              const d=await r.json();
              if(!d.ok)alert('Ошибка: '+(d.error||''));
              else loadNewTgMsgs();
            }}
            async function sendTgFile(input){{
              if(!input.files[0])return;
              const fd=new FormData();fd.append('conv_id',TG_CONV_ID);fd.append('file',input.files[0]);
              const r=await fetch('/tg_account/send_media',{{method:'POST',body:fd}});
              const d=await r.json();
              if(!d.ok)alert('Ошибка отправки: '+(d.error||''));
              else loadNewTgMsgs();
              input.value='';
            }}
            async function loadNewTgMsgs(){{
              const res=await fetch('/api/tg_account_messages/{conv_id}?after='+lastTgId);
              if(!res.ok)return;
              const data=await res.json();
              if(!data.messages||!data.messages.length)return;
              data.messages.forEach(m=>{{
                const d=document.createElement('div');
                d.className='msg '+m.sender_type;d.dataset.id=m.id;
                let inner='';
                if(m.media_url&&m.media_type&&m.media_type.startsWith('image/'))
                  inner='<img src="'+m.media_url+'" style="max-width:220px;border-radius:8px;display:block;cursor:pointer" onclick="window.open(this.src)"/>';
                else if(m.media_url)
                  inner='<a href="'+m.media_url+'" target="_blank" style="color:#60a5fa">📎 Открыть файл</a>';
                else
                  inner=m.content||'';
                const sl=m.sender_name&&m.sender_type==='manager'?'<div style="font-size:.68rem;color:var(--orange);margin-bottom:2px;text-align:right;opacity:.8">'+m.sender_name+'</div>':'';
                d.innerHTML=sl+'<div class="msg-bubble">'+inner+'</div><div class="msg-time">'+m.created_at.substring(11,16)+'</div>';
                msgBox.appendChild(d);lastTgId=m.id;
              }});
              msgBox.scrollTop=msgBox.scrollHeight;
            }}
            async function deleteTgConv(id){{
              if(!confirm('Удалить TG диалог и все сообщения?'))return;
              const r=await fetch('/tg_account/delete',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'conv_id='+id}});
              const d=await r.json();
              if(d.ok)window.location.href='/tg_account/chat?status_filter={status_filter}';
              else alert('Ошибка удаления');
            }}
            setInterval(loadNewTgMsgs, 3000);
            </script>"""

    content = f"""<div style="display:grid;grid-template-columns:300px 1fr;height:calc(100vh - 64px);overflow:hidden">
      <div style="border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden">
        <div style="padding:10px;border-bottom:1px solid var(--border)">
          {conn_badge}
          {tabs_html}
          <input type="text" id="tg-search" placeholder="🔍 Поиск..." oninput="filterTgConvs(this.value)"
            style="width:100%;padding:6px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.83rem"/>
        </div>
        <div style="overflow-y:auto;flex:1" id="tg-conv-list">{conv_items}</div>
      </div>
      <div style="display:flex;flex-direction:column;overflow:hidden" id="tg-chat-area">
        {chat_area}
      </div>
    </div>
    <script>
    function filterTgConvs(q){{
      document.querySelectorAll('#tg-conv-list a').forEach(el=>{{
        const n=el.querySelector('.conv-name')?.textContent?.toLowerCase()||'';
        el.style.display=n.includes(q.toLowerCase())?'':'none';
      }});
    }}
    </script>"""

    return HTMLResponse(base(content, "tg_account_chat", request))


# ══════════════════════════════════════════════════════════════════════════════
# TG АККАУНТ — страница подключения
# ══════════════════════════════════════════════════════════════════════════════

# @app.get("/tg_account/setup", response_class=HTMLResponse)
async def tg_account_setup_page(request: Request, msg: str = ""):
    user, err = require_auth(request, role="admin")
    if err: return err

    tg_status   = db.get_setting("tg_account_status", "disconnected")
    tg_username = db.get_setting("tg_account_username", "")
    tg_phone    = db.get_setting("tg_account_phone", "")
    alert = f'<div class="alert-green">✅ {msg}</div>' if msg else ""

    if tg_status == "connected":
        status_html = f"""
          <div style="background:#052e16;border:1px solid #166534;border-radius:12px;padding:16px;margin-bottom:20px">
            <div style="font-weight:700;color:#86efac;margin-bottom:4px">✅ Telegram аккаунт подключён</div>
            <div style="font-size:.85rem;color:#6ee7b7">@{tg_username} · +{tg_phone}</div>
          </div>
          <form method="post" action="/tg_account/disconnect">
            <button class="btn-gray" style="color:var(--red);border-color:#7f1d1d">🔌 Отключить аккаунт</button>
          </form>"""
    else:
        svc_status = await tg_api("get", "/status")
        svc_state  = svc_status.get("status", "disconnected") if not svc_status.get("error") else "disconnected"

        if svc_state == "awaiting_code":
            status_html = """
              <div style="background:#1c1a00;border:1px solid #713f12;border-radius:12px;padding:16px;margin-bottom:20px">
                <div style="font-weight:700;color:#fde047;margin-bottom:4px">📱 Ожидает код из SMS</div>
              </div>
              <form method="post" action="/tg_account/sign_in" style="display:flex;flex-direction:column;gap:12px;max-width:360px">
                <div class="field-group"><div class="field-label">Код из SMS</div>
                  <input type="text" name="code" placeholder="12345" autofocus required style="letter-spacing:.2em;font-size:1.1rem"/></div>
                <button class="btn">✅ Войти</button>
              </form>"""
        elif svc_state == "awaiting_2fa":
            status_html = """
              <div style="background:#1c1a00;border:1px solid #713f12;border-radius:12px;padding:16px;margin-bottom:20px">
                <div style="font-weight:700;color:#fde047;margin-bottom:4px">🔐 Требуется пароль двухфакторной аутентификации</div>
              </div>
              <form method="post" action="/tg_account/sign_in_2fa" style="display:flex;flex-direction:column;gap:12px;max-width:360px">
                <div class="field-group"><div class="field-label">Пароль 2FA</div>
                  <input type="password" name="password" autofocus required/></div>
                <button class="btn">🔓 Подтвердить</button>
              </form>"""
        else:
            status_html = """
              <div style="background:#2d0a0a;border:1px solid #7f1d1d;border-radius:12px;padding:16px;margin-bottom:20px">
                <div style="font-weight:700;color:#fca5a5;margin-bottom:4px">⚠️ Telegram аккаунт не подключён</div>
              </div>
              <form method="post" action="/tg_account/send_code" style="display:flex;flex-direction:column;gap:12px;max-width:360px">
                <div class="field-group"><div class="field-label">Номер телефона (с кодом страны)</div>
                  <input type="text" name="phone" placeholder="+79001234567" autofocus required/></div>
                <button class="btn">📱 Отправить код</button>
              </form>"""

    content = f"""<div class="page-wrap">
      <div class="page-title">📱 TG Аккаунт</div>
      <div class="page-sub">Подключение реального Telegram аккаунта для переписки</div>
      {alert}
      <div class="section">
        <div class="section-head"><h3>🔗 Управление подключением</h3></div>
        <div class="section-body">{status_html}</div>
      </div>
    </div>"""

    return HTMLResponse(base(content, "tg_account_setup", request))
