"""Solve InfinityFree anti-bot challenge and execute db/install.php to initialize MySQL tables."""

import http.cookiejar
import re
import urllib.request
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE_URL = "http://elsamanii.rf.gd"
INSTALL_URL = f"{BASE_URL}/db/install.php"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def run():
    print(f"Fetching initial challenge from {INSTALL_URL}...")
    req = urllib.request.Request(INSTALL_URL, headers=headers)
    html = urllib.request.urlopen(req).read().decode("utf-8")

    # Extract hex tokens
    match_a = re.search(r'var a=toNumbers\("([a-f0-9]+)"\)', html)
    match_b = re.search(r'b=toNumbers\("([a-f0-9]+)"\)', html)
    match_c = re.search(r'c=toNumbers\("([a-f0-9]+)"\)', html)

    if not (match_a and match_b and match_c):
        print("No AES challenge found, response may already be plain:")
        print(html[:500])
        return

    a_hex = match_a.group(1)
    b_hex = match_b.group(1)
    c_hex = match_c.group(1)

    key = bytes.fromhex(a_hex)
    iv = bytes.fromhex(b_hex)
    ciphertext = bytes.fromhex(c_hex)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(ciphertext) + decryptor.finalize()
    test_cookie = decrypted.hex()

    print(f"Calculated __test cookie: {test_cookie}")

    # Execute install.php with the verified __test cookie
    req2 = urllib.request.Request(
        f"{INSTALL_URL}?i=1",
        headers={
            **headers,
            "Cookie": f"__test={test_cookie}",
        },
    )

    resp2 = urllib.request.urlopen(req2)
    output = resp2.read().decode("utf-8", errors="ignore")

    print("\n============================================================")
    print("  INFINITYFREE DATABASE INSTALLATION OUTPUT:")
    print("============================================================")
    print(output)
    print("============================================================\n")


if __name__ == "__main__":
    run()
