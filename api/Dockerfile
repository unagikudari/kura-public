# kura API (and the one-shot database steps of compose.yaml). Build context: the repository root.
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY api/requirements.lock api/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r api/requirements.lock

COPY VERSION alembic.ini ./
COPY api api
COPY scripts scripts
COPY ops ops
# publish_release.py and the tagger setup run here (they need the API's database driver).
COPY tagger tagger

# Originals and renditions live on the /data volume; a new named volume takes this ownership.
RUN useradd --uid 1000 --create-home kura && mkdir /data && chown kura /data
USER kura
ENV PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2", \
     "--timeout-worker-healthcheck", "30", "--no-server-header"]
