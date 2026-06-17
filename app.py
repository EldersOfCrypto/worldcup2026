from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from database import get_db, init_db
from api import fetch_and_update_matches, is_locked, time_until_lock
import psycopg2.extras
import requests
import os
import random

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "worldcup2026secret")

DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DISCORD_AUTH_URL      = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL     = "https://discord.com/api/oauth2/token"
DISCORD_API_URL       = "https://discord.com/api/users/@me"

X_CLIENT_ID     = os.getenv("X_CLIENT_ID")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
X_REDIRECT_URI  = os.getenv("X_REDIRECT_URI", "http://localhost:5000/x-callback")
X_TWEET_ID      = os.getenv("X_TWEET_ID", "")
X_AUTH_URL      = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL     = "https://api.twitter.com/2/oauth2/token"
X_USER_URL      = "https://api.twitter.com/2/users/me"

ADMIN_PASSWORD = "1235"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

ORDINAL_AVATARS = [
    "god1.png","god2.png","god3.png","god4.png",
    "sprite55.png","sprite551.png","sprite5511.png","sprite55111.png",
    "sprite551111.png","sprite5511111.png","sprite55112.png","sprite5512.png",
    "sprite55121.png","sprite551211.png","sprite5513.png","sprite552.png",
    "sprite5521.png","sprite55211.png","sprite552111.png","sprite5522.png",
    "sprite553.png","sprite5531.png","sprite55311.png","sprite554.png",
]

