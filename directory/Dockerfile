# The community directory (Broadsheet Feeds). Built from the repo root: it shares the feed checks, starter feeds and fonts.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY directory/requirements.txt directory/requirements.txt
RUN pip install -r directory/requirements.txt
COPY crossposter/__init__.py crossposter/feedcheck.py ./crossposter/
COPY directory ./directory
COPY feeds ./feeds
COPY static/fonts ./static/fonts
COPY static/icons ./static/icons
# The database lives on a volume mounted at /data.
ENV DATA_DIR=/data
CMD ["python", "-m", "directory.app"]
