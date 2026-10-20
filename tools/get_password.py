#!/usr/bin/env python3
"""Fetches the gateway IP and PswOpen from the Eliot API.

Usage:
    python3 tools/get_password.py [email]

The configuration zip is protected by a password hardcoded in the Door Entry app
(constant in p074K2/C0594h.java:21 of the decompiled APK, not reproduced here). Set it in
the C300X_CONF_ZIP_PASSWORD environment variable to extract encrypted zips.
"""
import http.client
import json
import sys
import getpass
import base64
import zipfile
import io
import os

ELIOT_HOST = "www.myhomeweb.com"
ELIOT_PORT = 443


def eliot_request(method, path, headers=None, body=None):
    """HTTP request to Eliot."""
    conn = http.client.HTTPSConnection(ELIOT_HOST, ELIOT_PORT, timeout=10)

    req_headers = {"prj_name": "C3X"}
    if headers:
        req_headers.update(headers)

    if body:
        req_headers["Content-Type"] = "application/json"

    print(f"{method} {path}")
    conn.request(method, path, body=json.dumps(body) if body else None, headers=req_headers)

    resp = conn.getresponse()
    data = resp.read()

    # Read the headers
    resp_headers = {}
    for h, v in resp.getheaders():
        resp_headers[h.lower()] = v

    conn.close()

    if resp.status >= 400:
        error_name = {
            401: "Unauthorized (wrong email/password?)",
            432: "InvalidPassword",
            441: "TokenNotValidated",
            503: "ServiceUnavailable"
        }.get(resp.status, f"HTTP {resp.status}")

        print(f"✗ Error {resp.status}: {error_name}")
        try:
            print(f"  Response: {data.decode()}")
        except:
            pass
        sys.exit(1)

    result = {"status": resp.status, "headers": resp_headers}
    if data:
        try:
            result["json"] = json.loads(data)
        except:
            result["text"] = data.decode()

    return result


def login(email, password):
    """Eliot login."""
    print(f"\n=== Login for {email} ===\n")

    body = {
        "username": email,
        "pwd": password,
        "appVersion": "legacy-doorentryforc300x-android-1.17.5"
    }

    resp = eliot_request("POST", "/eliot/users/sign_in", body=body)
    token = resp["headers"].get("auth_token")

    if not token:
        print("✗ No token in the response")
        sys.exit(1)

    print(f"✓ Token: {token[:8]}...{token[-8:]}")
    return token


def get_plants(token):
    """Lists the plants (installations)."""
    print("\n=== Plants ===\n")

    headers = {"auth_token": token}
    resp = eliot_request("GET", "/eliot/plants", headers=headers)

    plants = resp["json"]
    if not plants:
        print("✗ No plant found")
        sys.exit(1)

    for i, p in enumerate(plants, 1):
        print(f"{i}. {p.get('PlantName', '?')} (ID: {p.get('PlantId')})")
        print(f"   GatewayId: {p.get('GatewayId')}")
        print(f"   Address: {p.get('Address', '?')}, {p.get('City', '?')}")
        print()

    return plants


def get_gateway_list(token, plant_id):
    """Lists the gateways of a plant."""
    print(f"\n=== Gateways ===\n")

    headers = {"auth_token": token}
    path = f"/eliot/plants/{plant_id}/gateway/"  # with trailing slash
    resp = eliot_request("GET", path, headers=headers)

    gateways = resp["json"]
    if not gateways:
        print("✗ No gateway found")
        return None

    for gw in gateways:
        gw_id = gw.get("GatewayId")
        print(f"GatewayId: {gw_id}")

    # Return the first one
    return gateways[0].get("GatewayId")


