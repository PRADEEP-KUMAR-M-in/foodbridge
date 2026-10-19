# Sets up the database connection and gives us a few shared helpers (the clock and the base class for tables).
import os  # lets us read environment variables (settings from outside the code)
from datetime import datetime, timezone  # datetime = a date + time, timezone = so we can ask for UTC
from sqlalchemy import create_engine, event  # create_engine opens the database, event lets us run code when it connects
from sqlalchemy.orm import sessionmaker, DeclarativeBase  # sessionmaker makes "conversations" with the DB, DeclarativeBase is the table parent class

# Where the database lives. If nobody sets DATABASE_URL we just use a local SQLite file called foodbridge.db.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///foodbridge.db")


def utcnow() -> datetime:
    """Current time in UTC with no timezone tag stuck on it (so SQLite and Postgres both stay chill)."""
    # Ask for the time in UTC, then peel off the timezone label so every time in the app looks the same.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    # Every table class in tables.py inherits from this, which is how SQLAlchemy knows they're tables.
    pass


def make_engine(url: str = DATABASE_URL):
    # Extra connection settings, empty unless the database needs special treatment.
    engine_args = {}
    if url.startswith("sqlite"):
        # SQLite: wait up to 30 seconds if the file is busy, and let different threads share the connection.
        engine_args["connect_args"] = {"timeout": 30, "check_same_thread": False}
    # Actually create the engine (the thing that talks to the database).
    db_engine = create_engine(url, **engine_args)
    if url.startswith("sqlite"):
        # Run this little function every time a new SQLite connection opens.
        @event.listens_for(db_engine, "connect")
        def _pragmas(conn, _):
            # WAL mode lets readers and a writer work at the same time without blocking each other.
            conn.execute("PRAGMA journal_mode=WAL")
            # SQLite ignores foreign keys unless you switch this on, so we switch it on.
            conn.execute("PRAGMA foreign_keys=ON")
    # Hand the ready-to-use engine back.
    return db_engine


# The one shared engine the whole app uses.
engine = make_engine()
# A factory that gives out database sessions; expire_on_commit=False keeps objects readable after we save.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(db_engine=None):
    # Importing tables makes sure every table class is registered before we create them.
    from . import tables  # noqa: F401
    # Create any tables that don't exist yet (it never touches ones that already exist).
    Base.metadata.create_all(db_engine or engine)
