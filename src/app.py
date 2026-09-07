#!/usr/bin/env python3
"""Kado-Web — lokaler Habit-Tracker-Nachbau (FastAPI).

Philosophie wie scastiel/kado (Kadō):
- Habit-Score als exponentieller gleitender Mittelwert (EMA, alpha=0.05),
  kein fragiler Streak. Spec: docs/habit-score.md im Upstream-Repo.
- Lokal-first: SQLite, kein Cloud-Zwang, kein Tracking.
- Web-UI auf 0.0.0.0:$PORT, systemd-fähig.

Tabellen:
  habits(id, name, freq_type, freq_days, every_n, target, kind, created_at, archived_at)
  completions(id, habit_id, day, value, note)  -- day = YYYY-MM-DD, UNIQUE(habit_id, day)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

APP_NAME = "kado-web"
ALPHA = 0.05
DATA_DIR = Path(os.environ.get("KADO_DATA_DIR", "/var/lib/kado"))
DB_PATH = DATA_DIR / "kado.db"
PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI(title="Kado-Web", version="1.0.0")


# ---------------------------------------------------------------- DB

def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS habits(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          freq_type TEXT NOT NULL DEFAULT 'daily',
          freq_days TEXT NOT NULL DEFAULT '[]',
          every_n INTEGER NOT NULL DEFAULT 2,
          target REAL NOT NULL DEFAULT 1.0,
          kind TEXT NOT NULL DEFAULT 'binary',
          created_at TEXT NOT NULL,
          archived_at TEXT
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS completions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
          day TEXT NOT NULL,
          value REAL NOT NULL DEFAULT 1.0,
          note TEXT NOT NULL DEFAULT '',
          UNIQUE(habit_id, day)
        )"""
    )
    return con


# -------------------------------------------------------------- Models

class HabitIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    freq_type: str = Field(default="daily", pattern="^(daily|specificDays|everyNDays|daysPerWeek)$")
    freq_days: list[int] = Field(default_factory=list)  # 0=Mo..6=So, für specificDays
    every_n: int = Field(default=2, ge=1, le=30)
    target: float = Field(default=1.0, gt=0)
    kind: str = Field(default="binary", pattern="^(binary|counter|timer|negative)$")


class CompleteIn(BaseModel):
    day: Optional[str] = None  # YYYY-MM-DD, default heute
    value: float = Field(default=1.0, ge=0)
    note: str = ""


# ------------------------------------------------------- Score / Streak
# Reimplementiert docs/habit-score.md + docs/streak.md (vereinfacht, daily-first).

def _parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def is_due(freq_type: str, freq_days: list[int], every_n: int,
           created: date, day: date, done_days: set[str]) -> bool:
    if day < created:
        return False
    if freq_type == "daily" or freq_type == "daysPerWeek":
        return True
    if freq_type == "specificDays":
        return day.weekday() in (freq_days or [0, 1, 2, 3, 4, 5, 6])
    if freq_type == "everyNDays":
        # Re-Anker: letzter erledigter Tag vor `day`, sonst created.
        anchor = created
        earlier = sorted(d for d in done_days if _parse_day(d) < day)
        if earlier:
            anchor = _parse_day(earlier[-1])
        delta = (day - anchor).days
        # Am Anker-Tag selbst due (Erledigung zählt), danach alle N Tage.
        if delta == 0:
            return True
        return delta % max(every_n, 1) == 0
    return True


def ema_score(created: date, as_of: date, completions: dict[str, float],
              freq_type: str, freq_days: list[int], every_n: int,
              alpha: float = ALPHA) -> tuple[float, list[dict[str, Any]]]:
    """Gibt (current_score, history[{day, score, value|None}]) zurück."""
    score = 0.0
    hist: list[dict[str, Any]] = []
    done = set(completions.keys())
    d = created
    while d <= as_of:
        ds = d.isoformat()
        if is_due(freq_type, freq_days, every_n, created, d, done):
            v = min(1.0, max(0.0, completions.get(ds, 0.0)))
            score = (1 - alpha) * score + alpha * v
            hist.append({"day": ds, "score": round(score, 4), "value": v})
        else:
            hist.append({"day": ds, "score": round(score, 4), "value": None})
        d += timedelta(days=1)
    return round(score, 4), hist


