import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

SECRET_KEY = re.compile(r'(password|passwd|secret|token|api.?key|authorization|credential|cookie)', re.I)
INLINE_SECRET = re.compile(
    r'(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*)'
    r'((?:Bearer\s+)?[^\s,;&]+)')


def canonical_arguments(arguments: object) -> str:
    return json.dumps(arguments, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False)


def arguments_digest(arguments: object) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode()).hexdigest()


def _redact(value):
    if isinstance(value, dict):
        return {str(key): '<redacted>' if SECRET_KEY.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        result = []
        hide_next = False
        for item in value:
            result.append('<redacted>' if hide_next else _redact(item))
            hide_next = (isinstance(item, str) and item.startswith('-')
                         and bool(SECRET_KEY.search(item)) and '=' not in item)
        return result
    if isinstance(value, str):
        value = INLINE_SECRET.sub(r'\1<redacted>', value)
        if value.startswith(('http://', 'https://')):
            try:
                parsed = urlsplit(value)
            except ValueError:
                return '<invalid URL; see digest>'
            # URLs can embed basic auth or signed query credentials.
            host = parsed.netloc.rsplit('@', 1)[-1]
            query = '&'.join(
                part.split('=', 1)[0] + '=<redacted>'
                if SECRET_KEY.search(part.split('=', 1)[0]) else part
                for part in parsed.query.split('&'))
            value = urlunsplit((parsed.scheme, host, parsed.path, query, ''))
        if len(value) > 2048:
            return value[:2048] + f'… <{len(value)} characters; see digest>'
    return value


def arguments_preview(arguments: dict) -> str:
    return json.dumps({**_redact(arguments), '_arguments_sha256': arguments_digest(arguments)},
                      ensure_ascii=True, sort_keys=True)
