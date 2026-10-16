FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr fonts-dejavu-core libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements-legacy-linux.txt ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install . -r requirements-legacy-linux.txt \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin framepull

USER 10001:10001
EXPOSE 8094

CMD ["python", "-m", "uvicorn", "frame_pull_service.main:app", "--host", "0.0.0.0", "--port", "8094"]
