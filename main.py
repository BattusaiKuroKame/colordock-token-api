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
player_states: Dict[str, Dict] = {}  # client_id -> {"room": str, "ready": bool, "endpoint": str, "game_id": str}

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
    
    print(f"🔌 New WS: {client_id} from {client_ip}")
    
    try:
        while client_id in connected_clients:
            data = await websocket.receive_text()
            msg = json.loads(data)

            print(data)
            
            if msg.get("type") == "join":
                await handle_join(client_id, websocket, client_ip, msg)
                
            elif msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif msg.get("type") == "quit":
                await handle_remove(client_id, websocket, client_ip, msg)

            elif msg.get("type") == "ready_state":
                await handle_ready_toggle(client_id, websocket, msg.get("message", False))
                
            elif msg.get("type") == "status":
                await handle_status(client_id, websocket, client_ip, msg)
                
    except WebSocketDisconnect:
        print(f"🔌 {client_id} disconnected")
    except Exception as e:
        print(f"WS Error {client_id}: {e}")
    finally:
        cleanup_client(client_id)

async def handle_status(client_id: str, websocket: WebSocket, client_ip: str, msg: dict):

    room_id = player_states[client_id]["room"]
    
    ###########################
    room_clients = rooms.get(room_id, [])
    # ready_clients = [cid for cid in room_clients 
    #                 if cid in player_states and player_states[cid]["ready"]]

    
    if len(room_clients) < 2:
        return
    
    # Each player gets ALL other players' endpoints
    peers = []
    ignore_list = ["room"]
    for other_id in room_clients:
        if other_id != client_id:

            temp = {
                "id": other_id,
            }
            peer_info = player_states[other_id]

            for k in ignore_list:
                peer_info.pop(k,None)

            temp.update(peer_info)

            peers.append(temp)

    #################################
    
    print('Room status query for room:  ',room_id)
    
    ready_count = sum(1 for cid in room_clients 
                    if cid in player_states and player_states[cid]["ready"])
    
    status_msg = {
        "message": 'room_query',
        "type": "room_status",
        "room": room_id,
        "ready_count": ready_count,
        "total_players": len(room_clients),
        "all_ready": ready_count == len(room_clients),
        "peers": peers
    }

    await websocket.send_text(json.dumps(status_msg))
    # await broadcast_room_status(room_id, 'Status Update')

async def handle_ready_toggle(client_id: str, websocket: WebSocket, ready_state: bool):
    player_states[client_id]["ready"] = ready_state
    await websocket.send_text(json.dumps({"type": "state_ack", "ready": ready_state}))
    await check_room_ready(player_states[client_id]["room"])

async def handle_join(client_id: str, websocket: WebSocket, client_ip: str, msg: dict):
    """PHASE 1: Handle initial join (immediate ACK)"""
    room_id = msg.get("room", "default")
    endpoint = f"{client_ip}:{msg.get('local_port', 54500)}"
    game_id = msg.get("game_id",'')
    
    # Store state
    player_states[client_id] = {
        "room": room_id,
        "ready": False,
        "endpoint": endpoint,
        "game_id": game_id
    }
    
    # Add to room
    if room_id not in rooms:
        rooms[room_id] = []
    rooms[room_id].append(client_id)
    
    # 🔥 IMMEDIATE JOIN ACK (Phase 1 complete!)
    await websocket.send_text(json.dumps({
        "type": "joined",
        "id": client_id,
        "ip": client_ip,
        "room": room_id,
        "players_needed": 2,  # Configurable
        "current_players": len(rooms[room_id])
    }))
    
    # Notify others + broadcast status
    await notify_player_joined(room_id, client_id)
    await broadcast_room_status(room_id, f'New player joined: {client_id}')
    
    print(f"✅ {client_id} joined {room_id} ({len(rooms[room_id])} players)")

async def handle_remove(client_id: str, websocket: WebSocket, client_ip: str, msg: dict):
    """PHASE 1: Handle initial join (immediate ACK)"""
    room_id = msg.get("room", "default")
    endpoint = f"{client_ip}:{msg.get('local_port', 54500)}"
    
    # Remove client from State logs
    # This will return None if the key is missing, avoiding a KeyError

    
    # Remove from room
    if room_id not in rooms:
        print('No valid room found')
        await websocket.send_text(json.dumps({
            "type": "quit",
            "status": 'Failed'
        }))
        return
    
    removed_value = player_states.pop(client_id, None)
    if not removed_value:
        print('cant find client in player state logs')
    print('Updated Player state logs:')
    print(player_states)

    rooms[room_id].remove(client_id)
    
    # 🔥 IMMEDIATE JOIN ACK (Phase 1 complete!)
    await websocket.send_text(json.dumps({
        "type": "quit",
        "status": 'granted'
    }))
    
    # Notify others + broadcast status
    # await notify_player_joined(room_id, client_id)
    await broadcast_room_status(room_id, f'Player quit: {client_id}')
    
    print(f"✅ {client_id} joined {room_id} ({len(rooms[room_id])} players)")

async def check_room_ready(room_id: str):
    """Check if room ready → PHASE 3 PUNCHNOW!"""
    room_clients = rooms.get(room_id, [])
    if len(room_clients) < 2:
        await broadcast_room_status(room_id,'Ready Check')
        return
        
    ready_count = sum(1 for cid in room_clients 
                     if cid in player_states and player_states[cid]["ready"])
    
    await broadcast_room_status(room_id,'Ready Check')
    
    # ALL READY → PUNCHNOW!
    if ready_count == len(room_clients) and ready_count > 1:
        print(f"🚀 {room_id}: {ready_count}/{len(room_clients)} READY → PUNCHNOW!")
        await punch_all_players(room_id)

