#!/usr/bin/env python3
"""Fetches and sets up SIP credentials from the Eliot API.

This script:
1. Logs in to Eliot
2. Fetches SipAccount/SipPassword (or creates them)
3. Generates an RSA key + CSR
4. Fetches the client certificate
5. Saves everything to sip_credentials.json

Usage:
    python3 tools/setup_sip_credentials.py <email> [--plant-id ID]
"""
import argparse
import base64
import getpass
import http.client
import io
import json
import os
import zipfile
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("⚠️  cryptography is not installed. Install it with:")
    print("   pip install cryptography")

ELIOT_HOST = "www.myhomeweb.com"
ELIOT_PORT = 443
CERTS_DIR = Path("sip_certs")
CREDS_FILE = Path("sip_credentials.json")


def eliot_request(method, path, headers=None, body=None):
    """HTTP request to Eliot."""
    conn = http.client.HTTPSConnection(ELIOT_HOST, ELIOT_PORT, timeout=30)

    req_headers = {"prj_name": "C3X"}
    if headers:
        req_headers.update(headers)

    if body:
        req_headers["Content-Type"] = "application/json"

    print(f"→ {method} {path}")
    conn.request(method, path, body=json.dumps(body) if body else None, headers=req_headers)

    resp = conn.getresponse()
    data = resp.read()

    resp_headers = {}
    for h, v in resp.getheaders():
        resp_headers[h.lower()] = v

    conn.close()

    if resp.status >= 400:
        print(f"✗ Error {resp.status}")
        try:
            print(f"  {data.decode('utf-8', errors='replace')}")
        except:
            pass
        raise Exception(f"HTTP {resp.status}")

    result = {"status": resp.status, "headers": resp_headers, "data": data}
    if data:
        # Try JSON first
        try:
            result["json"] = json.loads(data)
        except:
            # Try text
            try:
                result["text"] = data.decode('utf-8')
            except:
                # Binary data (like zip), keep as-is in "data"
                pass

    return result


def login(email, password):
    """Logs in and returns the token."""
    print(f"\n{'='*60}")
    print("1. LOGIN ELIOT")
    print('='*60)

    body = {
        "username": email,
        "pwd": password,
        "appVersion": "legacy-doorentryforc300x-android-1.17.5"
    }

    resp = eliot_request("POST", "/eliot/users/sign_in", body=body)
    token = resp["headers"].get("auth_token")

    if not token:
        raise Exception("No token in the response")

    print(f"✓ Token: {token[:12]}...{token[-12:]}")
    return token


def get_plants(token):
    """Lists the plants (installations)."""
    headers = {"auth_token": token}
    resp = eliot_request("GET", "/eliot/plants", headers=headers)
    return resp["json"]


def get_gateway_id(token, plant_id):
    """Fetches the GatewayId."""
    headers = {"auth_token": token}
    resp = eliot_request("GET", f"/eliot/plants/{plant_id}/gateway/", headers=headers)
    gateways = resp["json"]
    return gateways[0]["GatewayId"] if gateways else None


def get_sip_users(token, plant_id, gateway_id):
    """Fetches the list of SIP accounts."""
    print(f"\n{'='*60}")
    print("2. SIP ACCOUNTS")
    print('='*60)

    headers = {"auth_token": token}
    path = f"/eliot/sip/users/plants/{plant_id}/gateway/{gateway_id}"

    try:
        resp = eliot_request("GET", path, headers=headers)
        users = resp["json"]

        if users:
            print(f"✓ {len(users)} account(s) found")
            for u in users:
                print(f"  - {u.get('SipAccount')}")
            return users[0]  # First account

    except Exception as e:
        if "404" in str(e):
            print("✓ No SIP account (404)")
            return None
        raise