def db_fetchone(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()

def db_fetchall(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()

# ── Startup ───────────────────────────────────────────────────────────────────
init_db()
fetch_and_update_matches()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(fetch_and_update_matches, "interval", minutes=5)
scheduler.start()

# ── Template filters ──────────────────────────────────────────────────────────
@app.template_filter("fmt_date")
def fmt_date(utc_str):
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.strftime("%b %d")

@app.template_filter("fmt_time")
def fmt_time(utc_str):
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.strftime("%H:%M UTC")

@app.template_filter("fmt_stage")
def fmt_stage(stage):
    if not stage:
        return ""
    if stage == "GROUP_STAGE":
        return ""
    return stage.replace("_", " ").title()

@app.template_filter("fmt_group")
def fmt_group(group):
    if not group:
        return ""
    return "Group " + group.replace("GROUP_", "")

# ── Context processor ─────────────────────────────────────────────────────────
@app.context_processor
def inject_user():
    user = None
    if "user_id" in session:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        user = db_fetchone(cur, "SELECT * FROM users WHERE id=%s", (session["user_id"],))
        cur.close()
        conn.close()
        if not user:
            session.clear()
    return {"current_user": user}

def avatar_url(discord_id, avatar_hash, ordinal=None):
    if ordinal:
        return f"/static/ordinals/{ordinal}"
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/0.png"

app.jinja_env.globals["avatar_url"] = avatar_url

def get_badges(user_id, cur):
    badges = []
    cur.execute("SELECT COUNT(*) as c FROM predictions WHERE user_id=%s AND points_earned=3", (user_id,))
    if cur.fetchone()["c"] > 0:
        badges.append({"id": "sniper", "icon": "🎯", "name": "Sniper", "desc": "Predicted an exact score"})
    cur.execute("""
        SELECT p.points_earned FROM predictions p
        JOIN matches m ON p.match_id = m.id
        WHERE p.user_id=%s AND p.points_earned IS NOT NULL
        ORDER BY m.kickoff_utc
    """, (user_id,))
    rows = cur.fetchall()
    streak = max_streak = 0
    for r in rows:
        if r["points_earned"] > 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    if max_streak >= 3:
        badges.append({"id": "on_fire", "icon": "🔥", "name": "On Fire", "desc": "3+ correct results in a row"})
    cur.execute("""
        SELECT COUNT(*) as c FROM predictions p
        JOIN matches m ON p.match_id = m.id
        WHERE p.user_id=%s AND p.points_earned=3 AND m.stage != 'GROUP_STAGE'
    """, (user_id,))
    if cur.fetchone()["c"] > 0:
        badges.append({"id": "nostradamus", "icon": "🏆", "name": "Nostradamus", "desc": "Exact score in a knockout match"})
    cur.execute("""
        SELECT COUNT(*) as c FROM predictions p
        JOIN matches m ON p.match_id = m.id
        WHERE p.user_id=%s
        AND p.submitted_at IS NOT NULL
        AND (m.kickoff_utc::timestamptz - p.submitted_at) <= interval '1 hour'
        AND m.kickoff_utc::timestamptz > p.submitted_at
    """, (user_id,))
    if cur.fetchone()["c"] > 0:
        badges.append({"id": "speed_demon", "icon": "⚡", "name": "Speed Demon", "desc": "Predicted within 1 hour of kickoff"})
    return badges

def _send_discord_webhook_app(content):
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": content}, timeout=5)
    except Exception:
        pass

def send_daily_leaderboard():
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        top  = db_fetchall(cur, "SELECT username, total_points FROM users ORDER BY total_points DESC LIMIT 5")
        cur.close(); conn.close()
        if not top or top[0]["total_points"] == 0:
            return
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines  = [f"{medals[i]} **{u['username']}** — {u['total_points']} pts" for i, u in enumerate(top)]
        _send_discord_webhook_app("**📊 Daily Leaderboard — WC 2026**\n" + "\n".join(lines))
    except Exception as e:
        print(f"[Webhook] leaderboard error: {e}")

scheduler.add_job(send_daily_leaderboard, "cron", hour=20, minute=0)

# ── Discord OAuth ─────────────────────────────────────────────────────────────
@app.route("/discord-login")
def discord_login():
    return redirect(
        f"{DISCORD_AUTH_URL}"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify"
    )

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        flash("Discord login failed — no code received.")
        return redirect(url_for("index"))

    token_resp = requests.post(DISCORD_TOKEN_URL, data={
        "client_id":     DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  DISCORD_REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})

    if token_resp.status_code != 200:
        flash("Discord login failed — could not get token.")
        return redirect(url_for("index"))

    access_token = token_resp.json().get("access_token")
    user_resp    = requests.get(DISCORD_API_URL, headers={"Authorization": f"Bearer {access_token}"})
    if user_resp.status_code != 200:
        flash("Discord login failed — could not get user info.")
        return redirect(url_for("index"))

    discord_user = user_resp.json()
    discord_id   = discord_user["id"]
    username     = discord_user.get("global_name") or discord_user.get("username", "Unknown")
    avatar       = discord_user.get("avatar")

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    ordinal = random.choice(ORDINAL_AVATARS)
    cur.execute("""
        INSERT INTO users (discord_id, username, avatar, ordinal_avatar)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(discord_id) DO UPDATE SET
            username       = EXCLUDED.username,
            avatar         = EXCLUDED.avatar,
            ordinal_avatar = CASE
                WHEN users.ordinal_avatar IS NULL THEN EXCLUDED.ordinal_avatar
                ELSE users.ordinal_avatar
            END
    """, (discord_id, username, avatar, ordinal))
    conn.commit()
    user = db_fetchone(cur, "SELECT * FROM users WHERE discord_id=%s", (discord_id,))
    cur.close()
    conn.close()

    session["user_id"]  = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("predict"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ── X OAuth ───────────────────────────────────────────────────────────────────
import base64, hashlib, secrets as _secrets

def _x_code_verifier():
    if "x_code_verifier" not in session:
        session["x_code_verifier"] = _secrets.token_urlsafe(64)
    return session["x_code_verifier"]

def _x_code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

@app.route("/x-login")
def x_login():
    if "user_id" not in session:
        return redirect(url_for("index"))
    verifier  = _x_code_verifier()
    challenge = _x_code_challenge(verifier)
    state     = _secrets.token_urlsafe(16)
    session["x_state"] = state
    params = (
        f"{X_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={X_CLIENT_ID}"
        f"&redirect_uri={X_REDIRECT_URI}"
        f"&scope=tweet.read%20users.read"
        f"&state={state}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )
    return redirect(params)

@app.route("/x-callback")
def x_callback():
    if "user_id" not in session:
        return redirect(url_for("index"))

    error = request.args.get("error")
    if error:
        flash("X login was cancelled or failed.")
        return redirect(url_for("verify"))

    code  = request.args.get("code")
    state = request.args.get("state")
    if state != session.get("x_state"):
        flash("Invalid state — please try again.")
        return redirect(url_for("verify"))

    verifier   = session.get("x_code_verifier", "")
    token_resp = requests.post(X_TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  X_REDIRECT_URI,
            "code_verifier": verifier,
        },
        auth=(X_CLIENT_ID, X_CLIENT_SECRET),
    )
    if token_resp.status_code != 200:
        flash("X login failed — could not get token.")
        return redirect(url_for("verify"))

    access_token = token_resp.json().get("access_token")
    user_resp    = requests.get(X_USER_URL, headers={"Authorization": f"Bearer {access_token}"})
    if user_resp.status_code != 200:
        flash("X login failed — could not get user info.")
        return redirect(url_for("verify"))

    x_user     = user_resp.json().get("data", {})
    x_id       = x_user.get("id")
    x_username = x_user.get("username")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE users SET x_id=%s, x_username=%s, x_verified=0 WHERE id=%s",
                (x_id, x_username, session["user_id"]))
    conn.commit()
    cur.close()
    conn.close()

    session["x_access_token"] = access_token
    session["x_user_id"]      = x_id
    session["x_username"]     = x_username

    is_owner  = x_username and x_username.lower() == "cryptoelders"
    retweeted = is_owner or _check_retweet(access_token, x_id)
    if retweeted:
        _mark_verified(x_id, x_username, session["user_id"])
        flash(f"✅ Verified! Welcome @{x_username} — you can now predict!")
        return redirect(url_for("predict"))

    return redirect(url_for("verify"))

def _mark_verified(x_id, x_username, user_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE users SET x_id=%s, x_username=%s, x_verified=1 WHERE id=%s",
                (x_id, x_username, user_id))
    conn.commit()
    cur.close()
    conn.close()

def _check_retweet(access_token: str, x_user_id: str) -> bool:
    if not X_TWEET_ID:
        return True
    url  = f"https://api.twitter.com/2/tweets/{X_TWEET_ID}/retweeted_by"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        return False
    users = resp.json().get("data", [])
    return any(u["id"] == x_user_id for u in users)

@app.route("/check-retweet-debug")
def check_retweet_debug():
    access_token = session.get("x_access_token")
    x_id         = session.get("x_user_id")
    if not access_token or not x_id:
        return {"error": "no session data", "has_token": bool(access_token), "has_id": bool(x_id)}
    url  = f"https://api.twitter.com/2/tweets/{X_TWEET_ID}/retweeted_by"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    return {"status": resp.status_code, "body": resp.json(), "checking_user_id": x_id, "tweet_id": X_TWEET_ID}

@app.route("/check-retweet")
def check_retweet_poll():
    if "user_id" not in session:
        return {"verified": False}
    access_token = session.get("x_access_token")
    x_id         = session.get("x_user_id")
    x_username   = session.get("x_username", "")
    if not access_token or not x_id:
        return {"verified": False}
    is_owner  = x_username.lower() == "cryptoelders"
    retweeted = is_owner or _check_retweet(access_token, x_id)
    if retweeted:
        _mark_verified(x_id, x_username, session["user_id"])
        return {"verified": True}
    return {"verified": False}

@app.route("/verify")
def verify():
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE id=%s", (session["user_id"],))
    cur.close()
    conn.close()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    if user["x_verified"]:
        return redirect(url_for("predict"))
    x_connected = bool(session.get("x_access_token"))
    return render_template("verify.html", tweet_id=X_TWEET_ID,
                           x_connected=x_connected,
                           x_username=session.get("x_username", ""))

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("predict"))
    return render_template("index.html")

@app.route("/predict")
def predict():
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE id=%s", (session["user_id"],))
    if not user:
        cur.close()
        conn.close()
        session.clear()
        return redirect(url_for("index"))
    if not user["x_verified"]:
        cur.close()
        conn.close()
        return redirect(url_for("verify"))

    rows = db_fetchall(cur, """
        SELECT m.*,
               p.home_score AS pred_home,
               p.away_score AS pred_away
        FROM   matches m
        LEFT JOIN predictions p
               ON m.id = p.match_id AND p.user_id = %s
        WHERE  m.status IN ('TIMED','SCHEDULED','IN_PLAY')
        ORDER  BY m.kickoff_utc
    """, (session["user_id"],))

    from collections import defaultdict
    grouped = defaultdict(list)
    all_matches = []
    for r in rows:
        m = dict(r)
        m["locked"]          = is_locked(m["kickoff_utc"])
        m["time_until_lock"] = time_until_lock(m["kickoff_utc"])
        grouped[fmt_date(m["kickoff_utc"])].append(m)
        all_matches.append(m)

    locked_ids = [m["id"] for m in all_matches if m["locked"]]
    consensus = {}
    if locked_ids:
        cur.execute("""
            SELECT match_id, home_score, away_score, COUNT(*) as cnt
            FROM predictions
            WHERE match_id = ANY(%s) AND home_score IS NOT NULL
            GROUP BY match_id, home_score, away_score
            ORDER BY match_id, cnt DESC
        """, (locked_ids,))
        for row in cur.fetchall():
            mid = row["match_id"]
            if mid not in consensus:
                consensus[mid] = {"scores": [], "total": 0}
            consensus[mid]["total"] += row["cnt"]
            if len(consensus[mid]["scores"]) < 3:
                consensus[mid]["scores"].append({
                    "label": f"{row['home_score']}-{row['away_score']}",
                    "cnt": row["cnt"]
                })
        for mid in consensus:
            total = consensus[mid]["total"]
            for s in consensus[mid]["scores"]:
                s["pct"] = round(s["cnt"] / total * 100) if total > 0 else 0

    cur.close()
    conn.close()
    return render_template("predict.html", grouped=dict(grouped), consensus=consensus)

@app.route("/predict/<int:match_id>", methods=["POST"])
def submit_prediction(match_id):
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE id=%s", (session["user_id"],))
    if not user or not user["x_verified"]:
        cur.close()
        conn.close()
        return redirect(url_for("verify"))

    match = db_fetchone(cur, "SELECT * FROM matches WHERE id=%s", (match_id,))
    if not match or is_locked(match["kickoff_utc"]):
        flash("⏰ Predictions are locked for this match.")
        cur.close()
        conn.close()
        return redirect(url_for("predict"))

    try:
        home = max(0, int(request.form.get("home_score", 0)))
        away = max(0, int(request.form.get("away_score", 0)))
    except ValueError:
        flash("Invalid score.")
        cur.close()
        conn.close()
        return redirect(url_for("predict"))

    cur.execute("""
        INSERT INTO predictions (user_id, match_id, home_score, away_score)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(user_id, match_id) DO UPDATE SET
            home_score   = EXCLUDED.home_score,
            away_score   = EXCLUDED.away_score,
            submitted_at = CURRENT_TIMESTAMP,
            points_earned = NULL
    """, (session["user_id"], match_id, home, away))
    conn.commit()
    cur.close()
    conn.close()
    flash("✅ Prediction saved!")
    return redirect(url_for("predict"))

@app.route("/predict/save-all", methods=["POST"])
def save_all_predictions():
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE id=%s", (session["user_id"],))
    if not user or not user["x_verified"]:
        cur.close()
        conn.close()
        return redirect(url_for("verify"))

    saved  = 0
    locked = 0
    for key in request.form:
        if not key.startswith("home_score_"):
            continue
        try:
            match_id = int(key[len("home_score_"):])
            home     = max(0, int(request.form.get(f"home_score_{match_id}", 0)))
            away     = max(0, int(request.form.get(f"away_score_{match_id}", 0)))
        except (ValueError, TypeError):
            continue

        match = db_fetchone(cur, "SELECT * FROM matches WHERE id=%s", (match_id,))
        if not match or is_locked(match["kickoff_utc"]):
            locked += 1
            continue

        cur.execute("""
            INSERT INTO predictions (user_id, match_id, home_score, away_score)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(user_id, match_id) DO UPDATE SET
                home_score    = EXCLUDED.home_score,
                away_score    = EXCLUDED.away_score,
                submitted_at  = CURRENT_TIMESTAMP,
                points_earned = NULL
        """, (session["user_id"], match_id, home, away))
        saved += 1

    conn.commit()
    cur.close()
    conn.close()

    if saved:
        flash(f"✅ {saved} prediction{'s' if saved != 1 else ''} saved!")
    if locked:
        flash(f"⏰ {locked} match{'es' if locked != 1 else ''} already locked — skipped.")
    return redirect(url_for("predict"))

@app.route("/live-scores")
def live_scores():
    conn    = get_db()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    matches = db_fetchall(cur, "SELECT id, home_score, away_score FROM matches WHERE status='IN_PLAY'")
    cur.close(); conn.close()
    return {"matches": [{"id": m["id"], "home": m["home_score"], "away": m["away_score"]} for m in matches]}

@app.route("/profile/<username>")
def profile(username):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
    if not user:
        cur.close(); conn.close()
        flash(f"Player '{username}' not found.")
        return redirect(url_for("leaderboard"))
    history = db_fetchall(cur, """
        SELECT p.home_score AS pred_home, p.away_score AS pred_away,
               p.points_earned, p.submitted_at,
               m.home_team, m.away_team,
               m.home_score AS real_home, m.away_score AS real_away,
               m.kickoff_utc, m.stage, m.group_name, m.status
        FROM predictions p
        JOIN matches m ON p.match_id = m.id
        WHERE p.user_id=%s
        ORDER BY m.kickoff_utc DESC
    """, (user["id"],))
    graded  = [h for h in history if h["points_earned"] is not None]
    correct = len([h for h in graded if h["points_earned"] > 0])
    exact   = len([h for h in graded if h["points_earned"] == 3])
    accuracy = round(correct / len(graded) * 100) if graded else 0
    badges  = get_badges(user["id"], cur)
    cur.close(); conn.close()
    return render_template("profile.html",
        profile_user=user,
        history=history,
        badges=badges,
        stats={
            "total_points": user["total_points"],
            "predictions":  len(history),
            "graded":       len(graded),
            "correct":      correct,
            "exact":        exact,
            "accuracy":     accuracy,
        }
    )

@app.route("/leaderboard")
def leaderboard():
    conn  = get_db()
    cur   = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    users = db_fetchall(cur, """
        SELECT u.discord_id, u.username, u.avatar, u.ordinal_avatar, u.total_points,
               COUNT(CASE WHEN p.points_earned = 3 THEN 1 END) AS exact_scores,
               COUNT(CASE WHEN p.points_earned = 1 THEN 1 END) AS correct_winners,
               COUNT(CASE WHEN p.points_earned IS NOT NULL THEN 1 END) AS graded,
               COUNT(p.id) AS total_preds
        FROM   users u
        LEFT JOIN predictions p ON u.id = p.user_id
        GROUP  BY u.id, u.discord_id, u.username, u.avatar, u.ordinal_avatar, u.total_points
        ORDER  BY u.total_points DESC, exact_scores DESC, u.username
    """)
    cur.close()
    conn.close()
    return render_template("leaderboard.html", users=users)

@app.route("/results")
def results():
    conn    = get_db()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    matches = db_fetchall(cur, "SELECT * FROM matches WHERE status='FINISHED' ORDER BY kickoff_utc DESC")

    data = []
    for m in matches:
        preds = db_fetchall(cur, """
            SELECT u.username, u.discord_id, u.avatar,
                   p.home_score, p.away_score, p.points_earned
            FROM   predictions p
            JOIN   users u ON p.user_id = u.id
            WHERE  p.match_id = %s
            ORDER  BY COALESCE(p.points_earned,-1) DESC, u.username
        """, (m["id"],))
        data.append({"match": dict(m), "predictions": [dict(p) for p in preds]})

    cur.close()
    conn.close()
    return render_template("results.html", data=data)

@app.route("/ticker")
def ticker():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=3)
    rows = db_fetchall(cur, """
        SELECT u.username, u.ordinal_avatar,
               p.home_score, p.away_score,
               m.home_team, m.away_team
        FROM predictions p
        JOIN users u ON p.user_id = u.id
        JOIN matches m ON p.match_id = m.id
        WHERE m.status IN ('TIMED', 'SCHEDULED')
          AND m.kickoff_utc::timestamptz >= %s
          AND m.kickoff_utc::timestamptz <= %s
        ORDER BY m.kickoff_utc ASC, p.submitted_at DESC
        LIMIT 60
    """, (now, cutoff))
    cur.close()
    conn.close()
    items = []
    for r in rows:
        text = f"{r['username']} predicted {r['home_team']} {r['home_score']}-{r['away_score']} {r['away_team']}"
        items.append({"text": text, "avatar": r["ordinal_avatar"]})
    return {"items": items}

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
        else:
            return render_template("admin.html", error="Wrong password", authed=False)
    if not session.get("admin"):
        return render_template("admin.html", authed=False)
    conn  = get_db()
    cur   = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    users = db_fetchall(cur, """
        SELECT u.*, COUNT(p.id) as total_preds
        FROM users u
        LEFT JOIN predictions p ON u.id = p.user_id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    """)
    cur.execute("SELECT COUNT(*) AS c FROM users")
    total    = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE x_verified=1")
    verified = cur.fetchone()["c"]
    matches = db_fetchall(cur, """
        SELECT id, home_team, away_team, home_score, away_score, status
        FROM matches ORDER BY kickoff_utc DESC LIMIT 80
    """)
    cur.close()
    conn.close()
    return render_template("admin.html", authed=True, users=users, total=total, verified=verified,
                           matches=matches, points_msg=None, points_ok=False)

