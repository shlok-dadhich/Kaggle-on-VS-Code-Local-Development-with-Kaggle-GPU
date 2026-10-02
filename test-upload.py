import base64
import sys
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests


url = sys.argv[1]

parsed = urlsplit(url)

parts = [
    p for p in parsed.path.split("/")
    if p
]

token = parts[2]

base_url = (
    f"{parsed.scheme}://{parsed.netloc}"
    f"{parsed.path.rstrip('/')}"
)

session = requests.Session()

session.headers.update({
    "Authorization": f"token {token}"
})


def api(path):

    encoded = "/".join(
        quote(part, safe="")
        for part in path.strip("/").split("/")
    )

    return f"{base_url}/api/contents/{encoded}"


print("Testing Kaggle Contents API...")
print()

# ------------------------------------------------------------
# Create directory
# ------------------------------------------------------------

directory = "local-project"

response = session.put(
    api(directory),
    json={
        "type": "directory"
    },
    timeout=30
)

print(
    "Create directory:",
    response.status_code
)

if response.status_code not in (200, 201):
    print(response.text)
    response.raise_for_status()


# ------------------------------------------------------------
# Create test file
# ------------------------------------------------------------

content = "HELLO FROM WINDOWS SYNC\n"

encoded = base64.b64encode(
    content.encode("utf-8")
).decode("ascii")


response = session.put(
    api("local-project/sync_test.txt"),
    json={
        "type": "file",
        "format": "base64",
        "content": encoded
    },
    timeout=30
)

print(
    "Upload file:",
    response.status_code
)

if response.status_code not in (200, 201):
    print(response.text)
    response.raise_for_status()


# ------------------------------------------------------------
# Read file back
# ------------------------------------------------------------

response = session.get(
    api("local-project/sync_test.txt"),
    timeout=30
)

print(
    "Read file:",
    response.status_code
)

response.raise_for_status()

data = response.json()

print()
print("Remote file:")
print(data.get("name"))

print()
print("Remote content:")

import base64

content = data["content"]

if data.get("format") == "base64":
    decoded = base64.b64decode(content).decode("utf-8")
else:
    decoded = content


print(decoded)

print()
print("SUCCESS")