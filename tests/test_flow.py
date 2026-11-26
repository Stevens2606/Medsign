"""
Uji alur MedSign PQ dari awal sampai akhir (end-to-end) memakai Flask test client.
Jalankan dari folder proyek:  python -m pytest tests -v   atau   python tests/test_flow.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["MEDSIGN_INSTANCE"] = tempfile.mkdtemp(prefix="medsign_test_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, PngImagePlugin  # noqa: E402

import app as medsign  # noqa: E402
import crypto_core as cc  # noqa: E402


def sample_xray(size=(512, 512)) -> bytes:
    """Citra uji sintetis (grayscale) menyerupai foto thorax."""
    img = Image.new("L", size, 15)
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w * .12, h * .15, w * .47, h * .88), fill=95)
    d.ellipse((w * .53, h * .15, w * .88, h * .88), fill=95)
    d.rectangle((w * .47, h * .05, w * .53, h * .95), fill=170)
    for i in range(7):
        y = h * (.22 + i * .09)
        d.arc((w * .1, y - 30, w * .9, y + 50), 200, 340, fill=150, width=6)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def register(client, email="radiologi@kliniksehat.id"):
    return client.post("/daftar", data={
        "org_name": "Klinik Pratama Sehat Laguboti", "org_type": "Klinik Radiologi",
        "pic_name": "Maria Christin Gurning", "phone": "081234567890",
        "email": email, "password": "rahasia123", "confirm": "rahasia123",
    }, follow_redirects=True)


def upload(client, url, data: bytes, name="thorax_pa.png", **extra):
    return client.post(url, data={"image": (io.BytesIO(data), name), **extra},
                       content_type="multipart/form-data", follow_redirects=True)


def signed_file(client):
    r = upload(client, "/tandatangani", sample_xray(), description="Thorax PA, rujukan")
    assert "berhasil ditandatangani" in r.get_data(as_text=True)
    sid = r.request.path.rsplit("/", 1)[-1]
    return sid, client.get(f"/unduh/{sid}").data


def test_full_flow():
    c = medsign.app.test_client()

    # 1. daftar
    r = register(c)
    assert "Akun berhasil dibuat" in r.get_data(as_text=True)

    # 2. tanda tangan + unduh
    sid, data = signed_file(c)
    img = Image.open(io.BytesIO(data))
    meta = cc.read_signature(img)
    assert meta["payload"]["sid"] == sid and meta["alg"] == cc.ALGORITHM

    # 3. cek keaslian publik (keluar dulu agar tanpa login)
    c.get("/keluar")
    r = upload(c, "/cek", data, name="dari_klinik.png")
    assert "ASLI" in r.get_data(as_text=True) and "TIDAK ASLI" not in r.get_data(as_text=True)

    # 4. ubah 1 piksel -> harus terdeteksi
    img = Image.open(io.BytesIO(data))
    img.load()
    px = img.load()
    px[100, 100] = (px[100, 100] + 1) % 256
    info = PngImagePlugin.PngInfo()
    info.add_text(cc.META_KEY, img.text[cc.META_KEY])
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    r = upload(c, "/cek", buf.getvalue(), name="diubah.png")
    assert "citra telah diubah" in r.get_data(as_text=True)

    # 5. citra tanpa tanda tangan
    r = upload(c, "/cek", sample_xray(), name="polos.png")
    assert "Tanda tangan tidak ditemukan" in r.get_data(as_text=True)

    # 6. info rekaman lewat tautan
    assert "Terdaftar di MedSign PQ" in c.get(f"/cek/{sid}").get_data(as_text=True)


def test_validations():
    c = medsign.app.test_client()
    r = c.post("/daftar", data={"org_name": "A", "org_type": "", "pic_name": "", "email": "salah",
                                "password": "123", "confirm": "456"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    for msg in ("minimal 3 karakter", "Pilih jenis fasyankes", "Format email", "minimal 8", "tidak sama"):
        assert msg in body
    register(c, email="dobel@rs.id")
    c.get("/keluar")
    assert "Email sudah terdaftar" in register(c, email="dobel@rs.id").get_data(as_text=True)
    assert "Email atau kata sandi salah" in c.post("/masuk", data={"email": "dobel@rs.id", "password": "salahsalah"}).get_data(as_text=True)
    # halaman fasyankes wajib login
    assert c.get("/dashboard").status_code == 302


def test_tampered_signature_and_resign():
    c = medsign.app.test_client()
    register(c, email="rs2@rs.id")
    sid, data = signed_file(c)
    # citra yang sudah ditandatangani tidak boleh ditandatangani ulang
    r = upload(c, "/tandatangani", data)
    assert "sudah memiliki tanda tangan" in r.get_data(as_text=True)
    # rusak tanda tangan ML-DSA
    img = Image.open(io.BytesIO(data))
    img.load()
    meta = cc.read_signature(img)
    sig = bytearray(__import__("base64").b64decode(meta["sig_mldsa"]))
    sig[10] ^= 0xFF
    meta["sig_mldsa"] = __import__("base64").b64encode(bytes(sig)).decode()
    info = PngImagePlugin.PngInfo()
    info.add_text(cc.META_KEY, __import__("json").dumps(meta))
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    r = upload(c, "/cek", buf.getvalue())
    assert "tanda tangan tidak valid" in r.get_data(as_text=True)


def test_paket_dan_aktivasi_admin():
    c = medsign.app.test_client()
    # pembeli daftar -> otomatis Coba Gratis 5 citra
    register(c, email="pembeli@gmail.com")
    assert "Coba Gratis" in c.get("/dashboard").get_data(as_text=True)
    for _ in range(5):
        upload(c, "/tandatangani", sample_xray(size=(256 + _, 256)))
    r = upload(c, "/tandatangani", sample_xray(size=(300, 300)))
    assert "Kuota paket Anda sudah habis" in r.get_data(as_text=True)
    assert "Beli Paket Starter" in c.get("/dashboard").get_data(as_text=True)
    # pengguna biasa tidak bisa membuka admin
    assert c.get("/admin").status_code == 404
    c.get("/keluar")

    # admin mengaktifkan Starter
    a = medsign.app.test_client()
    register(a, email="admin@medsignpq.id")
    body = a.get("/admin").get_data(as_text=True)
    assert "pembeli@gmail.com" in body
    with medsign.app.app_context():
        uid = medsign.get_db().execute("SELECT id FROM users WHERE email='pembeli@gmail.com'").fetchone()[0]
    r = a.post(f"/admin/aktivasi/{uid}", data={"plan": "starter", "metode": "QRIS", "catatan": "bukti di form"},
               follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Paket Starter untuk pembeli@gmail.com aktif" in body and "Rp25.000" in body

    # pembeli bisa menandatangani lagi, kuota 0/30 baru
    c.post("/masuk", data={"email": "pembeli@gmail.com", "password": "rahasia123"})
    assert " / 30" in c.get("/dashboard").get_data(as_text=True)
    r = upload(c, "/tandatangani", sample_xray(size=(320, 320)))
    assert "berhasil ditandatangani" in r.get_data(as_text=True)


if __name__ == "__main__":
    for t in (test_full_flow, test_validations, test_tampered_signature_and_resign, test_paket_dan_aktivasi_admin):
        t()
        print("LULUS:", t.__name__)
