"""Runtime settings. Single source of truth: environment variables.

Confluence credentials intentionally reuse the same names as
``crawl_confluence.py`` so the script and the service share one env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(int(default))).strip().lower() not in ('0', '', 'false', 'no')


@dataclass(frozen=True)
class Settings:
    """Flat, immutable config. No validation on build; fail fast at use site."""

    base_url: str = ''
    user: str = ''
    token: str = ''
    spaces: tuple[str, ...] = ()
    output: str = 'confluence_export'
    timeout: int = 30
    db_path: str = 'data/app.db'
    event_key: str = ''  # shared secret for POST /v1/events (?key=); empty = accept all on LAN
    event_workers: int = 2  # parallel single-page fetches from the event queue
    attachments: bool = False
    host: str = '127.0.0.1'
    port: int = 8000
    script: str = field(default='crawl_confluence.py')  # crawl worker; port in-process later

    @classmethod
    def from_env(cls) -> 'Settings':
        spaces = tuple(s.strip() for s in os.environ.get('CONFLUENCE_SPACES', '').split(',') if s.strip())
        return cls(
            base_url=os.environ.get('CONFLUENCE_BASE_URL', '').rstrip('/'),
            user=os.environ.get('CONFLUENCE_EMAIL') or os.environ.get('CONFLUENCE_USER', ''),
            token=os.environ.get('CONFLUENCE_API_TOKEN')
            or os.environ.get('CONFLUENCE_TOKEN')
            or os.environ.get('CONFLUENCE_PASSWORD', ''),
            spaces=spaces,
            output=os.environ.get('CONFLUENCE_OUTPUT', 'confluence_export'),
            timeout=_int('CONFLUENCE_TIMEOUT', 30),
            db_path=os.environ.get('APP_DB', 'data/app.db'),
            event_key=os.environ.get('EVENT_KEY', ''),
            event_workers=_int('EVENT_WORKERS', 2),
            attachments=_bool('CONFLUENCE_ATTACHMENTS', False),
            host=os.environ.get('APP_HOST', '127.0.0.1'),
            port=_int('APP_PORT', 8000),
        )

    def require_confluence(self) -> None:
        """Fail fast before spawning the crawl worker with no credentials."""
        missing = [n for n, v in (('base-url', self.base_url), ('token', self.token)) if not v]
        if missing:
            raise RuntimeError(f'Missing Confluence {", ".join(missing)} '
                               f'(env CONFLUENCE_BASE_URL / CONFLUENCE_API_TOKEN)')
