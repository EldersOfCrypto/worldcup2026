import requests
import os
from datetime import datetime, timezone, timedelta
from database import get_db

BASE_URL = "https://api.football-data.org/v4"

def _headers():
    return {"X-Auth-Token": os.getenv("FOOTBALL_API_KEY", "")}

def fetch_and_update_matches():
    try:
        r = requests.get(f"{BASE_URL}/competitions/WC/matches", headers=_headers(), timeout=10)
        if r.status_code != 200:
            print(f"API error {r.status_code}: {r.text[:200]}")
            return
        matches = r.json().get("matches", [])
        conn = get_db()
        for m in matches:
            # Skip TBD matches (knockout stage teams not yet determined)
            if not m["homeTeam"].get("name") or not m["awayTeam"].get("name"):
                continue
            ft = m.get("score", {}).get("fullTime", {})
            conn.execute("""
                INSERT INTO matches
                    (id, home_team, away_team, home_crest, away_crest,
                     kickoff_utc, status, home_score, away_score, matchday, stage, group_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status      = excluded.status,
                    home_score  = excluded.home_score,
                    away_score  = excluded.away_score
            """, (
                m["id"],
                m["homeTeam"]["name"], m["awayTeam"]["name"],
                m["homeTeam"].get("crest"), m["awayTeam"].get("crest"),
                m["utcDate"], m["status"],
                ft.get("home"), ft.get("away"),
                m.get("matchday"), m.get("stage"), m.get("group"),
            ))
        conn.commit()
        _calculate_points(conn)
        conn.close()
        print(f"[API] Updated {len(matches)} matches")
    except Exception as e:
        print(f"[API] fetch error: {e}")

def _winner(home, away):
    if home > away:   return "home"
    if away > home:   return "away"
    return "draw"

def _calculate_points(conn):
    finished = conn.execute(
        "SELECT * FROM matches WHERE status='FINISHED' AND home_score IS NOT NULL"
    ).fetchall()
    for match in finished:
        preds = conn.execute(
            "SELECT * FROM predictions WHERE match_id=? AND points_earned IS NULL",
            (match["id"],)
        ).fetchall()
        for p in preds:
            pts = 0
            if p["home_score"] == match["home_score"] and p["away_score"] == match["away_score"]:
                pts = 3
            elif _winner(p["home_score"], p["away_score"]) == _winner(match["home_score"], match["away_score"]):
                pts = 1
            conn.execute("UPDATE predictions SET points_earned=? WHERE id=?", (pts, p["id"]))
    # Recalculate user totals
    conn.execute("""
        UPDATE users SET total_points = (
            SELECT COALESCE(SUM(points_earned), 0)
            FROM predictions WHERE user_id = users.id AND points_earned IS NOT NULL
        )
    """)
    conn.commit()

def is_locked(kickoff_utc: str) -> bool:
    kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) >= kickoff - timedelta(minutes=1)

def time_until_lock(kickoff_utc: str) -> str:
    kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    lock    = kickoff - timedelta(minutes=1)
    diff    = lock - datetime.now(timezone.utc)
    if diff.total_seconds() <= 0:
        return "Locked"
    h, rem = divmod(int(diff.total_seconds()), 3600)
    m, _   = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"
