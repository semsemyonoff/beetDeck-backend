# Stage 1: build the Vite frontend
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci
COPY frontend ./frontend
RUN cd frontend && npm run build

# Stage 2: Python runtime
FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY app.py /app/app.py
COPY src /app/src
COPY --from=frontend-build /build/src/static/dist /app/src/static/dist

RUN mkdir -p /tmp/beetdeck && chmod 1777 /tmp/beetdeck

WORKDIR /app
EXPOSE 5000
ENV TMPDIR=/tmp/beetdeck

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "--worker-tmp-dir", "/tmp/beetdeck", "app:app"]
