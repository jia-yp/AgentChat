FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server server
COPY bridge bridge
COPY skills skills
COPY deploy deploy
COPY agentchat.py README.md requirements-deepseek.txt ./
# A fresh named volume copies this owner (568:568, the usual NAS "apps" account).
# The entrypoint then runs the server as whoever owns /data; see deploy/docker-entrypoint.sh.
RUN mkdir -p /data && chown 568:568 /data && chmod 755 /app/deploy/docker-entrypoint.sh
ENV AGENTCHAT_DATA=/data AGENTCHAT_HOST=0.0.0.0 AGENTCHAT_PORT=8781 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8781
VOLUME ["/data"]
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
CMD ["python", "-m", "server"]
