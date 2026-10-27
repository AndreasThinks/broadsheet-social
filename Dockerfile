FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY crossposter ./crossposter
COPY static ./static
COPY feeds ./feeds
# Persistent state lives on a volume mounted at /data: the database and your editable feeds.
ENV DATA_DIR=/data FEEDS_DIR=/data/feeds
CMD ["python", "-m", "crossposter.app"]
