FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app.py programs.json /app/
ENV PORT=8080 MAX_EPISODES=10 CACHE_SECONDS=3600 PROGRAMS_FILE=/app/programs.json PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 900 --access-logfile - --error-logfile - app:APP"]
