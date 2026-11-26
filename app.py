"""
app.py
Aplikasi web MedSign PQ (MVP versi 3 - soft launch).
Jalankan:  python app.py   lalu buka http://127.0.0.1:5000
"""
import base64
import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from PIL import UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

import crypto_core as cc

try:  # untuk QR code tautan cek (pip install "qrcode[pil]")
    import qrcode
except ImportError:  # kalau belum terinstal, halaman tetap jalan tanpa QR code
    qrcode = None

BASE_DIR = Path(__file__).resolve().parent
INSTANCE = Path(os.environ.get("MEDSIGN_INSTANCE", BASE_DIR / "instance"))
SIGNED_DIR = INSTANCE / "signed"
DB_PATH = INSTANCE / "medsignpq.db"
ALLOWED_EXT = {"png", "jpg", "jpeg"}

# kuota_per: "masa" = kuota berlaku untuk seluruh masa aktif paket, "bulan" = kuota direset tiap bulan
PLANS = {
    "coba_gratis": {"nama": "Coba Gratis", "kuota": 5, "harga": "Gratis", "harga_rp": 0,
                    "durasi_hari": 7, "kuota_per": "masa"},
    "starter": {"nama": "Starter", "kuota": 30, "harga": "Rp25.000", "harga_rp": 25000,
                "durasi_hari": 30, "kuota_per": "masa"},
    "uji_coba": {"nama": "Mitra Uji Coba", "kuota": 300, "harga": "Gratis", "harga_rp": 0,
                 "durasi_hari": 90, "kuota_per": "bulan"},
    "klinik": {"nama": "Klinik", "kuota": 500, "harga": "Rp350.000/bulan", "harga_rp": 350000,
               "durasi_hari": 30, "kuota_per": "bulan"},
    "rumah_sakit": {"nama": "Rumah Sakit", "kuota": 2000, "harga": "Rp750.000/bulan", "harga_rp": 750000,
                    "durasi_hari": 30, "kuota_per": "bulan"},
}
PAKET_AWAL = "coba_gratis"  # paket otomatis saat akun baru dibuat
ORG_TYPES = ["Rumah Sakit", "Klinik Radiologi", "Laboratorium Radiologi",
             "Perorangan (tenaga/mahasiswa kesehatan)", "Lainnya"]
METODE_BAYAR = ["QRIS", "DANA", "GoPay", "OVO", "ShopeePay", "Transfer bank", "Tunai", "Gratis (mitra)"]

# ---------------------------------------------------------------- PENGATURAN PENJUALAN
# Ganti 3 nilai di bawah ini (atau isi lewat environment variable saat hosting).
# NOMOR_WA     : nomor WhatsApp tim untuk pemesanan, format 62xxxxxxxxxx (tanpa + dan tanpa 0 di depan)
# LINK_FORM    : link Google Form pemesanan. Kalau diisi, tombol "Beli Paket" membuka form ini.
# ADMIN_EMAILS : email akun yang boleh membuka halaman Admin (pisahkan dengan koma)
NOMOR_WA = os.environ.get("NOMOR_WA", "6281234567890")
LINK_FORM = os.environ.get("LINK_FORM", "")
ADMIN_EMAILS = {e.strip().lower() for e in
                os.environ.get("ADMIN_EMAILS", "jstiur@gmail.com,admin@medsignpq.id").split(",") if e.strip()}

app = Flask(__name__, instance_path=str(INSTANCE))
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB


# ------------------------------------------------------------------ setup
def _load_or_create(path: Path, make) -> bytes:
    if not path.exists():
        path.write_bytes(make())
    return path.read_bytes()


INSTANCE.mkdir(exist_ok=True)
SIGNED_DIR.mkdir(exist_ok=True)
app.secret_key = os.environ.get("SECRET_KEY") or _load_or_create(
    INSTANCE / "flask.key", lambda: secrets.token_hex(32).encode()).decode()
FERNET = Fernet(os.environ.get("KEY_ENCRYPTION_KEY") or _load_or_create(
    INSTANCE / "kek.key", Fernet.generate_key))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_name TEXT NOT NULL,
    org_type TEXT NOT NULL,
    pic_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    password_hash TEXT NOT NULL,
    ec_public TEXT NOT NULL,
    ml_public TEXT NOT NULL,
    ec_private_enc TEXT NOT NULL,
    ml_private_enc TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'uji_coba',
    plan_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    description TEXT,
    image_hash TEXT NOT NULL,
    width INTEGER, height INTEGER,
    signed_at TEXT NOT NULL,
    file_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    plan TEXT NOT NULL,
    harga_rp INTEGER NOT NULL,
    metode TEXT,
    catatan TEXT,
    admin_email TEXT,
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature_id TEXT,
    result TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


