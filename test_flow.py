import requests
import time

BASE = 'http://127.0.0.1:8000'
PHONE_ID = '1157440684128924'
CUSTOMER_PHONE = '94771234567'

def send_webhook(payload):
    return requests.post(f'{BASE}/webhook', json=payload)

print('--- Test 1: Customer texts "shop" ---')
payload1 = {
    'object': 'whatsapp_business_account',
    'entry': [{
        'id': 'test_entry',
        'changes': [{
            'field': 'messages',
            'value': {
                'metadata': {'phone_number_id': PHONE_ID},
                'messages': [{
                    'from': CUSTOMER_PHONE,
                    'id': 'msg_1',
                    'timestamp': str(int(time.time())),
                    'type': 'text',
                    'text': {'body': 'shop'}
                }]
            }
        }]
    }]
}
res = send_webhook(payload1)
print(f'Webhook response: {res.status_code}')
time.sleep(2)

print('\n--- Test 2: Customer selects product 1 ---')
payload2 = {
    'object': 'whatsapp_business_account',
    'entry': [{
        'id': 'test_entry',
        'changes': [{
            'field': 'messages',
            'value': {
                'metadata': {'phone_number_id': PHONE_ID},
                'messages': [{
                    'from': CUSTOMER_PHONE,
                    'id': 'msg_2',
                    'timestamp': str(int(time.time())),
                    'type': 'interactive',
                    'interactive': {
                        'type': 'list_reply',
                        'list_reply': {
                            'id': 'buy_product_1',
                            'title': 'Wooden Chair'
                        }
                    }
                }]
            }
        }]
    }]
}
res = send_webhook(payload2)
print(f'Webhook response: {res.status_code}')
time.sleep(2)

print('\n--- Fetching Orders from API ---')
orders = requests.get(f'{BASE}/api/tenants/1/orders').json()
print(f'Found {len(orders)} order(s).')
if orders:
    order = orders[0]
    print(f"Order details: {order['order_number']} - {order['product_name']} x{order['quantity']} - Status: {order['status']}")
    
    print('\n--- Test 3: Manager Confirms Order via API ---')
    update_res = requests.put(
        f'{BASE}/api/orders/{order["id"]}/status',
        json={'status': 'confirmed', 'confirmed_by': 'Test Script'}
    )
    print(f'Update response: {update_res.status_code}')
    if update_res.status_code == 200:
        print(f'New Status: {update_res.json()["status"]}')

print('\n--- Test 4: Manager texts CONFIRM via WhatsApp ---')
# First let us simulate an order being payment_uploaded so manager can confirm it
requests.put(
    f'{BASE}/api/orders/{order["id"]}/status',
    json={'status': 'payment_uploaded', 'confirmed_by': 'Test Script'}
)
payload3 = {
    'object': 'whatsapp_business_account',
    'entry': [{
        'id': 'test_entry',
        'changes': [{
            'field': 'messages',
            'value': {
                'metadata': {'phone_number_id': PHONE_ID},
                'messages': [{
                    'from': CUSTOMER_PHONE, # simulating manager
                    'id': 'msg_3',
                    'timestamp': str(int(time.time())),
                    'type': 'text',
                    'text': {'body': f'CONFIRM {order["order_number"]}'}
                }]
            }
        }]
    }]
}
res = send_webhook(payload3)
print(f'Webhook response: {res.status_code}')
time.sleep(1)
final_order = requests.get(f'{BASE}/api/orders/{order["id"]}').json()
print(f"Final Order Status: {final_order['status']}")
