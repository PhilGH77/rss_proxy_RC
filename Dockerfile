FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py programs.json ./
RUN mkdir -p /app/data
ENV PORT=8080 DATA_DIR=/app/data PROGRAMS_FILE=/app/programs.json MAX_EPISODES=10 CACHE_SECONDS=21600 AUTO_REFRESH=true PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile - app:APP"]
