#!/usr/bin/env python3
"""Create a fresh local trial configuration; never overwrite existing settings."""
import os
from pathlib import Path
import secrets


def main():
    destination = Path(__file__).resolve().parents[1] / ".env"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env already exists; no settings were changed.")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("MUSE2API_KEY=m2a_" + secrets.token_hex(24) + "\n")
        stream.write("MUSE_BACKEND=protocol\nMUSE_BIND_ADDRESS=127.0.0.1\nMUSE2API_CONCURRENCY=1\n")
    print("Created .env for a separate local trial. The API key was saved there and was not printed.")


if __name__ == "__main__":
    main()