with sqlite3.connect(DB_PATH) as _conn:
    _conn.executescript(SCHEMA)


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@app.template_filter("tgl")
def format_tanggal(value):
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value
    bulan = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    return f"{dt.day} {bulan[dt.month - 1]} {dt.year}, {dt:%H:%M} WIB"


@app.template_filter("rupiah")
def format_rupiah(value):
    return "Rp" + f"{int(value or 0):,}".replace(",", ".")


def beli_url(plan_key="starter", email=""):
    """Tautan tombol Beli Paket: ke Google Form kalau ada, kalau tidak ke chat WhatsApp tim."""
    if LINK_FORM:
        return LINK_FORM
    pesan = f"Halo MedSign PQ, saya ingin membeli paket {PLANS[plan_key]['nama']}."
    if email:
        pesan += f" Email akun saya: {email}"
    return f"https://wa.me/{NOMOR_WA}?text=" + quote(pesan)


def qr_data_uri(text):
    """Membuat QR code dari teks (tautan cek) dalam bentuk data URI PNG untuk ditampilkan di HTML."""
    if qrcode is None:
        return None
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ auth
def current_user():
    uid = session.get("uid")
    if uid is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def is_admin(user):
    return user is not None and user["email"].lower() in ADMIN_EMAILS


@app.context_processor
def inject_user():
    user = current_user()
    return {"user": user, "PLANS": PLANS, "is_admin": is_admin(user), "beli_url": beli_url}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            flash("Silakan masuk terlebih dahulu.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin(current_user()):
            abort(404)
        return view(*args, **kwargs)
    return wrapper


def quota_used(user):
    """Jumlah citra yang sudah ditandatangani dalam periode kuota paket aktif."""
    plan = PLANS[user["plan"]]
    if plan["kuota_per"] == "masa":
        mulai = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S") - timedelta(days=plan["durasi_hari"])
        mulai = mulai.strftime("%Y-%m-%d %H:%M:%S")
    else:
        mulai = datetime.now().strftime("%Y-%m-01 00:00:00")
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM signatures WHERE user_id = ? AND signed_at >= ?",
        (user["id"], mulai)).fetchone()
    return row["n"]


def plan_expired(user):
    return now_iso() > user["plan_until"]


# ------------------------------------------------------------------ halaman publik
@app.route("/")
def index():
    db = get_db()
    stats = {
        "fasyankes": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "citra": db.execute("SELECT COUNT(*) FROM signatures").fetchone()[0],
        "cek": db.execute("SELECT COUNT(*) FROM verifications").fetchone()[0],
    }
    return render_template("index.html", stats=stats)


@app.route("/paket")
def paket():
    return render_template("paket.html")


