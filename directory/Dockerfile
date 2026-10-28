# Broadsheet's website: downloads, the community directory and the browser version. Built from the repo root: it shares
# the feed checks, starter feeds and fonts, and packs the app itself for the browser version.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY directory/requirements.txt directory/requirements.txt
RUN pip install -r directory/requirements.txt
COPY directory ./directory
COPY feeds ./feeds
# The browser version at /web runs the whole app in the visitor's browser; web/build.py packs it into web/dist.
COPY crossposter ./crossposter
COPY static ./static
COPY web ./web
RUN python -m web.build
# The database lives on a volume mounted at /data.
ENV DATA_DIR=/data
CMD ["python", "-m", "directory.app"]
