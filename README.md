# TriviaBlitz ⚡

A real-time multiplayer trivia game built with **FastAPI** and **vanilla JavaScript**. Players join from any device using a Game ID or QR code and compete simultaneously on the same questions with live scoring and leaderboards.

🎮 **[Click here to play → TriviaBlitz](https://karthiknayakb.github.io/triviacore/)**

---

## Features

### Gameplay
- **Real-time multiplayer** — all players receive questions and see results simultaneously via WebSockets
- **Speed-based scoring** — correct answers earn bonus points the faster you answer
- **Live leaderboard** — displayed after every question showing rank, name, and score
- **Two question formats** — multiple choice (4 options) and True/False
- **Category labels** — each question shows its category (Science, History, Geography, etc.)

### Lobby & Joining
- **6–8 character Game ID** — randomly generated, unique per game
- **QR code** — displayed in the host's lobby for instant mobile joining; scanning pre-fills the Game ID
- **Shareable join link** — one-click copy of a URL that auto-fills the Game ID for anyone who opens it
- **25-second join window** — game locks automatically after the countdown; late joins are rejected
- **Duplicate name protection** — case-insensitive; two players cannot share the same display name

### Host Controls
- **Configurable questions** — host sets the number of questions (1–50) before the game starts
- **Configurable timer** — host sets seconds per question (15–120) before the game starts
- **End game at any time** — host can terminate the game from lobby, mid-question, or leaderboard with a confirmation dialog
- **Final leaderboard on termination** — all players see current scores immediately when host ends the game

### Session & Reconnection
- **Refresh recovery** — if a player refreshes the page mid-game, a Rejoin screen appears with their saved Game ID, name, and role
- **Catch-up on reconnect** — rejoining mid-question restores the question with the correct remaining time; rejoining on the leaderboard restores that screen
- **Stale session handling** — if the game no longer exists when rejoining, the session is cleared and the player is returned to the home screen

### Technical
- **No accounts required** — players join with a display name only
- **Multiple concurrent games** — up to 100 simultaneous games supported
- **Up to 100 players per game**
- **Full memory cleanup** — completed or terminated games are fully removed from memory
- **Mobile-first UI** — responsive layout works on phones, tablets, and desktops

---

## Game Rules

### Starting a Game

1. The **host** creates a game, chooses the number of questions and time per question, and shares the Game ID or QR code
2. **Players** join using the Game ID and a unique display name
3. The lobby stays open for **25 seconds** — anyone who joins during this window is in
4. After 25 seconds the lobby locks, no new players can join, and the game starts automatically

### Questions

- All players receive the **same question at the same time**
- Each question has a countdown timer (15–120 seconds, set by the host; default 35 seconds)
- Players select their answer by tapping one of the option buttons
- Once submitted, an answer **cannot be changed**
- If time runs out before a player answers, they receive **0 points** for that question

### Scoring

Points are awarded per question as follows:

```
Correct answer:   1.0 point  +  (0.05 × seconds remaining at submission)
Wrong answer:     0 points
No answer:        0 points
```

**Example:** Timer is 35 seconds. Player answers correctly with 20 seconds remaining.

```
Score = 1.0 + (0.05 × 20) = 1.0 + 1.0 = 2.0 points
```

Scores are calculated **server-side** using server time — client clock differences have no effect.

### Leaderboard

- Shown after **every question** for 6 seconds before the next question begins
- Displays each player's **rank**, **name**, and **total score** (rounded to 2 decimal places)
- Players with equal scores share the same rank
- Your own entry is highlighted

### Winning

- After all questions are answered, a **final leaderboard** is shown with the winner crowned
- If the host ends the game early, the leaderboard reflects scores at the moment of termination
