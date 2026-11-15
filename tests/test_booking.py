import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from store import SHANGHAI


def make_client(path):
    now = datetime(2026, 11, 15, 10, 12, tzinfo=SHANGHAI)
    return TestClient(create_app(path, now=lambda: now))


def request(**changes):
    return {
        "room": "A", "day": "2026-11-16", "start": 540, "end": 600,
        "attendees": 3, "title": "课程讨论", **changes,
    }


def test_booking_conflict_and_cancellation(tmp_path):
    with make_client(tmp_path / "rooms.sqlite") as client:
        assert client.get("/").status_code == 200
        result = client.post("/api/bookings", json=request())
        assert result.status_code == 201
        assert client.post("/api/bookings", json=request(start=570)).status_code == 409
        assert client.post("/api/bookings", json=request(start=600, end=630)).status_code == 201
        assert client.post("/api/bookings/cancel", json={"token": "a" * 32}).status_code == 404
        token = result.json()["token"]
        assert client.post("/api/bookings/cancel", json={"token": token}).status_code == 200
        assert client.post("/api/bookings/cancel", json={"token": token}).status_code == 200
        assert client.post("/api/bookings", json=request()).status_code == 201
        availability = client.get("/api/rooms", params={"day": "2026-11-16"}).json()["rooms"][0]
        assert availability["reserved"] == [[540, 600], [600, 630]]
        assert "token" not in availability and "title" not in availability


def test_day_time_and_capacity_rules(tmp_path):
    with make_client(tmp_path / "rooms.sqlite") as client:
        for data in [
            request(day="2026-11-22"), request(day="2026-11-15"), request(attendees=2),
            request(attendees=5), request(end=810), request(start=545), request(title=" "),
        ]:
            assert client.post("/api/bookings", json=data).status_code == 422, data


def test_two_requests_for_same_slot_only_create_one_booking(tmp_path):
    client = make_client(tmp_path / "rooms.sqlite")
    barrier = Barrier(2)

    def book():
        barrier.wait(timeout=5)
        return client.post("/api/bookings", json=request()).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: book(), range(2))) == [201, 409]
    assert client.get("/api/rooms", params={"day": "2026-11-16"}).json()["rooms"][0]["reserved"] == [[540, 600]]



def login(client):
    assert client.post("/api/session/admin", json={"password": "local-test-passphrase"}).status_code == 200


def test_approval_and_student_ownership(tmp_path):
    now = datetime(2026, 11, 15, 10, 12, tzinfo=SHANGHAI)
    app = create_app(tmp_path / "rooms.sqlite", now=lambda: now, admin_password="local-test-passphrase")
    window = {"first": "2026-11-15", "last": "2026-11-21"}
    with TestClient(app) as pupil, TestClient(app) as staff, TestClient(app) as other:
        assert staff.get("/api/admin/bookings", params=window).status_code == 403
        assert pupil.post("/api/session/student").status_code == 200
        result = pupil.post("/api/bookings", json=request(room="C")).json()
        assert result["status"] == "pending"
        assert other.post("/api/bookings/cancel", json={"token": result["token"]}).status_code == 404
        login(staff)
        entries = staff.get("/api/admin/bookings", params=window).json()
        assert len(entries) == 1 and "token" not in entries[0] and "owner" not in entries[0]
        record = entries[0]["id"]
        assert staff.post(f"/api/admin/bookings/{record}", json={"action": "approve"}).json() == {"status": "confirmed"}
        assert pupil.get("/api/bookings/mine").json()[0]["status"] == "confirmed"
        assert staff.post(f"/api/admin/bookings/{record}", json={"action": "reject"}).status_code == 409
        assert pupil.post("/api/bookings", json=request(room="C", start=600, end=630)).status_code == 201
        pending = next(row for row in staff.get("/api/admin/bookings", params=window).json()
                       if row["status"] == "pending")
        decision = staff.post(f"/api/admin/bookings/{pending['id']}",
                              json={"action": "reject", "note": "设备检修"})
        assert decision.json() == {"status": "rejected"}
        assert pupil.post("/api/bookings", json=request(room="C", start=600, end=630)).status_code == 201
        report = staff.get("/api/admin/report", params={**window, "format": "csv"})
        assert report.status_code == 200 and "协作会议室" in report.text


