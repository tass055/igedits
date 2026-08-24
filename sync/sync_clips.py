"""
Watches Postgres for newly completed igedits clips and pushes each one to
Dropbox (via rclone) along with an AI-suggested caption/hashtags .txt file.

Runs as a standalone loop inside the `dropbox-sync` container. Does not
modify the igedits backend/frontend — it only reads the DB and calls the
existing /api/instagram/suggest-caption endpoint.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

CLIPS_DIR = Path(os.environ.get("CLIPS_DIR", "/app/clips"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/state/synced_clips.json"))
EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/export"))

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8888")
DROPBOX_REMOTE = os.environ.get("DROPBOX_REMOTE", "dropbox")
DROPBOX_PATH = os.environ.get("DROPBOX_PATH", "igedits-clips")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "300"))

DB_DSN = (
    f"dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']} "
    f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
    f"port={os.environ.get('POSTGRES_PORT', '5432')}"
)

QUERY = """
    SELECT
        gc.id AS clip_id,
        gc.filename,
        gc.file_path,
        t.id AS task_id,
        t.user_id,
        s.title AS source_title
    FROM generated_clips gc
    JOIN tasks t ON t.id = gc.task_id
    LEFT JOIN sources s ON s.id = t.source_id
    WHERE t.status = 'completed'
    ORDER BY gc.created_at ASC
"""


def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(synced: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(synced)))


def safe_folder_name(name: str) -> str:
    name = (name or "untitled").strip()
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in name)[:100] or "untitled"


def fetch_caption(clip_id: str, user_id: str) -> str | None:
    try:
        resp = requests.get(
            f"{BACKEND_URL}/instagram/suggest-caption",
            params={"clip_id": clip_id},
            headers={"user_id": user_id},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  caption request failed ({resp.status_code}): {resp.text[:200]}")
            return None
        return resp.json().get("caption")
    except requests.RequestException as e:
        print(f"  caption request error: {e}")
        return None


def sync_clip(row, synced: set) -> None:
    clip_id = row["clip_id"]
    src_path = CLIPS_DIR / row["file_path"] if not row["file_path"].startswith("/") else Path(row["file_path"])
    if not src_path.exists():
        # try filename directly under CLIPS_DIR as a fallback
        src_path = CLIPS_DIR / row["filename"]
    if not src_path.exists():
        print(f"skip {clip_id}: file not found ({src_path})")
        return

    folder = safe_folder_name(row["source_title"] or row["task_id"])
    export_subdir = EXPORT_DIR / folder
    export_subdir.mkdir(parents=True, exist_ok=True)

    dest_video = export_subdir / row["filename"]
    if not dest_video.exists():
        dest_video.write_bytes(src_path.read_bytes())

    caption_path = export_subdir / f"{Path(row['filename']).stem}.txt"
    caption = fetch_caption(clip_id, row["user_id"])
    caption_path.write_text(caption or "(caption generation failed — add manually)")

    remote_target = f"{DROPBOX_REMOTE}:{DROPBOX_PATH}/{folder}"
    result = subprocess.run(
        ["rclone", "copy", str(export_subdir), remote_target, "--create-empty-src-dirs"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  rclone copy failed for {clip_id}: {result.stderr[:400]}")
        return

    print(f"synced {clip_id} -> {remote_target}")
    synced.add(clip_id)


def run_once() -> None:
    synced = load_state()
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()

    new_rows = [r for r in rows if r["clip_id"] not in synced]
    if not new_rows:
        print("no new clips to sync")
        return

    print(f"found {len(new_rows)} new clip(s) to sync")
    for row in new_rows:
        sync_clip(row, synced)
    save_state(synced)


def main() -> None:
    print(f"dropbox-sync starting, interval={SYNC_INTERVAL_SECONDS}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"sync cycle failed: {e}")
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
