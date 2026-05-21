from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from psycopg2 import pool
import redis
import secrets
import os

load_dotenv()

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/app")
def frontend():
    return FileResponse("static/index.html")

# Redis — single persistent connection is fine
cache = redis.from_url(os.getenv("REDIS_URL"))

# Connection pool — created once at startup, reused across all requests
# minconn=1: always keep 1 connection alive
# maxconn=10: never open more than 10 simultaneous connections
connection_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv("DATABASE_URL")
)

def get_db():
    """Borrow a connection from the pool"""
    return connection_pool.getconn()

def release_db(conn):
    """Return the connection back to the pool (not closed, reused)"""
    connection_pool.putconn(conn)

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id          SERIAL PRIMARY KEY,
                short_code  VARCHAR(20) UNIQUE NOT NULL,
                long_url    TEXT NOT NULL,
                clicks      INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT now()
            )
        """)
        conn.commit()
    finally:
        cursor.close()
        release_db(conn)  # return to pool, not closed

init_db()

def check_rate_limit(ip: str):
    key = f"rate_limit:{ip}"
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 60)
    if count > 5:
        raise HTTPException(status_code=429, detail="Too many requests. Max 5 per minute.")

@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.post("/shorten")
def shorten_url(long_url: str, request: Request):
    check_rate_limit(request.client.host)
    short_code = secrets.token_urlsafe(6)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO urls (short_code, long_url) VALUES (%s, %s)",
            (short_code, long_url)
        )
        conn.commit()
    except Exception:
        conn.rollback()  # undo failed transaction
        raise HTTPException(status_code=500, detail="Database error")
    finally:
        cursor.close()
        release_db(conn)  # back to pool
    cache.setex(short_code, 86400, long_url)
    return {"short_code": short_code, "long_url": long_url}

@app.get("/stats/{short_code}")
def get_stats(short_code: str):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT long_url, clicks, created_at FROM urls WHERE short_code = %s",
            (short_code,)
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        release_db(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Short URL not found")
    return {
        "short_code": short_code,
        "long_url":   row[0],
        "clicks":     row[1],
        "created_at": row[2]
    }

@app.get("/{short_code}")
def redirect_url(short_code: str):
    long_url = cache.get(short_code)
    if long_url:
        long_url = long_url.decode()
    else:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT long_url FROM urls WHERE short_code = %s",
                (short_code,)
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            release_db(conn)
        if not row:
            raise HTTPException(status_code=404, detail="Short URL not found")
        long_url = row[0]
        cache.setex(short_code, 86400, long_url)

    # Update click count
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE urls SET clicks = clicks + 1 WHERE short_code = %s",
            (short_code,)
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cursor.close()
        release_db(conn)

    return RedirectResponse(url=long_url)