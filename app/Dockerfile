FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m appuser
COPY . .
RUN DJANGO_DEBUG=1 python manage.py collectstatic --noinput
RUN mkdir -p /data/private && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8000
CMD ["waitress-serve", "--listen=0.0.0.0:8000", "config.wsgi:application"]
