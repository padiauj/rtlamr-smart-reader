FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

RUN \
  apk add --no-cache \
    ca-certificates \
    curl \
    python3 \
    py3-pip \
    rtl-sdr \
    tar \
    tzdata

COPY requirements.txt /tmp/requirements.txt
RUN \
  python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
  && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

RUN \
  set -eux; \
  case "${BUILD_ARCH}" in \
    amd64) RTLAMR_ARCH="amd64" ;; \
    aarch64) RTLAMR_ARCH="arm64" ;; \
    *) echo "Unsupported architecture for rtlamr: ${BUILD_ARCH}" >&2; exit 1 ;; \
  esac; \
  curl -fsSL \
    "https://github.com/bemasher/rtlamr/releases/download/v0.9.5/rtlamr_linux_${RTLAMR_ARCH}.tar.gz" \
    -o /tmp/rtlamr.tar.gz; \
  tar -xzf /tmp/rtlamr.tar.gz -C /usr/local/bin rtlamr; \
  chmod a+x /usr/local/bin/rtlamr; \
  rm -f /tmp/rtlamr.tar.gz

COPY app /app
COPY run.sh /
RUN chmod a+x /run.sh

ENV PATH="/opt/venv/bin:${PATH}"

CMD ["/run.sh"]