@app.route("/daftar", methods=["GET", "POST"])
def register():
    form = request.form
    if request.method == "POST":
        org_name = form.get("org_name", "").strip()
        org_type = form.get("org_type", "")
        pic_name = form.get("pic_name", "").strip()
        email = form.get("email", "").strip().lower()
        phone = form.get("phone", "").strip()
        password = form.get("password", "")
        confirm = form.get("confirm", "")

        errors = []
        if len(org_name) < 3:
            errors.append("Nama fasyankes/instansi minimal 3 karakter.")
        if org_type not in ORG_TYPES:
            errors.append("Pilih jenis fasyankes atau perorangan.")
        if len(pic_name) < 3:
            errors.append("Nama penanggung jawab minimal 3 karakter.")
        if "@" not in email or "." not in email.split("@")[-1]:
            errors.append("Format email tidak valid.")
        if len(password) < 8:
            errors.append("Kata sandi minimal 8 karakter.")
        if password != confirm:
            errors.append("Konfirmasi kata sandi tidak sama.")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            errors.append("Email sudah terdaftar. Silakan masuk.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=form, org_types=ORG_TYPES)

        ec_priv, ml_priv = cc.generate_keypairs()
        plan_until = (datetime.now() + timedelta(days=PLANS[PAKET_AWAL]["durasi_hari"])).strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            """INSERT INTO users (org_name, org_type, pic_name, email, phone, password_hash,
               ec_public, ml_public, ec_private_enc, ml_private_enc, plan, plan_until, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_name, org_type, pic_name, email, phone, generate_password_hash(password),
             cc.public_to_pem(ec_priv.public_key()), cc.public_to_pem(ml_priv.public_key()),
             cc.encrypt_private(ec_priv, FERNET), cc.encrypt_private(ml_priv, FERNET),
             PAKET_AWAL, plan_until, now_iso()))
        db.commit()
        session.clear()
        session["uid"] = cur.lastrowid
        flash("Akun berhasil dibuat. Anda mendapat paket Coba Gratis (5 citra, 7 hari). "
              "Kunci tanda tangan hibrida Anda sudah disiapkan.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form=form, org_types=ORG_TYPES)


@app.route("/masuk", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Email atau kata sandi salah.", "error")
            return render_template("login.html", email=email)
        session.clear()
        session["uid"] = row["id"]
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
    return render_template("login.html", email="")


@app.route("/keluar")
def logout():
    session.clear()
    flash("Anda sudah keluar.", "info")
    return redirect(url_for("index"))


# ------------------------------------------------------------------ fasyankes
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    rows = get_db().execute(
        """SELECT s.*, (SELECT COUNT(*) FROM verifications v WHERE v.signature_id = s.id) AS n_cek
           FROM signatures s WHERE user_id = ? ORDER BY signed_at DESC""", (user["id"],)).fetchall()
    plan = PLANS[user["plan"]]
    used = quota_used(user)
    return render_template("dashboard.html", rows=rows, plan=plan, used=used,
                           remaining=max(plan["kuota"] - used, 0), expired=plan_expired(user))


def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.route("/tandatangani", methods=["GET", "POST"])
@login_required
def sign():
    user = current_user()
    plan = PLANS[user["plan"]]
    used = quota_used(user)
    if request.method == "POST":
        file = request.files.get("image")
        description = request.form.get("description", "").strip()[:120]
        if file is None or file.filename == "":
            flash("Pilih file citra rontgen terlebih dahulu.", "error")
            return redirect(url_for("sign"))
        if not _allowed(file.filename):
            flash("Format file harus PNG atau JPG.", "error")
            return redirect(url_for("sign"))
        if plan_expired(user):
            flash("Masa aktif paket Anda sudah berakhir. Silakan beli paket untuk melanjutkan.", "error")
            return redirect(url_for("paket"))
        if used >= plan["kuota"]:
            flash("Kuota paket Anda sudah habis. Silakan beli paket untuk melanjutkan.", "error")
            return redirect(url_for("paket"))
        try:
            img = cc.open_image(file.read())
        except (UnidentifiedImageError, OSError):
            flash("File tidak dapat dibaca sebagai citra. Pastikan file PNG/JPG tidak rusak.", "error")
            return redirect(url_for("sign"))

        existing = cc.read_signature(img)
        if existing is not None:
            flash("Citra ini sudah memiliki tanda tangan MedSign PQ (ID "
                  f"{existing['payload'].get('sid', '-')}). Gunakan citra asli yang belum ditandatangani.", "error")
            return redirect(url_for("sign"))

        sid = "MSPQ-" + secrets.token_hex(4).upper()
        signed_at = now_iso()
        payload = {
            "sid": sid,
            "signer_id": user["id"],
            "signer_name": user["org_name"],
            "signed_at": signed_at,
        }
        ec_priv = cc.decrypt_private(user["ec_private_enc"], FERNET)
        ml_priv = cc.decrypt_private(user["ml_private_enc"], FERNET)
        png_bytes, meta = cc.sign_image(img, payload, ec_priv, ml_priv)

        base = Path(file.filename).stem[:60] or "citra"
        out_name = f"{base}_signed.png"
        path = SIGNED_DIR / f"{sid}.png"
        path.write_bytes(png_bytes)
        db = get_db()
        db.execute(
            """INSERT INTO signatures (id, user_id, filename, description, image_hash, width, height, signed_at, file_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, user["id"], out_name, description, meta["payload"]["image_hash"],
             img.width, img.height, signed_at, str(path)))
        db.commit()
        flash("Citra berhasil ditandatangani dengan tanda tangan hibrida.", "success")
        return redirect(url_for("sign_result", sid=sid))
    return render_template("sign.html", plan=plan, used=used, remaining=max(plan["kuota"] - used, 0))


def _own_signature(sid):
    row = get_db().execute("SELECT * FROM signatures WHERE id = ?", (sid,)).fetchone()
    if row is None or row["user_id"] != current_user()["id"]:
        abort(404)
    return row


@app.route("/tandatangani/<sid>")
@login_required
def sign_result(sid):
    row = _own_signature(sid)
    check_url = url_for("check_record", sid=sid, _external=True)
    return render_template("sign_result.html", row=row, check_url=check_url, qr=qr_data_uri(check_url))


@app.route("/unduh/<sid>")
@login_required
def download(sid):
    row = _own_signature(sid)
    # File dicari di folder instance/signed yang sedang dipakai, bukan dari alamat lama di database,
    # supaya tetap bisa diunduh walaupun folder proyek dipindah atau di-hosting.
    path = SIGNED_DIR / f"{sid}.png"
    if not path.exists():
        flash("File citra bertanda tangan tidak ditemukan di server. Silakan tandatangani ulang citranya.", "error")
        return redirect(url_for("dashboard"))
    return send_file(path, mimetype="image/png", as_attachment=True, download_name=row["filename"])


