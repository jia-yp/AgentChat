"""Reuse the native project of this bridge's exact Antigravity conversation."""
import sqlite3
from pathlib import Path


def native_project(session):
    if session['definition']['harness']!='antigravity':return ''
    explicit=session['definition'].get('project') or session.get('project_id')
    if explicit:return explicit
    native=session.get('native_id')
    if not native:return ''
    path=Path.home()/'.gemini/antigravity/conversation_summaries.db'
    if not path.is_file():return ''
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            row=db.execute('SELECT project_id FROM conversation_summaries WHERE conversation_id=?',(native,)).fetchone()
            return row[0] if row and row[0] else ''
    except sqlite3.Error:return ''
