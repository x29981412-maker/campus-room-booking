"""Campus room reservations with separate student and operator workflows."""
import csv
import io
import os
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from store import (
    CLOSE,
    GRACE,
    OPEN,
    STEP,
    advance,
    booking_rows,
    booking_view,
    clock,
    conflicts,
    connect,
    days_from,
    init,
    log,
    minute,
    path_from_env,
    report,
    session_key,
)

SESSION = "campus_room_session"
ADMIN_SESSION = "campus_room_admin"


class BookingRequest(BaseModel):
    room: str
    day: date
    start: int
    end: int
    attendees: int = Field(ge=3, le=30)
    title: str = Field(min_length=1, max_length=40, pattern=r"\S")
    repeat: Literal["once", "daily"] = "once"
    occurrences: int = Field(default=1, ge=1, le=7)


class Cancellation(BaseModel):
    token: str = Field(min_length=32, max_length=32)


class AdminLogin(BaseModel):
    password: str = Field(max_length=300)


class RoomEdit(BaseModel):
    name: str = Field(min_length=1, max_length=30, pattern=r"\S")
    area: str = Field(min_length=1, max_length=30, pattern=r"\S")
    capacity: int = Field(ge=3, le=30)
    equipment: str = Field(max_length=80)
    approval: bool = False
    active: bool = True


class Closure(BaseModel):
    room: str
    day: date
    start: int
    end: int
    reason: str = Field(min_length=1, max_length=60, pattern=r"\S")


class Review(BaseModel):
    action: Literal["approve", "reject", "cancel"]
    note: str = Field(default="", max_length=100)


def check_day(day, now):
    if not 0 <= (day - now.date()).days < 7:
        raise HTTPException(422, "请选择今天起七天内的日期。")


def check_time(day, start, end, now, *, max_length=240):
    if not OPEN <= start < end <= CLOSE or start % STEP or end % STEP:
        raise HTTPException(422, "请选择开放时段内的半小时档位。")
    if end - start > max_length:
        raise HTTPException(422, "每次最多预约四小时。")
    if day == now.date() and start <= minute(now):
        raise HTTPException(422, "开始时间须晚于当前时间。")


def date_window(first, last):
    if not 0 <= (last - first).days <= 30:
        raise HTTPException(422, "日期范围最多31天。")
    return first.isoformat(), last.isoformat()


def booking_days(request, now):
    if request.repeat == "once" and request.occurrences != 1:
        raise HTTPException(422, "单次预约只能提交一次。")
    days = days_from(request.day, request.occurrences)
    # Students always obey the published seven-day horizon.
    for day in days:
        check_day(day, now)
        check_time(day, request.start, request.end, now)
    return days


