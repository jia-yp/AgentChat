"""Classify terminal harness failures, never ordinary model text or tool output."""
import re


def is_rate_limit(text):
    return bool(re.search(r'(?i)(rate[ _-]?limit|too many requests|resource[_ ]exhausted|quota[_ ]exceeded|insufficient[_ ]quota|usage limit|you[\u2019\']?ve (?:hit|reached) your (?:(?:\d+[ -]?hour|weekly|daily|session) )?limit|(?:weekly|daily|session) limit (?:reached|exceeded)|credits? (?:exhausted|depleted)|(?:http|status(?: code)?)\D{0,6}429|额度(?:已用完|耗尽)|请求过于频繁)',str(text)))


def is_context_limit(text):
    return bool(re.search(r'(?i)(prompt (?:is )?too long|context[_ ](?:length[_ ]exceeded|window[_ ]exceeded)|maximum context length|input (?:is )?too long|too many (?:input )?tokens|上下文.{0,10}(?:超长|超出|超过))', str(text)))


class HarnessFailure(RuntimeError):
    def __init__(self,message):
        super().__init__(message)
        self.kind='rate_limit' if is_rate_limit(message) else 'context_limit' if is_context_limit(message) else 'harness_error'
