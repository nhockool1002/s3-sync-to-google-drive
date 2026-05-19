## Ứng dụng sync AWS S3 → Google Drive

Ứng dụng CLI (Python) hỗ trợ:

- Đồng bộ/bơm dữ liệu từ **AWS S3** sang **Google Drive**.
- **Tải file (video)** từ Google Drive xuống máy qua **Google Drive API** (OAuth, scope `drive.readonly`), tách token `token_download.json` khỏi luồng sync.
- **Xem video trong folder Drive** (kèm thư mục con) qua trình duyệt local: `app.view_video_drive` — stream byte-range qua máy bạn.
- Chạy được **trên máy local** hoặc trong **Docker**.
- Chạy theo **batch**, có **log chi tiết** (OK / FAIL / SKIP) kèm timestamp.
- Tự **skip file đã tồn tại** trên Drive, **bỏ qua file ẩn** như `.DS_Store`.

---

## 1. Chuẩn bị

### 1.1. Cài Python & virtualenv (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 1.2. Cấu hình AWS

Tạo file `.env` ở root project:

```bash
AWS_ACCESS_KEY_ID=your_aws_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=ap-southeast-1

LOG_DIR=./logs
```

### 1.3. Cấu hình Google OAuth (Drive)

1. Vào **Google Cloud Console** của project chứa Drive API.
2. Bật **Google Drive API**.
3. Vào **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**.
4. Tải file **`credentials.json`** về, đặt tại **root project**:

```text
sync-s3-to-google-drive/
  credentials.json
  app/
  ...
```

5. Đảm bảo trong **OAuth consent screen**:
   - App ở trạng thái **Testing**.
   - Email Google của bạn nằm trong **Test users**.

Lần chạy đầu tiên, app sẽ mở trình duyệt để bạn login và tạo `token.json` (sau đó dùng lại).

---

## 2. Lệnh chính

### 2.1. Chạy sync S3 → Google Drive (local)

```bash
python3 -m app.main
```

Flow nhập liệu:

1. `Nhập tên S3 bucket`  
   - Ví dụ: `mymemories-image`
2. `Nhập folder name trên S3 (có thể để trống)`  
   - Ví dụ: `bentrebatri180922`  
   - Để trống nếu muốn sync **cả bucket**.
3. `Nhập Google Drive folder ID`  
   - Lấy từ URL folder trên Drive:  
     `https://drive.google.com/drive/folders/<FOLDER_ID>`
4. `Nhập batch size (default 50)`  
   - Enter bỏ trống để dùng `50`, hoặc nhập số khác (vd: `20`, `100`).

Kết quả:

- File log được tạo trong thư mục `logs/` với tên dạng:  
  `YYYYMMDD_HHMMSS_<prefix>.log`
- Trong log có:
  - Tổng số object trên S3 (đã **bỏ qua file ẩn**).
  - Dòng chi tiết cho từng file: `[OK]`, `[SKIP]`, `[FAIL]`.
  - Tổng kết cuối cùng: `Total`, `Processed`, `Success`, `Failed`, `Skipped`.

### 2.2. Đếm số file trong 1 folder Drive

Script `app/count_drive.py`:

```bash
python3 -m app.count_drive
```

Sau đó nhập:

- `Google Drive folder ID` cần đếm.  
- Hoặc **Enter bỏ trống** để đếm số file ở **My Drive root**.

Kết quả in ra terminal:

- `Folder ID <ID> có X file (không tính file trong thùng rác).`
- Hoặc `My Drive root có X file (không tính file trong thùng rác).`

### 2.3. Tải file (video) từ Google Drive (`app.drive_download`)

Dùng khi bạn có quyền xem file trên Drive (chủ sở hữu hoặc được chia sẻ) và muốn tải bản nhị phân qua API. Phạm vi **`drive.readonly`** cho phép đọc các file mà tài khoản đăng nhập có quyền — **khác** với token sync (`token.json`, scope `drive.file`) chủ yếu cho file do app tạo.

**Chuẩn bị:** cùng `credentials.json` như mục 1.3; trong Google Cloud Console, **Google Drive API** phải đã bật. Lần đầu chạy tool tải, trình duyệt sẽ xin quyền và tạo **`token_download.json`** ở root project (có thể đổi bằng biến môi trường `GOOGLE_OAUTH_TOKEN_DOWNLOAD`).

```bash
python3 -m app.drive_download --help
```

**Tham số:**

