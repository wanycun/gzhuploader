#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzh-uploader: NAS-side WeChat Official Account draft uploader.
Usage: python3 uploader.py <package_dir> [--dry-run]
Package dir: article.md (with --- front matter: title, digest) + cover + images.
Pure stdlib. Config at /vol1/@appdata/gzh-uploader/config.json {appid, secret}.
Prints ONE JSON result line to stdout.
"""
import json, os, re, sys, time, uuid, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'config.json')
TOKEN_PATH = os.path.join(BASE, 'token.json')

# fpk bundle: data dir via target/data symlink (installed layout)
_d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
if os.path.isdir(_d):
    BASE = _d
    CFG_PATH = os.path.join(_d, 'config.json')
    TOKEN_PATH = os.path.join(_d, 'token.json')

LOG_PATH = os.path.join(BASE, 'runs.log')
API = 'https://api.weixin.qq.com'

def log(msg):
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except Exception:
        pass

def out(d):
    print(json.dumps(d, ensure_ascii=False))
    sys.exit(0 if d.get('ok') else 3)

def http_json(url, data=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))

def http_multipart(url, filepath):
    boundary = '----gzh' + uuid.uuid4().hex
    name = os.path.basename(filepath)
    with open(filepath, 'rb') as f:
        payload = f.read()
    body = ('--%s\r\nContent-Disposition: form-data; name="media"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (boundary, name)).encode('utf-8') \
           + payload + ('\r\n--%s--\r\n' % boundary).encode('utf-8')
    req = urllib.request.Request(url, data=body,
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))

def egress_ip():
    for u in ('https://myip.ipip.net', 'http://ip.3322.net'):
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'curl/8'})
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = r.read().decode('utf-8', 'ignore')
            m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', txt)
            if m:
                return m.group(1)
        except Exception:
            pass
    return None

def get_token(cfg, force=False):
    if not force and os.path.exists(TOKEN_PATH):
        try:
            t = json.load(open(TOKEN_PATH))
            if t.get('expires_at', 0) > time.time() + 300:
                return t['access_token']
        except Exception:
            pass
    q = urllib.parse.urlencode({'grant_type': 'client_credential',
                                'appid': cfg['appid'], 'secret': cfg['secret']})
    r = http_json(API + '/cgi-bin/token?' + q)
    if 'access_token' not in r:
        raise RuntimeError('token failed: %s' % json.dumps(r, ensure_ascii=False))
    json.dump({'access_token': r['access_token'],
               'expires_at': time.time() + r.get('expires_in', 7200)},
              open(TOKEN_PATH, 'w'))
    return r['access_token']

# ---------- markdown -> wechat html (electric-blueprint accents) ----------
S_WRAP = 'font-size:16px;line-height:1.9;color:#2f3542;letter-spacing:0.4px;'
S_H2 = 'font-size:19px;font-weight:bold;color:#0a2a5e;border-left:4px solid #1e6fd9;padding-left:10px;margin:30px 0 14px;line-height:1.5;'
S_H3 = 'font-size:17px;font-weight:bold;color:#0a2a5e;margin:24px 0 10px;line-height:1.5;'
S_P = 'margin:0 0 16px;'
S_IMG = 'width:100%;display:block;margin:18px 0;'
S_Q = 'background:#eef4ff;border-left:4px solid #1e6fd9;color:#33415c;padding:12px 16px;margin:18px 0;'
S_LI = 'margin:0 0 8px;'
S_STRONG = 'color:#0a2a5e;'

def inline(t):
    t = t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong style="%s">\1</strong>' % S_STRONG, t)
    t = re.sub(r'`([^`]+)`', r'\1', t)
    t = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', t)
    return t

def md_to_html(md):
    lines = md.split('\n')
    html, i = [], 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r'!\[(.*?)\]\((.*?)\)', ln.strip())
        if m:
            html.append('<img style="%s" src="%s" alt="%s"/>' % (S_IMG, m.group(2), m.group(1)))
            i += 1; continue
        if ln.startswith('### '):
            html.append('<h3 style="%s">%s</h3>' % (S_H3, inline(ln[4:]))); i += 1; continue
        if ln.startswith('## '):
            html.append('<h2 style="%s">%s</h2>' % (S_H2, inline(ln[3:]))); i += 1; continue
        if ln.startswith('# '):
            html.append('<h2 style="%s">%s</h2>' % (S_H2, inline(ln[2:]))); i += 1; continue
        if ln.strip() in ('---', '***'):
            html.append('<p style="text-align:center;color:#1e6fd9;margin:22px 0;">· · ·</p>'); i += 1; continue
        if ln.startswith('> '):
            html.append('<section style="%s">%s</section>' % (S_Q, inline(ln[2:]))); i += 1; continue
        if re.match(r'\s*[-*] ', ln):
            items = []
            while i < len(lines) and re.match(r'\s*[-*] ', lines[i]):
                items.append('<p style="%s">· %s</p>' % (S_LI, inline(re.sub(r'\s*[-*] ', '', lines[i])))); i += 1
            html.extend(items); continue
        if ln.strip() == '':
            i += 1; continue
        para = [ln]
        i += 1
        while i < len(lines) and lines[i].strip() != '' and not re.match(r'(!\[|#{1,3} |> |---)', lines[i]):
            para.append(lines[i]); i += 1
        html.append('<p style="%s">%s</p>' % (S_P, inline(''.join(para))))
    return '<section style="%s">%s</section>' % (S_WRAP, '\n'.join(html))

def parse_pkg(pkg):
    md_path = os.path.join(pkg, 'article.md')
    raw = open(md_path, encoding='utf-8').read()
    fm = {}
    body = raw
    m = re.match(r'^---\n(.*?)\n---\n', raw, re.S)
    if m:
        for kv in m.group(1).split('\n'):
            if ':' in kv:
                k, v = kv.split(':', 1)
                fm[k.strip()] = v.strip().strip('"').strip('：')
        body = raw[m.end():]
    imgs = re.findall(r'!\[(?:.*?)\]\((.*?)\)', body)
    return fm, body, imgs

def cmd_state():
    """状态模式：供桌面页 index.cgi 调用，输出 JSON。"""
    cfgp = CFG_PATH
    cfg_ok = False
    try:
        c = json.load(open(cfgp))
        cfg_ok = bool(c.get('secret')) and c.get('secret') != 'SECRET_HERE'
    except Exception:
        pass
    jobs = []
    jd = os.path.join(BASE, 'jobs')
    if os.path.isdir(jd):
        jobs = sorted(os.listdir(jd))
    runs = []
    lp = os.path.join(BASE, 'runs.log')
    if os.path.exists(lp):
        with open(lp, errors='ignore') as f:
            runs = [l.rstrip() for l in f.readlines()[-8:]]
    drafts = None
    if cfg_ok:
        try:
            tok = get_token(json.load(open(cfgp)))
            r = http_json(API + '/cgi-bin/draft/batchget?access_token=' + tok,
                          {'offset': 0, 'count': 20, 'no_content': 1})
            drafts = r.get('total_count')
        except Exception:
            drafts = None
    out({'ok': True, 'config_ok': cfg_ok, 'egress_ip': egress_ip(),
         'jobs': jobs, 'recent_runs': runs, 'draft_count': drafts})


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry = '--dry-run' in sys.argv
    if not args:
        out({'ok': False, 'stage': 'args', 'error': 'no package dir'})
    pkg = args[0]
    ip = egress_ip()
    fm, body, imgs = parse_pkg(pkg)
    missing = [im for im in imgs if not os.path.exists(os.path.join(pkg, im))]
    cover = fm.get('cover', 'cover.png')
    if not os.path.exists(os.path.join(pkg, cover)):
        missing.append(cover)
    if missing:
        out({'ok': False, 'stage': 'package', 'missing': missing, 'egress_ip': ip})
    html = md_to_html(body)
    _ch = os.path.join(pkg, 'content.html')
    if os.path.exists(_ch):
        html = open(_ch, encoding='utf-8').read()
        imgs = [x for x in re.findall(r'src="([^"]+)"', html) if not x.startswith('http')]
    _off = int(fm.get('content_offset', '0') or 0)
    if _off:
        html = html[_off:]
        imgs = [x for x in re.findall(r'src="([^"]+)"', html) if not x.startswith('http')]
        plain_len = len(re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', html)))
        print(json.dumps({'offset_applied': _off, 'remaining_plain_len': plain_len}, ensure_ascii=False), file=sys.stderr)
    if dry:
        p = os.path.join(pkg, 'typeset.html')
        open(p, 'w', encoding='utf-8').write(html)
        out({'ok': True, 'stage': 'dry-run', 'title': fm.get('title'),
             'images': imgs, 'cover': cover, 'html_bytes': len(html.encode('utf-8')),
             'egress_ip': ip, 'typeset': p})
    cfg = json.load(open(CFG_PATH))
    if not cfg.get('secret') or cfg['secret'] == 'SECRET_HERE':
        out({'ok': False, 'stage': 'config', 'error': 'AppSecret not filled in config.json',
             'egress_ip': ip})
    try:
        token = get_token(cfg)
        # article images -> wechat urls
        for im in imgs:
            r = http_multipart(API + '/cgi-bin/media/uploadimg?access_token=' + token,
                               os.path.join(pkg, im))
            if 'url' not in r:
                if r.get('errcode') in (40001, 42001, 40014):
                    token = get_token(cfg, force=True)
                    r = http_multipart(API + '/cgi-bin/media/uploadimg?access_token=' + token,
                                       os.path.join(pkg, im))
            if 'url' not in r:
                out({'ok': False, 'stage': 'uploadimg', 'image': im,
                     'wechat': r, 'egress_ip': ip})
            html = html.replace('src="%s"' % im, 'src="%s"' % r['url'])
        # cover -> permanent material
        r = http_multipart(API + '/cgi-bin/material/add_material?access_token=%s&type=image' % token,
                           os.path.join(pkg, cover))
        if 'media_id' not in r:
            out({'ok': False, 'stage': 'cover', 'wechat': r, 'egress_ip': ip})
        digest = fm.get('digest', '')
        if not digest:
            plain = re.sub(r'[#>*`\-\[\]!()]', '', body)
            plain = re.sub(r'\s+', '', plain)
            digest = plain[:100]
        draft = {'articles': [{
            'title': fm.get('title', 'untitled'),
            'author': fm.get('author', 'CHUN'),
            'digest': digest,
            'content': html,
            'content_source_url': '',
            'thumb_media_id': r['media_id'],
            'need_open_comment': 1,
            'only_fans_can_comment': 0,
        }]}
        upd = None
        if '--update' in sys.argv:
            upd = sys.argv[sys.argv.index('--update') + 1]
        if upd:
            payload = {"media_id": upd, "index": 0, "articles": draft["articles"][0]}
            print(json.dumps({'payload_bytes': len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 'content_chars': len(payload['articles'][0]['content'])}, ensure_ascii=False), file=sys.stderr)
            r = http_json(API + '/cgi-bin/draft/update?access_token=' + token, payload)
            if r.get('errcode') != 0:
                out({'ok': False, 'stage': 'draft_update', 'wechat': r, 'egress_ip': ip})
            log('OK pkg=%s updated draft_media_id=%s ip=%s' % (pkg, upd, ip))
            out({'ok': True, 'stage': 'updated', 'draft_media_id': upd,
                 'title': fm.get('title'), 'egress_ip': ip})
        r = http_json(API + '/cgi-bin/draft/add?access_token=' + token, draft)
        if 'media_id' not in r:
            out({'ok': False, 'stage': 'draft_add', 'wechat': r, 'egress_ip': ip})
        log('OK pkg=%s draft_media_id=%s ip=%s' % (pkg, r['media_id'], ip))
        out({'ok': True, 'stage': 'done', 'draft_media_id': r['media_id'],
             'title': fm.get('title'), 'egress_ip': ip})
    except Exception as e:
        log('FAIL pkg=%s err=%s' % (pkg, e))
        out({'ok': False, 'stage': 'api', 'error': str(e), 'egress_ip': ip})

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'state':
        cmd_state()
    else:
        main()
