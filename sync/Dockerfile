FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && curl https://rclone.org/install.sh | bash \
    && apt-get purge -y curl unzip \
    && rm -rf /var/lib/apt/lists/*

ENV RCLONE_CONFIG=/config/rclone/rclone.conf
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sync_clips.py .

CMD ["python", "sync_clips.py"]
