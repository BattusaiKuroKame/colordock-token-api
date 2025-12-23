import os
import traceback
from fastapi import FastAPI, HTTPException, Request
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

# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, List, Tuple
import uvicorn
import os

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



# In-memory rooms storage
# In-memory storage (production: Redis)
rooms: Dict[str, List[Dict]] = {}
room_cleanup_time = 300  # 5min inactive → cleanup

connected_clients: Dict[str, WebSocket] = {}
match_queue: List[Tuple[str, str]] = []  # (client_id, public_endpoint)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"client_{len(connected_clients)}"
    connected_clients[client_id] = websocket
    
    try:
        # Get client's public endpoint (from WebSocket peer info or STUN)
        data = await websocket.receive_text()
        endpoint = parse_endpoint(data)  # Implement STUN-like public IP:port detection
        match_queue.append((client_id, endpoint))
        await websocket.send_text(f"ID: {client_id}")
        
        while True:
            data = await websocket.receive_text()
            if len(match_queue) >= 2:
                peer1, peer2 = match_queue.pop(0), match_queue.pop(0)
                await connected_clients[peer1[0]].send_text(f'{{"type":"match","peer_endpoint":"{peer2[1]}"}}')
                await connected_clients[peer2[0]].send_text(f'{{"type":"match","peer_endpoint":"{peer1[1]}"}}')
                
    except WebSocketDisconnect:
        connected_clients.pop(client_id, None)

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
