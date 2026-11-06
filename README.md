# ContentHub (local)

Pipeline tự động đăng **YouTube Shorts + Facebook Reels** cho kênh NBA, chạy hoàn toàn trên PC
cá nhân bằng `docker compose` — thay cho bản AWS (Lambda + Fargate + RDS + S3 + Secrets Manager).

Tech stack giữ nguyên: **n8n, Python, docker compose, PostgreSQL, pgAdmin, pgvector**.
Hiện tại chỉ chuyển luồng `nba_reel_pipeline`.

## Bản cloud → bản local

| Bản AWS | Bản local |
|---|---|
| RDS PostgreSQL | service `postgres` (image `pgvector/pgvector:pg16`), volume `pg_data` |
| Secrets Manager `content-platform-dev/app-secrets` | file `.env` (xem `.env.example`) |
| S3 media bucket (`news/nba/...`) + lifecycle 7 ngày | thư mục `./data/media/nba/...` + job dọn rác trong `nba-content` |
| Lambda `nba-content` (Function URL) | service `nba-content` (FastAPI, cùng hợp đồng `mode` + `X-Api-Key`, thêm `mode: fetch_clips`) |
| ECS Fargate task `nba-render` (RunTask) | service `nba-render` (hàng đợi job trong tiến trình + ffmpeg) |
| Lambda `publish-media` | service `publish-media` (FastAPI) |
| `CRAWL_API_KEY` | `APP_API_KEY` |
| schema `content_real_estate` (có bảng `channels`) | schema **`content`** (thông tin dùng chung: `channels`, `bind_channels()`, `update_updated_at()`) |
| schema `content_nba` | schema `content_nba` (giữ nguyên tên và toàn bộ hàm) |
| Flyway qua ECS task | service `flyway` (chạy `migrate` rồi thoát) |
| IAM / VPC / NAT | không còn: mọi service nói chuyện qua network của compose |

Những điểm **không đổi**: n8n sở hữu toàn bộ đọc/ghi DB, service là bước không trạng thái;
quy tắc nghiệp vụ (chọn clip, chống làm trùng, retry) nằm trong hàm SQL; token không bao giờ
đi qua n8n (chỉ có `credential_key` = tên biến môi trường).

## Cấu trúc

```text
contenthub/
├── docker-compose.yml
├── .env.example                      # mọi secret, copy thành .env
├── db/
│   ├── init/01_create_n8n_database.sql   # tạo database n8n (chạy 1 lần)
│   ├── migrations/                       # Flyway: V1 schema content, V2 schema content_nba
│   └── seed/channels.sql.example         # khai báo kênh nba-yt / nba-fb
├── pgadmin/servers.json
├── services/
│   ├── nba-content/     # select / script / voice / music / render_start|status|wait / cleanup
│   ├── nba-render/      # POST /jobs, GET /jobs/{id}  (ffmpeg 1080x1920)
│   └── publish-media/   # POST / {mode: publish, format: reel}
├── workflows/n8n/nba_reel_pipeline.json
└── data/media/          # media sinh ra khi chạy (không commit)
```

## Luồng chạy

Workflow **chỉ chạy bằng Manual Trigger** (không có node cron): mỗi lượt chạy làm tối đa 10 video
và **đặt lịch** đăng vào giờ vàng của khán giả Mỹ.

```text
Manual Trigger → Set Parameters → Bind Channels (Postgres) → Resolve Config → Fetch Clips (nba-content → Storyteller)
  → Load Used Clip IDs → Select Clips (nba-content) → Save Selection → Plan Slots
  → [mỗi clip] Generate Script (OpenAI) → Save Script → Voice (ElevenLabs) → Save Voice
      → Music? → Music (ElevenLabs Music) → Build Render Spec → Start Render (nba-render)
      → Wait For Render → Save Video → Publish? → Plan Publishing → Publish (publish-media)
      → Collect Results → Finish Reel        (lỗi một clip → Mark Failed, vòng lặp chạy tiếp)
  → Summary → Run Had Failures nếu có clip lỗi
```

- **Chọn clip** (`services/nba-content/app/selector.py`): chỉ clip có `categories` chứa `highlights`
  hoặc `must-see-moments`, có `.mp4`, dài `min_duration`–`max_duration` (6–24 s), trong
  `lookback_hours`, chưa làm trước đó (`content_nba.used_clip_ids()`). Loại hẳn clip bên thứ ba
  (`ugc-clips`, `fangenerated-content`, `influencer`, `*provided`, mô tả có `(via @…)`).
  Điểm = 50% heuristic + 50% điểm biên tập của LLM; tối đa `max_per_player` clip / cầu thủ.
