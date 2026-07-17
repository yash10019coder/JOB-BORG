FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: postgres client libs (for psycopg) + build essentials.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements/ requirements/
ARG REQUIREMENTS=requirements/dev.txt
RUN pip install -r ${REQUIREMENTS}

COPY . .

RUN chmod +x scripts/entrypoint.sh scripts/wait-for-postgres.sh

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
