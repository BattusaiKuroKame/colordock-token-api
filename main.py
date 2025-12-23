import os
import traceback
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from models import LoginRequest, LoginResponse
from auth import verify_user
from github_app import generate_installation_token
from storage import issue_token_for_user

from pydantic import BaseModel
from typing import Dict, List, Optional, Tuple
import json
import time
from datetime import datetime, timedelta

TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", "900"))

app = FastAPI(title="GitHub Token API + P2P Matchmaking")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === EXISTING AUTH ENDPOINTS ===
@app.post("/login", response_model=LoginResponse)
def login(body: LoginRequest):
    try:
        if not verify_user(body.email, body.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        gh_token_data = generate_installation_token()
        gh_token = gh_token_data["token"]
        gh_expires_at = gh_token_data["expires_at"]

        session = issue_token_for_user(
            user_email=body.email,
            gh_token=gh_token,
            gh_expires_at=gh_expires_at,
        )

        return LoginResponse(
            status="ok",
            token=gh_token,
            expires_in=session["expires_in"],
        )

    except HTTPException:
        raise
    except Exception as e:
        print("LOGIN ERROR:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal Server Error")

# === MULTIPLAYER MATCHMAKING ===
connected_clients: Dict[str, WebSocket] = {}
match_queue: List[Tuple[str, str]] = []  # (client_id, endpoint)
rooms: Dict[str, List[str]] = {}  # room_id -> list of client_ids

class PlayerInfo(BaseModel):
    room: str
    ready: bool = False
    local_port: int = 54500

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "P2P Signaling Server LIVE!",
        "endpoints": {
            "POST /login": "GitHub token auth",
            "WS /ws": "WebSocket matchmaking",
            "GET /rooms": "List active rooms"
        },
        "active_rooms": len(rooms),
        "queued_players": len(match_queue)
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"client_{len(connected_clients) + 1}_{int(time.time())}"
    connected_clients[client_id] = websocket
    client_ip = websocket.client.host  # Server sees public IP
    
    await websocket.send_text(json.dumps({
        "type": "welcome", 
        "id": client_id,
        "public_ip": client_ip
    }))
    
    try:
        # Wait for player info (room, port)
        data = await websocket.receive_text()
        player_info = json.loads(data)
        
        if player_info.get("type") != "join":
            await websocket.close(code=1008, reason="Invalid join")
            return
            
        room_id = player_info.get("room", "default")
        endpoint = f"{client_ip}:{player_info.get('local_port', 54500)}"
        
        # Add to room queue
        if room_id not in rooms:
            rooms[room_id] = []
        rooms[room_id].append(client_id)
        match_queue.append((client_id, endpoint))
        
        await websocket.send_text(json.dumps({
            "type": "queued",
            "room": room_id,
            "position": len(rooms[room_id])
        }))
        
        # Matchmake: pair when room has 2+ players
        if len(rooms[room_id]) >= 2:
            await match_players(room_id, client_id)
        
        # Keep alive loop
        while True:
            try:
                await websocket.receive_text(timeout=30)  # Heartbeat
            except:
                break
                
    except WebSocketDisconnect:
        cleanup_client(client_id)
    except Exception as e:
        print(f"WS Error {client_id}: {e}")
        cleanup_client(client_id)

async def match_players(room_id: str, initiator_id: str):
    """Pair players in room and exchange endpoints"""
    room_clients = rooms.get(room_id, [])
    if len(room_clients) < 2:
        return
        
    # Take first 2 players
    peer1_id = room_clients.pop(0)
    peer2_id = room_clients.pop(0)
    
    # Remove from global queue too
    global match_queue
    match_queue = [(cid, ep) for cid, ep in match_queue if cid not in (peer1_id, peer2_id)]
    
    # Find their endpoints
    peer1_endpoint = next((ep for cid, ep in match_queue if cid == peer1_id), "unknown:54500")
    peer2_endpoint = next((ep for cid, ep in match_queue if cid == peer2_id), "unknown:54500")
    
    # Send match info
    await connected_clients[peer1_id].send_text(json.dumps({
        "type": "match",
        "peer_endpoint": peer2_endpoint,
        "room": room_id
    }))
    await connected_clients[peer2_id].send_text(json.dumps({
        "type": "match",
        "peer_endpoint": peer1_endpoint,
        "room": room_id
    }))
    
    print(f"Matched {peer1_id} <-> {peer2_id} in room {room_id}")

def cleanup_client(client_id: str):
    """Remove disconnected client"""
    connected_clients.pop(client_id, None)
    match_queue[:] = [(cid, ep) for cid, ep in match_queue if cid != client_id]
    for room_id, client_ids in rooms.items():
        if client_id in client_ids:
            client_ids.remove(client_id)
            if not client_ids:
                del rooms[room_id]

@app.get("/rooms")
async def list_rooms():
    """Debug: list active rooms"""
    return {"rooms": {k: len(v) for k, v in rooms.items()}}

# Test HTML page
@app.get("/test")
async def test_page():
    html = """
    <!DOCTYPE html>
    <html>
    <body>
        <h1>UDP Matchmaking Test</h1>
        <input id="room" placeholder="Room ID" value="testroom">
        <button onclick="join()">Join Queue</button>
        <pre id="log"></pre>
        <script>
        const ws = new WebSocket(`wss://${location.host}/ws`);
        let clientId;
        
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            log(data);
            if (data.type === 'match') {
                log(`🎮 P2P CONNECTED! Punching to ${data.peer_endpoint}`);
                // Start UDP hole punching here
            }
        };
        
        function join() {
            const room = document.getElementById('room').value;
            ws.send(JSON.stringify({type: 'join', room: room, local_port: 54500}));
        }
        
        function log(msg) { document.getElementById('log').textContent += JSON.stringify(msg) + '\\n'; }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