- **Video** (`services/nba-render`): intro 2.6–4 s (thumbnail + chữ hook + giọng hook) → clip gốc
  (giữ tiếng sân, tự giảm khi có lời bình) → outro CTA. 1080×1920, 30 fps, loudnorm −14 LUFS.
- **Hàng đợi render**: mỗi lần chỉ render `RENDER_CONCURRENCY` (mặc định 1) video. Spec đi qua
  file `data/media/nba/<reel_id>/render.json`, kết quả ở `render_result.json` + `reel.mp4`, nên
  kết quả vẫn còn sau khi service restart (trạng thái *đang chờ* thì không — reel đó báo failed
  và lượt sau làm lại).
- **Lịch đăng**: `slots` (HH:mm theo `slot_timezone` = `America/New_York`, slot tốt nhất đứng trước).
  `publish-media` nhận `publish_at` (11 phút – 30 ngày): YouTube upload `private` + `publishAt`,
  Facebook `published=false` + `scheduled_publish_time`.
- **Giới hạn YouTube**: một upload tốn 1.600/10.000 đơn vị quota ⇒ ~6 video/ngày; workflow chỉ đăng
  `youtube_max_clips` (6) clip tốt nhất lên YouTube, còn lại ghi `skipped`.

## Thiết lập lần đầu

```bash
cd ~/contenthub
cp .env.example .env
openssl rand -hex 32     # -> APP_API_KEY
openssl rand -hex 32     # -> N8N_ENCRYPTION_KEY
id -u; id -g             # -> HOST_UID / HOST_GID (để container ghi được vào ./data/media)
# điền tiếp POSTGRES_PASSWORD, PGADMIN_*, OPENAI_API_KEY, ELEVENLABS_API_KEY,
# ELEVENLABS_VOICE_ID, STORYTELLER_API_KEY, YOUTUBE_*, FACEBOOK_TOKEN_SOURCE_ACCESS_TOKEN

docker compose up -d --build        # flyway chạy migration rồi thoát (exit 0 là bình thường)
docker compose logs flyway          # kiểm tra "Successfully applied 2 migrations"
```

Khai báo kênh đăng:

```bash
cp db/seed/channels.sql.example db/seed/channels.sql   # điền <UC...> và <page_id>
docker compose exec -T postgres psql -U contenthub -d contenthub < db/seed/channels.sql
```

