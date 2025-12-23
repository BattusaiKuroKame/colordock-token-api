import os
import traceback
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from models import LoginRequest, LoginResponse
from auth import verify_user
from github_app import generate_installation_token
from storage import issue_token_for_user

from fastapi import FastAPI, WebSocket
from pydantic import BaseModel
from typing import Dict, List, Optional
import json
import time
from datetime import datetime, timedelta

TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", "900"))

app = FastAPI(title="GitHub Token API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/login", response_model=LoginResponse)
def login(body: LoginRequest):
    try:
        # Step 1: verify credentials (live from management repo)
        if not verify_user(body.email, body.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Step 2: get GitHub installation token for DATABASE_REPO
        gh_token_data = generate_installation_token()
        gh_token = gh_token_data["token"]
        gh_expires_at = gh_token_data["expires_at"]

        # Step 3: issue short-lived session token
        session = issue_token_for_user(
            user_email=body.email,
            gh_token=gh_token,
            gh_expires_at=gh_expires_at,
        )

        return LoginResponse(
            status="ok",
            # token=session["token"],
            token=gh_token,
            expires_in=session["expires_in"],
            # expires_in=gh_expires_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        print("LOGIN ERROR:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal Server Error")

# @app.get("/")
# def health():
#     return {"status": "ok"}

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "P2P Signaling Server LIVE!",
        "endpoints": {
            "POST /punch_in": "Register player {ip, port, room, ready}",
            "GET /room/{room_id}": "Room status",
            "DELETE /room/{room_id}": "Clear room"
        },
        "rooms": len(rooms)
    }

# multiplayer stuff

app = FastAPI()

# In-memory rooms storage
# In-memory storage (production: Redis)
rooms: Dict[str, List[Dict]] = {}
room_cleanup_time = 300  # 5min inactive → cleanup

class PunchRequest(BaseModel):
    ip: str
    port: int
    room: str
    ready: bool = False
    waiting: bool = False
    success: bool = False  # NEW: "connected!"

class RoomResponse(BaseModel):
    status: str
    room: str
    your_addr: str
    players: int
    ready_count: int
    targets: Optional[List[Dict]] = None
    total_players: Optional[int] = None

@app.post("/punch_in", response_model=RoomResponse)
async def punch_in(request: PunchRequest):

    room_id = request.room
    addr = f"{request.ip}:{request.port}"
    
    # Cleanup old rooms
    cleanup_rooms()

    if request.success:
        # Client reports success → remove from room
        addr = f"{request.ip}:{request.port}"
        if room_id in rooms:
            rooms[room_id] = [p for p in rooms[room_id] if f"{p['ip']}:{p['port']}" != addr]
            print(f"[{room_id}] ✅ {addr} SUCCESS - removed from room")
        return {"status": "SUCCESS_CONFIRMED"}
    
    # Init room
    if room_id not in rooms:
        rooms[room_id] = []
        print(f"🆕 Room '{room_id}' created")
    
    # Remove old entry for this player
    rooms[room_id] = [p for p in rooms[room_id] 
                     if f"{p['ip']}:{p['port']}" != addr]
    
    # Add/update player
    player = {
        "ip": request.ip,
        "port": request.port,
        "ready": request.ready,
        "waiting": request.waiting,
        "timestamp": time.time()
    }
    rooms[room_id].append(player)
    
    total_players = len(rooms[room_id])
    ready_players = [p for p in rooms[room_id] if p["ready"]]
    ready_count = len(ready_players)
    
    print(f"[{room_id}] {addr} {'✅READY' if request.ready else '⏳WAITING'} "
          f"({total_players}p, {ready_count}r)")
    
    # PUNCH TIME! All ready players get targets
    if ready_count >= 2:
        print(f"[{room_id}] 🚀 PUNCH TIME! {ready_count}/{total_players} ready")
        
        for i, player in enumerate(rooms[room_id]):
            if player["ready"]:
                # Send this player all OTHER players
                targets = [p for j, p in enumerate(rooms[room_id]) if j != i]
                target_list = [{"ip": t["ip"], "port": t["port"]} for t in targets]
                
                response = RoomResponse(
                    status="PUNCH",
                    room=room_id,
                    your_addr=addr,
                    players=total_players,
                    ready_count=ready_count,
                    targets=target_list,
                    total_players=total_players
                )
                print(f"[{room_id}] → {addr} punched {len(targets)} targets")
                return response
    
    # Still waiting
    response = RoomResponse(
        status="WAITING",
        room=room_id,
        your_addr=addr,
        players=total_players,
        ready_count=ready_count
    )
    return response

@app.get("/room/{room_id}")
async def get_room(room_id: str):
    """Debug: Room status"""
    room = rooms.get(room_id, [])
    return {
        "room": room_id,
        "players": len(room),
        "ready": len([p for p in room if p["ready"]]),
        "details": [{"addr": f"{p['ip']}:{p['port']}", "ready": p["ready"]} for p in room],
        "last_active": datetime.now().isoformat() if room else None,
        "total_rooms": len(rooms)
    }

@app.delete("/room/{room_id}")
async def clear_room(room_id: str):
    """Debug: Clear room"""
    if room_id in rooms:
        del rooms[room_id]
        print(f"🗑️ Room '{room_id}' cleared")
    return {"status": "cleared" if room_id in rooms else "not_found"}

def cleanup_rooms():
    """Remove inactive rooms (5min)"""
    cutoff = time.time() - room_cleanup_time
    expired = []
    for room_id, players in rooms.items():
        if players and players[0]["timestamp"] < cutoff:
            expired.append(room_id)
    
    for room_id in expired:
        print(f"🧹 Expired room '{room_id}'")
        del rooms[room_id]

@app.websocket("/ws/{room_id}")
async def websocket_debug(websocket: WebSocket, room_id: str):
    """Debug WebSocket (room events)"""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"[{room_id}] {json.dumps(rooms.get(room_id, []))}")
    except:
        pass

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
