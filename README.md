# OnTime API

OnTime merekomendasikan waktu berangkat berdasarkan target tiba. Backend ini dikembangkan untuk Tugas Besar TST × PWAM K01 dan dirancang berjalan di set-top box (STB). API melayani aplikasi mobile OnTime dan kelompok mitra.

Pengguna bisa menyimpan tempat favorit dan jadwal rutin, meminta prediksi, lalu mencatat waktu mulai dan sampai. Durasi aktual tersebut menjadi masukan untuk prediksi berikutnya. Aplikasi mobile dapat membuka Google Maps dengan koordinat asal dan tujuan yang sama untuk menampilkan petunjuk arah.

## Arsitektur

Pada deployment publik, request HTTPS masuk melalui Cloudflare Tunnel, diteruskan oleh Nginx, lalu diproses oleh Flask dan Gunicorn. SQLite menyimpan data pada volume Docker `ontime_data`. Saat dijalankan secara lokal, Nginx bisa diakses di `127.0.0.1:8080`.

| Komponen | Fungsi |
| --- | --- |
| [Core](core/app.py) | Akun, favorit, rencana, pencatatan perjalanan, laporan jalan, dan API prediksi |
| [Prediksi](core/prediction.py) | Pengolahan riwayat perjalanan, estimasi OSRM, dan penyesuaian cuaca Open-Meteo |
| [Pengingat](core/reminders.py) | Pemeriksaan jadwal rutin dan pengiriman notifikasi melalui Expo |
| [Gateway](gateway/nginx.conf) | Penerusan request ke core dan pembatasan laju request |
| [Docker Compose](docker-compose.yml) | Menjalankan core, gateway, tunnel opsional, dan volume database |

## Mulai cepat

Siapkan Docker dengan plugin Compose. Python 3 diperlukan untuk membuat secret dan menjalankan tes di luar Docker. Jalankan perintah berikut dari direktori repositori. Jika `.env` sudah ada, lanjutkan langsung ke penyesuaian isinya.

**Linux atau STB**

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

| Variabel | Nilai |
| --- | --- |
| `JWT_SECRET` | Wajib. Salin secret acak dari perintah di atas. Minimal 32 byte. |
| `PARTNER_KEYS` | Opsional. Objek JSON yang memetakan nama kelompok ke API key, misalnya `'{"kelompok-mitra":"<key-acak-minimal-32-karakter>"}'`. Buat key tersendiri untuk setiap mitra. |
| `TUNNEL_TOKEN` | Isi dengan token Cloudflare Tunnel kelompok saat mengaktifkan akses publik. Untuk uji lokal, biarkan kosong. |

Simpan nilai asli di `.env` dan bagikan kredensial mitra melalui kanal privat. Repositori menggunakan [`.env.example`](.env.example) sebagai templat.

```bash
docker compose up -d --build
docker compose ps
```