Trong n8n (http://localhost:5678) — lần đầu tạo tài khoản owner:

1. **Credentials → Postgres**, tên `ContentHub Postgres`:
   host `postgres`, port `5432`, database `contenthub`, user/password = `POSTGRES_USER` /
   `POSTGRES_PASSWORD` trong `.env`, SSL `disable`.
2. **Credentials → Header Auth**, tên `ContentHub Service API Key`:
   name `X-Api-Key`, value = `APP_API_KEY` trong `.env`.
3. **Workflows → Import from File** → `workflows/n8n/nba_reel_pipeline.json`.
   Hoặc nạp bằng CLI (file đã được mount vào container):

   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/nba_reel_pipeline.json
   docker compose restart n8n     # để n8n nạp lại workflow vừa import
   ```

4. Mở từng node Postgres / HTTP có credential và chọn lại đúng credential vừa tạo
   (file JSON để `id = REPLACE_ME`).
5. Chạy thử với `publish = false` trong *Set Parameters*: video render xong, không upload, clip
   được giải phóng (node *Release (dry run)*). Xem file ở `data/media/nba/<reel_id>/reel.mp4`.
   Ổn rồi thì đặt lại `publish = true` và bấm **Execute workflow** mỗi khi muốn chạy — workflow
   không có trigger tự động nên không cần bật *Active*. (Muốn chạy theo giờ về sau thì thêm một
   node *Schedule Trigger* nối vào *Set Parameters*.)

Workflow **không chứa secret nào**: khoá Storyteller nằm ở `STORYTELLER_API_KEY` và được
service `nba-content` dùng khi n8n gọi `mode: fetch_clips`; `voice_id` để trống ở *Set Parameters*
thì service lấy `ELEVENLABS_VOICE_ID`. Nhờ vậy container n8n **không cần ra internet** và
compose xoá luôn biến proxy mà Docker tiêm vào nó. Các tham số còn lại (`max_clips`, `slots`,
`bgm_volume`, …) sửa trực tiếp ở *Set Parameters*, hoặc ghi đè riêng cho một kênh bằng
`content.channels.content_profile`.

## Vận hành

```bash
docker compose ps
docker compose logs -f nba-content nba-render publish-media
docker compose restart nba-render
docker compose down                      # dừng (giữ dữ liệu)
docker compose up -d                     # chạy lại
docker compose exec postgres psql -U contenthub -d contenthub

# Theo dõi pipeline
#   SELECT * FROM content_nba.reel_overview;
#   SELECT * FROM content.channel_bindings;

# Hàng đợi render đang có gì
curl -s -H "X-Api-Key: $APP_API_KEY" localhost:8082/jobs | python3 -m json.tool

# Gọi thẳng một bước (debug, không qua n8n)
curl -s -X POST localhost:8081/ -H "X-Api-Key: $APP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"mode":"render_status","reel_id":"<uuid>"}'

# Dọn media cũ ngay (bình thường tự chạy mỗi CLEANUP_INTERVAL_HOURS giờ)
curl -s -X POST localhost:8081/ -H "X-Api-Key: $APP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"mode":"cleanup"}'
```

pgAdmin: http://localhost:5050 (server *ContentHub (local)* đã khai sẵn, mật khẩu =
`POSTGRES_PASSWORD`). Nếu `POSTGRES_USER`/`POSTGRES_DB` khác mặc định thì sửa
`pgadmin/servers.json`.

### Render nhanh/chậm

Render diễn ra trên CPU của PC (không còn Fargate). Một reel ~30 s thường mất 1–4 phút với
`FFMPEG_PRESET=medium`. Muốn nhanh hơn: đặt `FFMPEG_PRESET=veryfast` (file to hơn một chút)
trong `.env` rồi `docker compose up -d nba-render`. Node *Wait For Render* chờ tối đa 1800 s cho
mỗi reel; đổi `max_seconds` trong node nếu máy chậm (trần của service là 7200 s).

## Những chỗ dễ sai

- **Đừng sửa file `V*.sql` đã chạy.** Thêm `V3__...sql` mới và `CREATE OR REPLACE` hàm cần đổi.
  Quy tắc nghiệp vụ (chọn clip, chống trùng, retry) phải nằm trong hàm SQL, không nằm ở n8n hay Python.
- **Mỗi thư mục service là độc lập** (`storage.py` được nhân bản theo service, giống bản cloud).
  Không import chéo giữa các service.
- `N8N_ENCRYPTION_KEY` đổi là **mất toàn bộ credential** đã lưu trong n8n.
- `POSTGRES_PASSWORD` chỉ có tác dụng ở lần `up` đầu tiên (lúc khởi tạo volume `pg_data`). Đổi
  sau đó phải `ALTER USER ... PASSWORD` trong Postgres, không chỉ sửa `.env`.
- Hợp đồng của service: header `x-api-key` khớp `APP_API_KEY`, body có trường `mode`, `ValueError`
  → 400. `publish-media` trả lỗi nền tảng bằng HTTP 200 `{status: "failed"}` để n8n ghi được kết
  quả từng kênh.
- Media chỉ được xoá theo `MEDIA_RETENTION_DAYS`; DB vẫn giữ dòng reel (chỉ còn key). Đặt
  `MEDIA_RETENTION_DAYS=0` nếu muốn giữ hết (coi dung lượng đĩa).
- **Mạng công ty / proxy:** Docker tự tiêm `HTTP_PROXY`/`HTTPS_PROXY` vào container nhưng **không**
  tiêm `no_proxy`, và container **không phân giải được tên miền ngoài** (DNS nội bộ không tới được) —
  chính proxy mới resolve. Vì vậy: gọi giữa các container phải có `NO_PROXY` (anchor `x-no-proxy`)
  hoặc `trust_env=False`; gọi ra internet phải để httpx dùng proxy (`trust_env` mặc định).
  Triệu chứng khi làm sai: `gaierror: [Errno -2] Name or service not known` (không resolve được, do
  bỏ proxy) hoặc `502 Could not resolve or connect to proxy or host` (đi proxy cho địa chỉ nội bộ).
  Kiểm tra nhanh:
  ```bash
  docker compose exec nba-render python -c "import httpx; print(httpx.get('https://www.google.com/generate_204', timeout=15).status_code)"
  ```
- Rủi ro bản quyền: clip thuộc NBA; kênh fan vẫn có thể bị claim/strike dù đã thêm lời bình riêng.
- Chưa chuyển sang local: `crawl_news`, `news_post_pipeline`, `news_reel_pipeline`,
  `douyin_*`, `longform_telegram_bot`, và format `post` / `video` của `publish-media`.