def streaks(created: date, as_of: date, completions: dict[str, float],
            freq_type: str, freq_days: list[int], every_n: int) -> tuple[int, int]:
    done = {k for k, v in completions.items() if v > 0}
    # current: rückwärts ab heute (heute = Gnaden-Tag)
    cur = 0
    d = as_of
    while d >= created:
        ds = d.isoformat()
        if is_due(freq_type, freq_days, every_n, created, d, set(done)):
            if ds in done or d == as_of:
                # Gnaden-Tag: heute darf noch offen sein, zählt aber nur wenn
                # es davor lückenlos war; offenes heute bricht nicht, erhöht aber nicht.
                if ds in done:
                    cur += 1
                elif d == as_of:
                    pass  # offen, kein Break
                d -= timedelta(days=1)
                continue
            break
        d -= timedelta(days=1)
    # best: vorwärts
    best = run = 0
    d = created
    while d <= as_of:
        ds = d.isoformat()
        if is_due(freq_type, freq_days, every_n, created, d, set(done)):
            if ds in done:
                run += 1
                best = max(best, run)
            elif d == as_of:
                best = max(best, run)  # Gnaden-Tag bricht nicht
            else:
                run = 0
        d += timedelta(days=1)
    return cur, best


def habit_payload(row: sqlite3.Row, as_of: date) -> dict[str, Any]:
    con = db()
    try:
        comps = {r["day"]: r["value"] for r in con.execute(
            "SELECT day, value FROM completions WHERE habit_id=?", (row["id"],))}
    finally:
        con.close()
    freq_days = json.loads(row["freq_days"] or "[]")
    created = _parse_day(row["created_at"][:10])
    score, _ = ema_score(created, as_of, comps, row["freq_type"], freq_days, row["every_n"])
    cur, best = streaks(created, as_of, comps, row["freq_type"], freq_days, row["every_n"])
    pct = round(score * 100, 1)
    label = "Schwach" if score < 0.3 else "Im Aufbau" if score < 0.6 else "Stark" if score < 0.85 else "Felsensicher"
    return {
        "id": row["id"], "name": row["name"], "freq_type": row["freq_type"],
        "freq_days": freq_days, "every_n": row["every_n"], "target": row["target"],
        "kind": row["kind"], "created_at": row["created_at"],
        "score": score, "score_pct": pct, "score_label": label,
        "streak_current": cur, "streak_best": best,
        "completions": comps,
    }


# ---------------------------------------------------------------- API

@app.get("/healthz")
def healthz():
    return {"status": "ok", "app": APP_NAME}


@app.get("/api/habits")
def list_habits():
    today = date.today()
    con = db()
    try:
        rows = con.execute("SELECT * FROM habits WHERE archived_at IS NULL ORDER BY id").fetchall()
    finally:
        con.close()
    return [habit_payload(r, today) for r in rows]


@app.post("/api/habits", status_code=201)
def create_habit(h: HabitIn):
    con = db()
    try:
        cur = con.execute(
            "INSERT INTO habits(name,freq_type,freq_days,every_n,target,kind,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (h.name.strip(), h.freq_type, json.dumps(h.freq_days), h.every_n,
             h.target, h.kind, date.today().isoformat()),
        )
        con.commit()
        hid = cur.lastrowid
        row = con.execute("SELECT * FROM habits WHERE id=?", (hid,)).fetchone()
    finally:
        con.close()
    return habit_payload(row, date.today())


@app.post("/api/habits/{hid}/complete")
def complete(hid: int, c: CompleteIn):
    day = c.day or date.today().isoformat()
    try:
        _parse_day(day)
    except ValueError:
        raise HTTPException(400, "day muss YYYY-MM-DD sein")
    con = db()
    try:
        if not con.execute("SELECT 1 FROM habits WHERE id=?", (hid,)).fetchone():
            raise HTTPException(404, "Habit nicht gefunden")
        con.execute(
            "INSERT INTO completions(habit_id,day,value,note) VALUES(?,?,?,?)"
            " ON CONFLICT(habit_id,day) DO UPDATE SET value=excluded.value, note=excluded.note",
            (hid, day, min(1.0, max(0.0, c.value)), c.note or ""),
        )
        con.commit()
        row = con.execute("SELECT * FROM habits WHERE id=?", (hid,)).fetchone()
    finally:
        con.close()
    return habit_payload(row, date.today())


