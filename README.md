# ydtl-api

A small, self-hosted HTTP API that downloads video or audio from a URL with [yt-dlp](https://github.com/yt-dlp/yt-dlp), stores the result in S3-compatible object storage (Cloudflare R2 by default), and returns a short-lived signed download link.

Files are never kept: each one is deleted shortly after it is ready, either when the client confirms the download or when a timer runs out.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start (local)](#quick-start-local)
- [Configuration](#configuration)
- [Running the services](#running-the-services)
- [API reference](#api-reference)
  - [Conventions](#conventions)
  - [POST /jobs](#post-jobs)
  - [GET /jobs/{job_id}](#get-jobsjob_id)
  - [POST /jobs/{job_id}/complete](#post-jobsjob_idcomplete)
  - [GET /info](#get-info)
  - [GET /health](#get-health)
  - [Job states](#job-states)
  - [Error format and codes](#error-format-and-codes)
  - [Rate limiting](#rate-limiting)
- [Usage examples](#usage-examples)
- [File retention and cleanup](#file-retention-and-cleanup)
- [Security notes](#security-notes)
- [Deployment](#deployment)
- [Development](#development)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Legal notice](#legal-notice)

---

## Features

- **Asynchronous jobs.** Submit a URL, get a job ID, poll for progress.
- **Video or audio.** MP4 video at a chosen quality, or MP3 / M4A audio.
- **Correct quality for vertical video.** `720p` means the shorter side is about 720 px, so a 1080x1920 Short is ranked the way YouTube names it.
- **Metadata endpoint.** Title, thumbnail, duration, and per-quality size estimates without downloading anything.
- **Signed download links.** The bucket stays private. Clients download straight from storage through a temporary URL.
- **Three-layer cleanup.** Client acknowledgement, a delayed delete, and a sweeper that works from the bucket listing.
- **Input safety.** Only public http(s) URLs, SSRF checks, size cap, no playlists, no live streams.
- **Rate limiting** per client IP, with `Retry-After` on `429`.
- **Storage-agnostic.** Works with Cloudflare R2, Backblaze B2, MinIO, or any S3-compatible store.

## How it works

```
 Client                 API (FastAPI)            Redis              Worker (RQ)              R2 / S3
   |  POST /jobs            |                      |                    |                      |
   |----------------------->| validate + rate limit|                    |                      |
   |                        |--- job record ------>|                    |                      |
   |                        |--- enqueue --------->|------------------->|                      |
   |<-- 202 {job_id} -------|                      |                    | yt-dlp download      |
   |                        |                      |<-- progress -------|                      |
   |  GET /jobs/{id} (poll) |                      |                    |--- upload ---------->|
   |----------------------->|<-- job record -------|                    | schedule delete      |
   |<-- state: ready + URL -|                      |                    |                      |
   |  download via signed URL ------------------------------------------------------------->  |
   |  POST /jobs/{id}/complete --> API deletes the object immediately                          |
```

Three processes run side by side:

| Process | Role |
|---|---|
| **api** | FastAPI app. Validates input, creates jobs, reports status. |
| **worker** | RQ worker (with scheduler). Downloads, uploads, schedules the delayed delete. |
| **sweeper** | Loop that deletes any object older than the file TTL, even after a crash or Redis restart. |

Redis holds the job queue, job state, and rate-limit counters. There is no other database.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- Redis 7 or newer
- ffmpeg (needed to merge video and audio and to convert to MP3 / M4A)
- An S3-compatible bucket (a **private** bucket) with an access key

## Quick start (local)

```bash
# 1. Install dependencies
uv sync

# 2. Create your settings file
cp .env.example .env      # or create .env by hand, see Configuration

# 3. Make sure Redis is running on localhost:6379 and ffmpeg is installed
redis-cli ping            # PONG
ffmpeg -version

# 4. Start the three processes, each in its own terminal
uv run poe dev            # API on http://localhost:8000
uv run poe worker
uv run poe sweeper

# 5. Check it
curl -s localhost:8000/health
```

A healthy response is `{"status":"ok","checks":{"redis":"ok","storage":"ok"}}`. Interactive API docs are served at `http://localhost:8000/docs`.

Run all commands from the project root: `.env` is read relative to the folder you launch from.

## Configuration

Settings come from environment variables or a `.env` file in the project root. Names are case-insensitive.

### Required

| Variable | Description |
|---|---|
| `S3_ENDPOINT_URL` | Storage endpoint. For R2: `https://<account_id>.r2.cloudflarestorage.com` |
| `S3_BUCKET` | Bucket name. Must be private. |
| `S3_ACCESS_KEY_ID` | Access key ID |
| `S3_SECRET_ACCESS_KEY` | Secret access key |

### Optional

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `FILE_TTL_SECONDS` | `600` | How long a file stays in storage after upload before it is deleted |
| `SIGNED_URL_TTL_SECONDS` | `480` | How long the download link works |
| `SWEEP_INTERVAL_SECONDS` | `120` | How often the sweeper runs |
| `MAX_FILE_SIZE_MB` | `500` | Largest file a job may produce |
| `JOB_RECORD_TTL_SECONDS` | `3600` | How long a job record stays in Redis after its last update |
| `JOB_TIMEOUT_SECONDS` | `900` | Maximum run time of one job in the worker |
| `RATE_LIMIT_PER_MINUTE` | `5` | Requests per client IP per minute (counted separately for `/jobs` and `/info`) |

### Rules for the time settings

- `FILE_TTL_SECONDS` must be **larger** than `SIGNED_URL_TTL_SECONDS`, so the file outlives the link.
- `JOB_RECORD_TTL_SECONDS` must be larger than `FILE_TTL_SECONDS`, so clients see `expired` instead of `404`.
- Example for a one-hour link:

  ```
  SIGNED_URL_TTL_SECONDS=3600
  FILE_TTL_SECONDS=3900
  JOB_RECORD_TTL_SECONDS=7200
  ```

- Settings are read once at startup. Restart the API, worker, and sweeper after changing them.

### Example `.env` (Cloudflare R2)

```
REDIS_URL=redis://localhost:6379/0

S3_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
S3_BUCKET=<your-bucket>
S3_ACCESS_KEY_ID=<key-id>
S3_SECRET_ACCESS_KEY=<secret>
```

Keep `.env` out of version control.

## Running the services

| Command | What it does |
|---|---|
| `uv run poe dev` | API with auto-reload on port 8000 |
| `uv run poe worker` | RQ worker with the scheduler enabled |
| `uv run poe sweeper` | Sweeper loop |
| `uv run poe test` | Run the tests |
| `uv run poe lint` | Lint with ruff |
| `uv run poe fmt` | Format with ruff |

The worker must run with the scheduler (the `poe worker` task does this), otherwise the delayed deletes never fire. The worker and sweeper do not auto-reload, so restart them after code changes.

---

## API reference

### Conventions

- **Base URL:** `http://localhost:8000` locally, or your own domain in production.
- **Format:** requests and responses are JSON, except where noted.
- **Authentication:** none by default. Protect the API at the network level or add an API-key check before exposing it publicly.
- **Errors:** most errors use the [standard error shape](#error-format-and-codes). Malformed request bodies return FastAPI's default `422` format (see below).
- **Interactive docs:** `GET /docs` (Swagger UI) and `GET /openapi.json`.

### POST /jobs

Create a download job. The job is queued and processed in the background.

**Request body**

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string (http/https URL) | required | Link to the video or audio page |
| `type` | `"video"` or `"audio"` | `"video"` | What to download |
| `quality` | `"best"`, `"1080p"`, `"720p"`, `"480p"`, `"360p"` | `"best"` | Video only. Ignored for audio. |
| `audio_format` | `"mp3"` or `"m4a"` | `"mp3"` | Audio only. Ignored for video. |

```bash
curl -s -X POST http://localhost:8000/jobs \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=VIDEO_ID","type":"video","quality":"720p"}'
```

**Response: `202 Accepted`**

```json
{ "job_id": "250ad5cd66cb4cd4a91f134ea3ef58a9", "state": "queued" }
```

**Errors**

| Status | Code | When |
|---|---|---|
| `400` | `invalid_url` | Not an http(s) URL, unresolvable host, private or internal address, credentials in the URL, or a port other than 80 / 443 |
| `422` | none (FastAPI format) | Body missing or a field has an invalid value |
| `429` | `rate_limited` | Too many requests. See the `Retry-After` header. |
| `503` | `download_failed` | The job could not be queued. Try again shortly. |

Quality notes: a quality is matched by the **shorter side** of the video, so `720p` on a vertical 1080x1920 video gives about 720x1280. If the source has no stream that small, the closest available one is used. Use [`GET /info`](#get-info) to see what is available.

### GET /jobs/{job_id}

Get the status of a job. Poll this every 1 to 2 seconds until `state` is `ready`, `failed`, or `expired`.

```bash
curl -s http://localhost:8000/jobs/250ad5cd66cb4cd4a91f134ea3ef58a9
```

**Response: `200 OK`**

```json
{
  "job_id": "250ad5cd66cb4cd4a91f134ea3ef58a9",
  "state": "ready",
  "progress": 100,
  "title": "Video title",
  "file_size": 4292654,
  "download_url": "https://<account>.r2.cloudflarestorage.com/<bucket>/media/...?X-Amz-Signature=...",
  "expires_at": "2026-10-13T08:37:44.880540Z",
  "error": null
}
```

| Field | Type | Description |
|---|---|---|
| `job_id` | string | The job identifier |
| `state` | string | See [Job states](#job-states) |
| `progress` | integer 0 to 100 | Download progress. Stays at 99 until the job is `ready`, because merging and uploading still follow the download. |
| `title` | string or null | Video title. Set when the job is `ready`. |
| `file_size` | integer or null | File size in bytes. Set when `ready`. |
| `download_url` | string or null | Signed link. Only present when `ready`. |
| `expires_at` | ISO 8601 timestamp or null | When `download_url` stops working. Only present when `ready`. |
| `error` | object or null | `{ "code": ..., "message": ... }`. Only present when `failed`. |

The download link works until `expires_at`. The file itself is deleted a little later (see [retention](#file-retention-and-cleanup)).

**Errors**

| Status | Code | When |
|---|---|---|
| `404` | `not_found` | Unknown job ID, or the job record has expired from Redis |

### POST /jobs/{job_id}/complete

Tell the API the download is finished. The file is deleted immediately and the job becomes `expired`. This is optional but recommended, because it frees storage right away.

Safe to call more than once. Storage errors are logged on the server, not returned, because the other cleanup layers will retry.

```bash
curl -i -X POST http://localhost:8000/jobs/250ad5cd66cb4cd4a91f134ea3ef58a9/complete
```

**Response: `204 No Content`** (empty body)

| Status | Code | When |
|---|---|---|
| `404` | `not_found` | Unknown job ID, or the record has expired |

Do not call this while a download is still running, because the file may be removed mid-transfer.

### GET /info

Return metadata for a URL without downloading it. Use it to show a title, thumbnail, and a quality picker before creating a job.

**Query parameters**

| Name | Description |
|---|---|
| `url` | The media URL. Must be URL-encoded. |

```bash
curl -s -G http://localhost:8000/info \
  --data-urlencode "url=https://www.youtube.com/watch?v=VIDEO_ID"
```

**Response: `200 OK`**

```json
{
  "title": "Video title",
  "uploader": "Channel name",
  "duration": 180,
  "thumbnail": "https://i.ytimg.com/vi/VIDEO_ID/maxresdefault.jpg",
  "webpage_url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "site": "Youtube",
  "max_file_size": 524288000,
  "video": [
    { "quality": "best",  "width": 1080, "height": 1920, "ext": "mp4", "filesize": 61878854, "within_limit": true },
    { "quality": "720p",  "width": 720,  "height": 1280, "ext": "mp4", "filesize": 31901046, "within_limit": true },
    { "quality": "360p",  "width": 360,  "height": 640,  "ext": "mp4", "filesize": 13136429, "within_limit": true }
  ],
  "audio": [
    { "format": "mp3", "bitrate_kbps": 192, "filesize": 4320000 },
    { "format": "m4a", "bitrate_kbps": 256, "filesize": 5760000 }
  ]
}
```

| Field | Description |
|---|---|
| `duration` | Length in seconds, or null if unknown |
| `thumbnail` | A link to the source site's thumbnail. It is not stored or proxied by this API. |
| `max_file_size` | The server's size limit in bytes |
| `video[].quality` | A value you can send as `quality` in `POST /jobs` |
| `video[].width` / `height` | The real resolution you would get |
| `video[].filesize` | **Estimate** in bytes, or null if unknown. Often higher than the real file. |
| `video[].within_limit` | `false` means the worker would reject it as `too_large`. `null` means unknown, not safe: the limit is still enforced during download. |
| `audio[].format` | A value you can send as `audio_format` in `POST /jobs` |
| `audio[].filesize` | Estimate from duration times bitrate |

`best` and the highest named quality can be identical when the source tops out there. Clients may hide the duplicate.

**Errors**

| Status | Code | When |
|---|---|---|
| `400` | `invalid_url`, `unsupported_site` | Bad or blocked URL, a live stream, or a site yt-dlp does not support |
| `422` | `download_failed` | The site refused or the video is unavailable |
| `429` | `rate_limited` | Too many requests. Uses its own counter, separate from `/jobs`. |

### GET /health

Reports whether Redis and storage are reachable. Use it for uptime monitoring and container health checks.

```bash
curl -s http://localhost:8000/health
```

**Response: `200 OK`**

```json
{ "status": "ok", "checks": { "redis": "ok", "storage": "ok" } }
```

If either check fails, the status is `503` and the body is `{ "status": "degraded", "checks": { "redis": "ok", "storage": "error" } }`.

### Job states

| State | Meaning | What the client should do |
|---|---|---|
| `queued` | Waiting for a worker | Keep polling |
| `processing` | Downloading, merging, or uploading | Keep polling and show `progress` |
| `ready` | File is uploaded and `download_url` is set | Download before `expires_at` |
| `failed` | Something went wrong. See `error`. | Show the message. Resubmit if appropriate. |
| `expired` | The file was deleted (acknowledged or timed out) | Submit a new job if the user still wants the file |

A job's record lives in Redis for `JOB_RECORD_TTL_SECONDS` after its last update. After that, `GET /jobs/{id}` returns `404`.

### Error format and codes

Application errors use this shape:

```json
{ "error": { "code": "invalid_url", "message": "Only http and https URLs are allowed." } }
```

The same `error` object appears inside a `failed` job.

| Code | Meaning |
|---|---|
| `invalid_url` | The URL is malformed, points to a non-public address, or is otherwise not allowed |
| `unsupported_site` | yt-dlp does not support this site |
| `too_large` | The file is larger than `MAX_FILE_SIZE_MB` |
| `rate_limited` | The client exceeded the request limit |
| `download_failed` | The download or processing failed. The message says why when it is safe to show. |
| `not_found` | Unknown or expired job |
| `expired` | Reserved for expired files |

Request-validation failures (for example a missing `url` field) use FastAPI's default format instead:

```json
{ "detail": [ { "loc": ["body", "url"], "msg": "Field required", "type": "missing" } ] }
```

### Rate limiting

- Default: **5 requests per minute per client IP**, counted separately for `POST /jobs` and `GET /info`.
- Fixed one-minute window: the first request starts the window and later ones in it add to the same counter.
- Over the limit you get `429` with a `Retry-After` header (seconds):

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42

{"error":{"code":"rate_limited","message":"Too many requests."}}
```

Behind a reverse proxy, configure uvicorn's `--proxy-headers` and `--forwarded-allow-ips` so the real client IP is used. Otherwise every user shares the proxy's address and one limit.

---

## Usage examples

### Complete flow with curl

```bash
API=http://localhost:8000
URL='https://www.youtube.com/watch?v=VIDEO_ID'

# 1. Optional: inspect available qualities
curl -s -G $API/info --data-urlencode "url=$URL"

# 2. Create the job
JOB=$(curl -s -X POST $API/jobs -H 'content-type: application/json' \
  -d "{\"url\":\"$URL\",\"type\":\"video\",\"quality\":\"720p\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')

# 3. Poll until ready
curl -s $API/jobs/$JOB

# 4. Download (quote the URL, it contains & characters)
curl -o video.mp4 "$(curl -s $API/jobs/$JOB | python3 -c 'import sys,json; print(json.load(sys.stdin)["download_url"])')"

# 5. Acknowledge, which deletes the file now
curl -i -X POST $API/jobs/$JOB/complete
```

### Audio

```bash
curl -s -X POST http://localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=VIDEO_ID","type":"audio","audio_format":"mp3"}'
```

### Python

```python
import time
import requests

API = "http://localhost:8000"

job = requests.post(f"{API}/jobs", json={
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "type": "video",
    "quality": "720p",
}).json()
job_id = job["job_id"]

while True:
    status = requests.get(f"{API}/jobs/{job_id}").json()
    if status["state"] in ("ready", "failed", "expired"):
        break
    time.sleep(1.5)

if status["state"] == "ready":
    with requests.get(status["download_url"], stream=True) as r:
        r.raise_for_status()
        with open("video.mp4", "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    requests.post(f"{API}/jobs/{job_id}/complete")
else:
    print(status["error"])
```

### JavaScript (browser)

```js
const API = "http://localhost:8000";

async function download(url, quality = "720p") {
  const res = await fetch(`${API}/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url, type: "video", quality }),
  });
  if (res.status === 429) throw new Error(`Retry in ${res.headers.get("Retry-After")}s`);
  if (!res.ok) throw new Error((await res.json()).error?.message ?? "Request failed");
  const { job_id } = await res.json();

  let job;
  do {
    await new Promise((r) => setTimeout(r, 1500));
    job = await (await fetch(`${API}/jobs/${job_id}`)).json();
  } while (job.state === "queued" || job.state === "processing");

  if (job.state !== "ready") throw new Error(job.error?.message ?? job.state);

  // Safety net: acknowledge when the link expires or the page closes.
  const ack = () => navigator.sendBeacon(`${API}/jobs/${job_id}/complete`);
  setTimeout(ack, Math.max(new Date(job.expires_at) - Date.now(), 0));
  window.addEventListener("pagehide", ack, { once: true });

  window.location.href = job.download_url; // starts the browser download
  return job_id;
}
```

A browser calling the API from another origin needs CORS enabled on the server. Opening `download_url` as a normal link needs no CORS setup on the bucket.

---

## File retention and cleanup

Files are short-lived by design. Three layers delete them, and any layer may run after another has already finished, because deleting a missing object is a harmless no-op.

| Layer | Trigger | Timing |
|---|---|---|
| **1. Client acknowledgement** | Client calls `POST /jobs/{id}/complete` | Immediately |
| **2. Delayed delete** | Scheduled by the worker at upload | `FILE_TTL_SECONDS` after upload |
| **3. Sweeper** | Runs every `SWEEP_INTERVAL_SECONDS`, lists the bucket | Any object older than `FILE_TTL_SECONDS` |

With the defaults, the timeline for a file nobody acknowledges is:

| Time after upload | Event |
|---|---|
| 0:00 | File uploaded, link created |
| 8:00 | Link stops working (`SIGNED_URL_TTL_SECONDS`) |
| 10:00 | File deleted by the delayed delete (`FILE_TTL_SECONDS`) |
| up to 12:00 | Worst case if the worker was down: the sweeper removes it |

The server cannot see when a download finishes, because clients download directly from storage. That is why layer 1 depends on the client and layers 2 and 3 are the guarantee. You can also add a bucket lifecycle rule as a last backstop, but those work in whole days.

The sweeper reads the bucket listing, not Redis, so it still cleans up after a worker crash or a Redis restart. An empty `media/{job_id}/` "folder" in a storage dashboard is not an object, and disappears once the file is deleted.

## Security notes

- **Keep the bucket private.** The signed URL is the only way in. Never enable a public bucket URL or a public custom domain.
- **SSRF protection.** URLs are resolved and rejected if any address is non-public (loopback, private ranges, link-local and cloud metadata addresses, carrier-grade NAT). URLs with credentials and ports other than 80 and 443 are rejected.
- **Known limits of that check.** yt-dlp resolves the host again when it downloads, and follows redirects, so a hostile DNS server or redirect could still reach an internal address. For real protection, run the worker where it cannot reach your internal network or the cloud metadata endpoint.
- **No authentication by default.** Rate limiting is the only access control. Add an API key or put the service behind a gateway if it should not be open to everyone.
- **Secrets.** Keep `.env` out of git, use a bucket-scoped access key, and use a separate key for production.
- **Resource limits.** The size cap (`MAX_FILE_SIZE_MB`) is enforced during download and on the finished file. Each job writes to the worker's temp directory before upload, so size the disk accordingly.

## Deployment

### Docker Compose

`docker/Dockerfile` and `docker/docker-compose.yml` run the API, worker, sweeper, and Redis. Build from the project root:

```bash
docker compose -f docker/docker-compose.yml up --build -d
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f worker
curl -s localhost:8000/health
```

- `.env` is injected at runtime and is not baked into the image.
- Compose overrides `REDIS_URL` to point at the Redis container.
- `S3_ENDPOINT_URL=http://localhost:...` does not work inside a container, because `localhost` there is the container itself.
- Port 8000 is bound to `127.0.0.1`. Put a reverse proxy (Caddy or nginx) in front for public HTTPS.

### systemd on a VPS

Run the three processes as services that restart on failure:

- API: `uvicorn app.main:app --app-dir src --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1`
- Worker: `python -m app.worker.run worker`
- Sweeper: `python -m app.worker.run sweeper`

Set `PYTHONPATH=src` and the working directory to the project root. Put Caddy or nginx in front for HTTPS, bind Redis to localhost, and open only ports 80 and 443.

### Keep yt-dlp up to date

Sites change often and downloads break when yt-dlp is old. Update regularly:

```bash
uv lock --upgrade-package yt-dlp
```

Then restart the API and worker (or rebuild the Docker image).

### Datacenter IPs

Some sites, YouTube in particular, block requests from datacenter IP addresses. A download that works from your home connection may fail from a VPS with a "Sign in to confirm you're not a bot" message. Test one real download on the server before relying on it.

## Development

```bash
uv sync                  # install runtime and dev dependencies
uv run poe test          # pytest
uv run poe lint          # ruff check
uv run poe fmt           # ruff format
```

Tests that touch Redis or storage use fakes, so they need no running services.

When running a one-off script outside the poe tasks, put `src` on the Python path:

```bash
PYTHONPATH=src uv run python -c "from app.services import storage; print(storage.list_objects())"
```

## Project structure

```
.
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── src/app/
│   ├── main.py                  # FastAPI app, router registration, error handlers
│   ├── config.py                # Settings from environment
│   ├── api/
│   │   ├── deps.py              # Shared dependencies (Redis client)
│   │   └── routes/
│   │       ├── routes_jobs.py   # POST /jobs, GET /jobs/{id}, POST /jobs/{id}/complete
│   │       ├── routes_info.py   # GET /info
│   │       └── routes_health.py # GET /health
│   ├── models/
│   │   └── schemas.py           # Request and response models, enums
│   ├── services/
│   │   ├── jobs.py              # Job records in Redis
│   │   ├── storage.py           # All S3 / R2 access (boto3)
│   │   ├── retention.py         # Expiry and delete helpers
│   │   ├── ratelimit.py         # Per-IP request counters
│   │   ├── validators.py        # URL checks and SSRF protection
│   │   └── info.py              # Turns yt-dlp metadata into the /info response
│   └── worker/
│       ├── downloader.py        # yt-dlp wrapper (download, size cap, metadata)
│       ├── tasks.py             # process_job: download, upload, mark ready
│       ├── cleanup.py           # Delayed delete task and the sweeper
│       └── run.py               # Entrypoints: worker and sweeper
├── tests/
├── pyproject.toml
└── uv.lock
```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `/health` shows `"storage":"error"` | Check `S3_BUCKET`, the keys, and `S3_ENDPOINT_URL`. An empty bucket name is the usual cause. Make sure `.env` is in the folder you run from. |
| Job stays `queued` | The worker is not running, or it uses a different Redis than the API. |
| Job fails with a generic "Something went wrong" message | An unexpected error after the download (often upload). The worker log has the full traceback. |
| Job fails with "Sign in to confirm you're not a bot" or a 403 | The site is blocking your IP or client. Update yt-dlp first. From a VPS, see [Datacenter IPs](#datacenter-ips). |
| Code changes have no effect on jobs | The worker and sweeper do not auto-reload. Restart them. |
| Delayed deletes never run | The worker is not running with the scheduler. Use `uv run poe worker`. |
| `ModuleNotFoundError: No module named 'app'` | Run from the project root and set `PYTHONPATH=src` for one-off scripts. |
| Download link returns `AccessDenied` | The link expired. Check `expires_at` and create a new job. |
| Setting changes are ignored | Settings are read at startup. Restart all three processes. |
| `/info` sizes differ from the real file | Sizes are estimates and are often higher than the final file. |

## Legal notice

Downloading content from some websites can violate their terms of service, and what you may copy or distribute depends on copyright law in your country and on the content itself. You are responsible for how you use this software and for what you offer to other users. Only download content you have the right to download.

## License

MIT