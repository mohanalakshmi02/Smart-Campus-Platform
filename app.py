import os, re, sqlite3, secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
                  DATABASE=os.environ.get("DATABASE", "data/campus.db"))
SLA_HOURS = {"high": 24, "medium": 72, "low": 120}
FMT = "%Y-%m-%dT%H:%M"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------- DB ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('admin','student','staff')), created_at TEXT);
CREATE TABLE IF NOT EXISTS facilities(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, type TEXT NOT NULL,
 location TEXT NOT NULL, capacity INTEGER NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'available');
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE, start_time TEXT NOT NULL,
 end_time TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
 reporter_id INTEGER NOT NULL REFERENCES users(id), assigned_to INTEGER REFERENCES users(id), title TEXT NOT NULL,
 description TEXT NOT NULL, priority TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT,
 sla_due TEXT, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS ix_book ON bookings(facility_id, start_time, end_time);
"""

def now(): return datetime.now().strftime(FMT)

def init_db():
    os.makedirs(os.path.dirname(app.config["DATABASE"]) or ".", exist_ok=True)
    c = sqlite3.connect(app.config["DATABASE"]); c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM users").fetchone():
        for n, e, p, r in [("Admin User", "admin@campus.edu", "admin123", "admin"),
                           ("Priya Student", "student@campus.edu", "student123", "student"),
                           ("Ravi Staff", "staff@campus.edu", "staff123", "staff"),
                           ("Meena Technician", "tech@campus.edu", "staff123", "staff")]:
            c.execute("INSERT INTO users(name,email,password,role,created_at) VALUES(?,?,?,?,?)",
                      (n, e, generate_password_hash(p), r, now()))
        for f in [("Seminar Hall A", "Hall", "Main Block, Floor 1", 150, "Projector, AC, audio system"),
                  ("Computer Lab 2", "Lab", "IT Block, Floor 2", 60, "60 workstations"),
                  ("Badminton Court", "Sports", "Sports Complex", 8, "Indoor wooden court"),
                  ("Conference Room 101", "Meeting Room", "Admin Block", 20, "Video conferencing"),
                  ("Auditorium", "Hall", "Central Campus", 500, "Stage, lighting, sound")]:
            c.execute("INSERT INTO facilities(name,type,location,capacity,description) VALUES(?,?,?,?,?)", f)
        d = datetime.now().replace(minute=0, second=0, microsecond=0)
        s = lambda days, h: (d + timedelta(days=days, hours=h)).strftime(FMT)
        for u, f, a, b, p, st in [(2, 1, s(1, 1), s(1, 3), "Guest lecture", "approved"),
                                  (2, 2, s(2, 0), s(2, 2), "Project demo", "pending"),
                                  (3, 4, s(1, 5), s(1, 6), "Faculty meeting", "pending"),
                                  (2, 3, s(-3, 0), s(-3, 1), "Practice session", "rejected")]:
            c.execute("INSERT INTO bookings(user_id,facility_id,start_time,end_time,purpose,status,created_at) VALUES(?,?,?,?,?,?,?)", (u, f, a, b, p, st, now()))
        for f, r, a, t, ds, pr, st, cr in [(2, 2, 4, "Projector not working", "No display output.", "high", "in_progress", -30),
                                           (1, 3, None, "AC leaking water", "Water near front row.", "medium", "open", -80),
                                           (3, 2, 4, "Net torn", "Replace net.", "low", "resolved", -50)]:
            created = datetime.now() + timedelta(hours=cr)
            due = created + timedelta(hours=SLA_HOURS[pr])
            c.execute("INSERT INTO tickets(facility_id,reporter_id,assigned_to,title,description,priority,status,created_at,sla_due,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (f, r, a, t, ds, pr, st, created.strftime(FMT), due.strftime(FMT),
                       (created + timedelta(hours=20)).strftime(FMT) if st == "resolved" else None))
    c.commit(); c.close()

# ---------- Auth helpers ----------
def user():
    if "uid" not in session: return None
    if "user" not in g: g.user = q("SELECT * FROM users WHERE id=?", (session["uid"],), one=True)
    return g.user

def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            u = user()
            if not u: return redirect(url_for("login"))
            if roles and u["role"] not in roles: abort(403)
            return fn(*a, **k)
        return w
    return deco

@app.context_processor
def inject():
    session.setdefault("csrf", secrets.token_hex(16))
    return dict(me=user(), csrf=session["csrf"])

@app.before_request
def csrf_protect():
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
        abort(400, "Invalid CSRF token. Reload the page and try again.")

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def err(e):
    code = getattr(e, "code", 500)
    msg = {403: "You do not have access to this page.", 404: "Page not found."}.get(code, getattr(e, "description", "Server error."))
    if request.path.startswith("/api/"): return jsonify(error=msg), code
    return render_template("error.html", code=code, msg=msg), code

def sla_state(t):
    n = now()
    if t["status"] in ("resolved", "closed"):
        return "met" if (t["resolved_at"] or n) <= t["sla_due"] else "breached"
    return "breached" if n > t["sla_due"] else "on_track"

# ---------- Auth ----------
@app.route("/")
def index(): return redirect(url_for("dashboard" if user() else "login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),), one=True)
        if u and check_password_hash(u["password"], request.form.get("password", "")):
            session.clear(); session["uid"] = u["id"]; return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("auth.html", mode="login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form; name = f.get("name", "").strip(); email = f.get("email", "").strip().lower()
        pw = f.get("password", ""); role = f.get("role", "student")
        if not (2 <= len(name) <= 60): flash("Name must be 2-60 characters.", "error")
        elif not EMAIL_RE.match(email): flash("Enter a valid email.", "error")
        elif len(pw) < 6: flash("Password must be at least 6 characters.", "error")
        elif role not in ("student", "staff"): flash("Invalid role.", "error")
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True): flash("Email already registered.", "error")
        else:
            db().execute("INSERT INTO users(name,email,password,role,created_at) VALUES(?,?,?,?,?)",
                         (name, email, generate_password_hash(pw), role, now())); db().commit()
            flash("Registration successful. Please log in.", "success"); return redirect(url_for("login"))
    return render_template("auth.html", mode="register")

@app.route("/logout", methods=["POST"])
def logout(): session.clear(); return redirect(url_for("login"))

# ---------- Dashboard ----------
@app.route("/dashboard")
@login_required()
def dashboard():
    u = user(); s = {}
    if u["role"] == "admin":
        tix = q("SELECT * FROM tickets"); states = [sla_state(t) for t in tix]
        s = dict(facilities=q("SELECT COUNT(*) c FROM facilities", one=True)["c"],
                 bookings=q("SELECT COUNT(*) c FROM bookings", one=True)["c"],
                 pending=q("SELECT COUNT(*) c FROM bookings WHERE status='pending'", one=True)["c"],
                 tickets=sum(t["status"] not in ("resolved", "closed") for t in tix),
                 breached=states.count("breached"), on_track=states.count("on_track"), met=states.count("met"),
                 unassigned=sum(t["assigned_to"] is None and t["status"] == "open" for t in tix))
        recent = q("SELECT b.*, f.name fname, u.name uname FROM bookings b JOIN facilities f ON f.id=b.facility_id JOIN users u ON u.id=b.user_id WHERE b.status='pending' ORDER BY b.created_at LIMIT 5")
    else:
        uid = u["id"]
        tix = q("SELECT * FROM tickets WHERE reporter_id=? OR assigned_to=?", (uid, uid)); states = [sla_state(t) for t in tix]
        s = dict(facilities=q("SELECT COUNT(*) c FROM facilities WHERE status='available'", one=True)["c"],
                 bookings=q("SELECT COUNT(*) c FROM bookings WHERE user_id=?", (uid,), one=True)["c"],
                 pending=q("SELECT COUNT(*) c FROM bookings WHERE user_id=? AND status='pending'", (uid,), one=True)["c"],
                 tickets=sum(t["status"] not in ("resolved", "closed") for t in tix), breached=states.count("breached"),
                 on_track=states.count("on_track"), met=states.count("met"),
                 assigned=q("SELECT COUNT(*) c FROM tickets WHERE assigned_to=? AND status NOT IN('resolved','closed')", (uid,), one=True)["c"])
        recent = q("SELECT b.*, f.name fname FROM bookings b JOIN facilities f ON f.id=b.facility_id WHERE b.user_id=? ORDER BY b.start_time DESC LIMIT 5", (uid,))
    return render_template("dashboard.html", s=s, recent=recent)

# ---------- Facilities ----------
def facility_data(f):
    d = dict(name=f.get("name", "").strip(), type=f.get("type", "").strip(), location=f.get("location", "").strip(),
             description=f.get("description", "").strip(), status=f.get("status", "available"))
    try: d["capacity"] = int(f.get("capacity", ""))
    except ValueError: d["capacity"] = 0
    if not d["name"] or not d["type"] or not d["location"]: return d, "Name, type and location are required."
    if not 1 <= d["capacity"] <= 10000: return d, "Capacity must be between 1 and 10000."
    if d["status"] not in ("available", "maintenance", "closed"): return d, "Invalid status."
    return d, None

@app.route("/facilities")
@login_required()
def facilities():
    t = request.args.get("type", "")
    rows = q("SELECT * FROM facilities WHERE (?='' OR type=?) ORDER BY name", (t, t))
    types = [r["type"] for r in q("SELECT DISTINCT type FROM facilities ORDER BY type")]
    return render_template("facilities.html", rows=rows, types=types, sel=t)

@app.route("/facilities/new", methods=["GET", "POST"])
@app.route("/facilities/<int:fid>/edit", methods=["GET", "POST"])
@login_required("admin")
def facility_form(fid=None):
    f = q("SELECT * FROM facilities WHERE id=?", (fid,), one=True) if fid else None
    if fid and not f: abort(404)
    if request.method == "POST":
        d, e = facility_data(request.form)
        if e: flash(e, "error"); f = d
        else:
            try:
                if fid: db().execute("UPDATE facilities SET name=?,type=?,location=?,capacity=?,description=?,status=? WHERE id=?",
                                     (d["name"], d["type"], d["location"], d["capacity"], d["description"], d["status"], fid))
                else: db().execute("INSERT INTO facilities(name,type,location,capacity,description,status) VALUES(?,?,?,?,?,?)",
                                   (d["name"], d["type"], d["location"], d["capacity"], d["description"], d["status"]))
                db().commit(); flash("Facility saved.", "success"); return redirect(url_for("facilities"))
            except sqlite3.IntegrityError: flash("A facility with that name already exists.", "error"); f = d
    return render_template("facility_form.html", f=f)

@app.route("/facilities/<int:fid>/delete", methods=["POST"])
@login_required("admin")
def facility_delete(fid):
    db().execute("DELETE FROM facilities WHERE id=?", (fid,)); db().commit()
    flash("Facility deleted.", "success"); return redirect(url_for("facilities"))

# ---------- Bookings ----------
def overlap(fid, start, end, exclude=None):
    return q("SELECT 1 FROM bookings WHERE facility_id=? AND status IN('pending','approved') AND start_time<? AND end_time>? AND id IS NOT ?",
             (fid, end, start, exclude), one=True)

@app.route("/api/availability")
@login_required()
def api_availability():
    fid, day = request.args.get("facility_id", type=int), request.args.get("date", "")
    if not fid or not re.match(r"^\d{4}-\d{2}-\d{2}$", day): return jsonify(error="facility_id and date required"), 400
    rows = q("SELECT start_time, end_time, status FROM bookings WHERE facility_id=? AND status IN('pending','approved') AND start_time<? AND end_time>? ORDER BY start_time",
             (fid, day + "T23:59", day + "T00:00"))
    return jsonify(booked=[dict(r) for r in rows])

@app.route("/bookings", methods=["GET", "POST"])
@login_required()
def bookings():
    u = user()
    if request.method == "POST":
        if u["role"] == "admin": abort(403)
        f = request.form; fac = q("SELECT * FROM facilities WHERE id=?", (f.get("facility_id", type=int) if False else f.get("facility_id"),), one=True)
        purpose = f.get("purpose", "").strip()
        try: s, e = datetime.strptime(f.get("start", ""), FMT), datetime.strptime(f.get("end", ""), FMT)
        except ValueError: s = e = None
        err = None
        if not fac: err = "Select a valid facility."
        elif fac["status"] != "available": err = "This facility is not available for booking."
        elif not s or not e: err = "Enter valid start and end times."
        elif e <= s: err = "End time must be after start time."
        elif s < datetime.now(): err = "Start time cannot be in the past."
        elif e - s > timedelta(hours=8): err = "Bookings are limited to 8 hours."
        elif not 3 <= len(purpose) <= 200: err = "Purpose must be 3-200 characters."
        else:
            db().execute("BEGIN IMMEDIATE")
            if overlap(fac["id"], f["start"], f["end"]): err = "That time slot overlaps an existing booking for this facility."
            else:
                db().execute("INSERT INTO bookings(user_id,facility_id,start_time,end_time,purpose,created_at) VALUES(?,?,?,?,?,?)",
                             (u["id"], fac["id"], f["start"], f["end"], purpose, now()))
            db().commit()
        flash(err or "Booking request submitted for approval.", "error" if err else "success")
        return redirect(url_for("bookings"))
    base = "SELECT b.*, f.name fname, u.name uname FROM bookings b JOIN facilities f ON f.id=b.facility_id JOIN users u ON u.id=b.user_id "
    rows = q(base + "ORDER BY b.created_at DESC") if u["role"] == "admin" else q(base + "WHERE b.user_id=? ORDER BY b.start_time DESC", (u["id"],))
    facs = q("SELECT * FROM facilities WHERE status='available' ORDER BY name")
    return render_template("bookings.html", rows=rows, facs=facs, pre=request.args.get("facility_id", type=int), now=now())

@app.route("/bookings/<int:bid>/<action>", methods=["POST"])
@login_required()
def booking_action(bid, action):
    u = user(); b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    if not b: abort(404)
    if action in ("approve", "reject"):
        if u["role"] != "admin": abort(403)
        if b["status"] != "pending": flash("Only pending bookings can be reviewed.", "error")
        else:
            db().execute("UPDATE bookings SET status=? WHERE id=?", ("approved" if action == "approve" else "rejected", bid)); db().commit()
            flash(f"Booking {action}d.", "success")
    elif action == "cancel":
        if b["user_id"] != u["id"] and u["role"] != "admin": abort(403)
        if b["status"] in ("pending", "approved"):
            db().execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,)); db().commit(); flash("Booking cancelled.", "success")
    else: abort(404)
    return redirect(request.referrer or url_for("bookings"))

# ---------- Maintenance ----------
@app.route("/tickets", methods=["GET", "POST"])
@login_required()
def tickets():
    u = user()
    if request.method == "POST":
        f = request.form; title = f.get("title", "").strip(); desc = f.get("description", "").strip(); pr = f.get("priority", "")
        fac = q("SELECT id FROM facilities WHERE id=?", (f.get("facility_id"),), one=True)
        if not fac: flash("Select a valid facility.", "error")
        elif not 3 <= len(title) <= 100: flash("Title must be 3-100 characters.", "error")
        elif not 5 <= len(desc) <= 1000: flash("Description must be 5-1000 characters.", "error")
        elif pr not in SLA_HOURS: flash("Invalid priority.", "error")
        else:
            due = (datetime.now() + timedelta(hours=SLA_HOURS[pr])).strftime(FMT)
            db().execute("INSERT INTO tickets(facility_id,reporter_id,title,description,priority,created_at,sla_due) VALUES(?,?,?,?,?,?,?)",
                         (fac["id"], u["id"], title, desc, pr, now(), due)); db().commit(); flash("Maintenance ticket created.", "success")
        return redirect(url_for("tickets"))
    base = "SELECT t.*, f.name fname, r.name rname, a.name aname FROM tickets t JOIN facilities f ON f.id=t.facility_id JOIN users r ON r.id=t.reporter_id LEFT JOIN users a ON a.id=t.assigned_to "
    rows = q(base + "ORDER BY t.created_at DESC") if u["role"] == "admin" else \
        q(base + "WHERE t.reporter_id=? OR t.assigned_to=? ORDER BY t.created_at DESC", (u["id"], u["id"]))
    rows = [dict(r, sla=sla_state(r)) for r in rows]
    return render_template("tickets.html", rows=rows, facs=q("SELECT id,name FROM facilities ORDER BY name"),
                           staff=q("SELECT id,name FROM users WHERE role='staff' ORDER BY name"), sla_hours=SLA_HOURS)

@app.route("/tickets/<int:tid>/update", methods=["POST"])
@login_required("admin", "staff")
def ticket_update(tid):
    u = user(); t = q("SELECT * FROM tickets WHERE id=?", (tid,), one=True)
    if not t: abort(404)
    if u["role"] == "staff" and t["assigned_to"] != u["id"]: abort(403)
    status = request.form.get("status", t["status"])
    if status not in ("open", "in_progress", "resolved", "closed"): abort(400, "Invalid status.")
    assignee = t["assigned_to"]
    if u["role"] == "admin":
        a = request.form.get("assigned_to", "")
        if a:
            if not q("SELECT 1 FROM users WHERE id=? AND role='staff'", (a,), one=True): abort(400, "Invalid assignee.")
            assignee = int(a)
        else: assignee = None
        if assignee and status == "open": status = "in_progress" if t["status"] == "open" else status
    done = status in ("resolved", "closed")
    db().execute("UPDATE tickets SET status=?, assigned_to=?, resolved_at=? WHERE id=?",
                 (status, assignee, (t["resolved_at"] or now()) if done else None, tid)); db().commit()
    flash("Ticket updated.", "success"); return redirect(url_for("tickets"))

init_db()
if __name__ == "__main__": app.run(host="0.0.0.0", port=5000, debug=False)
