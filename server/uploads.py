"""Durable uploads linked to messages in the same SQLite transaction."""
import hashlib
import re
import time
from urllib.parse import unquote

from bridge.uploads import MAX_FILES, MAX_FILE_BYTES, UPLOAD_ID, filename

SCHEMA = '''
CREATE TABLE IF NOT EXISTS message_uploads(
 id TEXT PRIMARY KEY, room TEXT NOT NULL, sender TEXT NOT NULL,
 name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
 created REAL NOT NULL, message INTEGER REFERENCES messages(id), position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS uploads_message ON message_uploads(message, position);
CREATE INDEX IF NOT EXISTS uploads_sender ON message_uploads(sender, message);
'''
MAX_PENDING_FILES = 20
PENDING_SECONDS = 24 * 60 * 60
UPLOAD_PATH = re.compile(r'/api/rooms/[0-9a-f]+/uploads/[0-9a-f]{32}')


def is_upload_request(method, path):
    return method == 'POST' and UPLOAD_PATH.fullmatch(path) is not None


def sender_for(store, db, room, actor):
    from .core import Problem
    if actor['kind'] != 'user':
        raise Problem('只有群聊用户可以上传附件', 403)
    store.access(db, room, actor, manage=False)
    if actor['_room_role'] == 'observer':
        raise Problem('观察者只能阅读', 403)
    return actor['id'] + ':' + room


def check_access(store, room, actor):
    with store.tx() as db:
        sender_for(store, db, room, actor)


async def read_upload(request):
    from .core import Problem
    try:
        name = filename(unquote(request.headers.get('x-agentchat-filename', ''), errors='strict'))
    except ValueError as exc:
        raise Problem('文件名无效或过长，请重命名后再上传') from exc
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_FILE_BYTES:
            raise Problem('单个附件最多 10 MiB', 413)
        content.extend(chunk)
    return name, content


def save(store, room, actor, upload_id, name, content):
    from .core import Problem
    digest = hashlib.sha256(content).hexdigest()
    now = time.time()
    with store.tx() as db:
        sender = sender_for(store, db, room, actor)
        db.execute('DELETE FROM message_uploads WHERE message IS NULL AND created < ?', (now - PENDING_SECONDS,))
        row = db.execute('SELECT room,sender,name,size,sha256 FROM message_uploads WHERE id=?', (upload_id,)).fetchone()
        if row:
            if tuple(row) != (room, sender, name, len(content), digest):
                raise Problem('附件编号已用于其他文件', 409)
        else:
            pending = db.execute('SELECT COUNT(*) FROM message_uploads WHERE sender=? AND message IS NULL', (sender,)).fetchone()[0]
            if pending >= MAX_PENDING_FILES:
                raise Problem('未发送的附件太多，请先发送已有附件或等暂存附件过期', 429)
            db.execute('''INSERT INTO message_uploads(id,room,sender,name,size,sha256,content,created)
                VALUES(?,?,?,?,?,?,?,?)''', (upload_id, room, sender, name, len(content), digest, content, now))
    return {'id': upload_id}


def metadata(db, message):
    return [dict(row) for row in db.execute(
        'SELECT id,name,size,sha256 FROM message_uploads WHERE message=? ORDER BY position', (message,))]


def bind(db, room, sender, message, ids):
    from .core import Problem
    if len(ids) > MAX_FILES or len(set(ids)) != len(ids) or any(not UPLOAD_ID.fullmatch(value) for value in ids):
        raise Problem('无效的附件清单')
    for position, upload_id in enumerate(ids):
        result = db.execute('''UPDATE message_uploads SET message=?,position=?
            WHERE id=? AND room=? AND sender=? AND message IS NULL AND created>=?''',
            (message, position, upload_id, room, sender, time.time() - PENDING_SECONDS))
        if result.rowcount != 1:
            raise Problem('附件不可用，请重新选择文件', 409)


def download(store, actor, room, message, upload_id):
    from .core import Problem, visible_message
    from .synchronous import readable
    with store.tx() as db:
        store.access(db, room, actor, manage=False)
        source = db.execute('SELECT * FROM messages WHERE id=? AND room=?', (message, room)).fetchone()
        if not source or source['recalled_at'] or not visible_message(actor, source) or not readable(db, actor, source):
            raise Problem('附件不存在或不可见', 404)
        row = db.execute('SELECT name,content FROM message_uploads WHERE id=? AND message=?', (upload_id, message)).fetchone()
        if not row:
            raise Problem('附件不存在或不可见', 404)
        return row['name'], row['content']
