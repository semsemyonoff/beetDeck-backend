# Production image for beetDeck — the Flask API plus the pre-built SPA.
#
# The React/Vite SPA lives in its own repository and is built separately; its
# production output must be placed at src/static/dist/ in the build context
# before building this image (Flask serves it from /static/dist/). See README.md.
FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are declared in pyproject.toml (single source of truth).
COPY pyproject.toml README.md ./
COPY app.py ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /tmp/beetdeck && chmod 1777 /tmp/beetdeck

EXPOSE 5000
ENV TMPDIR=/tmp/beetdeck

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "--worker-tmp-dir", "/tmp/beetdeck", "app:app"]
