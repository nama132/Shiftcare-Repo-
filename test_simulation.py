"""Simulate the complete ShiftCare flow in DRY_RUN mode.

This script simulates:
1. Maria (571-572-8407) texts to cancel her 9am shift
2. System finds Priya (443-636-0988) as best candidate
3. Priya replies "YES" to accept coverage
4. Family (817-301-2688) receives notification

Run this with DRY_RUN_SMS=1 to see the flow without sending real SMS.
"""
from __future__ import annotations

import requests
import time
from urllib.parse import urlencode

BASE_URL = "http://localhost:5000"

def simulate_incoming_sms(from_number: str, body: str, provider: str = "telnyx"):
    """Simulate an incoming SMS to the webhook."""
    
    if provider == "telnyx":
        # Telnyx webhook format
        payload = {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "from": {"phone_number": from_number},
                    "to": [{"phone_number": "+12029927121"}],
                    "text": body
                }
            }
        }
        headers = {"Content-Type": "application/json"}
        url = f"{BASE_URL}/sms"
        response = requests.post(url, json=payload, headers=headers)
    
    elif provider == "twilio":
        # Twilio webhook format
        payload = {
            "From": from_number,
            "To": "+12029927121",
            "Body": body
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        url = f"{BASE_URL}/sms"
        response = requests.post(url, data=payload, headers=headers)
    
    else:  # vonage
        # Vonage webhook format
        payload = {
            "msisdn": from_number.replace("+", ""),
            "to": "12029927121",
            "text": body
        }
        url = f"{BASE_URL}/sms"
        response = requests.post(url, data=payload)
    
    print(f"📱 Simulated SMS from {from_number}: {body}")
    print(f"   Response: {response.status_code}")
    if response.status_code == 200:
        print(f"   ✅ Message processed successfully")
    else:
        print(f"   ❌ Error: {response.text}")
    return response


def main():
    print("\n" + "="*70)
    print("🏥 SHIFTCARE END-TO-END TEST SIMULATION (DRY_RUN MODE)")
    print("="*70 + "\n")
    
    # Step 1: Maria cancels her shift
    print("\n📍 STEP 1: Maria cancels her 9am shift")
    print("-" * 70)
    simulate_incoming_sms(
        from_number="+15715728407",  # Maria (571-572-8407)
        body="Hey it's Maria, I'm sick, can't make my 9am shift today",
        provider="telnyx"
    )
    
    print("\n⏳ Waiting 3 seconds for AI parsing and coverage hunt...")
    time.sleep(3)
    
    # Step 2: Check dashboard to see shift status changed to "uncovered"
    print("\n📍 STEP 2: Check dashboard for shift status")
    print("-" * 70)
    print("   Visit: http://localhost:5000/dashboard")
    print("   Expected: Maria's 9am shift should now show as 'uncovered' (red)")
    
    time.sleep(2)
    
    # Step 3: Priya replies YES to accept coverage
    print("\n📍 STEP 3: Priya accepts the coverage request")
    print("-" * 70)
    simulate_incoming_sms(
        from_number="+14436360988",  # Priya (443-636-0988)
        body="YES",
        provider="telnyx"
    )
    
    print("\n⏳ Waiting 2 seconds for shift claim processing...")
    time.sleep(2)
    
    # Step 4: Check final results
    print("\n📍 STEP 4: Verify final state")
    print("-" * 70)
    print("   ✅ Dashboard: Shift should now show 'covered' (green) with Priya")
    print("   ✅ Terminal logs should show:")
    print("      • Coverage request sent to Priya")
    print("      • Confirmation sent to Priya")
    print("      • Family notification sent to 817-301-2688")
    print("      • Owner summary sent to 571-406-3797")
    print("   ✅ Coverage log: Check at http://localhost:5000/admin/coverage-log")
    
    print("\n" + "="*70)
    print("✅ SIMULATION COMPLETE!")
    print("="*70)
    print("\nNext steps:")
    print("1. Review the terminal output where Flask is running")
    print("2. Check the dashboard at http://localhost:5000/dashboard")
    print("3. View coverage log at http://localhost:5000/admin/coverage-log")
    print("4. If everything looks good, set DRY_RUN_SMS=0 to test with real SMS")
    print("\n")


if __name__ == "__main__":
    main()
