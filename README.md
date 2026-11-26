# MedSign PQ — MVP Versi 1

Layanan verifikasi integritas citra rontgen berbasis tanda tangan digital hibrida pascakuantum
(ECDSA P-256 + ML-DSA-65). Kelompok 17, D3 Teknologi Informasi, Institut Teknologi Del.

## Fitur MVP v1
1. Daftar dan masuk akun fasyankes (kunci ECDSA + ML-DSA dibuat otomatis dan disimpan terenkripsi).
2. Tandatangani citra rontgen PNG/JPG; tanda tangan disisipkan ke metadata PNG (chunk `medsignpq`).
3. Cek keaslian publik tanpa login: ASLI / TIDAK ASLI (citra diubah / tanda tangan tidak valid) / belum ditandatangani.
4. Dashboard: paket aktif (Uji Coba 3 bulan, 300 citra/bulan), kuota, dan riwayat penandatanganan.

## Cara menjalankan
Butuh Python 3.10+ (pustaka `cryptography` versi 46 ke atas sudah mendukung ML-DSA).

```bash
pip install -r requirements.txt
python app.py
```
Buka http://127.0.0.1:5000

## Uji otomatis
```bash
python tests/test_flow.py
```

## Struktur
```
app.py            halaman, akun, database (SQLite)
crypto_core.py    hash SHA3-256, tanda tangan & verifikasi hibrida, metadata PNG
templates/        halaman HTML (Jinja2)
static/           CSS dan logo
tests/            uji alur end-to-end
instance/         dibuat otomatis: database, file bertanda tangan, kunci server (JANGAN diunggah ke GitHub)
```

## Catatan keamanan
- `instance/kek.key` adalah kunci untuk membuka kunci privat semua fasyankes. Simpan cadangannya dan jangan dibagikan.
  Saat hosting, sebaiknya isi lewat variabel lingkungan `KEY_ENCRYPTION_KEY` dan `SECRET_KEY`.
- Kirim citra bertanda tangan sebagai **dokumen/file**, bukan foto WhatsApp, karena kompresi foto menghapus metadata.