| Tham số | Ý nghĩa |
|--------|---------|
| `--file-id` | ID file trên Drive |
| `--url` | Link chia sẻ (dạng có `/file/d/<ID>` hoặc `?id=`) |
| `-o` / `--output` | Đường dẫn file lưu trên máy (vd: `./video.mp4`) |
| `--acknowledge-abuse` | Dùng nếu API/Drive yêu cầu xác nhận file có cảnh báo abuse |

Nếu **không** truyền `--file-id` và `--url`, chương trình sẽ hỏi: `Nhập FILE_ID hoặc URL Google Drive:`. Nếu **không** truyền `-o`, sẽ hỏi: `Đường dẫn file lưu (vd: ./video.mp4):`.

Ví dụ:

```bash
python3 -m app.drive_download --file-id "<FILE_ID>" -o ./video.mp4
python3 -m app.drive_download --url "https://drive.google.com/file/d/<FILE_ID>/view" -o ./video.mp4
python3 -m app.drive_download
```

Chạy không tham số (hoặc thiếu nguồn/đích) sẽ vào các bước nhập tương tác như trên.

**Lưu ý:** file kiểu Google Docs/Sheets (MIME `application/vnd.google-apps.*`) không tải bằng `get_media` như video; tool sẽ báo lỗi và gợi ý export nếu cần.

### 2.4. Xem video trong folder Drive (`app.view_video_drive`)

Dùng cùng OAuth **`drive.readonly`** và **`token_download.json`** như mục 2.3. Tool khởi động **web server chỉ trên `127.0.0.1`**, quét đệ quy folder (và folder con), liệt kê mọi file có `mimeType` dạng `video/*`, mở trình duyệt để bạn chọn **Xem**; luồng phát đi qua endpoint local `/stream/<FILE_ID>` (hỗ trợ `Range` để seek) tới Google Drive `alt=media`.

**Dependency:** `flask`, `requests` — đã khai báo trong `requirements.txt`, cài bằng `pip install -r requirements.txt`.

```bash
python3 -m app.view_video_drive --help
```

**Tham số:**

| Tham số | Ý nghĩa |
|--------|---------|
| `--folder-id` | ID folder Drive hoặc URL có `/folders/<ID>`; bỏ trống để nhập khi chạy |
| `--port` | Cổng HTTP (mặc định `0` = hệ thống chọn) |
| `--host` | Mặc định `127.0.0.1` (không nên mở ra mạng LAN) |
| `--no-browser` | Không tự mở trình duyệt |

Ví dụ:

```bash
python3 -m app.view_video_drive --folder-id "<FOLDER_ID>"
python3 -m app.view_video_drive --folder-id "https://drive.google.com/drive/folders/<FOLDER_ID>"
```

**Lưu ý:** shortcut Drive bị bỏ qua; file bị Drive chặn đọc (virus scan, abuse, quyền) vẫn có thể trả 403 khi phát. Cây thư mục rất lớn có thể mất lâu khi quét lần đầu.

---

## 3. Chạy bằng Docker

### 3.1. Build image

```bash
docker build -t s3-to-gdrive .
```

### 3.2. Chuẩn bị file cấu hình

- `.env` (giống phần 1.2).
- `credentials.json` ở root project (giống phần 1.3).
- Sau lần chạy đầu (local hoặc trong container), sẽ có thêm `token.json`.
- Nếu dùng **tải file từ Drive** (mục 2.3), mount thêm `token_download.json` khi đã tạo trên host (tương tự `token.json`).

### 3.3. Chạy container

```bash
docker run --rm -it \
  --env-file .env \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/credentials.json:/app/credentials.json:ro" \
  -v "$(pwd)/token.json:/app/token.json" \
  s3-to-gdrive
```

Giải thích mount:

- `logs` → lấy log ra ngoài host.
- `credentials.json` → thông tin OAuth client, read‑only.
- `token.json` → token sau khi bạn login lần đầu (nếu chưa có, có thể tạo bằng cách chạy local trước).

---

## 4. Ghi chú & giới hạn

- App **không giữ cấu trúc thư mục S3** trên Drive, mà upload mọi file vào **cùng 1 folder Drive** (tên file giữ nguyên).
- App **bỏ qua**:
  - File ẩn / hệ thống (bắt đầu bằng `.` hoặc `.DS_Store`).
  - File đã tồn tại trên Drive (trùng `name` bên trong folder đích).
- Việc upload nhanh hay chậm phụ thuộc:
  - Băng thông mạng của bạn.
  - Dung lượng file (video nặng sẽ mất nhiều giây/phút).
  - Hạn mức API của Google Drive.

