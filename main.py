import os
import traceback
import asyncio
import time
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from models import LoginRequest, LoginResponse
from auth import verify_user
from github_app import generate_installation_token
from storage import issue_token_for_user

from pydantic import BaseModel
from typing import Dict, List, Optional
import json
from datetime import datetime, timedelta

TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", "900"))

app = FastAPI(title="GitHub Token API + P2P Ready-State Matchmaking")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === STATE MANAGEMENT ===
connected_clients: Dict[str, WebSocket] = {}
rooms: Dict[str, List[str]] = {}  # room_id -> list of client_ids
player_states: Dict[str, Dict] = {}  # client_id -> {"room": str, "ready": bool, "endpoint": str}

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

# === READY-STATE MATCHMAKING ===
@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "P2P Ready-State Signaling Server LIVE!",
        "endpoints": {
            "POST /login": "GitHub token auth",
            "WS /ws": "WebSocket matchmaking (READY required)",
            "GET /rooms": "List active rooms",
            "GET /test": "Test page"
        },
        "active_rooms": len(rooms),
        "connected_clients": len(connected_clients)
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"client_{len(connected_clients) + 1}_{int(time.time())}"
    connected_clients[client_id] = websocket
    client_ip = websocket.client.host
    
    print(f"New connection: {client_id} from {client_ip}")
    
    try:
        while client_id in connected_clients:
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            if msg.get("type") == "join":
                await handle_join(client_id, websocket, client_ip, msg)
                
            elif msg.get("type") == "ready":
                await handle_ready(client_id, websocket)
                
            elif msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                
    except WebSocketDisconnect:
        print(f"Client {client_id} disconnected")
    except Exception as e:
        print(f"WS Error {client_id}: {e}")
    finally:
        cleanup_client(client_id)

async def handle_join(client_id: str, websocket: WebSocket, client_ip: str, msg: dict):
    """Handle client joining a room"""
    room_id = msg.get("room", "default")
    endpoint = f"{client_ip}:{msg.get('local_port', 54500)}"
    
    # Store player state
    player_states[client_id] = {
        "room": room_id,
        "ready": False,
        "endpoint": endpoint
    }
    
    # Add to room
    if room_id not in rooms:
        rooms[room_id] = []
    rooms[room_id].append(client_id)
    
    await websocket.send_text(json.dumps({
        "type": "joined",
        "id": client_id,
        "room": room_id,
        "players_needed": 2,
        "current_players": len(rooms[room_id])
    }))
    
    print(f"Player {client_id} joined {room_id} ({len(rooms[room_id])}/{2})")
    await broadcast_room_status(room_id)

async def handle_ready(client_id: str, websocket: WebSocket):
    """Handle client ready signal"""
    if client_id not in player_states:
        return
        
    player_states[client_id]["ready"] = True
    room_id = player_states[client_id]["room"]
    
    print(f"Player {client_id} READY in {room_id}")
    await websocket.send_text(json.dumps({"type": "ready_ack", "status": "ready"}))
    await check_room_ready(room_id)

async def check_room_ready(room_id: str):
    """Check if ALL players in room are ready"""
    room_clients = rooms.get(room_id, [])
    if len(room_clients) < 2:
        return
        
    ready_count = sum(1 for cid in room_clients 
                     if cid in player_states and player_states[cid].get("ready"))
    
    await broadcast_room_status(room_id)
    
    # ALL READY? PUNCHNOW!
    if ready_count == len(room_clients):
        print(f"🚀 {room_id}: ALL {len(room_clients)} READY! Sending PUNCHNOW")
        await punch_all_players(room_id)

async def broadcast_room_status(room_id: str):
    """Broadcast room status to all players"""
    room_clients = rooms.get(room_id, [])
    ready_count = sum(1 for cid in room_clients 
                     if cid in player_states and player_states[cid].get("ready"))
    
    status_msg = {
        "type": "room_status",
        "room": room_id,
        "ready_count": ready_count,
        "total_players": len(room_clients),
        "all_ready": ready_count == len(room_clients)
    }
    
    for client_id in room_clients:
        if client_id in connected_clients:
            await connected_clients[client_id].send_text(json.dumps(status_msg))

async def punch_all_players(room_id: str):
    """Send ALL players' endpoints to EVERYONE"""
    room_clients = rooms.get(room_id, [])
    ready_clients = [cid for cid in room_clients 
                    if cid in player_states and player_states[cid].get("ready")]
    
    if len(ready_clients) < 2:
        return
        
    # Build complete peer list for each player
    for client_id in ready_clients:
        peers = []
        for other_id in ready_clients:
            if other_id != client_id:  # Not self
                peers.append({
                    "id": other_id,
                    "endpoint": player_states[other_id]["endpoint"]
                })
        
        # Send FULL peer list!
        try:
            await connected_clients[client_id].send_text(json.dumps({
                "type": "PUNCHNOW",
                "room": room_id,
                "peers": peers,  # Array of ALL other players
                "total_players": len(ready_clients)
            }))
            print(f"🚀 {client_id} gets {len(peers)} peers")
        except:
            pass


def cleanup_client(client_id: str):
    """Remove disconnected client from all state"""
    if client_id in connected_clients:
        connected_clients.pop(client_id, None)
    if client_id in player_states:
        player_states.pop(client_id, None)
    
    # Remove from all rooms
    for room_id, client_ids in list(rooms.items()):
        if client_id in client_ids:
            client_ids.remove(client_id)
            if not client_ids:
                del rooms[room_id]
            else:
                asyncio.create_task(check_room_ready(room_id))

@app.get("/rooms")
async def list_rooms():
    """Debug endpoint"""
    room_status = {}
    for room_id, clients in rooms.items():
        ready_count = sum(1 for cid in clients 
                         if cid in player_states and player_states[cid].get("ready"))
        room_status[room_id] = {
            "players": len(clients),
            "ready": ready_count
        }
    return {"rooms": room_status}

@app.get("/test")
async def test_page():
    html = """
    <!DOCTYPE html>
    <html>
    <body>
        <h1>UDP Ready-State Matchmaking Test</h1>
        <input id="room" placeholder="Room ID" value="testroom">
        <button onclick="joinRoom()">Join Room</button>
        <button onclick="setReady()" id="readyBtn" disabled>READY ✓</button>
        <pre id="log"></pre>
        <script>
        const ws = new WebSocket(`wss://${location.host}/ws`);
        let clientId, roomId;
        
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            log(data);
            if (data.type === 'PUNCHNOW') {
                log(`🎮 P2P CONNECTED! Punching to ${data.peer_endpoint}`);
                document.getElementById('readyBtn').disabled = true;
            } else if (data.type === 'joined') {
                clientId = data.id; roomId = data.room;
                document.getElementById('readyBtn').disabled = false;
            } else if (data.type === 'room_status' && data.all_ready) {
                log('🎉 ALL READY - PUNCHNOW incoming!');
            }
        };
        
        function joinRoom() {
            roomId = document.getElementById('room').value;
            ws.send(JSON.stringify({type: 'join', room: roomId, local_port: 54500}));
        }
        
        function setReady() {
            ws.send(JSON.stringify({type: 'ready'}));
            document.getElementById('readyBtn').innerText = 'READY ✓';
            document.getElementById('readyBtn').disabled = true;
        }
        
        function log(msg) { 
            document.getElementById('log').textContent += 
                new Date().toLocaleTimeString() + ' ' + JSON.stringify(msg) + '\\n'; 
        }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
