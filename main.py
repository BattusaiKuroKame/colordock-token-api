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

@app.get("/")
def health():
    return {"status": "ok"}

# multiplayer stuff

app = FastAPI()

# In-memory rooms storage
rooms: Dict[str, List[Dict]] = {}  # {room_id: [{"ip": "...", "port": ..., "ready": bool, "waiting": bool}]}

class PunchRequest(BaseModel):
    ip: str
    port: int
    room: str
    ready: bool = False
    waiting: bool = False

@app.post("/punch_in")
async def punch_in(request: PunchRequest):
    room_id = request.room
    
    # Create room if not exists
    if room_id not in rooms:
        rooms[room_id] = []
    
    # Remove old entry for this IP:port
    rooms[room_id] = [p for p in rooms[room_id] if f"{p['ip']}:{p['port']}" != f"{request.ip}:{request.port}"]
    
    # Add new entry
    player = {
        "ip": request.ip,
        "port": request.port,
        "ready": request.ready,
        "waiting": request.waiting,
        "timestamp": time.time()
    }
    rooms[room_id].append(player)
    
    print(f"[{room_id}] {request.ip}:{request.port} {'READY' if request.ready else 'WAITING'} ({len(rooms[room_id])} players)")
    
    # All ready? Send punch targets!
    ready_players = [p for p in rooms[room_id] if p["ready"]]
    if len(ready_players) >= 2:
        print(f"[{room_id}] ALL READY! Sending punch targets...")
        
        # Send each player the OTHER players
        for i, player in enumerate(rooms[room_id]):
            targets = [p for j, p in enumerate(rooms[room_id]) if j != i]
            response = {
                "status": "PUNCH",
                "room": room_id,
                "your_addr": f"{player['ip']}:{player['port']}",
                "targets": [{"ip": t["ip"], "port": t["port"]} for t in targets],
                "total_players": len(rooms[room_id])
            }
            
            # Return punch targets to this player
            if player["ready"]:
                return response
    
    # Still waiting
    return {
        "status": "WAITING",
        "room": room_id,
        "players": len(rooms[room_id]),
        "your_addr": f"{request.ip}:{request.port}",
        "ready_count": len([p for p in rooms[room_id] if p["ready"]])
    }

@app.get("/room/{room_id}")
async def get_room(room_id: str):
    """Debug endpoint"""
    return {
        "room": room_id,
        "players": rooms.get(room_id, []),
        "total_rooms": len(rooms)
    }

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"[{room_id}] Echo: {data}")
    except: pass

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.1", port=8000)
