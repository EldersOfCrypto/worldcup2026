import requests
import os
from datetime import datetime, timezone, timedelta
from database import get_db

BASE_URL = "https://api.football-data.org/v4"

_last_leader    = None   # tracks leaderboard leader across cycles
_reminded_matches = set() # tracks matches we've already sent reminders for

def _headers():
    return {"X-Auth-Token": os.getenv("FOOTBALL_API_KEY", "")}

def _send_discord_webhook(content):
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": content}, timeout=5)
    except Exception:
        pass

def fetch_and_update_matches():
    try:
        r = requests.get(f"{BASE_URL}/competitions/WC/matches", headers=_headers(), timeout=10)
        if r.status_code != 200:
            print(f"API error {r.status_code}: {r.text[:200]}")
            return
        matches = r.json().get("matches", [])
        conn = get_db()
        cur = conn.cursor()
        for m in matches:
            if not m["homeTeam"].get("name") or not m["awayTeam"].get("name"):
                continue
            ft = m.get("score", {}).get("fullTime", {})
            cur.execute("""
                INSERT INTO matches
                    (id, home_team, away_team, home_crest, away_crest,
                     kickoff_utc, status, home_score, away_score, matchday, stage, group_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    status      = EXCLUDED.status,
                    home_score  = EXCLUDED.home_score,
                    away_score  = EXCLUDED.away_score
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
        cur.close()
        conn.close()
        print(f"[API] Updated {len(matches)} matches")
    except Exception as e:
        print(f"[API] fetch error: {e}")

def _winner(home, away):
    if home > away:   return "home"
    if away > home:   return "away"
    return "draw"

def _calculate_points(conn):
    global _last_leader
    cur = conn.cursor(cursor_factory=__import__('psycopg2').extras.RealDictCursor)
    cur.execute("SELECT * FROM matches WHERE status='FINISHED' AND home_score IS NOT NULL")
    finished = cur.fetchall()
    points_awarded = False
    for match in finished:
        cur.execute("""
            SELECT p.*, u.username FROM predictions p
            JOIN users u ON p.user_id = u.id
            WHERE p.match_id=%s AND p.points_earned IS NULL
        """, (match["id"],))
        preds = cur.fetchall()
        if not preds:
            continue
        points_awarded = True
        results = []
        for p in preds:
            pts = 0
            if p["home_score"] == match["home_score"] and p["away_score"] == match["away_score"]:
                pts = 3
            elif _winner(p["home_score"], p["away_score"]) == _winner(match["home_score"], match["away_score"]):
                pts = 1
            cur.execute("UPDATE predictions SET points_earned=%s WHERE id=%s", (pts, p["id"]))
            results.append({"username": p["username"], "pts": pts})
        conn.commit()
        # #1 — Match result summary
        exact  = [r for r in results if r["pts"] == 3]
        winner = [r for r in results if r["pts"] == 1]
        zero   = [r for r in results if r["pts"] == 0]
        msg = f"📊 **Full Time: {match['home_team']} {match['home_score']}–{match['away_score']} {match['away_team']}**\n"
        if exact:
            msg += "🎯 Exact: " + " · ".join(f"**{r['username']}** +3pts" for r in exact) + "\n"
        if winner:
            msg += "✅ Winner: " + " · ".join(f"**{r['username']}** +1pt" for r in winner) + "\n"
        if zero:
            msg += "😢 Missed: " + " · ".join(r["username"] for r in zero)
        _send_discord_webhook(msg.strip())
    cur.execute("""
        UPDATE users SET total_points = (
            SELECT COALESCE(SUM(points_earned), 0)
            FROM predictions WHERE user_id = users.id AND points_earned IS NOT NULL
        )
    """)
    conn.commit()
    # #2 — Leaderboard top 3 + #3 — Overtake alert (only when points were actually awarded)
    if points_awarded:
        cur.execute("SELECT username, total_points FROM users ORDER BY total_points DESC LIMIT 3")
        top3 = cur.fetchall()
        if top3 and top3[0]["total_points"] > 0:
            medals = ["🥇", "🥈", "🥉"]
            lb_msg = "🏆 **Leaderboard Update**\n"
            for i, u in enumerate(top3):
                lb_msg += f"{medals[i]} **{u['username']}** — {u['total_points']} pts\n"
            _send_discord_webhook(lb_msg.strip())
            new_leader = top3[0]["username"]
            if _last_leader and new_leader != _last_leader:
                _send_discord_webhook(
                    f"👑 **{new_leader}** just took the **LEAD** with {top3[0]['total_points']} pts!"
                )
            _last_leader = new_leader
    cur.close()


def send_match_reminders():
    """Send a Discord reminder ~1 hour before each match kickoff."""
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=__import__('psycopg2').extras.RealDictCursor)
        now  = datetime.now(timezone.utc)
        cur.execute("""
            SELECT id, home_team, away_team FROM matches
            WHERE status IN ('TIMED', 'SCHEDULED')
              AND kickoff_utc::timestamptz BETWEEN %s AND %s
        """, (now + timedelta(minutes=55), now + timedelta(minutes=65)))
        matches = cur.fetchall()
        cur.close()
        conn.close()
        for m in matches:
            if m["id"] not in _reminded_matches:
                _reminded_matches.add(m["id"])
                site = os.getenv("SITE_URL", "https://web-production-0f407c.up.railway.app")
                _send_discord_webhook(
                    f"⚽ **MATCH STARTS IN 1 HOUR!**\n"
                    f"🆚 **{m['home_team']} vs {m['away_team']}**\n"
                    f"🔒 Lock your prediction now → {site}/predict"
                )
    except Exception as e:
        print(f"[reminder] error: {e}")

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