def test_maintenance_and_series_are_atomic(tmp_path):
    now = datetime(2026, 11, 15, 10, 12, tzinfo=SHANGHAI)
    app = create_app(tmp_path / "rooms.sqlite", now=lambda: now, admin_password="local-test-passphrase")
    with TestClient(app) as pupil, TestClient(app) as staff:
        login(staff)
        closed = staff.post("/api/admin/closures", json={
            "room": "A", "day": "2026-11-17", "start": 540, "end": 570, "reason": "检查白板",
        })
        assert closed.status_code == 201
        series = request(repeat="daily", occurrences=3)
        assert pupil.post("/api/bookings", json=series).status_code == 409
        assert pupil.get("/api/rooms", params={"day": "2026-11-16"}).json()["rooms"][0]["reserved"] == []
        assert staff.delete(f"/api/admin/closures/{closed.json()['id']}").status_code == 200
        bookings = pupil.post("/api/bookings", json=series).json()["bookings"]
        assert [row["day"] for row in bookings] == ["2026-11-16", "2026-11-17", "2026-11-18"]
        assert pupil.get("/api/calendar", params={"day": "2026-11-17", "view": "week"}).json()["days"][1]["booked"] == 1
        assert len(pupil.get("/api/calendar", params={"day": "2026-11-17", "view": "month"}).json()["days"]) == 30
        assert staff.put("/api/admin/rooms/A", json={
            "name": "已经停用", "area": "东区", "capacity": 6, "equipment": "", "active": False,
        }).status_code == 409


def test_check_in_and_no_show_release(tmp_path):
    now = [datetime(2026, 11, 15, 10, 0, tzinfo=SHANGHAI)]
    app = create_app(tmp_path / "rooms.sqlite", now=lambda: now[0])
    with TestClient(app) as client:
        client.post("/api/session/student")
        first = client.post("/api/bookings", json=request(day="2026-11-15", start=630, end=690)).json()
        second = client.post("/api/bookings", json=request(room="B", day="2026-11-15", start=630, end=690)).json()
        assert client.post("/api/bookings/check-in", json={"token": first["token"]}).status_code == 409
        now[0] = now[0].replace(minute=25)
        assert client.post("/api/bookings/check-in", json={"token": first["token"]}).json()["status"] == "checked_in"
        now[0] = now[0].replace(minute=45)
        rows = {record["token"]: record["status"] for record in client.get("/api/bookings/mine").json()}
        assert rows == {first["token"]: "checked_in", second["token"]: "no_show"}
        assert client.post("/api/bookings", json=request(room="B", day="2026-11-15", start=660, end=690)).status_code == 201
        now[0] = now[0].replace(hour=11, minute=30)
        assert any(row["status"] == "completed" for row in client.get("/api/bookings/mine").json())


def test_existing_database_loads_without_losing_cancelled_bookings(tmp_path):
    import sqlite3

    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            create table booking (token text primary key, room text not null, day text not null,
                start integer not null, end integer not null, attendees integer not null,
                title text not null, cancelled integer not null default 0);
            insert into booking values('aabbccddeeffgghhiijjkkllmmnnoopp','B','2026-11-17',540,600,3,'旧记录',1);
        """)
    with make_client(path) as client:
        cancelled = client.post("/api/bookings/cancel", json={"token": "aabbccddeeffgghhiijjkkllmmnnoopp"})
        assert cancelled.json() == {"status": "cancelled"}
        assert client.post("/api/bookings", json=request(room="B", day="2026-11-17")).status_code == 201


def test_switching_out_of_operator_preserves_student_bookings(tmp_path):
    now = datetime(2026, 11, 15, 10, 12, tzinfo=SHANGHAI)
    app = create_app(tmp_path / "rooms.sqlite", now=lambda: now, admin_password="local-test-passphrase")
    with TestClient(app) as client:
        client.post("/api/session/student")
        assert client.post("/api/bookings", json=request()).status_code == 201
        login(client)
        assert client.get("/api/bookings/mine").status_code == 403
        client.post("/api/session/logout")
        assert client.get("/api/session").json()["role"] == "student"
        assert len(client.get("/api/bookings/mine").json()) == 1

