#!/usr/bin/env python3
"""Crawl all Confluence -> local tree + .md (stdlib only, no pip install).
Usage:
  set CONFLUENCE_BASE_URL=https://<site>.atlassian.net
  set CONFLUENCE_EMAIL=you@co.com
  set CONFLUENCE_API_TOKEN=xxxx   (Cloud API token; Server/DC: PAT or password)
  python crawl_confluence.py --space DEV,DOC   (optional filter, default: all spaces)
  python crawl_confluence.py --base-url https://x.atlassian.net --email a@b.c --token xyz -o out
  python crawl_confluence.py --selftest   (offline check, no network)
Output:  out/<SPACE_KEY>/<ancestor-id>/.../<page-id>/{content.md,metadata.json}  +  out/<SPACE_KEY>/TREE.md
# ponytail: naive storage-XHTML->md (no tables-nested/macros fidelity); swap in html2text/mistune if needed.
"""
import argparse, base64, datetime, html, json, os, re, sys, time, urllib.parse, urllib.request
from html.parser import HTMLParser

SEG_MAX = 50
WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}
SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WS_RE = re.compile(r'\s+')
EXPAND = 'ancestors,children.page,children.attachment,space,version,history,metadata.labels,body.storage'
_CQL_FMTS = ('%Y/%m/%d %H:%M', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
             '%Y-%m-%d %H:%M', '%Y-%m-%d')
def cql_since(space, since):
    # Poll incremental. Chuan hoa moi dang date ve CQL "yyyy/MM/dd HH:mm"; input la thi giu nguyen.
    s = str(since).strip().strip('"')
    for fmt in _CQL_FMTS:
        try:
            s = datetime.datetime.strptime(s, fmt).strftime('%Y/%m/%d %H:%M')
            break
        except ValueError:
            continue
    return f'type=page AND space="{space}" AND lastModified>"{s}" order by lastModified desc'
def sanitize(name, maxlen=SEG_MAX):
    name = html.unescape(WS_RE.sub(' ', name or 'untitled')).strip().strip('.')
    name = SANITIZE_RE.sub('_', name).strip()
    name = (name[:maxlen] or 'untitled').strip().rstrip('.')
    if name.upper() in WIN_RESERVED:
        name += '_'
    return name or 'untitled'
def fs(path):
    # Windows MAX_PATH bypass (\\?\ prefix); no-op elsewhere.
    if os.name == 'nt':
        ap = os.path.abspath(path)
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return path

