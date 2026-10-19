"""Resets the local database back to the exact demo state:  python reset_demo.py
DEMO PASSWORDS ONLY, these are fictional accounts. Never reuse them anywhere real."""
import os  # to check for files, delete them, and read settings
from app.database import init_db, SessionLocal  # create the tables, and open a database session
from app.demo_data import seed_demo  # fills the database with the fictional demo accounts and listings

# Delete the old database files (the main file plus SQLite's two helper files) so we start from a totally clean slate.
for file_name in ("foodbridge.db", "foodbridge.db-wal", "foodbridge.db-shm"):
    if os.path.exists(file_name):
        os.remove(file_name)
# Create fresh, empty tables.
init_db()
# The admin password: from the environment if set, otherwise the public demo one.
admin_password = os.getenv("FOODBRIDGE_ADMIN_PASSWORD", "demo-admin-123")
# Open a session and fill in the demo data.
with SessionLocal() as db:
    seeded = seed_demo(db, admin_password)
# Tell the person what was created and how to log in.
print(f"Seeded demo data (donation id {seeded['donation_id']}). Logins: admin / {admin_password} ; bakery, shelter-a, shelter-b, kitchen-c / demo-pass-123")