Cek [health endpoint](http://127.0.0.1:8080/api/health) dari mesin yang menjalankan Docker. Respons yang diharapkan adalah HTTP `200` dengan `{"service":"ontime","status":"ok"}`. Jika core gagal merespons, periksa `docker compose logs --tail 80 core gateway`. Database dibuat otomatis saat startup. Volume mempertahankan data ketika container di-restart atau dibangun ulang. Akun demo perlu didaftarkan di setiap lingkungan yang memakai volume berbeda.

## Kontrak API

Base URL lokal adalah `http://127.0.0.1:8080/api`. Gunakan [OpenAPI 3.1](core/openapi.json) untuk integrasi atau impor ke Postman. Saat layanan berjalan, kontrak juga tersedia melalui `GET /api/openapi.json`. Contoh alur request ada di [`api.http`](api.http).

- Pakai `Content-Type: application/json` untuk request dengan body JSON.
- Kirim koordinat dalam urutan `[latitude, longitude]` dan waktu ISO 8601 dengan offset zona waktu. Jadwal rutin menggunakan WIB dan nomor hari 1 untuk Senin hingga 7 untuk Minggu.
- Akun mobile mengirim `Authorization: Bearer <token>`. Token login berlaku 24 jam. Mitra mengirim `X-API-Key` untuk prediksi dan pembacaan kondisi jalan.

Semua path di tabel relatif terhadap base URL tersebut.

| Method dan path | Akses | Fungsi |
| --- | --- | --- |
| `GET /health`, `GET /openapi.json` | Publik | Periksa kesehatan core dan SQLite atau baca kontrak API |
| `POST /auth/register`, `POST /auth/login` | Publik | Daftar dan login dengan email dan password |
| `GET /favorites`, `POST /favorites` | Bearer | Lihat atau tambah tempat favorit akun |
| `PUT /favorites/{id}`, `DELETE /favorites/{id}` | Bearer pemilik | Ganti nama dan koordinat atau hapus favorit |
| `GET /plans`, `POST /plans` | Bearer | Lihat atau tambah rencana sekali jalan atau rutin |
| `PUT /plans/{id}`, `DELETE /plans/{id}` | Bearer pemilik | Ganti seluruh isi atau hapus rencana |
| `POST /trips/start` | Bearer | Mulai satu perjalanan aktif per akun. Konflik `409` memuat ID perjalanan yang sudah aktif. |
| `POST /trips/{id}/finish` | Bearer pemilik | Catat waktu sampai dan kembalikan `durationSeconds`. Pengulangan memberikan durasi yang sama. |
| `POST /conditions` | Bearer | Laporkan `traffic` atau `flood` selama dua jam |
| `GET /conditions?lat=...&lon=...` | Bearer atau API key | Baca laporan aktif di sekitar koordinat |
| `POST /predict` | Bearer atau API key | Hitung rekomendasi dan estimasi per slot keberangkatan |
| `PUT /reminders/device`, `DELETE /reminders/device` | Bearer | Daftarkan token Expo atau hentikan pengingat |

### Alur perjalanan pada aplikasi mobile

1. Buat favorit, misalnya `{"name":"Kos","location":[-6.88,107.62]}`, lalu pilih `location` sebagai `origin` atau `destination`.
2. Pakai koordinat yang dipilih saat menyimpan rencana atau meminta prediksi. Rencana menyimpan salinan koordinat pada saat penyimpanan, sehingga perubahan favorit berlaku ketika pengguna memilihnya lagi.
3. Saat pengguna menekan **Mulai**, catat perjalanan di API dan simpan ID yang diterima sebelum membuka Google Maps dari aplikasi mobile. Gunakan pasangan koordinat yang sama untuk membuka petunjuk arah.
4. Saat pengguna kembali ke OnTime dan menekan **Sampai**, selesaikan perjalanan dengan ID yang tadi disimpan.

## Cara kerja prediksi

Untuk target sampai tujuh hari ke depan, core menilai slot 15 menit yang belum lewat dalam rentang sekitar tiga jam sebelum target. Riwayat dikelompokkan berdasarkan zona asal dan tujuan, jenis kendaraan, hari dalam minggu, serta slot WIB. Zona dibentuk dengan pembulatan koordinat sampai dua desimal.

| Urutan | Sumber `source` | Durasi dasar |
| --- | --- | --- |
| 1 | `personal` | Median dari minimal tiga perjalanan yang cocok milik pengguna |
| 2 | `collective` | Median dari minimal tiga perjalanan yang cocok dari seluruh akun |
| 3 | `osrm` | Estimasi rute OSRM dengan profil `driving`, termasuk pendekatan awal untuk motor |

Untuk sumber OSRM, core menerapkan faktor jam sibuk 1,3 pada hari kerja pukul 07.00–08.59 dan 16.00–18.59 WIB. Prakiraan presipitasi Open-Meteo diperiksa di titik asal pada jam keberangkatan. Jika ada presipitasi, penyesuaiannya tiga menit untuk mobil dan lima menit untuk motor. Laporan jalan aktif di sekitar asal atau tujuan menambah lima menit selama laporan masih berlaku. Pengecekan laporan memakai jarak koordinat hingga 0,01 derajat pada masing-masing sumbu.

- `latestSafe` berisi slot keberangkatan terakhir yang memenuhi target dengan buffer lima menit.
- `fastest` berisi slot dengan durasi terpendek di antara slot yang memenuhi target. Keduanya bernilai `null` jika tidak ada slot yang memenuhi target.
- `slots` berisi estimasi per slot, sumber data, dan indikator cuaca. Jika cuaca gagal diambil, core tetap menghitung estimasi dengan `weatherAvailable: false`. Jika OSRM gagal, slot yang memiliki riwayat cukup tetap dihitung. API mengembalikan `503` jika semua slot kandidat membutuhkan OSRM yang sedang tidak tersedia.

Penjadwal memeriksa rencana rutin setiap menit. Setelah pengguna mendaftarkan token Expo, core mengirim pengingat jika waktu keberangkatan yang direkomendasikan berada dalam 20 menit berikutnya. Core menyimpan jadwal yang sudah diterima Expo untuk menghindari pengiriman ulang pada waktu keberangkatan yang sama.

## Pengujian

### Tes otomatis

Dari direktori repositori, gunakan Python 3.12 dan instal dependensi. Gunicorn dipakai saat berjalan dalam container Linux. Tes API sendiri berjalan melalui Flask test client.

```bash
python -m pip install -r core/requirements.txt
python core/test_api.py
```

Tes memakai SQLite sementara dan respons layanan eksternal yang disimulasikan. Cakupannya termasuk autentikasi, kepemilikan favorit, pencatatan perjalanan, serta fallback prediksi.

### Uji lewat gateway

Buka [`api.http`](api.http) di VS Code dengan REST Client, atau impor OpenAPI ke Postman. Jalankan **Health → Register → Login → Predict**. Sesuaikan `targetArrival` ke waktu dalam tujuh hari ke depan sebelum meminta prediksi. Periksa `slots[].source`, `latestSafe`, dan `weatherAvailable`. Lanjutkan dengan **Start → Finish** untuk memeriksa pencatatan durasi.

### Uji prediksi dengan riwayat sintetis

Jalankan dari PowerShell saat Compose lokal berjalan. Masing-masing perintah seeding membuat 65 perjalanan selesai dan mencetak body request untuk `POST /api/predict`. Pakai body dan target waktu yang dicetak supaya slotnya cocok.

```powershell
Get-Content -Raw seed_prediction_demo.py | docker compose exec -T core python - collective
Get-Content -Raw seed_prediction_demo.py | docker compose exec -T core python - personal --email demo@example.org
```

Uji setelah masing-masing perintah. Mode `collective` memakai tiga akun simulasi. Gunakan akun uji yang belum punya tiga sampel pribadi yang cocok. Mode `personal` mengganti fixture dengan riwayat milik email yang kamu pilih. Pastikan akun itu sudah terdaftar dan token login berasal dari akun tersebut. Setelah demo selesai, hapus data sintetis dari database uji.

```powershell
Get-Content -Raw seed_prediction_demo.py | docker compose exec -T core python - clean
```

## Deployment di STB

1. Salin proyek ke STB, siapkan `.env` pada STB, lalu jalankan core dan gateway mengikuti langkah setup. Pastikan health endpoint di loopback berhasil.
2. Di konfigurasi Cloudflare Tunnel kelompok, arahkan public hostname ke `http://gateway:80`. Isi `TUNNEL_TOKEN` di STB dan jalankan:

   ```bash
   docker compose --profile tunnel up -d --build
   docker compose --profile tunnel ps
   docker compose logs --tail 80 core gateway tunnel
   ```

3. Uji `/api/health` dan `POST /api/predict` dari jaringan lain melalui hostname HTTPS kelompok. `https://ontime.stei.fun/api` adalah URL target deployment. Buat akun uji di STB melalui endpoint register, lalu cek akses mitra dengan API key yang dikonfigurasi sebelum mendaftarkan URL aktif di API Registry.