FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System dependencies
# libmagic1            — required by python-magic (file type validation)
# libpango/pangocairo  — required by weasyprint (PDF generation)
# libffi-dev           — required by weasyprint
# fontconfig           — required by weasyprint for font discovery
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SECRET_KEY placeholder — safe for collectstatic, never baked into the image.
RUN SECRET_KEY=build-time-placeholder \
    python manage.py collectstatic --noinput --settings=config.settings.prod

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
