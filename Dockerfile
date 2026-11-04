# Small image for hosts that run containers (Fly.io, a VPS with Docker, etc.).
# No third-party Python packages are needed.
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html ./

# Put the database on a persistent volume mounted at /data.
ENV TRACKER_DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

# Set TRACKER_PASSWORD as a secret on your host; without it, remote requests are refused.
CMD ["python", "app.py"]
