"""Only user-configured execution limits; cancellation has a distinct cause."""
import time

from .harness_errors import HarnessFailure


def timeout_seconds(role):
    value=role.get('timeout_seconds')
    if value is None:return 0
    if type(value)!=int or (value!=0 and not 10<=value<=86400):
        raise ValueError('timeout_seconds 需为 0（不限时）或 10–86400 秒')
    return value


def cancel_job(job,kind,reason):
    if not job['cancel'].is_set():
        job['cancel_kind']=kind;job['cancel_reason']=reason
        job['cancel'].set()


class ExecutionStopped(HarnessFailure):
    def __init__(self,kind,reason):
        super().__init__(reason)
        self.kind=kind


class ExecutionGuard:
    def __init__(self,bridge,job,role):
        self.bridge=bridge;self.job=job;self.limit=timeout_seconds(role)
        self.started=time.monotonic()
        job['started_at']=time.time();job['timeout_seconds']=self.limit

    def check(self,*,check_timeout=True):
        if self.job['cancel'].is_set():
            raise ExecutionStopped(self.job.get('cancel_kind','lease_invalid'),
                self.job.get('cancel_reason','执行已停止：服务器不再认可此任务的租约'))
        if self.bridge.stop.is_set():
            raise ExecutionStopped('bridge_stopped','执行已停止：本地 bridge 已关闭；请检查部分成果')
        if check_timeout and self.limit and time.monotonic()-self.started>=self.limit:
            elapsed=int(time.monotonic()-self.started)
            raise ExecutionStopped('execution_timeout',f'执行超时：达到明确配置的 {self.limit} 秒时限（已运行 {elapsed} 秒）；请检查部分成果后再决定是否继续')
