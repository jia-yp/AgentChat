FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server server
COPY bridge bridge
COPY skills skills
COPY deploy deploy
COPY agentchat.py README.md requirements-deepseek.txt ./
ENV AGENTCHAT_DATA=/data AGENTCHAT_HOST=0.0.0.0 AGENTCHAT_PORT=8781 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8781
CMD ["python", "-m", "server"]
