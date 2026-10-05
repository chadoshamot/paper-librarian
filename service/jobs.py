"""Bounded background jobs with per-job progress; never redirect global stdout."""
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="paperbook")
_LOCK = threading.RLock()
_JOBS = {}
_LOCAL = threading.local()
_MUTATIONS = threading.RLock()
MAX_JOBS = 200


def progress(event):
    jid = getattr(_LOCAL, "jid", None)
    if jid:
        with _LOCK:
            _JOBS[jid]["events"].append(dict(event))
            _JOBS[jid]["events"] = _JOBS[jid]["events"][-100:]


def snapshot(jid):
    with _LOCK:
        return copy.deepcopy(_JOBS.get(jid))


def start(fn, *, serial=True):
    jid = uuid.uuid4().hex
    with _LOCK:
        for key, job in list(_JOBS.items()):
            if job["status"] in ("done", "error") and time.time() - job["created"] > 3600:
                del _JOBS[key]
        if len(_JOBS) >= MAX_JOBS:
            raise ValueError("后台任务已满，请稍后重试")
        _JOBS[jid] = {"status": "running", "result": None, "error": None,
                      "log": "", "events": [{"status": "queued", "label": "等待执行"}],
                      "created": time.time()}

    def run():
        _LOCAL.jid = jid
        try:
            if serial:
                with _MUTATIONS:
                    progress({"status": "running", "label": "开始执行"})
                    result = fn()
            else:
                result = fn()
            with _LOCK:
                _JOBS[jid].update(status="done", result=result)
        except Exception as exc:
            with _LOCK:
                _JOBS[jid].update(status="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            _LOCAL.jid = None

    _POOL.submit(run)
    return jid
