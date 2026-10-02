import sys
from urllib.parse import urlsplit

import requests


url = sys.argv[1]

print()
print("=" * 60)
print("KAGGLE URL DIAGNOSTIC")
print("=" * 60)

parsed = urlsplit(url)

print()
print("Scheme:")
print(parsed.scheme)

print()
print("Host:")
print(parsed.netloc)

print()
print("Path:")
print(parsed.path)

parts = [
    p for p in parsed.path.split("/")
    if p
]

print()
print("Path parts:")
for i, part in enumerate(parts):
    print(i, repr(part))

if len(parts) >= 4:

    token = parts[2]

    base = (
        f"{parsed.scheme}://{parsed.netloc}"
        f"{parsed.path.rstrip('/')}"
    )

    print()
    print("Session:")
    print(parts[1])

    print()
    print("Token length:")
    print(len(token))

    print()
    print("Proxy base:")
    print(base)

    print()
    print("Testing /api...")

    response = requests.get(
        f"{base}/api",
        headers={
            "Authorization": f"token {token}"
        },
        timeout=30,
    )

    print()
    print("HTTP status:")
    print(response.status_code)

    print()
    print("Response:")
    print(response.text[:1000])

else:

    print()
    print("URL format was not recognized.")