"""Install or remove synthetic history in a local OnTime test database."""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta

from app import WIB, db, route_key, app


DEMO_ROUTE = {
    "origin": [-6.88, 107.62],
    "destination": [-6.89, 107.61],
    "vehicle": "motorcycle",
}
TAG = "ontime-prediction-demo-v1"
DISABLED_PASSWORD = "!ontime-prediction-demo"
DEMO_EMAILS = tuple(f"prediction-demo-{n}@example.invalid" for n in range(1, 4))


def replace_history(mode, email=None):
    """Replace only this fixture's rows and return the matching prediction body."""
    tomorrow = datetime.now(WIB) + timedelta(days=1)
    target = tomorrow.replace(hour=7, minute=0, second=0, microsecond=0)

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if mode == "personal":
            owner = conn.execute(
                "SELECT id FROM users WHERE email=? AND password<>?", (email, DISABLED_PASSWORD)
            ).fetchone()
            if owner is None:
                raise ValueError(f"Register {email} on this backend first.")

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
        if "demo_tag" not in columns:
            if mode == "clean":
                return 0, None
            conn.execute("ALTER TABLE trips ADD COLUMN demo_tag TEXT")
        removed = conn.execute("DELETE FROM trips WHERE demo_tag=?", (TAG,)).rowcount
        conn.executemany(
            """DELETE FROM users WHERE email=? AND password=?
               AND NOT EXISTS (SELECT 1 FROM trips WHERE owner=users.id)
               AND NOT EXISTS (SELECT 1 FROM plans WHERE owner=users.id)
               AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE owner=users.id)""",
            [(address, DISABLED_PASSWORD) for address in DEMO_EMAILS],
        )
        if mode == "clean":
            return removed, None

        if mode == "personal":
            owners = [owner["id"]]
        else:
            owners = [
                conn.execute("INSERT INTO users(email,password) VALUES(?,?)",
                             (address, DISABLED_PASSWORD)).lastrowid
                for address in DEMO_EMAILS
            ]

        key = route_key(DEMO_ROUTE)
        rows = []
        for offset in range(12, -1, -1):
            slot = target - timedelta(minutes=15 * offset)
            for week, variation in enumerate((-2, 1, 0, 4, -1), start=1):
                started = int((slot - timedelta(weeks=week)).timestamp())
                minutes = (22 if mode == "collective" else 17) + variation
                owner_id = owners[(week - 1) % len(owners)]
                rows.append((owner_id, key, started, started + minutes * 60, TAG))
        conn.executemany(
            "INSERT INTO trips(owner,route,started,ended,demo_tag) VALUES(?,?,?,?,?)", rows
        )
    return len(rows), dict(DEMO_ROUTE, targetArrival=target.isoformat())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collective", "personal", "clean"))
    parser.add_argument("--email", help="Registered account email for personal mode")
    args = parser.parse_args()
    if args.mode == "personal" and not args.email:
        parser.error("personal requires --email")

    with app.app_context():
        try:
            count, body = replace_history(args.mode, args.email.strip().lower() if args.email else None)
        except (ValueError, sqlite3.IntegrityError) as error:
            parser.exit(1, f"Demo seed failed: {error}\n")
    if body is None:
        print(f"Removed {count} synthetic trips.")
    else:
        print(f"Installed {count} synthetic trips ({args.mode}). POST /api/predict with:")
        print(json.dumps(body, indent=2))


if __name__ == "__main__":
    main()
