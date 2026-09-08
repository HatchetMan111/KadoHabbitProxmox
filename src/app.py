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
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
<meta name="description" content="Kadō-Web — lokaler Habit-Tracker im LXC. Habit-Score statt Streak, offline-first, kein Cloud-Zwang.">
<meta name="theme-color" content="#FBF8F2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#121513" media="(prefers-color-scheme: dark)">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>Kadō-Web · lokal</title>
<style>
/* Design angelehnt an getkado.app; Favicon: kado-app-icon.svg aus scastiel/kado (MIT). */
:root{--bg:#FBF8F2;--bg-deep:#F0E8D8;--surface:#FFF;--ink:#1A1F1C;--ink-soft:#5a625c;--ink-faint:#9aa19c;
--accent:#355944;--accent-strong:#244031;--accent-soft:rgba(53,89,68,.08);--accent-border:rgba(53,89,68,.2);
--hairline:rgba(26,31,28,.08);--shadow:0 1px 2px rgba(26,31,28,.04),0 12px 40px rgba(26,31,28,.08);
--serif:ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Helvetica,Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--radius:14px}
@media (prefers-color-scheme:dark){:root{--bg:#121513;--bg-deep:#0C0E0D;--surface:#1A1F1C;--ink:#ECE8DE;
--ink-soft:#a8a9a3;--ink-faint:#6b6d66;--accent:#8fb8a0;--accent-strong:#b6d3bf;--accent-soft:rgba(143,184,160,.1);
--accent-border:rgba(143,184,160,.24);--hairline:rgba(236,232,222,.1);
--shadow:0 1px 2px rgba(0,0,0,.4),0 20px 48px rgba(0,0,0,.5)}}
*{box-sizing:border-box}body{margin:0;font-family:var(--sans);font-size:17px;line-height:1.55;color:var(--ink);
background:radial-gradient(ellipse at top,var(--bg-deep) 0%,var(--bg) 55%) no-repeat,var(--bg);
-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
h1,h2,h3{font-family:var(--serif);font-weight:500;letter-spacing:-.015em;line-height:1.15;margin:0 0 .5em}
h1{font-size:clamp(2rem,4vw + 1rem,3rem)}h2{font-size:clamp(1.4rem,1.5vw + 1rem,2rem)}
h3{font-size:1.125rem;font-family:var(--sans);font-weight:600}
p{margin:0 0 1em;color:var(--ink-soft)}
.container{width:100%;max-width:1100px;margin:0 auto;padding:0 24px}
.site-header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--hairline)}
.nav{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 24px;max-width:1100px;margin:0 auto}
.brand{display:flex;align-items:center;gap:6px;color:var(--ink)}.brand:hover{text-decoration:none}
.brand-mark{width:34px;height:34px;color:var(--ink);flex-shrink:0}
.brand-name{font-family:var(--serif);font-size:1.5rem;letter-spacing:-.01em}
.brand-tag{font-family:var(--serif);font-size:.72rem;color:var(--ink-faint);letter-spacing:.14em;white-space:nowrap;margin-left:4px}
@media(max-width:560px){.brand-tag{display:none}}
.nav nav{display:flex;align-items:center;gap:20px;font-size:.95rem}.nav nav a{color:var(--ink-soft)}
.nav nav a:hover{color:var(--ink);text-decoration:none}
.hero{text-align:center;padding:clamp(40px,7vw,80px) 24px clamp(24px,4vw,44px);max-width:860px;margin:0 auto}
.hero .lede{font-size:clamp(1.02rem,.5vw + 1rem,1.2rem);max-width:56ch;margin:0 auto 1.6rem}
.hero-note{margin-top:20px;font-size:.9rem;color:var(--ink-faint)}
.cta{display:flex;justify-content:center;align-items:center;gap:14px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;border-radius:999px;font-weight:500;
font-size:.98rem;border:1px solid transparent;cursor:pointer;background:var(--accent);color:var(--bg)}
.btn:hover{background:var(--accent-strong);text-decoration:none;transform:translateY(-1px)}
.btn-secondary{background:transparent;color:var(--ink);border-color:var(--hairline)}
.btn-secondary:hover{background:var(--accent-soft);border-color:var(--accent-border);color:var(--ink)}
section.block{padding:clamp(36px,6vw,72px) 0;border-top:1px solid var(--hairline)}
.section-head{text-align:center;max-width:48rem;margin:0 auto clamp(24px,4vw,44px)}
.section-kicker{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.14em;color:var(--accent);margin-bottom:12px}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}
.feature-grid article{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--radius);
padding:24px;transition:border-color .15s,transform .15s}
.feature-grid article:hover{border-color:var(--accent-border);transform:translateY(-2px)}
.feature-grid h3{color:var(--ink);margin-bottom:4px}.feature-grid p{margin:0 0 .6em;font-size:.98rem}
.meta{font-size:.85rem;color:var(--ink-faint);margin-bottom:12px}
.score-row{display:flex;align-items:baseline;gap:10px;margin:.4em 0 .2em}
.score-pct{font-family:var(--serif);font-size:1.9rem;color:var(--ink)}
.score-label{font-size:.85rem;color:var(--accent);font-weight:600}
.bar{height:10px;border-radius:6px;background:var(--hairline);overflow:hidden;margin:8px 0 12px}
.bar>i{display:block;height:100%;background:var(--accent);border-radius:6px;transition:width .3s}
.dots{display:flex;gap:6px;margin:4px 0 14px}.dots i{width:12px;height:12px;border-radius:50%;background:var(--hairline)}
.dots i.done{background:var(--accent)}.dots i.today{outline:2px solid var(--accent-border);outline-offset:1px}
.card-actions{display:flex;gap:10px;flex-wrap:wrap}
.btn-small{padding:8px 16px;font-size:.9rem}
.btn-ghost{background:transparent;color:var(--ink-soft);border-color:var(--hairline)}
.btn-ghost:hover{color:var(--ink);border-color:var(--accent-border);background:var(--accent-soft)}
.form-card{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--radius);padding:24px;
max-width:640px;margin:0 auto;box-shadow:var(--shadow)}
.form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{font-size:1rem;font-family:var(--sans);padding:11px 14px;border-radius:10px;border:1px solid var(--hairline);
background:var(--bg);color:var(--ink)}input:focus,select:focus{outline:2px solid var(--accent-border);border-color:var(--accent)}
#n{flex:2;min-width:180px}#msg{min-height:1.4em;font-size:.9rem;color:var(--accent);margin:.6em 0 0}
.empty{text-align:center;color:var(--ink-faint);padding:24px}
footer{padding:40px 0 28px;border-top:1px solid var(--hairline);background:var(--bg-deep);
color:var(--ink-soft);font-size:.92rem;text-align:center}
footer nav{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;margin-bottom:12px}
.foot-bottom{color:var(--ink-faint);font-size:.85rem;margin:0}
code{font-family:var(--mono);font-size:.92em;background:var(--accent-soft);color:var(--accent);padding:.1em .35em;border-radius:4px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<header class="site-header"><div class="nav">
<a href="/" class="brand" aria-label="Kadō-Web Start">
<svg class="brand-mark" viewBox="0 0 120 120" fill="none" aria-hidden="true">
<path d="M 90 32 C 100 46, 100 68, 88 82 C 76 96, 56 100, 40 92 C 24 84, 16 64, 22 46 C 28 28, 46 20, 62 24 C 72 26, 80 30, 86 36" stroke="currentColor" stroke-width="10" stroke-linecap="round" fill="none" opacity="0.95"/>
<circle cx="90" cy="32" r="5" fill="currentColor" opacity="0.95"/></svg>
<span class="brand-name">Kadō-Web</span><span class="brand-tag">稼働 · LOKAL</span></a>
<nav><a href="#heute">Heute</a><a href="#neu">Neu</a><a href="/api/export">Export</a><a href="/healthz">Status</a></nav>
</div></header>
<main>
<section class="hero">
<h1>Habits, die dich nicht bestrafen.</h1>
<p class="lede">Ein lokaler Habit-Tracker im Proxmox-LXC — mit Habit-Score statt fragiler Streak
(EMA α=0.05, wie <a href="https://github.com/scastiel/kado">scastiel/kado</a>). Offline-first,
SQLite im Container, kein Account, kein Cloud-Zwang.</p>
<div class="cta"><a class="btn" href="#neu">+ Neuer Habit</a>
<a class="btn btn-secondary" href="/api/export">JSON-Export</a></div>
<p class="hero-note" id="stats">Verbinde …</p>
</section>
<section class="block" id="heute"><div class="container">
<div class="section-head"><div class="section-kicker">Heute</div><h2>Deine Habits</h2>
<p>Antippen zum Abhaken. Ein verpasster Tag stupst den Score nur an — er löscht nicht Monate an Fortschritt.</p></div>
<div id="list" class="feature-grid"></div>
</div></section>
<section class="block" id="neu"><div class="container">
<div class="section-head"><div class="section-kicker">Neu anfangen</div><h2>Neuer Habit</h2></div>
<div class="form-card"><div class="form-row">
<input id="n" placeholder="z. B. Lesen" maxlength="120">
<select id="f"><option value="daily">Täglich</option><option value="specificDays">Bestimmte Wochentage</option><option value="everyNDays">Alle N Tage</option><option value="daysPerWeek">N× pro Woche</option></select>
<button class="btn btn-small" onclick="addHabit()">Anlegen</button></div>
<p id="msg"></p></div>
</div></section>
</main>
<footer><nav><a href="https://github.com/scastiel/kado">Upstream-Idee (Kadō)</a>
<a href="https://github.com/HatchetMan111/KadoHabbitProxmox">Installer-Repo</a>
<a href="/api/export">Export</a><a href="/healthz">Status</a></nav>
<p class="foot-bottom">Kadō-Web · lokaler Nachbau im LXC — inoffiziell, nicht mit Sébastien Castiel affiliiert · Design angelehnt an getkado.app (MIT)</p></footer>
<script>
const FREQ={daily:"Täglich",specificDays:"Wochentage",everyNDays:"Alle N Tage",daysPerWeek:"N×/Woche"};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(p,o){const r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});if(!r.ok)throw new Error(await r.text());return r.json()}
function dots(comps){const h=[];const t=new Date();for(let i=13;i>=0;i--){const d=new Date(t);d.setDate(t.getDate()-i);
const k=d.toISOString().slice(0,10);const done=comps&&comps[k]>0;
h.push(`<i class="${done?'done':''} ${i===0?'today':''}" title="${k}${done?' ✓':''}"></i>`)}return h.join('')}
async function load(){const h=await api('/api/habits');const el=document.getElementById('list');el.innerHTML='';
document.getElementById('stats').textContent=h.length?`${h.length} Habit${h.length>1?'s':''} ·Ø-Score ${Math.round(h.reduce((a,x)=>a+x.score_pct,0)/h.length)} %`:'Noch keine Habits — leg oben einen an.';
h.forEach(x=>{const d=document.createElement('article');
d.innerHTML=`<h3>${esc(x.name)}</h3><div class="meta">#${x.id} · ${FREQ[x.freq_type]||x.freq_type} · ${esc(x.score_label)}</div>
<div class="score-row"><span class="score-pct">${x.score_pct} %</span><span class="score-label">Streak ${x.streak_current} · Best ${x.streak_best}</span></div>
<div class="bar"><i style="width:${x.score_pct}%"></i></div>
<div class="dots">${dots(x.completions)}</div>
<div class="card-actions"><button class="btn btn-small" data-a="done">Heute erledigt ✓</button><button class="btn btn-small btn-ghost" data-a="del">Löschen</button></div>`;
d.querySelector('[data-a=done]').onclick=async e=>{e.target.disabled=true;try{await api('/api/habits/'+x.id+'/complete',{method:'POST',body:JSON.stringify({})});await load()}catch(err){document.getElementById('msg').textContent='Fehler: '+err.message;e.target.disabled=false}};
d.querySelector('[data-a=del]').onclick=async()=>{if(confirm(`„${x.name}" wirklich löschen?`)){await api('/api/habits/'+x.id,{method:'DELETE'});load()}};
el.appendChild(d)});
if(!h.length)el.innerHTML='<p class="empty">Noch keine Habits — <a href="#neu">leg deinen ersten an</a>.</p>'}
async function addHabit(){const n=document.getElementById('n').value.trim();const m=document.getElementById('msg');
if(!n){m.textContent='Bitte Namen eingeben.';return}m.textContent='';
try{await api('/api/habits',{method:'POST',body:JSON.stringify({name:n,freq_type:document.getElementById('f').value})});
document.getElementById('n').value='';m.textContent='Angelegt ✓';await load();document.getElementById('heute').scrollIntoView({behavior:'smooth'})}
catch(e){m.textContent='Fehler: '+e.message}}
document.getElementById('n').addEventListener('keydown',e=>{if(e.key==='Enter')addHabit()});
load().catch(e=>{document.getElementById('stats').textContent='Fehler: '+e.message});
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


# Favicon: 1:1 aus scastiel/kado (branding/kado-app-icon.svg), MIT-Lizenz,
# © Sébastien Castiel — https://github.com/scastiel/kado/blob/main/branding/kado-app-icon.svg
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180" fill="none">
  <defs>
    <linearGradient id="icon-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FBF8F2"></stop>
      <stop offset="1" stop-color="#F0E8D8"></stop>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="180" height="180" fill="url(#icon-bg)"></rect>
  <g transform="translate(30,30)">
    <path d="M 92 30
             C 102 44, 102 68, 90 82
             C 78 96, 56 100, 38 92
             C 20 84, 12 64, 18 46
             C 24 28, 44 18, 62 22
             C 74 24, 82 28, 88 34" stroke="#355944" stroke-width="12" stroke-linecap="round" fill="none"></path>
    <circle cx="92" cy="30" r="6" fill="#355944"></circle>
  </g>
</svg>"""


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
