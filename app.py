"""Traffic Rules & Fines Management System - Flask + SQLite, mobile-first, live updates via SSE."""
import os, re, json, queue, secrets
import pg8000
from urllib.parse import urlsplit, unquote
from dotenv import load_dotenv
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, g, request, session, redirect, url_for, render_template,
                   flash, Response, abort, jsonify)
from jinja2 import FileSystemLoader
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# ---- Fine rules (edit amounts here; server always computes fines) ----
RULES = {
    1: ("Riding without helmet", 1000),
    2: ("Driving without seatbelt", 1000),
    3: ("Jumping red signal", 5000),
    4: ("Over-speeding", 2000),
    5: ("Driving without licence", 5000),
    6: ("Driving without insurance", 2000),
    7: ("Using mobile phone while driving", 5000),
    8: ("Wrong / illegal parking", 500),
}
REPEAT_WINDOW_DAYS, REPEAT_MULTIPLIER = 365, 1.5
VEHICLE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")


def normalize_vehicle(v: str) -> str:
    return re.sub(r"[\s\-]", "", (v or "")).upper()


def valid_vehicle(v: str) -> bool:
    return bool(VEHICLE_RE.match(v))


def calc_fine(code: int, prior_same: int) -> int:
    """Base fine, escalated for repeat offences of the same violation within the window."""
    if code not in RULES:
        raise ValueError("Unknown violation code")
    base = RULES[code][1]
    return int(base * REPEAT_MULTIPLIER) if prior_same > 0 else base


# ---- App / DB ----
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
subs: list = []


