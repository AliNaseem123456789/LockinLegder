# OPTIONAL. Use this only if you want the pdftotext extractor.
#
# The statement reader tries three extractors in order:
#   pdfplumber  (pip)  ->  pdftotext -layout  (system binary)  ->  pypdf (pip)
#
# On Render's native Python runtime you cannot apt-get, so the middle one is
# simply absent and the reader falls back to pypdf. That degrades rather than
# breaks - but layout mode is what preserves the section headings that say
# which way the money went, so having the second opinion is worth a Dockerfile
# if statements are the main use case.
#
# To use it: set  runtime: docker  in render.yaml and remove buildCommand and
# startCommand, or pick "Docker" in the Render dashboard.

FROM python:3.11-slim

# poppler-utils provides pdftotext. The rest is what pdfplumber's imaging
# stack links against.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main2.py audit_log.py ./

# Render injects PORT. The shell form is required so it expands.
ENV PORT=8000
EXPOSE 8000

# One worker, deliberately - see render.yaml.
CMD uvicorn main2:app --host 0.0.0.0 --port $PORT --workers 1