def create_app(db_path=None, now=clock, admin_password=None):
    path = Path(db_path).resolve() if db_path else path_from_env()
    init(path)
    app = FastAPI(title="高校研讨空间预约", version="2.0")
    password = os.environ.get("ROOM_BOOKING_ADMIN_PASSWORD") if admin_password is None else admin_password

    def current_session(request: Request):
        with connect(path) as conn:
            current = int(now().timestamp())
            for cookie, role in ((ADMIN_SESSION, "admin"), (SESSION, "student")):
                key = request.cookies.get(cookie)
                if key and (role == "student" or password):
                    row = conn.execute("select key,role from sessions where key=? and role=? and expires>?",
                                       (key, role, current)).fetchone()
                    if row:
                        return dict(row)
        return None

    session_dep = Depends(current_session)

    def admin(session=session_dep):
        if session is None or session["role"] != "admin":
            raise HTTPException(403, "请先登录管理员工作台。")
        return session

    def student(session=session_dep):
        if session is None or session["role"] != "student":
            raise HTTPException(403, "请先进入学生预约。")
        return session

    admin_dep = Depends(admin)
    student_dep = Depends(student)

    def set_session(response, role, old=None):
        key = session_key()
        with connect(path, write=True) as conn:
            expiry = int(now().timestamp()) + 7 * 86400
            conn.execute("delete from sessions where expires<=?", (int(now().timestamp()),))
            if old and old["role"] == "admin":
                conn.execute("delete from sessions where key=?", (old["key"],))
            conn.execute("insert into sessions values(?,?,?)", (key, role, expiry))
        cookie = ADMIN_SESSION if role == "admin" else SESSION
        response.set_cookie(cookie, key, max_age=7 * 86400, httponly=True, samesite="strict")
        return {"role": role}

    @app.get("/api/session")
    def session_info(session=session_dep):
        return {"role": session["role"] if session else None, "admin_enabled": bool(password)}

    @app.post("/api/session/student")
    def start_student(response: Response, session=session_dep):
        if session:
            if session["role"] == "student":
                return {"role": "student"}
            raise HTTPException(409, "请退出管理员后再进入学生预约。")
        return set_session(response, "student")

    @app.post("/api/session/admin")
    def admin_login(body: AdminLogin, response: Response, session=session_dep):
        if not password:
            raise HTTPException(503, "请在启动前设置管理员口令。")
        if not secrets.compare_digest(body.password.encode("utf-8"), password.encode("utf-8")):
            raise HTTPException(403, "管理员口令不正确。")
        return set_session(response, "admin", session)

    @app.post("/api/session/logout")
    def logout(response: Response, session=session_dep):
        if session and session["role"] == "admin":
            with connect(path, write=True) as conn:
                conn.execute("delete from sessions where key=?", (session["key"],))
        response.delete_cookie(ADMIN_SESSION)
        return {"ok": True}

    @app.get("/api/rooms")
    def rooms(day: date):
        current = now()
        check_day(day, current)
        day_key = day.isoformat()
        with connect(path, write=True) as conn:
            advance(conn, current)
            listed = conn.execute("select * from rooms where active=1 order by area,id").fetchall()
            busy = conn.execute(
                "select room,start,end,status from booking where day=? "
                "and status in ('pending','confirmed','checked_in','completed') order by start",
                (day_key,),
            ).fetchall()
            closed = conn.execute("select room,start,end from closures where day=? order by start",
                                  (day_key,)).fetchall()
        return {
            "today": current.date().isoformat(), "day": day.isoformat(),
            "open": OPEN, "close": CLOSE, "step": STEP,
            "not_before": minute(current) + 1 if day == current.date() else 0,
            "rooms": [
                {**dict(room),
                 "reserved": [[row["start"], row["end"]] for row in busy if row["room"] == room["id"]],
                 "closed": [[row["start"], row["end"]] for row in closed if row["room"] == room["id"]]}
                for room in listed
            ],
        }

    @app.get("/api/calendar")
    def calendar(day: date, view: Literal["week", "month"] = "week"):
        if view == "month":
            first = day.replace(day=1)
            count = ((first + timedelta(days=32)).replace(day=1) - first).days
        else:
            first, count = day - timedelta(days=day.weekday()), 7
        days = days_from(first, count)
        with connect(path, write=True) as conn:
            advance(conn, now())
            rows = conn.execute(
                """select day,count(*) as count from booking join rooms on rooms.id=booking.room
                   where day between ? and ? and status in ('confirmed','checked_in','pending','completed')
                   and rooms.active=1 group by day""", (days[0].isoformat(), days[-1].isoformat()),
            )
            counts = {r["day"]: r["count"] for r in rows}
        return {"days": [{"date": item.isoformat(), "booked": counts.get(item.isoformat(), 0)} for item in days]}

    @app.post("/api/bookings", status_code=201)
    def book(body: BookingRequest, session=session_dep):
        if session and session["role"] == "admin":
            raise HTTPException(403, "请在学生页面提交预约。")
        owner = session["key"] if session else None
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            room = conn.execute("select * from rooms where id=? and active=1", (body.room,)).fetchone()
            if not room:
                raise HTTPException(404, "找不到可预约的研讨室。")
            if body.attendees > room["capacity"]:
                raise HTTPException(422, "参加人数超过研讨室容量。")
            days = booking_days(body, current)
            for day in days:
                if conflicts(conn, body.room, day, body.start, body.end):
                    raise HTTPException(409, f"{day.isoformat()} 的这个时段已被占用。")
            state = "pending" if room["approval"] else "confirmed"
            series = secrets.token_urlsafe(8) if len(days) > 1 else None
            result = []
            for day in days:
                token = secrets.token_urlsafe(24)
                conn.execute(
                    """insert into booking(token,room,day,start,end,attendees,title,status,owner,series)
                       values(?,?,?,?,?,?,?,?,?,?)""",
                    (token, body.room, day.isoformat(), body.start, body.end, body.attendees,
                     body.title.strip(), state, owner, series),
                )
                log(conn, current, "学生", "created", token[:8], room["name"])
                result.append({"token": token, "day": day.isoformat(), "status": state})
        return {**body.model_dump(mode="json"), **result[0], "bookings": result}

    @app.get("/api/bookings/mine")
    def mine(session=student_dep):
        with connect(path, write=True) as conn:
            advance(conn, now())
            return [booking_view(row, own=True) for row in booking_rows(conn, "where b.owner=?", (session["key"],))]

    @app.post("/api/bookings/cancel")
    def cancel(body: Cancellation, session=session_dep):
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            row = conn.execute("select owner,day,start,status from booking where token=?", (body.token,)).fetchone()
            if not row or (row["owner"] and (not session or session["key"] != row["owner"])):
                raise HTTPException(404, "找不到该预约。")
            if row["status"] == "cancelled":
                return {"status": "cancelled"}
            if row["status"] not in {"pending", "confirmed"} or (
                row["day"] == current.date().isoformat() and row["start"] <= minute(current)
            ):
                raise HTTPException(409, "当前状态无法取消。")
            conn.execute("update booking set status='cancelled', cancelled=1 where token=?", (body.token,))
            log(conn, current, "学生", "cancelled", body.token[:8])
        return {"status": "cancelled"}

    @app.post("/api/bookings/check-in")
    def check_in(body: Cancellation, session=student_dep):
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            row = conn.execute("select * from booking where token=? and owner=?",
                               (body.token, session["key"])).fetchone()
            if row is None:
                raise HTTPException(404, "找不到该预约。")
            if row["status"] == "checked_in":
                return {"status": "checked_in"}
            if row["status"] != "confirmed" or row["day"] != current.date().isoformat() or not (
                row["start"] - 10 <= minute(current) < min(row["end"], row["start"] + GRACE)
            ):
                raise HTTPException(409, "可在开始前10分钟至开始后15分钟内签到。")
            conn.execute("update booking set status='checked_in' where token=?", (body.token,))
            log(conn, current, "学生", "checked_in", body.token[:8])
        return {"status": "checked_in"}

    @app.get("/api/admin/rooms")
    def manage_rooms(_=admin_dep):
        with connect(path) as conn:
            return [dict(row) for row in conn.execute("select * from rooms order by area,id")]

    @app.put("/api/admin/rooms/{room}")
    def manage_room(room: str, body: RoomEdit, _=admin_dep):
        if not room.isascii() or not room.isalnum() or len(room) > 8:
            raise HTTPException(422, "房间编号请使用1至8位英文字母或数字。")
        room = room.upper()
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            count = conn.execute(
                """select count(*) from booking where room=? and day>=? and status in ('confirmed','pending','checked_in')
                   and (attendees>? or ?=0)""",
                (room, current.date().isoformat(), body.capacity, body.active),
            ).fetchone()[0]
            if count:
                raise HTTPException(409, "现有预约受影响，请先处理这些预约。")
            conn.execute(
                """insert into rooms values(?,?,?,?,?,?,?) on conflict(id) do update set name=excluded.name,
                   area=excluded.area, capacity=excluded.capacity,equipment=excluded.equipment,
                   approval=excluded.approval,active=excluded.active""",
                (room, body.name.strip(), body.area.strip(), body.capacity, body.equipment.strip(),
                 body.approval, body.active),
            )
            log(conn, current, "管理员", "room_saved", room, body.name.strip())
        return {"id": room}

    @app.get("/api/admin/bookings")
    def manage_bookings(first: date, last: date, _=admin_dep):
        window = date_window(first, last)
        with connect(path, write=True) as conn:
            advance(conn, now())
            return [booking_view(row) for row in booking_rows(conn, "where b.day between ? and ?", window)]

    @app.post("/api/admin/bookings/{booking_id}")
    def review(booking_id: int, body: Review, _=admin_dep):
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            row = conn.execute("select status from booking where rowid=?", (booking_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "找不到该预约。")
            allowed = {"approve": {"pending"}, "reject": {"pending"}, "cancel": {"confirmed"}}
            if row["status"] not in allowed[body.action]:
                raise HTTPException(409, "该预约状态已变化，请刷新。")
            state = {"approve": "confirmed", "reject": "rejected", "cancel": "cancelled"}[body.action]
            conn.execute("update booking set status=?, decision=?, cancelled=? where rowid=?",
                         (state, body.note.strip(), state == "cancelled", booking_id))
            log(conn, current, "管理员", state, str(booking_id), body.note.strip())
        return {"status": state}

    @app.get("/api/admin/closures")
    def closures(first: date, last: date, _=admin_dep):
        window = date_window(first, last)
        with connect(path) as conn:
            return [dict(row) for row in conn.execute(
                "select * from closures where day between ? and ? order by day,start,id", window,
            )]

    @app.post("/api/admin/closures", status_code=201)
    def close_room(body: Closure, _=admin_dep):
        with connect(path, write=True) as conn:
            current = now()
            advance(conn, current)
            if not 0 <= (body.day - current.date()).days < 31:
                raise HTTPException(422, "可封闭今天起31天内的时段。")
            check_time(body.day, body.start, body.end, current, max_length=CLOSE - OPEN)
            if not conn.execute("select 1 from rooms where id=? and active=1", (body.room,)).fetchone():
                raise HTTPException(404, "找不到该房间。")
            if conflicts(conn, body.room, body.day, body.start, body.end):
                raise HTTPException(409, "时段已有预约或封闭计划。")
            cursor = conn.execute("insert into closures(room,day,start,end,reason) values(?,?,?,?,?)",
                                  (body.room, body.day.isoformat(), body.start, body.end, body.reason.strip()))
            log(conn, current, "管理员", "closure_created", str(cursor.lastrowid), body.reason.strip())
        return {"id": cursor.lastrowid}

    @app.delete("/api/admin/closures/{closure_id}")
    def reopen_room(closure_id: int, _=admin_dep):
        with connect(path, write=True) as conn:
            cursor = conn.execute("delete from closures where id=?", (closure_id,))
            if not cursor.rowcount:
                raise HTTPException(404, "找不到该封闭计划。")
            log(conn, now(), "管理员", "closure_removed", str(closure_id))
        return {"ok": True}

    @app.get("/api/admin/report")
    def get_report(first: date, last: date, format: Literal["json", "csv"] = "json", _=admin_dep):
        date_window(first, last)
        with connect(path, write=True) as conn:
            advance(conn, now())
            result = report(conn, first, last)
        if format == "json":
            return result
        data = io.StringIO()
        if result["rooms"]:
            writer = csv.DictWriter(data, fieldnames=list(result["rooms"][0]))
            writer.writeheader()
            for row in result["rooms"]:
                writer.writerow({
                    key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                    else value for key, value in row.items()
                })
        return StreamingResponse(iter(["\ufeff" + data.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=room-report.csv"})

    @app.get("/api/admin/audit")
    def get_audit(limit: int = Query(default=40, ge=1, le=200), _=admin_dep):
        with connect(path) as conn:
            return [dict(row) for row in conn.execute("select * from audit order by id desc limit ?", (limit,))]

    app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="site")
    return app


