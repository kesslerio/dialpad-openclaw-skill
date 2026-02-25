#!/usr/bin/env python3
"""
Dialpad Contact Lookup - Resolves phone numbers to contact names.

Usage:
    python3 scripts/lookup_contact.py "+14155551234"
    DIALPAD_API_KEY=xxx python3 scripts/lookup_contact.py "+14155551234"
"""
import os
import sys
import json
import urllib.request
import urllib.error

API_KEY = os.environ.get("DIALPAD_API_KEY")
BASE_URL = "https://dialpad.com/api/v2"


def get_contact_name(phone_number):
    """Try to resolve a phone number to a contact name via Dialpad API."""
    if not API_KEY:
        print("Error: DIALPAD_API_KEY environment variable not set", file=sys.stderr)
        return None

    # Dialpad expects E.164, but for searching it can be flexible.
    # We use the /contacts endpoint with a search query.
    query = phone_number.replace("+", "")
    url = f"{BASE_URL}/contacts?query={query}"
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("items", [])
            if items:
                # Return the first matching name
                contact = items[0]
                first = contact.get("first_name", "")
                last = contact.get("last_name", "")
                name = f"{first} {last}".strip()
                return name or "Known Contact (No Name)"
            return None
    except Exception as e:
        print(f"Contact lookup error: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/lookup_contact.py \u003cphone_number\u003e")
        print("Example: python3 scripts/lookup_contact.py +14155551234")
        sys.exit(1)
    
    test_num = sys.argv[1]
    name = get_contact_name(test_num)
    if name:
        print(f"Lookup for {test_num}: {name}")
    else:
        print(f"Lookup for {test_num}: No contact found")
