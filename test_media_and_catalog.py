"""
Checks for sharing every kind of WhatsApp message (media_service.py) and the WhatsApp catalogue
(catalog_service.py): received files kept and shown in the inbox, files / locations / contacts /
products sent from the inbox, products synced to Meta, and a cart sent from WhatsApp becoming an order.

Offline: Meta is scripted; the database and the media folder are throwaways.

    python test_media_and_catalog.py
"""

import asyncio
import io
import os
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "media_test.db").replace("\\", "/")
os.environ["MEDIA_DIR"] = os.path.join(_TMP.name, "media")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

import catalog_service  # noqa: E402
import main  # noqa: E402
import media_service  # noqa: E402
import whatsapp_client  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, CartItem, ChatHistory, Order, Product, Tenant  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
CUSTOMER = "94771110000"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 2000


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


sent = []          # (kind, recipient, text) of every WhatsApp send
graph = []         # Meta catalogue API calls


async def record_send(phone_number_id, recipient, text, kind, ok, error, wa_id):
    sent.append((kind, recipient, text))


whatsapp_client.add_send_listener(record_send)


META_REFUSES = set()      # media ids Meta won't hand over right now (an expired token)


async def fake_download(media_id, token):
    if media_id in META_REFUSES:
        return b""
    return {"late1": JPEG,"img1": JPEG, "vid1": b"\x00\x00\x00\x18ftypmp4" + b"v" * 500, "doc1": b"%PDF-1.4 hello",
            "stk1": b"RIFF....WEBP"}.get(media_id, b"")


async def fake_graph(method, path, token, **kwargs):
    graph.append((method, path, kwargs))
    if path.endswith("items_batch"):
        return {"handles": ["h1"], "validation_status": []}
    return {"id": path, "name": "Main catalogue"}


def hook(phone, message, msg_id, pid="5050"):
    message = {"from": phone, "id": msg_id, "timestamp": "1700000000", **message}
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pid},
        "contacts": [{"wa_id": phone, "profile": {"name": "Nimal"}}], "messages": [message]}}]}]}


def picture(fmt, w, h, noise=False):
    from PIL import Image
    if noise:
        img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    else:
        img = Image.new("RGB", (w, h), "orange")
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def wav_tone(seconds=1.0):
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                               for i in range(int(16000 * seconds))))
    return buf.getvalue()


def webm_voice():
    """What Chrome's MediaRecorder makes: Opus audio in a WebM file."""
    import av
    import numpy as np
    out = io.BytesIO()
    with av.open(out, "w", format="webm") as c:
        stream = c.add_stream("libopus", rate=48000, layout="mono")
        t = np.arange(48000) / 48000
        pcm = (np.sin(2 * np.pi * 300 * t) * 8000).astype(np.int16)
        for i in range(0, pcm.size, 960):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + 960].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                c.mux(packet)
        for packet in stream.encode(None):
            c.mux(packet)
    return out.getvalue()


def served_size(url):
    return os.path.getsize(os.path.join(os.environ["MEDIA_DIR"], url.rsplit("/", 1)[1]))


async def rows(tenant_id, phone=CUSTOMER):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ChatHistory).where(ChatHistory.tenant_id == tenant_id,
                                                           ChatHistory.customer_phone_number == phone)
                                 .order_by(ChatHistory.id))).scalars().all()


