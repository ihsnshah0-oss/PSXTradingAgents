# PSX Trading Desk — a single-purpose Karachi equity research desk.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PSXDESK_WORK_DIR=/data

WORKDIR /app

# `uncompress` (gzip) is used for PSX's legacy .Z daily files; ca-certificates
# for the HTTPS calls to psx.com.pk / sbp.org.pk.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gzip \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY psx_desk ./psx_desk
RUN pip install --no-cache-dir .

RUN mkdir -p /data
VOLUME ["/data"]

ENTRYPOINT ["psx-desk"]
CMD ["describe"]