def get_gateway_info(token, plant_id, gw_id):
    """Gateway info."""
    print(f"\n=== Gateway {gw_id} ===\n")

    headers = {
        "auth_token": token,
        "need_token": "0"  # normal login, not association
    }

    path = f"/eliot/plants/{plant_id}/gateway/{gw_id}"
    resp = eliot_request("GET", path, headers=headers)

    gw = resp["json"]

    print(f"MAC: {gw.get('MacAddress', '?')}")
    print(f"PswOpen: {gw.get('PswOpen', '?')}")
    print(f"Token: {gw.get('Token', '?')}")
    print(f"TemporaryToken: {gw.get('TemporaryToken', '?')}")
    print(f"Associato: {gw.get('Associato', '?')}")

    return gw


def get_conf_zip(token, plant_id, gw_id):
    """Downloads and extracts the configuration zip."""
    print(f"\n=== Configuration ===\n")

    headers = {"auth_token": token}
    path = f"/eliot/plants/{plant_id}/gateway/{gw_id}/conf"
    resp = eliot_request("GET", path, headers=headers)

    # Decode the Base64
    b64_data = resp.get("text") or resp.get("json", {}).get("payload", "")
    if not b64_data:
        print("✗ No payload in the response")
        return None

    zip_data = base64.b64decode(b64_data)

    # Extract
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        password = os.environ.get("C300X_CONF_ZIP_PASSWORD", "").encode()

        # Try with the zip password
        try:
            if password:
                zf.setpassword(password)
        except:
            pass

        files = {}
        for name in zf.namelist():
            try:
                files[name] = zf.read(name)
            except:
                print(f"⚠ Cannot read {name}")

        return files

    except Exception as e:
        print(f"✗ Extraction error: {e}")
        return None


def main():
    if len(sys.argv) > 1:
        email = sys.argv[1]
    else:
        email = input("Door Entry email: ")

    password = getpass.getpass("Password: ")

    # 1. Login
    token = login(email, password)

    # 2. Plants
    plants = get_plants(token)

    if len(plants) == 1:
        plant = plants[0]
        print(f"→ Auto-selected: {plant['PlantName']}")
    else:
        choice = input(f"\nChoose a plant (1-{len(plants)}): ")
        plant = plants[int(choice) - 1]

    plant_id = plant["PlantId"]

    # 3. Gateway list
    gw_id = get_gateway_list(token, plant_id)
    if not gw_id:
        print("✗ Could not get the GatewayId")
        sys.exit(1)

    # 4. Gateway info
    gw = get_gateway_info(token, plant_id, gw_id)
    psw_open = gw.get("PswOpen")

    # 5. Config zip
    conf_files = get_conf_zip(token, plant_id, gw_id)

    ip = None
    dev = None
    addr = None

    if conf_files:
        # Read the IP
        if "read-only-par.txt" in conf_files:
            try:
                data = json.loads(conf_files["read-only-par.txt"])
                ip = data.get("wifi", {}).get("ip")
                if ip:
                    print(f"IP (from zip): {ip}")
            except:
                pass

        # Read current_modality
        if "current_modality" in conf_files:
            lines = conf_files["current_modality"].decode().strip().split("\n")
            if len(lines) >= 3:
                N, P, M = lines[0], lines[1], lines[2]
                print(f"Modality: N={N}, P={P}, M={M}")

                # Main device: dev="2" (or read from conf.xml), addr=P
                # For simplicity, assume dev=2
                dev = "2"
                addr = P

        print(f"\nFiles in the zip: {', '.join(conf_files.keys())}")

    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    if ip:
        print(f"Gateway IP: {ip}")
    else:
        print("Gateway IP: NOT FOUND (use a network scan)")

    if psw_open:
        print(f"OpenWebNet password: {psw_open}")
    else:
        print("Password: NOT FOUND (try 12345?)")

    if dev and addr:
        where = dev + addr
        print(f"Main door lock WHERE: {where}")
        print(f"\nTest command:")
        if ip and psw_open:
            print(f"  python3 tools/own_client.py test {ip} {psw_open}")
        elif ip:
            print(f"  python3 tools/own_client.py test {ip} [password]")

    print()


if __name__ == "__main__":
    main()
