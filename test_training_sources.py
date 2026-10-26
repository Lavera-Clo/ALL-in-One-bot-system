"""
Checks for the ways a business trains its own bot (training_sources.py, learning_service.py):
its website, photos of menus and price lists, an FAQ spreadsheet, and its team's own chat replies.

Offline: the AI is scripted, the "website" is a small local server, and the database is a throwaway.

    python test_training_sources.py
"""

import asyncio
import io
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(_TMP.name, "train_test.db").replace("\\", "/")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from sqlalchemy import select  # noqa: E402

import learning_service  # noqa: E402
import llm_router  # noqa: E402
import main  # noqa: E402
import training_sources as ts  # noqa: E402
from auth_service import generate_session_token, hash_password, session_expiry  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import AdminSession, AdminUser, ChatHistory, Conversation, KnowledgeGap, Tenant, TenantDocument  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0
CUSTOMER = "94771110000"


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


# --- a small website ---------------------------------------------------------------------------

PAGES = {
    "/": """<html><head><title>Lavera Clo</title><script>var secret = 1;</script><style>.x{}</style></head><body>
        <nav><a href="/">Home</a> <a href="/about">About</a> <a href="/prices">Prices</a> <a href="/contact">Contact</a>
        <a href="/login">Log in</a> <a href="/logo.png">logo</a> <a href="https://elsewhere.example/x">Partner</a>
        <a href="/go-inside">Inside</a></nav>
        <h1>Lavera Clo - linen clothing in Gampaha</h1><p>Open 9am to 8pm, Monday to Saturday.</p></body></html>""",
    "/prices": """<html><head><title>Prices</title></head><body><nav><a href="/">Home</a> <a href="/prices">Prices</a></nav>
        <table><tr><th>Item</th><th>Price</th></tr><tr><td>Linen Shirt</td><td>Rs 3,500</td></tr>
        <tr><td>Summer Dress</td><td>Rs 5,200</td></tr></table></body></html>""",
    "/contact": "<html><head><title>Contact</title></head><body>Delivery island-wide, Rs 450. Call 0771596433.</body></html>",
    "/about": "<html><head><title>About</title></head><body>Family business since 2015.</body></html>",
    "/login": "<html><body>LOGIN FORM</body></html>",
}


class Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/go-inside":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path == "/old":
            self.send_response(301)
            self.send_header("Location", "/")
            self.end_headers()
            return
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


async def test_website(port: int):
    print("\nA private address is never read")
    for bad in ["http://127.0.0.1:%d/" % port, "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/"]:
        try:
            await ts.read_website(bad)
            check(False, bad)
        except ts.SourceError as e:
            check("private network" in str(e), f"refused: {bad}")
    for bad in ["", "ftp://example.com", "not a url at all"]:
        try:
            ts.normalise_url(bad)
            check(False, bad)
        except ts.SourceError:
            check(True, f"not a website address: {bad!r}")
    check(ts.normalise_url("www.shop.lk") == "https://www.shop.lk", "an address without https:// is completed")

    print("\nReading a website")
    real = ts._public_host

    def only_local(host):                      # the test site is on this machine; everything else is "outside"
        if host != "127.0.0.1":
            real(host)

    ts._public_host = only_local
    try:
        site = await ts.read_website(f"http://127.0.0.1:{port}/old")
    finally:
        ts._public_host = real
    pages = [p.rsplit(str(port), 1)[1] for p in site["pages"]]
    check(pages[0] == "/" and set(pages) == {"/", "/prices", "/contact", "/about"},
          f"the home page and the pages it links to, followed through a redirect ({pages})")
    check(pages.index("/prices") < pages.index("/about") and pages.index("/contact") < pages.index("/about"),
          "the useful pages (prices, contact) are read before the rest")
    check("LOGIN FORM" not in site["text"] and "Partner" in site["text"] and "elsewhere" not in "".join(site["pages"]),
          "login pages, pictures and other websites are skipped")
    check("/go-inside" not in "".join(site["pages"]), "a link that redirects into a private address is dropped")
    check("var secret" not in site["text"] and ".x{}" not in site["text"], "scripts and styles are left out")
    check("Linen Shirt | Rs 3,500" in site["text"] and "Summer Dress | Rs 5,200" in site["text"],
          "a price table keeps each item with its price")
    check("Open 9am to 8pm" in site["text"] and "Rs 450" in site["text"], "facts from every page are kept")
    check(site["text"].count("Home") <= 2, "the menu repeated on every page is kept once")
    return site


