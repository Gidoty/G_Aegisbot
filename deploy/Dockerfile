# One image for both services; compose chooses the command.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 aegis
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY g_aegisbot ./g_aegisbot
RUN mkdir -p /app/data && chown -R aegis:aegis /app/data
USER aegis
CMD ["python", "-m", "g_aegisbot.engine.runner"]
