from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
import psycopg2
import redis
import secrets
import os

load_dotenv()

app = FastAPI()

# Redis connection (single persistent connection is fine for Redis)
cache = redis.from_url(os.getenv("REDIS_URL"))

def get_db():
    """Create a fresh database connection for each request"""
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def init_db():
    """Create table on startup"""
    conn = get_db()
    cursor = conn.cursor()
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
    cursor.close()
    conn.close()

init_db()

def check_rate_limit(ip: str):
    key = f"rate_limit:{ip}"
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 60)
    if count > 5:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Max 5 per minute."
        )

@app.get("/")
def home():
    return {"message": "URL shortener is alive"}

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
    finally:
        cursor.close()
        conn.close()

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
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Short URL not found")

    return {
        "short_code": short_code,
        "long_url":   row[0],
        "clicks":     row[1],
        "created_at": row[2],
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
            conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Short URL not found")

        long_url = row[0]
        cache.setex(short_code, 86400, long_url)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE urls SET clicks = clicks + 1 WHERE short_code = %s",
            (short_code,)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return RedirectResponse(url=long_url)