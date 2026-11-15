import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
OPEN, CLOSE, STEP = 540, 1260, 30
GRACE = 15

SCHEMA = """
create table if not exists booking (
    token text primary key, room text not null, day text not null,
    start integer not null, end integer not null, attendees integer not null,
    title text not null, cancelled integer not null default 0
);
create table if not exists rooms (
    id text primary key, name text not null, area text not null,
    capacity integer not null, equipment text not null, approval integer not null,
    active integer not null default 1
);
create table if not exists closures (
    id integer primary key, room text not null, day text not null,
    start integer not null, end integer not null, reason text not null,
    foreign key (room) references rooms(id)
);
create table if not exists sessions (
    key text primary key, role text not null, expires integer not null
);
create table if not exists audit (
    id integer primary key, at text not null, actor text not null,
    action text not null, subject text not null, detail text not null
);
"""
DEFAULTS = (
    ("A", "梧桐研讨室", "东区 · 7F", 4, "白板", 0),
    ("B", "思源研讨室", "东区 · 7F", 6, "白板、显示屏", 0),
    ("C", "协作会议室", "西区 · 7F", 10, "显示屏、视频会议", 1),
)


def clock():
    return datetime.now(SHANGHAI)


def minute(now):
    return now.hour * 60 + now.minute


def fmt(value):
    return f"{value // 60:02}:{value % 60:02}"


@contextmanager
def connect(path, *, write=False):
    with closing(sqlite3.connect(path, timeout=8)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        if write:
            conn.execute("begin immediate")
        yield conn


def init(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute("pragma journal_mode=wal")
        conn.executescript(SCHEMA)
    with connect(path, write=True) as conn:
        columns = {item["name"] for item in conn.execute("pragma table_info(booking)")}
        for name, sql in {
            "status": "text not null default 'confirmed'",
            "owner": "text",
            "series": "text",
            "decision": "text",
        }.items():
            if name not in columns:
                conn.execute(f"alter table booking add column {name} {sql}")
        if "status" not in columns:
            conn.execute("update booking set status='cancelled' where cancelled=1")
        conn.executemany(
            "insert or ignore into rooms(id,name,area,capacity,equipment,approval) values(?,?,?,?,?,?)",
            DEFAULTS,
        )
        conn.executescript("""
        create index if not exists booking_window on booking(room,day,status,start,end);
        create index if not exists booking_owner on booking(owner,day);
        create index if not exists closure_window on closures(room,day,start,end);
        """)


def path_from_env():
    return Path(os.environ.get("ROOM_BOOKING_DB", "runtime/rooms.sqlite")).resolve()


def session_key():
    return secrets.token_urlsafe(32)


def log(conn, now, actor, action, subject, detail=""):
    conn.execute(
        "insert into audit(at,actor,action,subject,detail) values(?,?,?,?,?)",
        (now.isoformat(timespec="seconds"), actor, action, subject, detail),
    )


def expired_status(booking, now):
    today = now.date().isoformat()
    day = booking["day"]
    status = booking["status"]
    past_day = day < today
    same_day = day == today
    current = minute(now)
    if status == "pending" and (past_day or (same_day and booking["start"] <= current)):
        return "expired"
    if status == "confirmed":
        if booking["owner"] is None:
            if past_day or (same_day and booking["end"] <= current):
                return "completed"
            return status
        deadline = min(booking["start"] + GRACE, booking["end"])
        if past_day or (same_day and deadline <= current):
            return "no_show"
    if status == "checked_in" and (past_day or (same_day and booking["end"] <= current)):
        return "completed"
    return status


def advance(conn, now):
    today = now.date().isoformat()
    rows = conn.execute(
        "select token,owner,day,start,end,status from booking "
        "where day <= ? and status in ('pending','confirmed','checked_in')", (today,),
    ).fetchall()
    for booking in rows:
        status = expired_status(booking, now)
        if status != booking["status"]:
            conn.execute("update booking set status=? where token=?", (status, booking["token"]))
            log(conn, now, "系统", status, booking["token"][:8])


def conflicts(conn, room, day, start, end):
    checks = (
        "from booking where status in ('pending','confirmed','checked_in')",
        "from closures where 1=1",
    )
    return any(conn.execute(
        f"select 1 {query} and room=? and day=? and start<? and end>? limit 1",
        (room, day.isoformat(), end, start),
    ).fetchone() for query in checks)


def days_from(first: date, count: int):
    return [first + timedelta(days=offset) for offset in range(count)]


def booking_rows(conn, where="", params=()):
    return conn.execute(
        """select b.rowid as id,b.token,b.room,b.day,b.start,b.end,b.status,b.title,b.attendees,
                  b.series,b.decision,r.name as room_name
           from booking b left join rooms r on r.id=b.room """
        + where + " order by b.day desc,b.start,b.token",
        params,
    ).fetchall()


def booking_view(row, *, own=False):
    result = dict(row)
    if not own:
        del result["token"]
    return result


def report(conn, first, last):
    window = (first.isoformat(), last.isoformat())
    metrics = [dict(row) for row in conn.execute(
        """select r.id, r.name, r.area,
           count(b.token) as total,
           count(case when b.status in ('confirmed','checked_in','completed') then 1 end) as accepted,
           count(case when b.status='pending' then 1 end) as pending,
           count(case when b.status='cancelled' then 1 end) as cancelled,
           count(case when b.status='no_show' then 1 end) as no_show,
           count(case when b.status in ('rejected','expired') then 1 end) as declined,
           coalesce(sum(case when b.owner is not null and b.status in ('checked_in','completed')
               then b.end-b.start else 0 end),0) as allocated_minutes
           from rooms r left join booking b on r.id=b.room and b.day between ? and ?
           group by r.id order by r.area,r.id""", window,
    )]
    return {"from": window[0], "to": window[1], "rooms": metrics}

