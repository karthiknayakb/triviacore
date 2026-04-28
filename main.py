import asyncio
import json
import os
import random
import string
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── CORS origins ──────────────────────────────────────────────────────────────
# Set ALLOWED_ORIGINS env var in Koyeb to your GitHub Pages URL, e.g.:
#   https://your-username.github.io
# Multiple origins can be comma-separated:
#   https://your-username.github.io,http://localhost:8000
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://localhost:5500,http://127.0.0.1:5500,https://karthiknayakb.github.io"
)
ALLOWED_ORIGINS: List[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app = FastAPI(title="Trivia Game API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load questions ────────────────────────────────────────────────────────────
questions_path = Path(__file__).parent / "questions.json"
ALL_QUESTIONS: List[dict] = json.loads(questions_path.read_text())

# ── Constants ─────────────────────────────────────────────────────────────────
JOIN_WINDOW_SECONDS = 25
QUESTION_TIMER_SECONDS = 35
LEADERBOARD_DISPLAY_SECONDS = 6
SPEED_BONUS_MULTIPLIER = 0.05
MAX_CONCURRENT_GAMES = 100          # hard cap on simultaneous active games

# ── Game state ────────────────────────────────────────────────────────────────
# game_id -> GameState
games: Dict[str, "GameState"] = {}


class PlayerState:
    def __init__(self, player_id: str, name: str):
        self.player_id = player_id
        self.name = name
        self.score: float = 0.0
        self.answered_current: bool = False
        self.current_answer: Optional[str] = None


class GameState:
    def __init__(self, game_id: str, host_id: str, total_questions: int = 10, question_timer: int = 35):
        self.game_id = game_id
        self.host_id = host_id
        self.phase = "lobby"          # lobby | question | leaderboard | ended
        self.players: Dict[str, PlayerState] = {}
        self.connections: Dict[str, WebSocket] = {}
        self.questions: List[dict] = []
        self.current_question_index: int = -1
        self.question_start_time: float = 0.0
        self.lobby_start_time: float = time.time()
        self.lock = asyncio.Lock()
        self.game_task: Optional[asyncio.Task] = None
        # ── Configurable per game ──────────────────────────
        self.total_questions: int = total_questions
        self.question_timer: int = question_timer

    def current_question(self) -> Optional[dict]:
        if 0 <= self.current_question_index < len(self.questions):
            return self.questions[self.current_question_index]
        return None

    def leaderboard(self) -> List[dict]:
        sorted_players = sorted(
            self.players.values(), key=lambda p: p.score, reverse=True
        )
        result = []
        rank = 1
        for i, p in enumerate(sorted_players):
            if i > 0 and p.score < sorted_players[i - 1].score:
                rank = i + 1
            result.append({
                "player_id": p.player_id,
                "name": p.name,
                "score": round(p.score, 2),
                "rank": rank,
            })
        return result


# ── Helper utilities ──────────────────────────────────────────────────────────
def generate_game_id() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        gid = "".join(random.choices(chars, k=random.randint(6, 8)))
        if gid not in games:
            return gid


async def broadcast(game: GameState, message: dict, exclude: Optional[str] = None):
    """Send a JSON message to all connected players."""
    dead: List[str] = []
    for pid, ws in list(game.connections.items()):
        if pid == exclude:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(pid)
    for pid in dead:
        game.connections.pop(pid, None)


async def send_to(ws: WebSocket, message: dict):
    try:
        await ws.send_json(message)
    except Exception:
        pass


async def cleanup_game(game: GameState) -> None:
    """
    Idempotent full teardown of a game:
      1. Guard against double-cleanup via phase check under lock.
      2. Cancel the game loop task and await its clean exit.
      3. Close every open WebSocket connection.
      4. Clear all in-memory collections.
      5. Remove the game from the global registry.
    Safe to call from both normal completion and host-forced termination.
    """
    async with game.lock:
        if game.phase == "cleaned":
            return                      # already cleaned — nothing to do
        game.phase = "cleaned"          # sentinel: blocks any further entry

    # ── 1. Cancel the game loop task ──────────────────────────────────────
    if game.game_task and not game.game_task.done():
        game.game_task.cancel()
        try:
            await game.game_task        # drain CancelledError cleanly
        except (asyncio.CancelledError, Exception):
            pass
    game.game_task = None

    # ── 2. Close all WebSocket connections ────────────────────────────────
    for ws in list(game.connections.values()):
        try:
            await ws.close(code=1000)   # 1000 = normal closure
        except Exception:
            pass
    game.connections.clear()

    # ── 3. Clear remaining in-memory state ────────────────────────────────
    game.players.clear()
    game.questions.clear()

    # ── 4. Remove from global registry ───────────────────────────────────
    games.pop(game.game_id, None)       # pop is safe even if already removed


# ── Game loop ─────────────────────────────────────────────────────────────────
async def run_game(game: GameState):
    """Main game loop: questions → leaderboard → repeat → end."""
    for idx in range(len(game.questions)):
        # ── Bail out if host ended the game between rounds ──
        if game.phase == "ended":
            return

        async with game.lock:
            game.current_question_index = idx
            game.phase = "question"
            game.question_start_time = time.time()
            q = game.current_question()
            # Reset per-question state
            for p in game.players.values():
                p.answered_current = False
                p.current_answer = None

        await broadcast(game, {
            "type": "question",
            "question_index": idx,
            "total_questions": len(game.questions),
            "question_id": q["id"],
            "text": q["text"],
            "options": q["options"],
            "question_type": q["type"],
            "category": q["category"],
            "timer": game.question_timer,
            "start_time": game.question_start_time,
        })

        # Wait for timer
        await asyncio.sleep(game.question_timer)

        # Reveal answer & compute scores
        async with game.lock:
            game.phase = "leaderboard"
            q = game.current_question()
            correct = q["correct"]
            # Award points for unanswered (0 already)
            answer_results = {}
            for p in game.players.values():
                answer_results[p.player_id] = {
                    "name": p.name,
                    "answer": p.current_answer,
                    "correct": p.current_answer == correct,
                    "score": round(p.score, 2),
                }

        await broadcast(game, {
            "type": "leaderboard",
            "correct_answer": correct,
            "leaderboard": game.leaderboard(),
            "answer_results": answer_results,
            "question_index": idx,
            "total_questions": len(game.questions),
        })

        await asyncio.sleep(LEADERBOARD_DISPLAY_SECONDS)

    # Game over — broadcast final results, then clean up
    async with game.lock:
        game.phase = "ended"

    await broadcast(game, {
        "type": "game_over",
        "leaderboard": game.leaderboard(),
    })

    # Small delay so clients receive game_over before the WS is closed
    await asyncio.sleep(1)
    await cleanup_game(game)


async def run_lobby_countdown(game: GameState):
    """Count down join window, then kick off the game."""
    start = game.lobby_start_time
    while True:
        elapsed = time.time() - start
        remaining = max(0, JOIN_WINDOW_SECONDS - elapsed)
        await broadcast(game, {
            "type": "lobby_countdown",
            "seconds_remaining": round(remaining, 1),
            "player_count": len(game.players),
            "players": [{"name": p.name, "player_id": p.player_id}
                        for p in game.players.values()],
            "total_questions": game.total_questions,
            "question_timer": game.question_timer,
        })
        if remaining <= 0:
            break
        await asyncio.sleep(1)

    # Lock lobby and start
    async with game.lock:
        game.phase = "starting"
        # Pick random subset of questions using host-configured count
        sample_size = min(game.total_questions, len(ALL_QUESTIONS))
        game.questions = random.sample(ALL_QUESTIONS, sample_size)

    await broadcast(game, {
        "type": "game_starting",
        "message": "Game is starting!",
        "total_questions": len(game.questions),
    })

    await asyncio.sleep(2)
    await run_game(game)


# ── REST endpoints ────────────────────────────────────────────────────────────
class CreateGameRequest(BaseModel):
    host_name: str
    num_questions: int = 10   # allowed: 1–50
    question_time: int = 35   # allowed: 15–120


class CreateGameResponse(BaseModel):
    game_id: str
    host_id: str


@app.post("/api/game/create", response_model=CreateGameResponse)
async def create_game(req: CreateGameRequest):
    if not req.host_name.strip():
        raise HTTPException(status_code=400, detail="Host name required")
    # ── Capacity check ─────────────────────────────────────
    active_count = sum(1 for g in games.values() if g.phase not in ("ended", "cleaned"))
    if active_count >= MAX_CONCURRENT_GAMES:
        raise HTTPException(
            status_code=503,
            detail="Maximum active games reached. Try again later."
        )
    # ── Validate configurable fields ───────────────────────
    if not (1 <= req.num_questions <= 50):
        raise HTTPException(status_code=400, detail="num_questions must be between 1 and 50")
    if not (15 <= req.question_time <= 120):
        raise HTTPException(status_code=400, detail="question_time must be between 15 and 120 seconds")
    if req.num_questions > len(ALL_QUESTIONS):
        raise HTTPException(
            status_code=400,
            detail=f"num_questions exceeds available questions ({len(ALL_QUESTIONS)})"
        )
    game_id = generate_game_id()
    host_id = "host_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    game = GameState(
        game_id=game_id,
        host_id=host_id,
        total_questions=req.num_questions,
        question_timer=req.question_time,
    )
    # Add host as a player
    game.players[host_id] = PlayerState(host_id, req.host_name.strip())
    games[game_id] = game
    return {"game_id": game_id, "host_id": host_id}


class JoinGameRequest(BaseModel):
    game_id: str
    player_name: str


class JoinGameResponse(BaseModel):
    player_id: str
    game_id: str


@app.post("/api/game/join", response_model=JoinGameResponse)
async def join_game(req: JoinGameRequest):
    game_id = req.game_id.strip().upper()
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Game not found")
    game = games[game_id]
    if game.phase not in ("lobby",):
        raise HTTPException(status_code=400, detail="Game already started or ended")
    if not req.player_name.strip():
        raise HTTPException(status_code=400, detail="Player name required")
    # Check name uniqueness
    taken = {p.name.lower() for p in game.players.values()}
    if req.player_name.strip().lower() in taken:
        raise HTTPException(status_code=400, detail="Name already taken")

    player_id = "player_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    game.players[player_id] = PlayerState(player_id, req.player_name.strip())
    return {"player_id": player_id, "game_id": game_id}


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws/{game_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str, player_id: str):
    game_id = game_id.upper()
    if game_id not in games:
        await websocket.close(code=4004)
        return
    game = games[game_id]
    if player_id not in game.players:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    game.connections[player_id] = websocket

    is_host = player_id == game.host_id

    # Send current state to reconnecting/new player
    await send_to(websocket, {
        "type": "connected",
        "player_id": player_id,
        "game_id": game_id,
        "is_host": is_host,
        "phase": game.phase,
        "players": [{"name": p.name, "player_id": p.player_id}
                    for p in game.players.values()],
        "total_questions": game.total_questions,
        "question_timer": game.question_timer,
    })

    # Host connects → start lobby countdown task
    if is_host and game.game_task is None:
        game.game_task = asyncio.create_task(run_lobby_countdown(game))

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                # WebSocketDisconnect = client left normally
                # RuntimeError = socket closed by cleanup_game() server-side
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "submit_answer":
                async with game.lock:
                    if game.phase != "question":
                        continue
                    player = game.players.get(player_id)
                    if not player or player.answered_current:
                        continue
                    answer = msg.get("answer", "")
                    elapsed = time.time() - game.question_start_time
                    seconds_remaining = max(0, game.question_timer - elapsed)
                    q = game.current_question()
                    player.answered_current = True
                    player.current_answer = answer
                    if answer == q["correct"]:
                        bonus = SPEED_BONUS_MULTIPLIER * seconds_remaining
                        player.score += 1 + bonus

                # Acknowledge to player
                await send_to(websocket, {
                    "type": "answer_received",
                    "answer": answer,
                })

            elif msg_type == "end_game":
                # ── Host-only: force-terminate the game ───────────
                if player_id != game.host_id:
                    continue  # silently ignore non-host requests

                async with game.lock:
                    if game.phase in ("ended", "cleaned"):
                        continue        # already over — idempotent
                    game.phase = "ended"

                # Snapshot the leaderboard before cleanup wipes players
                final_leaderboard = game.leaderboard()

                # Broadcast to all clients before cleanup closes their sockets
                await broadcast(game, {
                    "type": "game_ended_by_host",
                    "leaderboard": final_leaderboard,
                })

                # Small delay so clients receive the message before WS closes
                await asyncio.sleep(0.5)
                await cleanup_game(game)

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        game.connections.pop(player_id, None)


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "TriviaBlitz API is running"}


@app.get("/health")
async def health():
    active = sum(1 for g in games.values() if g.phase not in ("ended", "cleaned"))
    return {
        "status": "ok",
        "active_games": active,
        "capacity": MAX_CONCURRENT_GAMES,
        "slots_remaining": MAX_CONCURRENT_GAMES - active,
    }
