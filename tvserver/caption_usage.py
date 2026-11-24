"""Persistent caption API accounting, without audio, transcripts or credentials."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from . import db


ZONE = ZoneInfo("Europe/Prague")
NANOS = 1_000_000_000


def _cost_nanos(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        units = int((amount * NANOS).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return units if units <= 2**63 - 1 else None
    except (ValueError, InvalidOperation, OverflowError):
        return None


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63 else None


class CaptionUsage:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        if db.get_meta(con, "caption_usage_since") is None:
            db.set_meta(con, "caption_usage_since", db.now())
        # A server restart cannot establish what the provider charged for an
        # unfinished request. Keep its missing price visible in the totals.
        con.execute("UPDATE caption_api_usage SET status='interrupted', finished_at=? WHERE status='pending'", (db.now(),))
        con.commit()

    def start(self, model: str, channel_id: int | None = None) -> str:
        row = self.con.execute("SELECT name FROM channels WHERE id=?", (channel_id,)).fetchone()
        request_id = uuid.uuid4().hex
        self.con.execute("INSERT INTO caption_api_usage (id, started_at, channel_id, channel_name, model) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (request_id, db.now(), channel_id, row["name"] if row else "", model))
        self.con.commit()
        return request_id

    def finish(self, request_id, *, status, cost_usd=None, input_tokens=None, output_tokens=None,
               generation_id=None, http_status=None):
        if status not in {"success", "rejected", "http_error", "error", "timeout", "cancelled"}:
            raise ValueError("Invalid caption usage status")
        generation_id = generation_id[:200] if isinstance(generation_id, str) else None
        self.con.execute(
            "UPDATE caption_api_usage SET finished_at=?, status=?, cost_nanos=?, input_tokens=?, output_tokens=?, "
            "generation_id=?, http_status=? WHERE id=? AND status='pending'",
            (db.now(), status, _cost_nanos(cost_usd), _tokens(input_tokens), _tokens(output_tokens),
             generation_id, http_status, request_id))
        self.con.commit()

    @staticmethod
    def _stats(row):
        return {"requests": row["requests"], "known_cost_usd": row["cost_nanos"] / NANOS,
                "known_cost_requests": row["known"], "unknown_cost_requests": row["unknown"],
                "pending_requests": row["pending"], "failed_requests": row["failed"],
                "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}

    def summary(self, at: datetime | None = None):
        at = at or datetime.now(timezone.utc)
        local = at.astimezone(ZONE)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        month = midnight.replace(day=1)
        tomorrow = midnight + timedelta(days=1)
        next_month = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
        aggregates = (
            "COUNT(*) AS requests, COALESCE(SUM(cost_nanos),0) AS cost_nanos, "
            "COALESCE(SUM(cost_nanos IS NOT NULL),0) AS known, "
            "COALESCE(SUM(cost_nanos IS NULL AND status!='pending'),0) AS unknown, "
            "COALESCE(SUM(status='pending'),0) AS pending, "
            "COALESCE(SUM(status NOT IN ('success','pending')),0) AS failed, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens"
        )
        periods = {}
        for key, since, until in [("today", midnight, tomorrow), ("month", month, next_month), ("total", None, None)]:
            where = " WHERE started_at>=? AND started_at<?" if since else ""
            args = tuple(value.astimezone(timezone.utc).isoformat(timespec="microseconds") for value in (since, until)) if since else ()
            row = self.con.execute("SELECT " + aggregates + " FROM caption_api_usage" + where, args).fetchone()
            periods[key] = self._stats(row)
        by_channel = []
        for row in self.con.execute(
            "SELECT channel_id, COALESCE((SELECT name FROM channels WHERE id=u.channel_id), MAX(channel_name)) AS name, "
            + aggregates + " FROM caption_api_usage u GROUP BY channel_id ORDER BY cost_nanos DESC, requests DESC LIMIT 50"):
            by_channel.append({"channel_id": row["channel_id"], "channel_name": row["name"] or "Neznámý kanál",
                               **self._stats(row)})
        return {"currency": "USD", "timezone": str(ZONE), "since": db.get_meta(self.con, "caption_usage_since"),
                "updated_at": at.astimezone(timezone.utc).isoformat(), **periods, "by_channel": by_channel}
