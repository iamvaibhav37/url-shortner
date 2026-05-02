from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
from pathlib import Path
import psycopg2
import redis
import secrets
import os

# load_dotenv(dotenv_path=Path(r"C:\Users\vaibh\url-shortner\.env"))
load_dotenv()

app = FastAPI()

# Postgres connection
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cursor = conn.cursor()

# Redis connection
cache = redis.from_url(os.getenv("REDIS_URL"))

# Create table if not exists
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

# --- Rate limiter function ---
def check_rate_limit(ip: str):
    key = f"rate_limit:{ip}"          # unique key per IP in Redis
    count = cache.incr(key)           # increment counter (creates it if doesn't exist)

    if count == 1:
        cache.expire(key, 60)         # first request — set 60 second expiry

    if count > 5:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Max 5 per minute."
        )

@app.get("/")
def home():
    return {"message": "URL shortener is alive"}

@app.post("/shorten")
def shorten_url(long_url: str, request: Request):  # request gives us the IP
    check_rate_limit(request.client.host)           # check before doing anything

    short_code = secrets.token_urlsafe(6)

    cursor.execute(
        "INSERT INTO urls (short_code, long_url) VALUES (%s, %s)",
        (short_code, long_url)
    )
    conn.commit()

    cache.setex(short_code, 86400, long_url)

    return {"short_code": short_code, "long_url": long_url}

@app.get("/stats/{short_code}")
def get_stats(short_code: str):
    cursor.execute(
        "SELECT long_url, clicks, created_at FROM urls WHERE short_code = %s",
        (short_code,)
    )
    row = cursor.fetchone()

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
        cursor.execute(
            "SELECT long_url FROM urls WHERE short_code = %s",
            (short_code,)
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Short URL not found")

        long_url = row[0]
        cache.setex(short_code, 86400, long_url)

    cursor.execute(
        "UPDATE urls SET clicks = clicks + 1 WHERE short_code = %s",
        (short_code,)
    )
    conn.commit()

    return RedirectResponse(url=long_url)