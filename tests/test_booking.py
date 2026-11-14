import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import SHANGHAI, create_app


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

