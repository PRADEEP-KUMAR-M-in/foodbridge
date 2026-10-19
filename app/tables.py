# The database tables: one Python class = one table, and each line inside = one column.
from datetime import datetime  # type hint for date/time columns
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Index, text  # the column types and helpers we need
from sqlalchemy.orm import Mapped, mapped_column  # how we describe columns with type hints
from .database import Base, utcnow  # Base = parent of all tables, utcnow = default clock for "created at"


class Organisation(Base):
    # Anyone who uses the app: donors, recipients and the admin are all "organisations".
    __tablename__ = "organisations"
    id: Mapped[int] = mapped_column(primary_key=True)  # unique number for each organisation
    name: Mapped[str] = mapped_column(String(200))  # display name
    role: Mapped[str] = mapped_column(String(20))            # one of: donor | recipient | admin
    username: Mapped[str | None] = mapped_column(String(60), unique=True, nullable=True)  # login name, nobody else can have the same one
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)  # the scrambled password (never the real one)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # has the admin approved them yet?
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # switched off = can't log in
    lat: Mapped[float] = mapped_column(Float)  # latitude (where they are)
    lon: Mapped[float] = mapped_column(Float)  # longitude (where they are)
    accepts_categories: Mapped[str] = mapped_column(String(500), default="")   # comma-separated list
    storage_available: Mapped[str] = mapped_column(String(200), default="")    # comma-separated list
    capacity_remaining: Mapped[int] = mapped_column(Integer, default=0)  # how many more units they can take
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # how many claims they've made
    completed: Mapped[int] = mapped_column(Integer, default=0)  # how many of those ended in a finished handover
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # when they last got food (used for fair-share)


class Donation(Base):
    # One listing of surplus food.
    __tablename__ = "donations"
    id: Mapped[int] = mapped_column(primary_key=True)  # unique number for the listing
    donor_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))  # who posted it
    category: Mapped[str] = mapped_column(String(100))  # type of food, e.g. packaged_bakery
    storage: Mapped[str] = mapped_column(String(50))  # how it's stored, e.g. ambient
    quantity: Mapped[int] = mapped_column(Integer)  # number of packs/units
    prepared_at: Mapped[datetime] = mapped_column(DateTime)  # when the food was made/packed
    donor_deadline: Mapped[datetime] = mapped_column(DateTime)  # the latest time the donor said it can be collected
    latest_collect: Mapped[datetime] = mapped_column(DateTime)  # the real deadline after the safe-window rules were applied
    status: Mapped[str] = mapped_column(String(20), default="published")
    # where the listing is at: published | reserved | collected | rejected | expired | frozen | discarded
    offered_to: Mapped[int | None] = mapped_column(Integer, nullable=True)        # who has first dibs right now
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # when that first-dibs turn runs out
    tried_ids: Mapped[str] = mapped_column(String(500), default="")               # comma-separated ids of recipients who already got an offer / let it expire
    declaration: Mapped[str] = mapped_column(String(300), default="")             # note from the donor: ingredients / allergens / packaging
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # when the listing was created


class Claim(Base):
    # A recipient calling dibs on a listing.
    __tablename__ = "claims"
    id: Mapped[int] = mapped_column(primary_key=True)  # unique number for the claim
    donation_id: Mapped[int] = mapped_column(ForeignKey("donations.id"))  # which listing it's for
    recipient_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))  # who made the claim
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    # where the claim is at: reserved | completed | rejected | expired | frozen | closed
    expires_at: Mapped[datetime] = mapped_column(DateTime)  # the claim lapses if nothing happens by this time
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # when the donor confirmed the pickup code
    accepted_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)  # how many units the recipient actually accepted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # when the claim was made

    __table_args__ = (
        # the DATABASE (not our code) makes sure there's only one active claim per listing, so no double booking
        Index("one_active_claim", "donation_id", unique=True,
              sqlite_where=text("status = 'reserved'"),
              postgresql_where=text("status = 'reserved'")),
    )


class AuditEvent(Base):
    # One entry in the tamper-evident log (see proof_chain.py).
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)  # position in the log
    ts: Mapped[str] = mapped_column(String(40))  # timestamp saved as text
    type: Mapped[str] = mapped_column(String(50))  # what happened, e.g. CLAIM_RESERVED
    payload: Mapped[str] = mapped_column(String)             # the event details, saved as neat sorted JSON
    prev_hash: Mapped[str] = mapped_column(String(64), unique=True)  # stops the chain from splitting in two
    hash: Mapped[str] = mapped_column(String(64))  # this event's own fingerprint


class Incident(Base):
    # A "something's wrong with this food" report.
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)  # unique number for the report
    donation_id: Mapped[int] = mapped_column(ForeignKey("donations.id"))  # which listing it's about
    reporter_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))  # who reported it
    note: Mapped[str] = mapped_column(String(1000))  # what they wrote
    status: Mapped[str] = mapped_column(String(20), default="open")        # where the report is at: open | resolved
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)   # what the admin decided: release | discard
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)  # the admin's explanation
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # when it was reported
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # when the admin dealt with it
