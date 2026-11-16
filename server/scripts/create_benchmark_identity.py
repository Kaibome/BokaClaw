"""Create a dedicated benchmark user and API key.

Run inside the server container:

    docker exec bokaclaw-server python scripts/create_benchmark_identity.py \
        --email gaia-benchmark@example.invalid --name "GAIA Benchmark"

The plaintext API key is printed once. It is not stored in the clear.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.auth import generate_api_key, hash_password  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import APIKey, User  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="GAIA Benchmark")
    parser.add_argument("--label", default="gaia-benchmark")
    parser.add_argument("--password", default=None, help="Optional password; random if omitted.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    password = args.password or secrets.token_urlsafe(24)
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == args.email).one_or_none()
        if user is None:
            user = User(
                name=args.name,
                email=args.email,
                password_hash=hash_password(password),
                role="admin",
            )
            db.add(user)
            db.flush()
        elif user.role != "admin":
            user.role = "admin"
        full_key, prefix, key_hash = generate_api_key()
        db.add(APIKey(key_prefix=prefix, key_hash=key_hash, label=args.label, created_by=user.id))
        db.commit()
        sys.stdout.write(f"GAIA_USER_ID={user.id}\n")
        sys.stdout.write(f"GAIA_API_KEY={full_key}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