class MD(HTMLParser):
    """Minimal storage-XHTML -> markdown. Unknown ac:* macros degrade to inner text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.o = []; self.lists = []; self.in_pre = False; self.pre_lang = ''
        self.link = None; self.in_th = False; self.row = None; self.cell = ''
        self.in_cell = False
    def txt(self, s):
        s = html.unescape(s)
        self.o.append(s if self.in_pre else s)
    def handle_starttag(self, t, a):
        a = dict(a)
        if t in ('h1','h2','h3','h4','h5','h6'): self.o.append('\n' + '#' * int(t[1]) + ' ')
        elif t == 'p': self.o.append('\n\n')
        elif t == 'br': self.o.append('  \n')
        elif t == 'hr': self.o.append('\n\n---\n\n')
        elif t in ('strong','b'): self.o.append('**')
        elif t in ('em','i'): self.o.append('*')
        elif t == 'code' and not self.in_pre: self.o.append('`')
        elif t in ('pre','ac:plain-text-body'): self.o.append('\n```' + self.pre_lang + '\n'); self.in_pre = True
        elif t == 'blockquote': self.o.append('\n> ')
        elif t in ('ul','ol'): self.lists.append((t, 0))
        elif t == 'li':
            if self.lists and self.lists[-1][0] == 'ol':
                k, n = self.lists[-1]; n += 1; self.lists[-1] = (k, n)
                self.o.append(f'\n{"  " * (len(self.lists)-1)}{n}. ')
            else: self.o.append(f'\n{"  " * (len(self.lists)-1) if self.lists else ""}- ')
        elif t == 'a':
            self.link = a.get('href', '') or ''
            # confluence internal link may hide in ac:xxx handled below; keep text, resolve at endtag
            self.o.append('[')
        elif t in ('ri:page','ac:link'): self.link = '@page:' + a.get('ri:content-title', a.get('ac:content-title', ''))
        elif t in ('ri:attachment',): self.link = '@file:' + a.get('ri:filename', '')
        elif t in ('img','ac:image','ri:url'):
            src = a.get('src') or a.get('ri:value') or a.get('ac:src') or ''
            alt = a.get('alt') or a.get('ri:filename') or 'image'
            self.o.append(f'\n![{alt}]({src})\n')
        elif t == 'table': self.o.append('\n')
        elif t == 'tr': self.row = []
        elif t in ('th','td'): self.in_cell = True; self.cell = ''; self.in_th = (t == 'th')
        elif t == 'ac:structured-macro':
            m = a.get('ac:name', '')
            if m == 'code': self.pre_lang = ''; self.o.append('\n```\n'); self.in_pre = True
            elif m in ('info','note','warning','tip'): self.o.append(f'\n> **{m.upper()}**: ')
        elif t == 'ac:parameter':
            if self.in_pre and a.get('ac:name') == 'language': self._lang_capture = True
            else: self._lang_capture = False
        elif t == 'time': self.o.append('`')
    def handle_endtag(self, t):
        if t in ('h1','h2','h3','h4','h5','h6','p'): self.o.append('\n')
        elif t in ('strong','b'): self.o.append('**')
        elif t in ('em','i'): self.o.append('*')
        elif t == 'code' and not self.in_pre: self.o.append('`')
        elif t in ('pre','ac:plain-text-body','ac:structured-macro'):
            if self.in_pre: self.o.append('\n```\n'); self.in_pre = False; self.pre_lang = ''
            else: self.o.append('\n')
        elif t in ('ul','ol'):
            if self.lists: self.lists.pop()
            self.o.append('\n')
        elif t == 'a' or t in ('ac:link',):
            url = self.link or ''
            # '[' was opened; last chunk is text -> wrap. Simplest: close bracket + url if external.
            self.o.append(f']({url})' if url and not url.startswith('@') else ']')
            self.link = None
        elif t in ('th','td'):
            self.in_cell = False
            if self.row is not None: self.row.append(('**' + self.cell.strip() + '**' if self.in_th else self.cell.strip()))
        elif t == 'tr':
            if self.row is not None:
                self.o.append('\n| ' + ' | '.join(self.row) + ' |')
                if not getattr(self, '_hdr_done', False):
                    self.o.append('\n|' + '|'.join([' --- '] * len(self.row)) + '|')
                    self._hdr_done = True
            self.row = None
        elif t == 'table': self._hdr_done = False; self.o.append('\n')
        elif t == 'blockquote': self.o.append('\n')
        elif t == 'time': self.o.append('`')
    def handle_data(self, d):
        if getattr(self, '_lang_capture', False): self.pre_lang = d.strip(); self._lang_capture = False; return
        if self.in_cell: self.cell += d
        elif d.strip() or self.in_pre: self.txt(d)
        elif d == ' ': self.o.append(' ')
    def result(self):
        md = ''.join(self.o)
        md = re.sub(r'\n{3,}', '\n\n', md).strip() + '\n'
        return md

def storage_to_md(xhtml):
    p = MD(); p.feed(xhtml or ''); p.close(); return p.result()

def auth_header(user, secret):
    if user:  # Basic: Cloud (email + API token) | Server/DC (username + password)
        raw = f'{user}:{secret}'.encode()
        return 'Basic ' + base64.b64encode(raw).decode()
    return 'Bearer ' + secret  # Server/DC PAT (chi can token, khong can user)

def api(base, path, headers, params=None, retries=5, timeout=30):
    url = base.rstrip('/') + path
    if params: url += '?' + urllib.parse.urlencode(params)
    for i in range(retries):
        r = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as h:
                return json.load(h)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(2 ** i); continue
            body = e.read().decode('utf-8', 'replace')[:500]
            sys.exit(f'HTTP {e.code} {url}\n{body}\n(Kiem tra base-url/user/token & quyen doc space)')
        except urllib.error.URLError as e:
            if i < retries - 1:
                time.sleep(2 ** i); continue
            sys.exit(f'Network error: {e}\nURL: {url}\n'
                     f'(Khong noi duoc toi server — loi mang, chua lien quan auth:\n'
                     f' 1) Mo thu {base}/status tren browser CUNG MAY (phai tra {{"state":"RUNNING"}})\n'
                     f' 2) Dung IP/port + context-path (/confluence, /wiki) nhu tren browser\n'
                     f' 3) VPN/mang noi bo, firewall, http vs https\n'
                     f' 4) Thu: curl -v {base}/status | Test-NetConnection -ComputerName <ip> -Port <port>)')

def paged(base, path, headers, params, key='results', timeout=30):
    start = 0; limit = int(params.pop('limit', 50))
    while True:
        d = api(base, path, headers, {**params, 'start': start, 'limit': limit}, timeout=timeout)
        items = d.get(key, [])
        for it in items: yield it
        if len(items) < limit: break
        start += limit

def selftest():
    assert sanitize('a/b:c*?') == 'a_b_c__', sanitize('  ')
    assert sanitize('CON') == 'CON_' and len(sanitize('x' * 99)) <= SEG_MAX
    assert not sanitize('y' * 60 + '. .').endswith(('.', ' '))
    assert (os.name != 'nt') or fs('out').startswith('\\\\?\\')
    md = storage_to_md('<h1>T</h1><p>Hello <strong>w</strong> <a href="https://x">l</a></p><ul><li>a</li></ul><table><tr><th>H</th></tr><tr><td>v</td></tr></table><pre>print(1)</pre>')
    for s in ('# T', '**w**', '[l](https://x)', '- a', '| **H** |', '```'):
        assert s in md, f'missing {s} in:\n{md}'
    fake = {'id': '123', 'title': 'T', 'version': {'number': 2}, 'body': {'storage': {'value': '<p>x</p>'}}}
    meta = dict(fake); meta['_export'] = {'space': 'DOC', 'url': 'http://x/pages/123', 'path': '1/123'}
    assert '"number": 2' in json.dumps(meta, ensure_ascii=False, indent=2) and 'pages/123' in json.dumps(meta)
    assert cql_since('DKB', '2026/09/21 00:00') == 'type=page AND space="DKB" AND lastModified>"2026/09/21 00:00" order by lastModified desc'
    assert cql_since('DKB', '2026-09-22T04:11:18Z') == 'type=page AND space="DKB" AND lastModified>"2026/09/22 04:11" order by lastModified desc'
    print('selftest OK')

def main():
    import urllib.error
    ap = argparse.ArgumentParser(description='Crawl Confluence -> tree .md (stdlib only)')
    ap.add_argument('--base-url', default=os.environ.get('CONFLUENCE_BASE_URL', ''), help='vd https://site.atlassian.net/wiki')
    ap.add_argument('--email', '--user', dest='user', default=os.environ.get('CONFLUENCE_EMAIL') or os.environ.get('CONFLUENCE_USER', ''), help='Cloud: email | Server/DC: username (co the bo trong neu dung PAT)')
    ap.add_argument('--token', '--password', dest='token', default=os.environ.get('CONFLUENCE_API_TOKEN') or os.environ.get('CONFLUENCE_TOKEN') or os.environ.get('CONFLUENCE_PASSWORD', ''), help='Cloud: API token | Server/DC: password hoac PAT')
    ap.add_argument('--space', default=os.environ.get('CONFLUENCE_SPACES', ''), help='LOC nhau boi dau phay, mac dinh: tat ca')
    ap.add_argument('-o', '--output', default=os.environ.get('CONFLUENCE_OUTPUT', 'confluence_export'))
    ap.add_argument('--attachments', action='store_true', help='tai kem file dinh kem')
    ap.add_argument('--resume', action='store_true', help='bo qua page da co san metadata.json + content.md')
    ap.add_argument('--timeout', type=int, default=int(os.environ.get('CONFLUENCE_TIMEOUT', '30')), help='timeout giay moi request (default 30)')
    ap.add_argument('--since', default=os.environ.get('CONFLUENCE_SINCE', ''), help='chi lay page sua sau moc nay, vd "2026/09/21 00:00" (poll incremental, khong can admin)')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest: return selftest()
    if not a.base_url or not a.token: sys.exit('Thieu --base-url / --token (hoac env CONFLUENCE_BASE_URL, CONFLUENCE_API_TOKEN)')
    H = {'Authorization': auth_header(a.user, a.token), 'Accept': 'application/json'}

    spaces = [s.strip() for s in a.space.split(',') if s.strip()] or \
             [s['key'] for s in paged(a.base_url, '/rest/api/space', H, {'limit': 50, 'type': 'global'}, timeout=a.timeout)]
    print(f'{len(spaces)} spaces: {", ".join(spaces)}')

    for sk in spaces:
        if a.since:  # poll incremental qua CQL search — user thuong chay duoc, khong can admin
            pages = list(paged(a.base_url, '/rest/api/content/search', H,
                {'cql': cql_since(sk, a.since), 'expand': EXPAND, 'limit': 50}, timeout=a.timeout))
        else:
            pages = list(paged(a.base_url, '/rest/api/content', H,
                {'spaceKey': sk, 'type': 'page', 'status': 'current',
                 'expand': EXPAND, 'limit': 50, 'orderBy': 'id'}, timeout=a.timeout))
        by_id = {p['id']: p for p in pages}
        # path tu ancestors (chi dung title, sort id tang dan = root->leaf)
        def chain(p):
            anc = sorted([x for x in p.get('ancestors', []) if x['id'] in by_id], key=lambda x: int(x['id']))
            return anc + [p]
        root = os.path.join(a.output, sanitize(sk)); os.makedirs(fs(root), exist_ok=True)
        tree_lines = [f'# {sk} ({len(pages)} pages)', '']
        print(f'[{sk}] fetched {len(pages)} pages, writing...', flush=True)
        for n, p in enumerate(sorted(pages, key=lambda x: int(x['id'])), 1):
            ids = [t['id'] for t in chain(p)]  # cay thu muc chi bang id
            d = os.path.join(root, *ids); os.makedirs(fs(d), exist_ok=True)
            url = a.base_url.rstrip('/') + '/pages/' + p['id']
            depth = len(ids) - 1
            tree_lines.append(f"{'  ' * depth}- [{p['title']}]({'/'.join(ids + ['content.md'])})")
            mfile, cfile = os.path.join(d, 'metadata.json'), os.path.join(d, 'content.md')
            skipped = a.resume and os.path.exists(fs(mfile)) and os.path.exists(fs(cfile))
            if not skipped:  # da crawl roi thi chi ghi TREE
                meta = dict(p)
                meta['_export'] = {'space': sk, 'url': url, 'path': '/'.join(ids),
                                   'crawled_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                with open(fs(mfile), 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                body = storage_to_md((p.get('body') or {}).get('storage', {}).get('value', ''))
                with open(fs(cfile), 'w', encoding='utf-8') as f:
                    f.write(f"---\nid: {p['id']}\ntitle: {json.dumps(p['title'], ensure_ascii=False)}\nurl: {url}\n---\n\n# {p['title']}\n\n{body}")
            if n % 100 == 0 or n == len(pages):
                print(f'[{sk}] {n}/{len(pages)}', flush=True)
            if a.attachments and not skipped:
                att_dir = os.path.join(d, '_attachments'); os.makedirs(fs(att_dir), exist_ok=True)
                for at in paged(a.base_url, f"/rest/api/content/{p['id']}/child/attachment", H, {'limit': 50}, timeout=a.timeout):
                    dl = (at.get('_links') or {}).get('download', '')
                    if not dl: continue
                    dest = os.path.join(att_dir, sanitize(at.get('title', 'file')))
                    if os.path.exists(fs(dest)): continue
                    r = urllib.request.Request(a.base_url.rstrip('/') + dl, headers=H)
                    with urllib.request.urlopen(r, timeout=120) as h, open(fs(dest), 'wb') as f:
                        f.write(h.read())
        with open(fs(os.path.join(root, 'TREE.md')), 'w', encoding='utf-8') as f:
            f.write('\n'.join(tree_lines) + '\n')
        print(f'[{sk}] {len(pages)} pages -> {root}')

if __name__ == '__main__':
    main()
