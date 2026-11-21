"""Names shared by setup validation and room mention routing."""
import re


NAME_PATTERN = r'[a-zA-Z\u3400-\u4dbf\u4e00-\u9fff][a-zA-Z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,39}'
RESERVED = {'all', 'everyone', '所有人', '全体', '管理员', 'admin', 'system'}


def normalize_name(value):
    if not isinstance(value, str):
        raise ValueError('名称必须是文本')
    value = value.lower().strip()
    if not re.fullmatch(NAME_PATTERN, value) or value in RESERVED:
        raise ValueError('名称需以中文或英文字母开头，最多 40 个字符，可含数字、_、-，且不能是保留名称')
    return value