@app.delete("/api/habits/{hid}")
def delete_habit(hid: int):
    con = db()
    try:
        con.execute("DELETE FROM completions WHERE habit_id=?", (hid,))
        cur = con.execute("DELETE FROM habits WHERE id=?", (hid,))
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Habit nicht gefunden")
    finally:
        con.close()
    return {"deleted": hid}


@app.get("/api/export")
def export_all():
    con = db()
    try:
        habits = [dict(r) for r in con.execute("SELECT * FROM habits").fetchall()]
        comps = [dict(r) for r in con.execute("SELECT * FROM completions").fetchall()]
    finally:
        con.close()
    return JSONResponse({"app": APP_NAME, "habits": habits, "completions": comps})


# -------------------------------------------------------------- Web-UI

INDEX_HTML = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kadō-Web · lokal</title>
<style>
:root{color-scheme:light dark}body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.5}
.card{border:1px solid #8884;border-radius:12px;padding:1rem;margin:.75rem 0}
.bar{height:10px;border-radius:6px;background:#8883;overflow:hidden}.bar>i{display:block;height:100%;background:#2f9e44}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}input,select,button{font-size:1rem;padding:.45rem .6rem;border-radius:8px;border:1px solid #8886}
button{cursor:pointer}.muted{opacity:.7}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.75rem}
</style></head><body>
<h1>稼働 Kadō-Web <span class="muted">· lokal · kein Cloud-Zwang</span></h1>
<p class="muted">Score statt Streak (EMA α=0.05, wie <a href="https://github.com/scastiel/kado">scastiel/kado</a>). Daten: SQLite im Container.</p>
<div class="card"><h3>Neuer Habit</h3><div class="row">
<input id="n" placeholder="z. B. Lesen" style="flex:2">
<select id="f"><option value="daily">Täglich</option><option value="specificDays">Bestimmte Wochentage</option><option value="everyNDays">Alle N Tage</option><option value="daysPerWeek">N× pro Woche</option></select>
<button onclick="addHabit()">Anlegen</button></div>
<p class="muted" id="msg"></p></div>
<div id="list" class="grid"></div>
<p class="muted"><a href="/healthz">healthz</a> · <a href="/api/export">JSON-Export</a></p>
<script>
async function api(p,o){const r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});if(!r.ok)throw new Error(await r.text());return r.json()}
async function load(){const h=await api('/api/habits');const el=document.getElementById('list');el.innerHTML='';
h.forEach(x=>{const d=document.createElement('div');d.className='card';
d.innerHTML=`<b>${x.name}</b> <span class="muted">#${x.id} · ${x.freq_type} · ${x.score_label}</span>
<div class="bar"><i style="width:${x.score_pct}%"></i></div>
<p>Score <b>${x.score_pct}%</b> · Streak <b>${x.streak_current}</b> (best ${x.streak_best})</p>
<div class="row"><button data-a="done">Heute erledigt</button><button data-a="del">Löschen</button></div>`;
d.querySelector('[data-a=done]').onclick=async()=>{await api('/api/habits/'+x.id+'/complete',{method:'POST',body:JSON.stringify({})});load()};
d.querySelector('[data-a=del]').onclick=async()=>{if(confirm('Wirklich löschen?')){await api('/api/habits/'+x.id,{method:'DELETE'});load()}};
el.appendChild(d)});if(!h.length)el.innerHTML='<p class=muted>Noch keine Habits — oben anlegen.</p>'}
async function addHabit(){const n=document.getElementById('n').value.trim();if(!n){document.getElementById('msg').textContent='Bitte Namen eingeben.';return}
await api('/api/habits',{method:'POST',body:JSON.stringify({name:n,freq_type:document.getElementById('f').value})});document.getElementById('n').value='';load()}
load().catch(e=>document.getElementById('msg').textContent='Fehler: '+e.message);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
