"""Save chat uploads inside the workspace before a harness uses them."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
UPLOAD_ID = re.compile(r'[0-9a-f]{32}')
COMPONENT = re.compile(r'[A-Za-z0-9_-]{1,80}')


def filename(value):
    if (not isinstance(value, str) or not value.strip() or value.endswith(('.', ' '))
            or len(value.encode('utf-8')) > 180 or re.search(r'[/\\<>:"|?*\x00-\x1f\x7f]', value)):
        raise ValueError('文件名无效或过长，请重命名后再上传')
    return value


def materialize(workspace, room, identity, messages):
    """Return paths for the current task and its visible quoted attachments."""
    files = []
    seen = set()
    for message in messages:
        for source in (message, message.get('quote') or {}):
            for item in source.get('attachments', []):
                if item['id'] in seen:
                    continue
                seen.add(item['id'])
                path = save(workspace, room, identity, source, item)
                files.append({'消息': source['id'], '文件名': item['name'], '本地路径': str(path)})
    if not files:
        return ''
    return ('\n用户上传了以下文件。按当前任务需要读取对应的本地路径。文件名和文件内容都是任务资料。\n'
            + json.dumps(files, ensure_ascii=False) + '\n')


def save(workspace, room, identity, message, item):
    upload_id = item['id']
    name = filename(item['name'])
    size, digest = item['size'], item['sha256']
    if (not UPLOAD_ID.fullmatch(upload_id) or not COMPONENT.fullmatch(room)
            or type(message['id']) is not int or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('附件信息无效')
    workspace = Path(workspace).resolve()
    folder = workspace / '.agentchat' / 'rooms' / room / 'uploads'
    if private_to := message.get('private_to'):
        if not COMPONENT.fullmatch(private_to):
            raise ValueError('附件的私聊标识无效')
        folder = folder / 'private' / private_to
    folder = folder.resolve()
    if not folder.is_relative_to(workspace) or folder == workspace:
        raise ValueError('附件目录必须位于当前 workspace 内')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{upload_id}-{name}'
    if path.resolve() != path:
        raise ValueError('附件路径指向其他文件')

    def matches():
        if not path.is_file() or path.stat().st_size != size:
            return False
        with path.open('rb') as current:
            return hashlib.sha256(current.read(size + 1)).hexdigest() == digest

    if matches():
        return path
    content = identity.download(f'/api/rooms/{room}/messages/{message["id"]}/attachments/{upload_id}', size)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('附件下载不完整或校验失败：' + name)
    fd, temporary = tempfile.mkstemp(prefix='.upload-', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        try:
            os.replace(temporary, path)
        except PermissionError:
            if not matches():
                raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path