def test_photos():
    print("\nPhotos of a menu")
    imgs = []
    for color in ("white", "yellow"):
        buf = io.BytesIO()
        Image.new("RGB", (3000, 2000), color).save(buf, "JPEG")
        imgs.append((buf.getvalue(), f"{color}.jpg"))
    pdf = ts.photos_to_pdf(imgs)
    reader = PdfReader(io.BytesIO(pdf))
    check(len(reader.pages) == 2, "two photos -> one PDF with two pages")
    w = float(reader.pages[0].mediabox.width)
    check(w < 3000 * 72 / 150, "big phone photos are scaled down (smaller upload, same text)")
    for bad, label in [([], "no photos"), ([(b"not an image", "x.jpg")], "not a photo"), (imgs * 11, "more than 20")]:
        try:
            ts.photos_to_pdf(bad)
            check(False, label)
        except ts.SourceError:
            check(True, f"refused with a clear message: {label}")
    return pdf


def test_faq_parsing():
    print("\nFAQ spreadsheets")
    pairs = ts.parse_faq("Question,Answer\nDo you deliver?,Yes, island-wide for Rs 450\n\nOpen on Sunday?,No\n".encode(), "faq.csv")
    check(pairs == [("Do you deliver?", "Yes, island-wide for Rs 450"), ("Open on Sunday?", "No")],
          "CSV: header and blank rows skipped; an answer with a comma in it stays whole")
    semi = ts.parse_faq("Parking?;Free, 20 cars\n".encode(), "faq.csv")
    check(semi == [("Parking?", "Free, 20 cars")], "a semicolon-separated CSV (Excel in many countries) works")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Q", "A"])
    ws.append(["Return policy?", "Exchange within 7 days with the receipt"])
    ws.append(["Gift wrap?", None])
    buf = io.BytesIO()
    wb.save(buf)
    xl = ts.parse_faq(buf.getvalue(), "faq.xlsx")
    check(xl == [("Return policy?", "Exchange within 7 days with the receipt")], "Excel: rows without an answer are skipped")
    for content, name in [(b"just one column\n", "faq.csv"), (b"x", "faq.pdf")]:
        try:
            ts.parse_faq(content, name)
            check(False, name)
        except ts.SourceError:
            check(True, f"a clear message for {name} with nothing usable")
    return buf.getvalue()


