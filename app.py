"""Local prototype for booking university group-study rooms."""
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOMS = {
    "A": {"name": "研讨室 A", "capacity": 4, "equipment": "白板"},
    "B": {"name": "研讨室 B", "capacity": 6, "equipment": "白板 · 屏幕"},
    "C": {"name": "研讨室 C", "capacity": 10, "equipment": "屏幕 · 视频会议"},
}
OPEN, CLOSE = 9 * 60, 21 * 60
STEP = 30


class BookingRequest(BaseModel):
    room: str
    day: date
    start: int
    end: int
    attendees: int = Field(ge=3, le=10)
    title: str = Field(min_length=1, max_length=40, pattern=r"\S")


class Cancellation(BaseModel):
    token: str = Field(min_length=32, max_length=32)


def clock():
    return datetime.now(SHANGHAI)


def check_day(day, now):
    if not 0 <= (day - now.date()).days < 7:
        raise HTTPException(422, "请选择今天起七天内的日期。")


def check_time(day, start, end, now):
    if not OPEN <= start < end <= CLOSE or start % STEP or end % STEP:
        raise HTTPException(422, "请选择开放时段内的半小时档位。")
    if end - start > 240:
        raise HTTPException(422, "每次最多预约四小时。")
    minutes_now = now.hour * 60 + now.minute
    if day == now.date() and start <= minutes_now:
        raise HTTPException(422, "开始时间须晚于当前时间。")


@contextmanager
def database(path, *, write=False):
    with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
        conn.row_factory = sqlite3.Row
        if write:
            conn.execute("begin immediate")
        yield conn


def create_app(db_path=None, now=clock):
    path = Path(db_path or os.environ.get("ROOM_BOOKING_DB", "runtime/rooms.sqlite")).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as conn:
        conn.executescript("""
            create table if not exists booking (
                token text primary key,
                room text not null,
                day text not null,
                start integer not null,
                end integer not null,
                attendees integer not null,
                title text not null,
                cancelled integer not null default 0
            );
            create index if not exists booking_availability on booking(room, day, cancelled);
        """)
    app = FastAPI(title="高校研讨空间预约原型")

    @app.get("/api/rooms")
    def rooms(day: date):
        now_at = now()
        check_day(day, now_at)
        with database(path) as conn:
            booked = conn.execute(
                "select room, start, end from booking where day = ? and cancelled = 0 order by start",
                (day.isoformat(),),
            ).fetchall()
        return {
            "today": now_at.date().isoformat(),
            "day": day.isoformat(), "open": OPEN, "close": CLOSE, "step": STEP,
            "not_before": now_at.hour * 60 + now_at.minute + 1 if day == now_at.date() else 0,
            "rooms": [
                {"id": room_id, **room, "reserved": [
                    [entry["start"], entry["end"]] for entry in booked if entry["room"] == room_id
                ]}
                for room_id, room in ROOMS.items()
            ],
        }

    @app.post("/api/bookings", status_code=201)
    def book(request: BookingRequest):
        room = ROOMS.get(request.room)
        if not room:
            raise HTTPException(404, "找不到研讨室。")
        if request.attendees > room["capacity"]:
            raise HTTPException(422, "参加人数超过研讨室容量。")
        with database(path, write=True) as conn:
            now_at = now()
            check_day(request.day, now_at)
            check_time(request.day, request.start, request.end, now_at)
            conflict = conn.execute(
                """select 1 from booking where room = ? and day = ? and cancelled = 0
                   and start < ? and end > ? limit 1""",
                (request.room, request.day.isoformat(), request.end, request.start),
            ).fetchone()
            if conflict:
                raise HTTPException(409, "这个时段已被预约，请刷新后选择其他时段。")
            token = secrets.token_urlsafe(24)
            conn.execute(
                "insert into booking(token, room, day, start, end, attendees, title) values (?, ?, ?, ?, ?, ?, ?)",
                (token, request.room, request.day.isoformat(), request.start, request.end,
                 request.attendees, request.title.strip()),
            )
        return {"token": token, **request.model_dump(mode="json")}

    @app.post("/api/bookings/cancel")
    def cancel(request: Cancellation):
        with database(path, write=True) as conn:
            booking = conn.execute(
                "select day, start, cancelled from booking where token = ?", (request.token,),
            ).fetchone()
            if booking is None:
                raise HTTPException(404, "找不到该预约。")
            if not booking["cancelled"]:
                now_at = now()
                today = now_at.date().isoformat()
                current = now_at.hour * 60 + now_at.minute
                if booking["day"] < today or (booking["day"] == today and booking["start"] <= current):
                    raise HTTPException(409, "预约开始后无法取消。")
                conn.execute("update booking set cancelled = 1 where token = ?", (request.token,))
        return {"status": "cancelled"}

    app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="demo")
    return app

