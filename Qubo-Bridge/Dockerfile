FROM python:3.12-slim

# ffmpeg does the muxing + RTSP publish; the bridge does P2P + decrypt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge ./bridge

RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 \
    QUBO_SESSION_FILE=/data/qubopy_session.json

ENTRYPOINT ["python", "-m", "bridge"]
