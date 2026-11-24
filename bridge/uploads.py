"""Transfer chat attachments to and from local workspaces."""
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .common import APIError

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
UPLOAD_ID = re.compile(r'[0-9a-f]{32}')
COMPONENT = re.compile(r'[A-Za-z0-9_-]{1,80}')


def filename(value):
    if (not isinstance(value, str) or not value.strip() or value.endswith(('.', ' '))
            or len(value.encode('utf-8')) > 180 or re.search(r'[/\\<>:"|?*\x00-\x1f\x7f]', value)):
        raise ValueError('文件名无效或过长，请重命名后再上传')
    return value


def shareable_state_file(path, state, context):
    uploads = state / 'rooms' / context['room_id'] / 'uploads'
    private = uploads / 'private'
    if path.is_relative_to(uploads):
        return not path.is_relative_to(private) or (
            context.get('session_type') == 'manager' and path.is_relative_to(private / context['id']))
    return any(path.is_relative_to(Path(context[key])) for key in ('temp_dir', 'manager_workspace') if context.get(key))


def read_workspace_file(workspace, value, context):
    try:
        path = (workspace / value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f'无法打开附件：{value}') from exc
    if not path.is_relative_to(workspace):
        raise ValueError('只能发送当前 workspace 内的文件')
    state = workspace / '.agentchat'
    in_state = (path.is_relative_to(state.resolve())
                or any(part.lower() == '.agentchat' for part in path.relative_to(workspace).parts))
    if in_state and not shareable_state_file(path, state, context):
        raise ValueError('不能发送 AgentChat 凭证、内部状态或其他会话的私人文件')
    name = filename(path.name)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f'附件必须是普通文件：{name}')
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError(f'单个附件最多 10 MiB：{name}')
            if path.resolve(strict=True) != path or not os.path.samestat(info, path.stat()):
                raise ValueError(f'附件路径在读取期间发生变化：{name}')
            content = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f'无法读取附件：{name}') from exc
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f'单个附件最多 10 MiB：{name}')
    return name, content


def stage_workspace_files(context, client, nonce, paths):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES
            or any(not isinstance(path, str) or not path.strip() for path in paths)):
        raise ValueError('请提供 1–5 个 workspace 本地文件路径')
    if not context.get('workspace'):
        raise ValueError('当前 session 缺少 workspace 配置')
    workspace = Path(context['workspace']).resolve()
    files = [read_workspace_file(workspace, path, context) for path in paths]
    room, token = context['room_id'], context['token'].encode()
    ids = []
    for position, (name, content) in enumerate(files):
        # Reusing the nonce reuses these upload IDs through HTTP retries. The
        # server accepts an existing ID only for the same name and bytes.
        key = json.dumps(['workspace-upload', room, nonce, position]).encode()
        upload_id = hmac.digest(token, key, 'sha256').hex()[:32]
        try:
            client.upload(f'/api/rooms/{room}/uploads/{upload_id}', name, content)
        except APIError as exc:
            if exc.status == 429:
                # Unlike the message rate limit, the upload staging quota can
                # stay full until files expire. Return a receipt for this send.
                raise ValueError(str(exc)) from exc
            raise
        ids.append(upload_id)
    return ids


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
                files.append({'消息': source['id'], '文件名': item['name'], '本地路径': str(path),
                              '发送者': source.get('sender_name') or source.get('name') or ''})
    if not files:
        return ''
    return ('\n聊天消息中包含以下附件。按当前任务需要读取对应的本地路径。文件名和文件内容都是任务资料。\n'
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