async def main_async():
    await init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    site = await test_website(port)
    pdf = test_photos()
    xlsx = test_faq_parsing()

    async with AsyncSessionLocal() as db:
        t = Tenant(business_name="Lavera Clo", whatsapp_phone_number_id="5151", meta_access_token="EAA_placeholder_token",
                   system_prompt="x", knowledge_base="")
        other = Tenant(business_name="Other", whatsapp_phone_number_id="6161", meta_access_token="EAA_placeholder_token",
                       system_prompt="x", knowledge_base="")
        db.add_all([t, other])
        await db.flush()
        tokens = {}
        for key, tid in (("a", t.id), ("b", other.id)):
            u = AdminUser(username=f"{key}@train.test", password_hash=hash_password("x"), role="client", tenant_id=tid)
            db.add(u)
            await db.flush()
            tok = generate_session_token()
            db.add(AdminSession(admin_user_id=u.id, token=tok, expires_at=session_expiry()))
            tokens[key] = tok
        await db.commit()
        tid = t.id
    A = {"Authorization": f"Bearer {tokens['a']}"}
    B = {"Authorization": f"Bearer {tokens['b']}"}

    trained = []

    async def fake_training(document_id):
        trained.append(document_id)

    main.run_training = fake_training
    real_read = ts.read_website

    async def fake_read(url, max_pages=ts.MAX_PAGES):
        return {**site, "url": ts.normalise_url(url)}

    ts.read_website = fake_read
    real_candidates = llm_router.role_candidates
    llm_router.role_candidates = lambda role: [("fake", "m")]

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\nTraining from the website (portal)")
        r = await client.post(f"/api/tenants/{tid}/documents/website", headers=A, json={"url": "laveraclo.lk"})
        doc = r.json()["document"]
        check(r.status_code == 200 and doc["source"] == "website" and doc["original_filename"] == "https://laveraclo.lk",
              "a website document is made, remembering the address")
        check(trained == [doc["id"]], "and goes through the usual checked training")
        async with AsyncSessionLocal() as db:
            row = await db.get(TenantDocument, doc["id"])
        check("Linen Shirt | Rs 3,500" in row.raw_text and row.doc_type == "knowledge",
              "the website's text is what gets trained (answer-from-it only: there is no file to send)")
        async with AsyncSessionLocal() as db:
            row = await db.get(TenantDocument, doc["id"])
            row.status = "ready"
            await db.commit()
        site["text"] = site["text"].replace("Rs 3,500", "Rs 3,900")
        r = await client.post(f"/api/tenants/{tid}/documents/{doc['id']}/refresh-website", headers=A)
        async with AsyncSessionLocal() as db:
            row = await db.get(TenantDocument, doc["id"])
        check(r.status_code == 200 and "Rs 3,900" in row.raw_text and row.status == "queued" and trained[-1] == doc["id"],
              "'Read website again' picks up a new price and retrains")
        r = await client.post(f"/api/tenants/{tid}/documents/{doc['id']}/refresh-website", headers=B)
        check(r.status_code in (403, 404), "another business can't touch it")
        ts.read_website = real_read
        r = await client.post(f"/api/tenants/{tid}/documents/website", headers=A, json={"url": "http://10.1.2.3/"})
        check(r.status_code == 400 and "private network" in r.json()["detail"], "a private address is refused in the portal too")

        print("\nTraining from photos (portal)")
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), "white").save(buf, "PNG")
        r = await client.post(f"/api/tenants/{tid}/documents/photos", headers=A,
                              files=[("files", ("menu1.png", buf.getvalue(), "image/png")),
                                     ("files", ("menu2.png", buf.getvalue(), "image/png"))], data={"title": "Menu board"})
        doc = r.json()["document"]
        check(r.status_code == 200 and doc["source"] == "photos" and doc["title"] == "Menu board" and trained[-1] == doc["id"],
              "the photos become a document the AI vision readers train")
        async with AsyncSessionLocal() as db:
            row = await db.get(TenantDocument, doc["id"])
        check(row.stored_path and row.stored_path.endswith(".pdf") and not row.file_url,
              "kept as a PDF for the readers, but never sent to customers")
        r = await client.post(f"/api/tenants/{tid}/documents/photos", headers=A, files=[("files", ("x.jpg", b"nope", "image/jpeg"))])
        check(r.status_code == 400 and "photo" in r.json()["detail"], "something that isn't a photo: a clear message")

        print("\nFAQ spreadsheet (portal)")
        r = await client.post(f"/api/tenants/{tid}/faq/import", headers=A,
                              files={"file": ("faq.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        check(r.status_code == 200 and r.json()["added"] == 1, "each row taught at once")
        r = await client.post(f"/api/tenants/{tid}/faq/import", headers=A,
                              files={"file": ("faq.csv", b"Return policy?,Exchange within 14 days\nParking?,Free for 20 cars\n", "text/csv")})
        check(r.json()["added"] == 1 and r.json()["updated"] == 1, "importing again updates an answer instead of doubling it")
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tid)
            gaps = (await db.execute(select(KnowledgeGap).where(KnowledgeGap.tenant_id == tid))).scalars().all()
        check(all(g.status == "answered" and g.source == "faq" for g in gaps) and len(gaps) == 2, "saved as taught answers")
        check("Exchange within 14 days" in (tenant.knowledge_base or "") and "Free for 20 cars" in (tenant.knowledge_base or ""),
              "and the bot's knowledge has them (one rebuild for the whole sheet)")
        r = await client.post(f"/api/tenants/{tid}/faq/import", headers=B, files={"file": ("faq.csv", b"a?,b\n", "text/csv")})
        check(r.status_code in (403, 404), "another business can't import into this one")
        async with AsyncSessionLocal() as db:
            mine = (await db.execute(select(KnowledgeGap).where(KnowledgeGap.tenant_id == tid))).scalars().all()
        check(len(mine) == 2, "another business's import never lands in this one")

        print("\nLearning from the team's chat replies")
        calls = []

        async def fake_json(role, messages, **kw):
            calls.append(messages[1]["content"])
            assert "WhatsApp chat" in messages[0]["content"]
            return ({"learned": [{"question": "Do you deliver to Kandy?", "answer": "Yes, delivery to Kandy is Rs 450."},
                                 {"question": "Can you send it to my number?", "answer": "We will WhatsApp you on 0771234567."}]}, None)

        llm_router.complete_json = fake_json
        async with AsyncSessionLocal() as db:
            db.add(Conversation(tenant_id=tid, customer_phone=CUSTOMER))
            for sender, role, text in [("customer", "user", "hi do you deliver to Kandy?"), ("bot", "assistant", "Let me check with the team."),
                                       ("agent", "assistant", "Yes we do, Rs 450 to Kandy, 2-3 days."), ("customer", "user", "ok thanks")]:
                db.add(ChatHistory(tenant_id=tid, customer_phone_number=CUSTOMER, role=role, content=text, sender=sender,
                                   message_type="text", status="SENT"))
            await db.commit()
        learned = await learning_service.learn_from_chat(tid, CUSTOMER)
        check("Customer: hi do you deliver to Kandy?" in calls[-1] and "Staff: Yes we do, Rs 450" in calls[-1],
              "the team's reply is read together with the question it answered")
        check(len(learned) == 1 and learned[0]["answer"] == "Yes, delivery to Kandy is Rs 450.",
              "the general fact becomes a suggestion; the one about this customer's number doesn't")
        async with AsyncSessionLocal() as db:
            gap = await db.get(KnowledgeGap, learned[0]["gap_id"])
        check(gap.status == "open" and gap.source == "chat-team" and gap.answer is None,
              "it waits for the owner on 'Questions to answer'")
        n = len(calls)
        check(await learning_service.learn_from_chat(tid, CUSTOMER) == [] and len(calls) == n,
              "nothing new since: the same replies aren't read twice")

        print("\nA reply sent from the inbox is learned from (a little later)")
        learning_service.CHAT_LEARN_DELAY = 0.2
        r = await client.post(f"/api/tenants/{tid}/inbox/chats/{CUSTOMER}/messages", headers=A,
                              json={"text": "Also open on Sundays 10 to 4."})
        check(r.json()["ok"] and (tid, CUSTOMER) in learning_service._pending, "the team's reply schedules a look")
        learning_service.learn_from_chat_later(tid, CUSTOMER)
        check(sum(1 for k in learning_service._pending if k == (tid, CUSTOMER)) == 1, "several replies: one look per chat")
        await asyncio.sleep(0.6)
        check(len(calls) == n + 1 and "Sundays 10 to 4" in calls[-1] and (tid, CUSTOMER) not in learning_service._pending,
              "and it reads the new reply")

        print("\nLooking through past chats (portal button)")
        result = await learning_service.learn_from_past_chats(tid)
        check(result["chats"] == 1, "every chat where the team replied is looked at")
        r = await client.post(f"/api/tenants/{tid}/learn-from-chats", headers=A)
        check(r.status_code == 200, "the portal button starts it")
        r = await client.post(f"/api/tenants/{tid}/learn-from-chats", headers=B)
        check(r.status_code in (403, 404), "another business can't scan this one's chats")

    llm_router.role_candidates = real_candidates
    server.shutdown()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print(f"\nAll {_passed} checks passed.")
    finally:
        _TMP.cleanup()
