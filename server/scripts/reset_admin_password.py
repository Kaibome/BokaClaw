"""Reset a user's password by email. Run inside the `bokaclaw-server` container."""

import os
import sys

from sqlalchemy import select

# Allow `python scripts/<name>.py` from a checkout without PYTHONPATH.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


from app.core.auth import hash_password  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402

MIN_PASSWORD_LENGTH = 8


def main() -> int:
    email = os.environ.get("EMAIL", "").strip()
    password = os.environ.get("PASSWORD", "")

    if not email or not password:
        print("error: EMAIL and PASSWORD environment variables are required.", file=sys.stderr)
        return 2

    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"error: password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"error: no user found with email {email}.", file=sys.stderr)
            return 1

        user.password_hash = hash_password(password)
        db.commit()

    print(f"Password reset for {email}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
