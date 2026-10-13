import requests
import json

# Test 1: Auto-generate bot profile from knowledge base
print("--- TEST 1: Auto Bot Profile Generator ---")
res = requests.post("http://127.0.0.1:8000/api/tenants/1/auto-profile",
                     headers={"Authorization": "Bearer bypass-token"})
print(f"Status: {res.status_code}")
if res.status_code == 200:
    data = res.json()
    profile = data.get("profile", {})
    print(f"Business: {profile.get('business_name')}")
    print(f"Type: {profile.get('business_type')}")
    print(f"Bot Name: {profile.get('bot_name')}")
    print(f"Persona: {profile.get('persona_template')}")
    print(f"Currency: {profile.get('currency')}")
    print(f"Greeting: {profile.get('greeting_style')}")
    print(f"Quick Menu: {profile.get('quick_menu')}")
    print(f"Handoff Rules: {profile.get('handoff_rules')}")
else:
    print(res.text)

# Test 2: Learning stats
print()
print("--- TEST 2: Learning Stats ---")
res2 = requests.get("http://127.0.0.1:8000/api/tenants/1/learning-stats",
                     headers={"Authorization": "Bearer bypass-token"})
print(f"Status: {res2.status_code}")
print(json.dumps(res2.json(), indent=2))

# Test 3: QA Cache
print()
print("--- TEST 3: QA Cache ---")
res3 = requests.get("http://127.0.0.1:8000/api/tenants/1/qa-cache",
                     headers={"Authorization": "Bearer bypass-token"})
print(f"Status: {res3.status_code}")
data3 = res3.json()
print(f"Total cached pairs: {data3.get('total', 0)}")
if data3.get("pairs"):
    for p in data3["pairs"][:3]:
        print(f"  Q: {p['question'][:60]}...")
        print(f"  A: {p['answer'][:60]}...")
        print()
