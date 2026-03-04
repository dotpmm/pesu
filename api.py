from fastapi import FastAPI, Header, HTTPException
from dotenv import load_dotenv
import os
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, time
import asyncio

from pesuacademy.pesuacademy import PESUAcademy

load_dotenv()

API_KEY = os.getenv("API_KEY")
session: PESUAcademy | None = None
lock = asyncio.Lock()

CACHE_TTL = 20
cache = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session

    uname = os.getenv("PESU_USERNAME")
    pword = os.getenv("PESU_PASSWORD")

    if uname and pword:
        try:
            session = await PESUAcademy.login(uname, pword)
            print("Auto login successful")
        except Exception as e:
            print("Auto login failed:", e)

    yield

    if session:
        await session.close()
        session = None


app = FastAPI(lifespan=lifespan)


def to_json(data):
    if data is None:
        return None

    if isinstance(data, list):
        return [to_json(x) for x in data]

    if isinstance(data, dict):
        return {k: to_json(v) for k, v in data.items()}

    if isinstance(data, time):
        return data.strftime("%H:%M")

    if hasattr(data, "model_dump"):
        return data.model_dump()

    if hasattr(data, "__dict__"):
        return {k: to_json(v) for k, v in data.__dict__.items()}

    return data


async def get_session():
    global session

    async with lock:
        if session is None:
            uname = os.getenv("PESU_USERNAME")
            pword = os.getenv("PESU_PASSWORD")

            if not uname or not pword:
                raise HTTPException(401, "No credentials available")

            session = await PESUAcademy.login(uname, pword)

        return session


async def relogin():
    global session

    uname = os.getenv("PESU_USERNAME")
    pword = os.getenv("PESU_PASSWORD")

    if not uname or not pword:
        raise HTTPException(401, "No credentials available")

    async with lock:
        session = await PESUAcademy.login(uname, pword)
        return session


async def call_with_relogin(func):
    global session

    try:
        sess = await get_session()
        return await func(sess)

    except Exception:
        # try relogin once
        try:
            sess = await relogin()
            return await func(sess)
        except Exception as e:
            raise HTTPException(502, f"Session refresh failed: {e}")


def require_key(x_key: str):
    if x_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def clean_timetable(tt: dict):
    for day, slots in tt["days"].items():
        cleaned = []

        for slot in slots:
            t = slot["time"]

            start = t["start"]
            end = t["end"]
            duration = t["duration"]

            if duration <= 0:
                continue

            if start == "00:00" and end == "00:00":
                continue

            if (not slot["is_break"]) and slot["session"] is None:
                continue

            cleaned.append(slot)

        tt["days"][day] = cleaned

    return tt


def get_cache(key):
    entry = cache.get(key)
    if not entry:
        return None

    data, ts = entry
    if (datetime.now().timestamp() - ts) > CACHE_TTL:
        return None

    return data


def set_cache(key, value):
    cache[key] = (value, datetime.now().timestamp())


@app.get("/")
async def home():
    return {"status": "running"}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/timetable")
async def timetable(x_key: str = Header(None)):
    require_key(x_key)

    cached = get_cache("timetable")
    if cached:
        return cached

    raw = await call_with_relogin(lambda s: s.get_timetable())
    data = clean_timetable(to_json(raw))

    set_cache("timetable", data)
    return data


@app.get("/courses")
async def courses(semester: int | None = None, x_key: str = Header(None)):
    require_key(x_key)

    key = f"courses_{semester}"
    cached = get_cache(key)
    if cached:
        return cached

    data = await call_with_relogin(lambda s: s.get_courses(semester=semester))
    result = to_json(data)

    set_cache(key, result)
    return result


@app.get("/seating")
async def seating(x_key: str = Header(None)):
    require_key(x_key)

    cached = get_cache("seating")
    if cached:
        return cached

    data = await call_with_relogin(lambda s: s.get_seating_info())
    result = to_json(data)

    set_cache("seating", result)
    return result


@app.get("/attendance")
async def attendance(semester: int | None = None, x_key: str = Header(None)):
    require_key(x_key)

    key = f"attendance_{semester}"
    cached = get_cache(key)
    if cached:
        return cached

    data = await call_with_relogin(lambda s: s.get_attendance(semester=semester))
    result = to_json(data)

    set_cache(key, result)
    return result


@app.get("/announcements")
async def announcements(x_key: str = Header(None)):
    require_key(x_key)

    cached = get_cache("announcements")
    if cached:
        return cached

    data = await call_with_relogin(lambda s: s.get_announcements())
    result = to_json(data)

    set_cache("announcements", result)
    return result


@app.get("/me")
async def me(x_key: str = Header(None)):
    require_key(x_key)

    cached = get_cache("me")
    if cached:
        return cached

    data = await call_with_relogin(lambda s: s.get_profile())
    result = to_json(data)

    set_cache("me", result)
    return result


@app.get("/today")
async def today(x_key: str = Header(None)):
    require_key(x_key)

    raw = await call_with_relogin(lambda s: s.get_timetable())
    data = clean_timetable(to_json(raw))

    days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    today_name = days[datetime.today().weekday()]

    if today_name not in data["days"]:
        return {"day": today_name, "classes": [], "status": "no_classes"}

    return {
        "day": today_name,
        "classes": [s for s in data["days"][today_name] if s["session"] is not None],
    }


@app.post("/logout")
async def logout(x_key: str = Header(None)):
    require_key(x_key)

    global session

    if session:
        await session.close()
        session = None

    cache.clear()
    return {"status": "logged_out"}