# ------------------------------------------------------------------ verifikasi publik
def _log_check(sid, result):
    db = get_db()
    db.execute("INSERT INTO verifications (signature_id, result, checked_at) VALUES (?,?,?)",
               (sid, result, now_iso()))
    db.commit()


@app.route("/cek", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        return render_template("check.html")

    file = request.files.get("image")
    if file is None or file.filename == "":
        flash("Pilih file citra yang ingin dicek.", "error")
        return redirect(url_for("check"))
    try:
        img = cc.open_image(file.read())
    except (UnidentifiedImageError, OSError):
        flash("File tidak dapat dibaca sebagai citra.", "error")
        return redirect(url_for("check"))

    meta = cc.read_signature(img)
    if meta is None:
        _log_check(None, "tidak_bertanda_tangan")
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        return render_template("check_result.html", status="unsigned", filename=file.filename,
                               is_jpg=ext in ("jpg", "jpeg"))

    payload = meta["payload"]
    db = get_db()
    signer = db.execute("SELECT * FROM users WHERE id = ?", (payload.get("signer_id"),)).fetchone()
    record = db.execute("SELECT * FROM signatures WHERE id = ?", (payload.get("sid"),)).fetchone()
    if signer is None or record is None:
        _log_check(payload.get("sid"), "tidak_terdaftar")
        return render_template("check_result.html", status="unknown", payload=payload, filename=file.filename)

    result = cc.verify_image(img, meta, cc.load_public(signer["ec_public"]), cc.load_public(signer["ml_public"]))
    if result["valid"]:
        status = "valid"
    elif not result["hash_ok"] and result["ecdsa_ok"] and result["mldsa_ok"]:
        status = "modified"
    else:
        status = "invalid"
    _log_check(payload["sid"], status)
    return render_template("check_result.html", status=status, result=result, payload=payload,
                           signer=signer, record=record, filename=file.filename)


@app.route("/cek/<sid>")
def check_record(sid):
    db = get_db()
    record = db.execute(
        """SELECT s.*, u.org_name, u.org_type FROM signatures s JOIN users u ON u.id = s.user_id
           WHERE s.id = ?""", (sid,)).fetchone()
    return render_template("check_record.html", record=record, sid=sid)


# ------------------------------------------------------------------ admin (aktivasi paket)
@app.route("/admin")
@login_required
@admin_required
def admin():
    db = get_db()
    q = request.args.get("q", "").strip().lower()
    sql = "SELECT * FROM users"
    args = ()
    if q:
        sql += " WHERE lower(email) LIKE ? OR lower(org_name) LIKE ? OR lower(pic_name) LIKE ?"
        args = (f"%{q}%",) * 3
    users = db.execute(sql + " ORDER BY created_at DESC", args).fetchall()
    rows = []
    for u in users:
        plan = PLANS[u["plan"]]
        rows.append({"u": u, "plan": plan, "used": quota_used(u), "expired": plan_expired(u)})
    sales = db.execute(
        """SELECT a.*, u.org_name, u.pic_name, u.email, u.phone FROM activations a
           JOIN users u ON u.id = a.user_id ORDER BY a.activated_at DESC""").fetchall()
    paid = [s for s in sales if s["harga_rp"] > 0]
    summary = {"pengguna": len(users), "transaksi": len(paid), "total": sum(s["harga_rp"] for s in paid)}
    return render_template("admin.html", rows=rows, sales=sales, summary=summary, q=q,
                           metode_list=METODE_BAYAR)


@app.route("/admin/aktivasi/<int:uid>", methods=["POST"])
@login_required
@admin_required
def admin_activate(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    plan_key = request.form.get("plan", "")
    metode = request.form.get("metode", "")
    catatan = request.form.get("catatan", "").strip()[:200]
    if target is None or plan_key not in PLANS:
        flash("Pengguna atau paket tidak valid.", "error")
        return redirect(url_for("admin"))
    if metode not in METODE_BAYAR:
        flash("Pilih metode pembayaran.", "error")
        return redirect(url_for("admin"))
    plan = PLANS[plan_key]
    plan_until = (datetime.now() + timedelta(days=plan["durasi_hari"])).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE users SET plan = ?, plan_until = ? WHERE id = ?", (plan_key, plan_until, uid))
    harga = 0 if metode == "Gratis (mitra)" else plan["harga_rp"]
    db.execute(
        """INSERT INTO activations (user_id, plan, harga_rp, metode, catatan, admin_email, activated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (uid, plan_key, harga, metode, catatan, current_user()["email"], now_iso()))
    db.commit()
    flash(f"Paket {plan['nama']} untuk {target['email']} aktif sampai {format_tanggal(plan_until)}.", "success")
    return redirect(url_for("admin"))


@app.errorhandler(413)
def too_large(_e):
    flash("Ukuran file melebihi 20 MB.", "error")
    return redirect(request.referrer or url_for("index"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True)