class PGRow:
    def __init__(self, columns, values):
        self._data = dict(zip(columns, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()


class PGCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    def execute(self, query, params=()):
        query = query.replace("?", "%s")
        self.cursor.execute(query, params)

        if query.lstrip().upper().startswith("INSERT INTO CHALLANS"):
            self.cursor.execute("SELECT currval('public.challans_id_seq')")
            self.lastrowid = self.cursor.fetchone()[0]

        return self

    def fetchone(self):
        row = self.cursor.fetchone()

        if row is None:
            return None

        columns = [d[0] for d in self.cursor.description]
        return PGRow(columns, row)

    def fetchall(self):
        rows = self.cursor.fetchall()

        if not rows:
            return []

        columns = [d[0] for d in self.cursor.description]

        return [
            PGRow(columns, row)
            for row in rows
        ]


class PGConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        cursor = self.connection.cursor()
        return PGCursor(cursor).execute(query, params)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def connect_supabase():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is missing from .env")

    parsed = urlsplit(database_url)

    username = unquote(parsed.username)
    password = unquote(parsed.password)
    host = parsed.hostname
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "postgres"

    connection = pg8000.connect(
        user=username,
        password=password,
        host=host,
        port=port,
        database=database,
        ssl_context=True
    )

    return PGConnection(connection)


def db():
    if "db" not in g:
        g.db = connect_supabase()
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    pass


def audit(action, detail=""):
    db().execute("INSERT INTO audit(at,who,action,detail) VALUES(?,?,?,?)",
                 (now(), session.get("name", "-"), action, detail))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def publish(event: dict):
    for q in list(subs):
        q.put(json.dumps(event))


# ---- Auth / CSRF ----
def login_required(role=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return f(*a, **k)
        return w
    return deco


@app.before_request
def csrf_protect():
    if request.method == "POST":
        if request.form.get("_csrf") != session.get("_csrf"):
            abort(400, "Bad CSRF token")


@app.context_processor
def inject():
    session.setdefault("_csrf", secrets.token_hex(16))
    return dict(csrf=session["_csrf"], rules=RULES, user=session.get("name"), role=session.get("role"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?", (request.form.get("username", ""),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.update(uid=u["id"], name=u["name"], role=u["role"])
            audit("login"); db().commit()
            return redirect(url_for("dashboard"))
        flash("Invalid username or password", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("lookup"))


# ---- Pages ----
def stats():
    d = db()
    today = datetime.now().strftime("%Y-%m-%d")
    r = d.execute("""SELECT COUNT(*) n,
        COALESCE(SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END),0) pend, COALESCE(SUM(CASE WHEN status='PAID' THEN 1 ELSE 0 END),0) paid,
        COALESCE(SUM(CASE WHEN status='PAID' THEN fine_amount END),0) collected,
        COALESCE(SUM(CASE WHEN status='PENDING' THEN fine_amount END),0) due FROM challans""").fetchone()
    t = d.execute("SELECT COUNT(*) FROM challans WHERE created_at LIKE ?", (today + "%",)).fetchone()[0]
    return dict(total=r["n"], pending=r["pend"], paid=r["paid"], collected=r["collected"], due=r["due"], today=t)


@app.route("/")
@login_required()
def dashboard():
    d = db()
    recent = d.execute("SELECT * FROM challans ORDER BY id DESC LIMIT 10").fetchall()
    top = d.execute("""SELECT violation_name n, COUNT(*) c FROM challans GROUP BY violation_code, violation_name
                       ORDER BY c DESC LIMIT 5""").fetchall()
    return render_template("dashboard.html", s=stats(), recent=recent, top=top)


@app.route("/api/stats")
@login_required()
def api_stats():
    return jsonify(stats())


@app.route("/api/fine")
@login_required()
def api_fine():
    try:
        code = int(request.args.get("code", 0))
        return jsonify(fine=calc_fine(code, prior_count(normalize_vehicle(request.args.get("vehicle", "")), code)))
    except ValueError:
        return jsonify(fine=0)


def prior_count(vehicle, code):
    since = (datetime.now() - timedelta(days=REPEAT_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    return db().execute("SELECT COUNT(*) FROM challans WHERE vehicle_no=? AND violation_code=? AND created_at>=?",
                        (vehicle, code, since)).fetchone()[0]


@app.route("/new", methods=["GET", "POST"])
@login_required()
def new():
    if request.method == "POST":
        f = request.form
        v = normalize_vehicle(f.get("vehicle_no"))
        try:
            code = int(f.get("violation_code", ""))
            if code not in RULES:
                raise ValueError
        except ValueError:
            flash("Choose a valid violation", "err"); return render_template("new.html", f=f), 400
        if not valid_vehicle(v):
            flash("Invalid vehicle number (e.g. AP31AB1234)", "err"); return render_template("new.html", f=f), 400
        owner = f.get("owner_name", "").strip()[:80]
        if not owner:
            flash("Owner name is required", "err"); return render_template("new.html", f=f), 400
        cuid = f.get("client_uuid") or secrets.token_hex(8)
        d = db()
        dup = d.execute("SELECT challan_no FROM challans WHERE client_uuid=?", (cuid,)).fetchone()
        if dup:  # idempotent resubmit
            return redirect(url_for("receipt", no=dup[0]))
        fine = calc_fine(code, prior_count(v, code))
        cur = d.execute("""INSERT INTO challans(vehicle_no,owner_name,phone,violation_code,violation_name,
            fine_amount,location,notes,officer,created_at,client_uuid) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (v, owner, f.get("phone", "")[:15], code, RULES[code][0], fine, f.get("location", "")[:120],
             f.get("notes", "")[:300], session["name"], now(), cuid))
        no = "CH%05d" % cur.lastrowid
        d.execute("UPDATE challans SET challan_no=? WHERE id=?", (no, cur.lastrowid))
        audit("challan.create", no); d.commit()
        publish(dict(type="challan.created", no=no, vehicle=v, violation=RULES[code][0], fine=fine, at=now()))
        flash("Challan %s issued" % no, "ok")
        return redirect(url_for("receipt", no=no))
    return render_template("new.html", f={"client_uuid": secrets.token_hex(8)})


@app.route("/c/<no>")
@login_required()
def receipt(no):
    c = db().execute("SELECT * FROM challans WHERE challan_no=?", (no,)).fetchone() or abort(404)
    return render_template("receipt.html", c=c)


@app.route("/pay/<no>", methods=["POST"])
@login_required()
def pay(no):
    d = db()
    d.execute("UPDATE challans SET status='PAID', paid_at=? WHERE challan_no=? AND status='PENDING'", (now(), no))
    audit("challan.paid", no); d.commit()
    publish(dict(type="payment.completed", no=no))
    flash("Marked as paid", "ok")
    return redirect(url_for("receipt", no=no))


@app.route("/search")
@login_required()
def search():
    v = normalize_vehicle(request.args.get("v", ""))
    rows, tot = [], None
    if v:
        rows = db().execute("SELECT * FROM challans WHERE vehicle_no=? ORDER BY id DESC", (v,)).fetchall()
        tot = dict(total=sum(r["fine_amount"] for r in rows),
                   paid=sum(r["fine_amount"] for r in rows if r["status"] == "PAID"),
                   pending=sum(r["fine_amount"] for r in rows if r["status"] == "PENDING"))
    return render_template("search.html", v=v, rows=rows, tot=tot)


@app.route("/records")
@login_required()
def records():
    st = request.args.get("status", "")
    q = "SELECT * FROM challans" + (" WHERE status=?" if st in ("PAID", "PENDING") else "") + " ORDER BY id DESC LIMIT 200"
    rows = db().execute(q, (st,) if st in ("PAID", "PENDING") else ()).fetchall()
    return render_template("records.html", rows=rows, st=st)


@app.route("/rules")
@login_required()
def rules_page():
    return render_template("rules.html")


@app.route("/audit")
@login_required("admin")
def audit_page():
    rows = db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("audit.html", rows=rows)


@app.route("/lookup")
def lookup():
    v = normalize_vehicle(request.args.get("v", ""))
    rows = db().execute("SELECT * FROM challans WHERE vehicle_no=? ORDER BY id DESC", (v,)).fetchall() if v else []
    return render_template("lookup.html", v=v, rows=rows)


@app.route("/stream")
@login_required()
def stream():
    q = queue.Queue(); subs.append(q)

    def gen():
        try:
            while True:
                try:
                    yield "data: %s\n\n" % q.get(timeout=20)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            if q in subs:
                subs.remove(q)
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/healthz")
def health():
    return "ok"


# ---- Templates (kept inline so the project stays a single runnable file) ----
BASE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b5fff"><title>{% block title %}Traffic Fines{% endblock %}</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--tx:#14213d;--mu:#667;--pr:#0b5fff;--ok:#0a8a4a;--er:#c62828;--wa:#b26a00;--bd:#e3e7f0}
@media(prefers-color-scheme:dark){:root{--bg:#0e1420;--card:#171f2e;--tx:#e8ecf5;--mu:#97a2b8;--bd:#263047}}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--tx);padding-bottom:76px}
header{background:var(--pr);color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
header b{font-size:1.05rem}header form{margin:0}nav{display:none}
main{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(2,1fr)}
.stat b{display:block;font-size:1.6rem}.stat span{color:var(--mu);font-size:.85rem}
label{display:block;margin:12px 0 4px;font-weight:600}
input,select,textarea,button,.btn{font:inherit;width:100%;min-height:46px;padding:10px 12px;border-radius:10px;border:1px solid var(--bd);background:var(--card);color:var(--tx)}
button,.btn{background:var(--pr);color:#fff;border:0;font-weight:600;text-align:center;text-decoration:none;display:inline-block;cursor:pointer}
button.sec{background:transparent;color:#fff;border:1px solid #fff9;width:auto;min-height:36px;padding:4px 12px}
.badge{padding:2px 10px;border-radius:99px;font-size:.8rem;font-weight:700}
.PAID{background:#0a8a4a22;color:var(--ok)}.PENDING{background:#b26a0022;color:var(--wa)}
.flash{padding:12px;border-radius:10px;margin-bottom:12px}.flash.ok{background:#0a8a4a22;color:var(--ok)}.flash.err{background:#c6282822;color:var(--er)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--bd)}
.tabbar{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--bd);display:flex;padding-bottom:env(safe-area-inset-bottom)}
.tabbar a{flex:1;text-align:center;padding:12px 4px;color:var(--tx);text-decoration:none;font-size:.82rem;min-height:48px}
.big{font-size:2rem;font-weight:800}.muted{color:var(--mu)}.live{color:var(--ok);font-size:.8rem}
@media(max-width:699px){table.resp thead{display:none}table.resp tr{display:block;border:1px solid var(--bd);border-radius:10px;margin-bottom:10px;padding:6px}
table.resp td{display:flex;justify-content:space-between;border:0;padding:4px 8px}table.resp td:before{content:attr(data-l);color:var(--mu)}}
@media(min-width:700px){.grid{grid-template-columns:repeat(4,1fr)}body{padding-bottom:0}.tabbar{display:none}
nav{display:flex;gap:6px}nav a{color:#fff;text-decoration:none;padding:8px 12px;border-radius:8px}nav a:hover{background:#fff3}}
@media print{header,.tabbar,.noprint{display:none!important}body{background:#fff;color:#000}.card{border:1px solid #000}}
</style></head><body>
<header><b>🚦 Traffic Fines</b>
{% if user %}<nav><a href="/">Dashboard</a><a href="/new">New</a><a href="/search">Search</a><a href="/records">Records</a><a href="/rules">Rules</a>{% if role=='admin' %}<a href="/audit">Audit</a>{% endif %}</nav>
<form method=post action="/logout"><input type=hidden name=_csrf value="{{csrf}}"><button class=sec>Logout</button></form>
{% else %}<a href="/login" style="color:#fff">Officer login</a>{% endif %}</header>
<main>{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{c}}">{{m}}</div>{% endfor %}
{% block body %}{% endblock %}</main>
{% if user %}<div class=tabbar><a href="/">🏠<br>Home</a><a href="/new">➕<br>New</a><a href="/search">🔍<br>Search</a><a href="/records">📋<br>Records</a></div>{% endif %}
</body></html>"""

LOGIN = """{% extends 'base.html' %}{% block body %}<div class=card style="max-width:420px;margin:30px auto"><h2>Officer login</h2>
<form method=post><input type=hidden name=_csrf value="{{csrf}}"><label>Username</label><input name=username autocomplete=username required>
<label>Password</label><input name=password type=password autocomplete=current-password required><br><br><button>Sign in</button></form>
<p class=muted>Demo: officer / officer123 · admin / admin123</p><p><a href="/lookup">Check my fines (public)</a></p></div>{% endblock %}"""

ROWS = """{% macro table(rows) %}<table class=resp><thead><tr><th>Challan<th>Vehicle<th>Violation<th>Fine<th>Status<th>Time</tr></thead><tbody>
{% for r in rows %}<tr><td data-l=Challan><a href="/c/{{r.challan_no}}">{{r.challan_no}}</a><td data-l=Vehicle>{{r.vehicle_no}}
<td data-l=Violation>{{r.violation_name}}<td data-l=Fine>₹{{r.fine_amount}}<td data-l=Status><span class="badge {{r.status}}">{{r.status}}</span><td data-l=Time>{{r.created_at}}</tr>
{% else %}<tr><td colspan=6 class=muted>No records</tr>{% endfor %}</tbody></table>{% endmacro %}"""

DASH = """{% extends 'base.html' %}{% from 'rows.html' import table %}{% block body %}
<p><span class=live id=ls>● connecting…</span></p>
<div class=grid><div class="card stat"><b id=s_today>{{s.today}}</b><span>Today</span></div>
<div class="card stat"><b id=s_pending>{{s.pending}}</b><span>Pending</span></div>
<div class="card stat"><b id=s_paid>{{s.paid}}</b><span>Paid</span></div>
<div class="card stat"><b>₹<span id=s_col>{{s.collected}}</span></b><span>Collected</span></div></div>
<div class=card><h3>Live feed</h3><div id=feed class=muted>Waiting for new challans…</div></div>
<div class=card><h3>Recent challans</h3>{{ table(recent) }}</div>
<div class=card><h3>Top violations</h3>{% for t in top %}<div>{{t.n}} — <b>{{t.c}}</b></div>{% else %}<span class=muted>No data</span>{% endfor %}</div>
<script>
const es=new EventSource('/stream'),ls=document.getElementById('ls'),feed=document.getElementById('feed');
es.onopen=()=>ls.textContent='● live';es.onerror=()=>ls.textContent='○ reconnecting…';
async function refresh(){const s=await (await fetch('/api/stats')).json();
s_today.textContent=s.today;s_pending.textContent=s.pending;s_paid.textContent=s.paid;s_col.textContent=s.collected}
es.onmessage=e=>{const d=JSON.parse(e.data);if(feed.classList.contains('muted')){feed.textContent='';feed.classList.remove('muted')}
const p=document.createElement('div');p.textContent=d.type==='challan.created'?`🆕 ${d.no} · ${d.vehicle} · ${d.violation} · ₹${d.fine}`:`✅ ${d.no} paid`;
feed.prepend(p);refresh()};
</script>{% endblock %}"""

NEW = """{% extends 'base.html' %}{% block body %}<div class=card><h2>New violation</h2>
<form method=post><input type=hidden name=_csrf value="{{csrf}}"><input type=hidden name=client_uuid value="{{f.client_uuid}}">
<label>Vehicle number</label><input name=vehicle_no id=veh value="{{f.vehicle_no}}" placeholder="AP31AB1234" autocapitalize=characters required>
<label>Owner name</label><input name=owner_name value="{{f.owner_name}}" required>
<label>Phone</label><input name=phone type=tel inputmode=tel value="{{f.phone}}">
<label>Violation</label><select name=violation_code id=vc required><option value="">Select…</option>
{% for k,v in rules.items() %}<option value={{k}} {{'selected' if f.violation_code==k|string}}>{{v[0]}} (₹{{v[1]}})</option>{% endfor %}</select>
<p class=big>Fine: ₹<span id=fine>0</span></p><p class=muted id=rep></p>
<label>Location</label><input name=location value="{{f.location}}"><button type=button class=noprint style="margin-top:6px;background:#556" onclick="gps()">📍 Use my location</button>
<label>Notes</label><textarea name=notes rows=2>{{f.notes}}</textarea><br><br><button>Issue challan</button></form></div>
<script>
async function upd(){const c=vc.value;if(!c){fine.textContent=0;return}
const r=await (await fetch(`/api/fine?code=${c}&vehicle=${encodeURIComponent(veh.value)}`)).json();fine.textContent=r.fine;
const base={{ rules|tojson }}[c][1];rep.textContent=r.fine>base?'Repeat offence: fine escalated':''}
vc.onchange=upd;veh.onchange=upd;
function gps(){navigator.geolocation&&navigator.geolocation.getCurrentPosition(p=>document.querySelector('[name=location]').value=p.coords.latitude.toFixed(5)+', '+p.coords.longitude.toFixed(5))}
</script>{% endblock %}"""

RECEIPT = """{% extends 'base.html' %}{% block body %}<div class=card><h2>Fine receipt</h2>
<p class=big>{{c.challan_no}} <span class="badge {{c.status}}">{{c.status}}</span></p>
<table><tr><th>Vehicle<td>{{c.vehicle_no}}<tr><th>Owner<td>{{c.owner_name}}<tr><th>Violation<td>{{c.violation_name}}
<tr><th>Fine<td><b>₹{{c.fine_amount}}</b><tr><th>Location<td>{{c.location}}<tr><th>Officer<td>{{c.officer}}
<tr><th>Issued<td>{{c.created_at}}{% if c.paid_at %}<tr><th>Paid<td>{{c.paid_at}}{% endif %}</table></div>
<div class=noprint>{% if c.status=='PENDING' %}<form method=post action="/pay/{{c.challan_no}}"><input type=hidden name=_csrf value="{{csrf}}"><button style="background:var(--ok)">Mark as paid</button></form><br>{% endif %}
<button onclick="print()">🖨 Print receipt</button></div>{% endblock %}"""

SEARCH = """{% extends 'base.html' %}{% from 'rows.html' import table %}{% block body %}<div class=card><h2>Search by vehicle</h2>
<form><input name=v value="{{v}}" placeholder="AP31AB1234" autocapitalize=characters><br><br><button>Search</button></form></div>
{% if tot %}<div class=grid><div class="card stat"><b>₹{{tot.total}}</b><span>Total</span></div><div class="card stat"><b>₹{{tot.paid}}</b><span>Paid</span></div>
<div class="card stat"><b>₹{{tot.pending}}</b><span>Pending</span></div></div><div class=card>{{ table(rows) }}</div>{% endif %}{% endblock %}"""

RECORDS = """{% extends 'base.html' %}{% from 'rows.html' import table %}{% block body %}<div class=card><h2>All records</h2>
<p><a href="/records">All</a> · <a href="/records?status=PENDING">Pending</a> · <a href="/records?status=PAID">Paid</a></p>{{ table(rows) }}</div>{% endblock %}"""

RULESP = """{% extends 'base.html' %}{% block body %}<div class=card><h2>Fine rules</h2><table><tr><th>#<th>Violation<th>Fine
{% for k,v in rules.items() %}<tr><td>{{k}}<td>{{v[0]}}<td>₹{{v[1]}}{% endfor %}</table>
<p class=muted>Repeat offence of the same violation within 12 months: +50%.</p></div>{% endblock %}"""

AUDIT = """{% extends 'base.html' %}{% block body %}<div class=card><h2>Audit log</h2><table class=resp><thead><tr><th>When<th>Who<th>Action<th>Detail</tr></thead>
{% for r in rows %}<tr><td data-l=When>{{r.at}}<td data-l=Who>{{r.who}}<td data-l=Action>{{r.action}}<td data-l=Detail>{{r.detail}}</tr>{% endfor %}</table></div>{% endblock %}"""

LOOKUP = """{% extends 'base.html' %}{% from 'rows.html' import table %}{% block body %}<div class=card><h2>Check my fines</h2>
<form><input name=v value="{{v}}" placeholder="Vehicle number e.g. AP31AB1234" autocapitalize=characters><br><br><button>Check</button></form></div>
{% if v %}<div class=card>{{ table(rows) }}<p class=muted>Pay at your nearest traffic station. Online payment is not enabled in this prototype.</p></div>{% endif %}{% endblock %}"""

app.jinja_loader = FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))

init_db()

if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    print(f"\nOpen on this PC: http://localhost:5000\nOpen on phone (same Wi-Fi): http://{ip}:5000\n")
    app.run(host="0.0.0.0", port=5000, threaded=True)