async def broadcast_room_status(room_id: str, message: str = ''):
    """Broadcast status to entire room"""
    room_clients = rooms.get(room_id, [])
    ready_count = sum(1 for cid in room_clients 
                     if cid in player_states and player_states[cid]["ready"])
    
    temp = []
    for client in room_clients:
        d = player_states[client]
        new_dict = {k: v for k, v in d.items() if k != "room"}

        temp.append({
            "client_id": client,
            "client_info": new_dict
        })
    
    status_msg = {
        "message": message,
        "type": "room_status",
        "room": room_id,
        "ready_count": ready_count,
        "total_players": len(room_clients),
        "all_ready": ready_count == len(room_clients),
        "player_info": temp
    }
    
    for client_id in room_clients:
        if client_id in connected_clients:
            try:
                await connected_clients[client_id].send_text(json.dumps(status_msg))
            except:
                pass

async def punch_all_players(room_id: str):
    """PHASE 3: Send PUNCHNOW to ALL ready players"""
    room_clients = rooms.get(room_id, [])
    ready_clients = [cid for cid in room_clients 
                    if cid in player_states and player_states[cid]["ready"]]
    
    if len(ready_clients) < 2:
        return
        
    print(f"🎮 Sending PUNCHNOW to {len(ready_clients)} players...")
    
    # Each player gets ALL other players' endpoints
    for client_id in ready_clients:
        peers = []
        ignore_list = ["room","ready"]
        for other_id in ready_clients:
            if other_id != client_id:

                temp = {
                    "id": other_id,
                }
                peer_info = player_states[other_id]

                for k in ignore_list:
                    peer_info.pop(k,None)

                temp.update(peer_info)

                peers.append(temp)
        
        try:
            await connected_clients[client_id].send_text(json.dumps({
                "type": "PUNCHNOW",
                "room": room_id,
                "peers": peers,
                "total_players": len(ready_clients)
            }))
            print(f"🚀 {client_id} ← {len(peers)} peers")
        except Exception as e:
            print(f"❌ Failed PUNCHNOW {client_id}: {e}")

async def notify_player_joined(room_id: str, new_client_id: str):
    """Notify room about new joiner"""
    room_clients = rooms.get(room_id, [])
    
    # Existing players ← New player joined
    new_player_info = {
        "type": "player_joined",
        "player": {
            "id": new_client_id,
            "endpoint": player_states[new_client_id]["endpoint"],
            "joined_at": int(time.time())
        },
        "room": room_id
    }
    
    for existing_id in room_clients:
        if existing_id != new_client_id and existing_id in connected_clients:
            try:
                await connected_clients[existing_id].send_text(json.dumps(new_player_info))
            except:
                pass
    
    # New player ← Existing players
    existing_players = []
    for existing_id in room_clients:
        if existing_id != new_client_id and existing_id in player_states:
            existing_players.append({
                "id": existing_id,
                "endpoint": player_states[existing_id]["endpoint"],
                "ready": player_states[existing_id]["ready"]
            })
    
    welcome_msg = {
        "type": "welcome_existing",
        "room": room_id,
        "existing_players": existing_players,
        "message": f"Welcome to {room_id}! {len(existing_players)} already here"
    }
    
    try:
        await connected_clients[new_client_id].send_text(json.dumps(welcome_msg))
    except:
        pass

def cleanup_client(client_id: str):
    """Clean up disconnected client"""
    if client_id in connected_clients:
        connected_clients.pop(client_id, None)
    if client_id in player_states:
        player_states.pop(client_id, None)
    
    # Remove from rooms
    for room_id, client_ids in list(rooms.items()):
        if client_id in client_ids:
            client_ids.remove(client_id)
            if not client_ids:
                del rooms[room_id]
            else:
                # Recheck room readiness
                asyncio.create_task(check_room_ready(room_id))

@app.get("/rooms")
async def list_rooms():
    """Debug: Room status"""
    room_status = {}
    for room_id, clients in rooms.items():
        ready_count = sum(1 for cid in clients 
                         if cid in player_states and player_states[cid]["ready"])
        room_status[room_id] = {
            "players": len(clients),
            "ready": ready_count,
            "client_ids": clients[:3]  # First 3 for preview
        }
    return {"rooms": room_status}

@app.get("/test")
async def test_page():
    html = """
    <!DOCTYPE html>
    <html><body>
        <h1>P2P 3-Phase Test</h1>
        <input id="room" placeholder="Room" value="testroom">
        <button onclick="join()">Join</button>
        <button onclick="ready()" id="readyBtn" disabled>READY</button>
        <pre id="log"></pre>
        <script>
        const ws = new WebSocket(`wss://${location.host}/ws`);
        ws.onmessage = e => {
            const data = JSON.parse(e.data);
            log(data);
            if(data.type === 'joined') document.getElementById('readyBtn').disabled = false;
            if(data.type === 'PUNCHNOW') log('🎮 P2P READY!');
        };
        function join() { ws.send(JSON.stringify({type:'join', room:document.getElementById('room').value, local_port:54500})); }
        function ready() { ws.send(JSON.stringify({type:'ready'})); document.getElementById('readyBtn').disabled=true; }
        function log(msg) { document.getElementById('log').textContent += new Date().toLocaleTimeString() + ' ' + JSON.stringify(msg) + '\\n'; }
        </script>
    </body></html>
    """
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