def create_sip_user(token, plant_id, gateway_id, device_id, user_email):
    """Creates a SIP account."""
    print("\n→ Creating SIP account...")

    # SipAccount format: email.replace("@","-") + "-" + device_id + "@" + gateway_id + ".bs.iotleg.com"
    sip_account = user_email.replace("@", "-") + f"-{device_id}@{gateway_id}.bs.iotleg.com"

    headers = {"auth_token": token, "Content-Type": "application/json"}
    body = {
        "SipAccount": sip_account,
        "DeviceName": "Home Assistant",
        "GatewayId": str(gateway_id),
        "IdDevice": device_id,
    }

    resp = eliot_request("POST", "/eliot/sip/user", headers=headers, body=body)

    print(f"✓ Account created: {sip_account}")

    # Re-fetch to get the password
    return get_sip_users(token, plant_id, gateway_id)


def generate_key_and_csr(sip_account):
    """Generates an RSA 2048 key + CSR."""
    print(f"\n{'='*60}")
    print("3. KEY + CSR GENERATION")
    print('='*60)

    if not CRYPTO_AVAILABLE:
        raise Exception("cryptography not available")

    # RSA 2048 key
    print("→ Generating RSA 2048 key...")
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # CSR
    print("→ Generating CSR...")
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IT"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Italy"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Varese"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bticino spa"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "DSI-PQS"),
        x509.NameAttribute(NameOID.COMMON_NAME, sip_account),
    ])).sign(key, hashes.SHA256())

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    print("✓ Key and CSR generated")
    return key_pem, csr_pem


def sign_certificate(token, sip_account, csr_pem):
    """Sends the CSR and fetches the signed certificate."""
    print(f"\n{'='*60}")
    print("4. CERTIFICATE SIGNING")
    print('='*60)

    headers = {"auth_token": token, "Content-Type": "application/json"}
    body = {
        "CommonName": sip_account,
        "CSR": csr_pem.decode(),
    }

    print("→ Sending CSR...")
    resp = eliot_request("POST", "/eliot/users/cert/signcert", headers=headers, body=body)

    # The response may be raw binary (zip) or JSON with a Base64 payload
    zip_data = None

    if "json" in resp and "payload" in resp["json"]:
        # Base64 payload in JSON
        b64_data = resp["json"]["payload"]
        print("→ Decoding Base64...")
        zip_data = base64.b64decode(b64_data)
    elif "text" in resp:
        # Base64 text
        print("→ Decoding Base64...")
        zip_data = base64.b64decode(resp["text"])
    elif "data" in resp:
        # Raw binary data (zip)
        print("→ Binary data received...")
        zip_data = resp["data"]

    if not zip_data:
        raise Exception("No data in the response")

    print("→ Extracting certificate zip...")
    zf = zipfile.ZipFile(io.BytesIO(zip_data))

    cert_pem = None
    ca_chain_pem = None

    for name in zf.namelist():
        content = zf.read(name)
        if name.endswith(".cert.pem") and "ca-chain" not in name:
            cert_pem = content
            print(f"  ✓ {name}")
        elif "ca-chain" in name:
            ca_chain_pem = content
            print(f"  ✓ {name}")

    if not cert_pem or not ca_chain_pem:
        raise Exception("Incomplete certificate in the zip")

    print("✓ Certificate retrieved")
    return cert_pem, ca_chain_pem


def save_credentials(
    gateway_ip,
    gateway_id,
    sip_account,
    sip_password,
    key_pem,
    cert_pem,
    ca_chain_pem,
):
    """Saves everything to sip_certs/ and sip_credentials.json."""
    print(f"\n{'='*60}")
    print("5. SAVING")
    print('='*60)

    # Create directory
    CERTS_DIR.mkdir(exist_ok=True)

    # Write files
    key_file = CERTS_DIR / "private.key"
    cert_file = CERTS_DIR / "client.crt"
    ca_file = CERTS_DIR / "ca-chain.crt"

    key_file.write_bytes(key_pem)
    cert_file.write_bytes(cert_pem)
    ca_file.write_bytes(ca_chain_pem)

    os.chmod(key_file, 0o600)

    print(f"✓ Private key  : {key_file}")
    print(f"✓ Certificate  : {cert_file}")
    print(f"✓ CA chain     : {ca_file}")

    # JSON credentials
    creds = {
        "gateway_ip": gateway_ip,
        "gateway_id": str(gateway_id),
        "sip_account": sip_account,
        "sip_password": sip_password,
        "key_file": str(key_file),
        "cert_file": str(cert_file),
        "ca_file": str(ca_file),
        "domain": f"{gateway_id}.bs.iotleg.com",
    }

    CREDS_FILE.write_text(json.dumps(creds, indent=2))
    os.chmod(CREDS_FILE, 0o600)

    print(f"✓ Credentials  : {CREDS_FILE}")

    return creds