async def main_async():
    await init_db()
    whatsapp_client.download_whatsapp_media = fake_download
    catalog_service._graph = fake_graph
    catalog_service._mock = lambda tenant: False

    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lavera Clo", whatsapp_phone_number_id="5050", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="x", currency="LKR", bot_enabled=False)
        other = Tenant(business_name="Other", whatsapp_phone_number_id="6060", meta_access_token="EAA_placeholder_token",
                       system_prompt="x", knowledge_base="x")
        db.add_all([t, other])
        await db.flush()
        shirt = Product(tenant_id=t.id, name="Linen Shirt", description="White, all sizes", price=3500, stock_quantity=4,
                        image_url="/static/products/shirt.jpg")
        dress = Product(tenant_id=t.id, name="Summer Dress", price=5200, stock_quantity=10, image_url="https://cdn.example/dress.jpg")
        cap = Product(tenant_id=t.id, name="Cap", price=900, stock_quantity=0, image_url="https://cdn.example/cap.jpg")
        nopic = Product(tenant_id=t.id, name="Gift card", price=2000, stock_quantity=50)
        db.add_all([shirt, dress, cap, nopic])
        tokens = {}
        for key, tid in (("a", t.id), ("b", other.id)):
            u = AdminUser(username=f"{key}@media.test", password_hash=hash_password("x"), role="client", tenant_id=tid)
            db.add(u)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=u.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        await db.commit()
        tid, ids = t.id, {"shirt": shirt.id, "dress": dress.id, "cap": cap.id, "nopic": nopic.id}
    A = {"Authorization": f"Bearer {tokens['a']}"}
    B = {"Authorization": f"Bearer {tokens['b']}"}

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\nReceived: every kind of message is kept and shown")
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "image", "image": {"id": "img1", "mime_type": "image/jpeg", "caption": "this one?"}}, "w1"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "video", "video": {"id": "vid1", "mime_type": "video/mp4"}}, "w2"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "document", "document": {"id": "doc1", "mime_type": "application/pdf", "filename": "size-chart.pdf"}}, "w3"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "sticker", "sticker": {"id": "stk1", "mime_type": "image/webp"}}, "w4"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "location", "location": {"latitude": 7.09, "longitude": 79.99, "name": "Home", "address": "Gampaha Road"}}, "w5"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "contacts", "contacts": [{"name": {"formatted_name": "Kamal"}, "phones": [{"phone": "+94 77 222 3333"}]}]}, "w6"))
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "reaction", "reaction": {"message_id": "x", "emoji": "👍"}}, "w7"))
        got = await rows(tid)
        by_type = {r.message_type: r for r in got}
        check(by_type["image"].media_url and by_type["image"].media_mime == "image/jpeg" and "this one?" in by_type["image"].content,
              "photo: copy kept, caption shown")
        check(by_type["video"].media_url.endswith(".mp4") and by_type["document"].media_name == "size-chart.pdf"
              and by_type["sticker"].media_url.endswith(".webp"), "video, document (with its name) and sticker kept")
        name = by_type["image"].media_url.rsplit("/", 1)[1]
        check(len(name.split(".")[0]) == 32, "stored under an unguessable name")
        served = await client.get(f"/media/{name}")
        check(served.status_code == 200 and served.content == JPEG, "the inbox can open it at /media/...")
        check((await client.get("/media/")).status_code == 404, "the folder itself can't be listed")
        check("Gampaha Road" in by_type["location"].content and "maps.google.com/?q=7.09,79.99" in by_type["location"].content,
              "location: place name plus a map link")
        check("Kamal" in by_type["contacts"].content and "+94 77 222 3333" in by_type["contacts"].content, "contact card: name and number")
        check(by_type["reaction"].content == "[Reacted 👍]", "reaction shown")

        print("\nA photo that couldn't be downloaded (expired token) is fetched later")
        META_REFUSES.add("late1")
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "image", "image": {"id": "late1", "mime_type": "image/jpeg"}}, "w8"))
        late = [r for r in await rows(tid) if r.wa_message_id == "w8"][0]
        check(late.media_url is None and late.media_wa_id == "late1" and late.media_mime == "image/jpeg",
              "Meta refused: no file yet, but WhatsApp's id for it is kept")
        listed = (await client.get(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/messages", headers=A)).json()["messages"]
        mine = next(m for m in listed if m["waMessageId"] == "w8")
        check(mine["mediaPending"] and mine["media"] is None, "the inbox shows it as 'not downloaded yet'")
        check(await media_service.fetch_missing(tid, CUSTOMER) == 0, "still refused: tried, and left for later")
        META_REFUSES.discard("late1")           # the owner pasted a new token
        await client.get(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/messages", headers=A)
        late = [r for r in await rows(tid) if r.wa_message_id == "w8"][0]
        check(late.media_url and served_size(late.media_url) == len(JPEG),
              "opening the chat again fetches it, once Meta allows it")
        listed = (await client.get(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/messages", headers=A)).json()["messages"]
        mine = next(m for m in listed if m["waMessageId"] == "w8")
        check(not mine["mediaPending"] and mine["media"]["url"] == late.media_url, "and the photo shows")
        listed = (await client.get(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/messages", headers=A)).json()["messages"]
        img = next(m for m in listed if m["type"] == "image")
        check(img["media"] and img["media"]["mime"] == "image/jpeg" and img["media"]["url"].endswith(name),
              "the inbox API gives each message its file (url, type, name)")

        print("\nSent from the inbox: files")
        sent.clear()
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("shirt.jpg", JPEG, "image/jpeg")}, data={"caption": "Here it is"})
        check(r.status_code == 200 and r.json()["ok"], "a photo is sent")
        check(sent[-1][0] == "image" and "Here it is" in sent[-1][2], "as an image, with its caption")
        out = (await rows(tid))[-1]
        check(out.sender == "agent" and out.message_type == "image" and out.media_url and out.media_mime == "image/jpeg",
              "kept in the chat as the agent's photo, with the file")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("price-list.pdf", b"%PDF-1.4 x", "application/pdf")})
        check(r.json()["ok"] and sent[-1][0] == "document" and "price-list.pdf" in sent[-1][2], "a PDF goes as a document, with its name")
        for fname, data, mime, kind in [("clip.mp4", b"v" * 100, "video/mp4", "video"), ("song.mp3", b"a" * 100, "audio/mpeg", "audio"),
                                        ("s.webp", picture("WEBP", 512, 512), "image/webp", "sticker")]:
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A, files={"file": (fname, data, mime)})
            check(r.json()["ok"] and sent[-1][0] == kind, f"{mime} goes as {kind}")

        print("\nFiles WhatsApp won't take are converted, not refused")
        big = picture("PNG", 3000, 3000, noise=True)
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("big.png", big, "image/png")})
        out = (await rows(tid))[-1]
        check(len(big) > 5 * 1024 * 1024 and r.json()["ok"] and sent[-1][0] == "image" and out.media_mime == "image/jpeg"
              and served_size(out.media_url) <= 5 * 1024 * 1024,
              f"a {len(big) // 1048576} MB photo is shrunk under WhatsApp's 5 MB and sent as a photo")
        for fname, data, mime, label in [("pic.webp", picture("WEBP", 800, 600), "image/webp", "a WEBP photo (not a sticker)"),
                                         ("pic.bmp", picture("BMP", 400, 300), "image/bmp", "a BMP picture")]:
            r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A, files={"file": (fname, data, mime)})
            out = (await rows(tid))[-1]
            check(r.json()["ok"] and sent[-1][0] == "image" and out.media_mime == "image/jpeg" and out.media_name.endswith(".jpg"),
                  f"{label} goes as a JPEG photo")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("tour.mov", b"m" * 100, "video/quicktime")})
        check(r.json()["ok"] and sent[-1][0] == "document", "a .mov video WhatsApp can't play still arrives, as a file")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("memo.wav", wav_tone(), "audio/wav")})
        out = (await rows(tid))[-1]
        check(r.json()["ok"] and sent[-1][0] == "audio" and out.media_mime == "audio/ogg", "a WAV recording is converted to OGG audio")

        print("\nA voice note recorded in the browser")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("rec.webm", webm_voice(), "audio/webm;codecs=opus")}, data={"voice": "true"})
        out = (await rows(tid))[-1]
        check(r.json()["ok"] and sent[-1][0] == "audio" and sent[-1][2].startswith("[Voice note]"),
              "Chrome's WebM recording goes as a WhatsApp voice note")
        check(out.media_mime == "audio/ogg" and served_size(out.media_url) > 500, "converted to OGG/Opus, and kept to play back in the inbox")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("rec.webm", b"not audio", "audio/webm")}, data={"voice": "true"})
        check(r.status_code == 400 and "voice note" in r.json()["detail"], "a broken recording: a clear message")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("empty.pdf", b"", "application/pdf")})
        check(r.status_code == 400 and "empty" in r.json()["detail"], "an empty file is refused")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("photo.jpg", JPEG, "image/jpeg")}, data={"as_type": "document"})
        check(r.json()["ok"] and sent[-1][0] == "document", "a photo can be sent as a file instead (full quality)")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=B, files={"file": ("x.jpg", JPEG, "image/jpeg")})
        check(r.status_code in (403, 404), "another business can't send in this chat")
        n = len(sent)
        for _ in range(2):
            await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/media", headers=A,
                              files={"file": ("x.jpg", JPEG, "image/jpeg")}, data={"client_msg_id": "dup-1"})
        check(len(sent) == n + 1, "the same upload repeated (dropped connection) is sent once")

        print("\nSent from the inbox: location, contacts")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A,
                              json={"type": "location", "latitude": 7.0917, "longitude": 79.9942, "name": "Lavera Clo", "address": "Gampaha Road"})
        check(r.json()["ok"] and sent[-1][0] == "location" and "Lavera Clo" in sent[-1][2], "a location pin")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A,
                              json={"type": "contacts", "contacts": [{"name": "Delivery", "phone": "+94771596433"}]})
        check(r.json()["ok"] and sent[-1][0] == "contacts" and "+94771596433" in sent[-1][2], "a contact card")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A, json={"type": "location"})
        check(r.status_code == 400, "a location without coordinates is refused")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A, json={"type": "catalog"})
        check(r.status_code == 400 and "catalogue" in r.json()["detail"], "no catalogue yet: says to connect one")

        print("\nThe WhatsApp catalogue")
        r = await client.post(f"/api/tenants/{tid}/catalog", headers=A, json={"catalog_id": "1234567890123"})
        check(r.status_code == 200 and r.json()["connected"], "catalogue connected")
        settings_call = next(g for g in graph if g[1].endswith("whatsapp_commerce_settings"))
        check(settings_call[2]["params"] == {"is_cart_enabled": "true", "is_catalog_visible": "true"},
              "cart and shop icon switched on for the number")
        batch = next(g for g in graph if g[1] == "1234567890123/items_batch")[2]["json"]
        items = {q["data"]["id"]: q["data"] for q in batch["requests"]}
        check(set(items) == {f"p{ids['shirt']}", f"p{ids['dress']}", f"p{ids['cap']}"}, "products with a picture are synced (p<id>)")
        check(r.json()["result"]["skipped"] == [{"product": "Gift card", "reason": "no image"}], "one without a picture is skipped, and said so")
        shirt_item = items[f"p{ids['shirt']}"]
        check(shirt_item["price"] == "3500.00 LKR" and shirt_item["availability"] == "in stock"
              and shirt_item["image_link"].startswith("http") and shirt_item["image_link"].endswith("/static/products/shirt.jpg"),
              "price in the shop's currency, in stock, picture as a full link")
        check(items[f"p{ids['cap']}"]["availability"] == "out of stock", "0 in stock -> out of stock in WhatsApp")
        page = await client.get(shirt_item["link"].replace(main.settings.BASE_URL.rstrip("/"), ""))
        check(page.status_code == 200 and "Linen Shirt" in page.text and "3,500.00" in page.text, "each product has its page")
        check((await client.get(f"/shop/{tid + 1}/{ids['shirt']}")).status_code == 404, "not under another business's address")
        status = (await client.get(f"/api/tenants/{tid}/catalog", headers=A)).json()
        check(status["synced_at"] and status["error"] is None and status["without_image"] == ["Gift card"], "status: synced, no errors")
        check((await client.get(f"/api/tenants/{tid}/catalog", headers=B)).status_code in (403, 404), "another business can't see it")

        graph.clear()
        await client.put(f"/api/products/{ids['dress']}", headers=A, json={"price": 4900})
        check(any(g[1].endswith("items_batch") and g[2]["json"]["requests"][0]["data"]["price"] == "4900.00 LKR" for g in graph),
              "a price change in the portal updates WhatsApp")
        graph.clear()
        await client.delete(f"/api/products/{ids['nopic']}", headers=A)
        check(any(q["method"] == "DELETE" and q["data"]["id"] == f"p{ids['nopic']}"
                  for g in graph if g[1].endswith("items_batch") for q in g[2]["json"]["requests"]), "a deleted product is removed")

        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A,
                              json={"type": "products", "product_ids": [ids["shirt"], ids["dress"]], "text": "New this week"})
        check(r.json()["ok"] and sent[-1][0] == "products", "send products from the catalogue")
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/share", headers=A, json={"type": "catalog"})
        check(r.json()["ok"] and sent[-1][0] == "catalog", "send the 'View catalogue' message")

        print("\nA customer sends a cart from WhatsApp")
        async with AsyncSessionLocal() as db:
            tt = await db.get(Tenant, tid)
            tt.bot_enabled = True
            await db.commit()
        sent.clear()
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "text", "text": {"body": "show me your shop"}}, "s1"))
        check(sent and sent[-1][0] == "catalog", "'show me your shop' -> the WhatsApp catalogue")
        sent.clear()
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "order", "order": {"catalog_id": "1234567890123", "text": "for Saturday",
            "product_items": [{"product_retailer_id": f"p{ids['shirt']}", "quantity": 6, "item_price": 3500, "currency": "LKR"},
                              {"product_retailer_id": f"p{ids['dress']}", "quantity": 1, "item_price": 4900, "currency": "LKR"},
                              {"product_retailer_id": f"p{ids['cap']}", "quantity": 1, "item_price": 900, "currency": "LKR"},
                              {"product_retailer_id": "SKU-UNKNOWN", "quantity": 1, "item_price": 1, "currency": "LKR"}]}}, "o1"))
        inbox_row = [r for r in await rows(tid) if r.message_type == "order"][-1]
        check("7 item(s)" not in inbox_row.content and "9 item(s)" in inbox_row.content and "for Saturday" in inbox_row.content,
              "the inbox shows the cart and the customer's note")
        async with AsyncSessionLocal() as db:
            cart = {c.product_id: c.quantity for c in (await db.execute(select(CartItem).where(CartItem.customer_phone == CUSTOMER))).scalars().all()}
        check(cart == {ids["shirt"]: 4, ids["dress"]: 1}, "the cart: 6 shirts capped at the 4 in stock, the dress; the sold-out cap left out")
        reply = sent[-1]
        check(reply[0] == "buttons" and "Checkout" in reply[2] and "only 4 Linen Shirt" in reply[2] and "Cap is out of stock" in reply[2]
              and "don't recognise" in reply[2], "the reply lists the cart, explains each problem, and offers Checkout")
        graph.clear()
        await client.post("/webhook", json=hook(CUSTOMER, {"type": "interactive", "interactive": {"type": "button_reply",
                                                "button_reply": {"id": "catalog_checkout", "title": "Checkout"}}}, "o2"))
        async with AsyncSessionLocal() as db:
            order = (await db.execute(select(Order).where(Order.tenant_id == tid).order_by(Order.id.desc()))).scalars().first()
            shirt_now = await db.get(Product, ids["shirt"])
        check(order and order.total_amount == 4 * 3500 + 4900, "Checkout -> an order for the cart (4 x 3,500 + 4,900)")
        check(shirt_now.stock_quantity == 0, "stock taken as usual")
        await asyncio.sleep(0.3)
        check(any(g[1].endswith("items_batch") and any(q["data"].get("availability") == "out of stock" for q in g[2]["json"]["requests"])
                  for g in graph), "the shirt just sold out: WhatsApp shows it out of stock")

        print("\nDisconnect")
        r = await client.delete(f"/api/tenants/{tid}/catalog", headers=A)
        check(not r.json()["connected"], "catalogue disconnected ('shop' goes back to the product list)")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
