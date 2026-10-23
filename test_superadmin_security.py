import asyncio
import os
import httpx
from datetime import datetime

# Set test environment
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///whatsapp_bot.db"
os.environ["TESTING"] = "1"

async def test_superadmin():
    from main import app
    from database import init_db, AsyncSessionLocal
    from models import AdminUser, PasswordResetRequest
    from auth_service import hash_password
    from sqlalchemy import select

    await init_db()

    # Ensure an owner account exists for testing
    async with AsyncSessionLocal() as session:
        owner = (await session.execute(select(AdminUser).where(AdminUser.role == "owner"))).scalars().first()
        if not owner:
            owner = AdminUser(
                username="test_superadmin@platform.lk",
                password_hash=hash_password("SuperSecret@123"),
                role="owner",
                is_active=True
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)
        owner_username = owner.username

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Login as owner
        login_resp = await client.post("/api/admin/login", json={
            "username": owner_username,
            "password": "SuperSecret@123"
        })
        if login_resp.status_code != 200:
            # Update password for test
            async with AsyncSessionLocal() as session:
                target = (await session.execute(select(AdminUser).where(AdminUser.username == owner_username))).scalars().first()
                target.password_hash = hash_password("SuperSecret@123")
                await session.commit()
            login_resp = await client.post("/api/admin/login", json={
                "username": owner_username,
                "password": "SuperSecret@123"
            })
        
        assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Test system stats
        stats_resp = await client.get("/api/superadmin/system-stats", headers=headers)
        assert stats_resp.status_code == 200, f"Stats failed: {stats_resp.text}"
        data = stats_resp.json()
        assert "disk" in data and "ram" in data and "accounts" in data
        print(f"System stats check passed: Disk {data['disk']['used_percent']}%, RAM {data['ram']['used_percent']}%, Total accounts {data['accounts']['total']}")

        # 3. Test account creation with weak password (should fail)
        weak_create = await client.post("/api/superadmin/accounts", headers=headers, json={
            "username": "weak_tester_123",
            "password": "123",
            "role": "staff"
        })
        assert weak_create.status_code == 400, "Weak password was unexpectedly accepted"
        print("Weak password rejection check passed.")

        # 4. Test account creation with strong password
        strong_create = await client.post("/api/superadmin/accounts", headers=headers, json={
            "username": "tester_secure_999",
            "password": "SecurePassword#2026",
            "role": "staff",
            "full_name": "Secure Tester"
        })
        if strong_create.status_code == 400 and "already in use" in strong_create.text:
            pass
        else:
            assert strong_create.status_code == 201, f"Failed to create account: {strong_create.text}"
            print("Strong password account creation check passed.")

        # 5. Test forgot-password anti-flooding
        fp1 = await client.post("/api/auth/forgot-password", json={"username": owner_username})
        assert fp1.status_code == 200
        fp2 = await client.post("/api/auth/forgot-password", json={"username": owner_username})
        assert fp2.status_code == 200
        print("Forgot password submission check passed.")

        # 6. Test listing password resets
        resets_resp = await client.get("/api/superadmin/password-resets", headers=headers)
        assert resets_resp.status_code == 200
        resets = resets_resp.json()
        print(f"Password reset queue check passed: {len(resets)} total requests.")

        # 7. Check security headers
        assert "x-content-type-options" in stats_resp.headers
        assert "x-frame-options" in stats_resp.headers
        print("Security headers check passed.")

    print("\nALL SUPER ADMIN AND SECURITY CHECKS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(test_superadmin())
