# kura auto-tagger worker (compose.yaml profile "tagger"). Build context: the repository root.
# CPU torch; torch and torchvision come from the same index (README "Auto-tagging").
FROM python:3.14-slim

WORKDIR /app
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY tagger/requirements.txt tagger/requirements.txt
RUN pip install --no-cache-dir -r tagger/requirements.txt

COPY VERSION ./
COPY api api
COPY tagger tagger

# The model cache (and the first release build) live on the /models volume.
RUN useradd --uid 1000 --create-home kura && mkdir /models && chown kura /models
USER kura
ENV PYTHONUNBUFFERED=1 TAGGER_PROFILE=wd14 KURA_MODELS_DIR=/models HF_HOME=/models/huggingface
CMD ["python", "-m", "tagger.worker"]
