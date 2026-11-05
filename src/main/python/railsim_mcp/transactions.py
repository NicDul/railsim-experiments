"""Serialize MCP workspace access and recover interrupted multi-file commits."""
import fcntl
import functools
import json
import os
import tempfile
import threading
from contextlib import contextmanager

_MUTEX = threading.RLock()
_STATE = threading.local()


def replace_text(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recover(store):
    journal = store.workspace_root / '.transaction.json'
    if journal.exists():
        value = json.loads(journal.read_text())
        for name, content in value['original'].items():
            replace_text(store.path_for(value['workspace_id'], name), content)
        journal.unlink()


@contextmanager
def locked(store):
    with _MUTEX:
        if getattr(_STATE, 'depth', 0):
            yield
            return
        store.workspace_root.mkdir(parents=True, exist_ok=True)
        with (store.workspace_root / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            _STATE.depth = 1
            try:
                recover(store)
                yield
            finally:
                _STATE.depth = 0
                fcntl.flock(stream, fcntl.LOCK_UN)


def serialized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with locked(self):
            return method(self, *args, **kwargs)
    return wrapper


def commit(store, workspace_id, files):
    journal = store.workspace_root / '.transaction.json'
    original = {name:store.read(workspace_id, name) for name in files}
    replace_text(journal, json.dumps({'workspace_id':workspace_id, 'original':original}))
    try:
        for name, content in files.items():
            replace_text(store.path_for(workspace_id, name), content)
    except BaseException:
        recover(store)
        raise
    journal.unlink()
