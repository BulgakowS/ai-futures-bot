import base64
import os
import secrets
from pathlib import Path

path = Path(".env")
if not path.exists():
    password = secrets.token_hex(24)
    content = (
        Path(".env.example")
        .read_text()
        .replace(
            "BACKEND_AUTH_TOKEN=generate-with-make-setup",
            f"BACKEND_AUTH_TOKEN={secrets.token_urlsafe(48)}",
        )
        .replace("generate-with-make-setup", password)
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    print("Created .env with local bootstrap secrets (mode 0600)")
else:
    print("Preserving existing .env")

vault_path = Path(".env.credentials")
if not vault_path.exists():
    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fd = os.open(vault_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"CREDENTIAL_MASTER_KEY={master_key}\n")
    print("Created .env.credentials with local vault key (mode 0600)")
else:
    print("Preserving existing .env.credentials")