@app.route("/admin/give-points", methods=["POST"])
def admin_give_points():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    username = request.form.get("username", "").strip()
    try:
        pts = int(request.form.get("points", 0))
    except ValueError:
        pts = 0
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = db_fetchone(cur, "SELECT * FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
    if not user:
        cur.close(); conn.close()
        flash(f"User '{username}' not found.")
        return redirect(url_for("admin"))
    cur.execute("UPDATE users SET total_points = total_points + %s WHERE id=%s", (pts, user["id"]))
    conn.commit()
    new_pts = user["total_points"] + pts
    cur.close(); conn.close()
    flash(f"✅ Gave {pts:+d} pts to {user['username']} — now at {new_pts} pts.")
    return redirect(url_for("admin"))

@app.route("/admin/force-grade", methods=["POST"])
def admin_force_grade():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    from api import _calculate_points
    conn = get_db()
    _calculate_points(conn)
    conn.close()
    flash("✅ Points recalculated for all finished matches.")
    return redirect(url_for("admin"))

@app.route("/admin/set-score", methods=["POST"])
def admin_set_score():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    try:
        match_id = int(request.form.get("match_id"))
        home     = int(request.form.get("home_score"))
        away     = int(request.form.get("away_score"))
    except (ValueError, TypeError):
        flash("Invalid input.")
        return redirect(url_for("admin"))
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE matches SET home_score=%s, away_score=%s, status='FINISHED' WHERE id=%s",
                (home, away, match_id))
    conn.commit()
    cur.close()
    from api import _calculate_points
    _calculate_points(conn)
    conn.close()
    flash(f"✅ Match {match_id} set to {home}–{away} and graded.")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
