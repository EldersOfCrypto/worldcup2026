from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from database import get_db, init_db
from api import fetch_and_update_matches, is_locked, time_until_lock
import requests
import os

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
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        conn.close()
    return {"current_user": user}

def avatar_url(discord_id, avatar_hash):
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/0.png"

app.jinja_env.globals["avatar_url"] = avatar_url

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

    # Exchange code for access token
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

    # Get user info from Discord
    user_resp = requests.get(DISCORD_API_URL, headers={"Authorization": f"Bearer {access_token}"})
    if user_resp.status_code != 200:
        flash("Discord login failed — could not get user info.")
        return redirect(url_for("index"))

    discord_user = user_resp.json()
    discord_id   = discord_user["id"]
    username     = discord_user.get("global_name") or discord_user.get("username", "Unknown")
    avatar       = discord_user.get("avatar")

    # Save or update user in DB
    conn = get_db()
    conn.execute("""
        INSERT INTO users (discord_id, username, avatar)
        VALUES (?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            username = excluded.username,
            avatar   = excluded.avatar
    """, (discord_id, username, avatar))
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
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
    verifier   = _x_code_verifier()
    challenge  = _x_code_challenge(verifier)
    state      = _secrets.token_urlsafe(16)
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

    verifier = session.get("x_code_verifier", "")

    # Exchange code for token
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

    # Get X user info
    user_resp = requests.get(X_USER_URL,
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if user_resp.status_code != 200:
        flash("X login failed — could not get user info.")
        return redirect(url_for("verify"))

    x_user    = user_resp.json().get("data", {})
    x_id      = x_user.get("id")
    x_username = x_user.get("username")

    # Save X info (connected but not yet verified)
    conn = get_db()
    conn.execute("""
        UPDATE users SET x_id=?, x_username=?, x_verified=0
        WHERE id=?
    """, (x_id, x_username, session["user_id"]))
    conn.commit()
    conn.close()

    # Store token in session for polling
    session["x_access_token"] = access_token
    session["x_user_id"]      = x_id
    session["x_username"]     = x_username

    # Check immediately — owner bypass or already retweeted
    is_owner  = x_username and x_username.lower() == "cryptoelders"
    retweeted = is_owner or _check_retweet(access_token, x_id)
    if retweeted:
        _mark_verified(x_id, x_username, session["user_id"])
        flash(f"✅ Verified! Welcome @{x_username} — you can now predict!")
        return redirect(url_for("predict"))

    return redirect(url_for("verify"))

def _mark_verified(x_id, x_username, user_id):
    conn = get_db()
    conn.execute("UPDATE users SET x_id=?, x_username=?, x_verified=1 WHERE id=?",
                 (x_id, x_username, user_id))
    conn.commit()
    conn.close()

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

def _check_retweet(access_token: str, x_user_id: str) -> bool:
    if not X_TWEET_ID:
        return True
    url  = f"https://api.twitter.com/2/tweets/{X_TWEET_ID}/retweeted_by"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    print(f"[RETWEET CHECK] status={resp.status_code} user_id={x_user_id}")
    print(f"[RETWEET CHECK] body={resp.text[:500]}")
    if resp.status_code != 200:
        return False
    users = resp.json().get("data", [])
    return any(u["id"] == x_user_id for u in users)

@app.route("/verify")
def verify():
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
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
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    if not user["x_verified"]:
        return redirect(url_for("verify"))

    conn = get_db()
    rows = conn.execute("""
        SELECT m.*,
               p.home_score AS pred_home,
               p.away_score AS pred_away
        FROM   matches m
        LEFT JOIN predictions p
               ON m.id = p.match_id AND p.user_id = ?
        WHERE  m.status IN ('TIMED','SCHEDULED','IN_PLAY')
        ORDER  BY m.kickoff_utc
    """, (session["user_id"],)).fetchall()
    conn.close()

    from collections import defaultdict
    grouped = defaultdict(list)
    for r in rows:
        m = dict(r)
        m["locked"]          = is_locked(m["kickoff_utc"])
        m["time_until_lock"] = time_until_lock(m["kickoff_utc"])
        grouped[fmt_date(m["kickoff_utc"])].append(m)

    return render_template("predict.html", grouped=dict(grouped))

@app.route("/predict/<int:match_id>", methods=["POST"])
def submit_prediction(match_id):
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn  = get_db()
    user  = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not user["x_verified"]:
        conn.close()
        return redirect(url_for("verify"))

    conn  = get_db()
    match = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()

    if not match or is_locked(match["kickoff_utc"]):
        flash("⏰ Predictions are locked for this match.")
        conn.close()
        return redirect(url_for("predict"))

    try:
        home = max(0, int(request.form.get("home_score", 0)))
        away = max(0, int(request.form.get("away_score", 0)))
    except ValueError:
        flash("Invalid score.")
        conn.close()
        return redirect(url_for("predict"))

    conn.execute("""
        INSERT INTO predictions (user_id, match_id, home_score, away_score)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id, match_id) DO UPDATE SET
            home_score    = excluded.home_score,
            away_score    = excluded.away_score,
            submitted_at  = CURRENT_TIMESTAMP,
            points_earned = NULL
    """, (session["user_id"], match_id, home, away))
    conn.commit()
    conn.close()
    flash("✅ Prediction saved!")
    return redirect(url_for("predict"))

@app.route("/leaderboard")
def leaderboard():
    conn  = get_db()
    users = conn.execute("""
        SELECT u.discord_id, u.username, u.avatar, u.total_points,
               COUNT(CASE WHEN p.points_earned = 3 THEN 1 END) AS exact_scores,
               COUNT(CASE WHEN p.points_earned = 1 THEN 1 END) AS correct_winners,
               COUNT(CASE WHEN p.points_earned IS NOT NULL THEN 1 END) AS graded,
               COUNT(p.id) AS total_preds
        FROM   users u
        LEFT JOIN predictions p ON u.id = p.user_id
        GROUP  BY u.id
        ORDER  BY u.total_points DESC, exact_scores DESC, u.username
    """).fetchall()
    conn.close()
    return render_template("leaderboard.html", users=users)

@app.route("/results")
def results():
    conn    = get_db()
    matches = conn.execute("""
        SELECT * FROM matches WHERE status='FINISHED'
        ORDER BY kickoff_utc DESC
    """).fetchall()

    data = []
    for m in matches:
        preds = conn.execute("""
            SELECT u.username, u.discord_id, u.avatar,
                   p.home_score, p.away_score, p.points_earned
            FROM   predictions p
            JOIN   users u ON p.user_id = u.id
            WHERE  p.match_id = ?
            ORDER  BY COALESCE(p.points_earned,-1) DESC, u.username
        """, (m["id"],)).fetchall()
        data.append({"match": dict(m), "predictions": [dict(p) for p in preds]})

    conn.close()
    return render_template("results.html", data=data)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
