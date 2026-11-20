FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py programs.json ./
ENV PORT=8080 MAX_EPISODES=25 CACHE_SECONDS=1800 PROGRAMS_FILE=programs.json
EXPOSE 8080
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 900 app:APP"]
