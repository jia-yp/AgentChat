import os
import uvicorn

uvicorn.run('server.app:create_app',factory=True,host=os.environ.get('AGENTCHAT_HOST','127.0.0.1'),port=int(os.environ.get('AGENTCHAT_PORT','8781')),proxy_headers=False)
