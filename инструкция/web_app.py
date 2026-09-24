import os
import uuid
import sqlite3
import random
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
import asyncio
import uvicorn

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vpn_bot.db")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

CAPTCHA_STORE = {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS free_trials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT UNIQUE,
        server_id INTEGER,
        peer_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        free_slots INTEGER
    )
    ''')
    cursor.execute("SELECT COUNT(*) FROM servers")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO servers (name, free_slots) VALUES ('NL-Amster-01 (Test)', 50)")
    
    conn.commit()
    conn.close()

def get_db():
    # ИСПРАВЛЕНИЕ 1: Разрешаем SQLite работать в асинхронных потоках FastAPI
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def create_amnezia_peer(server_id: int) -> tuple[str, str]:
    dummy_config = f"[Interface]\nPrivateKey = СЕКРЕТ\nAddress = 10.8.0.2/24\nDNS = 1.1.1.1\n\n[Peer]\nPublicKey = КЛЮЧ\nEndpoint = 1.2.3.4:51820"
    peer_id = f"trial_{uuid.uuid4().hex[:8]}"
    return dummy_config, peer_id

def delete_amnezia_peer(server_id: int, peer_id: str):
    print(f"[CLEANUP] Успешно удален временный пир {peer_id} на сервере {server_id}")

async def amnezia_cleanup_task():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("SELECT id, server_id, peer_id FROM free_trials WHERE expires_at < ?", (now_str,))
            expired_trials = cursor.fetchall()
            
            for trial in expired_trials:
                try:
                    delete_amnezia_peer(trial["server_id"], trial["peer_id"])
                    cursor.execute("DELETE FROM free_trials WHERE id = ?", (trial["id"],))
                    conn.commit()
                except Exception as e:
                    print(f"Ошибка при удалении просроченного пира {trial['id']}: {e}")
            
            conn.close()
        except Exception as e:
            print(f"Ошибка планировщика: {e}")
            
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(amnezia_cleanup_task())

@app.get("/")
async def read_root(request: Request, db: sqlite3.Connection = Depends(get_db)):
    servers = []
    try:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM servers")
        rows = cursor.fetchall()
        for row in rows:
            srv = dict(row)
            servers.append({
                "id": srv.get("id", 0),
                "name": srv.get("name", srv.get("ip", "Безымянный сервер")),
                "free_slots": srv.get("free_slots", srv.get("slots", 0))
            })
    except Exception as e:
        print(f"Ошибка при чтении таблицы серверов: {e}")
        servers = []

    # ИСПРАВЛЕНИЕ 2: Точное указание request и context для новых версий Starlette/FastAPI
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "servers": servers})

@app.get("/api/captcha")
async def get_captcha():
    now = datetime.utcnow()
    expired = [k for k, v in CAPTCHA_STORE.items() if v["expires"] < now]
    for k in expired:
        CAPTCHA_STORE.pop(k, None)

    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    answer = num1 + num2
    
    session_id = str(uuid.uuid4())
    CAPTCHA_STORE[session_id] = {
        "answer": answer,
        "expires": now + timedelta(minutes=5)
    }
    return {"captcha_id": session_id, "question": f"Сколько будет {num1} + {num2}?"}

@app.post("/api/get-trial")
async def get_trial(request: Request, db: sqlite3.Connection = Depends(get_db)):
    data = await request.json()
    captcha_id = data.get("captcha_id")
    captcha_answer = data.get("answer")
    
    if not captcha_id or captcha_id not in CAPTCHA_STORE:
        raise HTTPException(status_code=400, detail="Капча устарела, обновите страницу.")
    
    saved_captcha = CAPTCHA_STORE.pop(captcha_id)
    if saved_captcha["expires"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Время действия капчи истекло.")
    
    try:
        if int(captcha_answer) != saved_captcha["answer"]:
            raise HTTPException(status_code=400, detail="Неверный ответ капчи.")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Ответом должно быть число.")

    client_ip = request.headers.get("X-Real-IP") or request.client.host
    if not client_ip:
         raise HTTPException(status_code=400, detail="Не удалось определить IP-адрес.")

    cursor = db.cursor()
    two_days_ago = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        "SELECT created_at FROM free_trials WHERE ip = ? AND created_at > ?", 
        (client_ip, two_days_ago)
    )
    if cursor.fetchone():
        raise HTTPException(status_code=400, detail="Доступ уже запрашивался с вашего IP за последние 48 часов.")

    cursor.execute("SELECT id FROM servers WHERE free_slots > 0")
    servers = cursor.fetchall()
    if not servers:
        raise HTTPException(status_code=400, detail="Нет свободных серверов. Попробуйте позже.")
    
    server_id = random.choice(servers)["id"]

    try:
        config_text, peer_id = create_amnezia_peer(server_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Ошибка генерации конфига.")

    expires_at = datetime.utcnow() + timedelta(hours=1)
    cursor.execute(
        "INSERT INTO free_trials (ip, server_id, peer_id, expires_at) VALUES (?, ?, ?, ?)",
        (client_ip, server_id, peer_id, expires_at.strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit()

    return Response(
        content=config_text,
        media_type="application/x-wireguard-profile",
        headers={"Content-Disposition": "attachment; filename=TWG.conf"}
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)