def main():
    parser = argparse.ArgumentParser(description="Set up SIP credentials from Eliot")
    parser.add_argument("email", help="Door Entry account email")
    parser.add_argument("--plant-id", help="Plant (installation) ID (automatic if there is only one)")
    parser.add_argument("--device-id", default="HOMEASSISTANT01", help="Device ID (default: HOMEASSISTANT01)")
    args = parser.parse_args()

    if not CRYPTO_AVAILABLE:
        print("\n✗ Required: pip install cryptography")
        return 1

    password = getpass.getpass("Password: ")

    try:
        # 1. Login
        token = login(args.email, password)

        # 2. Plants
        plants = get_plants(token)
        if not plants:
            print("✗ No plant found")
            return 1

        if args.plant_id:
            plant = next((p for p in plants if str(p["PlantId"]) == args.plant_id), None)
            if not plant:
                print(f"✗ Plant {args.plant_id} not found")
                return 1
        else:
            if len(plants) > 1:
                print("\nPlants:")
                for i, p in enumerate(plants, 1):
                    print(f"  {i}. {p['PlantName']} (ID: {p['PlantId']})")
                choice = int(input("Choose (1-N): ")) - 1
                plant = plants[choice]
            else:
                plant = plants[0]

        plant_id = plant["PlantId"]
        print(f"\n→ Plant        : {plant['PlantName']} (ID: {plant_id})")

        # 3. Gateway
        gateway_id = get_gateway_id(token, plant_id)
        if not gateway_id:
            print("✗ No gateway")
            return 1

        print(f"→ Gateway ID   : {gateway_id}")

        # Get the IP from the gateway info
        headers = {"auth_token": token, "need_token": "0"}
        resp = eliot_request("GET", f"/eliot/plants/{plant_id}/gateway/{gateway_id}", headers=headers)
        gateway_ip = resp["json"].get("IpAddress") or input("Gateway IP: ")

        print(f"→ Gateway IP   : {gateway_ip}")

        # 4. SIP users
        sip_user = get_sip_users(token, plant_id, gateway_id)

        if not sip_user:
            # Create it
            sip_user = create_sip_user(token, plant_id, gateway_id, args.device_id, args.email)

        sip_account = sip_user["SipAccount"]
        sip_password = sip_user["SipPassword"]

        print(f"\n→ SIP Account  : {sip_account}")
        print(f"→ SIP Password : {sip_password[:4]}...{sip_password[-4:]}")

        # 5. Certificate
        key_pem, csr_pem = generate_key_and_csr(sip_account)
        cert_pem, ca_chain_pem = sign_certificate(token, sip_account, csr_pem)

        # 6. Save
        creds = save_credentials(
            gateway_ip,
            gateway_id,
            sip_account,
            sip_password,
            key_pem,
            cert_pem,
            ca_chain_pem,
        )

        # Summary
        print(f"\n{'='*60}")
        print("✓ SETUP COMPLETE")
        print('='*60)
        print(f"Gateway IP     : {creds['gateway_ip']}")
        print(f"Gateway ID     : {creds['gateway_id']}")
        print(f"SIP Account    : {creds['sip_account']}")
        print(f"Domain         : {creds['domain']}")
        print(f"\nFiles:")
        print(f"  - {creds['key_file']}")
        print(f"  - {creds['cert_file']}")
        print(f"  - {creds['ca_file']}")
        print(f"  - {CREDS_FILE}")
        print(f"\nNext step:")
        print(f"  python3 tools/probe_call.py --ffplay")

    except KeyboardInterrupt:
        print("\n\nCancelled")
        return 1
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
